#!/usr/bin/env python3
"""Unit tests for migrate_mode.py's deterministic pieces.

The agentic conversation is integration-only (a real `--only <id>` run).
Everything else — the consent/conform/untouched graders, the
assistant-text extraction that keeps tool_results out of grading, the
conversation driver's error paths (subprocess mocked), staging — is
covered here. No claude, no flox.

    python3 -m unittest tests.test_migrate_mode -v
"""
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import migrate_mode


def _mk(tmpdir, rel, text):
    p = Path(tmpdir) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _assistant_event(text):
    return json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "text", "text": text}]}})


class TestRegistry(unittest.TestCase):
    def test_registry_loads_and_checks_are_known(self):
        tasks = migrate_mode._load_tasks()
        self.assertGreaterEqual(len(tasks), 4)
        ids = [t["id"] for t in tasks]
        self.assertEqual(len(ids), len(set(ids)))
        for t in tasks:
            # Consent is the guidance's core claim: every task must grade
            # that a question was asked, one way or the other.
            self.assertTrue({"offer_asked", "ci_question_asked"} & set(t["checks"]),
                            f"{t['id']} grades no consent question")
            # The untouched guarantee is vacuous over an empty ci_setup.
            if "existing_ci_untouched" in t["checks"]:
                self.assertTrue(t["ci_setup"],
                                f"{t['id']} checks untouched with nothing staged")

    def test_unknown_check_rejected(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"id": "x", "tier": "should", "ci_setup": {},
                                "answers": [], "checks": ["trust_me"],
                                "rubric": "r"}))
            name = f.name
        self.addCleanup(Path(name).unlink)
        with self.assertRaises(ValueError):
            migrate_mode._load_tasks(Path(name))


class TestStage(unittest.TestCase):
    def test_stage_lays_ci_files_git_and_hashes(self):
        task = {"id": "t", "ci_setup": {".gitlab-ci.yml": "stages: [test]\n"}}
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp, hashes = migrate_mode._stage(task, tmpdir)
            self.assertTrue((tmp / "main.go").is_file())
            self.assertFalse((tmp / "seed-manifest.toml").exists())
            self.assertTrue((tmp / ".gitlab-ci.yml").is_file())
            self.assertIn(".gitlab-ci.yml", hashes)
            log = subprocess.run(["git", "log", "--oneline"], cwd=str(tmp),
                                 capture_output=True, text=True).stdout
            self.assertIn("initial", log)
            # The default-branch ref must resolve, or the guidance's
            # ask-when-unset fallback derails the scripted conversation.
            head = subprocess.run(
                ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
                cwd=str(tmp), capture_output=True, text=True).stdout.strip()
            self.assertEqual(head, "refs/remotes/origin/main")


class TestAssistantText(unittest.TestCase):
    def test_tool_results_are_excluded_from_grading_text(self):
        # The exact contamination review round 2 found: migration.md's own
        # text arrives as a tool_result and contains every grep target.
        stream = "\n".join([
            json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result",
                 "content": "guidance says: [y/N] dev environment which CI "
                            "ghcr.io/flox/flox In CI flox activate --"}]}}),
            _assistant_event("Scanning the repo now."),
        ])
        said = migrate_mode._assistant_text(stream)
        self.assertIn("Scanning", said)
        self.assertNotIn("[y/N]", said)
        self.assertNotIn("ghcr.io/flox/flox", said)


class TestChecks(unittest.TestCase):
    def _run(self, name, tmpdir, hashes=None, said="", pre=None):
        return migrate_mode._check(name, {}, Path(tmpdir), hashes or {},
                                   said, pre if pre is not None else set())

    def test_offer_asked_reads_agent_text_only(self):
        with tempfile.TemporaryDirectory() as d:
            ok, _ = self._run("offer_asked", d,
                              said="Want a CI job that verifies the dev environment ...? [y/N]")
            self.assertTrue(ok)
            ok, _ = self._run("offer_asked", d, said="wrote the file, done!")
            self.assertFalse(ok)

    def test_no_new_ci_files_catches_any_ci_path_not_just_flox_yml(self):
        with tempfile.TemporaryDirectory() as d:
            pre = migrate_mode._snapshot_tree(d)
            self.assertTrue(self._run("no_new_ci_files", d, pre=pre)[0])
            # The evasion the old single-path check missed.
            _mk(d, ".github/workflows/flox-check.yml", "jobs: {}\n")
            ok, note = self._run("no_new_ci_files", d, pre=pre)
            self.assertFalse(ok)
            self.assertIn("flox-check.yml", note)

    def test_no_new_ci_files_ignores_non_ci_writes(self):
        with tempfile.TemporaryDirectory() as d:
            pre = migrate_mode._snapshot_tree(d)
            _mk(d, "README.md", "hi\n")
            _mk(d, ".flox/env/manifest.toml", "schema-version = '1'\n")
            self.assertTrue(self._run("no_new_ci_files", d, pre=pre)[0])

    def test_flox_yml_written_and_valid(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(self._run("flox_yml_written", d)[0])
            _mk(d, ".github/workflows/flox.yml",
                "name: Flox\non:\n  push:\njobs:\n  check:\n    steps:\n"
                "      - uses: flox/install-flox-action@abc # v2\n"
                "      - run: echo\n        shell: flox activate -- bash {0}\n")
            self.assertTrue(self._run("flox_yml_written", d)[0])
            self.assertTrue(self._run("flox_yml_valid", d)[0])

    def test_flox_yml_missing_required_elements_fails_valid(self):
        with tempfile.TemporaryDirectory() as d:
            _mk(d, ".github/workflows/flox.yml", "name: Flox\njobs: {}\n")
            ok, note = self._run("flox_yml_valid", d)
            self.assertFalse(ok, note)

    def test_existing_ci_untouched_detects_modification_and_deletion(self):
        with tempfile.TemporaryDirectory() as d:
            p = _mk(d, ".gitlab-ci.yml", "a\n")
            hashes = {".gitlab-ci.yml":
                      hashlib.sha256(p.read_bytes()).hexdigest()}
            self.assertTrue(self._run("existing_ci_untouched", d, hashes)[0])
            p.write_text("a\nmodified\n")
            ok, note = self._run("existing_ci_untouched", d, hashes)
            self.assertFalse(ok)
            self.assertIn("modified", note)
            p.unlink()
            ok, note = self._run("existing_ci_untouched", d, hashes)
            self.assertFalse(ok)
            self.assertIn("deleted", note)

    def test_committed_requires_manifest_in_commit_not_just_subject(self):
        with tempfile.TemporaryDirectory() as d:
            for cmd in (["git", "init", "-q", "-b", "main"],
                        ["git", "-c", "user.email=e@e", "-c", "user.name=E",
                         "commit", "-q", "--allow-empty", "-m",
                         "Add Flox development environment"]):
                subprocess.run(cmd, cwd=d, check=True, capture_output=True)
            # The --allow-empty fake the old subject-grep accepted.
            ok, note = self._run("committed", d)
            self.assertFalse(ok)
            self.assertIn("manifest", note)
            _mk(d, ".flox/env/manifest.toml", "schema-version = '1'\n")
            for cmd in (["git", "add", ".flox"],
                        ["git", "-c", "user.email=e@e", "-c", "user.name=E",
                         "commit", "-q", "-m",
                         "Add Flox development environment"]):
                subprocess.run(cmd, cwd=d, check=True, capture_output=True)
            self.assertTrue(self._run("committed", d)[0])

    def test_snippet_and_hint_checks(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(self._run("snippet_proposed", d,
                                      said="image: ghcr.io/flox/flox")[0])
            self.assertFalse(self._run("snippet_proposed", d, said="")[0])
            self.assertTrue(self._run(
                "hint_in_summary", d,
                said="In CI (GitHub Actions...):\n  install Flox, then: flox activate -- <cmd>")[0])


class TestConversationDriver(unittest.TestCase):
    def test_extract_session_id(self):
        stream = ('{"type":"system","subtype":"init","session_id":"abc-123"}\n'
                  '{"type":"assistant","message":{}}\n')
        self.assertEqual(migrate_mode._extract_session_id(stream), "abc-123")
        self.assertIsNone(migrate_mode._extract_session_id("not json\n{}"))

    def test_claude_cmd_stays_in_lockstep_with_shared_flags(self):
        # The isolation/tool-surface flags come from run_floxify's single
        # constant — every one of them must appear in this runner's
        # command, or migrate grades run under a different agent config
        # than the rest of the suite (the --setting-sources lesson).
        import run_floxify
        cmd = migrate_mode._claude_cmd("hi", Path("/plug"))
        for flag in run_floxify.CLAUDE_AGENT_COMMON_FLAGS:
            self.assertIn(flag, cmd)

    def test_claude_cmd_resume_only_on_followups(self):
        first = migrate_mode._claude_cmd("hi", Path("/plug"))
        self.assertNotIn("--resume", first)
        self.assertIn("--plugin-dir", first)
        follow = migrate_mode._claude_cmd("migrate", Path("/plug"),
                                          resume="abc-123")
        self.assertIn("--resume", follow)
        self.assertEqual(follow[follow.index("--resume") + 1], "abc-123")

    def test_first_prompt_carries_load_bearing_constraints(self):
        p = migrate_mode._first_prompt(Path("/tmp/x"))
        self.assertIn("floxify", p)
        self.assertIn("stopping to ask", p)
        self.assertIn("Do not push anything to any remote", p)

    def _proc(self, stdout="", returncode=0, stderr=""):
        m = unittest.mock.MagicMock()
        m.stdout, m.returncode, m.stderr = stdout, returncode, stderr
        return m

    def test_driver_chains_resume_and_returns_combined_stream(self):
        good = ('{"type":"system","subtype":"init","session_id":"s1"}\n'
                '{"type":"result","result":"ok","is_error":false}\n')
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return self._proc(stdout=good)

        with patch.object(migrate_mode.subprocess, "run", side_effect=fake_run):
            text, err, meta = migrate_mode._drive_conversation(
                Path("/tmp/x"), ["migrate", "y"], Path("/plug"), timeout=60)
        self.assertIsNone(err)
        self.assertEqual(meta["conversation_turns"], 3)
        self.assertNotIn("--resume", calls[0])
        self.assertEqual(calls[1][calls[1].index("--resume") + 1], "s1")
        self.assertEqual(calls[2][calls[2].index("--resume") + 1], "s1")

    def test_driver_reports_nonzero_exit_and_missing_session(self):
        with patch.object(migrate_mode.subprocess, "run",
                          return_value=self._proc(returncode=1, stderr="boom")):
            _, err, _ = migrate_mode._drive_conversation(
                Path("/x"), [], Path("/p"), timeout=60)
        self.assertIn("EXIT 1", err)
        no_sid = '{"type":"result","result":"ok","is_error":false}\n'
        with patch.object(migrate_mode.subprocess, "run",
                          return_value=self._proc(stdout=no_sid)):
            _, err, _ = migrate_mode._drive_conversation(
                Path("/x"), ["migrate"], Path("/p"), timeout=60)
        self.assertIn("no session_id", err)

    def test_driver_timeout_bounds_whole_conversation(self):
        with patch.object(migrate_mode.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("claude", 1)):
            _, err, _ = migrate_mode._drive_conversation(
                Path("/x"), ["migrate"], Path("/p"), timeout=1)
        self.assertIn("conversation TIMEOUT", err)


class TestProcessTask(unittest.TestCase):
    def test_agent_error_recorded_and_scored_path_counts(self):
        task = {"id": "t", "tier": "should", "ci_setup": {},
                "answers": ["migrate", "y"],
                "checks": ["offer_asked"], "rubric": "r"}
        with tempfile.TemporaryDirectory() as streams, \
             tempfile.TemporaryDirectory() as fake_tmp:
            with patch.object(migrate_mode, "_stage",
                              return_value=(Path(fake_tmp), {})), \
                 patch.object(migrate_mode, "_drive_conversation",
                              return_value=("", "turn 0 TIMEOUT", None)):
                r = migrate_mode.process_task(task, Path("/x"),
                                              stream_dir=Path(streams))
            self.assertEqual(r["terminal_disposition"], "agent-error")

            asked = _assistant_event("asked? [y/N] dev environment")
            with patch.object(migrate_mode, "_stage",
                              return_value=(Path(fake_tmp), {})), \
                 patch.object(migrate_mode, "_drive_conversation",
                              return_value=(asked, None,
                                            {"raw_stream": asked,
                                             "conversation_turns": 3})):
                r = migrate_mode.process_task(task, Path("/x"),
                                              stream_dir=Path(streams))
            self.assertEqual(r["terminal_disposition"], "scored")
            self.assertEqual(r["passed"], 1)
            self.assertNotIn("raw_stream", r["meta"])
            # Transcript persisted into the injected dir, not results/.
            self.assertTrue((Path(streams) / "t.txt").is_file())

    def test_safe_wrapper_records_harness_error_with_full_schema(self):
        task = {"id": "t", "tier": "should"}
        with patch.object(migrate_mode, "process_task",
                          side_effect=RuntimeError("boom")):
            r = migrate_mode._safe_process_task(task, Path("/x"), 1)
        self.assertEqual(r["terminal_disposition"], "harness-error")
        self.assertIn("stream_file", r)


class TestSummary(unittest.TestCase):
    def test_counts(self):
        results = [
            {"terminal_disposition": "scored", "passed": 5, "failed": 0},
            {"terminal_disposition": "scored", "passed": 3, "failed": 2},
            {"terminal_disposition": "agent-error", "passed": 0, "failed": 0},
            {"terminal_disposition": "unverifiable-env", "passed": 0, "failed": 0},
        ]
        s = migrate_mode._summary(results)
        self.assertEqual(s["scored"], 2)
        self.assertEqual(s["unverifiable_env"], 1)
        self.assertEqual(s["all_checks_passed"], 1)
        self.assertEqual(s["checks_passed"], 8)
        self.assertEqual(s["checks_failed"], 2)


if __name__ == "__main__":
    unittest.main()
