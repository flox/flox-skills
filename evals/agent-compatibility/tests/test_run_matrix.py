"""Runner tests over mocked subprocesses — no docker, no flox, no API spend."""
import contextlib
import io
import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import run_matrix
from lib import images
from lib.cells import CELLS


def cell(cell_id):
    """Address a cell by id.

    These tests used to index `CELLS` positionally, so inserting a cell at the
    front silently retargeted a regression test at a different cell — and
    nothing pins the tuple's order.
    """
    return next(c for c in CELLS if c.id == cell_id)


class TestImages(unittest.TestCase):
    def test_tag_is_env_name_and_version(self):
        # `flox containerize` names the repo after the ENVIRONMENT (agent-compat-base),
        # and -t is the tag alone.
        self.assertEqual(images.image_tag("base", "20260727"), "agent-compat-base:20260727")
        self.assertEqual(images.image_tag("withpkg", "20260727"), "agent-compat-withpkg:20260727")

    @patch("lib.images.subprocess.run")
    def test_image_exists_is_true_when_docker_prints_an_id(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="abc123\n", stderr="")
        self.assertTrue(images.image_exists("agent-compat-base:x"))

    @patch("lib.images.subprocess.run")
    def test_image_exists_is_false_when_docker_prints_nothing(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="\n", stderr="")
        self.assertFalse(images.image_exists("agent-compat-base:x"))

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
        # produced `agent-compat-base:base:20260727` and "invalid reference format".
        self.assertEqual(argv[argv.index("-t") + 1], "20260727")
        # The environment is a directory under environments/, and it has to
        # exist: `flox containerize -d` on a path with no .flox/ fails at
        # build time, long after this is cheap to catch.
        target = Path(argv[argv.index("-d") + 1])
        self.assertEqual(target.parent.name, "environments")
        self.assertTrue((target / ".flox" / "env.json").is_file(), target)

    @patch("lib.images.image_exists", return_value=False)
    @patch("lib.images.subprocess.run")
    def test_build_raises_on_failure(self, run, _exists):
        run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
        with self.assertRaises(images.BuildError):
            images.build("base", "20260727")


class TestDockerCmd(unittest.TestCase):
    def test_mounts_creds_rw_and_script_ro(self):
        cmd = run_matrix.docker_cmd("img:1", Path("/tmp/run"),
                                    Path("/tmp/run/cell.sh"),
                                    mount_credentials=True)
        joined = " ".join(cmd)
        self.assertIn("--rm", joined)
        self.assertIn("/tmp/run/claude:", joined)
        self.assertIn("cell.sh:/cell.sh:ro", joined)

    def test_sets_a_writable_home(self):
        # The image's default HOME is /var/empty; agents need a writable one
        # or every cell dies on config write.
        cmd = run_matrix.docker_cmd("img:1", Path("/tmp/run"),
                                    Path("/tmp/run/cell.sh"),
                                    mount_credentials=True)
        self.assertIn("HOME=/root", cmd)

    def test_runs_a_mounted_script_not_an_inline_payload(self):
        # `bash -lc '<script>'` is re-quoted by the flox entrypoint and breaks
        # on $( ), so the cell runs from a mounted script instead.
        cmd = run_matrix.docker_cmd("img:1", Path("/tmp/run"),
                                    Path("/tmp/run/cell.sh"),
                                    mount_credentials=True)
        self.assertEqual(cmd[-2:], ["bash", "/cell.sh"])
        self.assertNotIn("-lc", cmd)

    def test_never_passes_an_api_key(self):
        cmd = run_matrix.docker_cmd("img:1", Path("/tmp/run"),
                                    Path("/tmp/run/cell.sh"),
                                    mount_credentials=True)
        joined = " ".join(cmd)
        self.assertNotIn("ANTHROPIC_API_KEY", joined)
        self.assertNotIn("OPENAI_API_KEY", joined)


class TestRunCell(unittest.TestCase):
    def test_dry_run_invokes_nothing(self):
        with TemporaryDirectory() as tmp:
            with patch("run_matrix.subprocess.run") as run:
                out = run_matrix.run_cell(cell("claude-native"), "img:1", Path(tmp), dry_run=True)
                run.assert_not_called()
        self.assertEqual(out["load"], "dry-run")

    @patch("run_matrix.subprocess.run")
    def test_load_passes_when_list_cmd_prints_expect(self, run):
        run.return_value = subprocess.CompletedProcess(
            [], 0, stdout=run_matrix.LIST_MARKER + "\nflox@flox-skills", stderr="")
        with TemporaryDirectory() as tmp:
            out = run_matrix.run_cell(cell("claude-native"), "img:1", Path(tmp))
        self.assertEqual(out["load"], "pass")

    @patch("run_matrix.subprocess.run")
    def test_installer_chatter_cannot_satisfy_the_load_check(self, run):
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
            out = run_matrix.run_cell(cell("claude-npx"), "img:1", Path(tmp))
        self.assertEqual(out["load"], "fail")

    def test_list_output_discards_everything_before_the_marker(self):
        text = f"noise flox\n{run_matrix.LIST_MARKER}\nreal output"
        self.assertEqual(run_matrix.list_output(text).strip(), "real output")

    def test_list_output_is_empty_when_marker_missing(self):
        self.assertEqual(run_matrix.list_output("flox everywhere"), "")

    @patch("run_matrix.subprocess.run")
    def test_load_fails_when_expect_absent(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="no plugins", stderr="")
        with TemporaryDirectory() as tmp:
            out = run_matrix.run_cell(cell("claude-native"), "img:1", Path(tmp))
        self.assertEqual(out["load"], "fail")
        self.assertEqual(out["trigger"], "skipped")

    @patch("run_matrix.subprocess.run")
    def test_auth_failure_is_reported_distinctly(self, run):
        run.side_effect = [
            subprocess.CompletedProcess([], 0, stdout=run_matrix.LIST_MARKER + "\nflox",
                                        stderr=""),
            subprocess.CompletedProcess(
                [], 1, stdout="",
                stderr=run_matrix.LAUNCH_MARKER + "\nInvalid API key · Please run /login"),
        ]
        with TemporaryDirectory() as tmp:
            out = run_matrix.run_cell(cell("claude-native"), "img:1", Path(tmp))
        self.assertEqual(out["trigger"], "auth-error")

    @patch("run_matrix.subprocess.run", side_effect=RuntimeError("boom"))
    def test_a_crashing_cell_records_and_does_not_raise(self, _run):
        with TemporaryDirectory() as tmp:
            out = run_matrix.run_cell(cell("claude-native"), "img:1", Path(tmp))
        self.assertEqual(out["load"], "error")

    def test_script_shims_usr_bin_env(self):
        # Without this, npx-installed binaries die on `#!/usr/bin/env node`
        # because a flox container has no FHS layout.
        script = run_matrix.cell_script(cell("claude-npx"), include_launch=False)
        self.assertIn("/usr/bin/env", script)
        self.assertIn("ln -s", script)

    @patch("run_matrix.subprocess.run")
    def test_load_only_never_launches(self, run):
        run.return_value = subprocess.CompletedProcess(
            [], 0, stdout=run_matrix.LIST_MARKER + "\nflox", stderr="")
        with TemporaryDirectory() as tmp:
            out = run_matrix.run_cell(cell("claude-native"), "img:1", Path(tmp), load_only=True)
        self.assertEqual(out["trigger"], "not-attempted")
        # One container only: the load check. Relabelling after the fact
        # would still have spent the model call.
        self.assertEqual(run.call_count, 1)

    @patch("run_matrix.subprocess.run")
    def test_prompt_placeholder_is_substituted(self, run):
        run.return_value = subprocess.CompletedProcess(
            [], 0, stdout=run_matrix.LIST_MARKER + "\nflox", stderr="")
        with TemporaryDirectory() as tmp:
            run_matrix.run_cell(cell("claude-native"), "img:1", Path(tmp))
            script = (Path(tmp) / "cell.sh").read_text()
        self.assertIn(run_matrix.CONTAINER_PROMPT, script)
        self.assertNotIn("{prompt}", script)


class TestResults(unittest.TestCase):
    def test_results_are_one_json_object_per_line(self):
        rows = [{"cell": "a", "load": "pass"}, {"cell": "b", "load": "fail"}]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.jsonl"
            run_matrix.write_results(path, rows)
            lines = path.read_text().strip().splitlines()
        self.assertEqual([json.loads(x)["cell"] for x in lines], ["a", "b"])


class TestClassifyTrigger(unittest.TestCase):
    def test_the_flox_ai_warning_does_not_override_a_good_answer(self):
        """Regression, replacing a test that pinned the inverse.

        flox-ai prints this against a Flox-packaged Codex that IS patched: its
        `codexIsPatched` byte-scans the Nix wrapper on PATH rather than the ELF
        the wrapper execs, and injection is not gated on the check anyway. The
        classifier used to return `not-injected` here, which excluded a healthy
        cell from the green count forever.
        """
        text = ('warning: codex is not the flox-patched build; skills and rules '
                'will not be injected\npkg-path python312 [services] flox activate')
        self.assertEqual(run_matrix.classify_trigger(text), "answer-shaped")

    def test_the_warning_is_still_recorded_as_a_harness_note(self):
        text = 'warning: codex is not the flox-patched build'
        notes = run_matrix.harness_warnings(text)
        self.assertEqual(len(notes), 1)
        self.assertIn("false alarm", notes[0])

    def test_one_fingerprint_is_weak(self):
        # The boundary the >= 2 rule turns on, previously untested.
        self.assertEqual(run_matrix.classify_trigger("use pkg-path"), "weak")

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
            subprocess.CompletedProcess(
                [], 0, stdout=run_matrix.LAUNCH_MARKER + "\n" + answer, stderr=""),
        ]
        with TemporaryDirectory() as tmp:
            out = run_matrix.run_cell(cell("claude-native"), "img:1", Path(tmp))
        self.assertEqual(out["trigger"], "pass")

    @patch("run_matrix.subprocess.run")
    def test_a_real_auth_failure_still_reports(self, run):
        run.side_effect = [
            subprocess.CompletedProcess([], 0, stdout=run_matrix.LIST_MARKER + "\nflox",
                                        stderr=""),
            subprocess.CompletedProcess(
                [], 1, stdout="",
                stderr=run_matrix.LAUNCH_MARKER + "\nPlease run /login"),
        ]
        with TemporaryDirectory() as tmp:
            out = run_matrix.run_cell(cell("claude-native"), "img:1", Path(tmp))
        self.assertEqual(out["trigger"], "auth-error")


class TestResultsMerge(unittest.TestCase):
    def test_subset_run_does_not_discard_other_cells(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.jsonl"
            run_matrix.write_results(path, [
                {"cell": "claude-native", "load": "pass"},
                {"cell": "codex-npx", "load": "pass"},
            ])
            run_matrix.write_results(path, [{"cell": "codex-npx", "load": "fail"}])
            rows = {json.loads(x)["cell"]: json.loads(x)
                    for x in path.read_text().splitlines()}
        self.assertEqual(set(rows), {"claude-native", "codex-npx"})
        self.assertEqual(rows["codex-npx"]["load"], "fail")   # newer wins
        self.assertEqual(rows["claude-native"]["load"], "pass")  # survivor


class TestCleanup(unittest.TestCase):
    def test_host_owned_dir_is_removed_without_docker(self):
        with TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            (run_dir / "claude").mkdir(parents=True)
            (run_dir / "claude" / ".credentials.json").write_text("{}")
            with patch("run_matrix.subprocess.run") as run:
                leaked = run_matrix.cleanup_run_dir(run_dir, "img:1")
                run.assert_not_called()      # plain rmtree sufficed
        self.assertEqual(leaked, [])
        self.assertFalse(run_dir.exists())

    def test_root_owned_leftovers_trigger_a_container_sweep(self):
        with TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir(parents=True)
            with patch("run_matrix.shutil.rmtree"), \
                 patch("run_matrix.subprocess.run") as run:
                run_matrix.cleanup_run_dir(run_dir, "img:1")
            argv = run.call_args[0][0]
        self.assertIn("docker", argv)
        self.assertIn(f"{run_dir}:/sweep", argv)

    def test_surviving_credentials_are_reported(self):
        with TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            (run_dir / "codex").mkdir(parents=True)
            (run_dir / "codex" / "auth.json").write_text("{}")
            with patch("run_matrix.shutil.rmtree"), \
                 patch("run_matrix.subprocess.run"):
                leaked = run_matrix.cleanup_run_dir(run_dir, "img:1")
        self.assertEqual(len(leaked), 1)
        self.assertTrue(leaked[0].endswith("auth.json"))


class TestMarkerScoping(unittest.TestCase):
    """Every verdict judges one step's output, never the whole transcript."""

    def test_a_failing_git_clone_is_not_an_auth_error(self):
        """Regression: `codex-native` installs with `git clone`, the trigger
        container re-runs the install, and a failing clone prints "fatal:
        Authentication failed" — an AUTH_MARKER. The cell recorded
        "credential problem, not a skill problem" for a repo/network failure."""
        install_failure = ("Cloning into '/work/flox-skills'...\n"
                           "fatal: Authentication failed for "
                           "'https://github.com/flox/flox-skills.git/'\n")
        with patch("run_matrix.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(
                    [], 0, stdout=run_matrix.LIST_MARKER + "\nflox", stderr=""),
                # No launch marker: the script died in the install step.
                subprocess.CompletedProcess([], 1, stdout="", stderr=install_failure),
            ]
            with TemporaryDirectory() as tmp:
                out = run_matrix.run_cell(cell("codex-native"), "img:1", Path(tmp))
        self.assertEqual(out["trigger"], "fail")

    def test_an_agent_auth_failure_after_the_marker_still_reports(self):
        with patch("run_matrix.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(
                    [], 0, stdout=run_matrix.LIST_MARKER + "\nflox", stderr=""),
                subprocess.CompletedProcess(
                    [], 1, stdout="", stderr=run_matrix.LAUNCH_MARKER + "\nPlease run /login"),
            ]
            with TemporaryDirectory() as tmp:
                out = run_matrix.run_cell(cell("codex-native"), "img:1", Path(tmp))
        self.assertEqual(out["trigger"], "auth-error")

    def test_installer_chatter_cannot_reach_the_evidence_classifier(self):
        chatter = "pkg-path [services] python312 flox activate\n"
        with patch("run_matrix.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(
                    [], 0, stdout=run_matrix.LIST_MARKER + "\nflox", stderr=""),
                subprocess.CompletedProcess(
                    [], 0, stdout=chatter + run_matrix.LAUNCH_MARKER + "\nhello", stderr=""),
            ]
            with TemporaryDirectory() as tmp:
                out = run_matrix.run_cell(cell("claude-native"), "img:1", Path(tmp))
        self.assertEqual(out["evidence_class"], "weak")

    def test_the_launch_marker_is_emitted_on_both_streams(self):
        script = run_matrix.cell_script(cell("claude-native"), include_launch=True)
        self.assertIn(f"echo {run_matrix.LAUNCH_MARKER}", script)
        self.assertIn(f"echo {run_matrix.LAUNCH_MARKER} >&2", script)


class TestVerdictsSurviveEachOther(unittest.TestCase):
    def test_a_trigger_timeout_keeps_a_load_pass(self):
        with patch("run_matrix.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(
                    [], 0, stdout=run_matrix.LIST_MARKER + "\nflox", stderr=""),
                subprocess.TimeoutExpired(cmd="docker", timeout=600),
            ]
            with TemporaryDirectory() as tmp:
                out = run_matrix.run_cell(cell("claude-native"), "img:1", Path(tmp))
        self.assertEqual(out["load"], "pass")      # measured, and kept
        self.assertEqual(out["trigger"], "timeout")

    def test_a_load_timeout_is_still_recorded_as_one(self):
        with patch("run_matrix.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=600)):
            with TemporaryDirectory() as tmp:
                out = run_matrix.run_cell(cell("claude-native"), "img:1", Path(tmp))
        self.assertEqual(out["load"], "timeout")

    def test_a_trigger_crash_keeps_a_load_pass(self):
        with patch("run_matrix.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(
                    [], 0, stdout=run_matrix.LIST_MARKER + "\nflox", stderr=""),
                RuntimeError("boom"),
            ]
            with TemporaryDirectory() as tmp:
                out = run_matrix.run_cell(cell("claude-native"), "img:1", Path(tmp))
        self.assertEqual(out["load"], "pass")
        self.assertEqual(out["trigger"], "error")

    def test_both_halves_keep_their_own_transcript(self):
        with patch("run_matrix.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(
                    [], 0, stdout=run_matrix.LIST_MARKER + "\nflox plugin listed", stderr=""),
                subprocess.CompletedProcess(
                    [], 0, stdout=run_matrix.LAUNCH_MARKER + "\npkg-path [services]", stderr=""),
            ]
            with TemporaryDirectory() as tmp:
                out = run_matrix.run_cell(cell("claude-native"), "img:1", Path(tmp))
        self.assertIn("plugin listed", out["load_evidence"])
        self.assertIn("pkg-path", out["trigger_evidence"])


class TestCredentialMounts(unittest.TestCase):
    def test_the_load_container_gets_no_credentials(self):
        """The load check is documented credential-free, and it runs
        `npx --yes skills add` — unpinned code fetched at run time, as root."""
        cmd = run_matrix.docker_cmd("img:1", Path("/tmp/run"),
                                    Path("/tmp/run/cell.sh"),
                                    mount_credentials=False)
        joined = " ".join(cmd)
        self.assertNotIn("/.claude", joined)
        self.assertNotIn("/.codex", joined)
        self.assertIn("/prompt.txt", joined)      # the prompt still mounts

    def test_the_trigger_container_does(self):
        cmd = run_matrix.docker_cmd("img:1", Path("/tmp/run"),
                                    Path("/tmp/run/cell.sh"),
                                    mount_credentials=True)
        self.assertIn("/tmp/run/claude:/root/.claude:rw", cmd)

    def test_run_cell_mounts_credentials_only_for_the_launch(self):
        with patch("run_matrix.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                [], 0, stdout=run_matrix.LIST_MARKER + "\nflox", stderr="")
            with TemporaryDirectory() as tmp:
                run_matrix.run_cell(cell("claude-native"), "img:1", Path(tmp),
                                    load_only=True)
            argv = run.call_args[0][0]
        self.assertNotIn(".claude:rw", " ".join(argv))


class TestSelection(unittest.TestCase):
    def test_a_typo_beside_a_valid_id_is_an_error(self):
        with self.assertRaises(ValueError) as ctx:
            run_matrix.select_cells("claude-native,codex-nxp")
        self.assertIn("codex-nxp", str(ctx.exception))
        self.assertIn("claude-native", str(ctx.exception))   # lists the known ids

    def test_a_valid_subset_selects_it(self):
        got = [c.id for c in run_matrix.select_cells("codex-npx,claude-native")]
        self.assertEqual(sorted(got), ["claude-native", "codex-npx"])

    def test_no_argument_selects_everything(self):
        self.assertEqual(len(run_matrix.select_cells(None)), len(CELLS))


class TestMain(unittest.TestCase):
    """`main()` had no test, and every consensus defect the panel found lived here."""

    @staticmethod
    def _main(argv):
        """Run `main` with its summary table swallowed — these assert on the
        exit status and the results file, not on stdout."""
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return run_matrix.main(argv)

    def test_a_dry_run_writes_no_results(self):
        with TemporaryDirectory() as tmp:
            results = Path(tmp) / "results"
            with patch("run_matrix.RESULTS", results):
                rc = self._main(["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertFalse(results.exists(), "a dry run measured nothing and must write nothing")

    def test_a_dry_run_cannot_overwrite_a_real_run(self):
        """Regression: `--dry-run` is the command the README says to run first,
        and it used to stamp `dry-run` over an authenticated run's verdicts."""
        with TemporaryDirectory() as tmp:
            results = Path(tmp) / "results"
            out = results / "20260813.jsonl"
            run_matrix.write_results(out, [{"cell": "claude-native", "load": "pass",
                                            "trigger": "pass",
                                            "evidence_class": "answer-shaped"}])
            with patch("run_matrix.RESULTS", results):
                self._main(["--dry-run", "--version", "20260813"])
            row = json.loads(out.read_text().splitlines()[0])
        self.assertEqual(row["load"], "pass")
        self.assertEqual(row["trigger"], "pass")

    def test_an_unknown_cell_id_exits_2(self):
        self.assertEqual(self._main(["--dry-run", "--cells", "nope"]), 2)

    def test_a_version_that_is_not_a_path_component_exits_2(self):
        # A slash escapes results/; a colon yields an invalid Docker reference.
        self.assertEqual(self._main(["--dry-run", "--version", "../etc"]), 2)
        self.assertEqual(self._main(["--dry-run", "--version", "a:b"]), 2)

    def test_a_version_that_is_not_a_legal_docker_tag_exits_2(self):
        # Legal as a filename, illegal as a tag: Docker requires the first
        # character to be alphanumeric or `_`, and caps the tag at 128.
        self.assertEqual(self._main(["--dry-run", "--version", "."]), 2)
        self.assertEqual(self._main(["--dry-run", "--version", "a" * 129]), 2)

    def test_a_leading_dash_version_is_rejected_by_the_parser(self):
        # argparse takes it for a flag and exits 2 before the check below can
        # run — the same status by a different route, asserted so a future
        # parser change cannot turn it into a silent default.
        with self.assertRaises(SystemExit) as ctx:
            self._main(["--dry-run", "--version", "-x"])
        self.assertEqual(ctx.exception.code, 2)

    def test_an_ordinary_version_is_accepted(self):
        self.assertEqual(self._main(["--dry-run", "--version", "20260813"]), 0)
        self.assertEqual(self._main(["--dry-run", "--version", "v1.2_rc-3"]), 0)

    def _run_main_with(self, rows, argv, leaked=None, build=None):
        """Drive `main` with the containers mocked out.

        `cleanup_run_dir` is stubbed, and it is the only thing that removes
        `main`'s `mkdtemp` — so the run directory is redirected into the test's
        own temp tree rather than abandoned under /tmp.
        """
        with TemporaryDirectory() as tmp:
            results = Path(tmp) / "results"
            runs = Path(tmp) / "run"
            runs.mkdir()
            with patch("run_matrix.RESULTS", results), \
                 patch("run_matrix.tempfile.mkdtemp", return_value=str(runs)), \
                 patch("run_matrix.images.build",
                       side_effect=build, return_value="img:1"), \
                 patch("run_matrix.creds.prepare"), \
                 patch("run_matrix.cleanup_run_dir", return_value=leaked or []), \
                 patch("run_matrix.run_cell", side_effect=rows):
                return self._main(argv)

    @staticmethod
    def _green():
        """Rows in the shape `run_cell` really returns — see the contract test."""
        return [{"cell": c.id, "agent": c.agent,
                 "install_method": c.install_method, "image": c.image,
                 "load": "pass", "trigger": "pass",
                 "evidence_class": "answer-shaped", "load_evidence": "",
                 "trigger_evidence": "", "notes": ""} for c in CELLS]

    def test_an_all_green_run_exits_0(self):
        self.assertEqual(self._run_main_with(self._green(), []), 0)

    def test_a_red_cell_exits_1(self):
        rows = self._green()
        rows[3] = dict(rows[3], load="fail", trigger="skipped")
        self.assertEqual(self._run_main_with(rows, []), 1)

    def test_a_load_only_run_is_green_on_the_load_half_alone(self):
        rows = [dict(r, trigger="not-attempted", evidence_class="")
                for r in self._green()]
        self.assertEqual(self._run_main_with(rows, ["--load-only"]), 0)

    def test_surviving_credentials_exit_3(self):
        rc = self._run_main_with(self._green(), [],
                                 leaked=["/tmp/x/claude/.credentials.json"])
        self.assertEqual(rc, 3, "a credential copy on disk must not exit 0")

    def test_a_credential_leak_outranks_a_red_cell(self):
        """The codes are ranked, not disjoint: the one outcome with a security
        consequence must not hide behind an ordinary failure."""
        rows = self._green()
        rows[2] = dict(rows[2], load="fail", trigger="skipped")
        rc = self._run_main_with(rows, [],
                                 leaked=["/tmp/x/codex/auth.json"])
        self.assertEqual(rc, 3)

    def test_an_all_auth_error_run_is_distinguishable_from_a_broken_product(self):
        """`auth-error` is the one failure the runner separates from a skill
        failure on purpose; the exit status must not re-merge them."""
        rows = [dict(r, trigger="auth-error") for r in self._green()]
        self.assertEqual(self._run_main_with(rows, []), 4)

    def test_a_failed_image_build_does_not_look_like_a_red_cell(self):
        rc = self._run_main_with(self._green(), [],
                                 build=run_matrix.images.BuildError("no docker"))
        self.assertEqual(rc, 5)


class TestSummarize(unittest.TestCase):
    def test_load_only_reports_the_number_it_was_run_for(self):
        rows = [{"cell": "a", "load": "pass", "trigger": "not-attempted",
                 "evidence_class": ""}]
        out = run_matrix.summarize(rows, load_only=True)
        self.assertIn("1/1 cells load", out)
        self.assertNotIn("0/1", out)

    def test_a_dry_run_reports_a_plan_not_a_score(self):
        rows = [{"cell": "a", "load": "dry-run", "trigger": "dry-run",
                 "evidence_class": ""}]
        out = run_matrix.summarize(rows, dry_run=True)
        self.assertIn("planned", out)
        self.assertNotIn("green", out)


class TestResultsFile(unittest.TestCase):
    def test_results_are_written_private(self):
        """Each row carries a transcript tail from a credentialed session."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.jsonl"
            run_matrix.write_results(path, [{"cell": "a", "load": "pass"}])
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_one_unreadable_line_does_not_lose_the_new_rows(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.jsonl"
            path.write_text('{"cell": "a", "load": "pass"}\n{truncated\n')
            run_matrix.write_results(path, [{"cell": "b", "load": "pass"}])
            cells = {json.loads(x)["cell"] for x in path.read_text().splitlines()}
        self.assertEqual(cells, {"a", "b"})



class TestRowContract(unittest.TestCase):
    """`main`'s tests fabricate rows, so something has to pin the real shape."""

    ROW_KEYS = {"cell", "agent", "install_method", "image", "load", "trigger",
                "evidence_class", "load_evidence", "trigger_evidence", "notes"}

    def test_run_cell_returns_the_documented_keys(self):
        # These are the keys the README lists as the results-file contract.
        with TemporaryDirectory() as tmp:
            row = run_matrix.run_cell(cell("claude-native"), "img:1",
                                      Path(tmp), dry_run=True)
        self.assertEqual(set(row), self.ROW_KEYS)

    def test_a_real_run_returns_the_same_keys(self):
        with patch("run_matrix.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(
                    [], 0, stdout=run_matrix.LIST_MARKER + "\nflox", stderr=""),
                subprocess.CompletedProcess(
                    [], 0, stdout=run_matrix.LAUNCH_MARKER + "\npkg-path [services]",
                    stderr=""),
            ]
            with TemporaryDirectory() as tmp:
                row = run_matrix.run_cell(cell("claude-native"), "img:1", Path(tmp))
        self.assertEqual(set(row), self.ROW_KEYS)


class TestSweepTimeout(unittest.TestCase):
    def test_the_sweep_is_bounded(self):
        """It runs in a `finally` and it is what deletes the credential copies."""
        with TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            with patch("run_matrix.shutil.rmtree"), \
                 patch("run_matrix.subprocess.run") as run:
                run_matrix.cleanup_run_dir(run_dir, "img:1")
        self.assertEqual(run.call_args[1]["timeout"], run_matrix.SWEEP_TIMEOUT)

    def test_a_hung_sweep_still_reports_surviving_credentials(self):
        """The alarm matters more than the sweep: a timeout must not swallow it."""
        with TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            (run_dir / "codex").mkdir(parents=True)
            (run_dir / "codex" / "auth.json").write_text("{}")
            with patch("run_matrix.shutil.rmtree"), \
                 patch("run_matrix.subprocess.run",
                       side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=1)):
                leaked = run_matrix.cleanup_run_dir(run_dir, "img:1")
        self.assertEqual(len(leaked), 1)
        self.assertTrue(leaked[0].endswith("auth.json"))


if __name__ == "__main__":
    unittest.main()
