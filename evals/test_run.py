#!/usr/bin/env python3
"""Unit tests for run.py's deterministic pieces.

The agent + judge calls are integration-only. Everything here is pure logic
over mocked subprocesses — no claude, no network, no API spend.

    python3 -m unittest test_run -v
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
