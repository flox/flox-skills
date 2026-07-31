#!/usr/bin/env python3
"""Render the AI-435/AI-439 discrimination-screening report from screen.py output.

Reads one screen-<model>.json per screened model and emits a single markdown
report: per-model summary, a merged per-candidate table (per-model
classification + judge gap + hard-pass spread), and a ranked list of the
strongest discriminators (those that separate baseline from skills on the most
models). Pure stdlib; safe to re-run as models finish (a `--results` file that
is not there yet is named on stderr and skipped).

ONLY MEASURED CANDIDATES ARE REPORTED. `--candidates` is the registry the run
was drawn from, not the set that was screened: the report covers exactly the
ids present in `--results`, and registry entries with no measurement are listed
as unscreened rather than bucketed. Without that intersection a candidate that
was never run satisfies the no-signal predicate vacuously and is published as
"baseline already passes" — a measurement nobody took. A run in which NO
`--results` file exists is an error, not an empty report.

Usage (regenerating the committed report from the committed measurements):
  python3 gen_screening_report.py \
      --results baselines/screen-haiku.json baselines/screen-sonnet.json baselines/screen-opus.json \
      --candidates tasks/screening.jsonl \
      --out reports/SCREENING-REPORT.md

A fresh screen writes to results/ (gitignored), so pass those paths instead
when reporting on a run you just made.
"""
import argparse
import json
import sys
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
    """Return ({model_label: {"summary":..., "byid":..., "path":...}}, [missing paths]).

    A path that does not exist is returned as missing rather than dropped on
    the floor: re-running while models are still finishing is supported, but
    the caller has to be able to tell "not done yet" from "nothing was ever
    measured", and a silent skip made those two identical.
    """
    out = {}
    missing = []
    for p in paths:
        fp = Path(p)
        if not fp.exists():
            missing.append(str(p))
            continue
        data = json.loads(fp.read_text())
        summ = data["summary"]
        label = LABELS.get(summ.get("model", ""), summ.get("model", fp.stem))
        out[label] = {
            "summary": summ,
            "byid": {r["id"]: r for r in data["results"]},
            "path": str(p),
        }
    return out, missing


def hp(arm):
    c = arm.get("hard_pass_count")
    n = arm.get("ok_reps")
    if c is None or not n:
        return "?"
    return f"{c}/{n}"


def main():
    ap = argparse.ArgumentParser(
        description="Render the screening report from screen.py measurements. "
                    "Only candidates present in --results are reported."
    )
    ap.add_argument(
        "--results", nargs="+", required=True,
        help="screen-<model>.json measurement files. These define WHICH candidates "
             "the report covers. baselines/screen-*.json are the committed run; a "
             "screen you just made writes to results/ (gitignored).",
    )
    ap.add_argument(
        "--candidates", default=str(HERE / "tasks" / "screening.jsonl"),
        help="the registry the run was drawn from, used for each measured "
             "candidate's area/trigger metadata and to name the entries that were "
             "NOT screened (default: tasks/screening.jsonl). It does not widen the "
             "report: an entry with no measurement is never bucketed.",
    )
    ap.add_argument("--out", default=str(HERE / "reports" / "SCREENING-REPORT.md"))
    args = ap.parse_args()

    models, missing = load(args.results)  # ordered by insertion of existing files
    for p in missing:
        print(f"note: no results file at {p} — skipping", file=sys.stderr)
    if not models:
        print(
            "error: none of the --results paths exist "
            f"({', '.join(str(p) for p in args.results)}), so nothing was measured "
            "and there is no report to write. A report generated from zero "
            "measurements would file every candidate under \"baseline already "
            "passes\" without a single run behind it. The committed measurements "
            "are baselines/screen-{haiku,sonnet,opus}.json; a screen you just ran "
            "writes to results/ (gitignored).",
            file=sys.stderr,
        )
        sys.exit(2)

    order = [m for m in ("haiku", "sonnet", "opus") if m in models] or list(models)

    registry = [json.loads(l) for l in Path(args.candidates).read_text().splitlines() if l.strip()]
    cand_by_id = {c["id"]: c for c in registry}

    # THE intersection: report only what was measured. A registry entry with no
    # result on any model is not evidence of anything, so it never becomes a row
    # and never lands in a bucket — it is named under "Not screened" instead.
    measured_ids = {cid for m in order for cid in models[m]["byid"]}
    ids = [c["id"] for c in registry if c["id"] in measured_ids]
    cands = [cand_by_id[cid] for cid in ids]
    unscreened = [c for c in registry if c["id"] not in measured_ids]

    # A result whose id is in no registry the report was pointed at cannot be
    # rendered (no area, no trigger flag), and silently dropping it would
    # understate the run. Say so.
    orphans = sorted(measured_ids - set(cand_by_id))
    if orphans:
        print(
            f"warning: {len(orphans)} measured id(s) are absent from "
            f"{args.candidates} and are omitted from the report: "
            + ", ".join(orphans),
            file=sys.stderr,
        )

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
             "`evals/flox/tasks/screening.jsonl` + this report only.\n")

    # --- Method ---
    n_trig = sum(1 for c in cands if c["area"] == "triggering")
    n_fresh = sum(1 for c in cands if c["area"] == "freshness")
    L.append("## Method\n")
    L.append(f"- **{len(cands)} candidates screened** — {n_trig} triggering, {n_fresh} "
             f"freshness — of the {len(registry)} entries in "
             f"`{Path(args.candidates).name}`, with `evals/flox/screen.py` at **reps=5** "
             "(per AI-438 multi-rep policy) on each model below. Every number in this "
             "report comes from a measurement; the "
             f"{len(unscreened)} registry entr{'y' if len(unscreened) == 1 else 'ies'} "
             "with no result "
             f"{'is' if len(unscreened) == 1 else 'are'} listed under *Not screened* and "
             "counted nowhere else.")
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
    # `any(...)`: at least one model must actually have measured this candidate.
    # Rows are already restricted to measured ids, so this cannot fire — it is
    # here so the predicate is right on its own terms rather than only because
    # of what the caller filtered. `not r` means "this model did not measure it",
    # which is never a no-signal observation.
    nosig = [x for x in ranked if x["disc_models"] == 0
             and any(r for r in x["per"].values())
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
    L.append(f"- **Not screened — no measurement in this run ({len(unscreened)}):** "
             + (", ".join(f"`{c['id']}`" for c in unscreened) or "none")
             + ("" if not unscreened else
                ". These are registry entries the run did not cover. They are **not** "
                "no-signal: nothing was measured, so nothing is claimed."))
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

    # --- Provenance (generated, not hand-maintained) ---
    #
    # Every claim in this section is derived from the run: which registry, how
    # much of it was covered, which measurement files were read, and which
    # `--area` selection reproduces the screened set. It used to be prose
    # somebody typed into the .md, which meant regenerating the report deleted
    # the only record that the screened set was a subset of the registry.
    # Render every path relative to evals/flox when it lives there, so the
    # recipe is the command a reader can paste from that directory rather than
    # whatever absolute path this invocation happened to use.
    def rel(p):
        try:
            return str(Path(p).resolve().relative_to(HERE))
        except ValueError:
            return str(p)

    cand_rel = rel(args.candidates)
    areas = sorted({c["area"] for c in cands})
    area_flags = " ".join(f"--area {a}" for a in areas)
    # Entries the area selection would pick up today but that carry no
    # measurement here — the exact delta between "what was screened" and "what
    # re-running the recipe screens".
    in_area_unscreened = [c["id"] for c in unscreened if c["area"] in areas]
    result_paths = [rel(models[m]["path"]) for m in order]

    L.append("## Provenance / reproduce\n")
    L.append(f"Screened **{len(cands)} of the {len(registry)}** entries in "
             f"`{cand_rel}`, across "
             f"{'area' if len(areas) == 1 else 'areas'} "
             + ", ".join(f"`{a}`" for a in areas)
             + ". Measurements read from "
             + ", ".join(f"`{p}`" for p in result_paths) + ".")
    if len(cands) < len(registry):
        L.append(f"\nThe registry is the full candidate set, not the screened set — "
                 f"{len(unscreened)} entr{'y' if len(unscreened) == 1 else 'ies'} "
                 f"{'was' if len(unscreened) == 1 else 'were'} not measured in this run "
                 "and appear only under *Not screened* above. Selecting the areas above "
                 f"today yields {len(cands) + len(in_area_unscreened)} entries"
                 + (f" — {len(in_area_unscreened)} more than "
                    f"{'was' if len(cands) == 1 else 'were'} screened here: "
                    + ", ".join(f"`{i}`" for i in in_area_unscreened)
                    + ", present in the registry with no measurement in this run."
                    if in_area_unscreened else ", exactly the set screened here."))
    L.append("\n```bash\n"
             "flox activate\n"
             "cd evals/flox\n"
             "for m in " + " ".join(models[m]["summary"]["model"] for m in order) + "; do\n"
             f"  python3 screen.py {area_flags} --reps 5 --concurrency 4 \\\n"
             "    --model \"$m\" --out results/screen-${m%%-*}.json   # isolated by default\n"
             "done\n"
             "# Regenerate THIS report from the committed measurements:\n"
             "python3 gen_screening_report.py \\\n"
             f"    --results {' '.join(result_paths)} \\\n"
             f"    --candidates {cand_rel} --out reports/SCREENING-REPORT.md\n"
             "# ...or from the screen you just ran: --results results/screen-*.json\n"
             "```\n")

    Path(args.out).write_text("\n".join(L) + "\n")
    print(f"wrote {args.out}  ({len(cands)} of {len(registry)} registry candidates "
          f"screened, {len(unscreened)} unscreened, models: {', '.join(order)})")


if __name__ == "__main__":
    main()
