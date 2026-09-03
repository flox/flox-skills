#!/usr/bin/env python3
"""Unit tests for build_step.py's deterministic pieces.

The agentic run, the seed activation, and the independent `flox build` are
integration-only (exercised by a real `--only <id>` run). Everything here is
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


class TestManifestBuildTargets(unittest.TestCase):
    def test_finds_targets(self):
        text = '[build.app]\ncommand = "make"\n[build.docs]\ncommand = "x"\n'
        self.assertEqual(build_step._manifest_build_targets(text),
                         ["app", "docs"])

    def test_no_build_section(self):
        self.assertEqual(
            build_step._manifest_build_targets('[install]\ngo.pkg-path = "go"\n'),
            [])

    def test_invalid_toml_is_no_targets_not_a_crash(self):
        # A manifest the agent corrupted still yields a scoreable result.
        self.assertEqual(build_step._manifest_build_targets("[build\noops"), [])


class TestStage(unittest.TestCase):
    """_stage is filesystem-only; the flox side (init + seed + lock) is
    _seed_env, integration-only like the rest of the flox calls."""

    def test_fixture_copied_and_stray_file_removed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            task = build_step._load_tasks()[0]  # go-build
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
