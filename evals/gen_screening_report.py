#!/usr/bin/env python3
"""Render the AI-435/AI-439 discrimination-screening report from screen.py output.

Reads results/screen-<model>.json for each screened model and emits a single
markdown report: per-model summary, a merged per-candidate table (per-model
classification + judge gap + hard-pass spread), and a ranked list of the
strongest discriminators (those that separate baseline from skills on the most
models). Pure stdlib; safe to re-run as models finish (skips missing files).

Usage:
  flox activate -- python3 gen_screening_report.py \
      --results results/screen-haiku.json results/screen-sonnet.json results/screen-opus.json \
      --candidates candidates.jsonl \
      --out SCREENING-REPORT.md
"""
import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Short model label from the pinned id.
LABELS = {
    "claude-haiku-4-5-20251001": "haiku",
    "claude-sonnet-5": "sonnet",
    "claude-opus-4-8": "opus",
}
CLS_ABBR = {"discriminator": "DISC", "skill-gap": "GAP", "no-signal": "—"}


def load(paths):
    """Return {model_label: {"summary":..., "byid": {id: result}}} for existing files."""
    out = {}
    for p in paths:
        fp = Path(p)
        if not fp.exists():
            continue
        data = json.loads(fp.read_text())
        summ = data["summary"]
        label = LABELS.get(summ.get("model", ""), summ.get("model", fp.stem))
        out[label] = {
            "summary": summ,
            "byid": {r["id"]: r for r in data["results"]},
        }
    return out


def hp(arm):
    c = arm.get("hard_pass_count")
    n = arm.get("ok_reps")
    if c is None or not n:
        return "?"
    return f"{c}/{n}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", nargs="+", required=True)
    ap.add_argument("--candidates", default=str(HERE / "candidates.jsonl"))
    ap.add_argument("--out", default=str(HERE / "SCREENING-REPORT.md"))
    args = ap.parse_args()

    models = load(args.results)  # ordered by insertion of existing files
    order = [m for m in ("haiku", "sonnet", "opus") if m in models] or list(models)

    cands = [json.loads(l) for l in Path(args.candidates).read_text().splitlines() if l.strip()]
    cand_by_id = {c["id"]: c for c in cands}
    ids = [c["id"] for c in cands]

    # Per-candidate cross-model aggregation.
    rows = []
    for cid in ids:
        c = cand_by_id[cid]
        per = {}
        disc_models = 0
        gaps = []
        for m in order:
            r = models[m]["byid"].get(cid)
            if not r:
                per[m] = None
                continue
            per[m] = r
            if r["classification"] == "discriminator":
                disc_models += 1
            gaps.append(r["judge_gap"])
        mean_gap = round(sum(gaps) / len(gaps), 2) if gaps else 0.0
        rows.append({
            "id": cid, "area": c["area"], "trig": bool(c.get("trigger_test")),
            "per": per, "disc_models": disc_models, "mean_gap": mean_gap,
        })

    # Rank: most models discriminating, then mean judge gap.
    ranked = sorted(rows, key=lambda x: (x["disc_models"], x["mean_gap"]), reverse=True)

    L = []
    L.append("# AI-435 / AI-439 — Triggering + Freshness Discrimination Screening\n")
    L.append("Research pass for AI-439 (child of AI-435). **Nothing here is promoted "
             "into `tasks.jsonl`** — promotion and the eval-model-policy choice are "
             "Bill decisions (AI-439 is blocked on the model policy). This artifact is "
             "`evals/candidates.jsonl` + this report only.\n")

    # --- Method ---
    n_trig = sum(1 for c in cands if c["area"] == "triggering")
    n_fresh = sum(1 for c in cands if c["area"] == "freshness")
    L.append("## Method\n")
    L.append(f"- **{len(cands)} candidates** — {n_trig} triggering, {n_fresh} freshness "
             "— screened with `evals/screen.py` at **reps=5** (per AI-438 multi-rep "
             "policy) on each model below.")
    L.append("- **Arms.** *baseline* = bare model, no plugin; *skills* = the flox plugin "
             "loaded via `--plugin-dir`. A candidate is a **discriminator** when the "
             "skills arm passes (hard-check majority OR judge-correct) while the "
             "baseline fails the same measure; **skill-gap** when both arms fail "
             "(flagged, not discarded); **no-signal** when the baseline already passes.")
    L.append("- **Baseline isolation (harness fix).** On this host the flox plugin is "
             "enabled globally in `~/.claude/settings.json`, so the plain baseline arm "
             "loaded it and stopped being a bare model (it answered *\"Based on the Flox "
             "guide\"* and knew post-cutoff commands). `screen.py`/`run.py` now pass "
             "`--setting-sources project,local` to every arm, dropping user-level "
             "`enabledPlugins` while keeping OAuth; the skills arm re-adds exactly one "
             "plugin. Without this the whole screen collapses to no-signal.")
    L.append("- **Freshness axis = post-training-cutoff Flox behavior** (model cutoff "
             "~Jan 2026). Scanned the local `flox`, `floxdocs`, `floxhandbook` checkouts "
             "and the installed CLI (v1.13.2).\n")

    # --- Per-model summary ---
    L.append("## Per-model summary\n")
    L.append("| model | reps | discriminators | skill-gaps | no-signal | errors | "
             "base hard-pass | skills hard-pass | mean judge gap | cost |")
    L.append("|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|")
    for m in order:
        s = models[m]["summary"]
        L.append(f"| **{m}** (`{s['model']}`) | {s['reps']} | {s['discriminators']} | "
                 f"{s['skill_gaps']} | {s['no_signals']} | {s['errors']} | "
                 f"{s['mean_baseline_hard_pass_rate']} | {s['mean_skills_hard_pass_rate']} | "
                 f"{s['mean_judge_gap']} | ${s['total_cost_usd']} |")
    L.append("")

    # --- Ranked discriminators ---
    L.append("## Strongest discriminators (ranked)\n")
    L.append("Ranked by number of models on which the candidate discriminates, then by "
             "mean judge gap. `DISC` = discriminator, `GAP` = skill-gap, `—` = no-signal. "
             "Cells show `base_hp→skills_hp` (hard-pass count / scorable reps) and the "
             "judge gap.\n")
    hdr = "| rank | candidate | area | " + " | ".join(order) + " | disc models | mean gap |"
    L.append(hdr)
    L.append("|--:|---|---|" + "---|" * len(order) + "--:|--:|")
    for i, x in enumerate(ranked, 1):
        cells = []
        for m in order:
            r = x["per"].get(m)
            if not r:
                cells.append("n/a")
                continue
            b, s = r["baseline"], r["skills"]
            cells.append(f"{CLS_ABBR[r['classification']]} {hp(b)}→{hp(s)} g{r['judge_gap']:+}")
        L.append(f"| {i} | `{x['id']}` | {x['area']}{'/trig' if x['trig'] else ''} | "
                 + " | ".join(cells) + f" | {x['disc_models']}/{len(order)} | {x['mean_gap']:+} |")
    L.append("")

    # --- Buckets ---
    strong = [x for x in ranked if x["disc_models"] == len(order) and len(order) > 0]
    partial = [x for x in ranked if 0 < x["disc_models"] < len(order)]
    gaps = [x for x in ranked if x["disc_models"] == 0
            and any(r and r["classification"] == "skill-gap" for r in x["per"].values())]
    nosig = [x for x in ranked if x["disc_models"] == 0
             and all((not r) or r["classification"] == "no-signal" for r in x["per"].values())]

    L.append("## Buckets\n")
    L.append(f"- **Discriminates on every model ({len(strong)}):** "
             + (", ".join(f"`{x['id']}`" for x in strong) or "none"))
    L.append(f"- **Discriminates on some models ({len(partial)}):** "
             + (", ".join(f"`{x['id']}` ({x['disc_models']}/{len(order)})" for x in partial) or "none"))
    L.append(f"- **Skill-gap on all models — skill may be missing coverage ({len(gaps)}):** "
             + (", ".join(f"`{x['id']}`" for x in gaps) or "none"))
    L.append(f"- **No-signal — baseline already passes ({len(nosig)}):** "
             + (", ".join(f"`{x['id']}`" for x in nosig) or "none"))
    L.append("")

    # --- Freshness note ---
    L.append("## Freshness was thin — weighted toward triggering\n")
    L.append("The freshness scan found only a **small in-skill post-cutoff surface**: "
             "`flox run -p` (landed 2026-06-25, v1.13.x), native auto-activation "
             "(`auto_activate` config + `flox activate allow|deny`, v1.12.0, 2026-04-30), "
             "and the `nix-builds.toml`→`.flox/pkgs/` retirement (docs removed "
             "2026-07-16). `options.activate.mode` predates the cutoff (v1.3.17) so it is "
             "**not** fresh. Genuinely newer changes — FloxHub token now in the OS "
             "keyring, non-interactive `flox auth login --token-file`, service "
             "`depends_on` / shutdown signal+timeout — are **not yet in the skill** (and "
             "some postdate installed v1.13.2), so they can only screen as skill-gaps; "
             "they are noted as a **future skill-freshness backlog**, not screened here. "
             "Per AI-439's own guidance the candidate mix is therefore weighted toward "
             f"triggering ({n_trig}) over freshness ({n_fresh}).\n")

    L.append("## Provenance / reproduce\n")
    L.append("```bash\n"
             "cd evals\n"
             "for m in claude-haiku-4-5-20251001 claude-sonnet-5 claude-opus-4-8; do\n"
             "  flox activate -- python3 screen.py --candidates candidates.jsonl --reps 5 --concurrency 4 \\\n"
             "    --model \"$m\" --out results/screen-${m%%-*}.json   # isolated by default\n"
             "done\n"
             "flox activate -- python3 gen_screening_report.py --results results/screen-*.json \\\n"
             "    --candidates candidates.jsonl --out SCREENING-REPORT.md\n"
             "```\n")

    Path(args.out).write_text("\n".join(L) + "\n")
    print(f"wrote {args.out}  ({len(cands)} candidates, models: {', '.join(order) or 'NONE YET'})")


if __name__ == "__main__":
    main()
