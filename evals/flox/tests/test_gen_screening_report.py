"""gen_screening_report.py: a report may only claim what was measured.

The generator merges N per-model `screen-<model>.json` measurement files
against a candidate registry. The registry is the set a run was DRAWN FROM,
not the set that was screened -- `tasks/screening.jsonl` holds 51 entries and
the committed report covers the 19 that were actually run.

The bug these tests pin: rows were built from the registry rather than from the
measurements, and the no-signal bucket predicate read
`all((not r) or r["classification"] == "no-signal" ...)`. A candidate with no
result on any model satisfies that vacuously, so it was published under
"No-signal -- baseline already passes" -- an observation nobody made. With
`results/` gitignored, the documented `--results results/screen-*.json` matches
nothing on a fresh checkout, `load()` skipped the missing files by design, and
the command wrote a report claiming all 51 candidates screened and passing,
from zero measurements, at exit 0.

Two rules follow, and both are asserted below: an unmeasured candidate is never
bucketed, and a run with no measurements at all is an error rather than an
empty report.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import gen_screening_report as gen

HERE = Path(gen.__file__).resolve().parent


def _result(cid, classification="no-signal", gap=0.0):
    """One per-candidate record, in the shape screen.py writes."""
    return {
        "id": cid,
        "classification": classification,
        "judge_gap": gap,
        "baseline": {"hard_pass_count": 5, "ok_reps": 5},
        "skills": {"hard_pass_count": 5, "ok_reps": 5},
    }


def _results_file(path, model, results):
    path.write_text(json.dumps({
        "summary": {
            "model": model, "reps": 5, "discriminators": 0, "skill_gaps": 0,
            "no_signals": len(results), "errors": 0,
            "mean_baseline_hard_pass_rate": 1.0,
            "mean_skills_hard_pass_rate": 1.0,
            "mean_judge_gap": 0.0, "total_cost_usd": 1.0,
        },
        "results": results,
    }))


def _registry_file(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


class TestUnmeasuredCandidatesAreNeverBucketed(unittest.TestCase):
    """A registry entry with no result is not a measurement of anything."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.out = self.tmp / "REPORT.md"
        self.registry = self.tmp / "registry.jsonl"
        # Two entries; only `measured-one` is screened.
        _registry_file(self.registry, [
            {"id": "measured-one", "area": "triggering", "trigger_test": True},
            {"id": "never-run", "area": "builds"},
        ])
        self.res = self.tmp / "screen-haiku.json"
        _results_file(self.res, "claude-haiku-4-5-20251001",
                      [_result("measured-one")])

    def _run(self):
        with patch.object(sys, "argv", [
            "gen_screening_report.py", "--results", str(self.res),
            "--candidates", str(self.registry), "--out", str(self.out),
        ]):
            gen.main()
        return self.out.read_text()

    def test_unmeasured_id_is_absent_from_the_no_signal_bucket(self):
        nosig = next(l for l in self._run().splitlines()
                     if l.startswith("- **No-signal"))
        self.assertIn("measured-one", nosig)
        self.assertNotIn("never-run", nosig)
        self.assertIn("(1)", nosig)

    def test_unmeasured_id_is_reported_as_not_screened(self):
        line = next(l for l in self._run().splitlines()
                    if l.startswith("- **Not screened"))
        self.assertIn("never-run", line)
        self.assertNotIn("measured-one", line)

    def test_unmeasured_id_has_no_row_in_the_ranked_table(self):
        rows = [l for l in self._run().splitlines() if l.startswith("| ")]
        self.assertTrue(any("measured-one" in r for r in rows))
        self.assertFalse(any("never-run" in r for r in rows))

    def test_method_line_counts_measured_candidates_not_the_registry(self):
        method = next(l for l in self._run().splitlines()
                      if l.startswith("- **1 candidate"))
        self.assertIn("1 candidates screened", method)
        self.assertIn("of the 2 entries", method)

    def test_stdout_reports_both_counts(self):
        proc = subprocess.run(
            [sys.executable, str(HERE / "gen_screening_report.py"),
             "--results", str(self.res), "--candidates", str(self.registry),
             "--out", str(self.out)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("1 of 2 registry candidates screened", proc.stdout)
        self.assertIn("1 unscreened", proc.stdout)


class TestZeroMeasurementsIsAnError(unittest.TestCase):
    """The fresh-checkout case: `--results results/screen-*.json` with no
    results/ directory. An unmatched shell glob arrives as a literal path that
    does not exist, so every measurement file is missing."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.out = self.tmp / "REPORT.md"
        self.registry = self.tmp / "registry.jsonl"
        _registry_file(self.registry, [
            {"id": "a", "area": "triggering"}, {"id": "b", "area": "builds"},
        ])

    def _argv(self):
        return [
            "gen_screening_report.py",
            "--results", str(self.tmp / "results" / "screen-*.json"),
            "--candidates", str(self.registry), "--out", str(self.out),
        ]

    def test_exits_nonzero(self):
        with patch.object(sys, "argv", self._argv()):
            with self.assertRaises(SystemExit) as cm:
                gen.main()
        self.assertNotEqual(cm.exception.code, 0)

    def test_writes_no_report_at_all(self):
        with patch.object(sys, "argv", self._argv()):
            with self.assertRaises(SystemExit):
                gen.main()
        # The whole failure mode was a plausible-looking file appearing here.
        self.assertFalse(self.out.exists())

    def test_stderr_names_the_missing_path(self):
        proc = subprocess.run(
            [sys.executable, str(HERE / "gen_screening_report.py")]
            + self._argv()[1:],
            capture_output=True, text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("screen-*.json", proc.stderr)
        self.assertIn("baselines/screen-", proc.stderr)


class TestPartialRunsStillReport(unittest.TestCase):
    """"Safe to re-run as models finish" is a real requirement: a file that is
    not there YET is skipped with a note, as long as something was measured."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.out = self.tmp / "REPORT.md"
        self.registry = self.tmp / "registry.jsonl"
        _registry_file(self.registry, [{"id": "a", "area": "triggering"}])
        self.have = self.tmp / "screen-haiku.json"
        _results_file(self.have, "claude-haiku-4-5-20251001", [_result("a")])

    def test_missing_file_is_skipped_with_a_note_when_another_exists(self):
        proc = subprocess.run(
            [sys.executable, str(HERE / "gen_screening_report.py"),
             "--results", str(self.have), str(self.tmp / "screen-opus.json"),
             "--candidates", str(self.registry), "--out", str(self.out)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("screen-opus.json", proc.stderr)
        self.assertTrue(self.out.exists())

    def test_measured_id_absent_from_the_registry_is_flagged(self):
        orphan = self.tmp / "screen-opus.json"
        _results_file(orphan, "claude-opus-4-8", [_result("a"), _result("ghost")])
        proc = subprocess.run(
            [sys.executable, str(HERE / "gen_screening_report.py"),
             "--results", str(self.have), str(orphan),
             "--candidates", str(self.registry), "--out", str(self.out)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("ghost", proc.stderr)
        self.assertNotIn("ghost", self.out.read_text())


class TestProvenanceIsGeneratedFromTheRun(unittest.TestCase):
    """The 19-of-51 caveat used to be prose typed into SCREENING-REPORT.md, so
    regenerating the report deleted the only record that the screened set was a
    subset of the registry. It is derived now, and these assert it against the
    committed measurements -- i.e. the real report, not a fixture."""

    OUT = None

    @classmethod
    def setUpClass(cls):
        cls._tmp = TemporaryDirectory()
        out = Path(cls._tmp.name) / "REPORT.md"
        argv = ["gen_screening_report.py", "--results"] + [
            str(HERE / "baselines" / f"screen-{m}.json")
            for m in ("haiku", "sonnet", "opus")
        ] + ["--candidates", str(HERE / "tasks" / "screening.jsonl"),
             "--out", str(out)]
        with patch.object(sys, "argv", argv):
            gen.main()
        cls.OUT = out.read_text()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_names_the_screened_subset_against_the_registry_total(self):
        self.assertIn("Screened **19 of the 51** entries", self.OUT)

    def test_names_the_delta_the_reproduce_recipe_adds(self):
        # The recipe's `--area freshness --area triggering` selects 20, one more
        # than was screened. Naming which one is the whole point of the caveat.
        self.assertIn("trig-secret-free-shared-env", self.OUT)
        self.assertIn("yields 20 entries", self.OUT)

    def test_reproduce_block_points_at_files_that_exist(self):
        block = self.OUT.split("```bash")[1].split("```")[0]
        self.assertIn("--results baselines/screen-haiku.json", block)
        for m in ("haiku", "sonnet", "opus"):
            self.assertTrue((HERE / "baselines" / f"screen-{m}.json").exists())
        # The stale recipe pointed --results at gitignored results/ as the only
        # option, which is what produced a report from zero measurements.
        self.assertNotIn("--results results/screen-*.json \\", block)

    def test_reproduce_recipe_selection_matches_the_screened_areas(self):
        block = self.OUT.split("```bash")[1].split("```")[0]
        self.assertIn("--area freshness --area triggering", block)

    def test_unscreened_registry_entries_are_named_in_the_report(self):
        line = next(l for l in self.OUT.splitlines()
                    if l.startswith("- **Not screened"))
        self.assertIn("(32)", line)
        self.assertIn("trap-hook-return-not-exit", line)

    def test_no_signal_bucket_is_the_six_measured_ones(self):
        line = next(l for l in self.OUT.splitlines()
                    if l.startswith("- **No-signal"))
        self.assertIn("(6)", line)

    def test_committed_report_is_what_the_generator_emits(self):
        # The hand-edited caveat is gone precisely because the file is now
        # reproducible; if it drifts again this is what says so.
        committed = (HERE / "reports" / "SCREENING-REPORT.md").read_text()
        self.assertEqual(committed, self.OUT)


if __name__ == "__main__":
    unittest.main()
