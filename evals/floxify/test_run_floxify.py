#!/usr/bin/env python3
"""Unit tests for run_floxify's deterministic pieces.

The agentic skill run and the LLM judge are integration-only (exercised by a
real `--only <id>` run). Everything here is pure logic over mocked
subprocesses, so it is fast and safe to gate on.

    python3 -m unittest test_run_floxify -v
"""
import subprocess
import unittest
from unittest.mock import MagicMock, patch

import run_floxify


class TestCheckActivation(unittest.TestCase):
    """AI-454: the activation budget must be configurable, and a timeout must
    not masquerade as 'we didn't check'.

    `skipped` means *we could not run the check* (flox absent, --skip-activation).
    A timeout means *we ran it and the environment did not come up within the
    budget* — that is a finding about the environment, not an absence of one.
    Conflating them silently inflated `activation_skipped` and read as benign:
    posthog timed out at the hardcoded 120s and was recorded as skipped, so the
    largest repo in the corpus produced no activation signal at all.
    """

    @patch("run_floxify.shutil.which", return_value=None)
    def test_flox_absent_is_skipped(self, _which):
        ok, skipped, notes = run_floxify._check_activation("/tmp/x")
        self.assertIsNone(ok)
        self.assertTrue(skipped)
        self.assertIn("flox", notes.lower())

    @patch("run_floxify.shutil.which", return_value="/usr/bin/flox")
    @patch("run_floxify.subprocess.run")
    def test_timeout_is_a_failure_not_a_skip(self, mock_run, _which):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="flox", timeout=120)
        ok, skipped, notes = run_floxify._check_activation("/tmp/x", timeout=120)
        self.assertFalse(ok, "a timeout is a verdict, not an absence of one")
        self.assertFalse(skipped, "must not be recorded as skipped")
        self.assertIn("TIMEOUT", notes)
        self.assertIn("120", notes)

    @patch("run_floxify.shutil.which", return_value="/usr/bin/flox")
    @patch("run_floxify.subprocess.run")
    def test_timeout_budget_is_configurable(self, mock_run, _which):
        mock_run.return_value = MagicMock(returncode=0, stdout="__ok__", stderr="")
        run_floxify._check_activation("/tmp/x", timeout=1800)
        self.assertEqual(mock_run.call_args.kwargs["timeout"], 1800)

    @patch("run_floxify.shutil.which", return_value="/usr/bin/flox")
    @patch("run_floxify.subprocess.run")
    def test_default_budget_preserved_for_small_fixtures(self, mock_run, _which):
        mock_run.return_value = MagicMock(returncode=0, stdout="__ok__", stderr="")
        run_floxify._check_activation("/tmp/x")
        self.assertEqual(mock_run.call_args.kwargs["timeout"], 120)

    @patch("run_floxify.shutil.which", return_value="/usr/bin/flox")
    @patch("run_floxify.subprocess.run")
    def test_successful_activation(self, mock_run, _which):
        mock_run.return_value = MagicMock(returncode=0, stdout="__ok__\n", stderr="")
        ok, skipped, notes = run_floxify._check_activation("/tmp/x")
        self.assertTrue(ok)
        self.assertFalse(skipped)

    @patch("run_floxify.shutil.which", return_value="/usr/bin/flox")
    @patch("run_floxify.subprocess.run")
    def test_failed_activation_reports_stderr(self, mock_run, _which):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="could not resolve package foo"
        )
        ok, skipped, notes = run_floxify._check_activation("/tmp/x")
        self.assertFalse(ok)
        self.assertFalse(skipped)
        self.assertIn("could not resolve", notes)

    @patch("run_floxify.shutil.which", return_value="/usr/bin/flox")
    @patch("run_floxify.subprocess.run")
    def test_unexpected_error_is_skipped_not_failed(self, mock_run, _which):
        # An OSError from the harness is our problem, not the manifest's.
        mock_run.side_effect = OSError("fork failed")
        ok, skipped, notes = run_floxify._check_activation("/tmp/x")
        self.assertIsNone(ok)
        self.assertTrue(skipped)


class TestRunVerify(unittest.TestCase):
    """AI-461: run_floxify's own deterministic leg. check_catalog_live is
    always False here — these must run with no network, mirroring
    test_verify.py's own discipline."""

    def test_no_manifest_is_reported_as_skipped_not_an_error(self):
        result = run_floxify._run_verify(
            run_floxify.DEFAULT_SKILL_DIR, run_floxify.FIXTURES_DIR / "node-postgres",
            None, check_catalog_live=False,
        )
        self.assertEqual(result["violations"], [])
        self.assertIn("skipped", result)
        self.assertNotIn("error", result)

    def test_real_fixture_and_manifest_produce_a_violations_list(self):
        # node-postgres fixture has a `pg` dependency; a manifest with no
        # [services.*] should trip the leaf-datastore-not-served invariant.
        manifest = '[install]\nnodejs.pkg-path = "nodejs_20"\n'
        result = run_floxify._run_verify(
            run_floxify.DEFAULT_SKILL_DIR, run_floxify.FIXTURES_DIR / "node-postgres",
            manifest, check_catalog_live=False,
        )
        self.assertNotIn("error", result)
        rules = {v["rule"] for v in result["violations"]}
        self.assertIn("leaf-datastore-not-served", rules)

    def test_unloadable_skill_dir_reports_error_not_an_exception(self):
        result = run_floxify._run_verify(
            "/nonexistent/skill/dir", run_floxify.FIXTURES_DIR / "node-postgres",
            "[install]\n", check_catalog_live=False,
        )
        self.assertEqual(result["violations"], [])
        self.assertIn("error", result)


class TestCatalogNote(unittest.TestCase):
    """AI-451/AI-461: the judge prompt must stop grading catalog facts from
    memory — verify_result decides which note it gets instead."""

    def test_no_result_tells_judge_not_to_assert_from_memory(self):
        note = run_floxify._catalog_note(None)
        self.assertIn("do not assert catalog facts from memory", note.lower())

    def test_harness_error_tells_judge_not_to_assert_from_memory(self):
        note = run_floxify._catalog_note({"error": "boom", "violations": []})
        self.assertIn("do not assert catalog facts from memory", note.lower())

    def test_catalog_not_checked_tells_judge_not_to_assert_from_memory(self):
        note = run_floxify._catalog_note({"catalog_checked": False, "violations": []})
        self.assertIn("not run this pass", note.lower())

    def test_clean_catalog_confirms_resolution_to_judge(self):
        note = run_floxify._catalog_note({"catalog_checked": True, "violations": []})
        self.assertIn("confirmed to resolve", note.lower())

    def test_catalog_violations_are_listed_for_the_judge(self):
        result = {
            "catalog_checked": True,
            "violations": [
                {"rule": "catalog-systems-mismatch", "severity": "hard",
                 "message": "nodejs_24 has no build for x86_64-darwin"},
                {"rule": "vars-not-literal", "severity": "hard",
                 "message": "unrelated non-catalog violation"},
            ],
        }
        note = run_floxify._catalog_note(result)
        self.assertIn("1 pkg-path/version/system violation", note)
        self.assertIn("x86_64-darwin", note)
        self.assertNotIn("unrelated non-catalog violation", note)


if __name__ == "__main__":
    unittest.main()
