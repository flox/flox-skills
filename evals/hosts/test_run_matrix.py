"""Runner tests over mocked subprocesses — no docker, no flox, no API spend."""
import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import run_matrix
from lib import images
from lib.cells import CELLS


class TestImages(unittest.TestCase):
    def test_tag_is_env_name_and_version(self):
        # `flox containerize` names the repo after the ENVIRONMENT (hosts-base),
        # and -t is the tag alone.
        self.assertEqual(images.image_tag("base", "20260727"), "hosts-base:20260727")
        self.assertEqual(images.image_tag("withpkg", "20260727"), "hosts-withpkg:20260727")

    @patch("lib.images.subprocess.run")
    def test_image_exists_is_true_when_docker_prints_an_id(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="abc123\n", stderr="")
        self.assertTrue(images.image_exists("hosts-base:x"))

    @patch("lib.images.subprocess.run")
    def test_image_exists_is_false_when_docker_prints_nothing(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="\n", stderr="")
        self.assertFalse(images.image_exists("hosts-base:x"))

    @patch("lib.images.image_exists", return_value=True)
    @patch("lib.images.subprocess.run")
    def test_build_skips_when_image_present(self, run, _exists):
        images.build("base", "20260727")
        run.assert_not_called()

    @patch("lib.images.image_exists", return_value=True)
    @patch("lib.images.subprocess.run")
    def test_build_rebuilds_when_forced(self, run, _exists):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        images.build("base", "20260727", rebuild=True)
        self.assertEqual(run.call_count, 1)
        argv = run.call_args[0][0]
        self.assertIn("containerize", argv)
        # -t carries the bare version; a "name:version" here is the bug that
        # produced `hosts-base:base:20260727` and "invalid reference format".
        self.assertEqual(argv[argv.index("-t") + 1], "20260727")

    @patch("lib.images.image_exists", return_value=False)
    @patch("lib.images.subprocess.run")
    def test_build_raises_on_failure(self, run, _exists):
        run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
        with self.assertRaises(images.BuildError):
            images.build("base", "20260727")


class TestDockerCmd(unittest.TestCase):
    def test_mounts_creds_rw_and_script_ro(self):
        cmd = run_matrix.docker_cmd(CELLS[0], "img:1", Path("/tmp/run"),
                                    Path("/tmp/run/cell.sh"))
        joined = " ".join(cmd)
        self.assertIn("--rm", joined)
        self.assertIn("/tmp/run/claude:", joined)
        self.assertIn("cell.sh:/cell.sh:ro", joined)

    def test_sets_a_writable_home(self):
        # The image's default HOME is /var/empty (PROBE.md); agents need a
        # writable one or every cell dies on config write.
        cmd = run_matrix.docker_cmd(CELLS[0], "img:1", Path("/tmp/run"),
                                    Path("/tmp/run/cell.sh"))
        self.assertIn("HOME=/root", cmd)

    def test_runs_a_mounted_script_not_an_inline_payload(self):
        # `bash -lc '<script>'` is re-quoted by the flox entrypoint and breaks
        # on $( ) — see PROBE.md.
        cmd = run_matrix.docker_cmd(CELLS[0], "img:1", Path("/tmp/run"),
                                    Path("/tmp/run/cell.sh"))
        self.assertEqual(cmd[-2:], ["bash", "/cell.sh"])
        self.assertNotIn("-lc", cmd)

    def test_never_passes_an_api_key(self):
        cmd = run_matrix.docker_cmd(CELLS[0], "img:1", Path("/tmp/run"),
                                    Path("/tmp/run/cell.sh"))
        joined = " ".join(cmd)
        self.assertNotIn("ANTHROPIC_API_KEY", joined)
        self.assertNotIn("OPENAI_API_KEY", joined)


class TestRunCell(unittest.TestCase):
    def test_dry_run_invokes_nothing(self):
        with TemporaryDirectory() as tmp:
            with patch("run_matrix.subprocess.run") as run:
                out = run_matrix.run_cell(CELLS[0], "img:1", Path(tmp), dry_run=True)
                run.assert_not_called()
        self.assertEqual(out["tier_a"], "dry-run")

    @patch("run_matrix.subprocess.run")
    def test_tier_a_passes_when_list_cmd_prints_expect(self, run):
        run.return_value = subprocess.CompletedProcess(
            [], 0, stdout=run_matrix.LIST_MARKER + "\nflox@flox-skills", stderr="")
        with TemporaryDirectory() as tmp:
            out = run_matrix.run_cell(CELLS[0], "img:1", Path(tmp))
        self.assertEqual(out["tier_a"], "pass")

    @patch("run_matrix.subprocess.run")
    def test_installer_chatter_cannot_satisfy_tier_a(self, run):
        """Regression: skills.sh prints a picker listing 'flox' and 'floxify'
        while installing nothing. Judging the whole transcript passed the cell
        even though `claude plugin list` said 'No plugins installed'."""
        transcript = (
            "Found 2 skills\n  flox\n  floxify\n"
            + run_matrix.LIST_MARKER + "\n"
            + "No plugins installed. Use `claude plugin install` to install a plugin.\n"
        )
        run.return_value = subprocess.CompletedProcess([], 0, stdout=transcript, stderr="")
        with TemporaryDirectory() as tmp:
            out = run_matrix.run_cell(CELLS[1], "img:1", Path(tmp))
        self.assertEqual(out["tier_a"], "fail")

    def test_list_output_discards_everything_before_the_marker(self):
        text = f"noise flox\n{run_matrix.LIST_MARKER}\nreal output"
        self.assertEqual(run_matrix.list_output(text).strip(), "real output")

    def test_list_output_is_empty_when_marker_missing(self):
        self.assertEqual(run_matrix.list_output("flox everywhere"), "")

    @patch("run_matrix.subprocess.run")
    def test_tier_a_fails_when_expect_absent(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="no plugins", stderr="")
        with TemporaryDirectory() as tmp:
            out = run_matrix.run_cell(CELLS[0], "img:1", Path(tmp))
        self.assertEqual(out["tier_a"], "fail")
        self.assertEqual(out["tier_b"], "skipped")

    @patch("run_matrix.subprocess.run")
    def test_auth_failure_is_reported_distinctly(self, run):
        run.side_effect = [
            subprocess.CompletedProcess([], 0, stdout=run_matrix.LIST_MARKER + "\nflox",
                                        stderr=""),
            subprocess.CompletedProcess([], 1, stdout="",
                                        stderr="Invalid API key · Please run /login"),
        ]
        with TemporaryDirectory() as tmp:
            out = run_matrix.run_cell(CELLS[0], "img:1", Path(tmp))
        self.assertEqual(out["tier_b"], "auth-error")

    @patch("run_matrix.subprocess.run", side_effect=RuntimeError("boom"))
    def test_a_crashing_cell_records_and_does_not_raise(self, _run):
        with TemporaryDirectory() as tmp:
            out = run_matrix.run_cell(CELLS[0], "img:1", Path(tmp))
        self.assertEqual(out["tier_a"], "error")

    def test_script_shims_usr_bin_env(self):
        # Without this, npx-installed binaries die on `#!/usr/bin/env node`
        # because a flox container has no FHS layout.
        script = run_matrix.cell_script(CELLS[1], include_launch=False)
        self.assertIn("/usr/bin/env", script)
        self.assertIn("ln -s", script)

    @patch("run_matrix.subprocess.run")
    def test_tier_a_only_never_launches(self, run):
        run.return_value = subprocess.CompletedProcess(
            [], 0, stdout=run_matrix.LIST_MARKER + "\nflox", stderr="")
        with TemporaryDirectory() as tmp:
            out = run_matrix.run_cell(CELLS[0], "img:1", Path(tmp), tier_a_only=True)
        self.assertEqual(out["tier_b"], "not-attempted")
        # One container only: the Tier A check. Relabelling after the fact
        # would still have spent the model call.
        self.assertEqual(run.call_count, 1)

    @patch("run_matrix.subprocess.run")
    def test_prompt_placeholder_is_substituted(self, run):
        run.return_value = subprocess.CompletedProcess(
            [], 0, stdout=run_matrix.LIST_MARKER + "\nflox", stderr="")
        with TemporaryDirectory() as tmp:
            run_matrix.run_cell(CELLS[0], "img:1", Path(tmp))
            script = (Path(tmp) / "cell.sh").read_text()
        self.assertIn(run_matrix.CONTAINER_PROMPT, script)
        self.assertNotIn("{prompt}", script)


class TestResults(unittest.TestCase):
    def test_results_are_one_json_object_per_line(self):
        rows = [{"cell": "a", "tier_a": "pass"}, {"cell": "b", "tier_a": "fail"}]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.jsonl"
            run_matrix.write_results(path, rows)
            lines = path.read_text().strip().splitlines()
        self.assertEqual([json.loads(x)["cell"] for x in lines], ["a", "b"])


if __name__ == "__main__":
    unittest.main()


class TestClassifyTrigger(unittest.TestCase):
    def test_explicit_non_injection_beats_a_good_answer(self):
        text = ('warning: codex is not the flox-patched build; skills and rules '
                'will not be injected\npkg-path python312 [services] flox activate')
        self.assertEqual(run_matrix.classify_trigger(text), "not-injected")

    def test_greeting_only_is_weak(self):
        self.assertEqual(
            run_matrix.classify_trigger("I'm here and ready. What would you like to work on?"),
            "weak")

    def test_skill_shaped_answer(self):
        text = 'python312.pkg-path = "python312"\n[services]\npostgresql_16'
        self.assertEqual(run_matrix.classify_trigger(text), "answer-shaped")

    def test_empty_output(self):
        self.assertEqual(run_matrix.classify_trigger("   "), "no-output")


class TestAuthDetection(unittest.TestCase):
    @patch("run_matrix.subprocess.run")
    def test_prose_about_postgres_auth_is_not_an_auth_error(self, run):
        """Regression: Codex answered 'PostgreSQL uses trust authentication'
        and the cell was scored auth-error on an exit-0 run."""
        answer = ("PostgreSQL is socket-only and uses trust authentication, so "
                  "this is for local development. pkg-path python312 [services]")
        run.side_effect = [
            subprocess.CompletedProcess([], 0, stdout=run_matrix.LIST_MARKER + "\nflox",
                                        stderr=""),
            subprocess.CompletedProcess([], 0, stdout=answer, stderr=""),
        ]
        with TemporaryDirectory() as tmp:
            out = run_matrix.run_cell(CELLS[0], "img:1", Path(tmp))
        self.assertEqual(out["tier_b"], "pass")

    @patch("run_matrix.subprocess.run")
    def test_a_real_auth_failure_still_reports(self, run):
        run.side_effect = [
            subprocess.CompletedProcess([], 0, stdout=run_matrix.LIST_MARKER + "\nflox",
                                        stderr=""),
            subprocess.CompletedProcess([], 1, stdout="", stderr="Please run /login"),
        ]
        with TemporaryDirectory() as tmp:
            out = run_matrix.run_cell(CELLS[0], "img:1", Path(tmp))
        self.assertEqual(out["tier_b"], "auth-error")
