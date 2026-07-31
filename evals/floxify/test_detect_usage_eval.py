#!/usr/bin/env python3
"""Unit tests for detect_usage_eval's invocation matching.

The eval asserts that /floxify *ran* the bundled analyzer. It decided that by
substring — `"detect.py" in cmd` — so any command that merely *mentions* the
file passed: `cat detect.py`, `ls scripts/detect.py`, a grep, even an echo.
An eval that a `cat` satisfies is not evidence of anything (AI-455).

Pure logic, no subprocess — safe to gate on.

    python3 -m unittest test_detect_usage_eval -v
"""
import unittest

import detect_usage_eval as due


class TestIsAnalyzerInvocation(unittest.TestCase):
    """Only *execution* counts."""

    def test_flox_run_form_is_the_canonical_invocation(self):
        cmd = (
            'flox run -p python313 -- python3 '
            '"/home/x/flox-plugin/skills/floxify/scripts/detect.py" "/tmp/repo" 2>&1'
        )
        self.assertTrue(due._is_analyzer_invocation(cmd))

    def test_plain_python3_fallback_form_counts(self):
        self.assertTrue(
            due._is_analyzer_invocation('python3 scripts/detect.py /tmp/repo')
        )

    def test_python_without_3_counts(self):
        self.assertTrue(due._is_analyzer_invocation('python scripts/detect.py .'))

    # --- the bug: mentions that are not executions -----------------------

    def test_cat_does_not_count(self):
        self.assertFalse(due._is_analyzer_invocation('cat scripts/detect.py'))

    def test_ls_does_not_count(self):
        self.assertFalse(due._is_analyzer_invocation('ls -la scripts/detect.py'))

    def test_grep_does_not_count(self):
        self.assertFalse(
            due._is_analyzer_invocation('grep -n runtimes scripts/detect.py')
        )

    def test_head_does_not_count(self):
        self.assertFalse(due._is_analyzer_invocation('head -50 detect.py'))

    def test_unrelated_command_does_not_count(self):
        self.assertFalse(due._is_analyzer_invocation('echo hello'))

    # --- chained commands ------------------------------------------------

    def test_execution_after_a_separator_counts(self):
        self.assertTrue(
            due._is_analyzer_invocation('cd /tmp/repo && python3 detect.py .')
        )

    def test_mention_in_one_segment_and_python_in_another_does_not_count(self):
        # `python3 --version; cat detect.py` mentions both but executes neither.
        self.assertFalse(
            due._is_analyzer_invocation('python3 --version; cat detect.py')
        )


class TestViaFloxRun(unittest.TestCase):
    """`flox run` is the form the skill is *told* to use — tracked separately
    so the eval can report 'ran it, but not the documented way'."""

    def test_detects_flox_run(self):
        self.assertTrue(
            due._is_via_flox_run('flox run -p python313 -- python3 detect.py .')
        )

    def test_plain_python_is_not_via_flox_run(self):
        self.assertFalse(due._is_via_flox_run('python3 detect.py .'))


if __name__ == "__main__":
    unittest.main()
