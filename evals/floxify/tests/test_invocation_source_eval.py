#!/usr/bin/env python3
"""Unit tests for invocation_source_eval.py's matchers.

The eval itself spawns an agent and is opt-in, so its own correctness has
to be gated somewhere cheap. These are pure-function tests over command
strings: no claude, no flox, no network.

Run from the suite root (`evals/floxify/`):
    python3 -m unittest tests.test_invocation_source_eval -v
"""
import unittest
from pathlib import Path

from _skill_module_loader import load_module

HERE = Path(__file__).resolve().parent
SUITE = HERE.parent

ev = load_module(SUITE / "invocation_source_eval.py")

TAG = 'agentic.skill.floxify.1-1-0'
PRESERVING = f'FLOX_INVOCATION_SOURCE="${{FLOX_INVOCATION_SOURCE:+$FLOX_INVOCATION_SOURCE,}}{TAG}"'
BARE = f'FLOX_INVOCATION_SOURCE="{TAG}"'
RUN = 'flox run -p python313 -- python3 /s/scripts/detect.py /t'


class TestPrescribedMatcher(unittest.TestCase):
    def test_matches_detect_and_verify(self):
        for script in ("detect.py", "verify.py"):
            with self.subTest(script=script):
                cmd = f"flox run -p python313 -- python3 /s/scripts/{script} /t"
                self.assertTrue(ev._is_prescribed_flox_run(cmd))

    def test_ignores_an_unrelated_flox_run(self):
        # SKILL.md also shows `flox run -p php85 -- php -m` as an example.
        # It is not one of the tagged blocks and must not be scored.
        self.assertFalse(ev._is_prescribed_flox_run("flox run -p php85 -- php -m"))

    def test_ignores_the_system_python_fallback(self):
        # The documented fallback bypasses `flox run` entirely; Task 1's
        # module-load tag covers it, and it is not this eval's subject.
        self.assertFalse(
            ev._is_prescribed_flox_run("python3 /s/scripts/verify.py a b"))

    def test_ignores_reading_the_script(self):
        self.assertFalse(ev._is_prescribed_flox_run("cat /s/scripts/detect.py"))

    def test_empty_and_none_are_safe(self):
        for c in ("", None):
            with self.subTest(cmd=c):
                self.assertFalse(ev._is_prescribed_flox_run(c))
                self.assertFalse(ev._is_tagged(c))
                self.assertFalse(ev._preserves_existing(c))


class TestTagMatcher(unittest.TestCase):
    def test_preserving_form_is_tagged(self):
        self.assertTrue(ev._is_tagged(f"{PRESERVING} {RUN}"))

    def test_bare_form_is_tagged(self):
        self.assertTrue(ev._is_tagged(f"{BARE} {RUN}"))

    def test_untagged_run_is_not(self):
        self.assertFalse(ev._is_tagged(RUN))

    def test_a_different_skills_tag_does_not_count(self):
        other = 'FLOX_INVOCATION_SOURCE="agentic.skill.flox.1-1-0"'
        self.assertFalse(ev._is_tagged(f"{other} {RUN}"))

    def test_any_version_counts(self):
        # The drift test pins the version; this matcher must not, or a
        # version bump silently reports every run as untagged.
        older = 'FLOX_INVOCATION_SOURCE="agentic.skill.floxify.0-9-0"'
        self.assertTrue(ev._is_tagged(f"{older} {RUN}"))


class TestPreservationSignal(unittest.TestCase):
    def test_preserving_form_detected(self):
        self.assertTrue(ev._preserves_existing(f"{PRESERVING} {RUN}"))

    def test_bare_assignment_is_not_preserving(self):
        self.assertFalse(ev._preserves_existing(f"{BARE} {RUN}"))

    def test_preservation_is_reported_not_required(self):
        # A bare assignment is still tagged: the eval fails on missing
        # tags, and only NOTEs a collapsed append form.
        cmd = f"{BARE} {RUN}"
        self.assertTrue(ev._is_tagged(cmd))
        self.assertFalse(ev._preserves_existing(cmd))


class TestMatchesWhatSkillMdActuallyShips(unittest.TestCase):
    """The matchers are written against the literal in SKILL.md. If that
    line is reworded, these tests must fail rather than the eval quietly
    scoring every future run as untagged."""

    def test_skill_md_blocks_satisfy_both_matchers(self):
        skill_md = (SUITE.parent.parent / "flox-plugin" / "skills"
                    / "floxify" / "SKILL.md").read_text()
        lines = skill_md.splitlines()
        prefixed = [
            f"{lines[i]} {lines[i + 1]}"
            for i, ln in enumerate(lines[:-1])
            if "FLOX_INVOCATION_SOURCE" in ln and "flox run" in lines[i + 1]
        ]
        self.assertTrue(prefixed, "no tagged `flox run` block found in SKILL.md")
        for cmd in prefixed:
            with self.subTest(cmd=cmd[:70]):
                self.assertTrue(ev._is_tagged(cmd))
                self.assertTrue(ev._preserves_existing(cmd))
                self.assertTrue(ev._is_prescribed_flox_run(cmd))


if __name__ == "__main__":
    unittest.main()
