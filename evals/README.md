# Flox skills evals

A standalone eval suite for the Flox skill: tasks scored for correctness and
quality, used to catch regressions and to measure whether the skill triggers on
the right requests and supplies the right context. (It also gated the
consolidation work — each merge step had to hold or beat the recorded baseline —
but the suite stands on its own beyond that.)

Tasks are tiered by triggering expectation: **should** (must trigger + be
correct; these bind the gate), **may** (nice if it triggers), and **stretch**
(we'd like it to, but it's fine if not — these never fail the gate). Many
prompts deliberately never mention "flox", to test that the skill fires when a
user just wants "a new project" / "add nodejs 18.4" / "a PyTorch GPU setup".

## Policy: every skill change ships with an eval — written RED first

**Every PR that adds, changes, or *fixes* guidance in a skill MUST add an eval
that verifies the guidance is actually followed.** Reviewers should not approve
a skill PR without one. Two rules:

1. **RED first.** Write the eval before the fix, run it, and watch it **fail for
   the reason you claim**. A test written after the change only proves the change
   is self-consistent. This is not ceremony — see the worked example below.
2. **Prefer the cheap tier.** A Tier 1 fixture or prompt eval is the inner loop
   (seconds); the Tier 2 OSS runs are *confirmation* (a clone + a full agentic
   pass + realization). Reserve the expensive tier for proving the fix holds on
   real repos and for non-regression.

**Fixes to existing guidance are in scope**, not just new features. AI-449 is
why: the guidance that reasoned best is the guidance that broke real repos.

### Worked example: why RED first is not ceremony (AI-449)

The `script-started-postgres` fixture exists because of this policy, and the
first version of it **passed** — disproving the hypothesis it was written to
confirm.

| fixture version | launcher | result |
|---|---|---|
| v1 | bare `docker run postgres:16-alpine` | **passed** — skill wired the service correctly |
| v2 | cluster under `./target`, socket in the repo tree, percent-encoded `DATABASE_URL`, `db/schema.sql` load | **failed** — skill deferred |

Only one variable changed. The bug was never "a launcher script exists" (the
sentence we were about to rewrite) — it was **launcher *intricacy***: the skill
read the script, judged it couldn't faithfully replicate the fiddly parts, and
handed the datastore back. Had v1 failed as expected, the fix would have targeted
the wrong sentence and the real trigger would have survived it on three repos,
invisibly.

**A cheap eval that refuses to fail is a finding, not an obstacle.** If a fixture
won't reproduce the bug, say so in the PR and use the expensive tier as the test
rather than contriving a fixture that fails for a manufactured reason — a green
suite over a broken skill is the failure mode this whole policy exists to prevent.

Why: the investigation behind AI-435 showed that modern Claude already knows
Flox, so most guidance shows no measurable lift — *except* for features the
model can't already know (post-training-cutoff CLI, Flox-specific idioms). A
new feature is therefore the one place an eval genuinely discriminates, and the
task doubles as a conformance check: does the model, with the skill, produce the
idiom the skill teaches?

How:
- Write a prompt a user would ask that should invoke the new guidance (e.g.
  "the catalog only has X 2.12.1 but I need 2.12.2 — how, through Flox?").
- Add a deterministic `must_match` for the Flox-specific idiom the skill teaches
  (e.g. `\.flox/pkgs` + `overrideAttrs`), plus a judge rubric. Prefer asserting
  the *correct* construction (positive `must_match`) over detecting the wrong one
  (`must_not_match`) — good answers often show the anti-pattern as a labeled
  counter-example, which false-fires a negative check.
- Screen it baseline-vs-skills to confirm the skill arm follows the
  guidance; promote it into `tasks.jsonl` once it holds. Note: the
  `screen.py` screening harness has not landed on main (AI-438 tracks
  it, along with the multi-rep policy); until it does, this step
  requires the branch tooling.

## What it does

For every task in `tasks.jsonl`, runs `claude` headless with the Flox plugin
loaded and scores the answer two ways:

- **Hard checks** — deterministic regex checks (no hallucinated install URL, no
  absolute paths in manifests, required manifest sections present, correct
  commands used).
- **LLM judge** — a separate `claude` call grades the answer 1–5 against the
  task's rubric and returns a pass/fail.

### Trigger tests

Tasks with `"trigger_test": true` check **implicit triggering**: the prompt never
says "flox" (e.g. "create a new Node.js project"), and a neutral instruction is
used so nothing biases the model toward Flox. The `invokes_flox` hard-check then
verifies the skill fired anyway and produced Flox guidance. These guard the
behavior the retired MCP server used to encourage.

The harness runs a single arm today (`skills`, `--strict-mcp-config`, MCP
off); an MCP-assisted arm was measured and retired — see the AI-93 finding
under Baselines below.

## Run

```bash
python3 run.py --mode skills            # skills-only baseline
python3 run.py --mode skills --only node-env   # single task
python3 run.py --mode skills --gate     # exit non-zero if binding gates fail (CI)
```

Results land in `results/<mode>.json` with a summary (hard-pass rate, avg judge
score, correct rate). Pure stdlib — no node/uv required.

## Authentication

The harness shells out to `claude`, which needs credentials:

- **Locally** it uses whatever the `claude` CLI is logged in with (an OAuth
  token in `~/.claude/.credentials.json`, e.g. a Claude subscription) — usage
  counts against that account.
- **In CI** there is no local login, so the workflow
  (`.github/workflows/evals.yml`) exports the org-managed
  `MANAGED_SKILLS_ANTHROPIC_API_KEY` secret as `ANTHROPIC_API_KEY`; `claude` uses
  it for both the agent and judge calls. The job runs `run.py --mode skills
  --gate`, which fails the build only if a functional `should`-tier task fails
  its deterministic hard-checks (judge score and triggering are reported, not
  gated — see Gate policy below).

## Baselines

Recorded on the original 7-skill layout (pre-consolidation):

| Arm | Hard-check pass | Avg judge score | Correct rate | File |
|-----|-----------------|-----------------|--------------|------|
| skills-only | 8/8 (100%) | 4.62 / 5 | 8/8 | `results/skills.json` |

**AI-93 finding:** an MCP-assisted arm (skills + the flox-mcp server) was
measured alongside skills-only and scored 8/8 (100%) hard-pass, 4.25/5 avg
judge, 8/8 correct — within LLM-judge run-to-run noise of skills-only. No
measurable context gap from removing the MCP, so the arm was retired and
the harness now runs skills-only.

## Gate policy

Both the LLM judge (integer 1–5, plus a binary "correct") and implicit
triggering are **probabilistic run-to-run** — a single task can flip
correct↔incorrect or trigger↔not-trigger between identical runs. Binding CI on
those would make it flaky (15 functional tasks at ~95% judge reliability ≈ 46%
chance of an all-green run). So `--gate` is split:

- **Binding (deterministic):** every **functional `should`-tier** task must pass
  its **hard-checks** (no hallucinated Flox install, required manifest sections,
  correct commands, etc.). These are regex-deterministic and stable; a failure
  blocks the build.
- **Advisory (reported, never blocks):** `avg_judge_score`, `judge_correct_rate`,
  per-tier breakdown, and `should_trigger_rate`. These are tracked as quality/
  triggering trends (watch for a sustained drop), not pass/fail gates. `may` and
  `stretch` tasks are advisory by definition.
