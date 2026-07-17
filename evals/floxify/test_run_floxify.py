#!/usr/bin/env python3
"""Unit tests for run_floxify's deterministic pieces.

The agentic skill run and the LLM judge are integration-only (exercised by a
real `--only <id>` run). Everything here is pure logic over mocked
subprocesses, so it is fast and safe to gate on.

    python3 -m unittest test_run_floxify -v
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import run_floxify


class TestCheckActivation(unittest.TestCase):
    """AI-454: the activation budget must be configurable, and a timeout must
    not masquerade as 'we didn't check'.

    `skipped` means *we could not run the check* (flox absent, --skip-activation).
    A timeout means *we ran it and the environment did not come up within the
    budget* — that is a finding about the environment, not an absence of one.
    Conflating them silently inflated `activation_skipped` and read as benign:
    posthog timed out at the hardcoded 120s and was recorded as skipped, so the
    largest repo in the corpus produced no activation signal at all.
    """

    @patch("run_floxify.shutil.which", return_value=None)
    def test_flox_absent_is_skipped(self, _which):
        ok, skipped, notes = run_floxify._check_activation("/tmp/x")
        self.assertIsNone(ok)
        self.assertTrue(skipped)
        self.assertIn("flox", notes.lower())

    @patch("run_floxify.shutil.which", return_value="/usr/bin/flox")
    @patch("run_floxify.subprocess.run")
    def test_timeout_is_a_failure_not_a_skip(self, mock_run, _which):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="flox", timeout=120)
        ok, skipped, notes = run_floxify._check_activation("/tmp/x", timeout=120)
        self.assertFalse(ok, "a timeout is a verdict, not an absence of one")
        self.assertFalse(skipped, "must not be recorded as skipped")
        self.assertIn("TIMEOUT", notes)
        self.assertIn("120", notes)

    @patch("run_floxify.shutil.which", return_value="/usr/bin/flox")
    @patch("run_floxify.subprocess.run")
    def test_timeout_budget_is_configurable(self, mock_run, _which):
        mock_run.return_value = MagicMock(returncode=0, stdout="__ok__", stderr="")
        run_floxify._check_activation("/tmp/x", timeout=1800)
        self.assertEqual(mock_run.call_args.kwargs["timeout"], 1800)

    @patch("run_floxify.shutil.which", return_value="/usr/bin/flox")
    @patch("run_floxify.subprocess.run")
    def test_default_budget_preserved_for_small_fixtures(self, mock_run, _which):
        mock_run.return_value = MagicMock(returncode=0, stdout="__ok__", stderr="")
        run_floxify._check_activation("/tmp/x")
        self.assertEqual(mock_run.call_args.kwargs["timeout"], 120)

    @patch("run_floxify.shutil.which", return_value="/usr/bin/flox")
    @patch("run_floxify.subprocess.run")
    def test_successful_activation(self, mock_run, _which):
        mock_run.return_value = MagicMock(returncode=0, stdout="__ok__\n", stderr="")
        ok, skipped, notes = run_floxify._check_activation("/tmp/x")
        self.assertTrue(ok)
        self.assertFalse(skipped)

    @patch("run_floxify.shutil.which", return_value="/usr/bin/flox")
    @patch("run_floxify.subprocess.run")
    def test_failed_activation_reports_stderr(self, mock_run, _which):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="could not resolve package foo"
        )
        ok, skipped, notes = run_floxify._check_activation("/tmp/x")
        self.assertFalse(ok)
        self.assertFalse(skipped)
        self.assertIn("could not resolve", notes)

    @patch("run_floxify.shutil.which", return_value="/usr/bin/flox")
    @patch("run_floxify.subprocess.run")
    def test_unexpected_error_is_skipped_not_failed(self, mock_run, _which):
        # An OSError from the harness is our problem, not the manifest's.
        mock_run.side_effect = OSError("fork failed")
        ok, skipped, notes = run_floxify._check_activation("/tmp/x")
        self.assertIsNone(ok)
        self.assertTrue(skipped)


class TestRunVerify(unittest.TestCase):
    """AI-461: run_floxify's own deterministic leg. check_catalog_live is
    always False here — these must run with no network, mirroring
    test_verify.py's own discipline."""

    def test_no_manifest_is_reported_as_skipped_not_an_error(self):
        result = run_floxify._run_verify(
            run_floxify.DEFAULT_SKILL_DIR, run_floxify.FIXTURES_DIR / "node-postgres",
            None, check_catalog_live=False,
        )
        self.assertEqual(result["violations"], [])
        self.assertIn("skipped", result)
        self.assertNotIn("error", result)

    def test_real_fixture_and_manifest_produce_a_violations_list(self):
        # node-postgres fixture has a `pg` dependency; a manifest with no
        # [services.*] should trip the leaf-datastore-not-served invariant.
        manifest = '[install]\nnodejs.pkg-path = "nodejs_20"\n'
        result = run_floxify._run_verify(
            run_floxify.DEFAULT_SKILL_DIR, run_floxify.FIXTURES_DIR / "node-postgres",
            manifest, check_catalog_live=False,
        )
        self.assertNotIn("error", result)
        rules = {v["rule"] for v in result["violations"]}
        self.assertIn("leaf-datastore-not-served", rules)

    def test_unloadable_skill_dir_reports_error_not_an_exception(self):
        result = run_floxify._run_verify(
            "/nonexistent/skill/dir", run_floxify.FIXTURES_DIR / "node-postgres",
            "[install]\n", check_catalog_live=False,
        )
        self.assertEqual(result["violations"], [])
        self.assertIn("error", result)


class TestMissingFixtureErrorPath(unittest.TestCase):
    """AI-463: run_floxify.py is Tier 1 only (local fixtures/<id>
    checkouts); feeding it a tier2.jsonl entry (real-repo tasks, no
    "tier" key at all) crashed with KeyError: 'tier' in the fixture-not-
    found error path instead of reporting a clean error. Repro:
    `python3 run_floxify.py --tasks tier2.jsonl --only lemmy`.
    """

    def test_missing_fixture_returns_error_dict_not_a_crash(self):
        # A tier2.jsonl-shaped task (no "tier" key) must not raise.
        task = {"id": "definitely-not-a-real-fixture-xyz", "ecosystem": "rust"}
        result = run_floxify.process_task(task, run_floxify.DEFAULT_SKILL_DIR)
        self.assertIn("error", result)
        self.assertEqual(result["id"], task["id"])
        self.assertEqual(result["tier"], "?")

    def test_missing_fixture_error_hints_at_tier2(self):
        # The clearer rejection: point at tier2.jsonl/tier2.py rather than
        # a bare "fixture not found".
        task = {"id": "lemmy", "ecosystem": "rust"}
        result = run_floxify.process_task(task, run_floxify.DEFAULT_SKILL_DIR)
        self.assertIn("tier2.jsonl", result["error"])
        self.assertIn("tier2.py", result["error"])
        self.assertIn("lemmy", result["error"])

    def test_missing_fixture_with_tier_key_present_still_works(self):
        # A Tier 1 task.jsonl-shaped entry (has "tier") but a typo'd/
        # missing fixture id -- the original working case must stay intact.
        task = {"id": "definitely-not-a-real-fixture-xyz", "tier": "should",
                "ecosystem": "python"}
        result = run_floxify.process_task(task, run_floxify.DEFAULT_SKILL_DIR)
        self.assertIn("error", result)
        self.assertEqual(result["tier"], "should")

    def test_base_defaults_tier_to_placeholder_when_missing(self):
        task = {"id": "x", "ecosystem": "python"}
        base = run_floxify._base(task)
        self.assertEqual(base, {"id": "x", "tier": "?", "ecosystem": "python"})

    def test_base_preserves_real_tier_when_present(self):
        task = {"id": "x", "tier": "should", "ecosystem": "python"}
        base = run_floxify._base(task)
        self.assertEqual(base["tier"], "should")


class TestCatalogNote(unittest.TestCase):
    """AI-451/AI-461: the judge prompt must stop grading catalog facts from
    memory — verify_result decides which note it gets instead."""

    def test_no_result_tells_judge_not_to_assert_from_memory(self):
        note = run_floxify._catalog_note(None)
        self.assertIn("do not assert catalog facts from memory", note.lower())

    def test_harness_error_tells_judge_not_to_assert_from_memory(self):
        note = run_floxify._catalog_note({"error": "boom", "violations": []})
        self.assertIn("do not assert catalog facts from memory", note.lower())

    def test_catalog_not_checked_tells_judge_not_to_assert_from_memory(self):
        note = run_floxify._catalog_note({"catalog_checked": False, "violations": []})
        self.assertIn("not run this pass", note.lower())

    def test_clean_catalog_confirms_resolution_to_judge(self):
        note = run_floxify._catalog_note({"catalog_checked": True, "violations": []})
        self.assertIn("confirmed to resolve", note.lower())

    def test_catalog_violations_are_listed_for_the_judge(self):
        result = {
            "catalog_checked": True,
            "violations": [
                {"rule": "catalog-systems-mismatch", "severity": "hard",
                 "message": "nodejs_24 has no build for x86_64-darwin"},
                {"rule": "vars-not-literal", "severity": "hard",
                 "message": "unrelated non-catalog violation"},
            ],
        }
        note = run_floxify._catalog_note(result)
        self.assertIn("1 pkg-path/version/system violation", note)
        self.assertIn("x86_64-darwin", note)
        self.assertNotIn("unrelated non-catalog violation", note)

    def test_unknown_entries_are_excluded_from_the_confirmed_claim(self):
        # Regression: the note must not claim "every combination was
        # CONFIRMED" when verify.py itself couldn't establish some
        # entries' per-system availability (check_catalog's
        # `available is None` path, surfaced as catalog_unknown).
        result = {
            "catalog_checked": True,
            "violations": [],
            "catalog_unknown": [
                {"install_id": "weird", "pkg_path": "weird-pkg", "version": "2.0.0"},
            ],
        }
        note = run_floxify._catalog_note(result)
        self.assertIn("UNKNOWN", note)
        self.assertIn("weird", note)
        self.assertNotIn("every installed pkg-path/version/system combination "
                         "was CONFIRMED", note)

    def test_no_unknown_entries_still_confirms_cleanly(self):
        result = {"catalog_checked": True, "violations": [], "catalog_unknown": []}
        note = run_floxify._catalog_note(result)
        self.assertIn("every installed pkg-path/version/system combination "
                      "was CONFIRMED", note)


class TestStats(unittest.TestCase):
    """The harness leg is advisory (never gates --gate), so
    verify_hard_violation_rate is the one place its signal surfaces
    prominently — a future skill regression must be visible here even
    though nothing fails the build on it (see README's "Why verify.py is
    advisory in the harness")."""

    def _result(self, hard_count, catalog_checked=True, judge_score=5):
        return {
            "id": "x", "tier": "should", "ecosystem": "node",
            "hard_pass": True,
            "judge": {"score": judge_score, "correct": True, "issues": []},
            "verify": {"hard_count": hard_count, "advisory_count": 0,
                      "catalog_checked": catalog_checked},
        }

    def test_hard_violation_rate_reflects_fraction_with_violations(self):
        results = [self._result(0), self._result(2), self._result(0), self._result(1)]
        stats = run_floxify._stats(results)
        self.assertEqual(stats["verify_hard_violation_rate"], 0.5)

    def test_rate_counts_non_catalog_hard_violations_too(self):
        # A hard violation from a network-free invariant (vars-not-literal,
        # hook-mutates-tree, ...) must count even when the catalog leg
        # itself didn't run -- those checks are independent of flox/network.
        results = [self._result(1, catalog_checked=False)]
        stats = run_floxify._stats(results)
        self.assertEqual(stats["verify_hard_violation_rate"], 1.0)

    def test_rate_is_none_when_no_verify_results(self):
        results = [{
            "id": "x", "tier": "should", "ecosystem": "node", "hard_pass": True,
            "judge": {"score": 5, "correct": True, "issues": []},
        }]
        stats = run_floxify._stats(results)
        self.assertIsNone(stats["verify_hard_violation_rate"])

    def test_all_clean_gives_zero_rate(self):
        results = [self._result(0), self._result(0)]
        stats = run_floxify._stats(results)
        self.assertEqual(stats["verify_hard_violation_rate"], 0.0)


class TestVacuousRunMessage(unittest.TestCase):
    """AI-463 I1(a): a run where every task errored (e.g. tier2.jsonl
    entries fed to this Tier-1-only harness) must not exit 0 with an
    empty-looking "measurement run" — see run_floxify.py's KeyError fix
    above for the failure mode this generalizes past."""

    def test_all_errored_returns_a_hint(self):
        results = [
            {"id": "lemmy", "tier": "?", "ecosystem": "rust",
             "error": "no fixtures/lemmy directory ... tier2.py --only lemmy"},
        ]
        msg = run_floxify._vacuous_run_message(results)
        self.assertIsNotNone(msg)
        self.assertIn("tier2.py", msg)
        self.assertIn("lemmy", msg)

    def test_mixed_scored_and_errored_returns_none(self):
        # A per-task rejection among a larger run keeps the existing
        # record-error-and-continue discipline -- must NOT trigger this.
        results = [
            {"id": "ok-one", "tier": "should", "ecosystem": "node",
             "judge": {"score": 5, "correct": True, "issues": []}},
            {"id": "missing-one", "tier": "should", "ecosystem": "node",
             "error": "no fixtures/missing-one directory"},
        ]
        self.assertIsNone(run_floxify._vacuous_run_message(results))

    def test_all_scored_returns_none(self):
        results = [
            {"id": "ok-one", "tier": "should", "ecosystem": "node",
             "judge": {"score": 5, "correct": True, "issues": []}},
        ]
        self.assertIsNone(run_floxify._vacuous_run_message(results))

    def test_empty_results_returns_none(self):
        # Defensive: main() already exits earlier via --only's own
        # "no task with id" check before results could ever be empty in
        # practice, but the helper itself must not misreport this shape.
        self.assertIsNone(run_floxify._vacuous_run_message([]))


class TestVacuousRunIntegration(unittest.TestCase):
    """The exact reported repro, run as a real subprocess -- cheap and
    fast because it fails at the fixture-existence check, before the
    real `claude` agent is ever invoked."""

    def test_repro_shape_exits_nonzero_with_the_hint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = str(Path(tmpdir) / "repro-result.json")
            proc = subprocess.run(
                [sys.executable, "run_floxify.py", "--tasks", "tier2.jsonl",
                 "--only", "lemmy", "--out", out_path],
                cwd=str(run_floxify.HERE), capture_output=True, text=True, timeout=30,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("tier2.py", proc.stderr)
            self.assertIn("lemmy", proc.stderr)


class TestGateShouldFail(unittest.TestCase):
    """AI-463 I1(b): --gate must fail when the should-tier binding set is
    EMPTY, not just when something in it failed -- "GATE PASSED: all 0
    should-tier fixtures pass hard-checks" is vacuous truth, the same
    failure class the golden-lint vacuous-pass fix in PR #42 addressed."""

    def _should_result(self, hard_pass=True):
        return {"id": "x", "tier": "should", "hard_pass": hard_pass}

    def test_fails_when_binding_is_empty(self):
        # e.g. a run that only scored may/stretch-tier fixtures.
        self.assertTrue(run_floxify._gate_should_fail(binding=[], bad=[], errs=[]))

    def test_fails_when_a_should_tier_hard_check_failed(self):
        binding = [self._should_result(hard_pass=False)]
        self.assertTrue(run_floxify._gate_should_fail(binding, bad=binding, errs=[]))

    def test_fails_when_a_should_tier_task_errored(self):
        binding = [self._should_result()]
        errs = [{"id": "y", "tier": "should", "error": "boom"}]
        self.assertTrue(run_floxify._gate_should_fail(binding, bad=[], errs=errs))

    def test_passes_when_binding_is_non_empty_and_clean(self):
        binding = [self._should_result()]
        self.assertFalse(run_floxify._gate_should_fail(binding, bad=[], errs=[]))

    def test_larger_run_with_one_missing_fixture_still_gates_on_real_results(self):
        # A missing fixture among several tasks (here, a non-should-tier
        # one, isolating this from "a should-tier task itself errored")
        # must not zero out the should-tier binding built from the tasks
        # that DID score -- record-error-and-continue, not vacuous-fail.
        results = [
            {"id": "ok-one", "tier": "should", "ecosystem": "node",
             "hard_pass": True, "judge": {"score": 5, "correct": True, "issues": []}},
            {"id": "missing-one", "tier": "may", "ecosystem": "node",
             "error": "no fixtures/missing-one directory"},
        ]
        # main()'s own construction (mirrored here, not re-derived by hand).
        scored = [r for r in results if "judge" in r]
        binding = [r for r in scored if r["tier"] == "should"]
        bad = [r for r in binding if not r["hard_pass"]]
        errs = [r for r in results if "error" in r and r.get("tier") == "should"]

        self.assertEqual(len(binding), 1)
        self.assertEqual(errs, [])  # the error was may-tier, not should-tier
        self.assertFalse(run_floxify._gate_should_fail(binding, bad, errs))
        self.assertIsNone(run_floxify._vacuous_run_message(results))


if __name__ == "__main__":
    unittest.main()
