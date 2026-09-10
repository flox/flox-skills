#!/usr/bin/env python3
"""Behaviour of the pre-commit hook (AI-512 layer L1).

The hook is shell, so these drive the real file in a throwaway git repo
rather than asserting on its text. What matters is the shape of its
decisions, and each one is a way it could be quietly wrong:

  - fires only when a skill file is staged
  - a real drift finding blocks the commit
  - a missing flox or python3 skips instead of blocking

The last is the one worth a test. A hook that blocks a commit because a
tool it wanted is absent trains people to pass --no-verify by reflex, and
then it is not a gate any more.

Run from the suite root (`evals/drift/`):
    python3 -m unittest tests.test_hook -v
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUITE = HERE.parent
REPO_ROOT = SUITE.parent.parent
HOOK = REPO_ROOT / ".githooks" / "pre-commit"


def _git(*args, cwd, **kw):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, **kw)


class HookHarness(unittest.TestCase):
    """A real git repo carrying the real hook, the real checker, and a
    fake `flox` whose --help output the test controls."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / "evals" / "drift" / "tasks").mkdir(parents=True)
        (self.repo / "flox-plugin" / "skills" / "flox").mkdir(parents=True)
        (self.repo / ".githooks").mkdir()

        shutil.copy(HOOK, self.repo / ".githooks" / "pre-commit")
        shutil.copy(SUITE / "check.py", self.repo / "evals" / "drift" / "check.py")

        _git("init", "-q", cwd=self.repo)
        _git("config", "user.email", "t@example.com", cwd=self.repo)
        _git("config", "user.name", "T", cwd=self.repo)
        _git("config", "core.hooksPath", ".githooks", cwd=self.repo)

        self.skill = self.repo / "flox-plugin" / "skills" / "flox" / "SKILL.md"
        self.skill.write_text("Use `flox activate -m dev|run` to pick a mode.\n")
        self._write_claims([{
            "id": "activate-mode-flag", "kind": "flag_exists",
            "command": ["activate"], "flag": "--mode",
            "skill_ref": {"file": "skills/flox/SKILL.md",
                          "quote": "flox activate -m dev|run"},
        }])
        # A stub flox on PATH, so no test touches the real CLI.
        self.bin = self.repo / "_bin"
        self.bin.mkdir()
        self._write_flox("    -m, --mode=ARG    pick a mode\n")

    def _write_claims(self, claims):
        p = self.repo / "evals" / "drift" / "tasks" / "claims.jsonl"
        p.write_text("".join(json.dumps(c) + "\n" for c in claims))

    def _write_flox(self, options_body):
        f = self.bin / "flox"
        f.write_text("#!/usr/bin/env sh\ncat <<'EOF'\nAvailable options:\n"
                     + options_body + "EOF\n")
        f.chmod(0o755)

    def _commit(self, env_path=None):
        env = dict(os.environ)
        env["PATH"] = env_path if env_path is not None else \
            f"{self.bin}{os.pathsep}{env['PATH']}"
        return _git("commit", "-m", "wip", cwd=self.repo, env=env)


class TestHookFires(HookHarness):
    def test_a_skill_change_with_a_true_claim_commits(self):
        _git("add", "-A", cwd=self.repo)
        r = self._commit()
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_drifted_claim_blocks_the_commit(self):
        self._write_flox("    -d, --dir=<path>    a dir\n")   # --mode is gone
        _git("add", "-A", cwd=self.repo)
        r = self._commit()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no longer matches", r.stderr)

    def test_the_suggestion_reaches_the_committer(self):
        self._write_flox("    -d, --dir=<path>    a dir\n")
        _git("add", "-A", cwd=self.repo)
        r = self._commit()
        self.assertIn("--dir", r.stdout + r.stderr)

    def test_no_verify_still_commits_through_a_drift(self):
        self._write_flox("    -d, --dir=<path>    a dir\n")
        _git("add", "-A", cwd=self.repo)
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}{os.pathsep}{env['PATH']}"
        r = _git("commit", "--no-verify", "-m", "wip", cwd=self.repo, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)


class TestHookStaysOutOfTheWay(HookHarness):
    def test_a_non_skill_change_does_not_run_the_checker(self):
        # Seed a commit so there is a HEAD, then touch only a README.
        _git("add", "-A", cwd=self.repo)
        self._commit()
        (self.repo / "README.md").write_text("hi\n")
        self._write_flox("    -d, --dir=<path>    a dir\n")   # would drift
        _git("add", "README.md", cwd=self.repo)
        r = self._commit()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("checking its CLI claims", r.stdout)

    def test_missing_flox_skips_rather_than_blocks(self):
        # The failure mode worth guarding: a hook that blocks because a
        # tool is absent teaches people to pass --no-verify by reflex.
        _git("add", "-A", cwd=self.repo)
        r = self._commit(env_path="/usr/bin:/bin")   # no flox anywhere
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("flox not on PATH", r.stderr)

    def test_a_deleted_skill_file_does_not_block(self):
        # --diff-filter=ACMR excludes deletions: there is no line left to
        # check, and the registry's own quote check reports it instead.
        _git("add", "-A", cwd=self.repo)
        self._commit()
        self.skill.unlink()
        _git("add", "-A", cwd=self.repo)
        r = self._commit()
        self.assertEqual(r.returncode, 0, r.stderr)


class TestHookIsInstallable(unittest.TestCase):
    def test_hook_exists_and_is_executable(self):
        self.assertTrue(HOOK.is_file(), f"no hook at {HOOK}")
        self.assertTrue(os.access(HOOK, os.X_OK), "hook is not executable")

    def test_hook_watches_the_directory_the_skills_actually_live_in(self):
        # If skills/ moves, the hook silently stops firing. This fails
        # instead.
        prefix = "flox-plugin/skills/"
        self.assertIn(prefix, HOOK.read_text())
        self.assertTrue((REPO_ROOT / prefix).is_dir(),
                        f"{prefix} does not exist; the hook watches nothing")


if __name__ == "__main__":
    unittest.main()
