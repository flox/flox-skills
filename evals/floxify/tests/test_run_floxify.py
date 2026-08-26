#!/usr/bin/env python3
"""Unit tests for run_floxify's deterministic pieces.

The agentic skill run and the LLM judge are integration-only (exercised by a
real `--only <id>` run). Everything here is pure logic over mocked
subprocesses, so it is fast and safe to gate on.

    python3 -m unittest tests.test_run_floxify -v
"""
import json
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
    """AI-463: run_floxify.py is synthetic only (local fixtures/<id>
    checkouts); feeding it a real-world.jsonl entry (real-repo tasks, no
    "tier" key at all) crashed with KeyError: 'tier' in the fixture-not-
    found error path instead of reporting a clean error. Repro:
    `python3 run_floxify.py --tasks real-world.jsonl --only lemmy`.
    """

    def test_missing_fixture_returns_error_dict_not_a_crash(self):
        # A real-world.jsonl-shaped task (no "tier" key) must not raise.
        task = {"id": "definitely-not-a-real-fixture-xyz", "ecosystem": "rust"}
        result = run_floxify.process_task(task, run_floxify.DEFAULT_SKILL_DIR)
        self.assertIn("error", result)
        self.assertEqual(result["id"], task["id"])
        self.assertEqual(result["tier"], "?")

    def test_missing_fixture_error_hints_at_real_world(self):
        # The clearer rejection: point at real-world.jsonl/real_world.py rather than
        # a bare "fixture not found".
        task = {"id": "lemmy", "ecosystem": "rust"}
        result = run_floxify.process_task(task, run_floxify.DEFAULT_SKILL_DIR)
        self.assertIn("real-world.jsonl", result["error"])
        self.assertIn("real_world.py", result["error"])
        self.assertIn("lemmy", result["error"])

    def test_missing_fixture_with_tier_key_present_still_works(self):
        # A synthetic task.jsonl-shaped entry (has "tier") but a typo'd/
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
                {"install_id": "weird", "pkg_path": "weird-pkg",
                 "version": "2.0.0", "reason": "the reason it gave"},
            ],
        }
        note = run_floxify._catalog_note(result)
        self.assertIn("could NOT be evaluated", note)
        self.assertIn("weird", note)
        self.assertNotIn("every installed pkg-path/version/system combination "
                         "was CONFIRMED", note)
        # The entry's own reason is relayed rather than a single sentence
        # asserted over every entry -- verify.py has three distinct ways
        # of failing to conclude and one of them (a semver range it does
        # not resolve) is not about `flox show`'s text at all.
        self.assertIn("the reason it gave", note)

    def test_no_unknown_entries_still_confirms_cleanly(self):
        result = {"catalog_checked": True, "violations": [], "catalog_unknown": []}
        note = run_floxify._catalog_note(result)
        self.assertIn("every installed pkg-path/version/system combination "
                      "was CONFIRMED", note)

    def test_a_violation_does_not_hide_an_unchecked_entry(self):
        # The two are independent facts about different entries, and
        # reporting them exclusively told the judge about the violation
        # and nothing about the entry beside it that was never checked --
        # which is the shape this note exists to close, one branch over.
        result = {
            "catalog_checked": True,
            "violations": [
                {"rule": "catalog-systems-mismatch", "severity": "hard",
                 "message": "nodejs_24 has no build for x86_64-darwin"},
            ],
            "catalog_unknown": [
                {"install_id": "weird", "pkg_path": "weird-pkg",
                 "version": "^2.0", "reason": "the reason it gave"},
            ],
        }
        note = run_floxify._catalog_note(result)
        self.assertIn("1 pkg-path/version/system violation", note)
        self.assertIn("x86_64-darwin", note)
        self.assertIn("could NOT be evaluated", note)
        self.assertIn("the reason it gave", note)

    def test_the_singular_verb_agrees(self):
        result = {"catalog_checked": True, "violations": [],
                  "catalog_unknown": [{"install_id": "weird",
                                       "reason": "r"}]}
        note = run_floxify._catalog_note(result)
        self.assertIn("1 install entry could NOT be evaluated and was not "
                      "confirmed either way", note)

    def test_a_truncated_listing_says_so_and_keeps_the_count(self):
        # The note ends by claiming every entry it did not name resolved,
        # so an item dropped in silence is swept into a positive claim
        # that is false about it.
        result = {
            "catalog_checked": True, "violations": [],
            "catalog_unknown": [
                {"install_id": f"p{i}", "reason": "r"} for i in range(9)
            ],
        }
        note = run_floxify._catalog_note(result)
        self.assertIn("9 install entries could NOT be evaluated", note)
        self.assertIn("(first 5 of 9 shown)", note)
        self.assertNotIn("p8", note)


class TestStats(unittest.TestCase):
    """The harness leg is advisory (never gates --gate), so
    verify_hard_violation_rate is the one place its signal surfaces
    prominently — a future skill regression must be visible here even
    though nothing fails the build on it (see README's "Why verify.py is
    advisory in the harness")."""

    def _result(self, hard_count, catalog_checked=True, judge_score=5,
                catalog_unknown=None):
        return {
            "id": "x", "tier": "should", "ecosystem": "node",
            "hard_pass": True,
            "judge": {"score": judge_score, "correct": True, "issues": []},
            "verify": {"hard_count": hard_count, "advisory_count": 0,
                      "catalog_checked": catalog_checked,
                      "catalog_unknown": list(catalog_unknown or [])},
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

    def test_unchecked_entries_are_counted_apart_from_clean_ones(self):
        # `verify_clean` is "checked and no hard violation", and an entry
        # the catalog leg DECLINED to check contributes zero violations
        # -- so a fixture nothing could be established about scores
        # exactly like one that was confirmed good. Both fixtures below
        # are `verify_clean`; only one of them was actually verified.
        results = [
            self._result(0),
            self._result(0, catalog_unknown=[{"install_id": "a"},
                                             {"install_id": "b"}]),
        ]
        stats = run_floxify._stats(results)
        self.assertEqual(stats["verify_clean"], 2)
        self.assertEqual(stats["verify_unknown"], 1)
        self.assertEqual(stats["verify_unknown_entries"], 2)

    def test_no_unknowns_reports_zero_rather_than_nothing(self):
        stats = run_floxify._stats([self._result(0), self._result(1)])
        self.assertEqual(stats["verify_unknown"], 0)
        self.assertEqual(stats["verify_unknown_entries"], 0)


class TestVacuousRunMessage(unittest.TestCase):
    """AI-463 I1(a): a run where every task errored (e.g. real-world.jsonl
    entries fed to this synthetic-only harness) must not exit 0 with an
    empty-looking "measurement run" — see run_floxify.py's KeyError fix
    above for the failure mode this generalizes past."""

    def test_all_errored_returns_a_hint(self):
        results = [
            {"id": "lemmy", "tier": "?", "ecosystem": "rust",
             "error": "no fixtures/lemmy directory ... real_world.py --only lemmy"},
        ]
        msg = run_floxify._vacuous_run_message(results)
        self.assertIsNotNone(msg)
        self.assertIn("real_world.py", msg)
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
                [sys.executable, "run_floxify.py", "--tasks", "real-world.jsonl",
                 "--only", "lemmy", "--out", out_path],
                cwd=str(run_floxify.HERE), capture_output=True, text=True, timeout=30,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("real_world.py", proc.stderr)
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


# ---------------------------------------------------------------------------
# AI-442: cost/usage/turn envelope parsing (AI-459 port) + stream-json
# tool-call extraction (Q1)
# ---------------------------------------------------------------------------

STREAM_SAMPLE = (
    run_floxify.HERE / "samples" / "flox-search-sample.jsonl"
).read_text(encoding="utf-8")


class TestParseMetaFloxify(unittest.TestCase):
    """Direct mirror of AI-459's own test_run.py approach: well-formed,
    missing fields, non-dict usage, garbage cost -- never raises, always
    zeroes cleanly."""

    def test_well_formed_envelope(self):
        meta = run_floxify._parse_meta({
            "total_cost_usd": 0.51, "usage": {"output_tokens": 100},
            "duration_ms": 5000, "num_turns": 3,
        })
        self.assertEqual(meta["cost_usd"], 0.51)
        self.assertEqual(meta["usage"], {"output_tokens": 100})
        self.assertEqual(meta["duration_ms"], 5000)
        self.assertEqual(meta["num_turns"], 3)

    def test_missing_fields_zero_cleanly(self):
        meta = run_floxify._parse_meta({})
        self.assertEqual(meta["cost_usd"], 0.0)
        self.assertEqual(meta["usage"], {})
        self.assertEqual(meta["duration_ms"], 0)
        self.assertEqual(meta["num_turns"], 0)

    def test_non_dict_usage_becomes_empty_dict(self):
        meta = run_floxify._parse_meta({"usage": "not a dict"})
        self.assertEqual(meta["usage"], {})

    def test_garbage_cost_does_not_raise(self):
        meta = run_floxify._parse_meta({"total_cost_usd": "not a number"})
        self.assertEqual(meta["cost_usd"], 0.0)

    def test_garbage_duration_and_turns_do_not_raise(self):
        meta = run_floxify._parse_meta({
            "duration_ms": "garbage", "num_turns": None,
        })
        self.assertEqual(meta["duration_ms"], 0)
        self.assertEqual(meta["num_turns"], 0)

    def test_real_result_event_from_captured_stream(self):
        # The terminal `result` event of a REAL captured stream (AI-442
        # PR 1's sanctioned flag-verification call) -- confirms the field
        # names line up with a genuine claude invocation, not just a
        # hand-written fixture.
        events = [json.loads(l) for l in STREAM_SAMPLE.splitlines() if l.strip()]
        result_event = [e for e in events if e.get("type") == "result"][0]
        meta = run_floxify._parse_meta(result_event)
        self.assertGreater(meta["cost_usd"], 0)
        self.assertEqual(meta["num_turns"], 2)
        self.assertIn("output_tokens", meta["usage"])


class TestClassifyToolCalls(unittest.TestCase):
    def test_counts_a_bash_flox_search_call(self):
        events = [{
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": "flox search hello"}},
            ]},
        }]
        counts = run_floxify._classify_tool_calls(events)
        self.assertEqual(counts, {"total": 1, "flox_search": 1, "flox_show": 0})

    def test_counts_a_bash_flox_show_call(self):
        events = [{
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": "flox show nodejs_20"}},
            ]},
        }]
        counts = run_floxify._classify_tool_calls(events)
        self.assertEqual(counts, {"total": 1, "flox_search": 0, "flox_show": 1})

    def test_non_flox_bash_call_counts_toward_total_only(self):
        events = [{
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
            ]},
        }]
        counts = run_floxify._classify_tool_calls(events)
        self.assertEqual(counts, {"total": 1, "flox_search": 0, "flox_show": 0})

    def test_non_bash_tool_counts_toward_total_only(self):
        events = [{
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}},
            ]},
        }]
        counts = run_floxify._classify_tool_calls(events)
        self.assertEqual(counts["total"], 1)
        self.assertEqual(counts["flox_search"], 0)

    def test_flox_search_after_a_shell_separator_still_counts(self):
        events = [{
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": "cd /tmp && flox search redis"}},
            ]},
        }]
        counts = run_floxify._classify_tool_calls(events)
        self.assertEqual(counts["flox_search"], 1)

    def test_multiline_bash_block_counts_every_line_not_just_the_first(self):
        # AI-442 I1 (review-found): a Bash tool_use commonly carries a
        # MULTILINE script -- without \n in the separator class, every
        # line but the first was invisible, undercounting exactly the
        # reps that issued several catalog lookups in one Bash call.
        events = [{
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": "flox search nodejs\nflox show nodejs_20"}},
            ]},
        }]
        counts = run_floxify._classify_tool_calls(events)
        self.assertEqual(counts, {"total": 1, "flox_search": 1, "flox_show": 1})

    def test_mention_of_flox_search_in_unrelated_text_does_not_count(self):
        # "flox search" as a SUBSTRING mid-word/mid-sentence, not a
        # leading command, must not be mistaken for an invocation.
        events = [{
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": "echo 'reminder: try flox search later'"}},
            ]},
        }]
        counts = run_floxify._classify_tool_calls(events)
        self.assertEqual(counts["flox_search"], 0)

    def test_non_assistant_events_are_ignored(self):
        events = [{"type": "user", "message": {"content": []}}, {"type": "result"}]
        counts = run_floxify._classify_tool_calls(events)
        self.assertEqual(counts, {"total": 0, "flox_search": 0, "flox_show": 0})

    def test_malformed_events_do_not_raise(self):
        events = [None, {}, {"type": "assistant"}, {"type": "assistant", "message": {}},
                  {"type": "assistant", "message": {"content": "not a list"}},
                  {"type": "assistant", "message": {"content": [None, {"type": "text"}]}}]
        counts = run_floxify._classify_tool_calls(events)
        self.assertEqual(counts, {"total": 0, "flox_search": 0, "flox_show": 0})

    def test_real_captured_stream_matches_manual_inspection(self):
        # The real fixture has exactly one Bash tool_use, `flox search
        # hello` -- confirmed by manual inspection during the AI-442 PR 1
        # flag-verification call.
        events = [json.loads(l) for l in STREAM_SAMPLE.splitlines() if l.strip()]
        counts = run_floxify._classify_tool_calls(events)
        self.assertEqual(counts, {"total": 1, "flox_search": 1, "flox_show": 0})


class TestParseStream(unittest.TestCase):
    def test_real_captured_stream_end_to_end(self):
        result_text, meta, has_result = run_floxify._parse_stream(STREAM_SAMPLE)
        self.assertTrue(has_result)
        self.assertIn("first result", result_text.lower())
        self.assertEqual(meta["num_turns"], 2)
        self.assertGreater(meta["cost_usd"], 0)
        self.assertEqual(meta["tool_calls"],
                          {"total": 1, "flox_search": 1, "flox_show": 0})
        self.assertEqual(meta["raw_stream"], STREAM_SAMPLE)

    def test_empty_stream_yields_zero_meta_and_no_result(self):
        result_text, meta, has_result = run_floxify._parse_stream("")
        self.assertFalse(has_result)
        self.assertEqual(result_text, "")
        self.assertEqual(meta["cost_usd"], 0.0)
        self.assertEqual(meta["num_turns"], 0)
        self.assertEqual(meta["tool_calls"], {"total": 0, "flox_search": 0, "flox_show": 0})

    def test_garbled_lines_are_skipped_not_raised(self):
        stream = "not json\n{\"type\": \"assistant\"\n" + STREAM_SAMPLE
        result_text, meta, has_result = run_floxify._parse_stream(stream)
        # The garbled lines are skipped; the real sample's own result
        # event is still found and parsed correctly.
        self.assertTrue(has_result)
        self.assertEqual(meta["num_turns"], 2)

    def test_truncated_stream_with_no_result_event_still_counts_tool_calls(self):
        # A rep that timed out mid-stream: tool_use events exist, but no
        # terminal `result` line was ever written. Tool-call counting
        # must not silently drop to zero just because the stream is
        # incomplete -- that would undercount exactly the reps most
        # likely to show a real turns/tool-calls delta.
        lines = [l for l in STREAM_SAMPLE.splitlines() if l.strip()]
        truncated = "\n".join(l for l in lines
                              if json.loads(l).get("type") != "result")
        result_text, meta, has_result = run_floxify._parse_stream(truncated)
        self.assertFalse(has_result)
        self.assertEqual(result_text, "")
        self.assertEqual(meta["cost_usd"], 0.0)  # no result event -> no cost data
        self.assertEqual(meta["tool_calls"]["flox_search"], 1)  # but tool calls still counted


# ---------------------------------------------------------------------------
# AI-442 C1 (review-found Critical): a live review caught that this
# machine's user-scope ~/.claude/settings.json has flox@flox-skills
# enabled in enabledPlugins, and --strict-mcp-config only gates MCP
# servers, not plugins -- so the "baseline" arm would silently run WITH
# the skill loaded. The committed samples/flox-search-
# sample.jsonl (captured with NEITHER --plugin-dir NOR --setting-sources)
# is itself the reproduction: its init event shows the flox plugin
# loaded despite never being requested.
# ---------------------------------------------------------------------------

REAL_CONTAMINATED_INIT_EVENT = next(
    json.loads(l) for l in STREAM_SAMPLE.splitlines()
    if l.strip() and json.loads(l).get("type") == "system"
    and json.loads(l).get("subtype") == "init"
)

# The two follow-up sanctioned live calls made to verify the C1 fix
# (--setting-sources project,local): one with --plugin-dir (skills arm,
# must still load the plugin), one without (baseline arm, must NOT).
# See samples/README.md's "C1 fix verification" section.
SKILLS_ARM_SAMPLE = (
    run_floxify.HERE / "samples"
    / "skills-arm-setting-sources-sample.jsonl"
).read_text(encoding="utf-8")
BASELINE_ARM_SAMPLE = (
    run_floxify.HERE / "samples"
    / "baseline-arm-setting-sources-sample.jsonl"
).read_text(encoding="utf-8")


class TestC1FixRealCapturedEvidence(unittest.TestCase):
    """Encodes the review's demanded two-arm demonstration as a
    permanent regression test, not just a one-off manual verification:
    real `claude` calls, both with --setting-sources project,local,
    differing only in --plugin-dir presence."""

    def test_skills_arm_with_setting_sources_still_loads_the_plugin(self):
        init_event = run_floxify._find_init_event(SKILLS_ARM_SAMPLE)
        self.assertIsNotNone(init_event)
        self.assertTrue(run_floxify._detect_flox_plugin_contamination(init_event))
        # Loaded from the local --plugin-dir path, not the user-scope
        # marketplace cache -- proves --setting-sources excluded the
        # cached copy without breaking the CLI-level plugin load.
        sources = [p.get("source") for p in init_event.get("plugins", [])]
        self.assertIn("flox@inline", sources)

    def test_baseline_arm_with_setting_sources_is_genuinely_clean(self):
        init_event = run_floxify._find_init_event(BASELINE_ARM_SAMPLE)
        self.assertIsNotNone(init_event)
        self.assertEqual(init_event.get("plugins"), [])
        self.assertFalse(run_floxify._detect_flox_plugin_contamination(init_event))


class TestFindInitEvent(unittest.TestCase):
    def test_finds_the_real_captured_streams_init_event(self):
        event = run_floxify._find_init_event(STREAM_SAMPLE)
        self.assertIsNotNone(event)
        self.assertEqual(event["type"], "system")
        self.assertEqual(event["subtype"], "init")

    def test_returns_none_when_no_init_event_present(self):
        stream = '{"type": "assistant", "message": {"content": []}}\n'
        self.assertIsNone(run_floxify._find_init_event(stream))

    def test_returns_none_for_empty_stream(self):
        self.assertIsNone(run_floxify._find_init_event(""))

    def test_garbled_lines_do_not_raise(self):
        stream = "not json\n" + STREAM_SAMPLE
        event = run_floxify._find_init_event(stream)
        self.assertIsNotNone(event)


class TestDetectFloxPluginContamination(unittest.TestCase):
    def test_real_captured_init_event_is_detected_as_contaminated(self):
        # This is the actual reproduction: my own sanctioned flag-
        # verification call (no --plugin-dir, no --setting-sources)
        # still loaded the flox plugin on this machine.
        self.assertTrue(
            run_floxify._detect_flox_plugin_contamination(REAL_CONTAMINATED_INIT_EVENT)
        )

    def test_plugin_named_flox_via_flox_skills_source_is_detected(self):
        init_event = {"plugins": [{"name": "flox", "source": "flox@flox-skills"}]}
        self.assertTrue(run_floxify._detect_flox_plugin_contamination(init_event))

    def test_plugin_named_flox_via_flox_marketplace_source_is_detected(self):
        # This machine's settings.json has BOTH flox@flox-marketplace and
        # flox@flox-skills enabled -- either marketplace name must trip
        # the guard, not just the one in the reproduction fixture.
        init_event = {"plugins": [{"name": "flox", "source": "flox@flox-marketplace"}]}
        self.assertTrue(run_floxify._detect_flox_plugin_contamination(init_event))

    def test_flox_slash_command_alone_is_detected(self):
        # Redundant signal: even if the `plugins` list's shape changes
        # upstream, a flox: slash command being present is independent
        # evidence of contamination.
        init_event = {"plugins": [], "slash_commands": ["flox:floxify"]}
        self.assertTrue(run_floxify._detect_flox_plugin_contamination(init_event))

    def test_clean_init_event_with_unrelated_plugins_is_not_detected(self):
        init_event = {
            "plugins": [{"name": "slack", "source": "slack@claude-plugins-official"}],
            "slash_commands": ["slack:standup", "clipboard"],
        }
        self.assertFalse(run_floxify._detect_flox_plugin_contamination(init_event))

    def test_no_plugins_or_slash_commands_key_is_not_detected(self):
        self.assertFalse(run_floxify._detect_flox_plugin_contamination({}))

    def test_none_init_event_is_not_detected(self):
        # _find_init_event returns None when no init event was found at
        # all -- must not raise, must not false-positive.
        self.assertFalse(run_floxify._detect_flox_plugin_contamination(None))

    def test_malformed_plugins_entries_do_not_raise(self):
        init_event = {"plugins": [None, "not a dict", 42], "slash_commands": None}
        self.assertFalse(run_floxify._detect_flox_plugin_contamination(init_event))


class TestBuildPrompt(unittest.TestCase):
    """AI-442 batch-1 finding: the two arms need functionally equivalent
    prompts, not textually identical ones -- /floxify is an unrecognized
    slash command (a hard CLI rejection, not a graceful no-op) when the
    plugin isn't loaded, so the original identical-prompt design killed
    every baseline rep (40/40) in the first real batch."""

    def test_skills_arm_invokes_the_slash_command(self):
        prompt = run_floxify._build_prompt("/tmp/some-fixture", "skills")
        self.assertIn("/floxify /tmp/some-fixture", prompt)

    def test_baseline_arm_does_not_contain_the_slash_command(self):
        prompt = run_floxify._build_prompt("/tmp/some-fixture", "baseline")
        self.assertNotIn("/floxify", prompt)

    def test_baseline_arm_states_the_task_in_flox_s_own_vocabulary(self):
        # Fair per the screen.py precedent: naming Flox's own standard
        # conventions (what `flox init` creates, the activation success
        # anchor) is fair game -- it's the same vocabulary a real user
        # would put in a plain-language request, not SKILL.md guidance.
        prompt = run_floxify._build_prompt("/tmp/some-fixture", "baseline")
        self.assertIn(".flox/env/manifest.toml", prompt)
        self.assertIn("flox activate", prompt)
        self.assertIn("/tmp/some-fixture", prompt)

    def test_baseline_arm_does_not_ask_for_interactive_input(self):
        prompt = run_floxify._build_prompt("/tmp/some-fixture", "baseline")
        self.assertIn("Do not ask for or wait for user input", prompt)


class TestDetectHarnessMisconfiguration(unittest.TestCase):
    """A rep whose agent result shows Claude Code rejected an unrecognized
    slash command is a harness bug, not a task failure -- it must be
    discarded like the C1 arm-contamination guard, not scored as
    failed-verify."""

    def test_real_reported_excerpt_is_detected(self):
        # The literal signature from AI-442 batch-1's actual failure: all
        # 40 baseline reps died on this exact line before the prompt fix.
        result_text = "Unknown command: /floxify\n"
        self.assertTrue(
            run_floxify._detect_harness_misconfiguration(result_text)
        )

    def test_unknown_command_mid_output_is_still_detected(self):
        result_text = (
            "Some preamble text.\nUnknown command: /floxify\nMore output."
        )
        self.assertTrue(
            run_floxify._detect_harness_misconfiguration(result_text)
        )

    def test_normal_successful_output_is_not_flagged(self):
        result_text = "Wrote .flox/env/manifest.toml successfully."
        self.assertFalse(
            run_floxify._detect_harness_misconfiguration(result_text)
        )

    def test_none_result_text_does_not_raise(self):
        self.assertFalse(run_floxify._detect_harness_misconfiguration(None))

    def test_empty_result_text_is_not_flagged(self):
        self.assertFalse(run_floxify._detect_harness_misconfiguration(""))


class TestRunClaudeAgentArmIsolation(unittest.TestCase):
    """The design doc's own required test ("assert --arm baseline omits
    --plugin-dir from the argv and --arm skills includes it"), plus the
    AI-442 C1 fix on top: --setting-sources on both arms, and the
    baseline-arm runtime contamination guard."""

    def _mock_stream_proc(self, extra_events=""):
        result_event = json.dumps({
            "type": "result", "total_cost_usd": 0.01, "usage": {},
            "duration_ms": 100, "num_turns": 1, "result": "done",
        })
        stdout = extra_events + result_event + "\n"
        return MagicMock(returncode=0, stdout=stdout, stderr="")

    @patch("run_floxify.subprocess.run")
    def test_skills_arm_includes_plugin_dir(self, mock_run):
        mock_run.return_value = self._mock_stream_proc()
        run_floxify._run_claude_agent("prompt", "/some/skill/dir", arm="skills")
        cmd = mock_run.call_args.args[0]
        self.assertIn("--plugin-dir", cmd)
        self.assertIn("/some/skill/dir", cmd)

    @patch("run_floxify.subprocess.run")
    def test_baseline_arm_omits_plugin_dir(self, mock_run):
        mock_run.return_value = self._mock_stream_proc()
        run_floxify._run_claude_agent("prompt", "/some/skill/dir", arm="baseline")
        cmd = mock_run.call_args.args[0]
        self.assertNotIn("--plugin-dir", cmd)
        self.assertNotIn("/some/skill/dir", cmd)

    @patch("run_floxify.subprocess.run")
    def test_both_arms_pass_setting_sources_project_local(self, mock_run):
        mock_run.return_value = self._mock_stream_proc()
        for arm in ("skills", "baseline"):
            mock_run.reset_mock()
            run_floxify._run_claude_agent("prompt", "/some/skill/dir", arm=arm)
            cmd = mock_run.call_args.args[0]
            idx = cmd.index("--setting-sources")
            self.assertEqual(cmd[idx + 1], "project,local")

    @patch("run_floxify.subprocess.run")
    def test_contaminated_baseline_rep_is_discarded_as_arm_contamination(self, mock_run):
        # Real captured init event (contaminated) followed by a normal
        # result event -- the exact shape a leak would actually produce.
        init_line = json.dumps(REAL_CONTAMINATED_INIT_EVENT)
        mock_run.return_value = self._mock_stream_proc(extra_events=init_line + "\n")
        result_text, err, meta = run_floxify._run_claude_agent(
            "prompt", "/some/skill/dir", arm="baseline",
        )
        self.assertIsNone(result_text)
        self.assertIsNotNone(err)
        self.assertIn("arm contamination", err)
        self.assertEqual(meta, run_floxify.ZERO_META)

    @patch("run_floxify.subprocess.run")
    def test_contaminated_init_event_does_not_flag_the_skills_arm(self, mock_run):
        # The skills arm is SUPPOSED to have the plugin loaded -- the
        # same init event must not trip the guard there.
        init_line = json.dumps(REAL_CONTAMINATED_INIT_EVENT)
        mock_run.return_value = self._mock_stream_proc(extra_events=init_line + "\n")
        result_text, err, meta = run_floxify._run_claude_agent(
            "prompt", "/some/skill/dir", arm="skills",
        )
        self.assertIsNone(err)
        self.assertEqual(result_text, "done")

    @patch("run_floxify.subprocess.run")
    def test_clean_baseline_rep_is_not_flagged(self, mock_run):
        # A normal, uncontaminated stream (no init event at all in this
        # minimal mock) must not be mistaken for contamination.
        mock_run.return_value = self._mock_stream_proc()
        result_text, err, meta = run_floxify._run_claude_agent(
            "prompt", "/some/skill/dir", arm="baseline",
        )
        self.assertIsNone(err)
        self.assertEqual(result_text, "done")

    @patch("run_floxify.time.sleep")
    @patch("run_floxify.subprocess.run")
    def test_contamination_does_not_consume_a_retry_attempt(self, mock_run, mock_sleep):
        # A leak is a deterministic property of the environment, not a
        # transient flake -- retrying would just reproduce it. The call
        # must return immediately rather than looping through `retries`.
        init_line = json.dumps(REAL_CONTAMINATED_INIT_EVENT)
        mock_run.return_value = self._mock_stream_proc(extra_events=init_line + "\n")
        run_floxify._run_claude_agent(
            "prompt", "/some/skill/dir", arm="baseline", retries=3,
        )
        self.assertEqual(mock_run.call_count, 1)


class TestRunClaudeAgentHarnessMisconfigurationGuard(unittest.TestCase):
    """AI-442 batch-1: a stream whose result text carries the unrecognized-
    slash-command signature is a harness bug, not task data -- applies to
    either arm, unlike the baseline-only C1 contamination guard."""

    def _mock_stream_proc_with_result(self, result_text):
        result_event = json.dumps({
            "type": "result", "total_cost_usd": 0.01, "usage": {},
            "duration_ms": 100, "num_turns": 1, "result": result_text,
        })
        return MagicMock(returncode=0, stdout=result_event + "\n", stderr="")

    @patch("run_floxify.subprocess.run")
    def test_unknown_command_rep_is_discarded_as_harness_misconfiguration(
        self, mock_run
    ):
        # Real reported excerpt: every baseline rep in AI-442 batch-1 died
        # on this exact line before the arm-conditional prompt fix.
        mock_run.return_value = self._mock_stream_proc_with_result(
            "Unknown command: /floxify"
        )
        result_text, err, meta = run_floxify._run_claude_agent(
            "prompt", "/some/skill/dir", arm="baseline",
        )
        self.assertIsNone(result_text)
        self.assertIsNotNone(err)
        self.assertIn("harness misconfiguration", err)
        self.assertEqual(meta, run_floxify.ZERO_META)

    @patch("run_floxify.subprocess.run")
    def test_unknown_command_rep_is_flagged_on_the_skills_arm_too(
        self, mock_run
    ):
        # Not baseline-specific: a future regression that puts a wrong
        # slash command in the skills-arm prompt must be caught the same
        # way.
        mock_run.return_value = self._mock_stream_proc_with_result(
            "Unknown command: /floxify"
        )
        result_text, err, meta = run_floxify._run_claude_agent(
            "prompt", "/some/skill/dir", arm="skills",
        )
        self.assertIsNone(result_text)
        self.assertIn("harness misconfiguration", err)

    @patch("run_floxify.time.sleep")
    @patch("run_floxify.subprocess.run")
    def test_harness_misconfiguration_does_not_consume_a_retry_attempt(
        self, mock_run, mock_sleep
    ):
        # Deterministic property of the prompt/harness mismatch, not a
        # transient flake -- retrying would just reproduce it.
        mock_run.return_value = self._mock_stream_proc_with_result(
            "Unknown command: /floxify"
        )
        run_floxify._run_claude_agent(
            "prompt", "/some/skill/dir", arm="baseline", retries=3,
        )
        self.assertEqual(mock_run.call_count, 1)

    @patch("run_floxify.subprocess.run")
    def test_normal_result_is_not_flagged(self, mock_run):
        mock_run.return_value = self._mock_stream_proc_with_result("done")
        result_text, err, meta = run_floxify._run_claude_agent(
            "prompt", "/some/skill/dir", arm="baseline",
        )
        self.assertIsNone(err)
        self.assertEqual(result_text, "done")


# ---------------------------------------------------------------------------
# AI-442 Q2: verified-anchor strength -- services where declared,
# activation-only elsewhere
# ---------------------------------------------------------------------------

# The real verify.py, loaded once for this module -- exercises
# `_probe_service`'s actual `parse_manifest`/`matching_service_names`
# calls rather than a hand-rolled stand-in (same discipline
# test_real_world.py's `_VERIFY_MOD` module load uses for the AI-447 probe).
_, _VERIFY_MOD = run_floxify._load_detect_and_verify(run_floxify.DEFAULT_SKILL_DIR)


class TestProbeService(unittest.TestCase):
    @patch("run_floxify.shutil.which", return_value=None)
    def test_flox_absent_is_skipped(self, _which):
        ok, skipped, notes = run_floxify._probe_service(
            "/tmp/x", "postgres", "[install]\n", _VERIFY_MOD,
        )
        self.assertIsNone(ok)
        self.assertTrue(skipped)
        self.assertIn("flox", notes.lower())

    @patch("run_floxify.shutil.which", return_value="/usr/bin/flox")
    def test_no_probe_command_for_kind_is_skipped(self, _which):
        ok, skipped, notes = run_floxify._probe_service(
            "/tmp/x", "clickhouse", "[install]\n", _VERIFY_MOD,
        )
        self.assertIsNone(ok)
        self.assertTrue(skipped)
        self.assertIn("clickhouse", notes)

    @patch("run_floxify.shutil.which", return_value="/usr/bin/flox")
    def test_manifest_that_does_not_parse_is_skipped(self, _which):
        ok, skipped, notes = run_floxify._probe_service(
            "/tmp/x", "postgres", "this is [ not valid toml", _VERIFY_MOD,
        )
        self.assertIsNone(ok)
        self.assertTrue(skipped)

    @patch("run_floxify.shutil.which", return_value="/usr/bin/flox")
    def test_no_matching_service_entry_is_not_ok_and_not_skipped(self, _which):
        # AI-442 Q2: a manifest that never wired the declared service
        # (no [services.postgres]) is a genuine failure, not a skip --
        # activation succeeding here must not read as "verified".
        manifest = '[install]\npostgresql.pkg-path = "postgresql"\n'
        ok, skipped, notes = run_floxify._probe_service(
            "/tmp/x", "postgres", manifest, _VERIFY_MOD,
        )
        self.assertFalse(ok)
        self.assertFalse(skipped)
        self.assertIn("not wired", notes)

    @patch("run_floxify.subprocess.run")
    @patch("run_floxify.shutil.which", return_value="/usr/bin/flox")
    def test_matching_service_and_probe_confirms_connectivity(self, _which, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout=run_floxify._SERVICE_PROBE_OK + "\n", stderr="",
        )
        manifest = (
            '[install]\npostgresql.pkg-path = "postgresql"\n\n'
            '[services.postgres]\ncommand = "postgres"\n'
        )
        ok, skipped, notes = run_floxify._probe_service(
            "/tmp/x", "postgres", manifest, _VERIFY_MOD,
        )
        self.assertTrue(ok)
        self.assertFalse(skipped)

    @patch("run_floxify.subprocess.run")
    @patch("run_floxify.shutil.which", return_value="/usr/bin/flox")
    def test_matching_service_never_answers(self, _which, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout=run_floxify._SERVICE_PROBE_DEAD + "\n", stderr="",
        )
        manifest = (
            '[install]\npostgresql.pkg-path = "postgresql"\n\n'
            '[services.postgres]\ncommand = "postgres"\n'
        )
        ok, skipped, notes = run_floxify._probe_service(
            "/tmp/x", "postgres", manifest, _VERIFY_MOD,
        )
        self.assertFalse(ok)
        self.assertFalse(skipped)
        self.assertIn("never answered", notes)

    @patch("run_floxify.subprocess.run")
    @patch("run_floxify.shutil.which", return_value="/usr/bin/flox")
    def test_probe_timeout_is_a_real_failure_not_a_skip(self, _which, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="flox", timeout=300)
        manifest = (
            '[install]\npostgresql.pkg-path = "postgresql"\n\n'
            '[services.postgres]\ncommand = "postgres"\n'
        )
        ok, skipped, notes = run_floxify._probe_service(
            "/tmp/x", "postgres", manifest, _VERIFY_MOD, timeout=300,
        )
        self.assertFalse(ok)
        self.assertFalse(skipped)
        self.assertIn("TIMEOUT", notes)

    @patch("run_floxify.subprocess.run")
    @patch("run_floxify.shutil.which", return_value="/usr/bin/flox")
    def test_probe_script_never_ran_is_skipped(self, _which, mock_run):
        # Neither sentinel present -- flox itself errored before the
        # polling script ever executed. Not a verdict on the manifest.
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="some flox activation error",
        )
        manifest = (
            '[install]\npostgresql.pkg-path = "postgresql"\n\n'
            '[services.postgres]\ncommand = "postgres"\n'
        )
        ok, skipped, notes = run_floxify._probe_service(
            "/tmp/x", "postgres", manifest, _VERIFY_MOD,
        )
        self.assertIsNone(ok)
        self.assertTrue(skipped)


class TestExpectedServiceKind(unittest.TestCase):
    def test_node_postgres_shape_returns_postgres(self):
        task = {"checks": ["manifest_created", "valid_toml", "has_install_section",
                           "has_services_section", "no_abs_paths",
                           "no_fake_install_url", "pins_node_20", "pins_postgres"]}
        self.assertEqual(run_floxify._expected_service_kind(task), "postgres")

    def test_no_has_services_section_returns_none(self):
        # ruby/python-uv/go-mod/rust-cargo shape -- no service declared.
        task = {"checks": ["manifest_created", "valid_toml", "has_install_section",
                           "no_abs_paths", "no_fake_install_url", "pins_ruby"]}
        self.assertIsNone(run_floxify._expected_service_kind(task))

    def test_has_services_section_without_a_known_pins_kind_returns_none(self):
        task = {"checks": ["has_services_section", "no_abs_paths"]}
        self.assertIsNone(run_floxify._expected_service_kind(task))

    def test_pins_postgres_without_has_services_section_returns_none(self):
        # has_services_section is the gate -- a pins_<kind> check alone
        # (unusual shape, but must not accidentally trigger the stronger
        # anchor) does not imply a declared service.
        task = {"checks": ["pins_postgres"]}
        self.assertIsNone(run_floxify._expected_service_kind(task))

    def test_missing_checks_key_returns_none(self):
        self.assertIsNone(run_floxify._expected_service_kind({}))


class TestComputeVerification(unittest.TestCase):
    """AI-442 Q2's anchor rule as a pure, directly testable decision --
    the four terminal dispositions from the design doc's censoring table."""

    def test_activation_only_success_is_verified(self):
        verified, method, disposition = run_floxify._compute_verification(
            act_ok=True, act_skipped=False, service_kind=None, service_probe=None,
        )
        self.assertTrue(verified)
        self.assertEqual(method, "activation")
        self.assertEqual(disposition, "verified")

    def test_activation_only_failure_is_failed_verify(self):
        verified, method, disposition = run_floxify._compute_verification(
            act_ok=False, act_skipped=False, service_kind=None, service_probe=None,
        )
        self.assertFalse(verified)
        self.assertEqual(method, "activation")
        self.assertEqual(disposition, "failed-verify")

    def test_activation_skipped_is_unverifiable_env(self):
        verified, method, disposition = run_floxify._compute_verification(
            act_ok=None, act_skipped=True, service_kind=None, service_probe=None,
        )
        self.assertFalse(verified)
        self.assertEqual(method, "activation")
        self.assertEqual(disposition, "unverifiable-env")

    def test_service_declared_but_activation_skipped_is_unverifiable_env(self):
        verified, method, disposition = run_floxify._compute_verification(
            act_ok=None, act_skipped=True, service_kind="postgres", service_probe=None,
        )
        self.assertFalse(verified)
        self.assertEqual(method, "services")
        self.assertEqual(disposition, "unverifiable-env")

    def test_service_declared_but_activation_itself_failed_is_failed_verify(self):
        # Activation never succeeded -- the service was never even
        # attempted (service_probe is None, matching process_task's own
        # gating), but the anchor demanded is still "services".
        verified, method, disposition = run_floxify._compute_verification(
            act_ok=False, act_skipped=False, service_kind="postgres", service_probe=None,
        )
        self.assertFalse(verified)
        self.assertEqual(method, "services")
        self.assertEqual(disposition, "failed-verify")

    def test_service_declared_and_probe_confirms_connectivity_is_verified(self):
        verified, method, disposition = run_floxify._compute_verification(
            act_ok=True, act_skipped=False, service_kind="postgres",
            service_probe=(True, False, "connectivity confirmed"),
        )
        self.assertTrue(verified)
        self.assertEqual(method, "services")
        self.assertEqual(disposition, "verified")

    def test_service_declared_but_never_wired_is_failed_verify_not_verified(self):
        # AI-442 Q2's whole point: activation succeeding while the
        # declared service was never wired must NOT read as verified.
        verified, method, disposition = run_floxify._compute_verification(
            act_ok=True, act_skipped=False, service_kind="postgres",
            service_probe=(False, False, "no [services.*] entry matches"),
        )
        self.assertFalse(verified)
        self.assertEqual(method, "services")
        self.assertEqual(disposition, "failed-verify")

    def test_service_probe_skipped_is_unverifiable_env(self):
        # flox vanished mid-probe / no probe command for this kind --
        # not a verdict on the manifest.
        verified, method, disposition = run_floxify._compute_verification(
            act_ok=True, act_skipped=False, service_kind="postgres",
            service_probe=(None, True, "no connectivity probe for 'clickhouse'"),
        )
        self.assertFalse(verified)
        self.assertEqual(method, "services")
        self.assertEqual(disposition, "unverifiable-env")


# ---------------------------------------------------------------------------
# AI-442 §1.1 / Q5: censored efficiency aggregation -- the highest-value
# tests in the whole change (design doc: "the censoring logic is where a
# subtle averaging bug would silently corrupt the headline number").
# ---------------------------------------------------------------------------

def _rep(disposition, turns=5, tool_total=3, flox_search=1, flox_show=1,
        output_tokens=1000, cache_read_tokens=5000, cost=0.1):
    return {
        "id": "x", "arm": "skills", "rep": 1,
        "terminal_disposition": disposition,
        "num_turns": {"agent": turns, "judge": 1},
        "tool_calls": {"agent": {"total": tool_total, "flox_search": flox_search,
                                 "flox_show": flox_show}},
        "usage": {"agent": {"output_tokens": output_tokens,
                            "cache_read_input_tokens": cache_read_tokens},
                 "judge": {}},
        "cost": {"agent_usd": cost, "judge_usd": 0.01, "total_usd": cost + 0.01},
    }


class TestEfficiencySummary(unittest.TestCase):
    def test_decision_verification_all_failed_verify_gives_zero_rate_and_empty_cost(self):
        # The design doc's own acceptance bar: a giving-up arm (every rep
        # failed-verify) must produce verify_rate = 0 and an EMPTY
        # cost_to_verify (n=0) -- never a deceptively low mean computed
        # from reps that spent tokens and never arrived.
        results = [_rep("failed-verify", cost=0.3) for _ in range(5)]
        summary = run_floxify._efficiency_summary(results)
        self.assertEqual(summary["verify_rate"], 0.0)
        self.assertEqual(summary["cost_to_verify"], {"median_usd": None, "n": 0})
        self.assertEqual(summary["unverified_spend"]["n"], 5)
        self.assertIsNotNone(summary["unverified_spend"]["median_usd"])

    def test_unverifiable_env_and_agent_error_are_dropped_from_verify_rate(self):
        results = [
            _rep("verified"),
            _rep("unverifiable-env"),
            _rep("unverifiable-env"),
            _rep("agent-error"),
        ]
        summary = run_floxify._efficiency_summary(results)
        # 1 verified / (1 verified + 0 failed) = 1.0, NOT 1/4 = 0.25 --
        # the two dropped dispositions must not appear in the denominator.
        self.assertEqual(summary["verify_rate"], 1.0)
        self.assertEqual(summary["env_skipped"], 2)
        self.assertEqual(summary["agent_errors"], 1)

    def test_cost_to_verify_never_includes_failed_verify_reps(self):
        results = [
            _rep("verified", cost=1.0),
            _rep("failed-verify", cost=99.0),  # deliberately huge outlier
        ]
        summary = run_floxify._efficiency_summary(results)
        self.assertEqual(summary["cost_to_verify"]["n"], 1)
        self.assertEqual(summary["cost_to_verify"]["median_usd"], 1.01)  # 1.0 + judge 0.01
        # The huge failed-verify cost must land ONLY in unverified_spend.
        self.assertEqual(summary["unverified_spend"]["n"], 1)
        self.assertAlmostEqual(summary["unverified_spend"]["median_usd"], 99.01)

    def test_verify_rate_reflects_mixed_verified_and_failed(self):
        results = [_rep("verified"), _rep("verified"), _rep("failed-verify")]
        summary = run_floxify._efficiency_summary(results)
        self.assertAlmostEqual(summary["verify_rate"], 2 / 3, places=3)

    def test_median_and_iqr_on_a_five_rep_sample(self):
        results = [_rep("verified", turns=t) for t in (5, 7, 9, 11, 13)]
        summary = run_floxify._efficiency_summary(results)
        self.assertEqual(summary["turns_to_verify"]["n"], 5)
        self.assertEqual(summary["turns_to_verify"]["median"], 9)
        self.assertEqual(summary["turns_to_verify"]["p25"], 7)
        self.assertEqual(summary["turns_to_verify"]["p75"], 11)

    def test_tool_calls_distribution_is_computed_separately_from_turns(self):
        results = [
            _rep("verified", turns=10, tool_total=2, flox_search=1, flox_show=0),
            _rep("verified", turns=12, tool_total=6, flox_search=3, flox_show=1),
        ]
        summary = run_floxify._efficiency_summary(results)
        self.assertEqual(summary["tool_calls_to_verify"]["median_total"], 4)
        self.assertEqual(summary["tool_calls_to_verify"]["median_flox_search"], 2)
        self.assertEqual(summary["tool_calls_to_verify"]["median_flox_show"], 0.5)

    def test_empty_results_returns_zero_reps_and_none_rate(self):
        summary = run_floxify._efficiency_summary([])
        self.assertEqual(summary["reps"], 0)
        self.assertIsNone(summary["verify_rate"])
        self.assertEqual(summary["cost_to_verify"], {"median_usd": None, "n": 0})

    def test_single_verified_rep_does_not_raise_on_percentiles(self):
        summary = run_floxify._efficiency_summary([_rep("verified", turns=8)])
        self.assertEqual(summary["turns_to_verify"]["n"], 1)
        self.assertEqual(summary["turns_to_verify"]["median"], 8)
        self.assertEqual(summary["turns_to_verify"]["p25"], 8)
        self.assertEqual(summary["turns_to_verify"]["p75"], 8)

    def test_unrecognized_disposition_is_counted_not_silently_dropped(self):
        results = [_rep("verified"), {"id": "x", "terminal_disposition": "mystery"}]
        summary = run_floxify._efficiency_summary(results)
        self.assertEqual(summary["other_disposition"], 1)
        self.assertEqual(summary["reps"], 2)

    def test_missing_disposition_key_is_counted_as_other(self):
        results = [_rep("verified"), {"id": "x"}]
        summary = run_floxify._efficiency_summary(results)
        self.assertEqual(summary["other_disposition"], 1)


class TestMedianAndPercentile(unittest.TestCase):
    def test_median_odd_count(self):
        self.assertEqual(run_floxify._median([1, 3, 2]), 2)

    def test_median_even_count(self):
        self.assertEqual(run_floxify._median([1, 2, 3, 4]), 2.5)

    def test_median_empty_is_none(self):
        self.assertIsNone(run_floxify._median([]))

    def test_percentile_empty_is_none(self):
        self.assertIsNone(run_floxify._percentile([], 0.25))

    def test_percentile_single_value(self):
        self.assertEqual(run_floxify._percentile([7], 0.25), 7)
        self.assertEqual(run_floxify._percentile([7], 0.75), 7)

    def test_percentile_unsorted_input(self):
        self.assertEqual(run_floxify._percentile([9, 1, 5], 0.5), 5)


if __name__ == "__main__":
    unittest.main()
