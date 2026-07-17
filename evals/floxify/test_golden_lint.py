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

Whole-manifest lock-resolution leg (AI-479): the checks above are all
PER-PACKAGE (does this one pkg-path/version/system resolve) — none of
them can see a manifest whose packages each resolve individually but
cannot co-resolve TOGETHER on any single catalog page
("constraints for group 'X' are too tight"). AI-457 and AI-478 only
caught that class by hand, running `flox activate` themselves. This adds
one more per-golden test, `test_<fixture>_locks_cleanly`, that attempts
a real `flox edit -f` (resolve-only, never realizes the closure — orders
of magnitude cheaper than `flox activate`) in a throwaway environment.
Same skip discipline as the catalog leg above: advisory-skip when `flox`
is absent or `FLOXIFY_GOLDEN_LINT_LIVE_CATALOG=0`, never gating the
flox-less free-tier step. When it DOES run, a resolution failure is a
real finding on that golden — report it, don't allowlist it or fix
golden content in the same change that adds this check.

Run:
    python3 test_golden_lint.py
    pytest test_golden_lint.py
    FLOXIFY_GOLDEN_LINT_LIVE_CATALOG=0 python3 test_golden_lint.py   # no network
"""
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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


# --- whole-manifest lock-resolution leg (AI-479) ----------------------------

FLOX_BIN = "flox"


def _run_flox_init(cwd, timeout=30):
    """Thin subprocess wrapper — the whole surface a test needs to mock to
    keep the lock leg off the network (mirrors verify.py's own
    `_run_show_command` convention: everything below this line is pure
    logic over the wrapper's return value)."""
    return subprocess.run(
        [FLOX_BIN, "init", "-b"], cwd=cwd, capture_output=True, text=True,
        timeout=timeout,
    )


def _run_flox_edit(manifest_path, cwd, timeout=120):
    return subprocess.run(
        [FLOX_BIN, "edit", "-f", str(manifest_path)], cwd=cwd,
        capture_output=True, text=True, timeout=timeout,
    )


def _attempt_lock(manifest_text, timeout=120):
    """Attempt a whole-manifest LOCK (resolve, don't realize) in a
    throwaway environment. Returns (ok, message, elapsed_seconds).

    `flox edit -f` (not `flox activate`) triggers the resolver and writes
    manifest.lock but never builds or downloads the resolved store paths —
    orders of magnitude cheaper than a full activation while exercising
    exactly the cross-pkg-group resolution activation would. This is the
    failure class ("constraints for group 'X' are too tight") the
    per-package `check_catalog` leg above cannot see: packages that each
    resolve individually can still fail to co-resolve together on any
    single catalog page, and AI-457/AI-478 could only catch that by
    running `flox activate` themselves.

    A `flox init` failure or timeout (extremely rare — e.g. no disk space
    in the throwaway dir) is reported as a lock failure too, elapsed=0.0,
    rather than raising — a harness-side problem still needs to surface
    as "this golden's lock leg could not be verified," not crash the run.
    """
    with tempfile.TemporaryDirectory(prefix="floxify-golden-lock-") as tmp:
        try:
            init = _run_flox_init(tmp)
        except subprocess.TimeoutExpired:
            return False, "flox init timed out after 30s", 0.0
        if init.returncode != 0:
            return (
                False,
                f"flox init failed: {(init.stderr or init.stdout).strip()[:500]}",
                0.0,
            )

        manifest_path = Path(tmp) / "candidate-manifest.toml"
        manifest_path.write_text(manifest_text, encoding="utf-8")

        start = time.monotonic()
        try:
            edit = _run_flox_edit(manifest_path, tmp, timeout=timeout)
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - start
            return False, f"flox edit -f timed out after {timeout}s", elapsed
        elapsed = time.monotonic() - start

        if edit.returncode != 0:
            return False, (edit.stderr or edit.stdout).strip()[:1000], elapsed
        return True, "", elapsed


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

    def _lock(self, fixture_id, live_catalog=None, flox_available=None):
        """Whole-manifest lock-resolution leg (AI-479). `live_catalog`/
        `flox_available` are test-only overrides for the skip conditions
        (default to the real module/environment state) so the skip paths
        are directly testable without patching module globals — see
        TestLockResolutionLeg below.
        """
        live = LIVE_CATALOG if live_catalog is None else live_catalog
        if not live:
            self.skipTest(
                "FLOXIFY_GOLDEN_LINT_LIVE_CATALOG=0 -- lock leg needs live "
                "flox+network, same discipline as the catalog leg above"
            )
        available = (
            bool(shutil.which(FLOX_BIN)) if flox_available is None else flox_available
        )
        if not available:
            self.skipTest("flox not on PATH -- cannot attempt lock resolution")

        manifest_text = (GOLD_DIR / f"{fixture_id}.toml").read_text(encoding="utf-8")
        ok, message, elapsed = _attempt_lock(manifest_text)
        print(
            f"  [lock] {fixture_id}: {'OK' if ok else 'FAILED'} in {elapsed:.2f}s",
            flush=True,
        )
        if not ok:
            self.fail(
                f"{fixture_id}.toml FAILED whole-manifest lock resolution "
                f"({elapsed:.2f}s) -- its packages resolve individually "
                f"(the catalog leg above passes) but cannot co-resolve "
                f"together on any single catalog page. This is a REAL "
                f"finding, not a false positive -- do NOT allowlist it and "
                f"do NOT fix golden content in the same change that adds "
                f"this check; report it instead. Resolver output:\n{message}"
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


def _make_lock_test(fixture_id):
    def test(self):
        self._lock(fixture_id)
    test.__name__ = f"test_{fixture_id.replace('-', '_')}_locks_cleanly"
    return test


for _fixture_id in _GOLD_IDS:
    setattr(
        TestGoldenLint, _make_lock_test(_fixture_id).__name__,
        _make_lock_test(_fixture_id),
    )


class TestLockResolutionLeg(unittest.TestCase):
    """AI-479: mocked, no-network unit coverage for the lock-resolution
    leg's skip/fail/pass plumbing. The live behavior (does a real golden
    actually lock) belongs to the flox-equipped run — see the dynamically
    generated test_<fixture>_locks_cleanly methods above, exercised by
    `python3 -m unittest test_golden_lint -v` with `flox` on PATH."""

    def _instance(self):
        # Any bound TestGoldenLint instance works here -- we only need
        # self.fail/self.skipTest, not the test runner around it.
        return TestGoldenLint("test_catalog_leg_ran_when_expected")

    # --- _attempt_lock: mocked subprocess boundary --------------------

    @patch("test_golden_lint._run_flox_edit")
    @patch("test_golden_lint._run_flox_init")
    def test_attempt_lock_reports_success(self, mock_init, mock_edit):
        mock_init.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mock_edit.return_value = MagicMock(
            returncode=0, stdout="Environment successfully updated.", stderr="",
        )
        ok, message, elapsed = _attempt_lock("[install]\n")
        self.assertTrue(ok)
        self.assertEqual(message, "")
        self.assertGreaterEqual(elapsed, 0.0)

    @patch("test_golden_lint._run_flox_edit")
    @patch("test_golden_lint._run_flox_init")
    def test_attempt_lock_surfaces_resolver_failure_message(self, mock_init, mock_edit):
        # RED-first "fires" fixture: the exact failure class AI-457/AI-478
        # found only by hand -- packages that resolve individually but
        # cannot co-resolve together on one catalog page. Mocked here (a
        # genuinely impossible co-resolution needs live catalog state to
        # construct, which the unit-test tier must stay free of) so the
        # leg's fail-path is provably exercised without network.
        mock_init.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mock_edit.return_value = MagicMock(
            returncode=1, stdout="",
            stderr=(
                "✘ ERROR: resolution failed: constraints for group "
                "'toplevel' are too tight"
            ),
        )
        ok, message, elapsed = _attempt_lock(
            '[install]\na.pkg-path = "a"\nb.pkg-path = "b"\n'
        )
        self.assertFalse(ok)
        self.assertIn("constraints for group 'toplevel' are too tight", message)
        self.assertGreaterEqual(elapsed, 0.0)

    @patch("test_golden_lint._run_flox_init")
    def test_attempt_lock_handles_init_failure_gracefully(self, mock_init):
        mock_init.return_value = MagicMock(returncode=1, stdout="", stderr="disk full")
        ok, message, elapsed = _attempt_lock("[install]\n")
        self.assertFalse(ok)
        self.assertIn("disk full", message)
        self.assertEqual(elapsed, 0.0)

    @patch("test_golden_lint._run_flox_init")
    def test_attempt_lock_handles_init_timeout(self, mock_init):
        # PR #56 review M1: _run_flox_init's own timeout must degrade to
        # a reported lock failure, not propagate uncaught and crash the
        # test -- the same discipline _run_flox_edit's timeout already
        # got, now applied to init too.
        mock_init.side_effect = subprocess.TimeoutExpired(cmd="flox init", timeout=30)
        ok, message, elapsed = _attempt_lock("[install]\n")
        self.assertFalse(ok)
        self.assertIn("timed out", message)
        self.assertEqual(elapsed, 0.0)

    @patch("test_golden_lint._run_flox_edit")
    @patch("test_golden_lint._run_flox_init")
    def test_attempt_lock_handles_edit_timeout(self, mock_init, mock_edit):
        mock_init.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mock_edit.side_effect = subprocess.TimeoutExpired(cmd="flox edit -f", timeout=120)
        ok, message, elapsed = _attempt_lock("[install]\n", timeout=120)
        self.assertFalse(ok)
        self.assertIn("timed out", message)

    # --- TestGoldenLint._lock: skip conditions -------------------------

    def test_lock_skips_when_live_catalog_disabled(self):
        # PR #56 review M2: _GOLD_IDS[0], not a hardcoded golden id -- these
        # tests exercise _lock's own skip plumbing, not anything specific
        # to one golden's content, and must not couple to a name that
        # could be renamed or removed.
        instance = self._instance()
        with self.assertRaises(unittest.SkipTest):
            instance._lock(_GOLD_IDS[0], live_catalog=False)

    def test_lock_skips_when_flox_absent(self):
        instance = self._instance()
        with self.assertRaises(unittest.SkipTest):
            instance._lock(_GOLD_IDS[0], live_catalog=True, flox_available=False)

    # --- TestGoldenLint._lock: fail/pass, mocked _attempt_lock ---------

    @patch("test_golden_lint._attempt_lock")
    def test_lock_fails_and_surfaces_resolver_message_when_resolution_fails(
        self, mock_attempt
    ):
        mock_attempt.return_value = (
            False,
            "ERROR: resolution failed: constraints for group 'toplevel' are too tight",
            0.42,
        )
        instance = self._instance()
        with self.assertRaises(AssertionError) as ctx:
            instance._lock(_GOLD_IDS[0], live_catalog=True, flox_available=True)
        message = str(ctx.exception)
        self.assertIn("constraints for group 'toplevel' are too tight", message)
        self.assertIn("REAL finding", message)
        self.assertIn("do NOT allowlist", message)

    @patch("test_golden_lint._attempt_lock")
    def test_lock_passes_cleanly_when_resolution_succeeds(self, mock_attempt):
        mock_attempt.return_value = (True, "", 0.5)
        instance = self._instance()
        # must not raise
        instance._lock(_GOLD_IDS[0], live_catalog=True, flox_available=True)


if __name__ == "__main__":
    unittest.main()
