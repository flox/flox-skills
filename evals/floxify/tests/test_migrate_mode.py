#!/usr/bin/env python3
"""Unit tests for migrate_mode.py's deterministic pieces.

The agentic run is integration-only (a real `--only <id>` run). The checks
themselves — the consent/conform/untouched graders — are pure functions
over a worktree and a transcript string, so they are fully covered here
with fabricated inputs. No claude, no flox.

    python3 -m unittest tests.test_migrate_mode -v
"""
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


class TestRegistry(unittest.TestCase):
    def test_registry_loads_and_checks_are_known(self):
        tasks = migrate_mode._load_tasks()
        self.assertGreaterEqual(len(tasks), 4)
        ids = [t["id"] for t in tasks]
        self.assertEqual(len(ids), len(set(ids)))
        # Consent is the guidance's core claim: every task must grade that
        # a question was asked, one way or the other.
        for t in tasks:
            self.assertTrue({"offer_asked", "ci_question_asked"} & set(t["checks"]),
                            f"{t['id']} grades no consent question")

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


class TestChecks(unittest.TestCase):
    def _run(self, name, tmpdir, hashes=None, stream=""):
        return migrate_mode._check(name, {}, Path(tmpdir), hashes or {},
                                   stream, "")

    def test_offer_asked(self):
        with tempfile.TemporaryDirectory() as d:
            ok, _ = self._run("offer_asked", d,
                              stream="Want a CI job that verifies the dev environment ...? [y/N]")
            self.assertTrue(ok)
            ok, _ = self._run("offer_asked", d, stream="wrote the file, done!")
            self.assertFalse(ok)

    def test_flox_yml_written_and_valid(self):
        with tempfile.TemporaryDirectory() as d:
            ok, _ = self._run("flox_yml_written", d)
            self.assertFalse(ok)
            _mk(d, ".github/workflows/flox.yml",
                "name: Flox\non:\n  push:\njobs:\n  check:\n    steps:\n"
                "      - uses: flox/install-flox-action@abc # v2\n"
                "      - run: echo\n        shell: flox activate -- bash {0}\n")
            self.assertTrue(self._run("flox_yml_written", d)[0])
            self.assertTrue(self._run("flox_yml_valid", d)[0])
            self.assertFalse(self._run("no_flox_yml", d)[0])

    def test_flox_yml_missing_required_elements_fails_valid(self):
        with tempfile.TemporaryDirectory() as d:
            _mk(d, ".github/workflows/flox.yml", "name: Flox\njobs: {}\n")
            ok, note = self._run("flox_yml_valid", d)
            self.assertFalse(ok, note)

    def test_existing_ci_untouched_detects_modification_and_deletion(self):
        import hashlib
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

    def test_snippet_and_hint_and_commit_checks(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(self._run("snippet_proposed", d,
                                      stream="image: ghcr.io/flox/flox")[0])
            self.assertFalse(self._run("snippet_proposed", d, stream="")[0])
            self.assertTrue(self._run(
                "hint_in_summary", d,
                stream="In CI (GitHub Actions...):\n  install Flox, then: flox activate -- <cmd>")[0])
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=d, check=True)
            subprocess.run(["git", "-c", "user.email=e@e", "-c", "user.name=E",
                            "commit", "-q", "--allow-empty", "-m",
                            "Add Flox development environment"], cwd=d, check=True)
            self.assertTrue(self._run("committed", d)[0])


class TestConversationDriver(unittest.TestCase):
    def test_extract_session_id(self):
        stream = ('{"type":"system","subtype":"init","session_id":"abc-123"}\n'
                  '{"type":"assistant","message":{}}\n')
        self.assertEqual(migrate_mode._extract_session_id(stream), "abc-123")
        self.assertIsNone(migrate_mode._extract_session_id("not json\n{}"))

    def test_claude_cmd_resume_only_on_followups(self):
        first = migrate_mode._claude_cmd("hi", Path("/plug"))
        self.assertNotIn("--resume", first)
        self.assertIn("--plugin-dir", first)
        follow = migrate_mode._claude_cmd("migrate", Path("/plug"),
                                          resume="abc-123")
        self.assertIn("--resume", follow)
        self.assertEqual(follow[follow.index("--resume") + 1], "abc-123")


class TestProcessTask(unittest.TestCase):
    def test_agent_error_recorded_and_scored_path_counts(self):
        task = {"id": "t", "tier": "should", "ci_setup": {},
                "answers": ["migrate", "y"],
                "checks": ["offer_asked"], "rubric": "r"}
        with patch.object(migrate_mode, "_stage",
                          return_value=(Path("/nonexistent"), {})), \
             patch.object(migrate_mode, "_drive_conversation",
                          return_value=("", "turn 0 TIMEOUT after 1s", None)):
            r = migrate_mode.process_task(task, Path("/x"))
        self.assertEqual(r["terminal_disposition"], "agent-error")

        with patch.object(migrate_mode, "_stage",
                          return_value=(Path("/nonexistent"), {})), \
             patch.object(migrate_mode, "_drive_conversation",
                          return_value=("asked? [y/N] dev environment", None,
                                        {"raw_stream": "asked? [y/N] dev environment",
                                         "conversation_turns": 3})):
            r = migrate_mode.process_task(task, Path("/x"))
        self.assertEqual(r["terminal_disposition"], "scored")
        self.assertEqual(r["passed"], 1)
        self.assertNotIn("raw_stream", r["meta"])

    def test_safe_wrapper_records_harness_error(self):
        task = {"id": "t", "tier": "should"}
        with patch.object(migrate_mode, "process_task",
                          side_effect=RuntimeError("boom")):
            r = migrate_mode._safe_process_task(task, Path("/x"), 1)
        self.assertEqual(r["terminal_disposition"], "harness-error")


class TestSummary(unittest.TestCase):
    def test_counts(self):
        results = [
            {"terminal_disposition": "scored", "passed": 5, "failed": 0},
            {"terminal_disposition": "scored", "passed": 3, "failed": 2},
            {"terminal_disposition": "agent-error", "passed": 0, "failed": 0},
        ]
        s = migrate_mode._summary(results)
        self.assertEqual(s["scored"], 2)
        self.assertEqual(s["all_checks_passed"], 1)
        self.assertEqual(s["checks_passed"], 8)
        self.assertEqual(s["checks_failed"], 2)


if __name__ == "__main__":
    unittest.main()
