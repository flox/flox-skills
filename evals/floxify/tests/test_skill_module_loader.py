#!/usr/bin/env python3
"""Regression test for the C1 sys.modules collision (see
_skill_module_loader.py's docstring for the full incident).

This directly encodes the diagnostic that proved the bug: load the SAME
source file twice under two different `sys_modules_key`s (exactly what
test_verify.py and test_golden_lint.py do when both run in one
interpreter, as the CI free-tests step does), then confirm
`unittest.mock.patch` resolves each name to ITS OWN instance rather than
whichever was registered last.

    python3 -m unittest tests.test_skill_module_loader -v
"""
import unittest
from pathlib import Path
from unittest.mock import patch

from _skill_module_loader import load_module

HERE = Path(__file__).resolve().parent
SUITE = HERE.parent          # evals/floxify
REPO_ROOT = SUITE.parent.parent
VERIFY_PATH = REPO_ROOT / "flox-plugin" / "skills" / "floxify" / "scripts" / "verify.py"


class TestNoModuleCollision(unittest.TestCase):
    def test_two_independently_keyed_loads_do_not_collide(self):
        mod_a = load_module(VERIFY_PATH, sys_modules_key="verify_regression_a")
        mod_b = load_module(VERIFY_PATH, sys_modules_key="verify_regression_b")

        self.assertIsNot(mod_a, mod_b, "each load must be an independent instance")

        with patch("verify_regression_a._run_show_command") as patched_a:
            # The bug: patching by name A must never touch instance B's
            # function object, and vice versa below.
            self.assertIs(mod_a._run_show_command, patched_a)
            self.assertIsNot(mod_b._run_show_command, patched_a)

        with patch("verify_regression_b._run_show_command") as patched_b:
            self.assertIs(mod_b._run_show_command, patched_b)
            self.assertIsNot(mod_a._run_show_command, patched_b)

    def test_unkeyed_loads_never_touch_sys_modules(self):
        import sys
        before = dict(sys.modules)
        load_module(VERIFY_PATH)
        load_module(VERIFY_PATH)
        # No new global names — every call is a private instance, safe for
        # the harness's per-task (and concurrent, via ThreadPoolExecutor)
        # reloading in run_floxify.py.
        self.assertEqual(set(sys.modules) - set(before), set())


if __name__ == "__main__":
    unittest.main()
