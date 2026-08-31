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
from lib import creds, images
from lib.cells import CELLS

# Captured before any test patches `creds.prepare`: `run_matrix.creds` IS this
# module object, so a test that wants the real function has to have kept it.
REAL_PREPARE = creds.prepare


def cell(cell_id):
    """Address a cell by id.

    These tests used to index `CELLS` positionally, so inserting a cell at the
    front silently retargeted a regression test at a different cell — and
    nothing pins the tuple's order.
    """
    return next(c for c in CELLS if c.id == cell_id)


def _argv(call):
    return call.args[0] if call.args else call.kwargs.get("args")


def docker_calls(run, *subcmd):
    """Every mocked docker invocation matching `docker <subcmd...>`.

    Staging turned a cell from two `docker run`s into three plus a `commit`, a
    `rm` and an `rmi`, so a test that pinned behaviour to a position in a
    `side_effect` list would now be asserting about whichever call happened to
    land there. These tests address a call by what it is.
    """
    want = ["docker", *subcmd]
    return [a for a in (_argv(c) for c in run.call_args_list)
            if isinstance(a, list) and a[:len(want)] == want]


def mounted_script(argv):
    """The text of the script this `docker run` mounted, read from the host."""
    spec = next(a for a in argv
                if isinstance(a, str)
                and a.endswith(f"{run_matrix.CONTAINER_SCRIPT}:ro"))
    return Path(spec.rsplit(":", 2)[0]).read_text()


class FakeDocker:
    """Answer `docker run` by reading the script the runner mounted.

    Phase is derived from the script rather than from call order, and the
    cidfile is written because the real docker writes it and `stage_install`
    reads it back to name the container it commits — a fake that skipped it
    would make the commit path untestable.
    """

    def __init__(self, install=("", 0), load=("", 0), trigger=("", 0),
                 commit_rc=0):
        self.install, self.load, self.trigger = install, load, trigger
        self.commit_rc = commit_rc

    @staticmethod
    def _answer(spec, argv):
        """`(stdout, rc)`, `(stdout, stderr, rc)`, or an exception to raise."""
        if isinstance(spec, BaseException):
            raise spec
        out, err, rc = spec if len(spec) == 3 else (spec[0], "", spec[1])
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr=err)

    def __call__(self, argv, **kwargs):
        if argv[:2] == ["docker", "run"]:
            if "--cidfile" in argv:
                path = Path(argv[argv.index("--cidfile") + 1])
                path.write_text(f"cid-{path.stem}")
            text = mounted_script(argv)
            if run_matrix.LIST_MARKER in text:
                return self._answer(self.load, argv)
            if run_matrix.LAUNCH_MARKER in text:
                return self._answer(self.trigger, argv)
            return self._answer(self.install, argv)
        if argv[:2] == ["docker", "commit"]:
            return subprocess.CompletedProcess(argv, self.commit_rc,
                                               stdout="sha256:deadbeef",
                                               stderr="no space left on device")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


# Satisfies every cell's `expect` at once: the two native cells look for the
# plugin id, the other six for `floxify`. A load output that only satisfied one
# of them would quietly turn the other cells' tests into load-failure tests.
GOOD_LOAD = (run_matrix.LIST_MARKER + "\nflox@flox-skills\nfloxify", 0)
GOOD_TRIGGER = (run_matrix.LAUNCH_MARKER + "\npkg-path python312", 0)


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
        run.side_effect = FakeDocker(load=GOOD_LOAD)
        with TemporaryDirectory() as tmp:
            out = run_matrix.run_cell(cell("claude-native"), "img:1", Path(tmp))
        self.assertEqual(out["load"], "pass")

    @patch("run_matrix.subprocess.run")
    def test_installer_chatter_cannot_satisfy_the_load_check(self, run):
        """Regression: skills.sh prints a picker listing 'flox' and 'floxify'
        while installing nothing. Judging the whole transcript passed the cell
        even though `claude plugin list` said 'No plugins installed'.

        Staging the install put that chatter in a different container
        entirely, so the original route is closed — but the slicing is what
        makes the load verdict a statement about `list_cmd` alone, and this
        pins it against anything else the container prints first."""
        transcript = (
            "Found 2 skills\n  flox\n  floxify\n"
            + run_matrix.LIST_MARKER + "\n"
            + "No plugins installed. Use `claude plugin install` to install a plugin.\n"
        )
        run.side_effect = FakeDocker(load=(transcript, 0))
        with TemporaryDirectory() as tmp:
            out = run_matrix.run_cell(cell("claude-npx"), "img:1", Path(tmp))
        self.assertEqual(out["load"], "fail")

    def test_list_output_discards_everything_before_the_marker(self):
        text = f"noise flox\n{run_matrix.LIST_MARKER}\nreal output"
        self.assertEqual(run_matrix.list_output(text).strip(), "real output")

    def test_list_output_is_empty_when_marker_missing(self):
        self.assertEqual(run_matrix.list_output("flox everywhere", "").strip(), "")

    def test_list_output_reads_stderr_too(self):
        """`_emit` writes the marker to BOTH streams so either can be sliced.
        A `list_cmd` that renders on stderr used to record the expected token
        in `load_evidence` and a `fail` beside it."""
        out = run_matrix.list_output("", f"{run_matrix.LIST_MARKER}\nflox@flox-skills")
        self.assertIn("flox@flox-skills", out)

    @patch("run_matrix.subprocess.run")
    def test_load_fails_when_expect_absent(self, run):
        run.side_effect = FakeDocker(load=("no plugins", 0))
        with TemporaryDirectory() as tmp:
            out = run_matrix.run_cell(cell("claude-native"), "img:1", Path(tmp))
        self.assertEqual(out["load"], "fail")
        self.assertEqual(out["trigger"], "skipped")

    @patch("run_matrix.subprocess.run")
    def test_auth_failure_is_reported_distinctly(self, run):
        run.side_effect = FakeDocker(
            load=GOOD_LOAD,
            trigger=("", run_matrix.LAUNCH_MARKER + "\nInvalid API key · "
                         "Please run /login", 1))
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
        # Every phase, not just the one that used to carry the install: the
        # install phase needs it to run `npx` at all and commits the symlink,
        # and the two flox-package cells stage nothing and would never get it.
        for phase in (run_matrix.INSTALL, run_matrix.LOAD, run_matrix.TRIGGER):
            script = run_matrix.cell_script(cell("claude-npx"), phase)
            self.assertIn("/usr/bin/env", script, phase)
            self.assertIn("ln -s", script, phase)

    @patch("run_matrix.subprocess.run")
    def test_load_only_never_launches(self, run):
        run.side_effect = FakeDocker(load=GOOD_LOAD)
        with TemporaryDirectory() as tmp:
            out = run_matrix.run_cell(cell("claude-native"), "img:1", Path(tmp), load_only=True)
            # Install and load, and no launch: relabelling after the fact
            # would still have spent the model call. Asserted on what the
            # containers were told to do rather than on how many there were,
            # so staging the install did not have to weaken it. Inside the
            # temp dir, because that is where the scripts are read back from.
            self.assertEqual(len(docker_calls(run, "run")), 2)
            for argv in docker_calls(run, "run"):
                self.assertNotIn(run_matrix.LAUNCH_MARKER, mounted_script(argv))
        self.assertEqual(out["trigger"], "not-attempted")

    @patch("run_matrix.subprocess.run")
    def test_prompt_placeholder_is_substituted(self, run):
        run.side_effect = FakeDocker(load=GOOD_LOAD, trigger=GOOD_TRIGGER)
        with TemporaryDirectory() as tmp:
            run_matrix.run_cell(cell("claude-native"), "img:1", Path(tmp))
            # One script per phase now, each named for it: a single `cell.sh`
            # rewritten between phases would be diagnosed from whichever one
            # overwrote the others.
            script = (Path(tmp) / f"{run_matrix.TRIGGER}.sh").read_text()
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
        run.side_effect = FakeDocker(
            load=GOOD_LOAD,
            trigger=(run_matrix.LAUNCH_MARKER + "\n" + answer, 0))
        with TemporaryDirectory() as tmp:
            out = run_matrix.run_cell(cell("claude-native"), "img:1", Path(tmp))
        self.assertEqual(out["trigger"], "pass")

    @patch("run_matrix.subprocess.run")
    def test_a_real_auth_failure_still_reports(self, run):
        run.side_effect = FakeDocker(
            load=GOOD_LOAD,
            trigger=("", run_matrix.LAUNCH_MARKER + "\nPlease run /login", 1))
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
        container re-ran the install, and a failing clone prints "fatal:
        Authentication failed" — an AUTH_MARKER. The cell recorded
        "credential problem, not a skill problem" for a repo/network failure.

        Marker slicing fixed the misreading; staging the install closes the
        route. A clone that fails now fails in a container that launches no
        agent, so the auth classifier — which only ever reads the agent's own
        output — cannot be reached by it at all."""
        install_failure = ("Cloning into '/work/flox-skills'...\n"
                           "fatal: Authentication failed for "
                           "'https://github.com/flox/flox-skills.git/'\n")
        with patch("run_matrix.subprocess.run") as run:
            run.side_effect = FakeDocker(install=("", install_failure, 1))
            with TemporaryDirectory() as tmp:
                out = run_matrix.run_cell(cell("codex-native"), "img:1", Path(tmp))
        self.assertEqual(out["load"], "fail")
        self.assertEqual(out["trigger"], "skipped")
        self.assertNotEqual(out["trigger"], "auth-error")
        self.assertIn("Authentication failed", out["load_evidence"])

    def test_an_agent_auth_failure_after_the_marker_still_reports(self):
        with patch("run_matrix.subprocess.run") as run:
            run.side_effect = FakeDocker(
                load=GOOD_LOAD,
                trigger=("", run_matrix.LAUNCH_MARKER + "\nPlease run /login", 1))
            with TemporaryDirectory() as tmp:
                out = run_matrix.run_cell(cell("codex-native"), "img:1", Path(tmp))
        self.assertEqual(out["trigger"], "auth-error")

    def test_installer_chatter_cannot_reach_the_evidence_classifier(self):
        chatter = "pkg-path [services] python312 flox activate\n"
        with patch("run_matrix.subprocess.run") as run:
            run.side_effect = FakeDocker(
                load=GOOD_LOAD,
                trigger=(chatter + run_matrix.LAUNCH_MARKER + "\nhello", 0))
            with TemporaryDirectory() as tmp:
                out = run_matrix.run_cell(cell("claude-native"), "img:1", Path(tmp))
        self.assertEqual(out["evidence_class"], "weak")

    def test_the_launch_marker_is_emitted_on_both_streams(self):
        script = run_matrix.cell_script(cell("claude-native"), run_matrix.TRIGGER)
        self.assertIn(f"echo {run_matrix.LAUNCH_MARKER}", script)
        self.assertIn(f"echo {run_matrix.LAUNCH_MARKER} >&2", script)


class TestVerdictsSurviveEachOther(unittest.TestCase):
    def test_a_trigger_timeout_keeps_a_load_pass(self):
        with patch("run_matrix.subprocess.run") as run:
            run.side_effect = FakeDocker(
                load=GOOD_LOAD,
                trigger=subprocess.TimeoutExpired(cmd="docker", timeout=600))
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

    def test_a_load_timeout_does_not_claim_the_trigger_timed_out(self):
        """The trigger container never started, so `timeout` was a verdict
        about something that did not happen. `skipped` is the word this file
        already owns for "the load half did not pass, so the trigger was not
        attempted"."""
        with patch("run_matrix.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=600)):
            with TemporaryDirectory() as tmp:
                out = run_matrix.run_cell(cell("claude-native"), "img:1", Path(tmp))
        self.assertEqual(out["trigger"], "skipped")

    def test_a_load_crash_does_not_claim_the_trigger_errored(self):
        with patch("run_matrix.subprocess.run", side_effect=RuntimeError("boom")):
            with TemporaryDirectory() as tmp:
                out = run_matrix.run_cell(cell("claude-native"), "img:1", Path(tmp))
        self.assertEqual(out["load"], "error")
        self.assertEqual(out["trigger"], "skipped")

    def test_a_trigger_crash_keeps_a_load_pass(self):
        with patch("run_matrix.subprocess.run") as run:
            run.side_effect = FakeDocker(load=GOOD_LOAD,
                                         trigger=RuntimeError("boom"))
            with TemporaryDirectory() as tmp:
                out = run_matrix.run_cell(cell("claude-native"), "img:1", Path(tmp))
        self.assertEqual(out["load"], "pass")
        self.assertEqual(out["trigger"], "error")

    def test_both_halves_keep_their_own_transcript(self):
        with patch("run_matrix.subprocess.run") as run:
            run.side_effect = FakeDocker(
                load=(run_matrix.LIST_MARKER + "\nflox@flox-skills plugin listed", 0),
                trigger=(run_matrix.LAUNCH_MARKER + "\npkg-path [services]", 0))
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
                [], 0, stdout=run_matrix.LIST_MARKER + "\nflox@flox-skills", stderr="")
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

    def _run_main_with(self, rows, argv, leaked=None, build=None, prepare=None,
                       results_dir=None):
        """Drive `main` with the containers mocked out.

        `cleanup_run_dir` is stubbed, and it is the only thing that removes
        `main`'s `mkdtemp` — so the run directory is redirected into the test's
        own temp tree rather than abandoned under /tmp.

        `prepare` is a side effect for `creds.prepare`, and it is why several
        of this file's blocking defects survived a 97-test suite: the harness
        always stubbed it to a no-op, so no test here could express "credential
        preparation failed", and the only reachable image failure was the
        synthetic `BuildError` the code already caught. A mock that can only
        produce the exception the code handles can never fail.

        `results_dir` lets a caller seed and then read the day's results file.
        """
        with TemporaryDirectory() as tmp:
            results = Path(results_dir) if results_dir else Path(tmp) / "results"
            runs = Path(tmp) / "run"
            runs.mkdir()
            with patch("run_matrix.RESULTS", results), \
                 patch("run_matrix.tempfile.mkdtemp", return_value=str(runs)), \
                 patch("run_matrix.images.build",
                       side_effect=build, return_value="img:1"), \
                 patch("run_matrix.creds.prepare", side_effect=prepare), \
                 patch("run_matrix.cleanup_run_dir", return_value=leaked or []), \
                 patch("run_matrix.run_cell", side_effect=rows):
                return self._main(argv)

    @staticmethod
    def _green():
        """Rows in the shape `run_cell` really returns — see the contract test."""
        return [{"cell": c.id, "agent": c.agent,
                 "install_method": c.install_method, "image": c.image,
                 "load": "pass", "trigger": "pass", "model": "",
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

    def test_a_missing_docker_binary_exits_5_not_1(self):
        """`subprocess.run` raises before any `CompletedProcess` exists, so
        `build()` never got to construct a `BuildError` and this tracebacked
        out of `main` with 1 — "a cell did not come out green" — on a machine
        that had simply never installed docker."""
        rc = self._run_main_with(
            self._green(), [],
            build=FileNotFoundError(2, "No such file or directory", "docker"))
        self.assertEqual(rc, 5)

    @staticmethod
    def _real_prepare(claude_src, codex_src):
        """A side effect that runs the REAL `creds.prepare` on given sources.

        Handing `main` a pre-cooked exception would only test the `except`
        clause it already had. These two cases are about what `prepare` itself
        lets escape, so the real function has to run.
        """
        return lambda dest, **kw: REAL_PREPARE(dest, claude_src, codex_src)

    def test_a_malformed_credential_file_exits_5_not_1(self):
        """A half-written `~/.claude/.credentials.json` is a realistic state on
        a machine where Claude Code is refreshing tokens concurrently, which is
        exactly when someone runs this. `JSONDecodeError` is not
        `CredentialError`, so it used to escape `prepare` and traceback out."""
        with TemporaryDirectory() as tmp:
            bad = Path(tmp) / "claude.json"
            bad.write_text('{"claudeAiOauth": ')       # truncated mid-refresh
            ok = Path(tmp) / "codex.json"
            ok.write_text('{"auth_mode": "chatgpt"}')
            rc = self._run_main_with(self._green(), [],
                                     prepare=self._real_prepare(bad, ok))
        self.assertEqual(rc, 5)

    def test_an_unreadable_credential_file_exits_5_not_1(self):
        if os.geteuid() == 0:
            self.skipTest("root reads a mode-000 path, so this state is "
                          "unconstructible here")
        with TemporaryDirectory() as tmp:
            locked = Path(tmp) / "claude.json"
            locked.write_text("{}")
            os.chmod(locked, 0)
            ok = Path(tmp) / "codex.json"
            ok.write_text('{"auth_mode": "chatgpt"}')
            try:
                rc = self._run_main_with(self._green(), [],
                                         prepare=self._real_prepare(locked, ok))
            finally:
                os.chmod(locked, 0o600)
        self.assertEqual(rc, 5)

    def test_a_credential_file_that_is_not_an_object_exits_5(self):
        with TemporaryDirectory() as tmp:
            wrong = Path(tmp) / "claude.json"
            wrong.write_text('["not", "an", "object"]')
            ok = Path(tmp) / "codex.json"
            ok.write_text('{"auth_mode": "chatgpt"}')
            rc = self._run_main_with(self._green(), [],
                                     prepare=self._real_prepare(wrong, ok))
        self.assertEqual(rc, 5)

    def test_a_credential_leak_outranks_an_all_auth_error_run(self):
        """3-over-1 was pinned; 3-over-4 was not, and 4 is the code an
        all-credentials run reaches."""
        rows = [dict(r, trigger="auth-error") for r in self._green()]
        rc = self._run_main_with(rows, [], leaked=["/tmp/x/codex/auth.json"])
        self.assertEqual(rc, 3)

    def test_a_credential_leak_outranks_a_failed_start(self):
        """`return 5` fixed the return value before the `finally` ran, so the
        `rc = 3` assignment after `cleanup_run_dir` was dead on this path —
        and it is the easiest path to reach: `prepare` writes the Claude copy
        BEFORE it validates the Codex one, so a run that fails validation has
        already put a credential on disk. The run printed the WARNING and
        exited 5: human-visible, and not machine-readable."""
        rc = self._run_main_with(
            self._green(), [],
            prepare=run_matrix.creds.CredentialError("carries OPENAI_API_KEY"),
            leaked=["/tmp/x/claude/.credentials.json"])
        self.assertEqual(rc, 3)

    def test_a_failed_credential_prepare_runs_no_cell(self):
        """`side_effect` is a one-shot list: a cell that ran would consume it
        and this would raise StopIteration rather than return."""
        rc = self._run_main_with(
            [], [], prepare=run_matrix.creds.CredentialError("no login"))
        self.assertEqual(rc, 5)

    def test_a_load_only_run_does_not_destroy_a_measured_trigger(self):
        """The blocking case: `--version` defaults to today, so the
        `--load-only` run the README recommends as the cheap next step was the
        run that erased the authenticated evidence it would be checked
        against."""
        with TemporaryDirectory() as tmp:
            results = Path(tmp) / "results"
            out = results / "20260826.jsonl"
            run_matrix.write_results(out, [{
                "cell": "claude-native", "load": "pass", "trigger": "pass",
                "evidence_class": "answer-shaped",
                "trigger_evidence": "MEASURED", "notes": ""}])
            row = dict(self._green()[0], trigger="not-attempted",
                       evidence_class="", trigger_evidence="",
                       notes="--load-only")
            rc = self._run_main_with(
                [row], ["--load-only", "--cells", "claude-native",
                        "--version", "20260826"], results_dir=results)
            kept = json.loads(out.read_text().splitlines()[0])
        self.assertEqual(rc, 0)
        self.assertEqual(kept["trigger"], "pass")
        self.assertEqual(kept["evidence_class"], "answer-shaped")
        self.assertEqual(kept["trigger_evidence"], "MEASURED")
        self.assertEqual(kept["load"], "pass")   # this run DID measure the load half

    def test_every_finished_cell_is_on_disk_before_the_next_one_starts(self):
        """A full authenticated run is sixteen containers over hours. Writing
        once after the loop meant an interrupt at cell three discarded the two
        already paid for in rate limit."""
        rows = self._green()
        boom = RuntimeError("interrupted")
        with TemporaryDirectory() as tmp:
            results = Path(tmp) / "results"
            with self.assertRaises(RuntimeError):
                self._run_main_with(rows[:2] + [boom], [], results_dir=results)
            written = [json.loads(x)["cell"]
                       for x in (results / f"{run_matrix.datetime.now(run_matrix.timezone.utc).strftime('%Y%m%d')}.jsonl").read_text().splitlines()]
        self.assertEqual(written, [rows[0]["cell"], rows[1]["cell"]])

    def test_a_non_positive_timeout_is_an_argument_error(self):
        """`--cells` and `--version` are both exit 2 in this function; a
        `--timeout 0` turned every cell into a `timeout` verdict instead,
        indistinguishable in the results file from a real hang."""
        self.assertEqual(self._main(["--dry-run", "--timeout", "0"]), 2)
        self.assertEqual(self._main(["--dry-run", "--timeout", "-5"]), 2)

    def test_a_dry_run_prints_the_plan(self):
        """Five surfaces promise it and nothing rendered it: the shell was
        computed into `row["notes"]` and `summarize` printed four columns.
        `test_a_dry_run_writes_no_results` pins the absence of the file; this
        pins the presence of the plan."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(io.StringIO()):
            run_matrix.main(["--dry-run", "--cells", "claude-native"])
        out = buf.getvalue()
        self.assertIn("claude plugin list", out)
        self.assertIn("claude -p", out)          # the trigger half too


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
                "model", "evidence_class", "load_evidence", "trigger_evidence",
                "notes"}

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
                    [], 0, stdout=run_matrix.LIST_MARKER + "\nflox@flox-skills", stderr=""),
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


class TestContainerReclamation(unittest.TestCase):
    """A timeout must not leave a root container holding the OAuth mounts.

    `subprocess.run(timeout=)` SIGKILLs the docker CLI; the container is a
    child of the daemon, `--sig-proxy` cannot forward SIGKILL, and `--rm` fires
    only when the container exits. The run then swept the host directory, found
    it empty, and reported no leak while the tokens were still readable inside
    a live container.
    """

    def test_every_run_names_its_container(self):
        cmd = run_matrix.docker_cmd("img:1", Path("/tmp/run"),
                                    Path("/tmp/run/cell.sh"),
                                    mount_credentials=True,
                                    cidfile=Path("/tmp/run/trigger.cid"))
        self.assertIn("--cidfile", cmd)
        self.assertIn("/tmp/run/trigger.cid", cmd)

    def test_a_timeout_kills_the_container_it_started(self):
        with TemporaryDirectory() as tmp:
            work = Path(tmp)
            def fake_run(argv, **kw):
                if argv[:2] == ["docker", "run"]:
                    # docker writes the cidfile before the container starts.
                    Path(argv[argv.index("--cidfile") + 1]).write_text("deadbeef")
                    raise subprocess.TimeoutExpired(cmd="docker", timeout=1)
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
            with patch("run_matrix.subprocess.run", side_effect=fake_run) as run:
                out = run_matrix.run_cell(cell("claude-native"), "img:1", work)
            killed = [a[0][0] for a in run.call_args_list
                      if a[0][0][:2] in (["docker", "kill"], ["docker", "rm"])]
        self.assertEqual(out["load"], "timeout")
        self.assertTrue(any("deadbeef" in c for c in killed), killed)

    def test_a_container_that_cannot_be_reclaimed_is_a_leak(self):
        """`cleanup_run_dir` returning `[]` is an assertion about the host
        filesystem only. A container docker still reports as running holds the
        mount, so it belongs in the same alarm."""
        with TemporaryDirectory() as tmp:
            work = Path(tmp) / "cell"
            work.mkdir()
            (work / "trigger.cid").write_text("cafe1234")
            def fake_run(argv, **kw):
                if argv[:2] == ["docker", "inspect"]:
                    return subprocess.CompletedProcess(argv, 0, stdout="true\n", stderr="")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            with patch("run_matrix.subprocess.run", side_effect=fake_run):
                alive = run_matrix.reclaim_containers(Path(tmp))
        self.assertEqual(len(alive), 1)
        self.assertIn("cafe1234", alive[0])

    def test_a_daemon_that_cannot_be_asked_is_not_a_clean_bill(self):
        """An unanswered question is not an answer — the same rule the runner
        applies with `NOT_RUN` versus `dry-run`."""
        with TemporaryDirectory() as tmp:
            work = Path(tmp) / "cell"
            work.mkdir()
            (work / "load.cid").write_text("beef5678")
            with patch("run_matrix.subprocess.run",
                       side_effect=OSError("daemon gone")):
                alive = run_matrix.reclaim_containers(Path(tmp))
        self.assertEqual(len(alive), 1)

    def test_a_container_docker_says_is_gone_is_not_a_leak(self):
        with TemporaryDirectory() as tmp:
            work = Path(tmp) / "cell"
            work.mkdir()
            (work / "load.cid").write_text("abc999")
            def fake_run(argv, **kw):
                if argv[:2] == ["docker", "inspect"]:
                    return subprocess.CompletedProcess(argv, 1, stdout="", stderr="No such object")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            with patch("run_matrix.subprocess.run", side_effect=fake_run):
                alive = run_matrix.reclaim_containers(Path(tmp))
        self.assertEqual(alive, [])


class TestLeakScan(unittest.TestCase):
    def test_an_unreadable_directory_is_a_positive(self):
        """`Path.rglob` silently skips the contents of a directory it cannot
        read — measured on CPython 3.14.4 — and containers write into these
        trees as root, so the alarm was failing open in exactly the state it
        exists for."""
        if os.geteuid() == 0:
            self.skipTest("root reads a mode-000 path, so this state is "
                          "unconstructible here")
        with TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            hidden = run_dir / "cell" / "claude"
            hidden.mkdir(parents=True)
            (hidden / ".credentials.json").write_text("{}")
            os.chmod(hidden, 0)
            try:
                with patch("run_matrix.shutil.rmtree"), \
                     patch("run_matrix.subprocess.run",
                           return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")):
                    leaked = run_matrix.cleanup_run_dir(run_dir, "img:1")
            finally:
                os.chmod(hidden, 0o700)
        self.assertTrue(leaked, "an unreadable directory must not read as clean")

    def test_the_filename_allowlist_comes_from_creds(self):
        """It used to be a literal tuple four sites away from the module that
        owns the names, and this is the site whose failure mode is exit 0."""
        self.assertEqual(creds.CREDENTIAL_FILENAMES,
                         frozenset(s.filename for s in creds.STORES))


class TestOpenCodeModel(unittest.TestCase):
    """`--opencode-model`: the opt-in that gives the OpenCode cells a model
    they can be held to, instead of the no-login provider the shipped build
    falls back to — which answers about half the time and cannot be pinned."""

    def test_the_flag_reaches_only_opencode_cells(self):
        """Claude Code and Codex take `--model` with a different vocabulary,
        so one flag must not become three silently different meanings."""
        model = "openrouter/z-ai/glm-5.3-flash"
        self.assertEqual(run_matrix.model_flag(cell("opencode-npx"), model),
                         f" --model {model}")
        for cid in ("claude-npx", "codex-native", "claude-flox-ai"):
            self.assertEqual(run_matrix.model_flag(cell(cid), model), "")

    def test_no_flag_leaves_every_launch_as_it_was(self):
        for c in CELLS:
            self.assertEqual(run_matrix.model_flag(c, None), "")
            self.assertNotIn("--model",
                             run_matrix.cell_script(c, run_matrix.TRIGGER))

    def test_an_opencode_launch_carries_the_model(self):
        script = run_matrix.cell_script(cell("opencode-flox-ai"),
                                        run_matrix.TRIGGER,
                                        model="openrouter/z-ai/glm-5.3-flash")
        self.assertIn("--model openrouter/z-ai/glm-5.3-flash", script)

    def test_the_config_registers_the_model_under_its_provider(self):
        """OpenCode 1.18.8 bakes its catalogue in at build time and stops at
        `z-ai/glm-5.2`; an unregistered id fails with `UnknownError:
        Unexpected server error`, which reads as a provider outage. The
        provider is the first path segment and the model keeps the rest —
        OpenRouter ids carry a slash of their own."""
        cfg = run_matrix.opencode_config("openrouter/z-ai/glm-5.3-flash")
        self.assertEqual(cfg["model"], "openrouter/z-ai/glm-5.3-flash")
        self.assertIn("z-ai/glm-5.3-flash", cfg["provider"]["openrouter"]["models"])

    def test_a_model_without_a_provider_is_rejected(self):
        self.assertEqual(self._main(["--dry-run", "--opencode-model",
                                     "glm-5.3-flash"]), 2)

    def test_a_valid_model_is_accepted(self):
        self.assertEqual(self._main(["--dry-run", "--cells", "opencode-npx",
                                     "--opencode-model",
                                     "openrouter/z-ai/glm-5.3-flash"]), 0)

    @staticmethod
    def _main(argv):
        with patch("sys.stderr", new=io.StringIO()), \
             patch("sys.stdout", new=io.StringIO()):
            return run_matrix.main(argv)

    def test_the_dry_run_plan_names_the_model(self):
        """Five surfaces promise a dry run prints what each cell would run,
        and the model is the part that costs money."""
        row = run_matrix.run_cell(cell("opencode-npx"), "img:1", Path("/tmp/x"),
                                  dry_run=True,
                                  model="openrouter/z-ai/glm-5.3-flash")
        self.assertIn("--model openrouter/z-ai/glm-5.3-flash", row["notes"])

    def test_the_row_records_which_model_answered(self):
        """Same cell id and same `answer-shaped` either way; without this the
        free run and the paid one are indistinguishable on disk, and
        merge-by-cell-id lets either stand in for the other."""
        paid = run_matrix.run_cell(cell("opencode-npx"), "img:1", Path("/tmp/x"),
                                   dry_run=True,
                                   model="openrouter/z-ai/glm-5.3-flash")
        free = run_matrix.run_cell(cell("opencode-npx"), "img:1", Path("/tmp/x"),
                                   dry_run=True)
        other = run_matrix.run_cell(cell("claude-npx"), "img:1", Path("/tmp/x"),
                                    dry_run=True,
                                    model="openrouter/z-ai/glm-5.3-flash")
        self.assertEqual(paid["model"], "openrouter/z-ai/glm-5.3-flash")
        self.assertEqual(free["model"], "")
        self.assertEqual(other["model"], "")

    def test_the_credential_mounts_where_opencode_reads(self):
        cmd = run_matrix.docker_cmd("img:1", Path("/tmp/run"),
                                    Path("/tmp/run/cell.sh"),
                                    mount_credentials=True, agent="opencode",
                                    stores=creds.active_stores(True))
        self.assertIn("/tmp/run/opencode:/root/.local/share/opencode:rw", cmd)

    def test_without_the_flag_an_opencode_cell_still_receives_nothing(self):
        """The default path is unchanged, and this is the assertion that says
        so: the opt-in store must not arrive because it merely exists."""
        cmd = run_matrix.docker_cmd("img:1", Path("/tmp/run"),
                                    Path("/tmp/run/cell.sh"),
                                    mount_credentials=True, agent="opencode")
        joined = " ".join(cmd)
        self.assertNotIn("/root/.local/share/opencode", joined)
        self.assertNotIn("/root/.claude", joined)
        self.assertNotIn("/root/.codex", joined)

    def test_the_config_mounts_read_only_and_only_on_the_trigger(self):
        """Nothing in the container has business editing which model the run
        is measuring, and the load half does not launch an agent at all."""
        with patch("run_matrix.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            with TemporaryDirectory() as tmp:
                run_matrix._run(cell("opencode-npx"), "img:1", Path(tmp),
                                run_matrix.TRIGGER, timeout=1,
                                model="openrouter/z-ai/glm-5.3-flash")
                launch = " ".join(run.call_args[0][0])
                run_matrix._run(cell("opencode-npx"), "img:1", Path(tmp),
                                run_matrix.LOAD, timeout=1,
                                model="openrouter/z-ai/glm-5.3-flash")
                load = " ".join(run.call_args[0][0])
                run_matrix._run(cell("opencode-npx"), "img:1", Path(tmp),
                                run_matrix.INSTALL, timeout=1,
                                model="openrouter/z-ai/glm-5.3-flash")
                install = " ".join(run.call_args[0][0])
        self.assertIn(f"{run_matrix.CONTAINER_OPENCODE_CONFIG}:ro", launch)
        self.assertNotIn(run_matrix.CONTAINER_OPENCODE_CONFIG, load)
        self.assertNotIn(run_matrix.CONTAINER_OPENCODE_CONFIG, install)


class TestMergeRow(unittest.TestCase):
    def test_an_unattempted_trigger_does_not_overwrite_a_measured_one(self):
        prior = {"cell": "a", "load": "pass", "trigger": "pass",
                 "evidence_class": "answer-shaped", "trigger_evidence": "MEASURED"}
        new = {"cell": "a", "load": "pass", "trigger": "not-attempted",
               "evidence_class": "", "trigger_evidence": "", "notes": "--load-only"}
        merged = run_matrix.merge_row(prior, new)
        self.assertEqual(merged["trigger"], "pass")
        self.assertEqual(merged["evidence_class"], "answer-shaped")
        self.assertEqual(merged["trigger_evidence"], "MEASURED")

    def test_the_kept_verdict_keeps_the_model_that_produced_it(self):
        """`model` says which model answered, so it travels with the verdict.

        A same-day `--load-only` rerun is run without `--opencode-model` and
        so records `""` — the agent's own default, which for OpenCode is the
        free no-login provider. Left out of `TRIGGER_FIELDS`, the merge kept
        the pinned run's `pass`/`answer-shaped` and took the rerun's empty
        `model` beside it, relabelling a paid, reproducible verdict as one the
        free provider produced.
        """
        prior = {"cell": "opencode-npx", "load": "pass", "trigger": "pass",
                 "evidence_class": "answer-shaped",
                 "trigger_evidence": "MEASURED",
                 "model": "openrouter/z-ai/glm-5.3-flash"}
        new = {"cell": "opencode-npx", "load": "pass",
               "trigger": "not-attempted", "evidence_class": "",
               "trigger_evidence": "", "model": "", "notes": "--load-only"}
        merged = run_matrix.merge_row(prior, new)
        self.assertEqual(merged["model"], "openrouter/z-ai/glm-5.3-flash")

    def test_a_measured_trigger_overwrites_the_model_too(self):
        """The other direction: a run that DID measure names its own model."""
        prior = {"cell": "opencode-npx", "load": "pass", "trigger": "pass",
                 "evidence_class": "answer-shaped",
                 "model": "openrouter/z-ai/glm-5.3-flash"}
        new = {"cell": "opencode-npx", "load": "pass", "trigger": "pass",
               "evidence_class": "answer-shaped", "model": ""}
        self.assertEqual(run_matrix.merge_row(prior, new)["model"], "")

    def test_a_measured_load_still_overwrites(self):
        """The load half is what a `--load-only` run went to measure."""
        prior = {"cell": "a", "load": "pass", "trigger": "pass"}
        new = {"cell": "a", "load": "fail", "trigger": "not-attempted"}
        self.assertEqual(run_matrix.merge_row(prior, new)["load"], "fail")

    def test_a_measured_trigger_overwrites_a_measured_trigger(self):
        prior = {"cell": "a", "load": "pass", "trigger": "pass",
                 "evidence_class": "answer-shaped"}
        new = {"cell": "a", "load": "pass", "trigger": "fail",
               "evidence_class": "weak"}
        merged = run_matrix.merge_row(prior, new)
        self.assertEqual(merged["trigger"], "fail")
        self.assertEqual(merged["evidence_class"], "weak")

    def test_a_never_run_trigger_does_not_overwrite_either(self):
        prior = {"cell": "a", "trigger": "pass"}
        new = {"cell": "a", "trigger": run_matrix.NOT_RUN}
        self.assertEqual(run_matrix.merge_row(prior, new)["trigger"], "pass")

    def test_the_first_row_for_a_cell_is_kept_whole(self):
        new = {"cell": "a", "trigger": "not-attempted"}
        self.assertEqual(run_matrix.merge_row({}, new)["trigger"], "not-attempted")


class TestPerAgentMounts(unittest.TestCase):
    """Every trigger container used to get BOTH OAuth stores, so `npx --yes
    skills add` on a Codex cell ran as root with the live Claude subscription
    token readable beside it."""

    def test_a_codex_cell_does_not_receive_the_claude_token(self):
        cmd = run_matrix.docker_cmd("img:1", Path("/tmp/run"),
                                    Path("/tmp/run/cell.sh"),
                                    mount_credentials=True, agent="codex")
        joined = " ".join(cmd)
        self.assertIn("/tmp/run/codex:/root/.codex:rw", cmd)
        self.assertNotIn("/root/.claude", joined)

    def test_an_opencode_cell_receives_neither(self):
        """The shipped OpenCode resolves credentials only from
        `$XDG_DATA_HOME/opencode/auth.json` (else
        `~/.local/share/opencode/auth.json`) — verified by binary scan against
        1.18.23, where `claudeAiOauth` appears zero times — so it consumed
        neither mounted directory even when both were handed to it."""
        cmd = run_matrix.docker_cmd("img:1", Path("/tmp/run"),
                                    Path("/tmp/run/cell.sh"),
                                    mount_credentials=True, agent="opencode")
        joined = " ".join(cmd)
        self.assertNotIn("/root/.claude", joined)
        self.assertNotIn("/root/.codex", joined)
        self.assertIn("/prompt.txt", joined)

    def test_run_cell_gates_the_mount_on_the_cells_own_agent(self):
        with patch("run_matrix.subprocess.run") as run:
            run.side_effect = FakeDocker(
                load=(run_matrix.LIST_MARKER + "\nfloxify", 0),
                trigger=(run_matrix.LAUNCH_MARKER + "\npkg-path [services]", 0))
            with TemporaryDirectory() as tmp:
                run_matrix.run_cell(cell("codex-npx"), "img:1", Path(tmp))
                # Addressed by what the container was told to do: with three
                # containers per cell, a positional index names whichever call
                # happened to land there.
                launch = " ".join(
                    argv for run_argv in docker_calls(run, "run")
                    if run_matrix.LAUNCH_MARKER in mounted_script(run_argv)
                    for argv in run_argv)
        self.assertIn("/root/.codex", launch)
        self.assertNotIn("/root/.claude", launch)


class TestStagedInstall(unittest.TestCase):
    """The installer must never run in a container that holds a credential.

    Review point from David Sawyer on PR #95: the trigger container re-ran the
    install, so `npx --yes skills add` — unpinned code fetched from npm at run
    time, running as root — executed in the one container with a live OAuth
    token mounted beside it. The install now happens once, credential-free, and
    is committed to an image the other two halves run from.
    """

    def test_no_container_both_installs_and_mounts_a_credential(self):
        """The property this whole change exists to establish, over every cell."""
        for c in CELLS:
            with self.subTest(cell=c.id), TemporaryDirectory() as tmp:
                with patch("run_matrix.subprocess.run") as run:
                    run.side_effect = FakeDocker(load=GOOD_LOAD,
                                                 trigger=GOOD_TRIGGER)
                    run_matrix.run_cell(c, "agent-compat-base:v1", Path(tmp),
                                        version="v1")
                for argv in docker_calls(run, "run"):
                    mounts_credential = any(
                        f"{run_matrix.CONTAINER_HOME}/{s.container_dir}" in a
                        for a in argv if isinstance(a, str)
                        for s in creds.STORES)
                    if mounts_credential and c.install:
                        self.assertNotIn(c.install, mounted_script(argv))

    def test_the_trigger_script_carries_no_install(self):
        for c in CELLS:
            if not c.install:
                continue
            with self.subTest(cell=c.id):
                self.assertNotIn(
                    c.install, run_matrix.cell_script(c, run_matrix.TRIGGER))

    def test_the_install_script_is_the_install_and_nothing_else(self):
        script = run_matrix.cell_script(cell("claude-npx"), run_matrix.INSTALL)
        self.assertIn("npx --yes skills add", script)
        self.assertNotIn(run_matrix.LAUNCH_MARKER, script)
        self.assertNotIn(run_matrix.LIST_MARKER, script)

    def test_the_install_container_is_kept_so_it_can_be_committed(self):
        keep = run_matrix.docker_cmd("img:1", Path("/tmp/run"), Path("/tmp/s.sh"),
                                     mount_credentials=False, remove=False)
        self.assertNotIn("--rm", keep)
        self.assertIn("--rm", run_matrix.docker_cmd(
            "img:1", Path("/tmp/run"), Path("/tmp/s.sh"), mount_credentials=False))

    @patch("run_matrix.subprocess.run")
    def test_load_and_trigger_run_from_the_staged_image(self, run):
        run.side_effect = FakeDocker(load=GOOD_LOAD, trigger=GOOD_TRIGGER)
        with TemporaryDirectory() as tmp:
            out = run_matrix.run_cell(cell("claude-native"),
                                      "agent-compat-base:v1", Path(tmp),
                                      version="v1")
        staged = run_matrix.staged_tag("v1", "claude-native")
        runs = docker_calls(run, "run")
        self.assertEqual(len(runs), 3)
        self.assertIn("agent-compat-base:v1", runs[0])
        self.assertIn(staged, runs[1])
        self.assertIn(staged, runs[2])
        self.assertEqual(out["trigger"], "pass")

    @patch("run_matrix.subprocess.run")
    def test_the_install_container_is_committed_then_removed(self, run):
        run.side_effect = FakeDocker(load=GOOD_LOAD, trigger=GOOD_TRIGGER)
        with TemporaryDirectory() as tmp:
            run_matrix.run_cell(cell("claude-native"), "agent-compat-base:v1",
                                Path(tmp), version="v1")
        staged = run_matrix.staged_tag("v1", "claude-native")
        self.assertEqual(docker_calls(run, "commit"),
                         [["docker", "commit", "cid-install", staged]])
        self.assertIn(["docker", "rm", "cid-install"], docker_calls(run, "rm"))

    @patch("run_matrix.subprocess.run")
    def test_the_staged_image_is_removed_when_the_cell_ends(self, run):
        run.side_effect = FakeDocker(load=GOOD_LOAD, trigger=GOOD_TRIGGER)
        with TemporaryDirectory() as tmp:
            run_matrix.run_cell(cell("claude-native"), "agent-compat-base:v1",
                                Path(tmp), version="v1")
        self.assertIn(
            ["docker", "rmi", "-f", run_matrix.staged_tag("v1", "claude-native")],
            docker_calls(run, "rmi"))

    @patch("run_matrix.subprocess.run")
    def test_a_failed_install_is_a_red_load_not_a_crash(self, run):
        run.side_effect = FakeDocker(install=("npm ERR! 404 skills", 1))
        with TemporaryDirectory() as tmp:
            out = run_matrix.run_cell(cell("claude-npx"), "agent-compat-base:v1",
                                      Path(tmp), version="v1")
        self.assertEqual(out["load"], "fail")
        self.assertEqual(out["trigger"], "skipped")
        # Nothing was committed, so nothing ran against a half-installed image.
        self.assertEqual(docker_calls(run, "commit"), [])
        # And the reason survives: a red cell whose transcript was thrown away
        # is the failure mode the marker rules were written against.
        self.assertIn("npm ERR!", out["load_evidence"])

    @patch("run_matrix.subprocess.run")
    def test_a_commit_failure_does_not_become_a_pass(self, run):
        run.side_effect = FakeDocker(load=GOOD_LOAD, trigger=GOOD_TRIGGER,
                                     commit_rc=1)
        with TemporaryDirectory() as tmp:
            out = run_matrix.run_cell(cell("claude-native"),
                                      "agent-compat-base:v1", Path(tmp),
                                      version="v1")
        self.assertEqual(out["load"], "error")
        self.assertEqual(out["trigger"], "skipped")
        self.assertIn("commit", out["notes"])

    @patch("run_matrix.subprocess.run")
    def test_a_cell_with_nothing_to_install_is_not_staged(self, run):
        run.side_effect = FakeDocker(load=GOOD_LOAD, trigger=GOOD_TRIGGER)
        with TemporaryDirectory() as tmp:
            out = run_matrix.run_cell(cell("claude-flox-ai"),
                                      "agent-compat-withpkg:v1", Path(tmp),
                                      version="v1")
        self.assertEqual(len(docker_calls(run, "run")), 2)
        self.assertEqual(docker_calls(run, "commit"), [])
        self.assertEqual(docker_calls(run, "rmi"), [])
        for argv in docker_calls(run, "run"):
            self.assertIn("agent-compat-withpkg:v1", argv)
        self.assertEqual(out["trigger"], "pass")

    @patch("run_matrix.subprocess.run")
    def test_load_only_stages_and_stays_credential_free(self, run):
        run.side_effect = FakeDocker(load=GOOD_LOAD)
        with TemporaryDirectory() as tmp:
            out = run_matrix.run_cell(cell("claude-native"),
                                      "agent-compat-base:v1", Path(tmp),
                                      load_only=True, version="v1")
        self.assertEqual(out["trigger"], "not-attempted")
        runs = docker_calls(run, "run")
        self.assertEqual(len(runs), 2)          # install, then load
        for argv in runs:
            self.assertNotIn("/root/.claude", " ".join(a for a in argv
                                                       if isinstance(a, str)))

    @patch("run_matrix.subprocess.run")
    def test_an_install_timeout_is_recorded_against_the_load_half(self, run):
        """The install belongs to the credential-free half, so a hang there is
        that half's verdict — not a `timeout` written against a trigger
        container that never started."""
        run.side_effect = FakeDocker(
            install=subprocess.TimeoutExpired(cmd="docker", timeout=600))
        with TemporaryDirectory() as tmp:
            out = run_matrix.run_cell(cell("claude-native"), "img:1", Path(tmp),
                                      version="v1")
        self.assertEqual(out["load"], "timeout")
        self.assertEqual(out["trigger"], "skipped")

    @patch("run_matrix.subprocess.run")
    def test_the_sweep_removes_only_this_runs_staged_images(self, run):
        run.side_effect = lambda argv, **kw: subprocess.CompletedProcess(
            argv, 0,
            stdout=("agent-compat-staged:v1-claude-native\n"
                    "agent-compat-staged:v2-codex-npx\n"),
            stderr="")
        self.assertEqual(run_matrix.surviving_staged_images("v1"), [])
        removed = [a for a in docker_calls(run, "rmi")]
        self.assertEqual(removed,
                         [["docker", "rmi", "-f", "agent-compat-staged:v1-claude-native"]])

    def test_the_dry_run_plan_names_all_three_phases(self):
        with TemporaryDirectory() as tmp:
            with patch("run_matrix.subprocess.run") as run:
                out = run_matrix.run_cell(cell("claude-native"), "img:1",
                                          Path(tmp), dry_run=True, version="v1")
                run.assert_not_called()
        for phase in (run_matrix.INSTALL, run_matrix.LOAD, run_matrix.TRIGGER):
            self.assertIn(f"{phase}:", out["notes"])


if __name__ == "__main__":
    unittest.main()
