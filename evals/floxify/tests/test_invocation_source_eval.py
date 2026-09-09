#!/usr/bin/env python3
"""Unit tests for invocation_source_eval.py's matchers.

The eval spawns an agent and is opt-in, so its correctness has to be
gated somewhere cheap. These are pure-function tests over command
strings: no claude, no flox, no network.

Run from the suite root (`evals/floxify/`):
    python3 -m unittest tests.test_invocation_source_eval -v
"""
import unittest
from pathlib import Path

from _skill_module_loader import load_module

HERE = Path(__file__).resolve().parent
SUITE = HERE.parent
REPO_ROOT = SUITE.parent.parent
SKILL_MD = REPO_ROOT / "flox-plugin" / "skills" / "floxify" / "SKILL.md"

ev = load_module(SUITE / "invocation_source_eval.py")

WRAPPED = '"/s/scripts/flox-python.sh" "/s/scripts/detect.py" /t'
RAW_RUN = 'flox run -p python313 -- python3 "/s/scripts/detect.py" /t'
BARE_PY = 'python3 "/s/scripts/verify.py" a b'


class TestReachDetection(unittest.TestCase):
    def test_wrapper_call_is_a_reach(self):
        self.assertTrue(ev._reaches_a_prescribed_script(WRAPPED))

    def test_raw_flox_run_is_a_reach(self):
        # The bypass this eval exists to see. It runs the same script and
        # produces the same manifest, so nothing else would notice.
        self.assertTrue(ev._reaches_a_prescribed_script(RAW_RUN))

    def test_bare_python_is_a_reach(self):
        # The documented pre-1.13 fallback. Counted, then reported as a
        # bypass rather than silently dropped.
        self.assertTrue(ev._reaches_a_prescribed_script(BARE_PY))

    def test_reading_the_script_is_not_a_reach(self):
        for c in ('cat /s/scripts/detect.py', 'grep foo /s/scripts/verify.py'):
            with self.subTest(cmd=c):
                self.assertFalse(ev._reaches_a_prescribed_script(c))

    def test_an_unrelated_flox_run_is_not_a_reach(self):
        self.assertFalse(ev._reaches_a_prescribed_script("flox run -p php85 -- php -m"))

    def test_empty_and_none_are_safe(self):
        for c in ("", None):
            with self.subTest(cmd=c):
                self.assertFalse(ev._reaches_a_prescribed_script(c))
                self.assertFalse(ev._via_wrapper(c))


class TestWrapperDetection(unittest.TestCase):
    def test_wrapper_recognised(self):
        self.assertTrue(ev._via_wrapper(WRAPPED))

    def test_raw_run_is_not_the_wrapper(self):
        self.assertFalse(ev._via_wrapper(RAW_RUN))

    def test_bare_python_is_not_the_wrapper(self):
        self.assertFalse(ev._via_wrapper(BARE_PY))


class TestMatchesWhatSkillMdActuallyShips(unittest.TestCase):
    """The matchers are written against the command blocks SKILL.md
    ships. If those are reworded, this fails rather than the eval quietly
    scoring every future run as a bypass."""

    def test_shipped_blocks_read_as_wrapped_reaches(self):
        lines = [
            ln for ln in SKILL_MD.read_text().splitlines()
            if "flox-python.sh" in ln
            and ("detect.py" in ln or "verify.py" in ln)
        ]
        self.assertTrue(lines, "no wrapper invocation found in SKILL.md")
        for ln in lines:
            with self.subTest(line=ln.strip()[:70]):
                self.assertTrue(ev._reaches_a_prescribed_script(ln))
                self.assertTrue(ev._via_wrapper(ln))


if __name__ == "__main__":
    unittest.main()
