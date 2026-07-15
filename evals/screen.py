#!/usr/bin/env python3
"""Discrimination screening harness for stretch-tier eval candidates.

For each candidate in candidates.jsonl, runs the baseline arm (bare model,
no plugin) and the skills arm (plugin loaded, MCP off), scores both, and
classifies the candidate:

  discriminator  skills passes (hard OR judge_correct) while baseline fails
                 the same measure — promote this candidate to tasks.jsonl
  skill-gap      both arms fail — the skill may not cover this capability;
                 report separately rather than discarding
  no-signal      baseline already passes — candidate is too easy, needs
                 hardening before it can discriminate

Hard-check logic is data-driven per candidate (must_match / must_not_match
regex lists on the candidate record), replacing run.py's fixed CHECKS
registry. run_claude and judge are imported directly from run.py so the
same model pin, retry logic, and judge prompt are reused verbatim.

Output: results/screen.json  (full per-candidate records + summary)
Stdout: ranked table sorted by judge_gap descending
"""
import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Pull reusable machinery from run.py without reimplementing it.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run as _run
from run import run_claude, judge, NEUTRAL_SUFFIX, ANSWER_SUFFIX


def hard_check(answer: str, must_match: list, must_not_match: list) -> bool:
    """Return True iff every must_match regex is found and no must_not_match fires.

    Case-insensitive multiline matching applied to each pattern, consistent
    with how run.py's CHECKS regexes are used (re.I | re.M on the patterns).
    """
    flags = re.IGNORECASE | re.MULTILINE
    for pattern in must_match:
        if not re.search(pattern, answer, flags):
            return False
    for pattern in must_not_match:
        if re.search(pattern, answer, flags):
            return False
    return True


def _score_arm(candidate: dict, mode: str, allow_tools: list, reps: int = 1) -> dict:
    """Run one arm for a candidate `reps` times and return aggregated scores.

    Aggregation is what makes screening trustworthy: single runs have a high
    cell-level flip rate (observed ~50% on the baseline arm), so an individual
    P/F verdict is dominated by sampling noise. With reps>1 we report
    `hard_pass_rate` (fraction of reps whose deterministic check passed) and a
    mean judge score; `hard_pass`/`judge_correct` become the majority verdicts
    used for classification.

    For trigger_test candidates the neutral suffix is used (same path as
    run.py's trigger tasks) so the skill fires on its own without any
    Flox-directed bias in the prompt.
    """
    suffix = NEUTRAL_SUFFIX if candidate.get("trigger_test") else ANSWER_SUFFIX
    prompt = candidate["prompt"] + suffix

    hard_hits = 0
    judge_scores = []
    judge_correct_hits = 0
    first_excerpt = ""
    errors = []
    for _ in range(reps):
        answer, err = run_claude(prompt, mode, allow_tools)
        if err:
            errors.append(err)
            continue
        if hard_check(answer, candidate.get("must_match", []),
                      candidate.get("must_not_match", [])):
            hard_hits += 1
        verdict = judge(candidate, answer)
        judge_scores.append(verdict["score"])
        if verdict["correct"]:
            judge_correct_hits += 1
        if not first_excerpt:
            first_excerpt = answer[:1200]

    ok = reps - len(errors)  # reps that produced a scorable answer
    if ok == 0:
        return {
            "hard_pass": False, "hard_pass_rate": 0.0, "hard_pass_count": 0,
            "reps": reps, "ok_reps": 0,
            "judge_score": 0, "judge_correct": False,
            "judge_issues": [f"arm error: {errors[0] if errors else 'unknown'}"],
            "error": errors[0] if errors else "unknown", "answer_excerpt": "",
        }
    rate = hard_hits / ok
    mean_judge = round(sum(judge_scores) / len(judge_scores), 2) if judge_scores else 0
    return {
        "hard_pass": rate >= 0.5,                 # majority, for classification
        "hard_pass_rate": round(rate, 3),
        "hard_pass_count": hard_hits,
        "reps": reps, "ok_reps": ok,
        "judge_score": mean_judge,                # mean over reps
        "judge_correct": judge_correct_hits * 2 >= ok,
        "judge_issues": [],
        "answer_excerpt": first_excerpt,
    }


def screen_candidate(candidate: dict, allow_tools: list, reps: int = 1) -> dict:
    """Run both arms for one candidate and compute discrimination metrics."""
    cid = candidate["id"]

    baseline = _score_arm(candidate, "baseline", allow_tools, reps)
    skills = _score_arm(candidate, "skills", allow_tools, reps)

    discriminates_hard = skills["hard_pass"] and not baseline["hard_pass"]
    discriminates_judge = skills["judge_correct"] and not baseline["judge_correct"]
    judge_gap = round(skills["judge_score"] - baseline["judge_score"], 2)

    # Classification priority:
    #   1. discriminator — skill shows measurable lift
    #   2. skill-gap     — skills arm also fails; skill may be missing coverage
    #   3. no-signal     — baseline already passes; candidate needs hardening
    if discriminates_hard or discriminates_judge:
        classification = "discriminator"
    elif not skills["hard_pass"] and not skills["judge_correct"]:
        classification = "skill-gap"
    else:
        classification = "no-signal"

    b_err = baseline.get("error", "")
    s_err = skills.get("error", "")
    err_note = (
        f" [BASE ERR: {b_err[:60]}]" if b_err else ""
        + f" [SKILLS ERR: {s_err[:60]}]" if s_err else ""
    )
    def arm_str(a):
        return f"{a['hard_pass_count']}/{a['ok_reps']}h,j{a['judge_score']}"
    print(
        f"  {cid}  "
        f"base={arm_str(baseline)}  "
        f"skills={arm_str(skills)}  "
        f"gap={judge_gap:+.1f}  {classification}{err_note}",
        flush=True,
    )

    # Build per-arm record; omit error key when absent to keep JSON clean.
    def arm_record(arm: dict) -> dict:
        rec = {
            "hard_pass": arm["hard_pass"],
            "hard_pass_rate": arm.get("hard_pass_rate"),
            "hard_pass_count": arm.get("hard_pass_count"),
            "reps": arm.get("reps"),
            "ok_reps": arm.get("ok_reps"),
            "judge_score": arm["judge_score"],
            "judge_correct": arm["judge_correct"],
            "judge_issues": arm["judge_issues"],
            "answer_excerpt": arm["answer_excerpt"],
        }
        if arm.get("error"):
            rec["error"] = arm["error"]
        return rec

    return {
        "id": cid,
        "area": candidate["area"],
        "tier": candidate.get("tier", "stretch"),
        "trigger_test": bool(candidate.get("trigger_test")),
        "baseline": arm_record(baseline),
        "skills": arm_record(skills),
        "discriminates_hard": discriminates_hard,
        "discriminates_judge": discriminates_judge,
        "judge_gap": judge_gap,
        "classification": classification,
    }


def print_ranked_table(results: list) -> None:
    """Print candidates ranked by judge_gap descending."""
    sorted_rs = sorted(results, key=lambda r: r["judge_gap"], reverse=True)

    id_w = max(len(r["id"]) for r in results)
    area_w = max(len(r["area"]) for r in results)
    cls_w = max(len(r["classification"]) for r in results)

    # Header
    hdr = (
        f"{'id':<{id_w}}  {'area':<{area_w}}  "
        f"{'base(hard/judge)':<16}  {'skills(hard/judge)':<18}  "
        f"{'gap':>4}  {'classification'}"
    )
    print("\n" + hdr)
    print("-" * len(hdr))

    def cell(a):
        # "<hard_count>/<ok_reps>h j<mean>" — shows the pass-rate, not a lone P/F
        return f"{a.get('hard_pass_count')}/{a.get('ok_reps')}h j{a['judge_score']}"

    for r in sorted_rs:
        b, s = r["baseline"], r["skills"]
        err_flag = " [ERR]" if b.get("error") or s.get("error") else ""
        print(
            f"{r['id']:<{id_w}}  {r['area']:<{area_w}}  "
            f"{cell(b):<16}  {cell(s):<18}  "
            f"{r['judge_gap']:>+5.1f}  {r['classification']}{err_flag}"
        )
    print()


def main():
    ap = argparse.ArgumentParser(
        description="Screen candidate prompts: run baseline + skills arms and classify."
    )
    ap.add_argument(
        "--candidates",
        default=str(HERE / "candidates.jsonl"),
        help="path to candidates.jsonl (default: candidates.jsonl next to screen.py)",
    )
    ap.add_argument("--only", help="run a single candidate id")
    ap.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help=(
            "parallel candidates (default 4; each candidate = 2 agent + 2 judge "
            "calls, so keep conservative to avoid rate limits)"
        ),
    )
    ap.add_argument(
        "--out",
        default=str(HERE / "results" / "screen.json"),
        help="output path (default: results/screen.json)",
    )
    ap.add_argument(
        "--model",
        default=_run.MODEL,
        help=f"model id for agent and judge (default {_run.MODEL})",
    )
    ap.add_argument(
        "--plugin-dir",
        help="override the skills-arm plugin dir (e.g. a fixed-skill worktree)",
    )
    ap.add_argument(
        "--reps",
        type=int,
        default=1,
        help=(
            "runs per arm per candidate (default 1). Use >=5 for trustworthy "
            "pass-rates — single runs have a high cell-level flip rate."
        ),
    )
    args = ap.parse_args()

    # Propagate model override into run.py's module-level constant so that
    # both run_claude and judge pick it up without reimplementing the call.
    _run.MODEL = args.model
    if args.plugin_dir:
        _run.PLUGIN_DIR = Path(args.plugin_dir).resolve()

    # Skill and Read tools allowed; for the baseline arm no plugin is loaded
    # so the Skill tool is effectively unavailable — passing it is harmless.
    allow_tools = ["Skill", "Read"]

    candidates = [
        json.loads(line)
        for line in Path(args.candidates).read_text().splitlines()
        if line.strip()
    ]
    if args.only:
        candidates = [c for c in candidates if c["id"] == args.only]
        if not candidates:
            print(f"No candidate with id '{args.only}' found.", file=sys.stderr)
            sys.exit(1)

    n = min(args.concurrency, len(candidates)) or 1
    print(
        f"Screening {len(candidates)} candidate(s) at concurrency {n} "
        f"(baseline + skills per candidate, reps={args.reps}, model={args.model}) ...",
        flush=True,
    )

    with ThreadPoolExecutor(max_workers=n) as ex:
        results = list(
            ex.map(lambda c: screen_candidate(c, allow_tools, args.reps), candidates)
        )

    print_ranked_table(results)

    # Bucketed classification lists
    discriminators = [r for r in results if r["classification"] == "discriminator"]
    skill_gaps = [r for r in results if r["classification"] == "skill-gap"]
    no_signals = [r for r in results if r["classification"] == "no-signal"]
    errored = [
        r for r in results if r["baseline"].get("error") or r["skills"].get("error")
    ]

    # Mean judge_gap over candidates without arm errors
    scored = [
        r for r in results
        if not r["baseline"].get("error") and not r["skills"].get("error")
    ]
    mean_gap = round(sum(r["judge_gap"] for r in scored) / len(scored), 2) if scored else 0.0

    def mean_rate(arm_key):
        vals = [r[arm_key].get("hard_pass_rate") for r in scored
                if r[arm_key].get("hard_pass_rate") is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    summary = {
        "model": args.model,
        "reps": args.reps,
        "mean_baseline_hard_pass_rate": mean_rate("baseline"),
        "mean_skills_hard_pass_rate": mean_rate("skills"),
        "total": len(results),
        "errors": len(errored),
        "discriminators": len(discriminators),
        "skill_gaps": len(skill_gaps),
        "no_signals": len(no_signals),
        "mean_judge_gap": mean_gap,
        "discriminator_ids": [r["id"] for r in discriminators],
        "skill_gap_ids": [r["id"] for r in skill_gaps],
        "no_signal_ids": [r["id"] for r in no_signals],
        "error_ids": [r["id"] for r in errored],
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"summary": summary, "results": results}, indent=2))

    print("=== SCREEN SUMMARY ===")
    print(json.dumps(summary, indent=2))
    print(f"\nwritten: {out_path}")

    if errored:
        print(
            f"\nWARNING: {len(errored)} candidate(s) had arm errors — "
            f"check 'error' fields in {out_path}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
