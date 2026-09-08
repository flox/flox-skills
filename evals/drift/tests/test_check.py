#!/usr/bin/env python3
"""Unit tests for the drift checker (AI-512 layer L0).

The checker is a pure function of (registry, flox help output), so every
test here mocks the flox probe at its subprocess boundary and feeds canned
`--help` text. No flox, no network. One live smoke test lives separately.

Written RED first per the ticket: a deliberately wrong claim must flag
before the real registry is trusted.

Run from the suite root (`evals/drift/`):
    python3 -m unittest tests.test_check -v
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import check

HERE = Path(__file__).resolve().parent
SUITE = HERE.parent
REPO_ROOT = SUITE.parent.parent

# Trimmed from real `flox --help` and `flox activate --help` (1.14.0).
TOP_HELP = """\
Usage: flox [[-v]... | -q] [--floxhub-url=URL] [-V] [COMMAND ...]

Use environments
    activate               Enter the environment, run 'flox deactivate' to leave
    run                    Run a command from a Flox Catalog package

Discover packages
    search                 Search for system or library packages to install
    show                   Show details about a single package
"""

ACTIVATE_HELP = """\
Usage: flox activate [-d=<path> | -r=<owner>/<name> | -D]

Available options:
    -d, --dir=<path>         Path containing a .flox/ directory
    -m, --mode=ARG           Activate the environment in either "dev" or "run"
        --no-start-services  Suppress automatic service startup
"""


def _claim(**over):
    c = {
        "id": "activate-mode-flag",
        "kind": "flag_exists",
        "command": ["activate"],
        "flag": "--mode",
        "skill_ref": {"file": "skills/flox/SKILL.md", "quote": "flox activate -m dev|run"},
    }
    c.update(over)
    return c


def _help_for(argv):
    """Stand-in for the real probe: argv is the command path."""
    return ACTIVATE_HELP if argv == ["activate"] else TOP_HELP


class TestFlagExists(unittest.TestCase):
    def test_present_flag_passes(self):
        r = check.check_claim(_claim(), _help_for)
        self.assertTrue(r.ok, r.detail)

    def test_absent_flag_drifts(self):
        # The RED case the ticket asks for: a claim that is simply wrong.
        r = check.check_claim(_claim(flag="--not-a-real-flag"), _help_for)
        self.assertFalse(r.ok)
        self.assertIn("--not-a-real-flag", r.detail)

    def test_long_form_matches_a_short_and_long_pair(self):
        r = check.check_claim(_claim(flag="--dir"), _help_for)
        self.assertTrue(r.ok, r.detail)

    def test_long_only_flag_matches(self):
        # `--no-start-services` has no short form and is indented differently.
        r = check.check_claim(_claim(flag="--no-start-services"), _help_for)
        self.assertTrue(r.ok, r.detail)

    def test_a_flag_named_in_prose_does_not_count(self):
        # "dev" appears in --mode's description text. A substring search
        # would pass this; only a real flag token may.
        r = check.check_claim(_claim(flag="--dev"), _help_for)
        self.assertFalse(r.ok)


class TestCommandExists(unittest.TestCase):
    def _cmd(self, name):
        return {
            "id": f"{name}-command", "kind": "command_exists", "command": [name],
            "skill_ref": {"file": "skills/flox/SKILL.md", "quote": f"flox {name}"},
        }

    def test_present_command_passes(self):
        self.assertTrue(check.check_claim(self._cmd("run"), _help_for).ok)

    def test_absent_command_drifts(self):
        r = check.check_claim(self._cmd("teleport"), _help_for)
        self.assertFalse(r.ok)
        self.assertIn("teleport", r.detail)

    def test_a_word_in_a_description_is_not_a_command(self):
        # "packages" appears in several descriptions but is not a subcommand.
        self.assertFalse(check.check_claim(self._cmd("packages"), _help_for).ok)


class TestQuoteBackLink(unittest.TestCase):
    """The registry is two-way linked: a claim whose quote has left the
    skill file is itself stale, and must be reported rather than pass."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        f = self.root / "skills" / "flox" / "SKILL.md"
        f.parent.mkdir(parents=True)
        f.write_text("Use `flox activate -m dev|run` to pick a mode.\n")
        self.addCleanup(self.tmp.cleanup)

    def test_present_quote_passes(self):
        r = check.check_quote(_claim(), self.root)
        self.assertTrue(r.ok, r.detail)

    def test_missing_quote_is_a_finding(self):
        c = _claim()
        c["skill_ref"]["quote"] = "flox activate --gone"
        r = check.check_quote(c, self.root)
        self.assertFalse(r.ok)
        self.assertIn("quote", r.detail.lower())

    def test_missing_file_is_a_finding(self):
        c = _claim()
        c["skill_ref"]["file"] = "skills/flox/NOPE.md"
        r = check.check_quote(c, self.root)
        self.assertFalse(r.ok)

    def test_reports_the_line_number(self):
        r = check.check_quote(_claim(), self.root)
        self.assertEqual(r.line, 1)


class TestRegistryLoading(unittest.TestCase):
    def _write(self, lines):
        p = Path(self.tmp.name) / "claims.jsonl"
        p.write_text("".join(json.dumps(l) + "\n" for l in lines))
        return p

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_loads_valid_lines(self):
        self.assertEqual(len(check.load_claims(self._write([_claim()]))), 1)

    def test_blank_lines_are_skipped(self):
        p = self._write([_claim()])
        p.write_text(p.read_text() + "\n\n")
        self.assertEqual(len(check.load_claims(p)), 1)

    def test_unknown_kind_fails_at_load(self):
        # A registry typo must fail loudly at load, not as a mid-run KeyError.
        with self.assertRaises(ValueError):
            check.load_claims(self._write([_claim(kind="vibes")]))

    def test_missing_field_fails_at_load(self):
        bad = _claim()
        del bad["skill_ref"]
        with self.assertRaises(ValueError):
            check.load_claims(self._write([bad]))

    def test_duplicate_id_fails_at_load(self):
        with self.assertRaises(ValueError):
            check.load_claims(self._write([_claim(), _claim()]))


class TestSuggest(unittest.TestCase):
    """v1 surfaces the current surface next to the stale line. It never
    rewrites: a confident rename is a guess, and that is layer L3."""

    def test_drifted_flag_suggests_the_current_flag_list(self):
        s = check.suggest(_claim(flag="--gone"), _help_for)
        self.assertIn("--mode", s)
        self.assertIn("--dir", s)

    def test_drifted_command_suggests_the_current_command_list(self):
        c = {"id": "x", "kind": "command_exists", "command": ["teleport"],
             "skill_ref": {"file": "f", "quote": "q"}}
        s = check.suggest(c, _help_for)
        self.assertIn("activate", s)
        self.assertIn("search", s)

    def test_suggestion_is_not_a_rewrite(self):
        s = check.suggest(_claim(flag="--gone"), _help_for)
        self.assertNotIn("flox activate -m dev|run", s)


class TestRealRegistry(unittest.TestCase):
    """The shipped registry must load, and every quote it names must still
    exist in the skill it points at. Offline: no flox probe involved."""

    def test_shipped_registry_loads(self):
        claims = check.load_claims(SUITE / "tasks" / "claims.jsonl")
        self.assertGreaterEqual(len(claims), 8, "seed registry is too thin")

    def test_every_quote_still_exists_in_its_skill(self):
        for c in check.load_claims(SUITE / "tasks" / "claims.jsonl"):
            with self.subTest(claim=c["id"]):
                r = check.check_quote(c, REPO_ROOT / "flox-plugin")
                self.assertTrue(r.ok, r.detail)


if __name__ == "__main__":
    unittest.main()
