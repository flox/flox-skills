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
import re
import unittest
from pathlib import Path

from _skill_module_loader import load_module

HERE = Path(__file__).resolve().parent
SUITE = HERE.parent
REPO_ROOT = SUITE.parent.parent
PLUGIN_JSON = REPO_ROOT / "flox-plugin" / ".claude-plugin" / "plugin.json"
VERIFY = REPO_ROOT / "flox-plugin" / "skills" / "floxify" / "scripts" / "verify.py"
SKILL_MD = REPO_ROOT / "flox-plugin" / "skills" / "floxify" / "SKILL.md"

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

    def _plugin_version_dashed(self):
        self.assertTrue(PLUGIN_JSON.is_file(), f"plugin.json not at {PLUGIN_JSON}")
        return json.loads(PLUGIN_JSON.read_text())["version"].replace(".", "-")

    def test_verify_py_matches(self):
        want = self._plugin_version_dashed()
        self.assertEqual(
            verify.SKILL_VERSION, want,
            f"verify.py SKILL_VERSION ({verify.SKILL_VERSION}) has drifted from "
            f"plugin.json ({want}); update the literal.",
        )

    def test_skill_md_command_blocks_match(self):
        """SKILL.md's command blocks carry the tag as a literal a model
        copies, so the version lives there too and drifts the same way."""
        self.assertTrue(SKILL_MD.is_file(), f"SKILL.md not at {SKILL_MD}")
        want = self._plugin_version_dashed()
        found = set(re.findall(r"agentic\.skill\.floxify\.([0-9-]+)",
                               SKILL_MD.read_text()))
        self.assertTrue(found, "no agentic.skill.floxify tag in SKILL.md; "
                               "the command-block prefixes were removed")
        self.assertEqual(
            found, {want},
            f"SKILL.md tags {sorted(found)} disagree with plugin.json ({want})",
        )

    def test_every_flox_run_block_is_tagged(self):
        """A `flox run` block without the prefix is an untagged invocation
        the model will copy verbatim. Guards against one being added later
        without the tag, which is silent rather than broken."""
        blocks = [ln for ln in SKILL_MD.read_text().splitlines()
                  if "flox run -p python313" in ln]
        self.assertTrue(blocks, "no `flox run -p python313` block found")
        text = SKILL_MD.read_text()
        for ln in blocks:
            with self.subTest(line=ln.strip()[:60]):
                i = text.index(ln)
                # the prefix sits on the line above, joined by a backslash
                preceding = text[:i].rsplit("\n", 2)[-2] if i else ""
                self.assertIn("FLOX_INVOCATION_SOURCE", preceding,
                              f"untagged `flox run` block: {ln.strip()[:80]}")


if __name__ == "__main__":
    unittest.main()
