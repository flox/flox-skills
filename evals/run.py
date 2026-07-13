#!/usr/bin/env python3
"""Flox skills eval harness.

Runs each task in tasks.jsonl through `claude` headless, in one of three arms:

  --mode skills       skills only, MCP disabled (--strict-mcp-config, no --mcp-config)
  --mode skills+mcp   skills plus the flox-mcp server (--mcp-config)
  --mode baseline     bare model: no plugin, MCP disabled (unassisted baseline)

Each answer is scored with deterministic hard-checks plus an LLM judge.
Results are written to results/<mode>.json. Pure stdlib (no node/uv needed).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent / "flox-plugin"
MODEL = "claude-opus-4-8"  # pinned for reproducible scores; override with --model

ANSWER_SUFFIX = (
    "\n\nProvide the COMPLETE solution as your written answer: the manifest "
    "(in a ```toml code block) and the exact flox commands, with a brief "
    "explanation. Do not execute commands — just give the answer."
)

# Neutral suffix for trigger tests: must NOT mention flox/manifest, so the run
# genuinely tests whether the skill fires on its own (implicit triggering).
NEUTRAL_SUFFIX = (
    "\n\nProvide the complete solution as your written answer (setup steps and "
    "any config). Do not execute commands — just give the answer."
)

# ---- deterministic hard-checks ---------------------------------------------
# Flags hallucinated *Flox* install methods (the ai-13 bug). Only a curl|sh that
# mentions flox counts — a legit `curl … | sh` for some other tool is fine.
FAKE_INSTALL = re.compile(
    r"install\.flox\.dev|flox\.dev/install|curl[^\n]*flox[^\n]*\|\s*(ba)?sh", re.I
)
ABS_PATH = re.compile(r'=\s*"(/home/|/Users/|/usr/local/|/opt/|/root/)', re.I)


def toml_blocks(text):
    return "\n".join(re.findall(r"```(?:toml)?\n(.*?)```", text, re.S))


CHECKS = {
    "no_fake_install_url": lambda a: not FAKE_INSTALL.search(a),
    "no_abs_paths": lambda a: not ABS_PATH.search(toml_blocks(a)),
    "has_install_section": lambda a: "[install]" in a,
    "has_services_section": lambda a: "[services" in a,
    "has_build_section": lambda a: "[build" in a,
    "mentions_containerize": lambda a: "flox containerize" in a,
    "uses_flox_publish": lambda a: "flox publish" in a,
    "uses_include_or_layer": lambda a: "[include]" in a or "flox activate -r" in a,
    "uses_search_show": lambda a: "flox search" in a or "flox show" in a,
    "uses_remote_env": lambda a: "flox push" in a or "flox pull" in a or "flox activate -r" in a,
    # Implicit-trigger check: did the skill fire and produce Flox guidance even
    # though the prompt never said "flox"?
    "invokes_flox": lambda a: bool(
        re.search(r"\bflox\b", a, re.I)
        and (re.search(r"flox (init|install|search|show|containerize|publish|build|activate|push|edit)", a)
             or "[install]" in a or "manifest.toml" in a)),
}


def run_claude(prompt, mode, allow_tools, timeout=420, retries=3):
    cmd = ["claude", "-p", prompt, "--model", MODEL, "--output-format", "json"]
    if allow_tools:
        cmd += ["--allowedTools", *allow_tools]
    if mode == "skills":
        cmd += ["--plugin-dir", str(PLUGIN_DIR), "--strict-mcp-config"]
    elif mode == "skills+mcp":
        mcp_cfg = HERE / "flox-mcp.json"
        cmd += ["--plugin-dir", str(PLUGIN_DIR), "--mcp-config", str(mcp_cfg)]
    elif mode == "baseline":
        # Bare model: no plugin loaded, MCP disabled. Measures the unassisted baseline.
        cmd += ["--strict-mcp-config"]
    elif mode == "judge":
        cmd += ["--strict-mcp-config"]
    last = "unknown"
    for attempt in range(retries):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            last = "TIMEOUT"
        else:
            if out.returncode != 0:
                last = f"EXIT {out.returncode}: {out.stderr[:300]}"
            else:
                try:
                    return json.loads(out.stdout).get("result", ""), None
                except json.JSONDecodeError:
                    last = f"BAD JSON: {out.stdout[:300]}"
        # transient (rate limit / overload / blip) -> backoff and retry
        if attempt < retries - 1:
            time.sleep(2 + attempt * attempt * 3)
    return None, last


def judge(task, answer):
    prompt = (
        "You are grading an AI assistant's answer about the Flox package "
        "manager. Be strict and concrete.\n\n"
        f"TASK: {task['prompt']}\n\nRUBRIC: {task['rubric']}\n\n"
        f"ANSWER:\n{answer}\n\n"
        'Return ONLY a JSON object: {"score": <int 1-5>, "correct": <true|false>, '
        '"issues": [<short strings>]}'
    )
    result, err = run_claude(prompt, "judge", allow_tools=None)
    if err:
        return {"score": 0, "correct": False, "issues": [f"judge error: {err}"]}
    raw, m = {}, re.search(r"\{.*\}", result, re.S)
    if m:
        try:
            raw = json.loads(m.group(0))
        except json.JSONDecodeError:
            raw = {"issues": ["judge json parse fail"]}
    else:
        raw = {"issues": ["no json"]}
    # Normalize — the model occasionally omits a key; never let that KeyError later.
    try:
        score = int(raw.get("score", 0) or 0)
    except (TypeError, ValueError):
        score = 0
    return {"score": score, "correct": bool(raw.get("correct", False)),
            "issues": raw.get("issues", [])}


def process_task(t, mode, allow):
    """Run + score one task (agent call, hard-checks, judge). Thread-safe."""
    suffix = NEUTRAL_SUFFIX if t.get("trigger_test") else ANSWER_SUFFIX
    tier = t.get("tier", "should")
    base = {"id": t["id"], "area": t["area"], "tier": tier,
            "trigger_test": bool(t.get("trigger_test"))}
    answer, err = run_claude(t["prompt"] + suffix, mode, allow)
    if err:
        print(f"    [{tier}] {t['id']}: run error: {err}", flush=True)
        return {**base, "error": err}
    hard = {c: CHECKS[c](answer) for c in t["checks"]}
    hard_pass = all(hard.values())
    verdict = judge(t, answer)
    print(f"    [{tier}] {t['id']}: hard={'PASS' if hard_pass else 'FAIL'} "
          f"judge={verdict.get('score')}/5", flush=True)
    return {**base, "hard_checks": hard, "hard_pass": hard_pass,
            "judge": verdict, "answer_excerpt": answer[:1200]}


def _read_golden(name):
    """Load a committed results/<name> golden snapshot, or None if absent/bad."""
    try:
        return json.loads((HERE / "results" / name).read_text())
    except Exception:
        return None


def main():
    global MODEL, PLUGIN_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["skills", "skills+mcp", "baseline"], default="skills")
    ap.add_argument("--model", default=MODEL,
                    help=f"model id for both agent and judge (default {MODEL})")
    ap.add_argument("--tasks", default=str(HERE / "tasks.jsonl"))
    ap.add_argument("--only", help="run a single task id")
    ap.add_argument("--gate", action="store_true",
                    help="exit non-zero if binding gates fail (functional should-tier < 100%)")
    ap.add_argument("--plugin-dir", help="override the plugin dir (e.g. a pre-consolidation checkout)")
    ap.add_argument("--out", help="output filename under results/ (default: <mode>.json)")
    ap.add_argument("--concurrency", type=int, default=6,
                    help="parallel claude calls (default 6; lower if you hit rate limits)")
    args = ap.parse_args()

    MODEL = args.model
    if args.plugin_dir:
        PLUGIN_DIR = Path(args.plugin_dir).resolve()

    allow = ["Skill", "Read"]
    if args.mode == "skills+mcp":
        allow += ["mcp__flox-mcp"]

    tasks = [json.loads(l) for l in Path(args.tasks).read_text().splitlines() if l.strip()]
    if args.only:
        tasks = [t for t in tasks if t["id"] == args.only]

    n = min(args.concurrency, len(tasks)) or 1
    print(f"running {len(tasks)} tasks at concurrency {n} ({args.mode}) ...", flush=True)
    with ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(lambda t: process_task(t, args.mode, allow), tasks))

    scored = [r for r in results if "judge" in r]

    def stats(rs):
        n = max(len(rs), 1)
        return {
            "n": len(rs),
            "hard_pass_rate": round(sum(r["hard_pass"] for r in rs) / n, 3),
            "avg_judge_score": round(sum(r["judge"]["score"] for r in rs) / n, 2),
            "judge_correct_rate": round(sum(bool(r["judge"]["correct"]) for r in rs) / n, 3),
        }

    triggers = [r for r in scored if r["trigger_test"]]
    should_triggers = [r for r in triggers if r["tier"] == "should"]
    summary = {
        "mode": args.mode,
        "model": MODEL,
        "n_tasks": len(results),
        "n_errors": sum(1 for r in results if "error" in r),
        **stats(scored),
        "by_tier": {tier: stats([r for r in scored if r["tier"] == tier])
                    for tier in ("should", "may", "stretch") if any(r["tier"] == tier for r in scored)},
        # triggering is probabilistic — measured, not gated
        "n_trigger_tasks": len(triggers),
        "trigger_invokes_flox_rate": round(
            sum(r["hard_checks"].get("invokes_flox", False) for r in triggers) / max(len(triggers), 1), 3),
        "should_trigger_rate": round(
            sum(r["hard_checks"].get("invokes_flox", False) for r in should_triggers) / max(len(should_triggers), 1), 3),
    }
    out = {"summary": summary, "results": results}
    out_path = HERE / "results" / (args.out or f"{args.mode.replace('+', '_')}.json")
    # Snapshot this arm's committed golden BEFORE overwriting it, so the report can
    # diff against it (hard-check flips). The cross-arm metrics table reads the
    # other arms' goldens directly — those files aren't overwritten by this run.
    prev_golden = _read_golden(out_path.name)
    out_path.write_text(json.dumps(out, indent=2))
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))
    print(f"written: {out_path}")

    # Deterministic gate status: hard-checks on functional should-tier tasks.
    # The LLM judge's correctness/score is noisy run-to-run and triggering is
    # probabilistic, so both are *reported* but never fail the build — only the
    # structural checks do.
    binding = [r for r in scored if r["tier"] == "should" and not r["trigger_test"]]
    bad = [r for r in binding if not r["hard_pass"]]
    errs = [r for r in results if "error" in r and r.get("tier", "should") == "should"]

    write_step_summary(summary, results, binding, bad, errs, args.gate, prev_golden)

    if args.gate and (bad or errs):
        print(f"\nGATE FAILED: {len(bad)} functional should-tier task(s) failed hard-checks: "
              f"{[(r['id'], [k for k, v in r['hard_checks'].items() if not v]) for r in bad]}; errors: {[r['id'] for r in errs]}")
        sys.exit(1)
    if args.gate:
        print(f"\nGATE PASSED: all {len(binding)} functional should-tier tasks pass hard-checks. "
              f"(advisory: judge correct {summary['judge_correct_rate']}, avg {summary['avg_judge_score']}, "
              f"should-trigger {summary['should_trigger_rate']}).")


def _diff_vs_golden(summary, results, prev_golden):
    """Δ vs the of-record same-arm snapshot: hard-check flips (signal) + judge Δ (advisory)."""
    fname = f"{summary['mode'].replace('+', '_')}.json"
    if not prev_golden:
        return [f"### Δ vs main (`{fname}`)",
                f"_No committed golden for this arm — commit `evals/results/{fname}` to enable per-PR diffs._", ""]
    prev = {r["id"]: r for r in prev_golden.get("results", []) if "judge" in r}
    cur = {r["id"]: r for r in results if "judge" in r}
    regressed, fixed = [], []
    for tid in cur.keys() & prev.keys():
        if cur[tid]["hard_pass"] and not prev[tid]["hard_pass"]:
            fixed.append(tid)
        elif not cur[tid]["hard_pass"] and prev[tid]["hard_pass"]:
            regressed.append(f"`{tid}` ({cur[tid]['area']})")
    added = sorted(cur.keys() - prev.keys())
    removed = sorted(prev.keys() - cur.keys())
    ps = prev_golden.get("summary", {})
    lines = [f"### Hard-check diff vs main (of-record `{fname}`, model `{ps.get('model', '?')}`)"]
    lines.append(f"- ❌ **hard-check regressions ({len(regressed)}):** " + ", ".join(regressed)
                 if regressed else "- ✅ no hard-check regressions")
    if fixed:
        lines.append(f"- ✅ hard-check fixes ({len(fixed)}): " + ", ".join(f"`{t}`" for t in fixed))
    if added:
        lines.append(f"- ➕ new tasks ({len(added)}): " + ", ".join(f"`{t}`" for t in added))
    if removed:
        lines.append(f"- ➖ removed tasks ({len(removed)}): " + ", ".join(f"`{t}`" for t in removed))
    if "avg_judge_score" in ps:
        dj = round(summary["avg_judge_score"] - ps["avg_judge_score"], 2)
        lines.append(f"- judge avg {summary['avg_judge_score']} vs {ps['avg_judge_score']} "
                     f"(Δ {dj:+}) — _advisory, judge is noisy run-to-run_")
    lines.append("")
    return lines


def _metrics_table(summary):
    """Cross-arm metrics: this run (live) for its arm, committed goldens for the others."""
    arms = [("baseline", "baseline.json"), ("skills", "skills.json"), ("skills+mcp", "skills_mcp.json")]
    summ = {}
    for arm, fn in arms:
        if arm == summary["mode"]:
            summ[arm] = summary
        else:
            g = _read_golden(fn)
            summ[arm] = g.get("summary") if g else None

    def cell(arm, key, pct):
        s = summ[arm]
        if not s or key not in s:
            return "—"
        return f"{s[key]:.0%}" if pct else f"{s[key]:.2f}"

    def delta(key, pct):
        b, s = summ["baseline"], summ["skills"]
        if not b or not s or key not in b or key not in s:
            return "—"
        d = s[key] - b[key]
        return f"{d * 100:+.1f}pp" if pct else f"{d:+.2f}"

    def hdr(arm):
        return f"**{arm}**" if arm == summary["mode"] else arm

    metrics = [("Hard-pass", "hard_pass_rate", True), ("Avg judge", "avg_judge_score", False),
               ("Judge-correct", "judge_correct_rate", True), ("Should-trigger", "should_trigger_rate", True)]
    rows = [f"| metric | {hdr('baseline')} | {hdr('skills')} | {hdr('skills+mcp')} | Δ skills−baseline |",
            "|---|--:|--:|--:|--:|"]
    for label, key, pct in metrics:
        rows.append(f"| {label} | {cell('baseline', key, pct)} | {cell('skills', key, pct)} "
                    f"| {cell('skills+mcp', key, pct)} | {delta(key, pct)} |")
    return rows


def write_step_summary(summary, results, binding, bad, errs, gate_enabled, prev_golden=None):
    """Render a markdown report to $GITHUB_STEP_SUMMARY (the Actions run page)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    scored = [r for r in results if "judge" in r]
    if gate_enabled:
        verdict = "❌ **GATE FAILED**" if (bad or errs) else "✅ **GATE PASSED**"
    else:
        verdict = "ℹ️ measurement run (gate off)"

    out = [f"## Skill evals — **`{summary['mode']}`** arm (this run) — {verdict}", "",
           f"**Model** (agent + judge): `{summary.get('model', 'unknown')}` · "
           f"**{summary['n_tasks']} tasks** ({summary['n_errors']} errors)", ""]

    out += ["### Metrics", "",
            "- **Hard-pass** — share of tasks whose answer clears every deterministic "
            "structural check (e.g. has an `[install]` section, no hallucinated install URL). "
            "This is what the gate enforces.",
            "- **Avg judge** — average 1–5 quality score from an LLM judge grading each answer "
            "against that task's rubric.",
            "- **Judge-correct** — share of answers the judge marked factually correct.",
            "- **Should-trigger** — of the prompts that never mention Flox, the share where the "
            "assistant still proactively recommends it.", ""]
    out += _metrics_table(summary)
    out += ["",
            "_Arms: **baseline** = bare model (no plugin/MCP) · **skills** = plugin loaded, MCP "
            "off · **skills+mcp** = plugin + Flox MCP server. Bold column = this run (live); the "
            "others are the last committed golden (`—` if none). Δ compares skills-only to "
            "baseline, since the MCP is being deprecated._", ""]

    areas = {}
    for r in scored:
        areas.setdefault(r["area"], []).append(r)
    out += ["### By area", "| area | n | hard-pass | avg judge |", "|---|--:|--:|--:|"]
    for area in sorted(areas):
        rs = areas[area]
        hp = sum(x["hard_pass"] for x in rs) / len(rs)
        aj = sum(x["judge"]["score"] for x in rs) / len(rs)
        out.append(f"| {area} | {len(rs)} | {hp:.0%} | {aj:.1f} |")
    out.append("")

    out += _diff_vs_golden(summary, results, prev_golden)

    flags = []
    for r in results:
        if "error" in r:
            flags.append(f"- ⚠️ `{r['id']}` ({r['area']}): error — {r['error'][:80]}")
        elif not r["hard_pass"]:
            failed = ", ".join(k for k, v in r["hard_checks"].items() if not v)
            flags.append(f"- ❌ `{r['id']}` ({r['area']}, {r['tier']}): hard-check failed — {failed}")
        elif r["judge"]["score"] <= 2:
            issues = "; ".join(r["judge"].get("issues", [])[:2])
            flags.append(f"- 🟡 `{r['id']}` ({r['area']}, {r['tier']}): judge {r['judge']['score']}/5 — {issues}")
    if flags:
        out += ["### Needs attention", *flags, ""]

    out += ["<details><summary>All tasks</summary>", "",
            "| task | area | tier | hard | judge |", "|---|---|---|:--:|:--:|"]
    for r in results:
        if "error" in r:
            out.append(f"| {r['id']} | {r['area']} | {r['tier']} | ERROR | – |")
        else:
            hp = "✅" if r["hard_pass"] else "❌"
            out.append(f"| {r['id']} | {r['area']} | {r['tier']} | {hp} | {r['judge']['score']}/5 |")
    out += ["", "</details>", ""]

    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
