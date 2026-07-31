# AI-435 / AI-439 — Triggering + Freshness Discrimination Screening

Research pass for AI-439 (child of AI-435). **Nothing here is promoted into `tasks.jsonl`** — promotion and the eval-model-policy choice are Bill decisions (AI-439 is blocked on the model policy). This artifact is `evals/candidates.jsonl` + this report only.

## Method

- **19 candidates** — 13 triggering, 6 freshness — screened with `evals/screen.py` at **reps=5** (per AI-438 multi-rep policy) on each model below.
- **Arms.** *baseline* = bare model, no plugin; *skills* = the flox plugin loaded via `--plugin-dir`. A candidate is a **discriminator** when the skills arm passes (hard-check majority OR judge-correct) while the baseline fails the same measure; **skill-gap** when both arms fail (flagged, not discarded); **no-signal** when the baseline already passes.
- **Baseline isolation (harness fix).** On this host the flox plugin is enabled globally in `~/.claude/settings.json`, so the plain baseline arm loaded it and stopped being a bare model (it answered *"Based on the Flox guide"* and knew post-cutoff commands). `screen.py`/`run.py` now pass `--setting-sources project,local` to every arm, dropping user-level `enabledPlugins` while keeping OAuth; the skills arm re-adds exactly one plugin. Without this the whole screen collapses to no-signal.
- **Freshness axis = post-training-cutoff Flox behavior** (model cutoff ~Jan 2026). Scanned the local `flox`, `floxdocs`, `floxhandbook` checkouts and the installed CLI (v1.13.2).

## Per-model summary

| model | reps | discriminators | skill-gaps | no-signal | errors | base hard-pass | skills hard-pass | mean judge gap | cost |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| **haiku** (`claude-haiku-4-5-20251001`) | 5 | 9 | 1 | 9 | 0 | 0.568 | 0.947 | 1.17 | $8.0662 |
| **sonnet** (`claude-sonnet-5`) | 5 | 5 | 1 | 13 | 0 | 0.758 | 0.916 | 0.54 | $48.3998 |
| **opus** (`claude-opus-4-8`) | 5 | 5 | 0 | 14 | 0 | 0.832 | 0.968 | 0.84 | $58.9733 |

## Strongest discriminators (ranked)

Ranked by number of models on which the candidate discriminates, then by mean judge gap. `DISC` = discriminator, `GAP` = skill-gap, `—` = no-signal. Cells show `base_hp→skills_hp` (hard-pass count / scorable reps) and the judge gap.

| rank | candidate | area | haiku | sonnet | opus | disc models | mean gap |
|--:|---|---|---|---|---|--:|--:|
| 1 | `fresh-run-oneoff` | freshness | DISC 0/5→5/5 g+3.6 | DISC 2/5→5/5 g+2.0 | DISC 2/5→5/5 g+2.4 | 3/3 | +2.67 |
| 2 | `fresh-auto-activate-control` | freshness | DISC 0/5→4/5 g+2.4 | DISC 0/5→5/5 g+2.8 | DISC 0/5→5/5 g+1.8 | 3/3 | +2.33 |
| 3 | `fresh-auto-activate-cd` | freshness | — 3/5→4/5 g+1.2 | DISC 4/5→5/5 g+2.6 | DISC 5/5→5/5 g+2.0 | 2/3 | +1.93 |
| 4 | `fresh-nix-build-location` | freshness | DISC 1/5→5/5 g+2.8 | DISC 1/5→5/5 g+1.2 | — 5/5→5/5 g+0.2 | 2/3 | +1.4 |
| 5 | `fresh-run-no-version` | freshness | — 4/5→5/5 g+0.4 | DISC 0/5→2/5 g+1.8 | — 4/5→2/5 g+2.4 | 1/3 | +1.53 |
| 6 | `fresh-run-npx-equivalent` | freshness/trig | GAP 0/5→2/5 g+0.6 | GAP 0/5→0/5 g+0.0 | DISC 0/5→5/5 g+3.8 | 1/3 | +1.47 |
| 7 | `trig-gpu-pytorch` | triggering/trig | — 4/5→5/5 g+1.0 | — 5/5→5/5 g-0.2 | DISC 3/5→5/5 g+2.4 | 1/3 | +1.07 |
| 8 | `trig-image-no-dockerfile` | triggering/trig | DISC 2/5→5/5 g+3.2 | — 5/5→5/5 g+0.0 | — 5/5→5/5 g+0.0 | 1/3 | +1.07 |
| 9 | `trig-go-node-monorepo` | triggering/trig | DISC 2/5→5/5 g+1.0 | — 5/5→5/5 g+0.0 | — 5/5→5/5 g+0.2 | 1/3 | +0.4 |
| 10 | `trig-one-command-onboarding` | triggering/trig | DISC 4/5→5/5 g+1.0 | — 5/5→5/5 g+0.2 | — 5/5→5/5 g+0.0 | 1/3 | +0.4 |
| 11 | `trig-new-python-project` | triggering/trig | DISC 4/5→5/5 g+0.6 | — 5/5→5/5 g+0.4 | — 5/5→5/5 g-0.2 | 1/3 | +0.27 |
| 12 | `trig-share-exact-toolchain` | triggering/trig | DISC 5/5→5/5 g+1.0 | — 5/5→5/5 g-0.2 | — 5/5→5/5 g+0.0 | 1/3 | +0.27 |
| 13 | `trig-node-version-pin` | triggering/trig | DISC 1/5→5/5 g+0.4 | — 5/5→5/5 g-1.0 | — 5/5→5/5 g-0.6 | 1/3 | -0.4 |
| 14 | `trig-distribute-cli` | triggering/trig | — 4/5→5/5 g+0.2 | — 5/5→5/5 g+1.2 | — 5/5→5/5 g+1.0 | 0/3 | +0.8 |
| 15 | `trig-project-postgres` | triggering/trig | — 3/5→5/5 g+0.8 | — 5/5→5/5 g+0.6 | — 5/5→5/5 g+0.8 | 0/3 | +0.73 |
| 16 | `trig-redis-cache` | triggering/trig | — 5/5→5/5 g+0.6 | — 5/5→5/5 g+0.0 | — 5/5→5/5 g+0.2 | 0/3 | +0.27 |
| 17 | `trig-ci-laptop-parity` | triggering/trig | — 3/5→5/5 g+0.2 | — 5/5→5/5 g+0.0 | — 5/5→5/5 g-0.2 | 0/3 | +0.0 |
| 18 | `trig-project-local-clis` | triggering/trig | — 5/5→5/5 g+0.8 | — 5/5→5/5 g-0.8 | — 5/5→5/5 g+0.0 | 0/3 | +0.0 |
| 19 | `trig-newer-than-system` | triggering/trig | — 4/5→5/5 g+0.4 | — 5/5→5/5 g-0.4 | — 5/5→5/5 g-0.2 | 0/3 | -0.07 |

## Buckets

- **Discriminates on every model (2):** `fresh-run-oneoff`, `fresh-auto-activate-control`
- **Discriminates on some models (11):** `fresh-auto-activate-cd` (2/3), `fresh-nix-build-location` (2/3), `fresh-run-no-version` (1/3), `fresh-run-npx-equivalent` (1/3), `trig-gpu-pytorch` (1/3), `trig-image-no-dockerfile` (1/3), `trig-go-node-monorepo` (1/3), `trig-one-command-onboarding` (1/3), `trig-new-python-project` (1/3), `trig-share-exact-toolchain` (1/3), `trig-node-version-pin` (1/3)
- **Skill-gap on all models — skill may be missing coverage (0):** none
- **No-signal — baseline already passes (6):** `trig-distribute-cli`, `trig-project-postgres`, `trig-redis-cache`, `trig-ci-laptop-parity`, `trig-project-local-clis`, `trig-newer-than-system`

## Freshness was thin — weighted toward triggering

The freshness scan found only a **small in-skill post-cutoff surface**: `flox run -p` (landed 2026-06-25, v1.13.x), native auto-activation (`auto_activate` config + `flox activate allow|deny`, v1.12.0, 2026-04-30), and the `nix-builds.toml`→`.flox/pkgs/` retirement (docs removed 2026-07-16). `options.activate.mode` predates the cutoff (v1.3.17) so it is **not** fresh. Genuinely newer changes — FloxHub token now in the OS keyring, non-interactive `flox auth login --token-file`, service `depends_on` / shutdown signal+timeout — are **not yet in the skill** (and some postdate installed v1.13.2), so they can only screen as skill-gaps; they are noted as a **future skill-freshness backlog**, not screened here. Per AI-439's own guidance the candidate mix is therefore weighted toward triggering (13) over freshness (6).

## Provenance / reproduce

```bash
cd evals
for m in claude-haiku-4-5-20251001 claude-sonnet-5 claude-opus-4-8; do
  python3 screen.py --candidates candidates.jsonl --reps 5 --concurrency 4 \
    --model "$m" --out results/screen-${m%%-*}.json   # isolated by default
done
python3 gen_screening_report.py --results results/screen-*.json \
    --candidates candidates.jsonl --out SCREENING-REPORT.md
```

