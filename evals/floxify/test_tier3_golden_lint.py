#!/usr/bin/env python3
"""Golden-manifest lint for the Tier-3 stretch goldens (AI-431).

Sibling of test_golden_lint.py, which lints the Tier-2 real-repo goldens
under testdata/gold/. This one lints the Tier-3 known-hard / conversion-
mode goldens under gold/ — the reference manifests the Tier-3 judge grades
produced output against — with the SAME verify.py checker and the SAME
whole-manifest lock-resolution leg, so a Tier-3 gold with a hallucinated
pkg-path, a version/system that does not resolve, a non-literal [vars], a
tree-mutating hook, or packages that cannot co-resolve together is caught
deterministically, no `claude` and no agent run required.

Only the goldens named in tier3.jsonl are linted here (not every file in
gold/): the six original Tier-1 goldens predate this check and are out of
scope for AI-431 — adding them is a separate change, exactly as AI-456/457
scoped the Tier-2 goldens.

It reuses `verify.verify` (manifest-only checks — [vars] literalness,
hook-mutation, catalog resolution; the detect-cross-check invariants
degrade to no-ops on the empty `{}` facts these vendored-repo-free goldens
pass, same as test_golden_lint.py) and the lock-resolution leg
(`_attempt_lock` and its retry/transient-vs-resolution classification)
from test_golden_lint.py rather than re-deriving them.

Catalog + lock legs need `flox` + network. Same two-way discipline as
test_golden_lint.py, sharing its FLOXIFY_GOLDEN_LINT_LIVE_CATALOG switch
so the CI flox-less step disables both consistently:

  - FLOXIFY_GOLDEN_LINT_LIVE_CATALOG=0 forces no-network (catalog + lock
    legs skip);
  - unset/"1" runs them live when `flox` is on PATH.

Run:
    python3 -m unittest test_tier3_golden_lint -v
    FLOXIFY_GOLDEN_LINT_LIVE_CATALOG=0 python3 -m unittest test_tier3_golden_lint -v
"""
import json
import os
import shutil
import unittest
from pathlib import Path

from _skill_module_loader import load_module

# Lock-resolution leg + its status constants, reused from the Tier-2 golden
# lint (they operate on raw manifest text — nothing Tier-2-specific).
from test_golden_lint import (
    FLOX_BIN,
    LOCK_OK,
    LOCK_RESOLUTION_ERROR,
    _attempt_lock,
)

HERE = Path(__file__).resolve().parent
GOLD_DIR = HERE / "gold"
TIER3_FILE = HERE / "tier3.jsonl"
VERIFY = HERE.parent.parent / "flox-plugin" / "skills" / "floxify" / "scripts" / "verify.py"

# Unique sys.modules key — the loader's docstring warns that sharing a key
# across test modules lets whichever load ran last steal the other's
# @patch target. test_golden_lint.py uses "verify_under_test_golden_lint";
# this must differ.
verify_mod = load_module(VERIFY, sys_modules_key="verify_under_test_tier3_golden_lint")
verify = verify_mod.verify

LIVE_CATALOG = os.environ.get("FLOXIFY_GOLDEN_LINT_LIVE_CATALOG", "1") != "0"


def _tier3_ids():
    ids = [
        json.loads(line)["id"]
        for line in TIER3_FILE.read_text().splitlines()
        if line.strip()
    ]
    return sorted(ids)


_TIER3_IDS = _tier3_ids()
# A path typo or an emptied registry must fail loudly at collection time,
# not silently report "0 tests, all passed."
assert _TIER3_IDS, f"no Tier-3 ids found in {TIER3_FILE} — check the path"


class TestTier3GoldenLint(unittest.TestCase):
    """One lint + one lock test per Tier-3 golden, so a failure names the
    exact fixture."""

    def _lint(self, fixture_id):
        gold = GOLD_DIR / f"{fixture_id}.toml"
        self.assertTrue(gold.is_file(), f"missing gold/{fixture_id}.toml")
        manifest_text = gold.read_text(encoding="utf-8")
        # No detect facts for these goldens (the fixture repo is not passed
        # here) — manifest-only checks only, same as test_golden_lint.py.
        result = verify({}, manifest_text, check_catalog_live=LIVE_CATALOG)
        hard = verify_mod.hard_violations(result)
        if hard:
            detail = "\n".join(f"  [{v['rule']}] {v['message']}" for v in hard)
            self.fail(
                f"{fixture_id}.toml has {len(hard)} HARD violation(s) — a "
                f"Tier-3 gold must be clean (unlike the Tier-2 goldens, "
                f"there is no KNOWN_VIOLATIONS allowlist here; fix the "
                f"golden):\n{detail}"
            )

    def _lock(self, fixture_id, live_catalog=None, flox_available=None):
        """Whole-manifest lock-resolution leg. `live_catalog`/
        `flox_available` are test-only overrides for the skip conditions
        (default to the real env state), mirroring test_golden_lint.py."""
        live = LIVE_CATALOG if live_catalog is None else live_catalog
        if not live:
            self.skipTest(
                "FLOXIFY_GOLDEN_LINT_LIVE_CATALOG=0 — lock leg needs live "
                "flox+network"
            )
        available = (
            bool(shutil.which(FLOX_BIN)) if flox_available is None else flox_available
        )
        if not available:
            self.skipTest("flox not on PATH — cannot attempt lock resolution")

        manifest_text = (GOLD_DIR / f"{fixture_id}.toml").read_text(encoding="utf-8")
        status, message, elapsed = _attempt_lock(manifest_text)
        print(f"  [lock] {fixture_id}: {status} in {elapsed:.2f}s", flush=True)
        if status == LOCK_OK:
            return
        if status == LOCK_RESOLUTION_ERROR:
            self.fail(
                f"{fixture_id}.toml FAILED whole-manifest lock resolution "
                f"({elapsed:.2f}s) — its packages resolve individually but "
                f"cannot co-resolve together on any single catalog page. "
                f"This is a REAL finding: fix the golden (split pkg-groups "
                f"or relax a pin), do not ignore it. Resolver output:\n{message}"
            )
        # LOCK_TRANSIENT_ERROR, already retried inside _attempt_lock.
        self.fail(
            f"{fixture_id}.toml's lock attempt hit an environment/catalog "
            f"error, likely transient, on both tries ({elapsed:.2f}s) — the "
            f"resolver never reported 'resolution failed:', so this is NOT a "
            f"co-resolution defect. Re-run before treating it as a finding. "
            f"Raw output:\n{message}"
        )

    def test_catalog_leg_ran_when_expected(self):
        """Distinguishes 'genuinely clean' from 'silently skipped' — the
        same guard test_golden_lint.py carries."""
        sample = (GOLD_DIR / f"{_TIER3_IDS[0]}.toml").read_text(encoding="utf-8")
        result = verify({}, sample, check_catalog_live=LIVE_CATALOG)
        if LIVE_CATALOG:
            if shutil.which("flox"):
                self.assertTrue(
                    result["catalog_checked"],
                    "flox is on PATH and the live catalog leg was requested, "
                    "but catalog_checked=False — every golden would pass "
                    "trivially instead of genuinely linting the catalog",
                )
            # else: flox genuinely absent — acceptable ambient skip.
        else:
            self.assertFalse(
                result["catalog_checked"],
                "FLOXIFY_GOLDEN_LINT_LIVE_CATALOG=0 must guarantee no network "
                "calls, but catalog_checked=True",
            )


def _make_lint_test(fixture_id):
    def test(self):
        self._lint(fixture_id)
    test.__name__ = f"test_{fixture_id.replace('-', '_')}_has_no_violations"
    return test


def _make_lock_test(fixture_id):
    def test(self):
        self._lock(fixture_id)
    test.__name__ = f"test_{fixture_id.replace('-', '_')}_locks_cleanly"
    return test


for _fixture_id in _TIER3_IDS:
    setattr(TestTier3GoldenLint, _make_lint_test(_fixture_id).__name__,
            _make_lint_test(_fixture_id))
    setattr(TestTier3GoldenLint, _make_lock_test(_fixture_id).__name__,
            _make_lock_test(_fixture_id))


if __name__ == "__main__":
    unittest.main()
