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
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tier2


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


class TestServicePresent(unittest.TestCase):
    def test_matches_postgres_section(self):
        manifest = "[services.postgres]\ncommand = \"postgres\"\n"
        self.assertTrue(tier2._service_present(manifest, "postgres"))

    def test_matches_postgresql_variant(self):
        manifest = "[services.postgresql]\ncommand = \"postgres\"\n"
        self.assertTrue(tier2._service_present(manifest, "postgres"))

    def test_no_match_when_service_absent(self):
        manifest = "[services.redis]\ncommand = \"redis-server\"\n"
        self.assertFalse(tier2._service_present(manifest, "postgres"))

    def test_none_manifest_returns_false(self):
        self.assertFalse(tier2._service_present(None, "postgres"))


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
        checks = tier2._structural_checks(entry, manifest)
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
        checks = tier2._structural_checks(entry, manifest)
        self.assertTrue(checks["pins_ruby"])
        self.assertTrue(checks["has_service_postgres"])
        self.assertFalse(checks["has_service_redis"])
        self.assertFalse(all(checks.values()))

    def test_no_manifest_fails_everything(self):
        entry = {
            "id": "mastodon",
            "expected_runtimes": [{"name": "ruby", "pattern": r"ruby(_[0-9_]+)?"}],
            "expected_services": ["postgres"],
        }
        checks = tier2._structural_checks(entry, None)
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


if __name__ == "__main__":
    unittest.main()
