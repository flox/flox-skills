#!/usr/bin/env python3
"""Unit tests for run.py's deterministic pieces.

The agent + judge calls are integration-only. Everything here is pure logic
over mocked subprocesses — no claude, no network, no API spend.

    python3 -m unittest tests.test_run -v
"""
import json
import subprocess
import unittest
from unittest.mock import MagicMock, patch

import run


# A realistic `claude -p --output-format json` envelope. The harness has always
# read exactly one field of this (`result`) and dropped the rest — including the
# cost it is handed on every single call (AI-459).
CLAUDE_JSON = {
    "result": "here is your manifest",
    "total_cost_usd": 1.2717,
    "duration_ms": 406123,
    "num_turns": 14,
    "usage": {
        "input_tokens": 2,
        "cache_creation_input_tokens": 32775,
        "cache_read_input_tokens": 957709,
        "output_tokens": 18594,
    },
}


class TestParseMeta(unittest.TestCase):
    """Cost/usage extraction from the claude envelope."""

    def test_extracts_cost_usage_duration(self):
        meta = run._parse_meta(CLAUDE_JSON)
        self.assertAlmostEqual(meta["cost_usd"], 1.2717)
        self.assertEqual(meta["duration_ms"], 406123)
        self.assertEqual(meta["usage"]["output_tokens"], 18594)
        self.assertEqual(meta["usage"]["cache_read_input_tokens"], 957709)

    def test_missing_fields_do_not_raise(self):
        # Never let a cost-accounting detail break a run.
        meta = run._parse_meta({"result": "x"})
        self.assertEqual(meta["cost_usd"], 0.0)
        self.assertEqual(meta["usage"], {})

    def test_non_numeric_cost_is_zero_not_crash(self):
        meta = run._parse_meta({"result": "x", "total_cost_usd": None})
        self.assertEqual(meta["cost_usd"], 0.0)


class TestRunClaudeReturnsMeta(unittest.TestCase):
    @patch("run.subprocess.run")
    def test_success_returns_meta(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(CLAUDE_JSON), stderr=""
        )
        result, err, meta = run.run_claude("p", "skills", None)
        self.assertIsNone(err)
        self.assertEqual(result, "here is your manifest")
        self.assertAlmostEqual(meta["cost_usd"], 1.2717)

    @patch("run.subprocess.run")
    def test_error_returns_zero_cost_meta_not_none(self, mock_run):
        # A failed call may still have burned tokens; and callers must be able
        # to sum unconditionally without a None check.
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=1)
        result, err, meta = run.run_claude("p", "skills", None, retries=1)
        self.assertIsNone(result)
        self.assertEqual(err, "TIMEOUT")
        self.assertEqual(meta["cost_usd"], 0.0)


class TestProcessTaskRecordsCost(unittest.TestCase):
    """A task's cost is agent + judge — the judge is half of every run's calls
    and has never been separately visible."""

    TASK = {"id": "t1", "area": "env", "tier": "should",
            "prompt": "p", "rubric": "r", "checks": []}

    @patch("run.subprocess.run")
    def test_task_records_agent_and_judge_cost_split(self, mock_run):
        judge_json = dict(CLAUDE_JSON)
        judge_json["result"] = '{"score": 5, "correct": true, "issues": []}'
        judge_json["total_cost_usd"] = 0.2
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps(CLAUDE_JSON), stderr=""),
            MagicMock(returncode=0, stdout=json.dumps(judge_json), stderr=""),
        ]
        r = run.process_task(self.TASK, "skills", None)
        self.assertAlmostEqual(r["cost"]["agent_usd"], 1.2717)
        self.assertAlmostEqual(r["cost"]["judge_usd"], 0.2)
        self.assertAlmostEqual(r["cost"]["total_usd"], 1.4717)


class TestAutoStartChecks(unittest.TestCase):
    """`services.auto-start` hard-checks (AI-503).

    Both failure modes below produce a manifest flox refuses to load, so a
    check that merely greps for the string would pass a broken answer.
    """

    def _answer(self, toml):
        return f"Add this to your manifest:\n\n```toml\n{toml}```\n"

    def test_accepts_key_on_the_services_table(self):
        self.assertTrue(run._sets_auto_start(self._answer(
            'schema-version = "1.12.0"\n\n[services]\nauto-start = true\n'
            'web.command = "python3 -m http.server"\n'
        )))

    def test_accepts_top_level_dotted_form(self):
        self.assertTrue(run._sets_auto_start(self._answer(
            'schema-version = "1.12.0"\nservices.auto-start = true\n\n'
            '[services.web]\ncommand = "python3 -m http.server"\n'
        )))

    def test_rejects_key_inside_a_service(self):
        # flox: unknown field `auto-start`, expected one of `command`, `vars`, ...
        self.assertFalse(run._sets_auto_start(self._answer(
            'schema-version = "1.12.0"\n\n[services.web]\n'
            'command = "python3 -m http.server"\nauto-start = true\n'
        )))

    def test_rejects_prose_only_mention(self):
        self.assertFalse(run._sets_auto_start(
            "You can set auto-start = true somewhere in there."
        ))

    def test_does_not_borrow_a_services_header_from_another_block(self):
        answer = ('```toml\n[services]\nweb.command = "x"\n```\n'
                  '```toml\n[services.web]\nauto-start = true\n```\n')
        self.assertFalse(run._sets_auto_start(answer))

    def test_accepts_manifest_preceded_by_a_bash_block(self):
        # ANSWER_SUFFIX asks for the manifest *and* the commands, so a mixed
        # answer is the expected shape and block order varies run to run. The
        # old fence regex matched an empty info string, so the closing ```
        # of the bash block read as an opening one and the manifest was lost —
        # a red gate on a correct answer.
        answer = ('```bash\nflox edit\n```\n\n'
                  '```toml\nschema-version = "1.12.0"\n\n[services]\n'
                  'auto-start = true\nweb.command = "x"\n```\n')
        self.assertTrue(run._sets_auto_start(answer))
        self.assertTrue(run.CHECKS["auto_start_schema_version"](answer))

    def test_rejects_auto_start_inside_a_multiline_command_body(self):
        # tomllib: `services.auto-start` does not exist here — the text is
        # part of `web.command`. The old line scanner had no '''/""" state.
        self.assertFalse(run._sets_auto_start(self._answer(
            'schema-version = "1.12.0"\n\n[services]\nweb.command = \'\'\'\n'
            'auto-start = true\nsleep 100\n\'\'\'\n'
        )))

    def test_accepts_key_after_a_bracket_leading_shell_line(self):
        # `[ -d node_modules ] || npm ci` inside a command body set the
        # scanner's current table to `-d node_modules`, hiding the real key
        # that followed — a manifest tomllib confirms is correct.
        self.assertTrue(run._sets_auto_start(self._answer(
            'schema-version = "1.12.0"\n\n[services]\nweb.command = \'\'\'\n'
            '[ -d node_modules ] || npm ci\nnpm start\n\'\'\'\n'
            'auto-start = true\n'
        )))


class TestAutoStartSchemaVersion(unittest.TestCase):
    """The schema half of the `services.auto-start` gate (AI-503).

    All three facts — the key is set, the schema is new enough, no `version = 1`
    survives — must hold in the SAME fenced manifest. Asserting them across the
    whole answer certified manifests the check never inspected.
    """

    ok = staticmethod(lambda a: run.CHECKS["auto_start_schema_version"](a))

    def _manifest(self, version_line):
        return (f'```toml\n{version_line}\n\n[services]\nauto-start = true\n'
                'web.command = "python3 -m http.server"\n```\n')

    def test_accepts_1_12_and_newer(self):
        for v in ('"1.12.0"', '"1.13.0"', '"1.20.0"', '"1.100.0"', '"2.0.0"'):
            with self.subTest(v=v):
                self.assertTrue(self.ok(self._manifest(f"schema-version = {v}")))

    def test_accepts_toml_literal_string_form(self):
        # `'1.12.0'` is an ordinary TOML string; the old substring probe only
        # matched the double-quoted spelling.
        self.assertTrue(self.ok(self._manifest("schema-version = '1.12.0'")))

    def test_rejects_older_schema(self):
        for v in ('"1.11.0"', '"1.10.0"'):
            with self.subTest(v=v):
                self.assertFalse(self.ok(self._manifest(f"schema-version = {v}")))

    def test_rejects_legacy_version_line(self):
        self.assertFalse(self.ok(self._manifest("version = 1")))

    def test_rejects_malformed_versions(self):
        # A substring probe accepted all of these.
        for v in ('"1.12garbage"', '"1.29-nonsense"', '"1.12"', '"garbage"'):
            with self.subTest(v=v):
                self.assertFalse(self.ok(self._manifest(f"schema-version = {v}")))

    def test_rejects_prose_schema_over_a_version_1_manifest(self):
        # The RED this task exists to catch: "knows the key exists and even
        # places it correctly, but keeps `version = 1`". flox rejects it with
        # `invalid type: boolean true, expected struct ServiceDescriptor`.
        answer = ('You need schema-version = "1.12.0" for this.\n\n'
                  '```toml\nversion = 1\n\n[services]\nauto-start = true\n'
                  'web.command = "x"\n```\n')
        self.assertTrue(run._sets_auto_start(answer))  # placement is right
        self.assertFalse(self.ok(answer))              # ... but it cannot load

    def test_rejects_schema_declared_in_a_different_block(self):
        answer = ('```toml\nschema-version = "1.12.0"\n```\n\n'
                  '```toml\nversion = 1\n\n[services]\nauto-start = true\n```\n')
        self.assertFalse(self.ok(answer))

    def test_rejects_manifest_carrying_both_version_keys(self):
        # flox rejects a manifest with both spellings.
        answer = ('```toml\nversion = 1\nschema-version = "1.12.0"\n\n'
                  '[services]\nauto-start = true\n```\n')
        self.assertFalse(self.ok(answer))


class TestBuildSandboxChecks(unittest.TestCase):
    """`sandbox = "warn"|"enforce"` / `sandbox-allow` hard-checks (AI-503).

    Both fields arrived with schema 1.13.0; under `version = 1` flox rejects
    the manifest with ``unknown variant `warn`, expected `off` or `pure` ``.
    """

    def _answer(self, toml):
        return f"```toml\n{toml}```\n"

    ENFORCE = ('[build.app]\ncommand = "make"\nsandbox = "enforce"\n'
               'sandbox-allow = [ "~/.npm/**" ]\n')

    def test_accepts_enforce_with_schema_1_13(self):
        a = self._answer(f'schema-version = "1.13.0"\n\n{self.ENFORCE}')
        self.assertTrue(run.CHECKS["sets_build_sandbox_mode"](a))
        self.assertTrue(run.CHECKS["build_sandbox_schema_version"](a))

    def test_rejects_gated_field_under_version_1(self):
        a = self._answer(f"version = 1\n\n{self.ENFORCE}")
        self.assertTrue(run.CHECKS["sets_build_sandbox_mode"](a))
        self.assertFalse(run.CHECKS["build_sandbox_schema_version"](a))

    def test_rejects_schema_below_1_13(self):
        a = self._answer(f'schema-version = "1.12.0"\n\n{self.ENFORCE}')
        self.assertFalse(run.CHECKS["build_sandbox_schema_version"](a))

    def test_ungated_sandbox_values_do_not_count(self):
        for mode in ('"off"', '"pure"'):
            with self.subTest(mode=mode):
                a = self._answer(f'version = 1\n\n[build.app]\ncommand = "make"\n'
                                 f"sandbox = {mode}\n")
                self.assertFalse(run.CHECKS["sets_build_sandbox_mode"](a))

    def test_boolean_sandbox_does_not_count(self):
        # `sandbox = true` is the habit the skill exists to break.
        a = self._answer('schema-version = "1.13.0"\n\n[build.app]\n'
                         'command = "make"\nsandbox = true\n')
        self.assertFalse(run.CHECKS["sets_build_sandbox_mode"](a))

    def test_prose_only_mention_does_not_count(self):
        self.assertFalse(run.CHECKS["sets_build_sandbox_mode"](
            'Set sandbox = "enforce" in your build section.'
        ))


class TestFencedManifestExtraction(unittest.TestCase):
    """Fence handling is delegated to `skill_toml_lint.extract_blocks`."""

    def test_skips_non_toml_fences(self):
        text = '```bash\nflox install hello\n```\n\n```toml\nversion = 1\n```\n'
        self.assertEqual([b.body for b in run._fenced_manifests(text)], ["version = 1\n"])

    def test_unterminated_fence_does_not_lose_earlier_blocks(self):
        text = '```toml\nversion = 1\n```\n\n```toml\n[services]\nauto-start = true\n'
        self.assertEqual(len(run._fenced_manifests(text)), 2)

    def test_invalid_toml_block_is_dropped_not_raised(self):
        text = '```toml\nthis is not = = toml\n```\n\n```toml\nversion = 1\n```\n'
        self.assertEqual(run._parsed_manifests(text), [{"version": 1}])


class TestBuildParser(unittest.TestCase):
    def test_help_renders(self):
        # argparse percent-expands help lazily, so a bare `%` only raises when
        # the help is formatted. This covers every help string in run.py.
        self.assertIn("--gate", run.build_parser().format_help())


class TestCostSummary(unittest.TestCase):
    def test_sums_across_tasks_and_splits_agent_vs_judge(self):
        results = [
            {"cost": {"agent_usd": 1.0, "judge_usd": 0.2, "total_usd": 1.2}},
            {"cost": {"agent_usd": 2.0, "judge_usd": 0.3, "total_usd": 2.3}},
            {"error": "boom"},  # errored tasks must not break the sum
        ]
        c = run._cost_summary(results)
        self.assertAlmostEqual(c["total_usd"], 3.5)
        self.assertAlmostEqual(c["agent_usd"], 3.0)
        self.assertAlmostEqual(c["judge_usd"], 0.5)
        self.assertAlmostEqual(c["mean_per_task_usd"], 1.75)

    def test_empty_results(self):
        c = run._cost_summary([])
        self.assertEqual(c["total_usd"], 0.0)


if __name__ == "__main__":
    unittest.main()
