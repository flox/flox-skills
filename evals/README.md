# Flox skills evals

Two eval suites live here, split by what they measure rather than by skill —
`flox/` covers the two prose skills, `floxify/` the one that writes a file. Each
suite owns its runners, its `tests/` package, and its own data.

| Suite | What it measures | Where |
|---|---|---|
| [`flox/`](flox/) | The **written guidance** and **implicit triggering** of the `flox` and `flox-debug` skills — does a headless `claude` answer correctly, and does the skill fire on a prompt that never says "flox"? Also guards the TOML snippets the `flox` skill ships. | `flox/run.py`, `flox/screen.py`, `flox/skill_toml_lint.py` |
| [`floxify/`](floxify/README.md) | The `/floxify` skill's **conversion outcomes** — point it at a repo, then score the `.flox/env/manifest.toml` it actually produces. | `floxify/run_floxify.py`, `floxify/real_world.py` |

Pick `flox/` when you changed guidance a user reads. Pick `floxify/` when you
changed what the conversion produces. Most changes to `flox-plugin/skills/`
touch one or the other, not both.

`flox-debug` is evaluated in `flox/` rather than in a suite of its own: it is
prose graded the same two ways, and its cases are the five `resolution`
candidates in `tasks/screening.jsonl`. They are screening-only — none is
promoted into the gated `tasks/tasks.jsonl`, so per-PR eval cost is unchanged.

The rest of this file is the common operating guide plus the runbook for the
`flox` suite. `floxify/` has [its own README](floxify/README.md).

## Run this first: `flox activate`

Every command in this file and in [`floxify/README.md`](floxify/README.md)
assumes you are inside this repo's Flox environment.

```bash
flox activate            # once, from anywhere in the repo
```

`flox activate` searches upward for the environment, so it works from the repo
root, `evals/flox/`, or `evals/floxify/` — only the script path changes.

Activation supplies both of the things the harnesses shell out to:

- **`python3`** — `python311`, declared in [`.flox/env/manifest.toml`](../.flox/env/manifest.toml)
  and pinned by `manifest.lock` on all four systems. 3.11 is a floor, not a
  preference: the harnesses parse manifests with stdlib `tomllib`. A clean
  checkout needs `flox` and no ambient Python.
- **`claude`** — the `flox/claude-code` package, so agent and judge calls use a
  pinned CLI rather than whatever is on `PATH`.

For a non-interactive shell (scripts, CI, a one-off), use the `--` form:

```bash
flox activate -- python3 evals/flox/run.py --mode skills
```

CI enters the same environment the same way. Every job in
[`.github/workflows/evals.yml`](../.github/workflows/evals.yml) carries
`shell: flox activate -- bash --noprofile --norc -e -o pipefail {0}` at the step
level, then runs the same plain `python3 ...` commands documented here.

Nothing else is installed by hand. `flox` itself is the only prerequisite;
credentials are covered under [Cost, credentials, and network](#cost-credentials-and-network).

## Shared layout

Both suites use the same directory names for the same roles, so a directory
name tells you what kind of thing is inside it.

| Directory | Role | Committed? |
|---|---|---|
| `tasks/` | Registries of cases a runner executes (JSONL, one case per line) | yes |
| `tests/` | Deterministic unit tests for the harness itself | yes |
| `fixtures/` | Input repositories the skill is pointed at | yes |
| `expected/` | Reference manifests a produced manifest is graded against — expected *properties*, not byte-exact output | yes |
| `samples/` | Captured inputs (agent stream transcripts, a real run's manifest) that tests parse | yes |
| `baselines/` | Committed comparison measurements. Runners **read** these and never write them | yes |
| `reports/` | Selected human-readable analyses worth keeping | yes |
| `results/` | Generated run output. Every `--out` / `--json` lands here | **no — gitignored** |

The `baselines/` ↔ `results/` split is load-bearing: `--out` writes under
`results/` and `--baseline` reads under `baselines/`, so a local run can never
overwrite the snapshot it is being diffed against.

`floxify/` keeps its three registries (`synthetic.jsonl`, `stretch.jsonl`,
`real-world.jsonl`) at the suite root rather than under `tasks/`; each runner's
`--tasks` / `--registry` default points at the right one.

## Deterministic vs probabilistic

This distinction decides what may gate a build.

**Deterministic** — same input, same verdict. These can and do gate:

- Everything under a suite's `tests/` package (pure stdlib, subprocess
  boundaries mocked, no `claude`).
- `flox/skill_toml_lint.py --tier structural` — flox parses a snippet or it
  does not.
- The floxify golden lints (`tests/test_real_world_golden_lint.py`,
  `tests/test_stretch_golden_lint.py`) — deterministic given a fixed catalog.
  Their catalog and lock-resolution legs read the *live* catalog, so a catalog
  outage or a renamed package is a real but external source of change.

**Probabilistic** — anything whose input is an agent run. These are reported as
rates and trends, never gated:

- LLM judge score (integer 1–5) and its binary `correct` verdict.
- Implicit triggering (`invokes_flox`) — a prompt can trigger the skill on one
  run and not the next.
- The deterministic checks in `run.py` / `run_floxify.py` are themselves pure
  functions of the answer text, but the *answer being checked* comes from a
  non-deterministic agent, so a single run's pass/fail is not the same kind of
  signal as a unit test. That is why the gates below bind only on the narrow,
  most stable subset, and why screening requires repetitions.

The JSON output calls the deterministic-check block `hard_checks` and its
rollup `hard_pass` / `hard_pass_rate`.

## What binds CI

[`.github/workflows/evals.yml`](../.github/workflows/evals.yml) has five jobs.
There is **no scheduled run** — every job fires on pull requests or on manual
`workflow_dispatch`. A `changes` job computes path filters that decide which
per-PR jobs run.

| Job | Runs on | Needs network | Anthropic spend | Blocks the build? |
|---|---|---|---|---|
| `evals` | **every PR** (required status check) | no | **none by default** | yes — on the free unit-test suites below |
| `skill-toml-lint` | PRs touching `flox-plugin/skills/flox/**`, `evals/flox/skill_toml_lint.py`, its test, or the workflow; plus dispatch | no (`--offline`) | none | **yes** |
| `golden-lint` | PRs touching `evals/floxify/**` or `flox-plugin/skills/**`; plus dispatch | yes (live catalog) | none | **yes** |
| `floxify-evals` | dispatch only, `run_floxify=true` | yes | yes | gates within that run only |
| `floxify-real-world` | dispatch only, `run_floxify=true` | yes | yes | never — report-only |

Two things are worth stating plainly because they are easy to assume otherwise:

- **The paid `flox` suite does not run on pull requests.** The `evals` job is a
  required status check and must keep reporting on every PR, so it is not
  disabled — it is defunded. What runs by default is the free deterministic
  unit-test suite from both `evals/flox/tests/` and `evals/floxify/tests/`. The
  agentic `run.py --gate` step is behind an explicit
  `workflow_dispatch` opt-in (`run_paid_evals=true`).
- **The floxify outcome evals do not run on pull requests either.** They need a
  live `flox`, a reachable catalog, and Anthropic credentials, so they are
  dispatch-only. `golden-lint` is the exception that *does* run per-PR: it needs
  flox and the catalog but spawns no `claude`, so it carries none of the cost.

Re-run a paid job by hand from **Actions → skill-evals → Run workflow**, with
`run_paid_evals=true` (the `flox` suite) or `run_floxify=true` (both floxify
jobs).

## Cost, credentials, and network

| Command | `claude` calls | Credentials | Network |
|---|---|---|---|
| `python3 -m unittest ...` (either suite) | none | none | none |
| `python3 flox/skill_toml_lint.py --offline` | none | none | none (proxy vars pointed at a closed port) |
| `python3 flox/skill_toml_lint.py --tier catalog` | none | none | live catalog |
| `python3 -m unittest tests.test_real_world_golden_lint` | none | none | live catalog (set `FLOXIFY_GOLDEN_LINT_LIVE_CATALOG=0` to force offline) |
| `flox/run.py` | 2 per task (agent + judge) | yes | yes |
| `flox/screen.py` | 4 per candidate per rep (2 arms × agent + judge) | yes | yes |
| `floxify/run_floxify.py` | 2 per fixture per rep | yes | yes |
| `floxify/real_world.py` | 2 per repo per rep, plus a full clone | yes | yes |
| `floxify/detect_usage_eval.py`, `floxify/verify_usage_eval.py` | 1 agent run each | yes | yes |

The model is pinned to `claude-opus-4-8` for agent and judge in both suites
(override with `--model`). Opus calls carry large cached-context reads, so even
a short reply is not free — assume single dollars for a full `run.py` pass and
more for a multi-rep `screen.py` batch. `screen.py` and `run.py` read each
call's cost off the JSON envelope and roll it into the run summary; check
`summary.cost` / `summary.total_cost_usd` after a batch.

Credentials come from wherever the `claude` CLI already gets them — the Flox
environment supplies the *binary*, not the login:

- **Locally**, whatever `claude` is logged in with (an OAuth token under
  `~/.claude/.credentials.json`, e.g. a Claude subscription). Usage counts
  against that account.
- **In CI**, there is no local login; the workflow exports the org-managed
  `MANAGED_SKILLS_ANTHROPIC_API_KEY` secret as `ANTHROPIC_API_KEY`. That key is
  the only credential any job sees.

The deterministic unit tests need neither.

---

# The `flox` suite

Scores the `flox` skill's written answers. For each task it runs `claude`
headless with the plugin loaded, then grades the answer two ways: deterministic
checks over the answer text, and a separate `claude` judge call scoring 1–5
against the task's rubric.

Run its commands from `evals/flox/` — the harness modules import each other by
bare name (`import run`), so the suite root has to be the working directory.

```bash
cd evals/flox
```

## Tasks

`tasks/tasks.jsonl` is the gated registry (one JSON object per line). Each entry
carries an `id`, a `tier`, an `area`, the `checks` that apply, and a judge
`rubric`.

Tiers describe triggering expectation, and they decide gate policy:

| Tier | Meaning | Gates? |
|---|---|---|
| `should` | Must trigger and be correct | yes, on deterministic checks |
| `may` | Nice if it triggers | no |
| `stretch` | We would like it to; fine if it does not | no |

An entry with `"trigger_test": true` measures **implicit triggering**: the
prompt never says "flox" (e.g. "create a new Node.js project"), a neutral system
instruction is used so nothing biases the model, and the `invokes_flox` check
asks whether the skill fired anyway and produced Flox guidance. Triggering is
probabilistic, so trigger tasks are reported and never gated — including
`should`-tier ones.

## Run

```bash
python3 run.py --mode skills                     # the skills arm, whole registry
python3 run.py --mode baseline                   # the bare model, for comparison
python3 run.py --mode skills --only node-env     # one task
python3 run.py --mode skills --gate              # exit non-zero on a binding failure
python3 run.py --mode skills --concurrency 8     # parallel claude calls (default 6)
python3 run.py --plugin-dir /path/to/flox-plugin # score a different plugin checkout
```

Output lands in `results/<mode>.json` (gitignored) with a per-task record and a
summary: `hard_pass_rate`, `avg_judge_score`, `judge_correct_rate`, a `by_tier`
breakdown, triggering rates, and `cost`. The run also prints a diff against the
committed baseline of the same name under `baselines/`.

Unit tests, from the same directory:

```bash
python3 -m unittest discover -s tests -t . -v   # the whole suite
python3 -m unittest tests.test_run -v           # one module
```

## Gate policy

`--gate` binds on exactly one thing: **every non-trigger `should`-tier task must
pass all of its deterministic checks.** A failure, or an errored `should`-tier
task, exits non-zero.

Everything else is advisory and reported but never blocks:
`avg_judge_score`, `judge_correct_rate`, the `by_tier` breakdown,
`trigger_invokes_flox_rate`, and `should_trigger_rate`. Watch these for a
sustained drop rather than reacting to one run — with the judge at roughly 95%
per-task reliability, an all-green run across the registry is not the expected
outcome even from a healthy skill.

Checks that assert something about a **manifest** parse it with `tomllib` and
inspect the resulting dict rather than grepping the answer: a fenced block is
extracted with `skill_toml_lint.extract_blocks` and every fact about it is
asserted against that same block. A whole-answer grep would certify a manifest
it never inspected — an answer whose prose says `schema-version = "1.12.0"`
while its only manifest keeps `version = 1` passes a text search and hands the
user a file flox refuses to load.

The check functions are in [`run.py`](flox/run.py) (`CHECKS`); their tests are in
[`tests/test_run.py`](flox/tests/test_run.py).

## Manifest-snippet check (`skill_toml_lint.py`)

`run.py` checks manifests the model **generates**. This one checks the manifests
the skill **ships**: every fenced ` ```toml ` block in
`flox-plugin/skills/flox/SKILL.md` and `references/*.md` is fed to `flox edit -f`
inside a throwaway `flox init` environment, so a snippet a user would copy-paste
is proven to parse.

That path is `--skill-dir`'s default, and it is the only one CI checks: the
`skill-toml-lint` job's path filter and the guard's default both name the `flox`
skill. Another skill's blocks are checkable by hand — `--skill-dir
flox-plugin/skills/flox-debug` — but nothing runs it for you.

| Tier | Requires | Binds CI? |
|---|---|---|
| `structural` (default) | flox **parses** the snippet — only `Failed to parse manifest` fails | **yes**, via the `skill-toml-lint` job |
| `catalog` (`--tier catalog`) | ...and every package resolves against the live catalog | no, advisory |

flox parses the whole manifest before resolving anything, so the structural tier
catches parse defects **without the catalog** — roughly 25ms per snippet and
offline-safe. `--offline` points the proxy vars at a closed port so a networked
runner behaves exactly like an air-gapped one; CI runs it that way, which means
a catalog outage can never redden the job. The catalog tier is report-only by
design: it fails for reasons that have nothing to do with the skill.

```bash
python3 skill_toml_lint.py                     # structural tier (what CI gates on)
python3 skill_toml_lint.py --offline           # ...and prove it needs no network
python3 skill_toml_lint.py --tier catalog      # + live catalog resolution (advisory)
python3 skill_toml_lint.py --only services.md  # one document
python3 skill_toml_lint.py --list              # extract only, no flox
python3 skill_toml_lint.py -v                  # print every block, not just failures
python3 skill_toml_lint.py --json results/skill-toml-lint.json
python3 -m unittest tests.test_skill_toml_lint # the guard's own tests
```

Exit 0 if every checked snippet passed its tier, 1 otherwise. Your own
activation only supplies the interpreter — the `flox init` / `flox edit -f`
environments the guard drives are throwaway ones in temp dirs, unaffected by it.

**Snippets that declare `schema-version`.** The guard prepends `version = 1` to
any block that does not declare a schema itself. `version` and `schema-version`
are mutually exclusive in flox, so a block exercising a field a later schema
added (e.g. `services.auto-start`, which needs `schema-version = "1.12.0"`) must
declare that key, and a top-level `schema-version` suppresses the prepend
exactly like `version = 1` does.

**Opting a block out.** A block that is deliberately partial — package
descriptors with no `[install]` header, metadata meant to be merged into a
`[build.<name>]` — cannot parse standalone. Mark it explicitly; the guard never
guesses. Preferred is a standalone comment line inside the block, which keeps
` ```toml ` highlighting and forces you to write down why:

````markdown
```toml
# eval: skip fragment - metadata fields only, merge into a [build.<name>]
[build.mytool]
version.command = "git describe --tags"
```
````

Or, for a block that is not a flox manifest at all, use the fence info string:

````markdown
```toml-fragment
[tool.poetry]
```
````

The reason text is mandatory: `tests/test_skill_toml_lint.py` fails any block
marked without one. **Never add a marker to silence a real parse error** — fix
the snippet. `KNOWN_PARSE_FAILURES` in the script is the escape hatch for a
genuine defect too large to fix in the same PR; it is currently empty and meant
to stay that way. Entries are keyed by content hash, so a stale one — left
behind after its snippet was fixed — is itself a failure and cannot sit there
absorbing a future regression.

## Screening (`screen.py`)

`screen.py` is the pre-promotion tool: it runs candidate prompts through both
the **baseline** arm (bare model) and the **skills** arm (plugin loaded) and
classifies each as

- **discriminator** — the skills arm passes where the baseline fails,
- **skill-gap** — both fail, so the skill may be missing coverage,
- **no-signal** — the baseline already passes, so the candidate is too easy.

It is not wired into CI; run it by hand (or have an agent run it) before adding
a candidate to `tasks/tasks.jsonl`, which *is* gated.

`tasks/screening.jsonl` is the one active registry of screening candidates.
Subsets come from stable entry metadata, never from a second file:

| Selector | Entry field | Example |
|---|---|---|
| `--area` (repeatable) | `area` — `triggering`, `freshness`, `environments`, `builds`, `services`, `composition`, `sharing`, `publish`, `cuda`, `containers`, `resolution` | `--area triggering --area freshness` |
| `--regression` | `regression: true` — kept to guard a specific check or skill fix | `--regression` |
| `--only` | `id` | `--only trap-vars-no-interpolation` |

```bash
python3 screen.py --reps 5                                       # whole registry, n=5
python3 screen.py --area triggering --reps 5                     # one area
python3 screen.py --regression --reps 5                          # the fix-guard set
python3 screen.py --only trap-vars-no-interpolation --reps 5
python3 screen.py --plugin-dir /path/to/fixed-skill/flox-plugin  # test a skill edit
```

Results land in `results/screen.json`, including `summary.total_cost_usd` and a
per-arm `cost_usd`. Screening is not free and `--reps` multiplies call volume,
so check the cost after a batch.

**`--reps` ≥ 5 is required for any promote / discard / skill-gap decision.**
Single runs have a roughly 50% cell-level flip rate, so a lone pass/fail is
dominated by sampling noise. `screen.py` reports `hard_pass_rate` (the fraction
of reps passing) and a mean judge score; `hard_pass` and `judge_correct` are
majority verdicts. Compare **pass-rates**, not single cells: a discriminator
must show a rate gap that survives n≥5.

**Arm isolation.** `--setting-sources` (default `project,local`) is passed to
every arm. Dropping `user` suppresses globally-enabled plugins, so the baseline
is a genuinely bare model even on a host where the Flox plugin is enabled in
`~/.claude/settings.json`. Pass `all` only where no Flox plugin is globally
enabled.

**Model policy.** Screening uses the same model as the gate — Opus
(`claude-opus-4-8`). Content recall does not separate modern Claude from itself,
so the value of a screened candidate is in triggering and freshness, which the
Opus gate measures directly. There is no separate weaker-model arm.

### Designing a check

**First, the prompt has to be self-contained.** The harness runs the agent from
`evals/flox`, where there is no `.flox` directory to inspect, so a candidate for
a *diagnostic* skill must hand over the environment being diagnosed — the
manifest, the error, and the specific thing being changed. Given a scenario it
cannot inspect, a well-behaved skill correctly asks the user for the missing
information, and an answer that asks a question scores zero on every check.
Three of the four `resolution` candidates failed exactly this way before it was
diagnosed; the fix is a self-contained prompt, not a weaker skill.

Candidates then carry data-driven `must_match` / `must_not_match` regex lists.
Five rules, each of which exists because a check that ignored it produced false
verdicts:

- **A `must_match` must not be satisfiable from its own prompt.** Once a prompt
  carries a manifest and an error message, the model can satisfy the pattern by
  quoting the prompt back, and the candidate then measures nothing. Run every
  `must_match` against its own `prompt` and require no match. A check for
  `allow_unfree` is safe against a prompt containing `allow.unfree` — the model
  has to perform the translation — but `(?i)resolve` is *not* safe against a
  prompt containing the word "resolver".
- **Prefer positive `must_match` over negative `must_not_match`.** Assert the
  correct construction rather than detecting the wrong one.
- **A correct answer often illustrates the anti-pattern as a labeled
  counter-example**, so a negative or proximity check false-fires on good
  answers.
- **A case-insensitive `must_not_match` can match ordinary prose.** There is no
  safe case-insensitive substring for an all-caps directive like a Dockerfile
  `FROM` line — `re.I` matches the English word "from" too.
- **A literal multi-word `must_match` assumes one argument order.** Prefer
  `uv pip install\b.*--python\b` (same line, either order) over a fixed-order
  substring.

**Validate a new or edited check against a real known-good answer** before
trusting it — the `answer_excerpt` fields in `results/*.json` or
`baselines/*.json`, or a fixture copied into a unit test. A check is a pure
function of the answer text, so this needs no model calls.
[`tests/test_screen.py`](flox/tests/test_screen.py) does exactly this for every
shipped check.

### Reports

`gen_screening_report.py` renders one or more screening result files into a
human-readable analysis:

```bash
python3 gen_screening_report.py --results results/screen.json
```

It writes `reports/SCREENING-REPORT.md` by default. `reports/` holds analyses
kept on purpose, not ordinary run output.

## Baselines

`baselines/` holds committed comparison points that runners read and never
write:

| File | What it is |
|---|---|
| `skills.json` | `run.py --mode skills` over the gated registry |
| `baseline.json` | `run.py --mode baseline` — the bare-model arm |
| `screen-opus.json`, `screen-sonnet.json`, `screen-haiku.json` | `screen.py` at n=5 per model |

Refreshing one is a deliberate, reviewable act, never a side effect of a run:

```bash
python3 run.py --mode skills --out refresh.json
cp results/refresh.json baselines/skills.json
```

Commit the copy with the change that justifies it.

## Policy: every skill change ships with an eval — written RED first

**Every PR that adds, changes, or *fixes* guidance in a skill must add an eval
that verifies the guidance is actually followed.** Reviewers should not approve
a skill PR without one.

1. **RED first.** Write the eval before the fix, run it, and watch it **fail for
   the reason you claim**. An eval written after the change only proves the
   change is self-consistent.
2. **Prefer the cheap tier.** A prompt eval or a synthetic fixture is the inner
   loop (seconds). The real-world runs in `floxify/` are confirmation — a clone
   plus a full agentic pass. Reserve the expensive tier for proving a fix holds
   on real repos. The same split applies inside screening at a finer grain:
   `screen.py --only <candidate-id>` (one candidate, ~$0.30 at `--reps 5` on
   Haiku) is the inner loop for *why* something fails; `screen.py --area <area>`
   (~$1.50 at the same settings) confirms nothing else moved, and is not a
   diagnosis tool.
3. **Read a multi-fix skill end to end before it ships, against live data.**
   "Every change was reviewed" does not imply "the procedure is correct." Two
   commits to `flox-debug` were each correct alone and each passed its own diff
   review — one carried the environment's `allow` options into the catalog
   resolve request, changing the response's shape; a later one, by a different
   author, ruled that only `error`-level messages belong in a diagnosis. Together
   they told the agent to discard the only messages that named the cause: a group
   failing on a licence restriction returns the reason as `(trace,
   resolution_logic)`, while the `error`-level rows are one generic
   `constraints_too_tight` and a pile of `attr_path_not_found` scrape artefacts.
   No diff review caught it; a whole-file read probing the live API did. Probe
   one healthy case and one failing case for every response shape the skill
   claims, and beware a rule generalised from a single measurement — the first
   attempted fix keyed on `complete: false`, which is near-universal, and
   discarded 40 genuine findings.

Fixes to existing guidance are in scope, not just new features.

**A cheap eval that refuses to fail is a finding, not an obstacle.** If a
fixture will not reproduce the bug, say so in the PR and use the expensive tier
as the test, rather than contriving a fixture that fails for a manufactured
reason. A green suite over a broken skill is the failure mode this policy exists
to prevent.

Why an eval is worth writing at all: modern Claude already knows Flox, so most
guidance shows no measurable lift — *except* for what the model cannot already
know (post-cutoff CLI behavior, Flox-specific idioms). A new feature is
therefore the one place an eval genuinely discriminates, and the task doubles as
a conformance check: does the model, with the skill, produce the idiom the skill
teaches?

## Adding or updating an eval

**A gated task** (`tasks/tasks.jsonl`):

1. Write a prompt a user would actually ask that should invoke the new guidance.
2. Add a deterministic `must_match` for the Flox-specific idiom the skill
   teaches, plus a judge rubric. Follow the check-design rules above.
3. Screen it baseline-vs-skills at `--reps 5` to confirm the skills arm follows
   the guidance.
4. Append one line to `tasks/tasks.jsonl` with `id`, `tier`, `area`, `checks`,
   and `rubric`. Use `"trigger_test": true` if the prompt deliberately never
   says "flox".
5. Run `python3 run.py --mode skills --only <id>` to confirm it scores as
   expected end to end.

**A screening candidate** (`tasks/screening.jsonl`): append one line with the
`area` that makes it selectable, plus `regression: true` if it guards a specific
fix. Do not add a batch file — subsets come from entry metadata.

**A new deterministic check**: add it to `CHECKS` in `run.py`, add positive and
negative tests to `tests/test_run.py`, then reference it by name from the task's
`checks` array.
