#!/usr/bin/env python3
"""Unit tests for build_step.py's deterministic pieces.

The real agentic run, seed activation, and independent `flox build` are
integration-only (exercised by a real `--only <id>` run); `process_task`'s
disposition ladder IS covered here, with every subprocess boundary mocked,
the way the sibling suites test their orchestrators. Everything here is
pure logic over the local filesystem — no claude, no flox — so it is fast
and safe to gate on.

    python3 -m unittest tests.test_build_step -v
"""
import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import build_step


class TestRegistry(unittest.TestCase):
    """build.jsonl is a checked-in registry: every entry must be complete,
    and every fixture and seed manifest it names must exist and parse. A
    registry typo should fail HERE, not thirty minutes into an agent run."""

    def test_registry_loads_and_validates(self):
        tasks = build_step._load_tasks()
        self.assertGreaterEqual(len(tasks), 4)
        ids = [t["id"] for t in tasks]
        self.assertEqual(len(ids), len(set(ids)), "duplicate task ids")

    def test_seed_manifests_are_valid_toml_without_build_sections(self):
        # A seed that already carries [build.*] would score the seed, not
        # the agent.
        for task in build_step._load_tasks():
            text = (build_step.HERE / task["seed_manifest"]).read_text()
            data = tomllib.loads(text)  # raises on invalid TOML
            self.assertNotIn("build", data,
                             f"{task['id']}: seed manifest has a [build] section")

    def _write_registry(self, lines):
        tf = tempfile.NamedTemporaryFile(
            "w", suffix=".jsonl", delete=False, dir=tempfile.gettempdir())
        tf.write("\n".join(json.dumps(l) for l in lines))
        tf.close()
        self.addCleanup(os.unlink, tf.name)
        return Path(tf.name)

    def _valid_entry(self, **overrides):
        entry = {
            "id": "go-build", "tier": "should", "ecosystem": "go",
            "fixture": "go-build",
            "seed_manifest": "fixtures/go-build/seed-manifest.toml",
            "smoke": {"type": "run_bin", "args": [], "stdout_re": "x"},
            "rubric": "r",
        }
        entry.update(overrides)
        return entry

    def test_missing_field_rejected(self):
        entry = self._valid_entry()
        del entry["smoke"]
        with self.assertRaises(ValueError):
            build_step._load_tasks(self._write_registry([entry]))

    def test_unknown_smoke_type_rejected(self):
        entry = self._valid_entry(smoke={"type": "trust_me"})
        with self.assertRaises(ValueError):
            build_step._load_tasks(self._write_registry([entry]))

    def test_run_bin_without_stdout_re_rejected(self):
        entry = self._valid_entry(smoke={"type": "run_bin", "args": []})
        with self.assertRaises(ValueError):
            build_step._load_tasks(self._write_registry([entry]))

    def test_missing_fixture_rejected(self):
        entry = self._valid_entry(fixture="no-such-fixture")
        with self.assertRaises(ValueError):
            build_step._load_tasks(self._write_registry([entry]))

    def test_missing_seed_manifest_rejected(self):
        entry = self._valid_entry(seed_manifest="expected/no-such-seed.toml")
        with self.assertRaises(ValueError):
            build_step._load_tasks(self._write_registry([entry]))


class TestReadManifest(unittest.TestCase):
    def _write(self, tmpdir, text):
        env = Path(tmpdir) / ".flox" / "env"
        env.mkdir(parents=True)
        (env / "manifest.toml").write_text(text)

    def test_finds_targets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write(tmpdir, '[build.app]\ncommand = "make"\n[build.docs]\ncommand = "x"\n')
            text, valid, targets = build_step._read_manifest(tmpdir)
            self.assertTrue(valid)
            self.assertEqual(targets, ["app", "docs"])

    def test_no_build_section(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write(tmpdir, '[install]\ngo.pkg-path = "go"\n')
            text, valid, targets = build_step._read_manifest(tmpdir)
            self.assertTrue(valid)
            self.assertEqual(targets, [])

    def test_invalid_toml_reported_distinctly(self):
        # A manifest the agent corrupted must be distinguishable from one
        # that merely lacks a build target.
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write(tmpdir, "[build\noops")
            text, valid, targets = build_step._read_manifest(tmpdir)
            self.assertIsNotNone(text)
            self.assertFalse(valid)
            self.assertEqual(targets, [])

    def test_missing_manifest_is_none_not_a_crash(self):
        # The agent holds Write/Edit/Bash — it can delete the manifest.
        with tempfile.TemporaryDirectory() as tmpdir:
            text, valid, targets = build_step._read_manifest(tmpdir)
            self.assertIsNone(text)
            self.assertFalse(valid)
            self.assertEqual(targets, [])


class TestStage(unittest.TestCase):
    """_stage is filesystem-only; the flox side (init + seed + lock) is
    _seed_env, integration-only like the rest of the flox calls."""

    def test_fixture_copied_and_stray_file_removed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            task = next(t for t in build_step._load_tasks()
                        if t["id"] == "go-build")
            tmp = build_step._stage(task, tmpdir)
            # The agent must see a realistic repo: source present, no
            # seed-manifest.toml, and no .flox yet — flox init creates it.
            self.assertTrue((tmp / "main.go").is_file())
            self.assertFalse((tmp / "seed-manifest.toml").exists())
            self.assertFalse((tmp / ".flox").exists())


class TestSmoke(unittest.TestCase):
    def _fake_result(self, tmpdir, script="#!/bin/sh\necho 'Hello, Flox!'\n"):
        bin_dir = Path(tmpdir) / "result-app" / "bin"
        bin_dir.mkdir(parents=True)
        binary = bin_dir / "app"
        binary.write_text(script)
        binary.chmod(0o755)
        return binary

    def test_wrapped_dotfile_sibling_is_skipped(self):
        # flox build leaves `.greet-wrapped` beside `greet`; the smoke must
        # run the user-facing wrapper, not the hidden internal (seen live).
        with tempfile.TemporaryDirectory() as tmpdir:
            self._fake_result(tmpdir)
            hidden = Path(tmpdir) / "result-app" / "bin" / ".app-wrapped"
            hidden.write_text("#!/bin/sh\necho internal\n")
            hidden.chmod(0o755)
            bins = build_step._find_result_bins(tmpdir)
            self.assertEqual([b.name for b in bins], ["app"])

    def test_run_bin_matches(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._fake_result(tmpdir)
            ok, detail = build_step._smoke(
                {"smoke": {"type": "run_bin", "args": [],
                           "stdout_re": "Hello, Flox!"}}, tmpdir)
            self.assertTrue(ok, detail)

    def test_run_bin_wrong_output_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._fake_result(tmpdir, "#!/bin/sh\necho nope\n")
            ok, _ = build_step._smoke(
                {"smoke": {"type": "run_bin", "args": [],
                           "stdout_re": "Hello, Flox!"}}, tmpdir)
            self.assertFalse(ok)

    def test_run_bin_nonzero_exit_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._fake_result(tmpdir, "#!/bin/sh\necho 'Hello, Flox!'\nexit 3\n")
            ok, detail = build_step._smoke(
                {"smoke": {"type": "run_bin", "args": [],
                           "stdout_re": "Hello, Flox!"}}, tmpdir)
            self.assertFalse(ok)
            self.assertIn("exited 3", detail)

    def test_run_bin_without_result_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ok, detail = build_step._smoke(
                {"smoke": {"type": "run_bin", "args": [], "stdout_re": "x"}},
                tmpdir)
            self.assertFalse(ok)
            self.assertIn("no executable", detail)

    def test_artifact_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ok, _ = build_step._smoke(
                {"smoke": {"type": "artifact_exists"}}, tmpdir)
            self.assertFalse(ok)
            share = Path(tmpdir) / "result-pkg" / "share"
            share.mkdir(parents=True)
            (share / "pkg.whl").write_text("wheel")
            ok, _ = build_step._smoke(
                {"smoke": {"type": "artifact_exists"}}, tmpdir)
            self.assertTrue(ok)


class TestProcessTask(unittest.TestCase):
    """The disposition ladder, with every subprocess boundary mocked —
    mirrors how the sibling suites test their orchestrators."""

    def setUp(self):
        self.task = next(t for t in build_step._load_tasks()
                         if t["id"] == "go-build")

    def _run(self, seed=(True, ""), agent=("greet", None, {"num_turns": 3, "raw_stream": "..."}),
             manifest=("[build.greet]", True, ["greet"]),
             build=(True, ""), smoke=(True, "ok")):
        with patch.object(build_step, "_seed_env", return_value=seed), \
             patch.object(build_step, "_run_claude_agent", return_value=agent), \
             patch.object(build_step, "_read_manifest", return_value=manifest), \
             patch.object(build_step, "_run_flox_build", return_value=build), \
             patch.object(build_step, "_smoke", return_value=smoke):
            return build_step.process_task(self.task, Path("/nonexistent"))

    def test_seed_failure_is_unverifiable_env(self):
        r = self._run(seed=(False, "catalog down"))
        self.assertEqual(r["terminal_disposition"], "unverifiable-env")
        self.assertEqual(r["detail"], "catalog down")

    def test_agent_error_is_agent_error(self):
        r = self._run(agent=(None, "TIMEOUT", None))
        self.assertEqual(r["terminal_disposition"], "agent-error")

    def test_missing_manifest_scored_with_distinct_detail(self):
        r = self._run(manifest=(None, False, []))
        self.assertEqual(r["terminal_disposition"], "scored")
        self.assertFalse(r["manifest_present"])
        self.assertIn("missing", r["detail"])

    def test_corrupt_manifest_scored_distinct_from_no_target(self):
        r = self._run(manifest=("[build\noops", False, []))
        self.assertTrue(r["manifest_present"])
        self.assertFalse(r["manifest_valid_toml"])
        self.assertIn("no longer parses", r["detail"])
        r2 = self._run(manifest=("[install]", True, []))
        self.assertTrue(r2["manifest_valid_toml"])
        self.assertIn("no [build.*]", r2["detail"])
        self.assertNotEqual(r["detail"], r2["detail"])

    def test_build_failure_scored(self):
        r = self._run(build=(False, "hash mismatch"))
        self.assertEqual(r["terminal_disposition"], "scored")
        self.assertFalse(r["build_ok"])
        self.assertEqual(r["detail"], "hash mismatch")

    def test_success_records_everything(self):
        r = self._run()
        self.assertTrue(r["build_ok"] and r["smoke_ok"])
        self.assertEqual(r["build_targets"], ["greet"])
        self.assertEqual(r["agent_reported_target"], "greet")
        # raw_stream never reaches the result record.
        self.assertNotIn("raw_stream", r["meta"])
        self.assertEqual(r["meta"]["num_turns"], 3)

    def test_safe_wrapper_records_harness_error(self):
        with patch.object(build_step, "process_task",
                          side_effect=RuntimeError("boom")):
            r = build_step._safe_process_task(self.task, Path("/x"), 1)
        self.assertEqual(r["terminal_disposition"], "harness-error")
        self.assertIn("boom", r["detail"])


class TestClearResults(unittest.TestCase):
    def test_fabricated_result_dirs_removed_before_our_build(self):
        # An agent could hand-write result-a/bin/app; the harness's own
        # build must start from a tree with no result* entries at all.
        with tempfile.TemporaryDirectory() as tmpdir:
            fake = Path(tmpdir) / "result-a" / "bin"
            fake.mkdir(parents=True)
            (fake / "app").write_text("#!/bin/sh\necho fake\n")
            (Path(tmpdir) / "result-file").write_text("x")
            build_step._clear_results(tmpdir)
            self.assertEqual(list(Path(tmpdir).glob("result*")), [])


class TestBuildPrompt(unittest.TestCase):
    def test_prompt_carries_the_load_bearing_constraints(self):
        p = build_step._build_prompt(Path("/tmp/x"))
        self.assertIn("do not change its [install], [hook], "
                      "[vars], or [profile] sections", p)
        self.assertIn("references/builds.md", p)
        self.assertIn("flox build", p)
        self.assertIn("Do not modify the application source code", p)


class TestRegistryDisjointness(unittest.TestCase):
    def test_build_ids_do_not_collide_with_other_registries(self):
        # build.jsonl reuses fixtures/ and expected/ from other tiers, so
        # an id collision would stage the wrong repo with no error.
        others = set()
        for name in ("synthetic.jsonl", "stretch.jsonl", "real-world.jsonl"):
            path = build_step.HERE / name
            for line in path.read_text().splitlines():
                if line.strip():
                    others.add(json.loads(line)["id"])
        build_ids = {t["id"] for t in build_step._load_tasks()}
        self.assertFalse(build_ids & others,
                         f"build.jsonl ids collide: {build_ids & others}")


class TestSummary(unittest.TestCase):
    def test_dispositions_counted(self):
        results = [
            {"terminal_disposition": "scored", "build_targets": ["app"],
             "build_ok": True, "smoke_ok": True},
            {"terminal_disposition": "scored", "build_targets": ["app"],
             "build_ok": False, "smoke_ok": False},
            {"terminal_disposition": "scored", "build_targets": [],
             "build_ok": False, "smoke_ok": False},
            {"terminal_disposition": "unverifiable-env", "build_targets": [],
             "build_ok": False, "smoke_ok": False},
            {"terminal_disposition": "agent-error", "build_targets": [],
             "build_ok": False, "smoke_ok": False},
        ]
        s = build_step._summary(results)
        self.assertEqual(s["tasks"], 5)
        self.assertEqual(s["scored"], 3)
        self.assertEqual(s["unverifiable_env"], 1)
        self.assertEqual(s["agent_errors"], 1)
        self.assertEqual(s["authored_target"], 2)
        self.assertEqual(s["build_ok"], 1)
        self.assertEqual(s["smoke_ok"], 1)


if __name__ == "__main__":
    unittest.main()
