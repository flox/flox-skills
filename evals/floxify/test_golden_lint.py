#!/usr/bin/env python3
"""Golden-manifest lint: run verify.py's checker over every hand-curated
reference in testdata/gold/*.toml (AI-456 item 2).

These manifests are the reference every produced manifest is judged
against, and until now nothing had ever linted them — two hand reviews
(AI-455) found real defects. This is a cheap, unit-test-tier check: no
`claude`, no agent, no cloned repo. There is no detect.json for these
repos (they aren't vendored — cloning all eight just to lint golden TOML
would defeat the point of a cheap tier), so only the manifest-only checks
run: [vars] literalness, hook-mutation, and catalog resolution/version/
per-system availability. The detect-cross-check invariants (runtime
installed, leaf-datastore served) need facts this tier doesn't have and
are exercised instead by test_verify.py and the harness (run_floxify.py).

Catalog checks need `flox` + network. Two ways this stays no-network by
default rather than ambient-only:

  - Set FLOXIFY_GOLDEN_LINT_LIVE_CATALOG=0 to force `check_catalog_live=
    False` explicitly — CI's flox-less free-tests step does this, so its
    "no network" guarantee doesn't rely on `flox` merely happening to be
    absent from PATH.
  - Left unset (or "1"), the catalog leg runs live when `flox` IS on
    PATH (the default for a local run, and for CI's flox-equipped
    floxify-evals job, which is where this check has real teeth — see
    .github/workflows/evals.yml).

test_catalog_leg_ran_when_expected asserts catalog_checked matches
whichever mode was requested, so a silent skip (e.g. a future bug that
makes check_catalog bail out even with flox present) can't masquerade as
"genuinely clean."

KNOWN_VIOLATIONS is an explicit allowlist, one entry per current golden
defect, each tagged AI-457 (the follow-up that fixes the goldens — do NOT
fix golden content in this change, per the AI-461 ticket). Entries match
the violation's structured `pkg_path` field EXACTLY, not a substring of
the message — short needles like "uv" or "deno" would otherwise collide
with unrelated text a message might contain in the future. A dedicated
test (test_known_violations_allowlist_has_no_stale_entries) asserts every
entry still corresponds to a live violation, so AI-457 fixing a golden
without removing its entry doesn't leave a dead allowlist slot that could
silently absorb an unrelated future regression.

Run:
    python3 test_golden_lint.py
    pytest test_golden_lint.py
    FLOXIFY_GOLDEN_LINT_LIVE_CATALOG=0 python3 test_golden_lint.py   # no network
"""
import os
import shutil
import unittest
from pathlib import Path

from _skill_module_loader import load_module

HERE = Path(__file__).resolve().parent
GOLD_DIR = HERE / "testdata" / "gold"
VERIFY = HERE.parent.parent / "flox-plugin" / "skills" / "floxify" / "scripts" / "verify.py"

# Unique sys.modules key — see _skill_module_loader.py's docstring for the
# incident this avoids (test_verify.py loads the same verify.py under ITS
# own unique key; sharing a key let whichever load ran last silently
# steal the other's @patch target).
verify_mod = load_module(VERIFY, sys_modules_key="verify_under_test_golden_lint")
verify = verify_mod.verify

LIVE_CATALOG = os.environ.get("FLOXIFY_GOLDEN_LINT_LIVE_CATALOG", "1") != "0"

# (fixture id, rule, pkg-path) -> tracking ticket. `pkg-path` is matched
# EXACTLY against the violation's structured `pkg_path` field.
#
# Populated from a live `flox show` run against nixpkgs on 2026-07-16 (see
# the AI-461 PR description for that snapshot) and burned down to empty by
# AI-457, which fixed every golden's content instead of leaving it
# allowlisted. Kept as an empty dict rather than deleted: the stale-entry
# test (test_known_violations_allowlist_has_no_stale_entries) and every
# `_is_allowlisted` call still need the symbol to exist. New entries here
# should be rare and always tagged with the ticket that will resolve them.
KNOWN_VIOLATIONS = {}


def _matches(fixture_id, v, key):
    fid, rule, pkg_path = key
    return fid == fixture_id and rule == v["rule"] and v.get("pkg_path") == pkg_path


def _is_allowlisted(fixture_id, v):
    return any(_matches(fixture_id, v, key) for key in KNOWN_VIOLATIONS)


def _gold_ids():
    return sorted(p.stem for p in GOLD_DIR.glob("*.toml"))


_GOLD_IDS = _gold_ids()
# A path typo or refactor that empties this glob must fail loudly at
# collection time, not silently report "0 tests, all passed."
assert _GOLD_IDS, f"no golden manifests found under {GOLD_DIR} — check the path"


class TestGoldenLint(unittest.TestCase):
    """One test per golden so a failure names the exact fixture."""

    def _lint(self, fixture_id):
        manifest_text = (GOLD_DIR / f"{fixture_id}.toml").read_text(encoding="utf-8")
        # No detect facts for these repos (not vendored) -- manifest-only
        # checks only; see module docstring.
        result = verify({}, manifest_text, check_catalog_live=LIVE_CATALOG)
        hard = verify_mod.hard_violations(result)
        unlisted = [v for v in hard if not _is_allowlisted(fixture_id, v)]
        if unlisted:
            detail = "\n".join(f"  [{v['rule']}] {v['message']}" for v in unlisted)
            self.fail(
                f"{fixture_id}.toml has {len(unlisted)} violation(s) not in "
                f"KNOWN_VIOLATIONS (new regression, or the allowlist needs "
                f"an AI-457-tagged entry):\n{detail}"
            )

    def test_catalog_leg_ran_when_expected(self):
        """Distinguishes 'genuinely clean' from 'silently skipped.'"""
        sample = (GOLD_DIR / f"{_GOLD_IDS[0]}.toml").read_text(encoding="utf-8")
        result = verify({}, sample, check_catalog_live=LIVE_CATALOG)
        if LIVE_CATALOG:
            if shutil.which("flox"):
                self.assertTrue(
                    result["catalog_checked"],
                    "flox is on PATH and the live catalog leg was requested, "
                    "but catalog_checked=False — every golden would pass "
                    "trivially instead of genuinely linting the catalog",
                )
            # else: flox genuinely absent from this environment -- an
            # acceptable ambient skip, same as the harness's own
            # activation check.
        else:
            self.assertFalse(
                result["catalog_checked"],
                "FLOXIFY_GOLDEN_LINT_LIVE_CATALOG=0 must guarantee no "
                "network calls, but catalog_checked=True",
            )

    def test_known_violations_allowlist_has_no_stale_entries(self):
        """Every KNOWN_VIOLATIONS entry must still match a live violation.

        Otherwise AI-457 could fix a golden, leave the entry behind, and
        a future unrelated regression that happens to match the same
        (fixture, rule, pkg_path) triple would be silently allowlisted.
        """
        if not LIVE_CATALOG:
            self.skipTest("stale-allowlist check needs the live catalog leg")
        if not shutil.which("flox"):
            self.skipTest("flox not on PATH — cannot verify allowlist freshness")

        consumed = set()
        for fixture_id in _GOLD_IDS:
            manifest_text = (GOLD_DIR / f"{fixture_id}.toml").read_text(encoding="utf-8")
            result = verify({}, manifest_text, check_catalog_live=True)
            if not result["catalog_checked"]:
                self.skipTest(f"catalog leg did not run for {fixture_id}")
            for v in verify_mod.hard_violations(result):
                for key in KNOWN_VIOLATIONS:
                    if _matches(fixture_id, v, key):
                        consumed.add(key)

        stale = set(KNOWN_VIOLATIONS) - consumed
        if stale:
            detail = "\n".join(
                f"  {key} ({KNOWN_VIOLATIONS[key]})" for key in sorted(stale)
            )
            self.fail(
                f"{len(stale)} KNOWN_VIOLATIONS entr"
                f"{'y' if len(stale) == 1 else 'ies'} no longer match any live "
                f"violation — fixed upstream? Remove the entry so AI-457's "
                f"burn-down stays visible:\n{detail}"
            )


def _make_test(fixture_id):
    def test(self):
        self._lint(fixture_id)
    test.__name__ = f"test_{fixture_id.replace('-', '_')}_has_no_unlisted_violations"
    return test


for _fixture_id in _GOLD_IDS:
    setattr(TestGoldenLint, _make_test(_fixture_id).__name__, _make_test(_fixture_id))


if __name__ == "__main__":
    unittest.main()
