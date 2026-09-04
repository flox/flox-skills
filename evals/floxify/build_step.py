#!/usr/bin/env python3
"""Build-step outcome evals: can the skill author a WORKING `flox build`?

The question this suite answers (Bill LeVine, reviewing the CI-wiring
guidance): we have confidence the skills can set up a dev environment;
building a working artifact is a step deeper with more edge cases. Before
any skill offers to wire `flox build` verification for users, measure the
success rate of agent-authored builds across a few ecosystems — if it's
solid, add the offer; if not, the per-task failures here are the gap list
to diagnose and file.

What one task run does:

1. Stages a fixture repo into a temp dir and SEEDS a known-good dev
   manifest (a committed golden, or the fixture's own seed-manifest.toml),
   then locks it with a real `flox activate`. Seeding isolates the
   variable: this suite measures build-target authoring, never runtime
   detection — run_floxify.py already owns that.
2. Runs `claude` headlessly with the flox plugin loaded, prompting it to
   add a manifest `[build.*]` target per the flox skill's build guidance
   and iterate until `flox build` succeeds.
3. Scores the outcome deterministically, trusting nothing the agent said:
   re-parses the manifest for a `[build.*]` section, re-runs `flox build`
   itself, and smoke-tests the artifact (`run_bin`: execute a binary from
   `result*/bin/` and match stdout; `artifact_exists`: any file under a
   `result*/` output).

Gate policy: never gates — like real-world.jsonl, every run reports a rate.
The agentic run is non-deterministic and the builds hit the network (cargo
fetch, toolchain downloads), so results are evidence for a decision, not a
per-PR check. The deterministic helpers ARE gated, by
tests/test_build_step.py.

    flox activate                      # repo env: python3 + claude
    cd evals/floxify
    python3 build_step.py              # full registry
    python3 build_step.py --only go-build
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from run_floxify import DEFAULT_SKILL_DIR, _run_claude_agent  # noqa: E402

TASKS_FILE = HERE / "build.jsonl"
FIXTURES_DIR = HERE / "fixtures"
DEFAULT_OUT = HERE / "results" / "build.json"

REQUIRED_TASK_FIELDS = ("id", "tier", "ecosystem", "fixture", "seed_manifest", "smoke", "rubric")
SMOKE_TYPES = ("run_bin", "artifact_exists")

DEFAULT_AGENT_TIMEOUT = 1200   # the agent's own build-iterate loop
DEFAULT_BUILD_TIMEOUT = 900    # our independent flox build re-run
DEFAULT_SEED_TIMEOUT = 900     # first activation downloads toolchains
SMOKE_TIMEOUT = 30


def _load_tasks(path=TASKS_FILE):
    """Parse and validate build.jsonl. Raises ValueError on a malformed
    entry — a registry typo should fail loudly at load, not as a confusing
    mid-run KeyError."""
    tasks = []
    for i, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        task = json.loads(line)
        missing = [f for f in REQUIRED_TASK_FIELDS if f not in task]
        if missing:
            raise ValueError(f"build.jsonl line {i}: missing fields {missing}")
        smoke = task["smoke"]
        if smoke.get("type") not in SMOKE_TYPES:
            raise ValueError(
                f"build.jsonl line {i}: smoke.type must be one of {SMOKE_TYPES}")
        if smoke["type"] == "run_bin" and "stdout_re" not in smoke:
            raise ValueError(f"build.jsonl line {i}: run_bin smoke needs stdout_re")
        if not (HERE / task["seed_manifest"]).is_file():
            raise ValueError(
                f"build.jsonl line {i}: seed_manifest {task['seed_manifest']} not found")
        if not (FIXTURES_DIR / task["fixture"]).is_dir():
            raise ValueError(
                f"build.jsonl line {i}: fixture {task['fixture']} not found")
        tasks.append(task)
    return tasks


def _stage(task, tmpdir):
    """Copy the fixture into the temp dir. The fixture-local
    seed-manifest.toml, when present, is removed from the staged tree so
    the agent never sees a stray file the real repo would not have.
    Filesystem-only — the flox side of seeding is _seed_env."""
    fixture_src = FIXTURES_DIR / task["fixture"]
    shutil.copytree(str(fixture_src), tmpdir, dirs_exist_ok=True)
    tmp = Path(tmpdir)
    staged_seed = tmp / "seed-manifest.toml"
    if staged_seed.exists():
        staged_seed.unlink()
    return tmp


def _seed_env(task, tmp, timeout=DEFAULT_SEED_TIMEOUT):
    """Create a real environment and install the seed manifest into it.

    A bare .flox/env/manifest.toml is NOT a valid flox environment (no
    env.json — flox refuses the directory; found live by the plumbing
    check), so seeding is: `flox init --no-auto-setup` for the metadata,
    overwrite the generated manifest with the seed, then one activation so
    the seed resolves and locks. A failure here is an environment problem
    (catalog, network, seed rot) — the task is unverifiable, which is
    different from the build step failing."""
    try:
        proc = subprocess.run(
            ["flox", "init", "--no-auto-setup", "-d", str(tmp)],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return False, "flox init timed out"
    except FileNotFoundError:
        return False, "flox not on PATH"
    if proc.returncode != 0:
        return False, f"flox init failed: {(proc.stderr or proc.stdout)[-2000:]}"
    seed_text = (HERE / task["seed_manifest"]).read_text()
    (tmp / ".flox" / "env" / "manifest.toml").write_text(seed_text)
    try:
        proc = subprocess.run(
            ["flox", "activate", "-c", "echo __seed_ok__"],
            cwd=str(tmp), capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"seed activation timed out after {timeout}s"
    if "__seed_ok__" not in proc.stdout:
        return False, (proc.stderr or proc.stdout)[-2000:]
    return True, ""


def _build_prompt(tmp):
    return (
        f"The project at {tmp} already has a working Flox dev environment "
        f"(.flox/env/manifest.toml — do not change its [install], [hook], "
        f"[vars], or [profile] sections). Your task: following the flox "
        f"skill's build guidance (references/builds.md), add a manifest "
        f"[build.<name>] target that builds this project's application and "
        f"installs it into $out (executables under $out/bin). Then run "
        f"`flox build` yourself and iterate on the build target until it "
        f"succeeds and the artifact under ./result-<name> works. Do not "
        f"modify the application source code (generating lockfiles like "
        f"go.sum is fine). Your final message must be exactly the build "
        f"target name you added, nothing else."
    )


def _read_manifest(tmp):
    """Read the post-agent manifest defensively. Returns
    (text | None, valid_toml, targets): text is None when the agent
    deleted or moved the file (it holds Write/Edit/Bash — assume nothing);
    valid_toml is False when the text no longer parses, which the result
    record reports separately so a corrupted manifest is distinguishable
    from one that simply lacks a build target."""
    path = Path(tmp) / ".flox" / "env" / "manifest.toml"
    if not path.is_file():
        return None, False, []
    text = path.read_text()
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return text, False, []
    build = data.get("build")
    targets = sorted(build.keys()) if isinstance(build, dict) else []
    return text, True, targets


def _clear_results(tmp):
    """Remove every result* entry before OUR flox build runs. The working
    tree was the agent's for the whole run — a stale or hand-fabricated
    result-*/bin/ would otherwise be scoreable by the smoke, and the
    smoke must only ever see what the harness's own build produced."""
    for entry in Path(tmp).glob("result*"):
        if entry.is_symlink() or entry.is_file():
            entry.unlink()
        else:
            shutil.rmtree(entry)


def _run_flox_build(tmp, timeout=DEFAULT_BUILD_TIMEOUT):
    """Our own `flox build`, independent of whatever the agent ran."""
    _clear_results(tmp)
    try:
        proc = subprocess.run(
            ["flox", "build"], cwd=str(tmp),
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"flox build timed out after {timeout}s"
    ok = proc.returncode == 0
    return ok, "" if ok else (proc.stderr or proc.stdout)[-2000:]


def _find_result_bins(tmp):
    """Executable files under any result*/bin/, resolved through the
    result symlink."""
    bins = []
    for result in sorted(Path(tmp).glob("result*")):
        bin_dir = result / "bin"
        if not bin_dir.is_dir():
            continue
        for f in sorted(bin_dir.iterdir()):
            # Skip dotfiles: flox build wraps executables, leaving the
            # internal `.<name>-wrapped` beside the real `<name>` (seen
            # live) — the wrapper is the product a user would run.
            if f.name.startswith("."):
                continue
            if f.is_file() and os.access(str(f), os.X_OK):
                bins.append(f)
    return bins


def _artifact_exists(tmp):
    """Any regular file anywhere under any result*/ output."""
    for result in sorted(Path(tmp).glob("result*")):
        for f in result.rglob("*"):
            if f.is_file():
                return True
    return False


def _smoke(task, tmp):
    """Returns (ok, detail). Never trusts the agent: executes the artifact
    (or checks it exists) directly."""
    smoke = task["smoke"]
    if smoke["type"] == "artifact_exists":
        ok = _artifact_exists(tmp)
        return ok, "artifact found" if ok else "no files under any result*/"
    bins = _find_result_bins(tmp)
    if not bins:
        return False, "no executable under any result*/bin/"
    binary = bins[0]
    try:
        proc = subprocess.run(
            [str(binary)] + list(smoke.get("args", [])),
            cwd=str(tmp), capture_output=True, text=True,
            timeout=SMOKE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"{binary.name} timed out after {SMOKE_TIMEOUT}s"
    except OSError as e:
        return False, f"{binary.name} failed to execute: {e}"
    if proc.returncode != 0:
        return False, f"{binary.name} exited {proc.returncode}: {proc.stderr[-500:]}"
    if not re.search(smoke["stdout_re"], proc.stdout):
        return False, (f"{binary.name} stdout {proc.stdout[:200]!r} did not "
                       f"match /{smoke['stdout_re']}/")
    return True, f"{binary.name} ran and matched /{smoke['stdout_re']}/"


def _slim_meta(meta):
    """Keep the countable fields; raw_stream (the full agent transcript)
    would make results/build.json multi-MB and undiffable — the siblings
    route streams to separate files, and this suite just drops them."""
    if not isinstance(meta, dict):
        return meta
    return {k: v for k, v in meta.items() if k != "raw_stream"}


def process_task(task, skill_dir, agent_timeout=DEFAULT_AGENT_TIMEOUT):
    result = {
        "id": task["id"], "tier": task["tier"], "ecosystem": task["ecosystem"],
        "terminal_disposition": None, "seed_lock_ok": False,
        "manifest_present": False, "manifest_valid_toml": False,
        "build_targets": [], "agent_reported_target": None,
        "build_ok": False, "smoke_ok": False,
        "detail": "", "meta": None,
    }
    with tempfile.TemporaryDirectory(prefix=f"floxify-build-{task['id']}-") as tmpdir:
        tmp = _stage(task, tmpdir)

        ok, err = _seed_env(task, tmp)
        result["seed_lock_ok"] = ok
        if not ok:
            result["terminal_disposition"] = "unverifiable-env"
            result["detail"] = err
            return result

        agent_text, agent_err, meta = _run_claude_agent(
            _build_prompt(tmp), skill_dir, timeout=agent_timeout)
        result["meta"] = _slim_meta(meta)
        if agent_err:
            result["terminal_disposition"] = "agent-error"
            result["detail"] = agent_err
            return result
        # The prompt's contract: final message is the target name. Record
        # it so a mismatch with the parsed targets stays diagnosable.
        result["agent_reported_target"] = (agent_text or "").strip()[:200]

        manifest_text, valid, targets = _read_manifest(tmp)
        result["manifest_present"] = manifest_text is not None
        result["manifest_valid_toml"] = valid
        result["build_targets"] = targets
        if manifest_text is None:
            result["terminal_disposition"] = "scored"
            result["detail"] = "manifest missing after agent run (.flox/env/manifest.toml deleted or moved)"
            return result
        if not valid:
            result["terminal_disposition"] = "scored"
            result["detail"] = "manifest no longer parses as TOML after agent run"
            return result
        if not targets:
            result["terminal_disposition"] = "scored"
            result["detail"] = "no [build.*] section in the final manifest"
            return result

        result["build_ok"], build_err = _run_flox_build(tmp)
        if not result["build_ok"]:
            result["terminal_disposition"] = "scored"
            result["detail"] = build_err
            return result

        result["smoke_ok"], result["detail"] = _smoke(task, tmp)
        result["terminal_disposition"] = "scored"
        return result


def _safe_process_task(task, skill_dir, agent_timeout):
    """One task's crash must cost one task, not the whole run — results
    are only written after the loop, so an uncaught exception here would
    discard every completed task."""
    try:
        return process_task(task, skill_dir, agent_timeout=agent_timeout)
    except Exception as e:  # noqa: BLE001 — record and continue
        return {
            "id": task["id"], "tier": task["tier"],
            "ecosystem": task["ecosystem"],
            "terminal_disposition": "harness-error",
            "seed_lock_ok": False, "manifest_present": False,
            "manifest_valid_toml": False, "build_targets": [],
            "agent_reported_target": None,
            "build_ok": False, "smoke_ok": False,
            "detail": f"{type(e).__name__}: {e}", "meta": None,
        }


def _summary(results):
    scored = [r for r in results if r["terminal_disposition"] == "scored"]
    return {
        "tasks": len(results),
        "scored": len(scored),
        "unverifiable_env": sum(
            1 for r in results if r["terminal_disposition"] == "unverifiable-env"),
        "agent_errors": sum(
            1 for r in results if r["terminal_disposition"] == "agent-error"),
        "harness_errors": sum(
            1 for r in results if r["terminal_disposition"] == "harness-error"),
        "authored_target": sum(1 for r in scored if r["build_targets"]),
        "build_ok": sum(1 for r in scored if r["build_ok"]),
        "smoke_ok": sum(1 for r in scored if r["smoke_ok"]),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", help="run a single task id")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--skill-dir", type=Path, default=DEFAULT_SKILL_DIR)
    ap.add_argument("--agent-timeout", type=int, default=DEFAULT_AGENT_TIMEOUT)
    ap.add_argument("--concurrency", type=int, default=2,
                    help="tasks in flight at once (like the sibling runners)")
    args = ap.parse_args()

    tasks = _load_tasks()
    if args.only:
        tasks = [t for t in tasks if t["id"] == args.only]
        if not tasks:
            sys.exit(f"no task {args.only!r} in build.jsonl")

    def _run_one(task):
        print(f"  [{task['tier']}] {task['id']}: running...", flush=True)
        r = _safe_process_task(task, args.skill_dir, args.agent_timeout)
        verdict = ("BUILD+SMOKE" if r["smoke_ok"] else
                   "BUILD" if r["build_ok"] else
                   r["terminal_disposition"] if r["terminal_disposition"] != "scored"
                   else "FAILED")
        print(f"  [{task['tier']}] {task['id']}: {verdict}  {r['detail'][:120]}",
              flush=True)
        return r

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        results = list(pool.map(_run_one, tasks))

    summary = _summary(results)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "results": results}, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
