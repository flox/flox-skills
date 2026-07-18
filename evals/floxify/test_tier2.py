#!/usr/bin/env python3
"""Unit tests for the Tier 2 /floxify eval harness (tier2.py).

Covers the deterministic, unit-testable pieces: structural-conformance
checks (runtime pin / service-block regexes), registry loading, and the
clone-at-SHA fallback chain. The agentic skill run and LLM judge call are
integration-only (same as Tier 1's run_floxify.py has no unit tests around
`_run_claude_agent`/`_judge`) and are exercised by an actual `--only
mastodon` run, not here.

Run: python3 -m unittest test_tier2 -v
"""
import json
import shutil
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import tier2

# Loaded once for the whole module: the real verify.py under the in-repo
# skill dir, used wherever a test needs the actual `matching_service_names`/
# `_service_covers` rule (AI-468) rather than re-deriving the alias table
# in test code. Pure logic, no network — safe to load at import time.
_VERIFY_MOD = tier2._load_verify_module(tier2.DEFAULT_SKILL_DIR)


def _agent_writes_manifest(manifest_text):
    """A `_run_claude_agent` stand-in: parses the target directory out of
    the `/floxify <dir>` prompt and writes a manifest there, simulating
    the skill's output.

    Used as the agent mock's side_effect rather than writing the manifest
    via the clone mock (as earlier tests did): `process_entry` now strips
    any in-tree `.flox/` between the clone and the agent invocation
    (AI-469), so a manifest planted during the clone step would be
    deleted before the agent mock's return value is ever inspected.
    Writing it here, after the strip point in the real call order,
    matches what the harness actually does.
    """
    def _agent(prompt, skill_dir, timeout=1800):
        target = Path(prompt.split("\n", 1)[0].removeprefix("/floxify ").strip())
        (target / ".flox" / "env").mkdir(parents=True, exist_ok=True)
        (target / ".flox" / "env" / "manifest.toml").write_text(manifest_text)
        # AI-442: _run_claude_agent is a 3-tuple now (adds cost/usage/
        # tool-call meta) -- tier2.py's own call site only mechanically
        # unpacks and discards the third element (out of AI-442 PR 1's
        # scope), so a zeroed stand-in is enough here.
        return "agent output", None, {
            "cost_usd": 0.0, "usage": {}, "duration_ms": 0, "num_turns": 0,
            "tool_calls": {"total": 0, "flox_search": 0, "flox_show": 0},
            "raw_stream": None,
        }
    return _agent


class TestRunGit(unittest.TestCase):
    """`_run_git` wraps subprocess.run into a (ok, error) tuple."""

    @patch("tier2.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        ok, err = tier2._run_git(["git", "init"], timeout=10)
        self.assertTrue(ok)
        self.assertEqual(err, "")

    @patch("tier2.subprocess.run")
    def test_failure_captures_stderr(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=128, stdout="", stderr="fatal: repository not found"
        )
        ok, err = tier2._run_git(["git", "clone", "bad-url"], timeout=10)
        self.assertFalse(ok)
        self.assertIn("fatal: repository not found", err)

    @patch("tier2.subprocess.run")
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=10)
        ok, err = tier2._run_git(["git", "fetch"], timeout=10)
        self.assertFalse(ok)
        self.assertIn("timed out", err)


class TestCloneAtSha(unittest.TestCase):
    """`_clone_at_sha` falls back direct-fetch -> partial-clone -> full-clone."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="tier2-clone-test-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("tier2._try_full_clone")
    @patch("tier2._try_partial_clone")
    @patch("tier2._try_direct_fetch")
    def test_direct_fetch_succeeds_short_circuits(
        self, mock_direct, mock_partial, mock_full
    ):
        mock_direct.return_value = None
        result = tier2._clone_at_sha("https://example.com/r", "abc123", self.tmpdir)
        self.assertIsNone(result)
        mock_direct.assert_called_once()
        mock_partial.assert_not_called()
        mock_full.assert_not_called()

    @patch("tier2._try_full_clone")
    @patch("tier2._try_partial_clone")
    @patch("tier2._try_direct_fetch")
    def test_falls_back_to_partial_clone(self, mock_direct, mock_partial, mock_full):
        mock_direct.return_value = "couldn't find remote ref abc123"
        mock_partial.return_value = None
        result = tier2._clone_at_sha("https://example.com/r", "abc123", self.tmpdir)
        self.assertIsNone(result)
        mock_direct.assert_called_once()
        mock_partial.assert_called_once()
        mock_full.assert_not_called()

    @patch("tier2._try_full_clone")
    @patch("tier2._try_partial_clone")
    @patch("tier2._try_direct_fetch")
    def test_all_strategies_fail_reports_combined_error(
        self, mock_direct, mock_partial, mock_full
    ):
        mock_direct.return_value = "direct failed"
        mock_partial.return_value = "partial failed"
        mock_full.return_value = "full failed"
        result = tier2._clone_at_sha("https://example.com/r", "abc123", self.tmpdir)
        self.assertIsNotNone(result)
        self.assertIn("direct failed", result)
        self.assertIn("partial failed", result)
        self.assertIn("full failed", result)


class TestCaptureAndStripUpstreamFlox(unittest.TestCase):
    """AI-469: a real repo can ship its own hand-maintained .flox/ at the
    pinned SHA (PostHog does — a git-tracked, 207-line manifest.toml).
    That's a real signal worth capturing for the golden-vs-upstream
    review, but the conversion task must not see or be anchored by it:
    one PostHog rep refused to overwrite the upstream manifest, so the
    harness scored the UPSTREAM manifest instead of the skill's output."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="tier2-upstream-flox-test-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_flox_dir_returns_false_none_empty(self):
        had, manifest, files, note = tier2._capture_and_strip_upstream_flox(
            self.tmpdir
        )
        self.assertFalse(had)
        self.assertIsNone(manifest)
        self.assertEqual(files, [])
        self.assertEqual(note, "")

    def test_flox_dir_with_manifest_is_captured_and_stripped(self):
        flox_env = Path(self.tmpdir) / ".flox" / "env"
        flox_env.mkdir(parents=True)
        (flox_env / "manifest.toml").write_text('[install]\nfoo.pkg-path = "foo"\n')

        had, manifest, files, note = tier2._capture_and_strip_upstream_flox(
            self.tmpdir
        )

        self.assertTrue(had)
        self.assertEqual(manifest, '[install]\nfoo.pkg-path = "foo"\n')
        self.assertIn("env/manifest.toml", files)
        self.assertEqual(note, "")
        self.assertFalse((Path(self.tmpdir) / ".flox").exists())

    def test_flox_dir_without_manifest_captures_files_but_manifest_none(self):
        flox_dir = Path(self.tmpdir) / ".flox"
        flox_dir.mkdir()
        (flox_dir / ".gitignore").write_text("cache/\n")

        had, manifest, files, note = tier2._capture_and_strip_upstream_flox(
            self.tmpdir
        )

        self.assertTrue(had)
        self.assertIsNone(manifest)
        self.assertEqual(files, [".gitignore"])
        self.assertEqual(note, "")
        self.assertFalse((Path(self.tmpdir) / ".flox").exists())

    def test_multiple_files_are_all_listed_sorted(self):
        # Mirrors the real PostHog shape: .gitignore, env.json, and three
        # files under env/ (manifest.toml, direnv-setup.sh, on-activate.sh).
        flox_env = Path(self.tmpdir) / ".flox" / "env"
        flox_env.mkdir(parents=True)
        (Path(self.tmpdir) / ".flox" / ".gitignore").write_text("cache/\n")
        (Path(self.tmpdir) / ".flox" / "env.json").write_text("{}\n")
        (flox_env / "manifest.toml").write_text("[install]\n")
        (flox_env / "on-activate.sh").write_text("#!/usr/bin/env bash\n")

        had, _manifest, files, _note = tier2._capture_and_strip_upstream_flox(
            self.tmpdir
        )

        self.assertTrue(had)
        self.assertEqual(
            files,
            [".gitignore", "env.json", "env/manifest.toml", "env/on-activate.sh"],
        )

    def test_symlinked_flox_dir_degrades_to_recorded_state_not_crash(self):
        # PR #49 review I1: shutil.rmtree refuses to operate on a
        # symlinked root, and nothing between process_entry and main's
        # pool.map catches it — a symlinked .flox must never reach that
        # path. The symlink target itself must survive: unlinking the
        # symlink must not delete (or even read) whatever it points to.
        target = Path(tempfile.mkdtemp(prefix="tier2-symlink-target-"))
        try:
            (target / "env").mkdir()
            (target / "env" / "manifest.toml").write_text("SENSITIVE\n")
            flox_link = Path(self.tmpdir) / ".flox"
            flox_link.symlink_to(target, target_is_directory=True)

            had, manifest, files, note = tier2._capture_and_strip_upstream_flox(
                self.tmpdir
            )

            self.assertTrue(had)
            self.assertIsNone(manifest)
            self.assertEqual(files, [])
            self.assertIn("symlink", note)
            self.assertFalse(flox_link.exists())
            # The symlink itself is gone, but its target was never
            # touched — proves we unlinked, not rmtree'd-through.
            self.assertTrue((target / "env" / "manifest.toml").exists())
            self.assertEqual(
                (target / "env" / "manifest.toml").read_text(), "SENSITIVE\n"
            )
        finally:
            shutil.rmtree(target, ignore_errors=True)

    def test_symlinked_manifest_yields_null_capture_with_note(self):
        # PR #49 review I2: exists()/read_text() follow symlinks, so a
        # symlinked manifest.toml would land arbitrary host-file content
        # in upstream_manifest — a value that persists to the results
        # JSON and uploads as a CI artifact. Must never be read through.
        secret = Path(tempfile.mkdtemp(prefix="tier2-symlink-secret-"))
        try:
            secret_file = secret / "secret.toml"
            secret_file.write_text("SENSITIVE\n")
            flox_env = Path(self.tmpdir) / ".flox" / "env"
            flox_env.mkdir(parents=True)
            (flox_env / "manifest.toml").symlink_to(secret_file)

            had, manifest, files, note = tier2._capture_and_strip_upstream_flox(
                self.tmpdir
            )

            self.assertTrue(had)
            self.assertIsNone(manifest)
            self.assertNotIn("SENSITIVE", files)
            self.assertIn("symlink", note)
            # The rest of .flox/ is a real directory tree (only the leaf
            # manifest is a symlink), so the strip itself still succeeds.
            self.assertFalse((Path(self.tmpdir) / ".flox").exists())
        finally:
            shutil.rmtree(secret, ignore_errors=True)


class TestRuntimePinned(unittest.TestCase):
    def test_matches_generic_pkg_path(self):
        manifest = 'ruby.pkg-path = "ruby"\n'
        self.assertTrue(tier2._runtime_pinned(manifest, r"ruby(_[0-9_]+)?"))

    def test_matches_versioned_pkg_path(self):
        manifest = 'ruby.pkg-path = "ruby_3_3"\n'
        self.assertTrue(tier2._runtime_pinned(manifest, r"ruby(_[0-9_]+)?"))

    def test_rejects_near_miss_package(self):
        # rubyPackages.foo must not satisfy a "ruby" runtime pin.
        manifest = 'lint.pkg-path = "rubyPackages.foo"\n'
        self.assertFalse(tier2._runtime_pinned(manifest, r"ruby(_[0-9_]+)?"))

    def test_matches_nodejs_24_exactly(self):
        manifest = 'nodejs.pkg-path = "nodejs_24"\n'
        self.assertTrue(tier2._runtime_pinned(manifest, "nodejs_24"))

    def test_rejects_wrong_node_version(self):
        manifest = 'nodejs.pkg-path = "nodejs_20"\n'
        self.assertFalse(tier2._runtime_pinned(manifest, "nodejs_24"))

    def test_none_manifest_returns_false(self):
        self.assertFalse(tier2._runtime_pinned(None, "nodejs_24"))


class TestLoadVerifyModule(unittest.TestCase):
    """AI-468 fix pass (PR #48 review, I1): a --skill-dir checkout whose
    verify.py loads fine but predates the matching_service_names export
    must degrade to None (fail closed) — the same as an unloadable skill
    dir — not raise AttributeError at the first call site and crash the
    whole run before any results are written. Reproduced by the reviewer
    against a stub old verify.py.

    AI-470/PR #51 review (I1+M1): the guard originally checked only
    `matching_service_names`, leaving `manifest_wires_compose` (this
    ticket's new dependency) unguarded — a checkout with the former but
    not the latter passed the guard, then AttributeError'd inside
    `_structural_checks`, crashing the run. Both exports are now
    required together (`_REQUIRED_VERIFY_EXPORTS`)."""

    @patch("tier2._load_detect_and_verify")
    def test_module_missing_the_export_degrades_to_none(self, mock_load):
        # Has parse_manifest (the older, pre-AI-468 function) but neither
        # of the two required exports — the exact shape of a checkout
        # that predates both tickets.
        old_verify_mod = types.SimpleNamespace(
            parse_manifest=lambda text: ({}, None),
        )
        mock_load.return_value = (None, old_verify_mod)
        self.assertIsNone(tier2._load_verify_module("/some/skill/dir"))

    @patch("tier2._load_detect_and_verify")
    def test_module_with_matching_service_names_but_not_compose_check_degrades_to_none(
        self, mock_load
    ):
        # AI-470/PR #51 review's exact repro: a checkout between AI-466
        # and AI-468/AI-470 in commit order — has the AI-468 export this
        # harness already guarded, but not the newer AI-470 dependency.
        # Must fail closed, not AttributeError three frames later inside
        # _structural_checks.
        partial_verify_mod = types.SimpleNamespace(
            parse_manifest=lambda text: ({}, None),
            matching_service_names=lambda manifest, kind: [],
        )
        mock_load.return_value = (None, partial_verify_mod)
        self.assertIsNone(tier2._load_verify_module("/some/skill/dir"))

    @patch("tier2._load_detect_and_verify")
    def test_module_with_both_exports_loads_normally(self, mock_load):
        current_verify_mod = types.SimpleNamespace(
            parse_manifest=lambda text: ({}, None),
            matching_service_names=lambda manifest, kind: [],
            manifest_wires_compose=lambda manifest: False,
        )
        mock_load.return_value = (None, current_verify_mod)
        self.assertIs(
            tier2._load_verify_module("/some/skill/dir"), current_verify_mod
        )

    @patch("tier2._load_detect_and_verify", side_effect=FileNotFoundError("boom"))
    def test_load_failure_returns_none(self, _mock_load):
        self.assertIsNone(tier2._load_verify_module("/nonexistent"))

    def test_partial_module_flows_through_structural_checks_without_crashing(self):
        # End-to-end through the same path PR #51 review reproduced the
        # crash in: a module with matching_service_names but not
        # manifest_wires_compose must degrade to None and let
        # _structural_checks fail has_service_* closed, not raise, for a
        # deferred-ok service that would otherwise call the missing method.
        partial_verify_mod = types.SimpleNamespace(
            parse_manifest=lambda text: ({"services": {}}, None),
            matching_service_names=lambda manifest, kind: [],
        )
        with patch("tier2._load_detect_and_verify",
                    return_value=(None, partial_verify_mod)):
            verify_mod = tier2._load_verify_module("/some/skill/dir")
        self.assertIsNone(verify_mod)
        entry = {
            "id": "x", "expected_runtimes": [],
            "expected_services": [{"name": "clickhouse", "disposition": "deferred-ok"}],
        }
        manifest = (
            '[install]\n'
            'docker-compose.pkg-path = "docker-compose"\n'
            '[hook]\n'
            'on-activate = "docker-compose up -d clickhouse"\n'
        )
        checks = tier2._structural_checks(entry, manifest, verify_mod=verify_mod)
        self.assertFalse(checks["has_service_clickhouse"], checks)

    def test_old_module_flows_through_structural_checks_without_crashing(self):
        # End-to-end through the same path PR #48 review reproduced the
        # crash in: an old-shaped verify_mod reaching _structural_checks
        # must fail has_service_* closed, not raise.
        old_verify_mod = types.SimpleNamespace(
            parse_manifest=lambda text: ({"services": {"postgres": {}}}, None),
        )
        with patch("tier2._load_detect_and_verify",
                    return_value=(None, old_verify_mod)):
            verify_mod = tier2._load_verify_module("/some/skill/dir")
        entry = {"id": "x", "expected_runtimes": [], "expected_services": ["postgres"]}
        checks = tier2._structural_checks(
            entry, "[services.postgres]\ncommand = \"postgres\"\n",
            verify_mod=verify_mod,
        )
        self.assertFalse(checks["has_service_postgres"], checks)


class TestMatchingServiceNames(unittest.TestCase):
    """AI-468: `_matching_service_names`/`_parsed_manifest` are the tier2-
    side glue around verify.py's shared `matching_service_names` rule —
    name OR command match against SERVICE_KIND_ALIASES, not name-only.
    The alias table itself is exercised in verify.py's own test_verify.py;
    this covers the glue (parsing + None-handling), using the real loaded
    module rather than a stub, so no alias table is re-derived here."""

    def test_name_match_still_works(self):
        manifest = tier2._parsed_manifest(
            _VERIFY_MOD, "[services.postgres]\ncommand = \"postgres\"\n"
        )
        self.assertEqual(
            tier2._matching_service_names(_VERIFY_MOD, manifest, "postgres"),
            ["postgres"],
        )

    def test_unconventional_name_with_kind_matching_command_matches(self):
        # The exact AI-468 shape: `[services.db]` running postgres, which
        # tier2's old name-only regex reported as "not declared".
        manifest = tier2._parsed_manifest(
            _VERIFY_MOD, '[services.db]\ncommand = "postgres -D /data"\n'
        )
        self.assertEqual(
            tier2._matching_service_names(_VERIFY_MOD, manifest, "postgres"),
            ["db"],
        )

    def test_genuinely_absent_service_does_not_match(self):
        manifest = tier2._parsed_manifest(
            _VERIFY_MOD, "[services.redis]\ncommand = \"redis-server\"\n"
        )
        self.assertEqual(
            tier2._matching_service_names(_VERIFY_MOD, manifest, "postgres"), []
        )

    def test_none_manifest_dict_returns_empty(self):
        self.assertEqual(
            tier2._matching_service_names(_VERIFY_MOD, None, "postgres"), []
        )

    def test_none_verify_mod_returns_empty_without_raising(self):
        self.assertEqual(tier2._parsed_manifest(None, "[services.postgres]\n"), None)
        self.assertEqual(
            tier2._matching_service_names(None, {"services": {}}, "postgres"), []
        )


class TestServiceExpectation(unittest.TestCase):
    """AI-470: `_service_expectation` normalizes an `expected_services`
    registry entry to (name, disposition) — accepting both the pre-AI-470
    bare-string shape (implicit expect-wired) and the new
    {"name", "disposition"} dict shape, so every fixture but posthog's
    unmodified entries keep working without a schema migration."""

    def test_bare_string_defaults_to_expect_wired(self):
        self.assertEqual(
            tier2._service_expectation("postgres"), ("postgres", "expect-wired")
        )

    def test_dict_with_explicit_disposition(self):
        self.assertEqual(
            tier2._service_expectation({"name": "clickhouse", "disposition": "deferred-ok"}),
            ("clickhouse", "deferred-ok"),
        )

    def test_dict_without_disposition_defaults_to_expect_wired(self):
        self.assertEqual(
            tier2._service_expectation({"name": "postgres"}),
            ("postgres", "expect-wired"),
        )


class TestComposeWired(unittest.TestCase):
    """AI-470: `_compose_wired` is a thin wrapper around verify.py's own
    public `manifest_wires_compose` (AI-466's carve-out, promoted to a
    public export by PR #51 review) — no re-derivation of what
    "genuinely invokes docker-compose" means."""

    def test_manifest_that_wires_compose(self):
        manifest = tier2._parsed_manifest(
            _VERIFY_MOD,
            '[install]\n'
            'docker-compose.pkg-path = "docker-compose"\n'
            '[hook]\n'
            'on-activate = "docker-compose up -d clickhouse"\n',
        )
        self.assertTrue(tier2._compose_wired(_VERIFY_MOD, manifest))

    def test_manifest_that_does_not_wire_compose(self):
        manifest = tier2._parsed_manifest(_VERIFY_MOD, "[install]\n")
        self.assertFalse(tier2._compose_wired(_VERIFY_MOD, manifest))

    def test_none_manifest_dict_is_false_not_a_crash(self):
        self.assertFalse(tier2._compose_wired(_VERIFY_MOD, None))

    def test_none_verify_mod_is_false_not_a_crash(self):
        self.assertFalse(tier2._compose_wired(None, {"hook": {}}))


class TestServiceDispositionResults(unittest.TestCase):
    """AI-470: `_service_disposition_results` is the honest wired/
    deferred/missing record behind `has_service_<kind>` — independent of
    whether that outcome satisfies the structural check."""

    def _entry(self, expected_services):
        return {"id": "x", "expected_services": expected_services}

    def test_wired_service_is_recorded_wired(self):
        entry = self._entry([{"name": "postgres", "disposition": "expect-wired"}])
        manifest = '[services.postgres]\ncommand = "postgres"\n'
        results = tier2._service_disposition_results(entry, manifest, _VERIFY_MOD)
        self.assertEqual(results, {"postgres": "wired"})

    def test_deferred_ok_with_mechanism_is_recorded_deferred(self):
        entry = self._entry([{"name": "clickhouse", "disposition": "deferred-ok"}])
        manifest = (
            '[install]\n'
            'docker-compose.pkg-path = "docker-compose"\n'
            '[hook]\n'
            'on-activate = "docker-compose up -d clickhouse"\n'
        )
        results = tier2._service_disposition_results(entry, manifest, _VERIFY_MOD)
        self.assertEqual(results, {"clickhouse": "deferred"})

    def test_deferred_ok_without_mechanism_is_recorded_missing(self):
        entry = self._entry([{"name": "clickhouse", "disposition": "deferred-ok"}])
        manifest = "[install]\n"
        results = tier2._service_disposition_results(entry, manifest, _VERIFY_MOD)
        self.assertEqual(results, {"clickhouse": "missing"})

    def test_expect_wired_with_compose_but_not_wired_is_recorded_missing(self):
        # A compose invocation does NOT count for expect-wired — only
        # deferred-ok accepts a mechanism in place of direct wiring.
        entry = self._entry([{"name": "postgres", "disposition": "expect-wired"}])
        manifest = (
            '[install]\n'
            'docker-compose.pkg-path = "docker-compose"\n'
            '[hook]\n'
            'on-activate = "docker-compose up -d postgres"\n'
        )
        results = tier2._service_disposition_results(entry, manifest, _VERIFY_MOD)
        self.assertEqual(results, {"postgres": "missing"})

    def test_bare_string_entry_defaults_to_expect_wired(self):
        entry = self._entry(["redis"])
        results = tier2._service_disposition_results(entry, "[install]\n", _VERIFY_MOD)
        self.assertEqual(results, {"redis": "missing"})


class TestStructuralChecks(unittest.TestCase):
    def test_full_conformant_manifest(self):
        entry = {
            "id": "mastodon",
            "expected_runtimes": [
                {"name": "ruby", "pattern": r"ruby(_[0-9_]+)?"},
                {"name": "nodejs_24", "pattern": "nodejs_24"},
            ],
            "expected_services": ["postgres", "redis"],
        }
        manifest = (
            'schema-version = "1.13.0"\n'
            "[install]\n"
            'ruby.pkg-path = "ruby"\n'
            'nodejs.pkg-path = "nodejs_24"\n'
            "[services.postgres]\n"
            'command = "postgres"\n'
            "[services.redis]\n"
            'command = "redis-server"\n'
        )
        checks = tier2._structural_checks(entry, manifest, verify_mod=_VERIFY_MOD)
        self.assertTrue(checks["manifest_created"])
        self.assertTrue(checks["valid_toml"])
        self.assertTrue(checks["no_abs_paths"])
        self.assertTrue(checks["pins_ruby"])
        self.assertTrue(checks["pins_nodejs_24"])
        self.assertTrue(checks["has_service_postgres"])
        self.assertTrue(checks["has_service_redis"])
        self.assertTrue(all(checks.values()))

    def test_missing_service_fails_that_check_only(self):
        entry = {
            "id": "mastodon",
            "expected_runtimes": [{"name": "ruby", "pattern": r"ruby(_[0-9_]+)?"}],
            "expected_services": ["postgres", "redis"],
        }
        manifest = (
            'schema-version = "1.13.0"\n'
            "[install]\n"
            'ruby.pkg-path = "ruby"\n'
            "[services.postgres]\n"
            'command = "postgres"\n'
        )
        checks = tier2._structural_checks(entry, manifest, verify_mod=_VERIFY_MOD)
        self.assertTrue(checks["pins_ruby"])
        self.assertTrue(checks["has_service_postgres"])
        self.assertFalse(checks["has_service_redis"])
        self.assertFalse(all(checks.values()))

    def test_unconventionally_named_service_passes_the_structural_check(self):
        # AI-468: has_service_<kind> means "a service of this kind exists",
        # not "a service named this exists" — a [services.db] running
        # postgres must now satisfy has_service_postgres.
        entry = {
            "id": "x",
            "expected_runtimes": [],
            "expected_services": ["postgres"],
        }
        manifest = (
            "[install]\n"
            'pg.pkg-path = "postgresql_16"\n'
            "[services.db]\n"
            'command = "postgres -D /data"\n'
        )
        checks = tier2._structural_checks(entry, manifest, verify_mod=_VERIFY_MOD)
        self.assertTrue(checks["has_service_postgres"], checks)

    def test_genuinely_absent_service_fails_the_structural_check(self):
        entry = {"id": "x", "expected_runtimes": [], "expected_services": ["postgres"]}
        manifest = "[services.redis]\ncommand = \"redis-server\"\n"
        checks = tier2._structural_checks(entry, manifest, verify_mod=_VERIFY_MOD)
        self.assertFalse(checks["has_service_postgres"], checks)

    def test_no_verify_mod_fails_has_service_closed(self):
        # A skill-dir load failure must not silently pass a service check
        # it never actually evaluated.
        entry = {"id": "x", "expected_runtimes": [], "expected_services": ["postgres"]}
        manifest = "[services.postgres]\ncommand = \"postgres\"\n"
        checks = tier2._structural_checks(entry, manifest, verify_mod=None)
        self.assertFalse(checks["has_service_postgres"], checks)

    def test_no_manifest_fails_everything(self):
        entry = {
            "id": "mastodon",
            "expected_runtimes": [{"name": "ruby", "pattern": r"ruby(_[0-9_]+)?"}],
            "expected_services": ["postgres"],
        }
        checks = tier2._structural_checks(entry, None, verify_mod=_VERIFY_MOD)
        self.assertFalse(checks["manifest_created"])
        self.assertFalse(checks["valid_toml"])
        self.assertFalse(checks["no_abs_paths"])
        self.assertFalse(checks["pins_ruby"])
        self.assertFalse(checks["has_service_postgres"])

    def test_absolute_path_fails_check(self):
        entry = {"id": "x", "expected_runtimes": [], "expected_services": []}
        manifest = (
            "[vars]\n"
            'cache_dir = "/home/user/.cache"\n'
        )
        checks = tier2._structural_checks(entry, manifest)
        self.assertFalse(checks["no_abs_paths"])

    # --- disposition semantics (AI-470) ---------------------------------

    def test_deferred_ok_wired_directly_passes(self):
        entry = {
            "id": "x", "expected_runtimes": [],
            "expected_services": [{"name": "clickhouse", "disposition": "deferred-ok"}],
        }
        manifest = '[services.clickhouse]\ncommand = "clickhouse-server"\n'
        checks = tier2._structural_checks(entry, manifest, verify_mod=_VERIFY_MOD)
        self.assertTrue(checks["has_service_clickhouse"], checks)

    def test_deferred_ok_with_compose_mechanism_passes(self):
        # The exact posthog shape: not wired natively, but the manifest
        # genuinely invokes docker-compose from [hook] — deferred-ok must
        # pass, not fail, when a real mechanism stands in for direct wiring.
        entry = {
            "id": "x", "expected_runtimes": [],
            "expected_services": [{"name": "clickhouse", "disposition": "deferred-ok"}],
        }
        manifest = (
            '[install]\n'
            'docker-compose.pkg-path = "docker-compose"\n'
            '[hook]\n'
            'on-activate = "docker-compose up -d clickhouse"\n'
        )
        checks = tier2._structural_checks(entry, manifest, verify_mod=_VERIFY_MOD)
        self.assertTrue(checks["has_service_clickhouse"], checks)

    def test_expect_wired_with_only_compose_mechanism_fails(self):
        # The same compose mechanism does NOT satisfy expect-wired — only
        # deferred-ok accepts a mechanism in place of direct wiring.
        entry = {
            "id": "x", "expected_runtimes": [],
            "expected_services": [{"name": "postgres", "disposition": "expect-wired"}],
        }
        manifest = (
            '[install]\n'
            'docker-compose.pkg-path = "docker-compose"\n'
            '[hook]\n'
            'on-activate = "docker-compose up -d postgres"\n'
        )
        checks = tier2._structural_checks(entry, manifest, verify_mod=_VERIFY_MOD)
        self.assertFalse(checks["has_service_postgres"], checks)

    def test_silently_dropped_service_fails_both_dispositions(self):
        # No wiring, no mechanism -- must fail regardless of disposition.
        # This is the exact failure the harness must never wave through
        # by "helpfully" treating deferred-ok as always-optional.
        manifest = "[install]\n"
        for disposition in ("expect-wired", "deferred-ok"):
            entry = {
                "id": "x", "expected_runtimes": [],
                "expected_services": [{"name": "redis", "disposition": disposition}],
            }
            checks = tier2._structural_checks(entry, manifest, verify_mod=_VERIFY_MOD)
            self.assertFalse(
                checks["has_service_redis"],
                f"disposition={disposition!r} must not pass a silently-dropped service",
            )


class TestLoadRegistry(unittest.TestCase):
    def test_parses_jsonl_lines(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            f.write(json.dumps({"id": "a"}) + "\n")
            f.write("\n")  # blank lines are skipped
            f.write(json.dumps({"id": "b"}) + "\n")
            path = f.name
        try:
            entries = tier2._load_registry(path)
            self.assertEqual([e["id"] for e in entries], ["a", "b"])
        finally:
            Path(path).unlink()


class TestRegistrySchema(unittest.TestCase):
    """AI-470: every `expected_services` entry in the real tier2.jsonl must
    be a well-formed {"name", "disposition"} dict with a recognized
    disposition — and every fixture but posthog must be behavior-
    preserving (all expect-wired, the pre-AI-470 default)."""

    @classmethod
    def setUpClass(cls):
        here = Path(tier2.__file__).resolve().parent
        cls.entries = tier2._load_registry(here / "tier2.jsonl")

    def test_every_expected_services_entry_is_a_well_formed_dict(self):
        for entry in self.entries:
            for service in entry.get("expected_services", []):
                self.assertIsInstance(
                    service, dict,
                    f"{entry['id']}: expected_services entries must be dicts "
                    f"post-AI-470, got {service!r}",
                )
                self.assertIn("name", service, entry["id"])
                self.assertIn("disposition", service, entry["id"])
                self.assertIn(
                    service["disposition"], tier2.KNOWN_DISPOSITIONS,
                    f"{entry['id']}: unrecognized disposition "
                    f"{service['disposition']!r}",
                )

    def test_posthog_has_the_adjudicated_dispositions(self):
        posthog = next(e for e in self.entries if e["id"] == "posthog")
        by_name = {s["name"]: s["disposition"] for s in posthog["expected_services"]}
        self.assertEqual(by_name.get("postgres"), "expect-wired")
        self.assertEqual(by_name.get("redis"), "expect-wired")
        self.assertEqual(by_name.get("clickhouse"), "deferred-ok")

    def test_every_other_fixture_is_behavior_preserving_expect_wired(self):
        for entry in self.entries:
            if entry["id"] == "posthog":
                continue
            for service in entry.get("expected_services", []):
                self.assertEqual(
                    service["disposition"], "expect-wired",
                    f"{entry['id']}.{service['name']}: only posthog's "
                    f"clickhouse is deferred-ok per AI-470 — every other "
                    f"fixture's expectations must stay behavior-preserving",
                )


def _run(hard_pass=True, score=5, correct=True, error=None, verify=None):
    """Build a per-run result the shape `process_entry` returns."""
    if error is not None:
        return {"id": "x", "error": error}
    result = {
        "id": "x",
        "hard_pass": hard_pass,
        "hard_checks": {"manifest_created": hard_pass},
        "judge": {"score": score, "correct": correct},
        "activation": {"ok": None, "skipped": True},
    }
    if verify is not None:
        result["verify"] = verify
    return result


class TestSummarize(unittest.TestCase):
    """Guards the reps>1 reporting bug: an aggregate entry has no top-level
    `judge` key, so an unflattened summary silently reported all-zeros even
    when every run passed. `_summarize` must flatten first."""

    def test_reps_1_plain_entries(self):
        results = [_run(hard_pass=True, score=5)]
        summary = tier2._summarize(results, "skill@branch")
        self.assertEqual(summary["n_repos"], 1)
        self.assertEqual(summary["n"], 1)
        self.assertEqual(summary["hard_pass_rate"], 1.0)
        self.assertEqual(summary["avg_judge_score"], 5.0)
        self.assertEqual(summary["n_errors"], 0)

    def test_reps_gt_1_aggregate_is_flattened_not_zeroed(self):
        # Aggregate shape from process_task when reps>1: no top-level judge.
        results = [
            {
                "id": "mastodon",
                "reps": 2,
                "runs": [_run(hard_pass=True, score=5), _run(hard_pass=True, score=4)],
                "hard_pass_rate_across_reps": 1.0,
            }
        ]
        summary = tier2._summarize(results, "skill@branch")
        self.assertEqual(summary["n_repos"], 1)     # one repo
        self.assertEqual(summary["n"], 2)           # two scored runs, NOT zero
        self.assertEqual(summary["hard_pass_rate"], 1.0)
        self.assertEqual(summary["avg_judge_score"], 4.5)
        self.assertEqual(summary["n_errors"], 0)

    def test_reps_gt_1_counts_error_runs(self):
        results = [
            {
                "id": "x",
                "reps": 2,
                "runs": [_run(error="clone failed"), _run(hard_pass=True, score=3)],
            }
        ]
        summary = tier2._summarize(results, "s")
        self.assertEqual(summary["n_errors"], 1)
        self.assertEqual(summary["n"], 1)  # one scored run among the two

    def test_verify_fields_flow_through(self):
        # AI-465: tier2 runs must feed _stats the same "verify" shape
        # run_floxify.py produces, or verify_checked/verify_clean/
        # verify_hard_violation_rate silently stay zero for tier2 runs.
        results = [
            _run(hard_pass=True, score=5, verify={
                "hard_count": 0, "advisory_count": 0, "catalog_checked": True,
            }),
        ]
        summary = tier2._summarize(results, "skill@branch")
        self.assertEqual(summary["verify_checked"], 1)
        self.assertEqual(summary["verify_clean"], 1)
        self.assertEqual(summary["verify_hard_violation_rate"], 0.0)

    def test_verify_hard_violation_lowers_clean_count_not_checked_count(self):
        results = [
            _run(hard_pass=True, score=5, verify={
                "hard_count": 2, "advisory_count": 0, "catalog_checked": True,
            }),
        ]
        summary = tier2._summarize(results, "skill@branch")
        self.assertEqual(summary["verify_checked"], 1)
        self.assertEqual(summary["verify_clean"], 0)
        self.assertEqual(summary["verify_hard_violation_rate"], 1.0)

    def test_no_verify_block_leaves_rate_none(self):
        results = [_run(hard_pass=True, score=5)]
        summary = tier2._summarize(results, "skill@branch")
        self.assertEqual(summary["verify_checked"], 0)
        self.assertIsNone(summary["verify_hard_violation_rate"])


class TestRegistryPatternDriftGuard(unittest.TestCase):
    """Runs the real mastodon registry patterns against a committed capture
    of the skill's actual manifest output. Fails if a future registry-pattern
    edit silently stops matching real output (regex drift)."""

    def test_mastodon_patterns_match_real_manifest(self):
        here = Path(tier2.__file__).resolve().parent
        entry = next(
            e for e in tier2._load_registry(here / "tier2.jsonl")
            if e["id"] == "mastodon"
        )
        manifest = (here / "testdata" / "mastodon-manifest.toml").read_text()
        checks = tier2._structural_checks(entry, manifest, verify_mod=_VERIFY_MOD)
        self.assertTrue(checks["pins_ruby_4_0"], checks)
        self.assertTrue(checks["pins_nodejs_24"], checks)
        self.assertTrue(checks["has_service_postgres"], checks)
        self.assertTrue(checks["has_service_redis"], checks)
        self.assertTrue(checks["no_abs_paths"], checks)
        self.assertTrue(all(checks.values()), checks)


class TestProbeCommandFor(unittest.TestCase):
    """AI-447: per-kind connectivity probes.

    The postgres probe deliberately passes NO host/port. `pg_isready` reads
    PGHOST/PGPORT from the environment, and the environment is what the
    manifest's own [vars] set — so a bare `pg_isready` asserts the service is
    reachable *at the address the manifest advertises*. That is the check that
    catches a manifest whose [vars] point at a datastore nothing serves.
    """

    def test_postgres_probe_is_bare_pg_isready(self):
        cmd = tier2._probe_command_for("postgres")
        self.assertIn("pg_isready", cmd)
        self.assertNotIn("-h ", cmd)
        self.assertNotIn("-p ", cmd)

    def test_postgresql_alias_resolves(self):
        self.assertIn("pg_isready", tier2._probe_command_for("postgresql"))

    def test_redis_probe_expects_pong(self):
        cmd = tier2._probe_command_for("redis")
        self.assertIn("redis-cli", cmd)
        self.assertIn("ping", cmd.lower())

    def test_mariadb_probe(self):
        self.assertIn("admin", tier2._probe_command_for("mariadb"))

    def test_unknown_kind_has_no_probe(self):
        self.assertIsNone(tier2._probe_command_for("clickhouse"))


class TestProbeServices(unittest.TestCase):
    """AI-447: prove services actually serve, not just that a section exists.

    Services can only be started from *inside* an activation — `flox services
    start` on an unactivated env errors with "Cannot start services for an
    environment that is not activated". So the probe is a single
    `flox activate --start-services -c <script>`, where the script polls the
    connectivity probe and prints a sentinel.

    The sentinels matter: they separate "the service did not serve" (a real
    verdict about the manifest) from "flox/the environment errored" (a harness
    problem that must never be reported as a service failure).
    """

    @patch("tier2.shutil.which", return_value=None)
    def test_flox_absent_skips_rather_than_fails(self, _which):
        res = tier2._probe_services("/tmp/x", ["postgres"])
        self.assertTrue(res["postgres"]["skipped"])
        self.assertIsNone(res["postgres"]["ok"])

    @patch("tier2.shutil.which", return_value="/usr/bin/flox")
    @patch("tier2._run_flox")
    def test_uses_activate_start_services_not_bare_services_start(
        self, mock_flox, _which
    ):
        mock_flox.return_value = (True, "__SERVICE_OK__")
        tier2._probe_services("/tmp/x", ["postgres"])
        args = mock_flox.call_args_list[0].args[0]
        self.assertIn("activate", args)
        self.assertIn("--start-services", args)

    @patch("tier2.shutil.which", return_value="/usr/bin/flox")
    @patch("tier2._run_flox")
    def test_service_serving_is_ok(self, mock_flox, _which):
        mock_flox.return_value = (True, "__SERVICE_OK__")
        res = tier2._probe_services("/tmp/x", ["postgres"])
        self.assertTrue(res["postgres"]["ok"], res)
        self.assertFalse(res["postgres"]["skipped"])

    @patch("tier2.shutil.which", return_value="/usr/bin/flox")
    @patch("tier2._run_flox")
    def test_service_never_comes_up_is_a_real_failure(self, mock_flox, _which):
        # THE case this ticket exists for: [services.*] present, activation ok,
        # but nothing ever answers on the advertised address.
        mock_flox.return_value = (False, "__SERVICE_DEAD__")
        res = tier2._probe_services("/tmp/x", ["postgres"])
        self.assertFalse(res["postgres"]["ok"], res)
        self.assertFalse(res["postgres"]["skipped"], res)

    @patch("tier2.shutil.which", return_value="/usr/bin/flox")
    @patch("tier2._run_flox")
    def test_harness_error_is_skipped_not_a_service_failure(self, mock_flox, _which):
        # No sentinel in the output => flox itself errored (bad flag, env
        # broken, timeout). Reporting that as "your postgres is broken" would
        # be a lie — exactly the confusion AI-454 flags for activation.
        mock_flox.return_value = (False, "ERROR: unknown flag --start-services")
        res = tier2._probe_services("/tmp/x", ["postgres"])
        self.assertTrue(res["postgres"]["skipped"], res)
        self.assertIsNone(res["postgres"]["ok"], res)
        self.assertIn("could not be probed", res["postgres"]["notes"])

    @patch("tier2.shutil.which", return_value="/usr/bin/flox")
    @patch("tier2._run_flox")
    def test_unprobeable_service_is_skipped_not_failed(self, mock_flox, _which):
        # clickhouse has no probe; absence of a probe must never read as failure.
        mock_flox.return_value = (True, "__SERVICE_OK__")
        res = tier2._probe_services("/tmp/x", ["clickhouse"])
        self.assertTrue(res["clickhouse"]["skipped"])
        self.assertIsNone(res["clickhouse"]["ok"])

    @patch("tier2.shutil.which", return_value="/usr/bin/flox")
    @patch("tier2._run_flox")
    def test_undeclared_service_is_not_probed(self, mock_flox, _which):
        """A service the manifest never declared must not be probed at all.

        Regression: lemmy produced a manifest with NO [services.*] section
        whose [hook] nevertheless started postgres to bootstrap the database.
        A bare `pg_isready` then answered, and the probe reported OK — for an
        environment with no service. A hook-spawned postgres is not a
        Flox-managed service: `flox services` can't start/stop/status it and it
        dies with the activation. Crediting it is a false positive.

        `has_service_*` owns "did you wire it"; the probe owns "does the wired
        service work". Probing an undeclared service answers neither.
        """
        manifest = '[install]\npg.pkg-path = "postgresql_16"\n[hook]\non-activate = "pg_ctl start"\n'
        res = tier2._probe_services(
            "/tmp/x", ["postgres"], manifest_text=manifest, verify_mod=_VERIFY_MOD,
        )
        self.assertTrue(res["postgres"]["skipped"], res)
        self.assertIsNone(res["postgres"]["ok"], res)
        self.assertIn("no [services.*] entry matches", res["postgres"]["notes"])
        mock_flox.assert_not_called()

    @patch("tier2.shutil.which", return_value="/usr/bin/flox")
    @patch("tier2._run_flox")
    def test_declared_service_is_probed(self, mock_flox, _which):
        mock_flox.return_value = (True, "__SERVICE_OK__")
        manifest = '[services.postgres]\ncommand = "postgres"\n'
        res = tier2._probe_services(
            "/tmp/x", ["postgres"], manifest_text=manifest, verify_mod=_VERIFY_MOD,
        )
        self.assertTrue(res["postgres"]["ok"], res)
        mock_flox.assert_called()

    @patch("tier2.shutil.which", return_value="/usr/bin/flox")
    @patch("tier2._run_flox")
    def test_unconventionally_named_service_resolves_as_probe_target(
        self, mock_flox, _which
    ):
        # AI-468: the exact rep-3 shape — a [services.db] entry running
        # postgres, which the old name-only gate reported as "not
        # declared" and skipped, so it was never probed.
        mock_flox.return_value = (True, "__SERVICE_OK__")
        manifest = '[services.db]\ncommand = "postgres -D /data"\n'
        res = tier2._probe_services(
            "/tmp/x", ["postgres"], manifest_text=manifest, verify_mod=_VERIFY_MOD,
        )
        self.assertTrue(res["postgres"]["ok"], res)
        self.assertFalse(res["postgres"]["skipped"], res)
        mock_flox.assert_called()

    @patch("tier2.shutil.which", return_value="/usr/bin/flox")
    @patch("tier2._run_flox")
    def test_multiple_matches_probes_first_and_notes_ambiguity(
        self, mock_flox, _which
    ):
        mock_flox.return_value = (True, "__SERVICE_OK__")
        manifest = (
            '[services.postgres]\ncommand = "postgres"\n'
            '[services.pg-replica]\ncommand = "postgres --replica"\n'
        )
        res = tier2._probe_services(
            "/tmp/x", ["postgres"], manifest_text=manifest, verify_mod=_VERIFY_MOD,
        )
        self.assertTrue(res["postgres"]["ok"], res)
        self.assertIn("multiple", res["postgres"]["notes"])
        self.assertIn("postgres", res["postgres"]["notes"])

    @patch("tier2.shutil.which", return_value="/usr/bin/flox")
    @patch("tier2._run_flox")
    def test_probe_script_polls_for_readiness(self, mock_flox, _which):
        # Services start asynchronously — a single immediate probe would race.
        mock_flox.return_value = (True, "__SERVICE_OK__")
        tier2._probe_services("/tmp/x", ["postgres"])
        script = mock_flox.call_args_list[0].args[0][-1]
        self.assertIn("pg_isready", script)
        self.assertIn("sleep", script)


class TestProcessEntryVerifyLeg(unittest.TestCase):
    """AI-465: tier2.py never ran the deterministic verify.py leg
    run_floxify.py's Tier 1 harness runs (AI-461) — it trusted the
    skill's self-report. `process_entry` must reuse `_run_verify` the
    same way Tier 1's `process_task` does: re-scan the cloned checkout,
    record a per-repo `verify` block, and feed the confirmed-catalog
    note to the judge.

    Clone, agent invocation, and (where irrelevant to the case) the
    judge are mocked — no network, no `claude`, no real repo clone."""

    def _entry(self, **overrides):
        entry = {
            "id": "x", "repo_url": "https://example.com/r", "sha": "abc123",
            "expected_runtimes": [], "expected_services": [],
        }
        entry.update(overrides)
        return entry

    @patch("tier2._judge_tier2")
    @patch("tier2._run_verify")
    @patch("tier2._run_claude_agent")
    @patch("tier2._clone_at_sha")
    def test_verify_leg_result_recorded_in_output(
        self, mock_clone, mock_agent, mock_verify, mock_judge
    ):
        mock_clone.return_value = None
        mock_agent.side_effect = _agent_writes_manifest("[install]\n")
        mock_verify.return_value = {
            "violations": [
                {"rule": "vars-not-literal", "severity": "hard", "message": "m"},
                {"rule": "outputs-heuristic", "severity": "advisory", "message": "n"},
            ],
            "catalog_checked": False,
            "catalog_unknown": [],
        }
        mock_judge.return_value = {"score": 4, "correct": True, "issues": []}

        result = tier2.process_entry(self._entry(), "/fake/skill/dir")

        self.assertIn("verify", result)
        self.assertEqual(result["verify"]["hard_count"], 1)
        self.assertEqual(result["verify"]["advisory_count"], 1)
        self.assertFalse(result["verify"]["catalog_checked"])
        self.assertEqual(len(result["verify"]["violations"]), 2)

    @patch("tier2._judge_tier2")
    @patch("tier2._run_verify")
    @patch("tier2._run_claude_agent")
    @patch("tier2._clone_at_sha")
    @patch("tier2._check_activation", return_value=(True, False, ""))
    def test_catalog_live_follows_activate_true(
        self, mock_check_act, mock_clone, mock_agent, mock_verify, mock_judge
    ):
        mock_clone.return_value = None
        mock_agent.side_effect = _agent_writes_manifest("[install]\n")
        mock_verify.return_value = {
            "violations": [], "catalog_checked": True, "catalog_unknown": [],
        }
        mock_judge.return_value = {"score": 5, "correct": True, "issues": []}

        tier2.process_entry(self._entry(), "/fake/skill/dir", activate=True)

        self.assertTrue(mock_verify.call_args.kwargs["check_catalog_live"])

    @patch("tier2._judge_tier2")
    @patch("tier2._run_verify")
    @patch("tier2._run_claude_agent")
    @patch("tier2._clone_at_sha")
    def test_catalog_live_follows_activate_false(
        self, mock_clone, mock_agent, mock_verify, mock_judge
    ):
        # --activate is opt-in and off by default; the catalog sub-leg
        # must not attempt a live check when the caller never opted in.
        mock_clone.return_value = None
        mock_agent.side_effect = _agent_writes_manifest("[install]\n")
        mock_verify.return_value = {
            "violations": [], "catalog_checked": False, "catalog_unknown": [],
        }
        mock_judge.return_value = {"score": 5, "correct": True, "issues": []}

        tier2.process_entry(self._entry(), "/fake/skill/dir", activate=False)

        self.assertFalse(mock_verify.call_args.kwargs["check_catalog_live"])

    @patch("tier2._judge_tier2")
    @patch("tier2._run_verify")
    @patch("tier2._run_claude_agent")
    @patch("tier2._clone_at_sha")
    def test_verify_result_fed_to_judge(
        self, mock_clone, mock_agent, mock_verify, mock_judge
    ):
        mock_clone.return_value = None
        mock_agent.side_effect = _agent_writes_manifest("[install]\n")
        sentinel = {"violations": [], "catalog_checked": True, "catalog_unknown": []}
        mock_verify.return_value = sentinel
        mock_judge.return_value = {"score": 5, "correct": True, "issues": []}

        tier2.process_entry(self._entry(), "/fake/skill/dir")

        self.assertIs(mock_judge.call_args.kwargs["verify_result"], sentinel)

    @patch("tier2._judge_tier2")
    @patch("tier2._run_claude_agent")
    @patch("tier2._clone_at_sha")
    def test_no_manifest_records_verify_as_skipped_not_error(
        self, mock_clone, mock_agent, mock_judge
    ):
        # Clone succeeds but the skill never wrote a manifest — _run_verify
        # (not mocked here) must short-circuit to a skip, matching Tier 1's
        # own no-manifest test in test_run_floxify.py.
        mock_clone.return_value = None
        mock_agent.return_value = ("agent output", None, {
            "cost_usd": 0.0, "usage": {}, "duration_ms": 0, "num_turns": 0,
            "tool_calls": {"total": 0, "flox_search": 0, "flox_show": 0},
            "raw_stream": None,
        })
        mock_judge.return_value = {"score": 0, "correct": False, "issues": []}

        result = tier2.process_entry(self._entry(), str(tier2.DEFAULT_SKILL_DIR))

        self.assertEqual(result["verify"]["violations"], [])
        self.assertEqual(result["verify"]["hard_count"], 0)
        self.assertFalse(result["verify"]["catalog_checked"])

    @patch("tier2._judge_tier2")
    @patch("tier2._run_claude_agent")
    @patch("tier2._clone_at_sha")
    def test_real_verify_leg_flags_non_literal_vars(
        self, mock_clone, mock_agent, mock_judge
    ):
        # Integration: does NOT mock _run_verify — proves the tier2 wiring
        # actually reaches the real detect.py/verify.py against the cloned
        # checkout, not just that a mock was called. check_catalog_live is
        # False (activate defaults off), so this runs with no network,
        # mirroring test_run_floxify.py's own TestRunVerify discipline.
        manifest = '[vars]\nfoo = "$HOME/data"\n'
        mock_clone.return_value = None
        mock_agent.side_effect = _agent_writes_manifest(manifest)
        mock_judge.return_value = {"score": 3, "correct": False, "issues": []}

        result = tier2.process_entry(self._entry(), str(tier2.DEFAULT_SKILL_DIR))

        self.assertNotIn("error", result["verify"])
        rules = {v["rule"] for v in result["verify"]["violations"]}
        self.assertIn("vars-not-literal", rules)
        self.assertGreaterEqual(result["verify"]["hard_count"], 1)


class TestProcessEntryServiceRuleAndManifestPersistence(unittest.TestCase):
    """AI-468: 'serves postgres' must mean the same thing everywhere in a
    single process_entry run — the structural has_service_<kind> check and
    the AI-447 probe must both resolve a [services.db] running postgres —
    and the full produced manifest must be recoverable from the result
    dict, not just a 3000-char excerpt (the gap that blocked forensics on
    the lemmy rep-3 residual twice)."""

    def _entry(self, **overrides):
        entry = {
            "id": "x", "repo_url": "https://example.com/r", "sha": "abc123",
            "expected_runtimes": [], "expected_services": ["postgres"],
        }
        entry.update(overrides)
        return entry

    @patch("tier2._judge_tier2")
    @patch("tier2._run_verify")
    @patch("tier2._run_claude_agent")
    @patch("tier2._clone_at_sha")
    def test_unconventionally_named_service_passes_structural_check_end_to_end(
        self, mock_clone, mock_agent, mock_verify, mock_judge
    ):
        # Real skill dir (not mocked) so process_entry loads the real
        # verify.py and exercises matching_service_names for real.
        manifest = (
            "[install]\n"
            'pg.pkg-path = "postgresql_16"\n'
            "[services.db]\n"
            'command = "postgres -D /data"\n'
        )
        mock_clone.return_value = None
        mock_agent.side_effect = _agent_writes_manifest(manifest)
        mock_verify.return_value = {
            "violations": [], "catalog_checked": False, "catalog_unknown": [],
        }
        mock_judge.return_value = {"score": 4, "correct": True, "issues": []}

        result = tier2.process_entry(self._entry(), str(tier2.DEFAULT_SKILL_DIR))

        self.assertTrue(result["hard_checks"]["has_service_postgres"], result)

    @patch("tier2._judge_tier2")
    @patch("tier2._run_verify")
    @patch("tier2._run_claude_agent")
    @patch("tier2._clone_at_sha")
    def test_genuinely_absent_service_fails_structural_check_end_to_end(
        self, mock_clone, mock_agent, mock_verify, mock_judge
    ):
        manifest = "[services.redis]\ncommand = \"redis-server\"\n"
        mock_clone.return_value = None
        mock_agent.side_effect = _agent_writes_manifest(manifest)
        mock_verify.return_value = {
            "violations": [], "catalog_checked": False, "catalog_unknown": [],
        }
        mock_judge.return_value = {"score": 1, "correct": False, "issues": []}

        result = tier2.process_entry(self._entry(), str(tier2.DEFAULT_SKILL_DIR))

        self.assertFalse(result["hard_checks"]["has_service_postgres"], result)

    @patch("tier2._judge_tier2")
    @patch("tier2._run_verify")
    @patch("tier2._run_claude_agent")
    @patch("tier2._clone_at_sha")
    def test_full_manifest_persisted_in_result(
        self, mock_clone, mock_agent, mock_verify, mock_judge
    ):
        # A manifest longer than the 3000-char excerpt cap, so this also
        # proves "manifest" is NOT the same truncated value as
        # "manifest_excerpt" — the exact gap that blocked forensics twice.
        manifest = (
            "[install]\n"
            + "".join(f'pkg{i}.pkg-path = "pkg{i}"\n' for i in range(200))
        )
        self.assertGreater(len(manifest), 3000)
        mock_clone.return_value = None
        mock_agent.side_effect = _agent_writes_manifest(manifest)
        mock_verify.return_value = {
            "violations": [], "catalog_checked": False, "catalog_unknown": [],
        }
        mock_judge.return_value = {"score": 3, "correct": True, "issues": []}

        result = tier2.process_entry(
            self._entry(expected_services=[]), str(tier2.DEFAULT_SKILL_DIR),
        )

        self.assertEqual(result["manifest"], manifest)
        self.assertEqual(result["manifest_excerpt"], manifest[:3000])
        self.assertLess(len(result["manifest_excerpt"]), len(result["manifest"]))


class TestProcessEntryUpstreamFloxStrip(unittest.TestCase):
    """AI-469 end-to-end: process_entry must strip an in-tree .flox/
    before the conversion task runs, but capture it as data first — the
    audit found PostHog is the one registry entry that ships one at its
    pinned SHA, and one un-stripped rep scored the UPSTREAM manifest
    instead of anything the skill produced."""

    def _entry(self, **overrides):
        entry = {
            "id": "x", "repo_url": "https://example.com/r", "sha": "abc123",
            "expected_runtimes": [], "expected_services": [],
        }
        entry.update(overrides)
        return entry

    @staticmethod
    def _clone_writes_upstream_flox(manifest_text, extra_files=None):
        """A `_clone_at_sha` stand-in that plants an in-tree .flox/ — the
        real shape a clone brings for a repo like PostHog. Distinct from
        `_agent_writes_manifest`: this represents genuine upstream
        content process_entry must strip, not the skill's own output."""
        def _clone(url, sha, dest, timeout=900):
            d = Path(dest)
            flox_env = d / ".flox" / "env"
            flox_env.mkdir(parents=True, exist_ok=True)
            (flox_env / "manifest.toml").write_text(manifest_text)
            for rel in extra_files or []:
                p = d / ".flox" / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("")
            return None
        return _clone

    @patch("tier2._judge_tier2")
    @patch("tier2._run_verify")
    @patch("tier2._run_claude_agent")
    @patch("tier2._clone_at_sha")
    def test_upstream_flox_stripped_before_agent_invocation(
        self, mock_clone, mock_agent, mock_verify, mock_judge
    ):
        mock_clone.side_effect = self._clone_writes_upstream_flox(
            "[install]\nupstream = true\n"
        )
        seen = {"flox_present_at_agent_call": None}

        def _agent(prompt, skill_dir, timeout=1800):
            target = Path(prompt.split("\n", 1)[0].removeprefix("/floxify ").strip())
            seen["flox_present_at_agent_call"] = (target / ".flox").exists()
            (target / ".flox" / "env").mkdir(parents=True, exist_ok=True)
            (target / ".flox" / "env" / "manifest.toml").write_text(
                "[install]\nfrom_skill = true\n"
            )
            return "agent output", None, {
                "cost_usd": 0.0, "usage": {}, "duration_ms": 0, "num_turns": 0,
                "tool_calls": {"total": 0, "flox_search": 0, "flox_show": 0},
                "raw_stream": None,
            }

        mock_agent.side_effect = _agent
        mock_verify.return_value = {
            "violations": [], "catalog_checked": False, "catalog_unknown": [],
        }
        mock_judge.return_value = {"score": 5, "correct": True, "issues": []}

        result = tier2.process_entry(self._entry(), "/fake/skill/dir")

        self.assertFalse(
            seen["flox_present_at_agent_call"],
            "the conversion task must not see the upstream .flox/",
        )
        self.assertTrue(result["had_upstream_flox"])
        self.assertEqual(result["upstream_manifest"], "[install]\nupstream = true\n")
        self.assertEqual(result["manifest"], "[install]\nfrom_skill = true\n")

    @patch("tier2._judge_tier2")
    @patch("tier2._run_verify")
    @patch("tier2._run_claude_agent")
    @patch("tier2._clone_at_sha")
    def test_upstream_flox_files_list_captured(
        self, mock_clone, mock_agent, mock_verify, mock_judge
    ):
        mock_clone.side_effect = self._clone_writes_upstream_flox(
            "[install]\n", extra_files=[".gitignore", "env/on-activate.sh"],
        )
        mock_agent.side_effect = _agent_writes_manifest(
            "[install]\nfrom_skill = true\n"
        )
        mock_verify.return_value = {
            "violations": [], "catalog_checked": False, "catalog_unknown": [],
        }
        mock_judge.return_value = {"score": 5, "correct": True, "issues": []}

        result = tier2.process_entry(self._entry(), "/fake/skill/dir")

        self.assertTrue(result["had_upstream_flox"])
        self.assertIn(".gitignore", result["upstream_flox_files"])
        self.assertIn("env/manifest.toml", result["upstream_flox_files"])
        self.assertIn("env/on-activate.sh", result["upstream_flox_files"])

    @patch("tier2._judge_tier2")
    @patch("tier2._run_verify")
    @patch("tier2._run_claude_agent")
    @patch("tier2._clone_at_sha")
    def test_no_upstream_flox_records_false_and_none(
        self, mock_clone, mock_agent, mock_verify, mock_judge
    ):
        mock_clone.return_value = None  # clean clone, no in-tree .flox
        mock_agent.side_effect = _agent_writes_manifest(
            "[install]\nfrom_skill = true\n"
        )
        mock_verify.return_value = {
            "violations": [], "catalog_checked": False, "catalog_unknown": [],
        }
        mock_judge.return_value = {"score": 5, "correct": True, "issues": []}

        result = tier2.process_entry(self._entry(), "/fake/skill/dir")

        self.assertFalse(result["had_upstream_flox"])
        self.assertIsNone(result["upstream_manifest"])
        self.assertEqual(result["upstream_flox_files"], [])

    @patch("tier2._judge_tier2")
    @patch("tier2._run_verify")
    @patch("tier2._run_claude_agent")
    @patch("tier2._clone_at_sha")
    def test_symlinked_upstream_flox_does_not_abort_the_run(
        self, mock_clone, mock_agent, mock_verify, mock_judge
    ):
        # PR #49 review I1, end-to-end: process_entry itself must not
        # raise when the clone brings a symlinked .flox/ — the run must
        # complete and return a normal per-rep result, not propagate an
        # exception up through main's pool.map and abort the whole batch
        # over one weird rep.
        target = tempfile.mkdtemp(prefix="tier2-symlink-target-")
        self.addCleanup(shutil.rmtree, target, ignore_errors=True)

        def _clone(url, sha, dest, timeout=900):
            Path(dest, ".flox").symlink_to(target, target_is_directory=True)
            return None

        mock_clone.side_effect = _clone
        mock_agent.side_effect = _agent_writes_manifest(
            "[install]\nfrom_skill = true\n"
        )
        mock_verify.return_value = {
            "violations": [], "catalog_checked": False, "catalog_unknown": [],
        }
        mock_judge.return_value = {"score": 5, "correct": True, "issues": []}

        result = tier2.process_entry(self._entry(), "/fake/skill/dir")

        self.assertNotIn("error", result)
        self.assertTrue(result["had_upstream_flox"])
        self.assertIsNone(result["upstream_manifest"])
        self.assertIn("symlink", result["upstream_flox_note"])
        self.assertEqual(result["manifest"], "[install]\nfrom_skill = true\n")

    @patch("tier2._judge_tier2")
    @patch("tier2._run_verify")
    @patch("tier2._run_claude_agent")
    @patch("tier2._clone_at_sha")
    def test_symlinked_upstream_manifest_yields_null_capture_with_note(
        self, mock_clone, mock_agent, mock_verify, mock_judge
    ):
        # PR #49 review I2, end-to-end: a symlinked manifest.toml inside
        # a real .flox/env/ must not be read through.
        secret = tempfile.mkdtemp(prefix="tier2-symlink-secret-")
        self.addCleanup(shutil.rmtree, secret, ignore_errors=True)
        secret_file = Path(secret) / "secret.toml"
        secret_file.write_text("SENSITIVE\n")

        def _clone(url, sha, dest, timeout=900):
            flox_env = Path(dest) / ".flox" / "env"
            flox_env.mkdir(parents=True)
            (flox_env / "manifest.toml").symlink_to(secret_file)
            return None

        mock_clone.side_effect = _clone
        mock_agent.side_effect = _agent_writes_manifest(
            "[install]\nfrom_skill = true\n"
        )
        mock_verify.return_value = {
            "violations": [], "catalog_checked": False, "catalog_unknown": [],
        }
        mock_judge.return_value = {"score": 5, "correct": True, "issues": []}

        result = tier2.process_entry(self._entry(), "/fake/skill/dir")

        self.assertNotIn("error", result)
        self.assertTrue(result["had_upstream_flox"])
        self.assertIsNone(result["upstream_manifest"])
        self.assertIn("symlink", result["upstream_flox_note"])
        self.assertEqual(result["manifest"], "[install]\nfrom_skill = true\n")


class TestProcessEntryServiceDisposition(unittest.TestCase):
    """AI-470 end-to-end: process_entry's has_service_<kind> and the new
    service_observed field must agree with the disposition-aware
    semantics through the real call path — real skill dir, unmocked
    _structural_checks/_service_disposition_results/_probe_services — not
    just at the unit level."""

    def _entry(self, **overrides):
        entry = {
            "id": "posthog", "repo_url": "https://example.com/posthog",
            "sha": "abc123", "expected_runtimes": [],
            "expected_services": [
                {"name": "postgres", "disposition": "expect-wired"},
                {"name": "clickhouse", "disposition": "deferred-ok"},
            ],
        }
        entry.update(overrides)
        return entry

    @staticmethod
    def _clone_writes_manifest(manifest_text):
        def _clone(url, sha, dest, timeout=900):
            d = Path(dest)
            (d / ".flox" / "env").mkdir(parents=True, exist_ok=True)
            (d / ".flox" / "env" / "manifest.toml").write_text(manifest_text)
            return None
        return _clone

    @patch("tier2._judge_tier2")
    @patch("tier2._run_verify")
    @patch("tier2._run_claude_agent")
    @patch("tier2._clone_at_sha")
    def test_deferred_ok_service_deferred_with_mechanism_passes_hard_check(
        self, mock_clone, mock_agent, mock_verify, mock_judge
    ):
        # Mirrors the rebuilt posthog golden's shape: postgres wired
        # directly, clickhouse deferred to a genuine docker-compose
        # invocation.
        manifest = (
            '[install]\n'
            'docker-compose.pkg-path = "docker-compose"\n'
            '[services.postgres]\n'
            'command = "postgres"\n'
            '[hook]\n'
            'on-activate = "docker-compose up -d clickhouse"\n'
        )
        mock_clone.side_effect = self._clone_writes_manifest("[install]\n")
        mock_agent.side_effect = _agent_writes_manifest(manifest)
        mock_verify.return_value = {
            "violations": [], "catalog_checked": False, "catalog_unknown": [],
        }
        mock_judge.return_value = {"score": 5, "correct": True, "issues": []}

        result = tier2.process_entry(self._entry(), str(tier2.DEFAULT_SKILL_DIR))

        self.assertTrue(result["hard_checks"]["has_service_postgres"], result)
        self.assertTrue(result["hard_checks"]["has_service_clickhouse"], result)
        self.assertEqual(
            result["service_observed"],
            {"postgres": "wired", "clickhouse": "deferred"},
        )

    @patch("tier2._judge_tier2")
    @patch("tier2._run_verify")
    @patch("tier2._run_claude_agent")
    @patch("tier2._clone_at_sha")
    def test_silently_dropped_deferred_ok_service_fails_hard_check(
        self, mock_clone, mock_agent, mock_verify, mock_judge
    ):
        # postgres wired, clickhouse neither wired nor deferred with any
        # mechanism -- deferred-ok must not wave this through.
        manifest = (
            '[services.postgres]\n'
            'command = "postgres"\n'
        )
        mock_clone.side_effect = self._clone_writes_manifest("[install]\n")
        mock_agent.side_effect = _agent_writes_manifest(manifest)
        mock_verify.return_value = {
            "violations": [], "catalog_checked": False, "catalog_unknown": [],
        }
        mock_judge.return_value = {"score": 2, "correct": False, "issues": []}

        result = tier2.process_entry(self._entry(), str(tier2.DEFAULT_SKILL_DIR))

        self.assertTrue(result["hard_checks"]["has_service_postgres"], result)
        self.assertFalse(result["hard_checks"]["has_service_clickhouse"], result)
        self.assertFalse(result["hard_pass"], result)
        self.assertEqual(
            result["service_observed"],
            {"postgres": "wired", "clickhouse": "missing"},
        )


class TestJudgeTier2CatalogNote(unittest.TestCase):
    """AI-465: the tier2 judge prompt must carry verify.py's confirmed
    catalog resolution table, same as Tier 1's `_judge` (AI-451/AI-461) —
    otherwise the judge grades catalog facts from memory again, just on
    real OSS repos instead of fixtures."""

    def _entry(self):
        return {
            "id": "x", "repo_url": "https://example.com/r", "sha": "abc123",
            "gold": {"runtimes": "ruby", "services": "postgres"},
            "rubric": "",
        }

    @patch("tier2._run_judge")
    def test_no_verify_result_tells_judge_not_to_assert_from_memory(
        self, mock_run_judge
    ):
        mock_run_judge.return_value = ('{"score": 3, "correct": true, "issues": []}', None, {"cost_usd": 0.0, "usage": {}, "duration_ms": 0, "num_turns": 0})
        tier2._judge_tier2(self._entry(), "[install]\n", verify_result=None)
        prompt = mock_run_judge.call_args.args[0]
        self.assertIn("do not assert catalog facts from memory", prompt.lower())

    @patch("tier2._run_judge")
    def test_clean_catalog_confirms_resolution_to_judge(self, mock_run_judge):
        mock_run_judge.return_value = ('{"score": 5, "correct": true, "issues": []}', None, {"cost_usd": 0.0, "usage": {}, "duration_ms": 0, "num_turns": 0})
        verify_result = {"catalog_checked": True, "violations": []}
        tier2._judge_tier2(self._entry(), "[install]\n", verify_result=verify_result)
        prompt = mock_run_judge.call_args.args[0]
        self.assertIn("confirmed to resolve", prompt.lower())


if __name__ == "__main__":
    unittest.main()
