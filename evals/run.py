#!/usr/bin/env python3
"""Flox skills eval harness.

Runs each task in tasks.jsonl through `claude` headless, in one of two arms:

  --mode skills       skills only, MCP disabled (--strict-mcp-config, no --mcp-config)
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
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import skill_toml_lint

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent / "flox-plugin"
MODEL = "claude-opus-4-8"  # pinned for reproducible scores; override with --model

# Setting-source isolation (screening only). When set (e.g. "project,local"),
# each `claude` call is invoked with `--setting-sources <value>`, which drops
# USER-level settings — most importantly `enabledPlugins`. On a machine where
# the Flox plugin is globally enabled in ~/.claude/settings.json (as it is on
# the dev/night-shift hosts), the plain baseline arm would otherwise load that
# plugin and stop being a bare model — the baseline answers "Based on the Flox
# guide" and the discrimination signal collapses to zero. Excluding "user"
# suppresses the global plugin while OAuth credentials (a separate file) still
# load, so `flox run`/`flox activate allow` etc. are only known to the skills
# arm (which re-adds exactly one plugin via --plugin-dir). None = load all
# sources (run.py's original behavior; the gate is unaffected by default).
SETTING_SOURCES = None

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


def _fenced_manifests(text):
    """Every fenced ```toml block in `text`, in document order.

    Delegates fence handling to `skill_toml_lint.extract_blocks`, which is
    indent- and info-string-aware and unit-tested in this same package. The
    regex this replaced (`` ```(?:toml)?\\n(.*?)``` ``) matched an empty info
    string, so a bare *closing* fence read as an opening one: in an answer
    whose ```bash block preceded its ```toml block, the manifest was silently
    lost. Since ANSWER_SUFFIX asks for the manifest *and* the commands, that
    is the expected shape of a correct answer.
    """
    try:
        return skill_toml_lint.extract_blocks(text, "<answer>")
    except ValueError:
        # A model answer can end mid-fence. Close it and retry rather than
        # dropping every block in the answer.
        try:
            return skill_toml_lint.extract_blocks(text + "\n```\n", "<answer>")
        except ValueError:
            return []


def toml_blocks(text):
    return "\n".join(b.body for b in _fenced_manifests(text))


def _parsed_manifests(text):
    """Each fenced ```toml block parsed with `tomllib`, as a dict.

    Blocks that are not valid TOML are dropped: a check cannot certify a
    manifest flox would refuse to read.
    """
    out = []
    for block in _fenced_manifests(text):
        try:
            out.append(tomllib.loads(block.body))
        except (tomllib.TOMLDecodeError, ValueError):
            continue
    return out


# `services.auto-start` (AI-503). Two things are checkable and both are things a
# model gets wrong without the skill:
#
#   1. Placement. The key belongs to the `[services]` table itself, alongside the
#      service names — under a `[services.<name>]` block flox rejects it
#      ("unknown field `auto-start`"). A plain string grep would pass that wrong
#      manifest, so _sets_auto_start tracks the enclosing table.
#   2. Schema version. The key was introduced in schema 1.12.0; in a
#      `version = 1` manifest it fails to parse ("invalid type: boolean `true`,
#      expected struct ServiceDescriptor"). An answer that never mentions
#      `schema-version` hands the user a manifest that cannot be loaded.
# Both facts are asserted against the *parsed* manifest rather than its text.
# The line scanner this replaced tracked the enclosing table with a regex and
# had no `'''`/`\"\"\"` state, so it both over- and under-reported: an
# `auto-start = true` line inside a multiline command body counted as a real
# key, and a `[ -d node_modules ] || npm ci` line inside one set the current
# table to `-d node_modules`, hiding a correct key that followed. Asking
# `tomllib` for `services["auto-start"]` makes both impossible.
# Full `X.Y.Z` only — flox matches the value against a literal list, so a
# two-component `"1.12"` is rejected outright (`manifest had invalid schema
# version '1.12'`, verified on flox 1.13.2).
_SCHEMA_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_MIN_AUTO_START_SCHEMA = (1, 12)


def _auto_start_manifests(answer):
    """Parsed manifests that set `auto-start = true` on the `[services]` table.

    Scoped per block (each fenced block is its own manifest) so a `[services]`
    header in one snippet can't vouch for a stray `auto-start` line in another,
    and `[services.<name>] auto-start = true` — which flox rejects with
    ``unknown field `auto-start` `` — lands under the service, not `services`,
    so it correctly does not count.
    """
    return [
        m for m in _parsed_manifests(answer)
        if isinstance(m.get("services"), dict) and m["services"].get("auto-start") is True
    ]


def _sets_auto_start(answer):
    return bool(_auto_start_manifests(answer))


def _schema_at_least(value, minimum):
    """True iff `value` is a version string at or above `minimum` (major, minor)."""
    if not isinstance(value, str):
        return False
    m = _SCHEMA_VERSION.match(value.strip())
    return bool(m) and (int(m.group(1)), int(m.group(2))) >= minimum


def _auto_start_schema_version(answer):
    """True iff the block that carries `auto-start` also carries a new-enough schema.

    All three facts are asserted against the *same* manifest. Searching the
    whole answer certified manifests it never inspected: an answer whose prose
    said `schema-version = "1.12.0"` while its only fenced manifest kept
    `version = 1` passed every check in the task, and that manifest does not
    load (``invalid type: boolean `true`, expected struct ServiceDescriptor``)
    — which is the exact RED failure this task exists to catch.
    """
    return any(
        _schema_at_least(m.get("schema-version"), _MIN_AUTO_START_SCHEMA)
        # `version` and `schema-version` are mutually exclusive in flox; a
        # surviving `version = 1` line means the manifest is still rejected.
        and "version" not in m
        for m in _auto_start_manifests(answer)
    )


# Build sandbox modes (AI-503, second half). `sandbox = "warn"|"enforce"` and
# `sandbox-allow` all arrived with schema 1.13.0, so an answer that uses them
# under `version = 1` hands the user a manifest that will not load:
# ``unknown variant `warn`, expected `off` or `pure` ``. Same shape as the
# auto-start pair: placement, then the version line that makes it parse.
_MIN_SANDBOX_MODE_SCHEMA = (1, 13)
_GATED_SANDBOX_MODES = {"warn", "enforce"}


def _sandbox_mode_manifests(answer):
    """Parsed manifests using a 1.13.0-gated build sandbox field."""
    out = []
    for m in _parsed_manifests(answer):
        builds = m.get("build")
        if not isinstance(builds, dict):
            continue
        for descriptor in builds.values():
            if isinstance(descriptor, dict) and (
                descriptor.get("sandbox") in _GATED_SANDBOX_MODES
                or "sandbox-allow" in descriptor
            ):
                out.append(m)
                break
    return out


def _sets_sandbox_mode(answer):
    return bool(_sandbox_mode_manifests(answer))


def _sandbox_schema_version(answer):
    """True iff the block using the gated sandbox field also declares schema 1.13.0+."""
    return any(
        _schema_at_least(m.get("schema-version"), _MIN_SANDBOX_MODE_SCHEMA)
        and "version" not in m
        for m in _sandbox_mode_manifests(answer)
    )


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
    "sets_services_auto_start": _sets_auto_start,
    "auto_start_schema_version": _auto_start_schema_version,
    "sets_build_sandbox_mode": _sets_sandbox_mode,
    "build_sandbox_schema_version": _sandbox_schema_version,
    "uses_remote_env": lambda a: "flox push" in a or "flox pull" in a or "flox activate -r" in a,
    # Implicit-trigger check: did the skill fire and produce Flox guidance even
    # though the prompt never said "flox"?
    "invokes_flox": lambda a: bool(
        re.search(r"\bflox\b", a, re.I)
        and (re.search(r"flox (init|install|search|show|containerize|publish|build|activate|push|edit)", a)
             or "[install]" in a or "manifest.toml" in a)),
}


# --- cost accounting (AI-459) ----------------------------------------------
# `claude -p --output-format json` returns total_cost_usd + usage on EVERY
# call. This harness read only `.result` and dropped the rest, so we could not
# answer "what does a run cost?" from our own data — while spending it on every
# PR. Measured: one agent call on a real task is $1.27 (18.6k output, ~957k
# cache-read, 406s); a trivial 4-token reply is still $0.088. At 27 tasks x
# (agent + judge) that is ~$40/run, which is why CI is defunded until this is
# visible.

def _parse_meta(envelope):
    """Extract cost/usage from a claude JSON envelope. Never raises — a
    cost-accounting detail must not be able to break an eval run."""
    try:
        cost = float(envelope.get("total_cost_usd") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    usage = envelope.get("usage")
    try:
        duration = int(envelope.get("duration_ms") or 0)
    except (TypeError, ValueError):
        duration = 0
    return {
        "cost_usd": cost,
        "usage": usage if isinstance(usage, dict) else {},
        "duration_ms": duration,
    }


ZERO_META = {"cost_usd": 0.0, "usage": {}, "duration_ms": 0}


def _cost_summary(results):
    """Roll per-task cost into a run total, split agent vs judge.

    The judge split matters: it is half of every run's calls, pinned to the
    same frontier model as the agent, for a grading job AI-451 shows it does
    badly. That trade is invisible without this number.
    """
    costed = [r["cost"] for r in results if "cost" in r]
    agent = sum(c.get("agent_usd", 0.0) for c in costed)
    judge_total = sum(c.get("judge_usd", 0.0) for c in costed)
    total = sum(c.get("total_usd", 0.0) for c in costed)
    return {
        "total_usd": round(total, 4),
        "agent_usd": round(agent, 4),
        "judge_usd": round(judge_total, 4),
        "mean_per_task_usd": round(total / len(costed), 4) if costed else 0.0,
        "n_costed_tasks": len(costed),
    }


def run_claude(prompt, mode, allow_tools, timeout=420, retries=3):
    cmd = ["claude", "-p", prompt, "--model", MODEL, "--output-format", "json"]
    if allow_tools:
        cmd += ["--allowedTools", *allow_tools]
    # Isolate from globally-enabled plugins so the baseline stays a bare model
    # (see SETTING_SOURCES above). Applies to every arm — baseline, skills, and
    # judge — so the only Flox context in the skills arm is the --plugin-dir one.
    if SETTING_SOURCES:
        cmd += ["--setting-sources", SETTING_SOURCES]
    if mode == "skills":
        cmd += ["--plugin-dir", str(PLUGIN_DIR), "--strict-mcp-config"]
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
                    envelope = json.loads(out.stdout)
                except json.JSONDecodeError:
                    last = f"BAD JSON: {out.stdout[:300]}"
                else:
                    return (
                        envelope.get("result", ""),
                        None,
                        _parse_meta(envelope),
                    )
        # transient (rate limit / overload / blip) -> backoff and retry
        if attempt < retries - 1:
            time.sleep(2 + attempt * attempt * 3)
    # A failed call may still have burned tokens, but the envelope is gone.
    # Return a zeroed meta so callers can sum unconditionally.
    return None, last, dict(ZERO_META)


def judge(task, answer):
    prompt = (
        "You are grading an AI assistant's answer about the Flox package "
        "manager. Be strict and concrete.\n\n"
        f"TASK: {task['prompt']}\n\nRUBRIC: {task['rubric']}\n\n"
        f"ANSWER:\n{answer}\n\n"
        'Return ONLY a JSON object: {"score": <int 1-5>, "correct": <true|false>, '
        '"issues": [<short strings>]}'
    )
    result, err, meta = run_claude(prompt, "judge", allow_tools=None)
    if err:
        return {"score": 0, "correct": False,
                "issues": [f"judge error: {err}"]}, meta
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
            "issues": raw.get("issues", [])}, meta


def process_task(t, mode, allow):
    """Run + score one task (agent call, hard-checks, judge). Thread-safe."""
    suffix = NEUTRAL_SUFFIX if t.get("trigger_test") else ANSWER_SUFFIX
    tier = t.get("tier", "should")
    base = {"id": t["id"], "area": t["area"], "tier": tier,
            "trigger_test": bool(t.get("trigger_test"))}
    answer, err, agent_meta = run_claude(t["prompt"] + suffix, mode, allow)
    if err:
        print(f"    [{tier}] {t['id']}: run error: {err}", flush=True)
        return {**base, "error": err, "cost": {
            "agent_usd": agent_meta["cost_usd"], "judge_usd": 0.0,
            "total_usd": agent_meta["cost_usd"]}}
    hard = {c: CHECKS[c](answer) for c in t["checks"]}
    hard_pass = all(hard.values())
    verdict, judge_meta = judge(t, answer)
    cost = {
        "agent_usd": round(agent_meta["cost_usd"], 4),
        "judge_usd": round(judge_meta["cost_usd"], 4),
        "total_usd": round(agent_meta["cost_usd"] + judge_meta["cost_usd"], 4),
    }
    print(f"    [{tier}] {t['id']}: hard={'PASS' if hard_pass else 'FAIL'} "
          f"judge={verdict.get('score')}/5  ${cost['total_usd']:.2f}", flush=True)
    return {**base, "hard_checks": hard, "hard_pass": hard_pass,
            "judge": verdict, "cost": cost,
            "usage": {"agent": agent_meta["usage"], "judge": judge_meta["usage"]},
            "duration_ms": {"agent": agent_meta["duration_ms"],
                            "judge": judge_meta["duration_ms"]},
            "answer_excerpt": answer[:1200]}


def _read_golden(name):
    """Load a committed results/<name> golden snapshot, or None if absent/bad."""
    try:
        return json.loads((HERE / "results" / name).read_text())
    except Exception:
        return None


def build_parser():
    """The CLI parser, extracted so a test can render every help string.

    argparse percent-expands help text lazily, so a bare `%` is only caught
    when the help is *formatted* — `--gate`'s "< 100%)" made the harness die
    on import under Python 3.14 (`ValueError: badly formed help string`) and
    on `--help` under 3.11, which CI pins. Nothing in the suite constructed
    the parser, so no test could have caught it. `test_run.py` now calls
    `format_help()` on this, which covers every help string in the file.
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["skills", "baseline"], default="skills")
    ap.add_argument("--model", default=MODEL,
                    help=f"model id for both agent and judge (default {MODEL})")
    ap.add_argument("--tasks", default=str(HERE / "tasks.jsonl"))
    ap.add_argument("--only", help="run a single task id")
    ap.add_argument("--gate", action="store_true",
                    # `%%` — argparse percent-expands help strings, and a bare
                    # `%)` raises "badly formed help string" on Python 3.14.
                    help="exit non-zero if binding gates fail (functional should-tier < 100%%)")
    ap.add_argument("--plugin-dir", help="override the plugin dir (e.g. a pre-consolidation checkout)")
    ap.add_argument("--out", help="output filename under results/ (default: <mode>.json)")
    ap.add_argument("--concurrency", type=int, default=6,
                    help="parallel claude calls (default 6; lower if you hit rate limits)")
    return ap


def main():
    global MODEL, PLUGIN_DIR
    args = build_parser().parse_args()

    MODEL = args.model
    if args.plugin_dir:
        PLUGIN_DIR = Path(args.plugin_dir).resolve()

    allow = ["Skill", "Read"]

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
        "cost": _cost_summary(results),
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
    """Cross-arm metrics: this run (live) for its arm, committed golden for the other."""
    arms = [("baseline", "baseline.json"), ("skills", "skills.json")]
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
    rows = [f"| metric | {hdr('baseline')} | {hdr('skills')} | Δ skills−baseline |",
            "|---|--:|--:|--:|"]
    for label, key, pct in metrics:
        rows.append(f"| {label} | {cell('baseline', key, pct)} | {cell('skills', key, pct)} "
                    f"| {delta(key, pct)} |")
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

    cost = summary.get("cost") or {}
    cost_line = ""
    if cost.get("total_usd"):
        cost_line = (
            f" · **cost: ${cost['total_usd']:.2f}** "
            f"(agent ${cost.get('agent_usd', 0):.2f} + judge "
            f"${cost.get('judge_usd', 0):.2f}, "
            f"${cost.get('mean_per_task_usd', 0):.2f}/task)"
        )

    out = [f"## Skill evals — **`{summary['mode']}`** arm (this run) — {verdict}", "",
           f"**Model** (agent + judge): `{summary.get('model', 'unknown')}` · "
           f"**{summary['n_tasks']} tasks** ({summary['n_errors']} errors)"
           f"{cost_line}", ""]

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
            "_Arms: **baseline** = bare model, no plugin loaded · **skills** = plugin "
            "loaded. Bold column = this run (live); the other is the last committed "
            "golden (`—` if none). Δ compares skills-only to baseline._", ""]

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
