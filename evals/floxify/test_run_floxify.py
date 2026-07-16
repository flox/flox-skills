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


if __name__ == "__main__":
    unittest.main()
