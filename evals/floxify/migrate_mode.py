#!/usr/bin/env python3
"""Migrate-mode outcome evals: is the CI-wiring guidance actually followed?

The repo policy (evals/README.md) says a PR that changes skill guidance
ships an eval verifying the guidance is followed. The CI-wiring guidance
lives in floxify's migrate step — the [y/N] offer, the detect-and-conform
branches, the never-touch-existing-CI rule — and none of it executes in a
plain `/floxify <dir>` run. This runner drives the skill THROUGH migrate
with a scripted user and grades the three claims the guidance makes:

1. **Consent** — the offer was asked before anything was written
   (`offer_asked`, from the transcript), and a "n" produced no file
   (`no_flox_yml`).
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
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from run_floxify import DEFAULT_SKILL_DIR, MODEL, _parse_stream  # noqa: E402

TASKS_FILE = HERE / "migrate.jsonl"
FIXTURES_DIR = HERE / "fixtures"
BASE_FIXTURE = "go-build"
DEFAULT_OUT = HERE / "results" / "migrate.json"
DEFAULT_AGENT_TIMEOUT = 1500

KNOWN_CHECKS = {
    "offer_asked", "ci_question_asked", "flox_yml_written", "flox_yml_valid",
    "no_flox_yml", "existing_ci_untouched", "snippet_proposed", "committed",
    "hint_in_summary",
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
    cmd = [
        "claude", "-p", prompt,
        "--model", MODEL,
        "--output-format", "stream-json",
        "--verbose",
        "--allowedTools", "Bash", "Read", "Write", "Edit", "Skill",
        "--strict-mcp-config",
        "--setting-sources", "project,local",
        "--plugin-dir", str(skill_dir),
    ]
    if resume:
        cmd += ["--resume", resume]
    return cmd


def _drive_conversation(tmp, answers, skill_dir, timeout):
    """One real conversation: the floxify invocation, then each scripted
    user answer as its own --resume turn. Returns
    (combined_stream_text, err, meta)."""
    streams = []
    session = None
    prompts = [_first_prompt(tmp)] + list(answers)
    for i, prompt in enumerate(prompts):
        cmd = _claude_cmd(prompt, skill_dir, resume=session)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            return "".join(streams), f"turn {i} TIMEOUT after {timeout}s", None
        streams.append(proc.stdout)
        if proc.returncode != 0:
            return ("".join(streams),
                    f"turn {i} EXIT {proc.returncode}: {proc.stderr[:300]}",
                    None)
        _, _, has_result = _parse_stream(proc.stdout)
        if not has_result:
            return "".join(streams), f"turn {i} BAD_STREAM", None
        if session is None:
            session = _extract_session_id(proc.stdout)
            if session is None:
                return "".join(streams), "no session_id in first turn", None
    combined = "".join(streams)
    return combined, None, {"conversation_turns": len(prompts),
                            "raw_stream": combined}


def _check(name, task, tmp, hashes, stream_text, err_detail):
    """One named check → (passed, note)."""
    flox_yml = tmp / ".github" / "workflows" / "flox.yml"
    if name == "offer_asked":
        ok = "[y/N]" in stream_text and "dev environment" in stream_text
        return ok, "offer text found" if ok else "offer question not in transcript"
    if name == "ci_question_asked":
        ok = "which CI" in stream_text or "Which CI" in stream_text
        return ok, "which-CI question found" if ok else "no which-CI question in transcript"
    if name == "flox_yml_written":
        return flox_yml.is_file(), str(flox_yml.relative_to(tmp))
    if name == "no_flox_yml":
        ok = not flox_yml.exists()
        return ok, "absent as required" if ok else "flox.yml written without consent"
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
        ok = "ghcr.io/flox/flox" in stream_text
        return ok, "GitLab-idiom snippet in output" if ok else "no snippet in transcript"
    if name == "committed":
        proc = subprocess.run(["git", "log", "--oneline"], cwd=str(tmp),
                              capture_output=True, text=True)
        ok = "Add Flox development environment" in proc.stdout
        return ok, "migration commit present" if ok else "migration commit missing"
    if name == "hint_in_summary":
        ok = "flox activate --" in stream_text and "In CI" in stream_text
        return ok, "In-CI hint present" if ok else "In-CI hint missing from summary"
    return False, f"unknown check {name}"


def process_task(task, skill_dir, agent_timeout=DEFAULT_AGENT_TIMEOUT):
    result = {"id": task["id"], "tier": task["tier"],
              "terminal_disposition": None, "checks": {}, "passed": 0,
              "failed": 0, "detail": "", "meta": None}
    with tempfile.TemporaryDirectory(prefix=f"floxify-migrate-{task['id']}-") as tmpdir:
        try:
            tmp, hashes = _stage(task, tmpdir)
        except subprocess.CalledProcessError as e:
            result["terminal_disposition"] = "unverifiable-env"
            result["detail"] = f"staging failed: {e}"
            return result

        stream_text, agent_err, meta = _drive_conversation(
            tmp, task["answers"], skill_dir, agent_timeout)
        if isinstance(meta, dict):
            result["meta"] = {k: v for k, v in meta.items() if k != "raw_stream"}
        else:
            result["meta"] = meta
        # Persist the transcript like run_floxify does — check failures
        # are only diagnosable against what the agent actually printed.
        stream_dir = HERE / "results" / "streams"
        stream_dir.mkdir(parents=True, exist_ok=True)
        stream_file = stream_dir / f"migrate-{task['id']}.txt"
        stream_file.write_text(stream_text)
        result["stream_file"] = str(stream_file.relative_to(HERE))
        if agent_err:
            result["terminal_disposition"] = "agent-error"
            result["detail"] = agent_err
            return result

        for name in task["checks"]:
            ok, note = _check(name, task, tmp, hashes, stream_text, "")
            result["checks"][name] = {"ok": ok, "note": note}
        result["passed"] = sum(1 for c in result["checks"].values() if c["ok"])
        result["failed"] = len(result["checks"]) - result["passed"]
        result["terminal_disposition"] = "scored"
        return result


def _safe_process_task(task, skill_dir, agent_timeout):
    try:
        return process_task(task, skill_dir, agent_timeout=agent_timeout)
    except Exception as e:  # noqa: BLE001 — one task's crash costs one task
        return {"id": task["id"], "tier": task["tier"],
                "terminal_disposition": "harness-error", "checks": {},
                "passed": 0, "failed": 0,
                "detail": f"{type(e).__name__}: {e}", "meta": None}


def _summary(results):
    scored = [r for r in results if r["terminal_disposition"] == "scored"]
    return {
        "tasks": len(results),
        "scored": len(scored),
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

    def _run_one(task):
        print(f"  [{task['tier']}] {task['id']}: running...", flush=True)
        r = _safe_process_task(task, args.skill_dir, args.agent_timeout)
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
