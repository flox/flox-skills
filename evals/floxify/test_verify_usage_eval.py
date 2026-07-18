#!/usr/bin/env python3
"""Unit tests for verify_usage_eval's invocation matching.

Same discipline as test_detect_usage_eval.py (AI-455): only *execution*
of verify.py counts, never a mention (`cat verify.py`, `grep verify.py`).

Pure logic, no subprocess — safe to gate on.

    python3 -m unittest test_verify_usage_eval -v
"""
import unittest

import verify_usage_eval as vue


class TestIsVerifierInvocation(unittest.TestCase):
    def test_flox_run_form_is_the_canonical_invocation(self):
        cmd = (
            'flox run -p python313 -- python3 '
            '"/home/x/flox-plugin/skills/floxify/scripts/verify.py" '
            '/tmp/floxify-detect.json /tmp/repo/.flox/env/manifest.toml 2>&1'
        )
        self.assertTrue(vue._is_verifier_invocation(cmd))

    def test_plain_python3_fallback_form_counts(self):
        self.assertTrue(
            vue._is_verifier_invocation(
                'python3 scripts/verify.py detect.json manifest.toml'
            )
        )

    def test_python_without_3_counts(self):
        self.assertTrue(vue._is_verifier_invocation('python scripts/verify.py - manifest.toml'))

    # --- the bug: mentions that are not executions -----------------------

    def test_cat_does_not_count(self):
        self.assertFalse(vue._is_verifier_invocation('cat scripts/verify.py'))

    def test_grep_does_not_count(self):
        self.assertFalse(
            vue._is_verifier_invocation('grep -n catalog scripts/verify.py')
        )

    def test_unrelated_command_does_not_count(self):
        self.assertFalse(vue._is_verifier_invocation('flox activate -c "echo __ok__"'))

    # --- chained commands ------------------------------------------------

    def test_execution_after_a_separator_counts(self):
        self.assertTrue(
            vue._is_verifier_invocation(
                'cd /tmp/repo && python3 scripts/verify.py detect.json manifest.toml'
            )
        )

    def test_mention_in_one_segment_and_python_in_another_does_not_count(self):
        self.assertFalse(
            vue._is_verifier_invocation('python3 --version; cat verify.py')
        )

    def test_detect_py_invocation_does_not_satisfy_verify_check(self):
        # Running detect.py (Phase 1) must not be mistaken for verify.py
        # (Phase 3c) — the two invocation matchers are independent.
        self.assertFalse(
            vue._is_verifier_invocation('flox run -p python313 -- python3 detect.py .')
        )


class TestViaFloxRunVerify(unittest.TestCase):
    """Reused from detect_usage_eval — verify the shared helper still
    applies to verify.py's own command line."""

    def test_detects_flox_run(self):
        self.assertTrue(
            vue._is_via_flox_run('flox run -p python313 -- python3 verify.py - manifest.toml')
        )

    def test_plain_python_is_not_via_flox_run(self):
        self.assertFalse(vue._is_via_flox_run('python3 verify.py - manifest.toml'))


if __name__ == "__main__":
    unittest.main()
