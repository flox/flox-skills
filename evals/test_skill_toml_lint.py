#!/usr/bin/env python3
"""Unit tests for skill_toml_lint.py's deterministic pieces.

Pure logic and a mocked subprocess boundary — no flox, no network, no API
spend, same discipline as test_run.py / test_screen.py. The live `flox edit`
run is the skill-toml-lint CI job; this suite is what makes the guard itself
trustworthy enough to gate on, and it runs on every PR.

Three concerns:

  1. Block extraction — fences, info strings, indentation, skip markers. This
     is where a guard silently loses teeth: an extractor that quietly drops
     blocks reports "0 failed" forever.
  2. The tier split — classify() / is_failure() against REAL `flox edit`
     output (captured on flox 1.13.2), so the structural/catalog boundary is
     tested against what flox actually prints, not a paraphrase.
  3. The shipped skill itself — every document extracts cleanly and every
     skip marker carries a reason. Free, so it gates per-PR alongside 1 and 2.

    flox activate -- python3 -m unittest test_skill_toml_lint -v
"""
import unittest
from unittest.mock import patch

import skill_toml_lint as lint

# ---- real `flox edit -f` output --------------------------------------------
# Captured verbatim on flox 1.13.2. PARSE_ERROR_OUTPUT is what the guard
# printed when a `[hook]` bare-shell-line bug was deliberately re-injected
# into references/services.md to prove the guard fails on a real defect.

PARSE_ERROR_OUTPUT = """✘ ERROR: Failed to parse manifest:

TOML parse error at line 12, column 6
   |
12 | echo "broken on purpose"
   |      ^
key with no value, expected `=`
"""

# A snippet that PARSED fine and then failed looking for packages. The
# structural tier must treat this as a pass: flox got past the parser.
CATALOG_ERROR_OUTPUT = """✘ ERROR: could not be resolved: nosuchpkg
"""


class TestExtraction(unittest.TestCase):
    def extract(self, text, path="doc.md"):
        return lint.extract_blocks(text, path)

    def test_extracts_toml_and_records_fence_line(self):
        text = "intro\n\n```toml\nversion = 1\n```\n"
        (block,) = self.extract(text)
        # 1-based line of the OPENING fence — what the failure message points at.
        self.assertEqual(block.line, 3)
        self.assertEqual(block.info, "toml")
        self.assertEqual(block.body, "version = 1\n")
        self.assertEqual(block.id, "doc.md:3")

    def test_ignores_non_toml_fences(self):
        text = "```bash\nflox init\n```\n\n```\nplain\n```\n\n```json\n{}\n```\n"
        self.assertEqual(self.extract(text), [])

    def test_extracts_multiple_blocks_in_document_order(self):
        text = "```toml\na = 1\n```\ntext\n```toml\nb = 2\n```\n"
        first, second = self.extract(text)
        self.assertEqual([first.line, second.line], [1, 5])
        self.assertEqual([first.body, second.body], ["a = 1\n", "b = 2\n"])

    def test_indented_fence_is_dedented(self):
        # The skill nests blocks under list items; the body must be dedented or
        # every line arrives at flox with leading whitespace.
        text = "- item:\n\n  ```toml\n  version = 1\n  [install]\n  ```\n"
        (block,) = self.extract(text)
        self.assertEqual(block.body, "version = 1\n[install]\n")

    def test_bash_block_inside_prose_does_not_swallow_following_toml(self):
        text = "```bash\nflox init\n```\n\n```toml\nversion = 1\n```\n"
        (block,) = self.extract(text)
        self.assertEqual(block.line, 5)

    def test_unterminated_fence_raises(self):
        # Silently swallowing the rest of the file would hide every later block.
        with self.assertRaises(ValueError) as ctx:
            self.extract("```toml\nversion = 1\n")
        self.assertIn("unterminated", str(ctx.exception))

    def test_longer_closing_fence_run_closes_block(self):
        text = "````toml\nversion = 1\n````\n"
        (block,) = self.extract(text)
        self.assertEqual(block.body, "version = 1\n")


class TestSkipMarkers(unittest.TestCase):
    def test_unmarked_block_is_checked(self):
        (block,) = lint.extract_blocks("```toml\nversion = 1\n```\n", "d.md")
        self.assertIsNone(block.skip_reason)

    def test_comment_marker_skips_and_captures_reason(self):
        text = "```toml\n# eval: skip fragment - descriptors only\n[install]\n```\n"
        (block,) = lint.extract_blocks(text, "d.md")
        self.assertEqual(block.skip_reason, "fragment - descriptors only")

    def test_marker_is_recognised_anywhere_in_the_block(self):
        text = "```toml\n[build.x]\n# eval: skip metadata fields only\n```\n"
        (block,) = lint.extract_blocks(text, "d.md")
        self.assertEqual(block.skip_reason, "metadata fields only")

    def test_marker_without_reason_is_still_a_skip_but_labelled(self):
        (block,) = lint.extract_blocks("```toml\n# eval: skip\n```\n", "d.md")
        self.assertEqual(block.skip_reason, "(no reason given)")

    def test_fragment_info_string_skips(self):
        (block,) = lint.extract_blocks("```toml-fragment\n[install]\n```\n", "d.md")
        self.assertEqual(block.info, "toml-fragment")
        self.assertEqual(block.skip_reason, "```toml-fragment fence")

    def test_prose_mentioning_the_marker_does_not_skip(self):
        # The marker must be a standalone comment LINE, not a substring: a block
        # documenting the convention must not disable itself.
        text = '```toml\nnote = "write # eval: skip to opt out"\n```\n'
        (block,) = lint.extract_blocks(text, "d.md")
        self.assertIsNone(block.skip_reason)


class TestManifestText(unittest.TestCase):
    def test_prepends_version_when_absent(self):
        (block,) = lint.extract_blocks("```toml\n[install]\n```\n", "d.md")
        self.assertEqual(block.manifest_text(), "version = 1\n[install]\n")

    def test_leaves_snippet_alone_when_top_level_version_present(self):
        (block,) = lint.extract_blocks("```toml\nversion = 1\n[install]\n```\n", "d.md")
        self.assertEqual(block.manifest_text(), "version = 1\n[install]\n")

    def test_leaves_snippet_alone_when_schema_version_present(self):
        # `schema-version` REPLACES `version = 1` (a manifest holding both is
        # rejected: "unknown field `schema-version`"), so a snippet declaring it
        # — the only way to exercise a later-schema field like
        # `services.auto-start` — must not get `version = 1` prepended (AI-503).
        text = '```toml\nschema-version = "1.12.0"\n[services]\nauto-start = true\n```\n'
        (block,) = lint.extract_blocks(text, "d.md")
        self.assertEqual(
            block.manifest_text(),
            'schema-version = "1.12.0"\n[services]\nauto-start = true\n',
        )

    def test_schema_version_inside_a_table_does_not_count(self):
        text = '```toml\n[build.x]\nschema-version = "1.0"\n```\n'
        (block,) = lint.extract_blocks(text, "d.md")
        self.assertTrue(block.manifest_text().startswith("version = 1\n"))

    def test_version_inside_a_table_does_not_count(self):
        # [build.x] version = "1.0" is a package version, not the manifest
        # schema version — prepending is still required or flox rejects it.
        text = '```toml\n[build.x]\nversion = "1.0"\n```\n'
        (block,) = lint.extract_blocks(text, "d.md")
        self.assertTrue(block.manifest_text().startswith("version = 1\n"))


class TestClassify(unittest.TestCase):
    def test_exit_zero_is_ok(self):
        self.assertEqual(lint.classify(0, ""), lint.OK)

    def test_real_parse_error_output(self):
        self.assertEqual(lint.classify(1, PARSE_ERROR_OUTPUT), lint.PARSE_ERROR)

    def test_real_catalog_error_output_is_not_a_parse_error(self):
        self.assertEqual(lint.classify(1, CATALOG_ERROR_OUTPUT), lint.CATALOG_ERROR)

    def test_unrecognised_failure_is_other(self):
        self.assertEqual(lint.classify(1, "✘ ERROR: disk full"), lint.OTHER_ERROR)


class TestTierBinding(unittest.TestCase):
    def test_structural_binds_only_on_parse_errors(self):
        self.assertTrue(lint.is_failure(lint.PARSE_ERROR, "structural"))
        # Parsed fine, then went looking for packages — passed this tier.
        self.assertFalse(lint.is_failure(lint.CATALOG_ERROR, "structural"))
        self.assertFalse(lint.is_failure(lint.OTHER_ERROR, "structural"))

    def test_catalog_binds_on_any_non_clean_exit(self):
        self.assertTrue(lint.is_failure(lint.PARSE_ERROR, "catalog"))
        self.assertTrue(lint.is_failure(lint.CATALOG_ERROR, "catalog"))
        self.assertTrue(lint.is_failure(lint.OTHER_ERROR, "catalog"))

    def test_ok_skipped_and_known_never_bind(self):
        for tier in ("structural", "catalog"):
            for status in (lint.OK, lint.SKIPPED, lint.KNOWN_FAILURE):
                self.assertFalse(lint.is_failure(status, tier), (status, tier))


class TestStaleAllowlist(unittest.TestCase):
    def result(self, path, fingerprint, status):
        return {"file": path, "fingerprint": fingerprint, "status": status}

    def test_entry_whose_block_now_passes_is_stale(self):
        with patch.dict(lint.KNOWN_PARSE_FAILURES, {("a.md", "dead"): "fixed"}, clear=True):
            stale = lint.stale_allowlist_entries([self.result("a.md", "dead", lint.OK)])
        self.assertEqual(stale, [("a.md", "dead")])

    def test_entry_still_matching_a_known_failure_is_not_stale(self):
        with patch.dict(lint.KNOWN_PARSE_FAILURES, {("a.md", "live"): "still broken"}, clear=True):
            stale = lint.stale_allowlist_entries(
                [self.result("a.md", "live", lint.KNOWN_FAILURE)]
            )
        self.assertEqual(stale, [])

    def test_unvisited_document_is_not_judged(self):
        # A --only run sees a subset; it must not declare the rest stale.
        with patch.dict(lint.KNOWN_PARSE_FAILURES, {("b.md", "x"): "broken"}, clear=True):
            stale = lint.stale_allowlist_entries([self.result("a.md", "y", lint.OK)])
        self.assertEqual(stale, [])


class TestCheckBlocksWiring(unittest.TestCase):
    """check_blocks against a mocked subprocess boundary (no flox)."""

    def run_blocks(self, text, returncode=0, output=""):
        blocks = lint.extract_blocks(text, "d.md")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            rc = 0 if cmd[1] == "init" else returncode
            return type("P", (), {"returncode": rc, "stdout": output, "stderr": ""})()

        with patch.object(lint.subprocess, "run", side_effect=fake_run):
            results = lint.check_blocks(blocks)
        edits = [c for c in calls if c[1] == "edit"]
        return results, edits

    def test_skipped_block_never_invokes_flox(self):
        text = "```toml\n# eval: skip fragment\n[install]\n```\n"
        results, edits = self.run_blocks(text)
        self.assertEqual(edits, [])
        self.assertEqual(results[0]["status"], lint.SKIPPED)
        self.assertFalse(results[0]["failed"])

    def test_checked_block_invokes_flox_edit_once(self):
        results, edits = self.run_blocks("```toml\nversion = 1\n```\n")
        self.assertEqual(len(edits), 1)
        self.assertEqual(results[0]["status"], lint.OK)

    def test_parse_error_is_reported_with_file_and_line(self):
        text = "pad\n\n```toml\nbroken\n```\n"
        results, _ = self.run_blocks(text, returncode=1, output=PARSE_ERROR_OUTPUT)
        self.assertEqual(results[0]["status"], lint.PARSE_ERROR)
        self.assertTrue(results[0]["failed"])
        self.assertEqual(results[0]["id"], "d.md:3")

    def test_allowlisted_parse_error_downgrades_to_known_failure(self):
        text = "```toml\nbroken\n```\n"
        (block,) = lint.extract_blocks(text, "d.md")
        entry = {("d.md", block.fingerprint): "tracked separately"}
        with patch.dict(lint.KNOWN_PARSE_FAILURES, entry, clear=True):
            results, _ = self.run_blocks(text, returncode=1, output=PARSE_ERROR_OUTPUT)
        self.assertEqual(results[0]["status"], lint.KNOWN_FAILURE)
        self.assertFalse(results[0]["failed"])


class TestShippedSkill(unittest.TestCase):
    """The real skill documents — extraction only, so this stays free."""

    @classmethod
    def setUpClass(cls):
        cls.blocks = lint.collect_blocks(lint.DEFAULT_SKILL_DIR)

    def test_skill_documents_include_skill_md_and_references(self):
        docs = [p.name for p in lint.skill_documents(lint.DEFAULT_SKILL_DIR)]
        self.assertIn("SKILL.md", docs)
        self.assertGreater(len(docs), 1, "references/*.md should be picked up")

    def test_every_document_extracts_without_error(self):
        # collect_blocks raises on an unterminated fence; reaching here means
        # every shipped document is well-formed.
        self.assertTrue(self.blocks)

    def test_the_guard_actually_checks_most_blocks(self):
        # A regression that skipped everything would still exit 0. Pin the
        # ratio loosely so ordinary edits don't trip it.
        checked = [b for b in self.blocks if not b.skip_reason]
        self.assertGreater(len(checked), len(self.blocks) // 2)

    def test_every_skip_marker_states_a_reason(self):
        vague = [
            b.id for b in self.blocks
            if b.skip_reason in ("(no reason given)",)
        ]
        self.assertEqual(vague, [], "opting a block out requires a written reason")

    def test_allowlist_is_empty(self):
        # The intended steady state: fix the snippet, don't allowlist it.
        # If you must add an entry, delete this test's assertion knowingly.
        self.assertEqual(
            dict(lint.KNOWN_PARSE_FAILURES), {},
            "KNOWN_PARSE_FAILURES should stay empty; fix the snippet instead",
        )


if __name__ == "__main__":
    unittest.main()
