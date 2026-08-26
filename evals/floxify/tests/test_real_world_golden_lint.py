#!/usr/bin/env python3
"""Golden-manifest lint: run verify.py's checker over every hand-curated
reference in expected/*.toml (AI-456 item 2).

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

Unknowns are deliberately un-allowlistable. KNOWN_VIOLATIONS covers
violations only; an entry verify.py declined to check is not a defect to
be carried with a ticket but a gold nothing can say is correct, and the
remedy is always to write a `version` the checker can resolve. Both
`_lint`s therefore fail on a non-empty `catalog_unknown` with no valve.

KNOWN_VIOLATIONS is an explicit allowlist, one entry per open golden
defect, each tagged with the ticket that will resolve it. IT IS EMPTY
RIGHT NOW — see the note at the assignment for what its last two entries
were and why they are gone. Entries match the violation's structured
`pkg_path` field EXACTLY, not a substring of the message — short needles
like "uv" or "deno" would otherwise collide with unrelated text a message
might contain in the future. A dedicated test
(test_known_violations_allowlist_has_no_stale_entries) asserts every
entry still corresponds to a live violation, so fixing a golden without
removing its entry doesn't leave a dead allowlist slot that could
silently absorb an unrelated future regression; with the allowlist empty
that test has nothing to check and skips.

Whole-manifest lock-resolution leg (AI-479): the checks above are all
PER-PACKAGE (does this one pkg-path/version/system resolve) — none of
them can see a manifest whose packages each resolve individually but
cannot co-resolve TOGETHER on any single catalog page
("constraints for group 'X' are too tight"). AI-457 and AI-478 only
caught that class by hand, running `flox activate` themselves. This adds
one more per-golden test, `test_<fixture>_locks_cleanly`, that attempts
a real `flox list -c` (resolution-only — see `_attempt_lock`'s docstring
for the source-level and empirical proof it never realizes the closure)
in a throwaway environment. Same skip discipline as the catalog leg
above: advisory-skip when `flox` is absent or
`FLOXIFY_GOLDEN_LINT_LIVE_CATALOG=0`, never gating the flox-less
free-tier step. When it DOES run, a genuine resolution failure is a real
finding on that golden — report it, don't allowlist it or fix golden
content in the same change that adds this check. A catalog-API
communication error is a different, transient failure class and is
reported honestly as such rather than as a resolution finding — see
`_classify_lock_failure`.

Run from the suite root (`evals/floxify/`) — that is what puts
`_skill_module_loader` on `sys.path`. Running the file by path
(`python3 tests/test_real_world_golden_lint.py`) fails with
`ModuleNotFoundError` instead:
    python3 -m unittest tests.test_real_world_golden_lint -v
    python3 -m tests.test_real_world_golden_lint       # same, via the __main__ block
    pytest tests/test_real_world_golden_lint.py
    FLOXIFY_GOLDEN_LINT_LIVE_CATALOG=0 python3 -m unittest tests.test_real_world_golden_lint  # no network
"""
import json
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
SUITE = HERE.parent          # evals/floxify
REPO_ROOT = SUITE.parent.parent
EXPECTED_DIR = SUITE / "expected"
REAL_WORLD_FILE = SUITE / "real-world.jsonl"
VERIFY = REPO_ROOT / "flox-plugin" / "skills" / "floxify" / "scripts" / "verify.py"

# Unique sys.modules key — see _skill_module_loader.py's docstring for the
# incident this avoids (test_verify.py loads the same verify.py under ITS
# own unique key; sharing a key let whichever load ran last silently
# steal the other's @patch target).
verify_mod = load_module(VERIFY, sys_modules_key="verify_under_test_real_world_golden_lint")
verify = verify_mod.verify

LIVE_CATALOG = os.environ.get("FLOXIFY_GOLDEN_LINT_LIVE_CATALOG", "1") != "0"

# (fixture id, rule, pkg-path) -> tracking ticket. `pkg-path` is matched
# EXACTLY against the violation's structured `pkg_path` field.
#
# Populated from a live `flox show` run against nixpkgs on 2026-07-16 (see
# the AI-461 PR description for that snapshot). It has been emptied twice,
# by opposite means: AI-457 fixed every golden's content, and the later
# entries below were retired by fixing the CHECKER with no golden content
# touched at all. New entries here should be rare and always tagged with
# the ticket that will resolve them.
#
# EMPTY AGAIN, and by the fix its last two entries themselves named. Both
# were live-catalog drift on an UNPINNED package -- lemmy's `gcc` (Latest
# 15.3.0 has no x86_64-darwin build; 15.2.0 below it does, and a
# single-package `flox install gcc` really does lock 15.2.0) and
# supabase's `nodejs_22` (Latest 22.23.2 has none, 22.23.1 does) -- and
# the supabase entry had already written down the exit: "either flox's
# default resolve set stops including x86_64-darwin ... or verify.py
# stops equating 'unpinned' with 'Latest's Systems: line'." verify.py now
# descends the version rows the way the resolver does (see
# `_resolve_rows` there), so neither is a violation to allowlist any
# more. Nothing in either golden changed.
#
# One caveat the burn-down does not license, kept here because this is
# where someone will look before adding the next entry: what supabase's
# whole group locks is not what a per-package walk predicts. Its
# `nodejs_22` really locks 22.21.1, not the 22.23.1 above, because
# `pnpm_10.version = "10.24.0"` shares its pkg-group and resolution picks
# ONE page for the group. The 22.23.1 here is the newest row that could
# serve nodejs_22 alone; `test_<fixture>_locks_cleanly` is what actually
# answers the group question.
#
# What this means for FUTURE entries of this shape: an unpinned or
# prefix-pinned package whose newest matching version sheds a platform is
# no longer an allowlist candidate. If `catalog-systems-mismatch` still
# fires on one, then either no version builds that platform at all or no
# single version builds every declared platform together -- the message
# says which, and both are real findings rather than checker artifacts.
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


def _run_flox_list_c(cwd, timeout=60):
    """Thin subprocess wrapper — the whole surface a test needs to mock to
    keep the lock leg off the network (mirrors verify.py's own
    `_run_show_command` convention: everything below this line is pure
    logic over the wrapper's return value)."""
    return subprocess.run(
        [FLOX_BIN, "list", "-c"], cwd=cwd, capture_output=True, text=True,
        timeout=timeout,
    )


# _attempt_lock's three possible outcomes. A resolution error is the REAL
# finding this leg exists to catch; a transient error is an infra/network
# hiccup that must never be reported as one.
LOCK_OK = "ok"
LOCK_RESOLUTION_ERROR = "resolution_error"
LOCK_TRANSIENT_ERROR = "transient_error"


def _classify_lock_failure(stderr):
    """Discriminate a genuine resolver defect (the class this leg exists
    to catch) from a catalog-API communication hiccup (must NOT be
    reported as a resolution finding).

    `flox list -c` is resolution-only (see `_attempt_lock`'s docstring)
    but it still talks to the catalog API to resolve, so it is
    "resolution-only," not "network-free" — it can fail on a connectivity
    problem unrelated to the manifest's content. Confirmed empirically
    (2026-07-17, forcing an unreachable --floxhub-url): that failure
    shape is `catalog error: Communication Error: error sending request
    for url ...`. A genuine resolver defect, by contrast, always starts
    `resolution failed: ` — confirmed live for both shapes this leg cares
    about: "constraints for group 'X' are too tight" (cross-pkg-group
    conflict) and "could not find package '...'" (missing pkg-path).
    """
    if "resolution failed:" in stderr:
        return LOCK_RESOLUTION_ERROR
    return LOCK_TRANSIENT_ERROR


def _attempt_lock_once(manifest_text, timeout):
    with tempfile.TemporaryDirectory(prefix="floxify-golden-lock-") as tmp:
        try:
            init = _run_flox_init(tmp)
        except subprocess.TimeoutExpired:
            return LOCK_TRANSIENT_ERROR, "flox init timed out after 30s", 0.0
        if init.returncode != 0:
            return (
                LOCK_TRANSIENT_ERROR,
                f"flox init failed: {(init.stderr or init.stdout).strip()[:500]}",
                0.0,
            )

        # Written directly rather than via `flox edit -f <file>`: `edit`
        # transactionally BUILDS the environment to validate the edit
        # (`man flox-edit`: "Once the editor is closed the environment is
        # built in order to validate the edit") -- exactly the
        # network-heavy, cold-store-flaky realize step this leg must
        # avoid (see `_attempt_lock`'s docstring for the CI evidence).
        manifest_path = Path(tmp) / ".flox" / "env" / "manifest.toml"
        # mkdir needed because mocked-init unit tests never run the real
        # `flox init -b` that would have created these directories.
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(manifest_text, encoding="utf-8")

        start = time.monotonic()
        try:
            listed = _run_flox_list_c(tmp, timeout=timeout)
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - start
            return (
                LOCK_TRANSIENT_ERROR,
                f"flox list -c timed out after {timeout}s",
                elapsed,
            )
        elapsed = time.monotonic() - start

        if listed.returncode != 0:
            stderr = (listed.stderr or listed.stdout).strip()[:1000]
            return _classify_lock_failure(stderr), stderr, elapsed
        return LOCK_OK, "", elapsed


def _attempt_lock(manifest_text, timeout=60):
    """Attempt a whole-manifest LOCK (resolve, don't realize) in a
    throwaway environment. Returns (status, message, elapsed_seconds)
    where status is one of LOCK_OK, LOCK_RESOLUTION_ERROR, or
    LOCK_TRANSIENT_ERROR (see `_classify_lock_failure`).

    Instrument: `flox list -c`, not `flox edit -f`. PR #56's first
    version used `flox edit -f` on the (wrong) assumption that it was
    resolve-only; it went RED in CI because `edit` transactionally builds
    the environment to validate the edit (see `_attempt_lock_once`), so
    it realizes -- fetches and builds -- the full closure on every
    invocation. `flox list -c` is resolution-only by construction:
    flox-rust-sdk's `CoreEnvironment::lock` (a catalog-API-only resolve
    that produces `manifest.lock`) and `CoreEnvironment::build` (which
    fetches/builds store paths) are architecturally separate methods --
    `build`'s own doc comment reads "Does not lock the manifest. Call
    [Self::lock] explicitly before building" -- and the CLI's `list`
    command path (`List::handle`) only ever calls `Environment::lockfile`
    (which locks via `ensure_locked`/`lock`), never `build`, regardless
    of `-c`/`-n`/default list mode.

    Confirmed empirically (2026-07-17): `flox list -c` run against all 8
    golden manifests made zero net additions to `/nix/store` (identical
    top-level directory count before and after, across both passing and
    a deliberately cross-pkg-group-broken manifest) and completed in a
    consistent ~0.5-1.5s locally regardless of outcome -- no order-of-
    magnitude jump on failure the way a partial realize would produce.
    `flox edit -f`, by contrast, was CI-observed at 8-34s per golden with
    real store fetches (a fetch failure for a resolvable package, e.g.
    supabase's nodejs_22, is exactly the false "cannot co-resolve"
    finding this rewrite fixes).

    `flox list -c` still calls the catalog API to resolve, so it is not
    network-FREE, just realize-free -- it can still hit a catalog
    communication error. A transient-classified failure gets one retry
    (a catalog blip, not the manifest, likely caused it) before being
    reported; a resolution-content error never retries, since the
    manifest's content won't change between attempts.

    A `flox init` failure or timeout (extremely rare — e.g. no disk space
    in the throwaway dir) is always LOCK_TRANSIENT_ERROR, elapsed=0.0,
    rather than raising — a harness-side problem still needs to surface
    as "this golden's lock leg could not be verified," not crash the run.
    """
    status, message, elapsed = _attempt_lock_once(manifest_text, timeout)
    if status == LOCK_TRANSIENT_ERROR:
        status, message, elapsed = _attempt_lock_once(manifest_text, timeout)
    return status, message, elapsed


def _gold_ids():
    """The real-world goldens, named by the real-world registry.

    Selection is by REGISTRY, not by globbing expected/. Before AI-509
    Ticket 3 the real-world goldens had their own directory
    (testdata/gold/) and a glob meant exactly "the real-repo goldens";
    now every suite's reference manifests share expected/, so a glob
    would silently pull in the synthetic and stretch goldens, which are
    deliberately out of scope here. The stretch goldens are linted at
    their own scope by test_stretch_golden_lint.py; the six synthetic
    goldens are currently linted by nothing, which was equally true
    before they shared this directory (that module's own docstring
    scopes them out: they "predate this check"). Naming the registry
    keeps this lint's scope the same set it has always had — a glob
    would have silently CHANGED it. The sibling stretch lint selects the
    same way.
    """
    return sorted(
        json.loads(line)["id"]
        for line in REAL_WORLD_FILE.read_text().splitlines()
        if line.strip()
    )


_GOLD_IDS = _gold_ids()
# A path typo or an emptied registry must fail loudly at collection time,
# not silently report "0 tests, all passed."
assert _GOLD_IDS, f"no real-world ids found in {REAL_WORLD_FILE} — check the path"
_MISSING = [i for i in _GOLD_IDS if not (EXPECTED_DIR / f"{i}.toml").is_file()]
assert not _MISSING, f"real-world entries with no expected/<id>.toml: {_MISSING}"


class TestGoldenLint(unittest.TestCase):
    """One test per golden so a failure names the exact fixture."""

    def _lint(self, fixture_id):
        manifest_text = (EXPECTED_DIR / f"{fixture_id}.toml").read_text(encoding="utf-8")
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
                f"a tagged entry):\n{detail}"
            )

        # An entry that stops being CHECKABLE is the other way a golden
        # can quietly leave the verified set, and with KNOWN_VIOLATIONS
        # empty it is the only silent channel left. verify.py declines to
        # conclude for three reasons (see its UNKNOWN_REASONS), and every
        # one of them means this golden's pkg-path/version/systems went
        # unverified while the lint stayed green. These manifests are
        # hand-curated and per-package verified, so the expected count is
        # zero; a real one belongs in the same review as whatever made it
        # unresolvable.
        unknown = result.get("catalog_unknown") or []
        if unknown:
            detail = "\n".join(
                f"  {u['install_id']} ({u.get('pkg_path')}"
                f"{'@' + str(u['version']) if u.get('version') else ''}): "
                f"{u.get('reason', 'no reason recorded')}"
                for u in unknown
            )
            self.fail(
                f"{fixture_id}.toml has {len(unknown)} install entr"
                f"{'y' if len(unknown) == 1 else 'ies'} the catalog leg could "
                f"not evaluate — verification silently stopped covering "
                f"{'it' if len(unknown) == 1 else 'them'}:\n{detail}"
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

        manifest_text = (EXPECTED_DIR / f"{fixture_id}.toml").read_text(encoding="utf-8")
        status, message, elapsed = _attempt_lock(manifest_text)
        print(
            f"  [lock] {fixture_id}: {status} in {elapsed:.2f}s",
            flush=True,
        )
        if status == LOCK_OK:
            return
        if status == LOCK_RESOLUTION_ERROR:
            self.fail(
                f"{fixture_id}.toml FAILED whole-manifest lock resolution "
                f"({elapsed:.2f}s) -- its packages resolve individually "
                f"(the catalog leg above passes) but cannot co-resolve "
                f"together on any single catalog page. This is a REAL "
                f"finding, not a false positive -- do NOT allowlist it and "
                f"do NOT fix golden content in the same change that adds "
                f"this check; report it instead. Resolver output:\n{message}"
            )
        # LOCK_TRANSIENT_ERROR, survived a retry inside _attempt_lock.
        self.fail(
            f"{fixture_id}.toml's lock attempt hit an environment/catalog "
            f"error, likely transient, on both tries ({elapsed:.2f}s) -- "
            f"the resolver never reported 'resolution failed:', so this "
            f"is NOT a co-resolution defect in the golden. Re-run the job "
            f"before treating this as a finding. Raw output:\n{message}"
        )

    def test_catalog_leg_ran_when_expected(self):
        """Distinguishes 'genuinely clean' from 'silently skipped.'"""
        sample = (EXPECTED_DIR / f"{_GOLD_IDS[0]}.toml").read_text(encoding="utf-8")
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

        Otherwise a golden could be fixed, the entry left behind, and a
        future unrelated regression that happens to match the same
        (fixture, rule, pkg_path) triple would be silently allowlisted.
        """
        if not KNOWN_VIOLATIONS:
            # `set() - consumed` is empty for every possible `consumed`,
            # so the loop below cannot change the outcome -- it would
            # just re-`verify()` all eight goldens to compute a
            # guaranteed pass. Skipping states the invariant instead of
            # spending a live catalog run establishing it.
            self.skipTest("allowlist is empty — no entry can be stale")
        if not LIVE_CATALOG:
            self.skipTest("stale-allowlist check needs the live catalog leg")
        if not shutil.which("flox"):
            self.skipTest("flox not on PATH — cannot verify allowlist freshness")

        consumed = set()
        for fixture_id in _GOLD_IDS:
            manifest_text = (EXPECTED_DIR / f"{fixture_id}.toml").read_text(encoding="utf-8")
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
                f"violation — fixed upstream? Remove the entry so the "
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


class TestUnknownGate(unittest.TestCase):
    """The checkability half of `_lint`, unit-tested against a synthetic
    record.

    Same argument `TestAllowlistMatching` makes below: every committed
    golden's `version` passes `_is_version_literal` and the offline path
    returns `([], False, [])`, so this block's `detail` comprehension has
    no live exerciser and would first run on the day it reports a real
    problem. Needs no live catalog.
    """

    UNKNOWN = {"install_id": "node", "pkg_path": "nodejs_22",
               "version": "^20.0", "reason": "the reason it gave"}

    def _lint_over(self, unknown):
        lint = TestGoldenLint("_lint")
        with patch(f"{__name__}.verify",
                   return_value={"violations": [],
                                 "catalog_checked": True,
                                 "catalog_unknown": unknown}):
            lint._lint("sentry")

    def test_a_clean_result_passes(self):
        self._lint_over([])

    def test_an_unknown_entry_fails_and_names_it(self):
        with self.assertRaises(AssertionError) as caught:
            self._lint_over([self.UNKNOWN])
        message = str(caught.exception)
        self.assertIn("1 install entry the catalog leg could not evaluate",
                      message)
        self.assertIn("node (nodejs_22@^20.0): the reason it gave", message)

    def test_an_unknown_with_no_version_still_renders(self):
        # `version` is None whenever no row was established as the one
        # that applies -- the record deliberately does not name a row it
        # only guessed at, and the render must not print a bare "@".
        with self.assertRaises(AssertionError) as caught:
            self._lint_over([{**self.UNKNOWN, "version": None}])
        self.assertIn("node (nodejs_22): the reason it gave",
                      str(caught.exception))
        self.assertNotIn("@", str(caught.exception))

    def test_the_plural_agrees(self):
        with self.assertRaises(AssertionError) as caught:
            self._lint_over([self.UNKNOWN, {**self.UNKNOWN,
                                            "install_id": "pg"}])
        self.assertIn("2 install entries the catalog leg could not evaluate",
                      str(caught.exception))


class TestAllowlistMatching(unittest.TestCase):
    """`_matches`' exact-`pkg_path` rule, unit-tested against a synthetic
    entry rather than through `KNOWN_VIOLATIONS`.

    The entries were data and the matching is behavior, and with the
    allowlist empty the behavior has no live exerciser at all -- every
    `_is_allowlisted` call now iterates an empty dict and `_matches` is
    never invoked. The module docstring goes out of its way to explain
    why the match is exact ("short needles like 'uv' or 'deno' would
    otherwise collide"), which is worth keeping honest for whoever adds
    the next entry under incident pressure. Needs no live catalog.
    """

    VIOLATION = {"rule": "catalog-systems-mismatch", "pkg_path": "uv",
                 "message": 'no build for x86_64-darwin; see also deno and uvloop'}

    def test_exact_triple_matches(self):
        key = ("sentry", "catalog-systems-mismatch", "uv")
        self.assertTrue(_matches("sentry", self.VIOLATION, key))
        with patch.dict(KNOWN_VIOLATIONS, {key: "TICKET-1"}, clear=True):
            self.assertTrue(_is_allowlisted("sentry", self.VIOLATION))

    def test_near_misses_do_not_match(self):
        # One field wrong in each direction, plus the substring case the
        # exact rule exists to reject: `uvloop` must not be absorbed by
        # an entry written for `uv`.
        for key in (
            ("supabase", "catalog-systems-mismatch", "uv"),   # other fixture
            ("sentry", "catalog-version-missing", "uv"),      # other rule
            ("sentry", "catalog-systems-mismatch", "uvloop"),  # superstring
            ("sentry", "catalog-systems-mismatch", "u"),       # substring
        ):
            with self.subTest(key):
                self.assertFalse(_matches("sentry", self.VIOLATION, key))
                with patch.dict(KNOWN_VIOLATIONS, {key: "TICKET-1"},
                                clear=True):
                    self.assertFalse(_is_allowlisted("sentry", self.VIOLATION))

    def test_a_violation_without_a_pkg_path_needs_a_none_keyed_entry(self):
        # Not every rule carries `pkg_path` (malformed-section, for one),
        # and `.get` yields None. Stating what the rule actually is
        # rather than what one might hope: such a violation is NOT
        # unallowlistable -- it is allowlisted by an entry whose third
        # element is literally None, and by nothing else. Worth being
        # exact about here, because the audience for this class is
        # whoever is adding an entry under incident pressure.
        v = {"rule": "malformed-section"}
        self.assertTrue(_matches("sentry", v, ("sentry", "malformed-section", None)))
        self.assertFalse(_matches("sentry", v, ("sentry", "malformed-section", "uv")))
        self.assertFalse(_matches("sentry", v, ("supabase", "malformed-section", None)))


class TestLockResolutionLeg(unittest.TestCase):
    """AI-479: mocked, no-network unit coverage for the lock-resolution
    leg's skip/fail/pass plumbing. The live behavior (does a real golden
    actually lock) belongs to the flox-equipped run — see the dynamically
    generated test_<fixture>_locks_cleanly methods above, exercised by
    `python3 -m unittest tests.test_real_world_golden_lint -v` with `flox` on PATH."""

    def _instance(self):
        # Any bound TestGoldenLint instance works here -- we only need
        # self.fail/self.skipTest, not the test runner around it.
        return TestGoldenLint("test_catalog_leg_ran_when_expected")

    # --- _attempt_lock: mocked subprocess boundary --------------------

    @patch(f"{__name__}._run_flox_list_c")
    @patch(f"{__name__}._run_flox_init")
    def test_attempt_lock_reports_success(self, mock_init, mock_list):
        mock_init.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mock_list.return_value = MagicMock(
            returncode=0, stdout="[install]\n", stderr="",
        )
        status, message, elapsed = _attempt_lock("[install]\n")
        self.assertEqual(status, LOCK_OK)
        self.assertEqual(message, "")
        self.assertGreaterEqual(elapsed, 0.0)

    @patch(f"{__name__}._run_flox_list_c")
    @patch(f"{__name__}._run_flox_init")
    def test_attempt_lock_surfaces_resolver_failure_message(self, mock_init, mock_list):
        # RED-first "fires" fixture: the exact failure class AI-457/AI-478
        # found only by hand -- packages that resolve individually but
        # cannot co-resolve together on one catalog page. Mocked here (a
        # genuinely impossible co-resolution needs live catalog state to
        # construct, which the unit-test tier must stay free of) so the
        # leg's fail-path is provably exercised without network.
        mock_init.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mock_list.return_value = MagicMock(
            returncode=1, stdout="",
            stderr=(
                "✘ ERROR: resolution failed: constraints for group "
                "'toplevel' are too tight"
            ),
        )
        status, message, elapsed = _attempt_lock(
            '[install]\na.pkg-path = "a"\nb.pkg-path = "b"\n'
        )
        self.assertEqual(status, LOCK_RESOLUTION_ERROR)
        self.assertIn("constraints for group 'toplevel' are too tight", message)
        self.assertGreaterEqual(elapsed, 0.0)

    @patch(f"{__name__}._run_flox_list_c")
    @patch(f"{__name__}._run_flox_init")
    def test_attempt_lock_classifies_catalog_error_as_transient_and_retries(
        self, mock_init, mock_list
    ):
        # The failure class PR #56's CI run actually hit: a catalog/
        # fetch-shaped error unrelated to the manifest's content
        # (confirmed live 2026-07-17 by forcing an unreachable
        # --floxhub-url). Both attempts fail identically here, so the
        # retry is exhausted and the caller still sees LOCK_TRANSIENT_
        # ERROR, never LOCK_RESOLUTION_ERROR -- this is the exact
        # discrimination that must NOT fire the "cannot co-resolve, do
        # NOT allowlist" verdict for a network blip.
        mock_init.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mock_list.return_value = MagicMock(
            returncode=1, stdout="",
            stderr=(
                "✘ ERROR: catalog error: Communication Error: error "
                "sending request for url (https://.../api/v1/catalog/resolve)"
            ),
        )
        status, message, elapsed = _attempt_lock("[install]\n")
        self.assertEqual(status, LOCK_TRANSIENT_ERROR)
        self.assertIn("Communication Error", message)
        self.assertEqual(mock_list.call_count, 2, "transient failure must retry once")
        self.assertGreaterEqual(elapsed, 0.0)

    @patch(f"{__name__}._run_flox_list_c")
    @patch(f"{__name__}._run_flox_init")
    def test_attempt_lock_retry_recovers_from_transient_error(
        self, mock_init, mock_list
    ):
        mock_init.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mock_list.side_effect = [
            MagicMock(
                returncode=1, stdout="",
                stderr="✘ ERROR: catalog error: Communication Error: timeout",
            ),
            MagicMock(returncode=0, stdout="[install]\n", stderr=""),
        ]
        status, message, elapsed = _attempt_lock("[install]\n")
        self.assertEqual(status, LOCK_OK)
        self.assertEqual(message, "")
        self.assertEqual(mock_list.call_count, 2)

    @patch(f"{__name__}._run_flox_init")
    def test_attempt_lock_handles_init_failure_gracefully(self, mock_init):
        mock_init.return_value = MagicMock(returncode=1, stdout="", stderr="disk full")
        status, message, elapsed = _attempt_lock("[install]\n")
        self.assertEqual(status, LOCK_TRANSIENT_ERROR)
        self.assertIn("disk full", message)
        self.assertEqual(elapsed, 0.0)

    @patch(f"{__name__}._run_flox_init")
    def test_attempt_lock_handles_init_timeout(self, mock_init):
        # PR #56 review M1: _run_flox_init's own timeout must degrade to
        # a reported lock failure, not propagate uncaught and crash the
        # test -- the same discipline _run_flox_list_c's timeout already
        # got, now applied to init too.
        mock_init.side_effect = subprocess.TimeoutExpired(cmd="flox init", timeout=30)
        status, message, elapsed = _attempt_lock("[install]\n")
        self.assertEqual(status, LOCK_TRANSIENT_ERROR)
        self.assertIn("timed out", message)
        self.assertEqual(elapsed, 0.0)

    @patch(f"{__name__}._run_flox_list_c")
    @patch(f"{__name__}._run_flox_init")
    def test_attempt_lock_handles_list_timeout(self, mock_init, mock_list):
        mock_init.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mock_list.side_effect = subprocess.TimeoutExpired(cmd="flox list -c", timeout=60)
        status, message, elapsed = _attempt_lock("[install]\n", timeout=60)
        self.assertEqual(status, LOCK_TRANSIENT_ERROR)
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

    @patch(f"{__name__}._attempt_lock")
    def test_lock_fails_and_surfaces_resolver_message_when_resolution_fails(
        self, mock_attempt
    ):
        mock_attempt.return_value = (
            LOCK_RESOLUTION_ERROR,
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

    @patch(f"{__name__}._attempt_lock")
    def test_lock_fails_with_transient_framing_when_catalog_errors_persist(
        self, mock_attempt
    ):
        # This is the exact bug PR #56's first version had in CI: a
        # network/catalog blip must produce an honest "likely transient"
        # message, never the "REAL finding, do NOT allowlist" verdict
        # that a genuine cross-pkg-group defect gets.
        mock_attempt.return_value = (
            LOCK_TRANSIENT_ERROR,
            "catalog error: Communication Error: error sending request",
            1.1,
        )
        instance = self._instance()
        with self.assertRaises(AssertionError) as ctx:
            instance._lock(_GOLD_IDS[0], live_catalog=True, flox_available=True)
        message = str(ctx.exception)
        self.assertIn("transient", message)
        self.assertIn("Communication Error", message)
        self.assertNotIn("REAL finding", message)
        self.assertNotIn("do NOT allowlist", message)

    @patch(f"{__name__}._attempt_lock")
    def test_lock_passes_cleanly_when_resolution_succeeds(self, mock_attempt):
        mock_attempt.return_value = (LOCK_OK, "", 0.5)
        instance = self._instance()
        # must not raise
        instance._lock(_GOLD_IDS[0], live_catalog=True, flox_available=True)


if __name__ == "__main__":
    unittest.main()
