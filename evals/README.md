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
  guidance; promote it into `tasks.jsonl` once it holds. See the
  Screening section below for `screen.py`, the rep policy, and
  check-design rules.

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

## The runtime: run everything through `flox activate`

**Every command in this file and in [`floxify/README.md`](floxify/README.md)
runs through `flox activate --`, and that is the supported way to run them.**
The interpreter is declared in this repo's own environment
(`.flox/env/manifest.toml` → `python311`) and pinned by `manifest.lock` on all
four systems, so a clean checkout needs flox and no ambient Python. CI runs the
identical commands — `.github/workflows/evals.yml` has no `actions/setup-python`
— which is the point: a suite that passes locally and fails in CI because two
machines shipped different `python3` is not a signal about the skill.

```bash
flox activate -- python3 run.py --mode skills   # from evals/
```

`flox activate` finds the environment by searching upward, so it works from
`evals/`, `evals/floxify/`, or the repo root; only the script path changes.
Activation also supplies `claude` (the manifest installs `flox/claude-code`),
so the harnesses' agent and judge calls use a pinned CLI rather than whatever
happens to be on PATH.

3.11 is a floor, not a preference: the harnesses parse manifests with stdlib
`tomllib`.

## Run

```bash
flox activate -- python3 run.py --mode skills            # skills-only baseline
flox activate -- python3 run.py --mode skills --only node-env   # single task
flox activate -- python3 run.py --mode skills --gate     # exit non-zero if binding gates fail (CI)
```

Results land in `results/<mode>.json` with a summary (hard-pass rate, avg judge
score, correct rate). Pure stdlib — no node/uv required.

## Authentication

The harness shells out to `claude`, which needs credentials. The *binary* comes
from the Flox environment (see the runtime note above); the *credentials* are
unchanged by that and still come from wherever they always did:

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
  correct commands, etc.). These are deterministic and stable; a failure blocks
  the build.

  Checks that assert something about a *manifest* parse it with `tomllib` and
  inspect the resulting dict, rather than grepping the answer — a fenced block
  is extracted with `skill_toml_lint.extract_blocks` and every fact about it is
  asserted against that same block. This is not stylistic. A whole-answer grep
  certifies manifests it never inspected: an answer whose prose says
  `schema-version = "1.12.0"` while its only manifest keeps `version = 1`
  passes a text search and hands the user a file flox refuses to load, which is
  exactly the RED such a task exists to catch. Ad-hoc line scanners have the
  same problem in miniature — without `'''`/`"""` state, a key inside a command
  body reads as a real key, and a `[ -d dir ] || cmd` line reads as a table
  header.
- **Advisory (reported, never blocks):** `avg_judge_score`, `judge_correct_rate`,
  per-tier breakdown, and `should_trigger_rate`. These are tracked as quality/
  triggering trends (watch for a sustained drop), not pass/fail gates. `may` and
  `stretch` tasks are advisory by definition.

## Skill TOML snippet guard (`skill_toml_lint.py`)

The eval suites above check manifests the model **generates**. This one checks
the manifests the skill **ships**: every fenced ` ```toml ` block in
`flox-plugin/skills/flox/SKILL.md` and `references/*.md` is fed to
`flox edit -f` inside a throwaway `flox init` environment, so a snippet a user
would copy-paste is proven to parse.

It exists because AI-494's predecessor (1a8119c) found four classes of snippet
that failed `flox edit` outright — `is-daemon` with no shutdown command, an
invented `[include]` version field, bare `systems` under `[install]`, a
`[nodejs]` table. Running the guard over the whole skill found **five more** the
manual pass had missed (three CUDA `[hook]` blocks holding bare shell lines
instead of `on-activate = '''...'''`, a `[profile.common]` shell table, and a
multi-line inline `labels` table). Reading for parse errors does not scale;
`flox edit` never misses one.

### Two tiers

| tier | what it requires | binds CI? |
|---|---|---|
| `structural` (default) | flox **parses** the snippet — only `Failed to parse manifest` fails | **yes** |
| `catalog` (`--tier catalog`) | ...and every package resolves against the live catalog (`flox edit` exits 0) | no, advisory |

flox parses the whole manifest before resolving anything, so the structural
tier catches every bug above **without the catalog** — deterministic, ~25ms per
snippet, and offline-safe. `--offline` points the proxy vars at a closed port so
a networked runner behaves exactly like an air-gapped one. The catalog tier is
report-only by design: it fails for reasons that have nothing to do with the
skill (catalog outage, a package legitimately renamed).

### Snippets that declare `schema-version`

The guard prepends `version = 1` to any block that doesn't declare a schema
itself. A block exercising a field a **later** schema added —
`services.auto-start`, which needs `schema-version = "1.12.0"` — must declare that key
instead, and the two spellings are mutually exclusive in flox (a manifest
carrying both is rejected with ``unknown field `schema-version` ``). So a
top-level `schema-version` suppresses the prepend exactly like `version = 1`
does. AI-503 found this the hard way: the first correct auto-start snippet in
`services.md` failed the guard for a reason that had nothing to do with the
snippet.

### Opting a block out

A block that is deliberately partial — package descriptors with no `[install]`
header, metadata fields meant to be merged into a `[build.<name>]` — cannot
parse standalone. Mark it **explicitly**; the guard never guesses.

Preferred, a standalone comment line inside the block (keeps ` ```toml `
highlighting, and forces you to write down *why*):

````markdown
```toml
# eval: skip fragment - metadata fields only, merge into a [build.<name>]
[build.mytool]
version.command = "git describe --tags"
```
````

Or, for a block that isn't a flox manifest at all, the fence info string:

````markdown
```toml-fragment
[tool.poetry]
```
````

The reason text is mandatory in practice — `test_skill_toml_lint.py` fails any
block marked without one. **Never add a marker to silence a real parse error:**
fix the snippet. `KNOWN_PARSE_FAILURES` in the script is the escape hatch for a
genuine defect too large to fix in the same PR (same discipline as
`floxify/test_golden_lint.py`'s `KNOWN_VIOLATIONS`); it is currently **empty**
and meant to stay that way. Entries are keyed by content hash, so a stale one —
left behind after its snippet was fixed — is itself a failure and can't sit
there absorbing a future regression.

### Run

```bash
flox activate -- python3 skill_toml_lint.py                     # structural tier (what CI gates on)
flox activate -- python3 skill_toml_lint.py --offline           # ...and prove it needs no network
flox activate -- python3 skill_toml_lint.py --tier catalog      # + live catalog resolution (advisory)
flox activate -- python3 skill_toml_lint.py --only services.md  # one document
flox activate -- python3 skill_toml_lint.py --list              # extract only, no catalog
flox activate -- python3 skill_toml_lint.py -v                  # print every block, not just failures
flox activate -- python3 -m unittest test_skill_toml_lint       # the guard's own tests (no catalog)
```

The outer `flox activate` only supplies the interpreter; the `flox init` /
`flox edit -f` environments the guard drives are throwaway ones in temp dirs,
unaffected by it. Two comments above used to say "no flox" — with flox now
supplying python3 they say **no catalog**, which is what they always meant:
those paths make no catalog call.

Exit 0 if every checked snippet passed its tier, 1 otherwise. Pure stdlib.

### CI

Two jobs in `.github/workflows/evals.yml`, split by what they cost:

- `test_skill_toml_lint` runs in the free per-PR unit-test step of the `evals`
  job — no catalog, no network, no API spend (flox is on PATH there now, but
  only as the interpreter). It is what makes the guard itself
  trustworthy enough to gate on (an extractor that silently drops blocks would
  report "0 failed" forever).
- `skill-toml-lint` is a separate per-PR job that installs flox and runs the
  real `--offline` structural check. Like `golden-lint`, it spawns no `claude`
  and costs zero Anthropic spend, so it needs no dispatch-only cost gate; it is
  path-filtered to PRs touching the flox skill or the guard.

## Screening (`screen.py`)

`screen.py` develops the *discriminating stretch tier*: it runs candidate prompts
(`candidates*.jsonl`) through the **baseline** arm (bare model) and the **skills**
arm (plugin loaded) and classifies each as a **discriminator** (skill lifts over
baseline), a **skill-gap** (both fail — the skill may be missing coverage), or
**no-signal** (baseline already passes — too easy). Hard-checks are data-driven
per candidate via `must_match` / `must_not_match` regex lists.

`candidates-all.jsonl` is the default and the only candidate set this harness
ships — it is a superset of the original `candidates.jsonl` (retired) with the
same false-firing checks fixed under new ids
(`trap-layer-vs-compose-fixed`, `trap-containerize-nopush-fixed`) plus the
pass2/regression batches folded in. Pass `--candidates` to screen a specific
historical batch (`candidates-pass2.jsonl`, `candidates-regression.jsonl`,
`candidates-triggering.jsonl`, `candidates-new-features.jsonl`) instead.

```bash
flox activate -- python3 screen.py --reps 5                                    # default set, n=5
flox activate -- python3 screen.py --candidates candidates-pass2.jsonl --reps 5   # one batch, n=5
flox activate -- python3 screen.py --only trap-vars-no-interpolation --reps 5
flox activate -- python3 screen.py --plugin-dir /path/to/fixed-skill/flox-plugin  # test a skill edit
```

Like `run.py`, each `claude` call's cost/usage is read from the JSON envelope
(AI-459) and rolled into `results/screen.json`'s `summary.total_cost_usd` and
each arm's `cost_usd` — screening is not free, and the multi-rep policy below
multiplies call volume by `reps`, so cost is worth watching per run.

### Multi-rep policy (required)

Single runs have a **~50% cell-level flip rate** — the baseline arm alone flipped
hard-pass on 3 of 6 cells between identical runs. A lone P/F is dominated by
sampling noise. Therefore:

- **`--reps` ≥ 5 is required** for any promote / discard / skill-gap decision.
  `screen.py` reports `hard_pass_rate` (fraction of reps passing) and mean judge;
  `hard_pass`/`judge_correct` are majority verdicts.
- Compare **pass-rates**, not single cells. A discriminator must show a rate gap
  that survives n≥5.

### Model policy

The discriminating tier screens and gates on the **same model as the functional
gate — Opus (`claude-opus-4-8`)**. Rationale: content recall does not separate
modern Claude from itself (Opus *and* Sonnet already know Flox specifics), so the
tier's value is **triggering + freshness**, which the Opus gate measures directly.
No separate weaker-model arm.

### Check-design rules (learned the hard way)

- **Prefer positive `must_match` over negative `must_not_match`.** Assert the
  *correct* construction rather than detecting the wrong one.
- **A correct answer often illustrates the anti-pattern as a labeled
  counter-example.** A proximity/negative check then false-fires on good answers
  — this sank three checks independently: `trap-vars-no-interpolation`
  (`\[vars\]…PATH`), the pre-fix `trap-hook-return-not-exit`
  (`\bexit\s+[0-9]\b` fired on a documented "Don't: `exit 0`" table cell), and
  the retired `stretch-layer-vs-compose` (`\[include\]` fired on the sentence
  explaining why `[include]` is the wrong tool). Fixed by asserting the
  positive construction only and dropping the negative check.
- **A case-insensitive `must_not_match` can match ordinary prose, not just the
  pattern it targets.** The retired `stretch-containerize-nopush` used
  `FROM\s+\w` to catch a Dockerfile `FROM` line, but `re.I` also matches the
  common English word "from" (e.g. "builds an image **from** your
  environment") — it false-fired on nearly any prose answer. There is no safe
  case-insensitive substring for an all-caps Dockerfile directive; the fix
  (`trap-containerize-nopush-fixed`) drops the negative check entirely.
- **A literal multi-word `must_match` assumes one argument order.**
  `trap-uv-venv-invocation`'s `"uv pip install --python"` required `--python`
  to immediately follow the subcommand, but real correct answers commonly
  write `uv pip install -r requirements.txt --python ...` — flag order varies
  and a fixed-order substring false-negatives on it. Loosen to
  `uv pip install\b.*--python\b` (same line, either order) rather than
  enumerating every permutation.
- **Validate a new/edited check against a real known-good answer** (the
  `answer_excerpt` fields in `results/*.json`, or a fixture copied into a unit
  test) before trusting it — the check is a pure function of the answer text,
  so this needs no model calls. `evals/test_screen.py` does this for every
  check above.

### CI-gate policy — DECIDED (Bill, 2026-07-18)

`screen.py` is not yet wired into `.github/workflows/evals.yml`; it remains
a pre-promotion tool run manually or by an agent before a candidate is added
to `tasks.jsonl` (which *is* gated, per Gate policy above). This decision
governs the future stretch tier when one is created; no agent-eval CI tier
exists or is being added now — agent evals stay out of CI, free
deterministic tests only. The choice between reps-per-task gating and
aggregate pass-rate gating (framed as Option A / Option B below) is now
decided:

- **Gate on aggregate pass-rate** (`mean_skills_hard_pass_rate`), not on any
  single candidate's `hard_pass_rate` cell — Option B's shape. A per-task
  gate at the harness's own documented n≥5 reliability floor is too
  expensive to run on every PR, and at a cheaper n=3 (Option A) it
  reintroduces the flakiness that floor exists to avoid (see Multi-rep
  policy above).
- **Report per-task results alongside the gate.** Only the aggregate blocks
  the build, but a regression on one candidate must stay visible in CI
  output (e.g. the step summary), the same way `run.py` reports `by_tier`
  breakdowns as advisory detail next to its binding gate.
- **The numeric threshold is deferred.** How far `mean_skills_hard_pass_rate`
  may drop before the gate fails is not decided here — it needs a baseline
  of real screening runs on the stretch tier to calibrate against, which
  does not exist yet. Wiring this gate is future work, once a stretch tier
  exists. It is not part of AI-483: that ticket wires three existing
  deterministic, API-less unit suites into the current free CI tier and
  does not implement this gate or add any model-calling tier.

The option analysis this decision was made from, kept as a rationale record:

- **Option A: gate at `--reps` ≥ 3 per task.** Run screening at reduced `n=3`
  (cheaper than the documented `n=5` promotion bar) on every PR that touches
  `candidates-all.jsonl` or a skill file, and fail if any `should`-tier
  candidate's `hard_pass_rate` drops under a threshold (e.g. < 2/3). Pro:
  catches a regression on the exact candidate that changed, fast. Con: n=3
  is below the harness's own documented reliability floor (n≥5), so the gate
  itself inherits some of the flakiness `--gate` in `run.py` was designed to
  avoid; false-fail risk on a real PR is non-trivial at n=3.
- **Option B: gate on aggregate pass-rate, not per-task.** Run the full
  screening batch at n=5 on a schedule (not per-PR, given cost) and gate on
  `mean_skills_hard_pass_rate` staying above a floor (e.g. no more than a
  5pp drop from the last committed golden), rather than any single
  candidate's cell. Pro: matches the harness's own reliability
  recommendation (n≥5) and is less prone to single-candidate flakiness. Con:
  a real regression on one candidate can be masked by noise/improvement on
  others; slower feedback (schedule, not per-PR); needs a committed
  `results/screen.json` golden to diff against, which does not yet exist.
- **Chosen: Option B**, with per-task visibility folded in from Option A's
  strength (fast localization of *which* candidate regressed) so that
  advantage isn't lost — gate on the aggregate, but always report the
  per-task breakdown next to it.
