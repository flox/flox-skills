#!/usr/bin/env python3
"""Migrate-mode outcome evals: is the CI-wiring guidance actually followed?

The repo policy (evals/README.md) says a PR that changes skill guidance
ships an eval verifying the guidance is followed. The CI-wiring guidance
lives in floxify's migrate step — the [y/N] offer, the detect-and-conform
branches, the never-touch-existing-CI rule — and none of it executes in a
plain `/floxify <dir>` run. This runner drives the skill THROUGH migrate
with a scripted user and grades the three claims the guidance makes:

1. **Consent** — the offer was asked before anything was written
   (`offer_asked`, graded against the agent's OWN text only — tool
   results are excluded, since the guidance file itself contains every
   phrase being grepped for), and a "n" produced no new CI file
   anywhere (`no_new_ci_files`, from a pre/post tree snapshot).
2. **Conform** — the artifact matches the detected CI system: a NEW
   `.github/workflows/flox.yml` for GitHub Actions (`flox_yml_written`,
   `flox_yml_valid`), a proposed snippet — not an edit — for single-file
   systems (`snippet_proposed`), a which-CI question when nothing was
   detected (`ci_question_asked`), the summary hint on "none"
   (`hint_in_summary`).
3. **Don't touch what the maintainers own** — every pre-existing CI file
   is byte-identical after the run (`existing_ci_untouched`).

Each task stages the go-build fixture (a small dependency-free Go CLI),
lays down the task's `ci_setup` files, `git init`s it (migrate needs a
repo), and drives ONE REAL MULTI-TURN CONVERSATION: the floxify
invocation first, then each scripted user answer as its own
`claude -p --resume` turn. (A single-shot role-play prompt was tried
first and the skill correctly refused it — it stopped at the Phase 4
menu because its own instructions say to wait for the user.) With real
turns, `offer_asked` is a genuine observation: the offer either appears
in the agent's output before the scripted "y"/"n" turn answers it, or
the check fails.

Gate policy: never gates (agentic runs report a rate, like real-world and
build tiers). The deterministic helpers are gated by
tests/test_migrate_mode.py.

    flox activate                      # repo env: python3 + claude
    cd evals/floxify
    python3 migrate_mode.py            # full registry
    python3 migrate_mode.py --only migrate-gh-yes
"""
import argparse
import fnmatch
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from run_floxify import (  # noqa: E402
    CLAUDE_AGENT_COMMON_FLAGS, DEFAULT_SKILL_DIR, _parse_stream)

TASKS_FILE = HERE / "migrate.jsonl"
FIXTURES_DIR = HERE / "fixtures"
BASE_FIXTURE = "go-build"
DEFAULT_OUT = HERE / "results" / "migrate.json"
DEFAULT_AGENT_TIMEOUT = 1500

KNOWN_CHECKS = {
    "offer_asked", "ci_question_asked", "flox_yml_written", "flox_yml_valid",
    "no_new_ci_files", "existing_ci_untouched", "snippet_proposed",
    "committed", "hint_in_summary",
}


def _load_tasks(path=TASKS_FILE):
    tasks = []
    for i, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        task = json.loads(line)
        for field in ("id", "tier", "ci_setup", "answers", "checks", "rubric"):
            if field not in task:
                raise ValueError(f"migrate.jsonl line {i}: missing {field}")
        unknown = set(task["checks"]) - KNOWN_CHECKS
        if unknown:
            raise ValueError(f"migrate.jsonl line {i}: unknown checks {unknown}")
        tasks.append(task)
    return tasks


def _stage(task, tmpdir):
    """go-build fixture + the task's CI files + a git repo (migrate
    requires one: it branches and commits). Returns (tmp, ci_hashes) —
    the pre-run digest of every staged CI file, for the untouched check."""
    shutil.copytree(str(FIXTURES_DIR / BASE_FIXTURE), tmpdir, dirs_exist_ok=True)
    tmp = Path(tmpdir)
    (tmp / "seed-manifest.toml").unlink(missing_ok=True)
    for rel, content in task["ci_setup"].items():
        dest = tmp / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
    for cmd in (["git", "init", "-q", "-b", "main"],
                ["git", "add", "-A"],
                ["git", "-c", "user.email=eval@flox.dev", "-c",
                 "user.name=Eval", "commit", "-q", "-m", "initial"],
                # A real migrated repo has an origin with a default-branch
                # ref; without one, the guidance's ask-when-unset fallback
                # fires a question the script has no answer for (seen
                # live). The URL is inert — nothing pushes.
                ["git", "remote", "add", "origin",
                 "https://github.com/example/fixture.git"],
                ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
                ["git", "symbolic-ref", "refs/remotes/origin/HEAD",
                 "refs/remotes/origin/main"]):
        subprocess.run(cmd, cwd=str(tmp), check=True, capture_output=True)
    hashes = {rel: hashlib.sha256((tmp / rel).read_bytes()).hexdigest()
              for rel in task["ci_setup"]}
    return tmp, hashes


def _first_prompt(tmp):
    return (
        f"Invoke the floxify skill on {tmp}. Follow the skill exactly as "
        f"with a real user — including stopping to ask whenever it tells "
        f"you to. Do not push anything to any remote."
    )


CI_FILE_GLOBS = (
    ".github/workflows/*", ".gitlab-ci.yml", ".circleci/*", ".buildkite/*",
    "Jenkinsfile", ".woodpecker.yml", "azure-pipelines.yml", ".drone.yml",
)


def _snapshot_tree(tmp):
    """Every file path in the staged tree (relative), pre-agent — the
    baseline for the no-new-CI-files check."""
    return {str(p.relative_to(tmp)) for p in Path(tmp).rglob("*")
            if p.is_file() and ".git/" not in str(p.relative_to(tmp)) + "/"}


def _assistant_text(stream_json_text):
    """ONLY the agent's own words, concatenated. The raw stream also
    carries tool_results — including the full text of migration.md, which
    contains every phrase the transcript checks grep for — so grading the
    raw stream passes the moment the agent READS the guidance (found by
    review round 2, verified against a live transcript). Grading must see
    what the agent said, never what it read."""
    out = []
    for line in stream_json_text.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    out.append(block.get("text", ""))
    return "\n".join(out)


def _extract_session_id(stdout_text):
    """The stream-json init event carries the session id `--resume` needs."""
    for line in stdout_text.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = ev.get("session_id")
        if sid:
            return sid
    return None


def _claude_cmd(prompt, skill_dir, resume=None):
    """Mirror run_floxify._run_claude_agent's flag set (same tool surface,
    same settings isolation), plus --resume for follow-up turns — the one
    thing this suite needs that the shared helper doesn't do. A scripted
    single-shot role-play was tried first and the skill correctly refused
    to play along: it stopped at the Phase 4 menu because its own text
    says to wait for the user. Real turns are the only honest driver, and
    they make `offer_asked` an observation instead of an act."""
    cmd = (["claude", "-p", prompt] + CLAUDE_AGENT_COMMON_FLAGS
           + ["--plugin-dir", str(skill_dir)])
    if resume:
        cmd += ["--resume", resume]
    return cmd


def _drive_conversation(tmp, answers, skill_dir, timeout):
    """One real conversation: the floxify invocation, then each scripted
    user answer as its own --resume turn. `timeout` bounds the WHOLE
    conversation, not each turn — a per-turn reading silently multiplied
    the stated budget by the answer count. Runs with cwd=tmp so the
    agent's relative operations land in the staged repo, exactly as a
    real user running floxify from inside their project. Returns
    (combined_stream_text, err, meta)."""
    deadline = time.monotonic() + timeout
    streams = []
    turn_metas = []
    session = None
    prompts = [_first_prompt(tmp)] + list(answers)
    for i, prompt in enumerate(prompts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "".join(streams), f"conversation TIMEOUT after {timeout}s (at turn {i})", None
        cmd = _claude_cmd(prompt, skill_dir, resume=session)
        try:
            proc = subprocess.run(cmd, cwd=str(tmp), capture_output=True,
                                  text=True, timeout=remaining)
        except subprocess.TimeoutExpired:
            return "".join(streams), f"conversation TIMEOUT after {timeout}s (in turn {i})", None
        streams.append(proc.stdout)
        if proc.returncode != 0:
            return ("".join(streams),
                    f"turn {i} EXIT {proc.returncode}: {proc.stderr[:300]}",
                    None)
        _, turn_meta, has_result = _parse_stream(proc.stdout)
        if not has_result:
            return "".join(streams), f"turn {i} BAD_STREAM", None
        turn_metas.append({k: v for k, v in (turn_meta or {}).items()
                           if k != "raw_stream"})
        if session is None:
            session = _extract_session_id(proc.stdout)
            if session is None:
                return "".join(streams), "no session_id in first turn", None
    combined = "".join(streams)
    return combined, None, {"conversation_turns": len(prompts),
                            "turns": turn_metas,
                            "raw_stream": combined}


def _check(name, task, tmp, hashes, said, pre_files):
    """One named check → (passed, note). `said` is ASSISTANT text only
    (never tool results — see _assistant_text); `pre_files` is the
    pre-agent tree snapshot for the new-CI-files check."""
    flox_yml = tmp / ".github" / "workflows" / "flox.yml"
    if name == "offer_asked":
        ok = "[y/N]" in said and "dev environment" in said
        return ok, "offer text found" if ok else "offer question not in agent output"
    if name == "ci_question_asked":
        ok = "which CI" in said or "Which CI" in said
        return ok, "which-CI question found" if ok else "no which-CI question in agent output"
    if name == "flox_yml_written":
        return flox_yml.is_file(), str(flox_yml.relative_to(tmp))
    if name == "no_new_ci_files":
        new = _snapshot_tree(tmp) - pre_files
        offenders = sorted(
            rel for rel in new
            if any(fnmatch.fnmatch(rel, g) or rel == g for g in CI_FILE_GLOBS))
        ok = not offenders
        return ok, ("no new CI files" if ok
                    else f"CI file(s) written without consent: {offenders}")
    if name == "flox_yml_valid":
        if not flox_yml.is_file():
            return False, "file missing"
        text = flox_yml.read_text()
        ok = ("install-flox-action" in text and "on:" in text
              and "flox activate" in text)
        return ok, "has install action + trigger + activation" if ok else "missing required elements"
    if name == "existing_ci_untouched":
        for rel, digest in hashes.items():
            path = tmp / rel
            if not path.is_file():
                return False, f"{rel} deleted"
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                return False, f"{rel} modified"
        return True, f"{len(hashes)} pre-existing CI file(s) byte-identical"
    if name == "snippet_proposed":
        ok = "ghcr.io/flox/flox" in said
        return ok, "GitLab-idiom snippet in agent output" if ok else "no snippet in agent output"
    if name == "committed":
        # Subject alone is fakeable with --allow-empty; require the
        # migration's defining artifact in the commit's file list.
        proc = subprocess.run(
            ["git", "log", "--format=%H %s"], cwd=str(tmp),
            capture_output=True, text=True)
        sha = next((l.split()[0] for l in proc.stdout.splitlines()
                    if "Add Flox development environment" in l), None)
        if sha is None:
            return False, "migration commit missing"
        files = subprocess.run(
            ["git", "show", "--name-only", "--format=", sha], cwd=str(tmp),
            capture_output=True, text=True).stdout
        ok = ".flox/env/manifest.toml" in files
        return ok, ("migration commit present with manifest" if ok
                    else "commit exists but carries no .flox/env/manifest.toml")
    if name == "hint_in_summary":
        ok = "flox activate --" in said and "In CI" in said
        return ok, "In-CI hint present" if ok else "In-CI hint missing from summary"
    return False, f"unknown check {name}"


def process_task(task, skill_dir, agent_timeout=DEFAULT_AGENT_TIMEOUT,
                 stream_dir=None):
    result = {"id": task["id"], "tier": task["tier"],
              "terminal_disposition": None, "checks": {}, "passed": 0,
              "failed": 0, "detail": "", "meta": None, "stream_file": None}
    with tempfile.TemporaryDirectory(prefix=f"floxify-migrate-{task['id']}-") as tmpdir:
        try:
            tmp, hashes = _stage(task, tmpdir)
        except subprocess.CalledProcessError as e:
            result["terminal_disposition"] = "unverifiable-env"
            result["detail"] = f"staging failed: {e}"
            return result
        pre_files = _snapshot_tree(tmp)

        stream_text, agent_err, meta = _drive_conversation(
            tmp, task["answers"], skill_dir, agent_timeout)
        if isinstance(meta, dict):
            result["meta"] = {k: v for k, v in meta.items() if k != "raw_stream"}
        else:
            result["meta"] = meta
        # Persist the transcript like run_floxify does — check failures
        # are only diagnosable against what the agent actually printed.
        # Keyed to the summary file's stem so two runs with different
        # --out never overwrite each other's transcripts.
        if stream_dir is None:
            stream_dir = HERE / "results" / "streams" / "migrate"
        stream_dir.mkdir(parents=True, exist_ok=True)
        stream_file = stream_dir / f"{task['id']}.txt"
        stream_file.write_text(stream_text)
        result["stream_file"] = str(stream_file)
        if agent_err:
            result["terminal_disposition"] = "agent-error"
            result["detail"] = agent_err
            return result

        said = _assistant_text(stream_text)
        for name in task["checks"]:
            ok, note = _check(name, task, tmp, hashes, said, pre_files)
            result["checks"][name] = {"ok": ok, "note": note}
        result["passed"] = sum(1 for c in result["checks"].values() if c["ok"])
        result["failed"] = len(result["checks"]) - result["passed"]
        result["terminal_disposition"] = "scored"
        return result


def _safe_process_task(task, skill_dir, agent_timeout, stream_dir=None):
    try:
        return process_task(task, skill_dir, agent_timeout=agent_timeout,
                            stream_dir=stream_dir)
    except Exception as e:  # noqa: BLE001 — one task's crash costs one task
        return {"id": task["id"], "tier": task["tier"],
                "terminal_disposition": "harness-error", "checks": {},
                "passed": 0, "failed": 0,
                "detail": f"{type(e).__name__}: {e}", "meta": None,
                "stream_file": None}


def _summary(results):
    scored = [r for r in results if r["terminal_disposition"] == "scored"]
    return {
        "tasks": len(results),
        "scored": len(scored),
        "unverifiable_env": sum(1 for r in results
                                if r["terminal_disposition"] == "unverifiable-env"),
        "agent_errors": sum(1 for r in results
                            if r["terminal_disposition"] == "agent-error"),
        "harness_errors": sum(1 for r in results
                              if r["terminal_disposition"] == "harness-error"),
        "all_checks_passed": sum(1 for r in scored if r["failed"] == 0),
        "checks_passed": sum(r["passed"] for r in scored),
        "checks_failed": sum(r["failed"] for r in scored),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", help="run a single task id")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--skill-dir", type=Path, default=DEFAULT_SKILL_DIR)
    ap.add_argument("--agent-timeout", type=int, default=DEFAULT_AGENT_TIMEOUT)
    ap.add_argument("--concurrency", type=int, default=2)
    args = ap.parse_args()

    tasks = _load_tasks()
    if args.only:
        tasks = [t for t in tasks if t["id"] == args.only]
        if not tasks:
            sys.exit(f"no task {args.only!r} in migrate.jsonl")

    stream_dir = args.out.parent / "streams" / args.out.stem

    def _run_one(task):
        print(f"  [{task['tier']}] {task['id']}: running...", flush=True)
        r = _safe_process_task(task, args.skill_dir, args.agent_timeout,
                               stream_dir=stream_dir)
        if r["terminal_disposition"] == "scored":
            bad = [n for n, c in r["checks"].items() if not c["ok"]]
            verdict = "ALL PASS" if not bad else f"FAILED: {', '.join(bad)}"
        else:
            verdict = r["terminal_disposition"]
        print(f"  [{task['tier']}] {task['id']}: {verdict}", flush=True)
        return r

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        results = list(pool.map(_run_one, tasks))

    summary = _summary(results)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "results": results}, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
