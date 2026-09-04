#!/usr/bin/env python3
"""FLOX_INVOCATION_SOURCE tagging in verify.py (AI-597).

The tag is how a flox invocation gets attributed to this skill rather than
to the agent host that happened to run it. Two things need holding: the
append semantics (a nested context's tag must survive), and the version
literal (it cannot be read from plugin.json at runtime, so nothing but a
test keeps the two in step).

Run from the suite root (`evals/floxify/`):
    python3 -m unittest tests.test_invocation_source -v
"""
import json
import unittest
from pathlib import Path

from _skill_module_loader import load_module

HERE = Path(__file__).resolve().parent
SUITE = HERE.parent
REPO_ROOT = SUITE.parent.parent
PLUGIN_JSON = REPO_ROOT / "flox-plugin" / ".claude-plugin" / "plugin.json"
VERIFY = REPO_ROOT / "flox-plugin" / "skills" / "floxify" / "scripts" / "verify.py"

# No sys_modules_key: nothing here patches by string name, so this gets a
# private instance rather than competing for the shared "verify" key.
verify = load_module(VERIFY)


class TestInvocationSourceTag(unittest.TestCase):
    def test_tag_shape(self):
        # agentic.skill.<skill>.<version>, version dash-delimited so a
        # consumer truncating to three fields coalesces across versions.
        self.assertEqual(
            verify.INVOCATION_SOURCE,
            f"agentic.skill.floxify.{verify.SKILL_VERSION}",
        )
        self.assertRegex(verify.SKILL_VERSION, r"^\d+(-\d+)*$")

    def test_version_is_right_anchored_extractable(self):
        # The ticket's consumer contract: the version is the LAST segment
        # matching the numeric shape, not field 4 — an optional reference
        # path between the skill and the version varies the field count.
        last = verify.INVOCATION_SOURCE.split(".")[-1]
        self.assertEqual(last, verify.SKILL_VERSION)

    def test_sets_when_unset(self):
        env = {}
        verify._tag_invocation_source(env)
        self.assertEqual(env["FLOX_INVOCATION_SOURCE"], verify.INVOCATION_SOURCE)

    def test_appends_and_preserves_an_existing_tag(self):
        # flox-mcp-server or a CI wrapper set theirs first; both survive.
        env = {"FLOX_INVOCATION_SOURCE": "agentic.flox-mcp"}
        verify._tag_invocation_source(env)
        self.assertEqual(
            env["FLOX_INVOCATION_SOURCE"],
            f"agentic.flox-mcp,{verify.INVOCATION_SOURCE}",
        )

    def test_preserves_multiple_existing_tags_in_order(self):
        env = {"FLOX_INVOCATION_SOURCE": "agentic.claude-code.cli,agentic.flox-mcp"}
        verify._tag_invocation_source(env)
        self.assertEqual(
            env["FLOX_INVOCATION_SOURCE"],
            f"agentic.claude-code.cli,agentic.flox-mcp,{verify.INVOCATION_SOURCE}",
        )

    def test_is_idempotent(self):
        # Re-running must not duplicate: the CLI unions the list, but a
        # repeated entry misreports as two invocations in the raw header.
        env = {}
        verify._tag_invocation_source(env)
        verify._tag_invocation_source(env)
        self.assertEqual(env["FLOX_INVOCATION_SOURCE"], verify.INVOCATION_SOURCE)

    def test_empty_string_is_not_treated_as_a_tag(self):
        # A parent that exported the variable empty must not yield a
        # leading comma, which would read as an empty source.
        env = {"FLOX_INVOCATION_SOURCE": ""}
        verify._tag_invocation_source(env)
        self.assertEqual(env["FLOX_INVOCATION_SOURCE"], verify.INVOCATION_SOURCE)

    def test_import_already_tagged_the_process(self):
        # Module load sets it, which is what makes every `flox show` this
        # script runs carry the tag without per-call plumbing.
        import os
        self.assertIn(verify.INVOCATION_SOURCE,
                      os.environ.get("FLOX_INVOCATION_SOURCE", "").split(","))


class TestVersionMatchesPluginJson(unittest.TestCase):
    """SKILL_VERSION is a literal because plugin.json does not ship in three
    of the four agent layouts (flox-agent-layout.sh gives codex, pi and
    opencode bare skill dirs with no plugin root). This is the only thing
    keeping the literal honest, so it asserts rather than skips."""

    def test_matches(self):
        self.assertTrue(PLUGIN_JSON.is_file(), f"plugin.json not at {PLUGIN_JSON}")
        version = json.loads(PLUGIN_JSON.read_text())["version"]
        self.assertEqual(
            verify.SKILL_VERSION, version.replace(".", "-"),
            f"verify.py SKILL_VERSION ({verify.SKILL_VERSION}) has drifted from "
            f"plugin.json version ({version}); update the literal.",
        )


if __name__ == "__main__":
    unittest.main()
