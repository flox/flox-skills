# Flox /floxify skill evals

Outcome-based eval suite for the `/floxify` skill. Unlike `../flox/run.py`
(which scores text answers), this harness copies a synthetic fixture repo
to a temp dir, runs the `/floxify` skill headlessly, and scores the
`.flox/env/manifest.toml` it produces.

Run every command below from this directory (`evals/floxify/`) — it is this
suite's root, and the unit tests live in the `tests/` package beside the
runners (AI-509 Ticket 2):

```bash
python3 -m unittest discover -s tests -t . -v   # the whole unit-test suite
python3 -m unittest tests.test_verify -v        # one module
```

**Run `flox activate` once before the commands below** — see
[the runtime note in `../README.md`](../README.md#the-runtime-activate-once).
`python3` and `claude` both come from this repo's own environment
(`.flox/env/manifest.toml`), pinned by `manifest.lock`, and CI gets the same
two the same way. Note the two senses of "activate" in this file: your own
activation is just how you get the harness a Python, while the *activation
check* below is the harness activating a manifest the skill produced, in a temp
dir, as a scored outcome. They are unrelated.

## Phase 1 analyzer (`scripts/detect.py`) — grounding, and its two evals

The skill's Phase 1 runs a bundled deterministic analyzer,
`flox-plugin/skills/floxify/scripts/detect.py`, before it reads anything by
hand. The analyzer scans the repo and emits grounded JSON — runtime version
pins (each tagged with the file it came from), package-manager versions read
from lockfiles, docker-compose services (with a `config_coupled` flag),
service-client dependencies, and monorepo/orchestrator markers. It never
touches the catalog: mapping a detected runtime to a `pkg-path` stays with the
model via `flox search` / `flox show`, so every `search_terms` value is a hint
to verify, not an asserted package. The skill invokes it through Flox so it
needs no system Python (and prompts the user to upgrade if `flox run`, which
shipped in 1.13, is unavailable):

```bash
flox run -p python313 -- python3 "<skill-dir>/scripts/detect.py" "$TARGET_DIR"
```

Per the repo-wide new-feature eval policy (`../README.md`), this feature ships
with two eval layers:

- **`tests/test_detect.py`** — fast, deterministic unit tests. Asserts the analyzer
  extracts the right facts from every `fixtures/` repo (Node from `.nvmrc`,
  Ruby + bundler from `Gemfile`/`Gemfile.lock`, `requires-python` from
  `pyproject.toml`, `pg` → postgres, deno from `deno.json` / an
  `edge-runtime` image, `config_coupled` compose services, …). Pure stdlib,
  no `claude` calls — safe to run anywhere and cheap enough to gate.

  ```bash
  python3 tests/test_detect.py  # standalone (pytest also works, if you have one -- the environment does not ship it)
  ```

- **`detect_usage_eval.py`** — behavioral conformance. Runs a real,
  Phase-1-bounded `/floxify` against a fixture with a tool-call-visible stream
  and asserts the skill *actually invoked* `detect.py` (bonus signal: through
  `flox run`). Heavy and opt-in like real-world — it spawns a `claude` agent — so
  it's manual/scheduled, never in the fast gate.

  ```bash
  python3 detect_usage_eval.py                 # default fixture (node-postgres)
  python3 detect_usage_eval.py --fixture ruby
  ```

`tests/test_detect.py` proves the analyzer is *correct*; `detect_usage_eval.py`
proves the skill *reaches for it*. Together they close the loop the policy asks
for: guidance added, guidance verified.

## Phase 3c checker (`scripts/verify.py`) — grounding the OUTPUT

`detect.py` grounds the skill's INPUT; nothing grounded the OUTPUT — the
produced `manifest.toml` was checked only by an LLM judge (which has graded
catalog facts from memory and accused a *correct* pin of being hallucinated,
AI-451) and by prose the model follows most, not all, of the time (AI-460).
`flox-plugin/skills/floxify/scripts/verify.py` closes that gap: it takes
`detect.py`'s JSON facts plus a produced `manifest.toml` and reports concrete
violations — every detected runtime installed, every leaf-datastore client
and `[vars]` connection-string endpoint actually served, `[vars]` staying
literal, hooks never mutating the tracked git tree, and every
`pkg-path`/`version`/`systems` combination resolving in the live catalog
(via `flox show`, ADVISORY-skipped when flox/network is unavailable — same
treatment `_check_activation` already gets). A native-build-input-with-no-
`outputs` heuristic is ADVISORY-only by design — hard-failing a judgment
call reproduces the judge's own failure mode in Python. The skill runs it as
Phase 3c Step 4, gating the report the same way Steps 1-3 already do.

Three consumers, one checker (no duplicated logic):

- **The skill** (Phase 3c Step 4) — violations stop the flow; fix, re-run.
- **The eval harnesses** (`run_floxify.py` synthetic, `real_world.py` real-world since
  AI-465) — each re-scans its fixture/checkout and runs the checker
  against the produced manifest as its own deterministic leg, reported
  per-task/per-repo and in the summary (`verify_checked`, `verify_clean`,
  `verify_hard_violation_rate`); advisory, same reason activation is
  advisory (see "Why verify.py is advisory in the harness" below). Its
  confirmed catalog resolution table is handed to the LLM judge so it
  stops grading catalog facts from memory (AI-451). The real-world tier
  reuses the synthetic tier's `_run_verify`/`_catalog_note` rather than
  duplicating them — see
  "What's different from synthetic" under the real-world section below.
- **The goldens** (`expected/*.toml`) — linted by the same checker as a
  cheap unit-test-tier check (no `claude`, no agent); see `tests/test_real_world_golden_lint.py`
  below.

Eval layers, same two-tier shape as `detect.py`'s:

- **`tests/test_verify.py`** — fast, deterministic unit tests. Every invariant
  carries a positive test (fires on the real defect) and a negative test
  against a realistic manifest shape (proves it does NOT false-fire) — "a
  wrong invariant is worse than no invariant." Catalog checks are mocked at
  the `flox show` boundary (`_run_show_command`) so the whole suite runs
  with no network.

  ```bash
  python3 -m unittest tests.test_verify -v
  ```

- **`tests/test_real_world_golden_lint.py`** — runs the checker over the
  real-world goldens named in `real-world.jsonl`. Selection is by registry,
  not by globbing `expected/`, which since AI-509 Ticket 3 also holds the
  synthetic and stretch reference manifests. Two hand reviews (AI-455) found real
  defects in those goldens that had never been linted before; this check
  found 16 more (per-system catalog gaps across 6 of 8 goldens) the moment
  it ran with live flox. Golden content is intentionally NOT fixed here —
  that is AI-457's consolidated pass — so this lands with an explicit
  `KNOWN_VIOLATIONS` allowlist, one entry per current defect, matched
  against the violation's structured `pkg_path` field EXACTLY (not a
  substring of the message — a short needle like `uv` or `deno` would
  otherwise risk colliding with unrelated text). A dedicated test asserts
  every allowlist entry still matches a live violation, so AI-457 fixing a
  golden without removing its entry doesn't leave a stale slot that could
  silently absorb a future unrelated regression. Degrades gracefully
  (passes trivially, no network) without flox — but set
  `FLOXIFY_GOLDEN_LINT_LIVE_CATALOG=0` to force that mode explicitly rather
  than relying on flox's ambient absence. Since AI-509 that switch is the
  *only* thing holding the free tier catalog-free: flox is now always on
  PATH there (it supplies `python3`), so the CI step that used to be
  flox-less pins the switch to 0 and means it. The lint's real teeth are
  still the `golden-lint` job, which leaves the switch at its default.

  ```bash
  python3 -m unittest tests.test_real_world_golden_lint -v
  FLOXIFY_GOLDEN_LINT_LIVE_CATALOG=0 python3 -m unittest tests.test_real_world_golden_lint -v  # no network
  ```

  **Whole-manifest lock-resolution leg (AI-479).** Every check above is
  PER-PACKAGE — none of them can see a manifest whose packages each
  resolve individually but cannot co-resolve TOGETHER on any single
  catalog page (`constraints for group 'X' are too tight`). AI-457 and
  AI-478 only caught that class by hand, running `flox activate`
  themselves against a scratch directory. `tests/test_real_world_golden_lint.py` now adds
  one more test per golden, `test_<fixture>_locks_cleanly`, that attempts
  a real `flox list -c` in a throwaway environment with the candidate
  manifest written directly into it. `flox list -c` is resolution-only:
  it locks (writes `manifest.lock` via a catalog-API-only resolve) but
  never builds or fetches store paths — unlike `flox edit -f`, which
  transactionally builds the environment to validate the edit (`man
  flox-edit`) and was this leg's original, wrong instrument (see the
  caveat below). Measured locally against all 8 goldens: 0.5-1.5s each,
  zero net `/nix/store` writes. Same skip discipline as the catalog leg:
  advisory-skip when `flox` is absent or
  `FLOXIFY_GOLDEN_LINT_LIVE_CATALOG=0` is set, never gating the flox-less
  free-tests step. When it DOES run, a genuine resolver defect (`flox
  list -c` stderr starting `resolution failed:`) is a real, reportable
  finding on that golden — not something to allowlist or silently work
  around by fixing golden content in the same change that adds the
  check. A catalog-API communication error is a different, transient
  failure class: it gets one retry and, if it persists, is reported with
  an honest "likely transient" message rather than the resolution-defect
  verdict — see `_classify_lock_failure` in `tests/test_real_world_golden_lint.py`.
  `TestLockResolutionLeg` covers the skip/fail/pass/retry/classification
  plumbing with mocked, no-network unit tests; the live behavior (does a
  real golden actually lock) is exercised by the per-golden tests above,
  which need the same `golden-lint` CI job's live flox install the
  catalog leg does.

  **Instrument history:** the leg originally used `flox edit -f`, chosen
  because it looked resolve-only in local testing (a warm nix store made
  it fast). It went RED in CI (PR #56) — `flox edit -f`'s build-to-
  validate step realized the closure on a cold runner, and a fetch
  failure for one otherwise-fine package (supabase's `nodejs_22`)
  surfaced as a false "cannot co-resolve" finding. `flox list -c`
  replaced it once source-reading (flox-rust-sdk's `lock`/`build` are
  architecturally separate methods) and empirical testing (no store
  writes, consistent sub-2s timing on pass and fail) confirmed it never
  realizes.

  **Caveat:** the LLM judge (in `run_floxify.py` and `real_world.py`) grades
  produced manifests against these same goldens. Until AI-457 lands, a
  defective golden could in principle nudge the judge to penalize a
  correct manifest that differs from the defect, or reward one that
  copies it. The judge score is advisory and noisy run-to-run regardless
  (see the Gate policy section below), so this doesn't change what's
  gate-binding — but it's worth knowing the reference isn't yet clean.

- **`verify_usage_eval.py`** — behavioral conformance, same shape as
  `detect_usage_eval.py` but Phase-3-bounded (needs a written manifest for
  verify.py to have anything to check): runs a real `/floxify` through
  package resolution and manifest-writing, and asserts the skill *actually
  invoked* verify.py against it. Heavy and opt-in — spawns a `claude`
  agent — never in the fast gate.

  ```bash
  python3 verify_usage_eval.py                 # default fixture (node-postgres)
  ```

### Why verify.py is advisory in the harness

The live skill hard-gates on verify.py's invariants (Phase 3c Step 4
blocks the report on any HARD violation), but `run_floxify.py`'s
deterministic leg never fails `--gate` on the same checks — including the
network-free ones (`runtime-not-installed`, `leaf-datastore-not-served`,
`vars-not-literal`, `hook-mutates-tree`) that don't need flox/network and
so, in principle, could gate reliably. Two reasons this stays a
measurement rather than a gate:

1. **Evals report rates, not states.** The AI-460 lesson: a single run
   flipping pass/fail on a probabilistic agent output is not the same
   kind of signal as a deterministic unit test, even when the *check*
   itself is deterministic — the *manifest being checked* still comes
   from a non-deterministic agent run. `hard_pass_rate`, `avg_judge_score`,
   and now `verify_hard_violation_rate` are tracked as trends (watch for a
   sustained rise), the same treatment every other advisory metric in this
   harness already gets.
2. **There is no per-PR skill run to gate.** The paid `floxify-evals` job
   (which is what would produce these manifests on a PR) is dispatch-only
   — see the CI section below — so making the harness leg gate-binding
   would not actually protect any PR; it would only bind the weekly/manual
   runs, which already report their `--gate` verdict through the existing
   hard-check column.

Watch `verify_hard_violation_rate` in the run summary and the per-fixture
`verify` column in the CI step summary for a sustained regression — that
is the signal this leg exists to surface, even though nothing here fails
the build on it.

## What it does

For each fixture in `fixtures/`, the harness:

1. Copies the fixture to a temp dir (no `.flox/` — the skill creates it)
2. Runs `claude /floxify <dir>` headlessly with the skill loaded
3. Reads `.flox/env/manifest.toml` from the temp dir
4. Scores it with deterministic hard-checks + an LLM judge (vs `expected/`)
5. Attempts `flox activate -c "echo __ok__"` (advisory; skipped if
   unavailable)

### Hard checks (deterministic — bind the gate)

| Check | What it verifies |
|-------|-----------------|
| `manifest_created` | `.flox/env/manifest.toml` was written |
| `valid_toml` | file parses as valid TOML |
| `has_install_section` | `[install]` section present |
| `has_services_section` | `[services.*]` present (node-postgres only) |
| `no_abs_paths` | no `/home/` `/Users/` etc. in manifest values |
| `no_fake_install_url` | no hallucinated Flox install URLs |
| `pins_node_20` | manifest names `nodejs_20` (not generic `nodejs`) |
| `pins_python` | manifest references a python package |
| `pins_go` | manifest references the `go` package |
| `pins_rust` | manifest references `cargo` |
| `pins_ruby` | manifest references `ruby` |

Each task in `synthetic.jsonl` lists which checks apply via the `checks` array.
Checks not listed for a task are not evaluated.

### Activation check (advisory)

After the skill writes the manifest, the harness runs:

```bash
flox activate -c "echo __ok__"
```

in the temp dir. It records:
- `ok: true` — activation succeeded, `__ok__` appeared in stdout
- `ok: false` — activation ran but failed (check `notes` for the error)
- `skipped: true` — `flox` not in `PATH`, timed out, or
  `--skip-activation` was passed

Activation is **advisory** — it is reported but never blocks the gate.
Catalog resolution requires a working Flox installation and network
access to FloxHub, so CI runs without those should pass
`--skip-activation`.

### LLM judge (advisory)

The judge compares the produced manifest against `expected/<id>.toml` and
grades 1–5 on package choices, hook quality, and idiomatic Flox usage.
The gold files are references, not byte-exact match targets — a
well-structured manifest that differs idiomatically can still score 4-5.

Judge scores are **advisory** — reported in the summary, never block
the gate.

## Run

```bash
# Single fixture (fastest for development):
python3 run_floxify.py --only node-20

# All 6 fixtures:
python3 run_floxify.py

# With gate (fails CI if any should-tier hard-check fails):
python3 run_floxify.py --gate

# Skip the produced-manifest activation check (no catalog / network):
python3 run_floxify.py --skip-activation

# Custom skill dir (skill ships in-repo at flox-plugin/; override if needed):
python3 run_floxify.py --skill-dir /path/to/flox-plugin

# Custom output path:
python3 run_floxify.py --out results/my-run.json

# Diff against a specific committed baseline (default: synthetic.json):
python3 run_floxify.py --baseline synthetic.json   # a file under baselines/
```

Results land in `results/` as JSON with a summary (hard-pass rate,
avg judge score, activation counts). `results/` is **gitignored** generated
output; the committed comparison points live in `baselines/`, which the
runners read and never write. Pure stdlib — no pip install needed.

## Regression detection

After each run the harness diffs the results against the committed
baseline (`baselines/synthetic.json` by default, override with
`--baseline`) and prints a **regression diff**:

- **hard-check regressions** — a fixture that passed all hard-checks in
  the baseline but fails now (the signal that matters; these are the
  gate-binding checks)
- **hard-check fixes** — the reverse
- **new / removed fixtures** — fixtures added to or dropped from the suite
- **judge-score delta** — advisory only; the judge is noisy run-to-run

A run can no longer clobber its own comparison target: `--out` writes
under `results/` and `--baseline` reads under `baselines/` (AI-509
Ticket 3), so the two are never the same file.

To refresh the baseline after an intentional skill change, run the full
suite, then copy its output over `baselines/synthetic.json` and commit
that — a deliberate, reviewable act rather than a side effect of a run:

```bash
python3 run_floxify.py --out results/refresh.json
cp results/refresh.json baselines/synthetic.json
```

## Prerequisites

1. **`flox` CLI** — the only thing you install by hand. It supplies both
   `python3` and `claude` through this repo's environment, and the
   activation checks below need it anyway. (It used to be listed here as
   *optional*, for activation only; since AI-509 it is the entry point.)
2. **Credentials for `claude`** — logged in (`claude auth login`) or
   `ANTHROPIC_API_KEY` set. The deterministic unit tests need neither.
3. **The floxify skill** — ships in this repo at
   `flox-plugin/skills/floxify/`, no separate checkout needed. The
   default `--skill-dir` resolves to `flox-plugin/` two levels up from
   this file. Override with `--skill-dir` to point at an alternate
   flox-plugin directory.

## Gate policy

- **Binding (deterministic):** every `should`-tier fixture must pass all
  hard-checks listed in its `synthetic.jsonl` entry. A failure exits non-zero
  under `--gate`.
- **Advisory (reported, never blocks):** `avg_judge_score`,
  `judge_correct_rate`, `activation_ok`. These track quality trends —
  watch for a sustained drop, but a single noisy run should not block.

`may` and `stretch` tiers do not exist in this suite yet (all 6 fixtures
are `should`). Add them when non-blocking exploratory fixtures are needed.

## CI: scheduled/manual, NOT per-PR

Unlike the text-answer `skills` eval (`evals/flox/run.py`), which gates on
**every** pull request, the `/floxify` eval does **not** run per-PR. It is
an outcome eval: it runs the skill against fixture repos and then attempts
`flox activate`, which needs a live `flox` binary, a reachable Flox
catalog, and network access. That is too slow and too environment-dependent
to block PRs on.

Instead, the `floxify-evals` job in `.github/workflows/evals.yml` runs:

- **weekly** (scheduled, Monday 06:00 UTC) as a regression watch, and
- **on manual dispatch** (`workflow_dispatch` with `run_floxify=true`).

The job checks out this repo (the skill under test ships in-repo at
`flox-plugin/skills/floxify/`), installs flox, and runs
`run_floxify.py --gate`. Its `--gate` still binds on should-tier
hard-checks — it just runs on a schedule rather than per-PR, so a
genuine regression surfaces within a week (or immediately on manual
dispatch) rather than never.

Per-PR floxify regression-catching is therefore **manual/scheduled by
design** — the live-flox dependency makes a fast per-PR gate impractical.

**Exception: `golden-lint`.** The golden-manifest lint (`tests/test_real_world_golden_lint.py`,
see "Phase 3c checker" above) is a SEPARATE job from `floxify-evals` and
does NOT follow the dispatch-only rule above — it needs `flox` (for
`flox show`) but never spawns `claude`, so it carries none of the cost or
trust concerns that keep the outcome evals dispatch-only. It runs on
every PR that touches floxify sources (via the `changes` path filter),
same trigger shape as the fast `evals` job, just in its own job so the
flox install doesn't slow down the flox-less unit tests.

## Fixtures

| id | files | what the skill must detect |
|----|-------|---------------------------|
| `node-20` | `package.json`, `.nvmrc` | Node 20 from both sources → `nodejs_20` |
| `python-uv` | `pyproject.toml`, `uv.lock`, `src/main.py` | Python 3.12, uv, uv sync |
| `go-mod` | `go.mod`, `main.go` | Go 1.21 |
| `rust-cargo` | `Cargo.toml`, `src/main.rs` | cargo + rustc toolchain |
| `ruby` | `Gemfile`, `Gemfile.lock` | Ruby 3.3.0, bundle install |
| `node-postgres` | `package.json`, `.env.example` | Node 20 + postgres service |

Fixtures deliberately have NO `.flox/` directory. The skill writes it.

## Gold manifests

`expected/<id>.toml` — hand-tuned reference manifests seeded from:

| fixture | source |
|---------|--------|
| `node-20` | `floxenvs/javascript-node/.flox/env/manifest.toml` |
| `python-uv` | `floxenvs/python-uv/.flox/env/manifest.toml` |
| `go-mod` | `floxenvs/go/.flox/env/manifest.toml` |
| `rust-cargo` | `floxenvs/rust/.flox/env/manifest.toml` |
| `ruby` | `floxenvs/ruby/.flox/env/manifest.toml` |
| `node-postgres` | `floxenvs/javascript-node` + `floxenvs/postgresql` (merged) |

Cross-checked against `floxdocs/docs/languages/*.md` idioms. Gold files
are **references for the LLM judge**, not byte-exact match targets.

## Adding a fixture

1. Create `fixtures/<new-id>/` with the project files (no `.flox/`)
2. Create `expected/<new-id>.toml` with the ideal manifest
3. Add a line to `synthetic.jsonl` with `id`, `tier`, `ecosystem`,
   `checks`, `rubric`
4. Run `python3 run_floxify.py --only <new-id>` to verify end-to-end

## Baseline

`baselines/synthetic.json` — recorded against the in-repo
`flox-plugin/skills/floxify/` skill. The `summary.skill` field records
a portable identity (`<dir-name>@<branch>`, e.g.
`flox-plugin@main`) rather than an absolute host path, so the committed
baseline stays reproducible across machines. `summary.model` records
the pinned judge/agent model.

When activation was not available in the recording environment, it is
recorded as `"skipped": true` with a note. Hard-checks and judge scores
are still populated.

## Environment limitations

Running this harness in full requires:

- Network access to the Flox catalog (for `flox search` during skill
  execution)
- A working `flox` binary (for `flox init` and activation checks)
- Sufficient API credits (6 skill runs + 6 judge calls = ~12 claude
  invocations)

If catalog access or `flox` is unavailable:
- The skill may still produce a manifest (from its hardcoded knowledge)
  but hard-checks will reflect whether it actually ran catalog searches
- Activation is automatically skipped and recorded as such
- Use `--skip-activation` to suppress the activation attempt entirely

## Efficiency axis (AI-442)

AI-435 found Claude reaches a correct manifest on its own, across
model tiers — correctness no longer discriminates the skill from the
bare model. What the skill actually changes is the *path*: how much
search-and-verify effort it takes to get there. This section is the
capture-and-measurement machinery for that axis. PR 1 lands the
plumbing and its unit tests, not a measurement run — "First real
signal" below is how to run one once you're ready to spend the API
budget.

### What gets captured

Every agent and judge call already returns cost/usage/turn data on the
envelope (`total_cost_usd`, `usage`, `num_turns`, `duration_ms`) —
AI-459 proved this for `../flox/run.py`'s single-turn harness, and this
harness threw it away at both spawn points (`_run_claude_agent`,
`_run_judge`). AI-442 ports that parsing (`_parse_meta`, mirroring
`../flox/run.py:81`) into both functions, which changes their return shape
from `(result, err)` to `(result, err, meta)`. `real_world.py` imports both
and picks up the port for free at the call-site level — its own two
call sites now unpack a 3-tuple, but it does not record cost in its
own output yet (out of PR 1's scope; a mechanical fix, not a feature).

Cost alone was not the sharpest instrument for Bill's actual thesis —
"the skill saves the search loop" — so PR 1 goes straight to
`--output-format stream-json --verbose` for the agent call (per-ticket
decision Q1: no `num_turns`-only phase first) and parses the event
stream for `tool_use` blocks, counting `flox search`/`flox show`
invocations specifically (via the Bash tool's `input.command`)
alongside the total tool-call count. The judge call stays on plain
`--output-format json` — it never calls a tool, so there is nothing to
stream-parse. The flag combination (does `stream-json` work headless
with `-p`? is `--verbose` required?) was verified with one live,
minimal `claude` call before any code was written; the captured
transcript lives in `samples/`, and its own README
documents exactly what that call confirmed.

### The verified anchor (Q2)

Efficiency is meaningless without an anchor — cost to reach *what*?
`flox activate -c "echo __ok__"` proves packages resolve and build; it
does not prove a declared service actually serves (the same gap
`--services` closes for real-world — see "…and activation doesn't tell
you the services serve" above). The binding decision on Q2: use the
stronger anchor for exactly the fixtures that declare one. A task's
own `checks` array is the signal — `has_services_section` plus a
`pins_<kind>` check (currently only `pins_postgres`, i.e.
`node-postgres`) means the fixture expects a service, and
`_probe_service` (a leaner, synthetic-local version of `real_world.py`'s
AI-447 probe — not imported from there, since `real_world.py` already
imports from this module and a reverse import would be circular) runs
the same `flox activate --start-services -c <polling script>`
technique to confirm real connectivity before crediting the rep as
verified. Every other fixture stays activation-only. Each rep records
which anchor applied (`verify_method`: `"activation"` or
`"services"`), so the aggregation never conflates a runtime-only pass
with a service-answers pass.

### Censoring (Q5: distributions, never a pooled scalar)

A rep can spend tokens and never reach the anchor — averaging "cost
to succeed" over reps that never succeeded would let a giving-up arm
look artificially cheap. Every rep ends in exactly one terminal
disposition:

| Disposition | Meaning | Feeds `verify_rate`? | Feeds cost/turns "to verify"? |
|---|---|---|---|
| `verified` | anchor reached | yes (numerator) | **yes** |
| `failed-verify` | activation/service ran, came back non-ok, or timed out | yes (denominator) | no — right-censored into `unverified_spend` |
| `unverifiable-env` | flox absent / harness error / `--skip-activation` | **dropped** | no |
| `agent-error` | `claude` call failed, no manifest produced | **dropped** | no |

`_efficiency_summary` (sibling to `_stats`) computes this per fixture,
reporting median + p25/p75 + `n` for turns, tool-calls (total /
`flox search` / `flox show`), tokens (output, cache-read), and cost —
**never** a single pooled number across fixtures (Q5: a trivial
fixture's short loop and a service fixture's long one would hide
exactly the contrast that makes a result credible). The
`test_decision_verification_*` test in `tests/test_run_floxify.py` is the
one to trust most: a giving-up arm (every rep `failed-verify`) must
produce `verify_rate = 0` and an EMPTY `cost_to_verify` (`n=0`), never
a deceptively low mean — confirmed by deliberately breaking the
censoring logic during development and watching that exact test catch it.

### Two arms (Q7)

`--arm {skills,baseline}` (default `skills`) selects whether
`--plugin-dir` is passed to `claude` — the ONLY difference between
arms. Both get the identical tool surface (`Bash Read Write Edit
Skill`); `baseline` simply has no skill to invoke, so `/floxify <dir>`
resolves to nothing and the model falls back to its own unassisted
judgment with the same tools. This is a DIFFERENT flag from the
pre-existing `--baseline` (the regression-diff file to compare
against) — that flag keeps its original meaning untouched; the naming
collision was flagged and deliberately avoided.

### First real signal — running a batch

```bash
# Five-fixture batch (Q3): three long search/verify loops
# (ruby/python-uv/node-postgres) + one native-linkage/pkg-group-
# pressure fixture (rust-cargo) + one negative control (go-mod: a
# single runtime, no services, no hook -- the loop should be short
# regardless of arm).
python3 run_floxify.py \
  --only ruby,python-uv,node-postgres,rust-cargo,go-mod \
  --arm skills --reps 8 --out results/floxify-skills-batch.json

python3 run_floxify.py \
  --only ruby,python-uv,node-postgres,rust-cargo,go-mod \
  --arm baseline --reps 8 --out results/synthetic-batch.json
```

n=8 per (fixture, arm) — above the AI-438 n≥5 floor, for a readable
IQR (Q4). Run the two arms interleaved in the same session window if
catalog drift matters (the live catalog moves; keep it fixed across
the pair you're comparing). Raw agent streams persist per rep under
`results/streams/<out-basename-without-extension>/<id>__<arm>__rep<N>__agent.jsonl`
— keyed to the summary file's own name, so a rep stays traceable back
to the exact run that produced it for forensics later.

The headline read: `verify_rate` roughly equal across arms (confirms
AI-435 — both arms *get there*), `turns_to_verify` / `tool_calls_to_verify`
materially lower on the skills arm for the three long-loop fixtures,
and ~0 delta on `go-mod`. That contrast — not a pooled scalar — is the
evidence for the axis.

### What PR 1 does not do

Cost/usage capture and the two-arm CLI machinery are additive and
zero-API to land — mocked stream fixtures (derived from the one real
captured sample) and RED-first tests for the parser, disposition
classification, censoring rules, and arm selection. The batch above is
a separate, paid, subscription-covered step that runs AFTER this
lands: PR 1 ships the instrument, not a measurement.

## Real-world tier: pinned OSS repos (`real_world.py`)

The synthetic fixtures above are small dirs vendored into this repo. The
real-world tier (`real_world.py` + `real-world.jsonl`) runs `/floxify` against **real
open-source repos**, which are too large to vendor and too heavy to
fully `flox activate`. It's a sibling harness that imports shared
machinery from `run_floxify.py` (`_run_claude_agent`, `_is_valid_toml`,
`_check_activation`, `_run_judge`, `_stats`, `_skill_identity`,
`DEFAULT_SKILL_DIR`, and — since AI-465 — the verify.py deterministic
leg: `_run_verify`, `_hard_verify_violations`,
`_advisory_verify_violations`, `_catalog_note`) rather than duplicating
it.

### What's different from synthetic

1. **Fixtures are cloned at a pinned SHA**, not copied from disk.
   `_clone_at_sha` tries three strategies in increasing order of cost: a
   direct fetch of the pinned commit (cheapest, but most hosts reject
   fetching an arbitrary SHA for public repos), a partial clone
   (`--filter=blob:none`, full commit graph with blobs deferred until
   checkout), and a full clone as a last resort. A clone failure is
   recorded as a per-entry error, not a crash.
2. **The primary check is structural conformance**, not full
   activation. Each `real-world.jsonl` entry declares `expected_runtimes`
   (regex patterns matched against `pkg-path` values, e.g.
   `ruby(_[0-9_]+)?`, `nodejs_24`) and `expected_services` (substring
   matched against `[services.*]` section headers, e.g. `postgres`,
   `redis`). `manifest_created`, `valid_toml`, and `no_abs_paths` are
   checked the same way as synthetic.
3. **Activation is opt-in and off by default** (`--activate`). These
   dev environments (Rails monoliths, pnpm/turbo monorepos) are too
   heavy to reliably activate in CI; when off, activation is recorded
   as `skipped` with a note, same as synthetic's advisory treatment.
4. **The LLM judge compares against a textual gold characterization**
   (registry `gold.runtimes` / `gold.services` / `gold.notes`), not a
   gold TOML file — there's no hand-tuned reference manifest for a repo
   the size of Sentry or Supabase. The rubric is conformance/
   idiomaticity-focused, not exact-match.
5. **Report-only — this tier never gates the build**, in any mode.
   There's no `--gate` flag.
6. **A cloned checkout can ship its own in-tree `.flox/`** (synthetic's
   vendored fixtures deliberately never do — "the skill creates it").
   `process_entry` strips it before the conversion task runs, so the
   skill starts from a clean slate rather than being anchored by — or
   refusing to overwrite — an existing env (AI-469: PostHog ships a
   git-tracked, hand-maintained `manifest.toml` at its pinned SHA, and
   one un-stripped rep scored that UPSTREAM manifest instead of the
   skill's own output). The upstream env is captured, not discarded:
   `had_upstream_flox`, `upstream_manifest` (full text), and
   `upstream_flox_files` (every path under `.flox/`) land in the per-rep
   result — a known-working answer worth comparing against this
   fixture's golden route, feeding a separate golden-vs-upstream
   adoption review rather than this harness's own scoring.

Everything else about the verify.py leg is the *same* as synthetic, not
different (AI-465): `process_entry` re-runs `detect.py` against the
cloned checkout, runs `verify.py` against the produced manifest, and
records the same `verify` block shape (`violations`, `hard_count`,
`advisory_count`, `catalog_checked`) that flows into `_stats`'
`verify_checked` / `verify_clean` / `verify_hard_violation_rate`. The
catalog sub-leg is tied to `--activate` (the real-world analogue of the synthetic tier
1's `--skip-activation`): live `flox show` calls only run when
`--activate` is set, and degrade to a clean skip if `flox` itself is
unavailable regardless (`verify.py`'s own `shutil.which` guard). The
confirmed-catalog note is handed to the real-world judge the same way
synthetic's `_judge` gets it — see "Phase 3c checker" above and "Why
verify.py is advisory in the harness"; this leg never gates real-world,
which has no `--gate` at all.

### What a structural pass does and does not tell you

A structural pass is a real but narrow signal. It tells you the produced
manifest **pins the right runtimes and wires the right services** — the
`pkg-path` values match the expected runtime patterns and a `[services.*]`
block exists for each expected service. That's it.

It does **not** tell you the manifest actually builds or activates, or
that the commands inside the `[hook]` / `[services.*]` blocks are valid.
A manifest can pin `ruby_4_0`, wire `[services.postgres]`, pass all
structural checks and score 5/5 from the judge, and still contain a hook
command that fails the moment you run it — because with activation off,
nothing runs it. **Use `--activate` to check that.**

The mastodon run is the worked example: its hook pins
`gem install bundler -v 4.0.13`, but bundler is a 2.x project — there is
no bundler 4.0.13, so that command would fail on activation. The 7/7
structural + 5/5 judge result never caught it, and that is exactly where
Ruby's real onboarding pain lives — in the hook commands, not the package
pins. This is deliberately **not** a structural hard-check: validating
that a hook command resolves is activation's job, not structural
conformance's. Reading a green structural row as "the environment works"
is the specific over-read this note exists to prevent.

### …and activation doesn't tell you the services serve (`--services`)

`--activate` closes one gap and reveals the next. It proves the packages
resolve and build — it never runs the `[services.*]` command. So the ladder
has three rungs, and each one looks green from the rung above:

| check | proves | blind to |
|---|---|---|
| `has_service_postgres` | a `[services.*]` section header exists | whether the command in it is valid |
| `flox activate` | the packages resolve and build | whether the service ever starts |
| **`--services`** | **the service starts and answers** | whether the app's queries succeed |

lemmy is the worked example, and it appeared the moment AI-449 closed the rung
above: with the service guard in place lemmy went to hard=PASS, judge 4/5,
activate=ok — a fully green row for an environment whose Postgres was still in
question. Before AI-449 the datastore was *missing*; after it, it was
*declared*. Both states pass `flox activate`. Only starting it separates them.

`--services` (opt-in, advisory, needs `--activate`) runs one
`flox activate --start-services -c <script>` per expected service, where the
script polls a connectivity probe. Two design notes worth knowing:

- **The postgres probe passes no host or port.** Bare `pg_isready` reads
  `PGHOST`/`PGPORT` from the environment — which is exactly what the manifest's
  own `[vars]` set. So it asserts the service is reachable *at the address this
  manifest advertises*, which is what catches plausible's shape: `DATABASE_URL`
  pointing at a datastore nothing serves.
- **Three outcomes, deliberately distinct.** `ok` (answered), `fail` (polled to
  exhaustion, nothing answered — a real verdict on the manifest), and `skipped`
  (flox absent, no probe for that kind, or flox itself errored). A harness
  problem must never be reported as a broken service; an unprobeable service
  (clickhouse) must never read as a failed one.

Services can only be started from *inside* an activation — `flox services
start` on an unactivated environment errors with "Cannot start services for an
environment that is not activated". The first version of this probe got that
wrong, passed its own unit tests, and only real `flox` caught it; the tests now
pin the actual contract.

### Golden reference manifests (`expected/<id>.toml`)

The registry's prose `gold` field characterizes the *right answer* in words.
`expected/<id>.toml` goes one better: a concrete, hand-curated,
per-package verified reference manifest for each registered repo (not
whole-manifest lock-tested — see the caveat below) — the manifest a
careful engineer would write after reading the repo in full. When one exists,
`_judge_real_world` passes it to the judge alongside the prose characterization, so
grading compares the produced manifest against a real target rather than a
description. It is an **idiomatic reference, not an exact-match target** — a
well-structured produced manifest may differ in layout, comments, or hook style
and still score 5/5.

These are distinct from `samples/mastodon-manifest.toml`, which is a
*representative capture of actual skill output* used by `tests/test_real_world.py` as a
regex-drift guard (bugs and all). The golden references are the *ideal*.

Each `<id>.toml` has a sibling `<id>-notes.md` recording provenance (every pin
traced to its source file), the catalog verification log (`flox show` / `flox
search` runs that confirmed each `pkg-path` and version), and skill-improvement
observations. The mastodon golden is the direct answer to the bundler over-read
above: where the live skill run emitted `gem install bundler -v 4.0.13` (a
nonexistent version), the golden just runs `bundle install` and lets the
bundler shipped with `ruby_4_0` resolve the lockfile — no hallucinated pin.

Captured so far: `mastodon`, `posthog`, `sentry`, `supabase`, `gitea`,
`plausible`, `lemmy`, `firefly-iii` — grounded + per-package verified.
Every `pkg-path` and version was confirmed individually via `flox show` /
`flox search`, and (AI-457, 2026-07-16) all eight are also
resolution-tested: `flox activate -c "echo __ok__"` against each
manifest in a throwaway directory on x86_64-linux, proving the whole
group actually locks and the `[hook]` prelude runs. That is NOT the same
as functionally tested — no real repo was checked out, so no gem/wheel
native build ever compiled and hook commands that touch project files
(`bundle install`, `composer install`, ...) fail on missing inputs by
design. Each golden passes its own registry entry's structural checks and
the deterministic golden lint (`tests/test_real_world_golden_lint.py`, AI-456/AI-457).

### Registry (`real-world.jsonl`)

One JSON object per line:

| Field | Meaning |
|-------|---------|
| `id` | Short identifier, e.g. `mastodon` |
| `repo_url` | Repo to clone |
| `sha` | Pinned commit (short SHA is fine — passed straight to `git checkout`) |
| `ecosystem` | Primary language, informational |
| `expected_runtimes` | `[{"name": ..., "pattern": ...}]` — `pattern` is matched as `` pkg-path = "<pattern>" `` |
| `expected_services` | `[{"name": ..., "disposition": ...}, ...]` — `name` matched via the shared name-or-command rule (AI-468); `disposition` is `expect-wired` or `deferred-ok` (AI-470, see below) |
| `gold` | `{"runtimes": ..., "services": ..., "notes": ...}` — textual characterization for the judge |
| `rubric` | Judge guidance specific to this repo |

### Per-service disposition (AI-470)

Each `expected_services` entry carries a `disposition`, answering: **does
a developer need this service running locally to develop against?**
(Bill's adjudication — the SDLC build/runtime split floxify may eventually
need is a larger surface, tracked separately as AI-475.)

- **`expect-wired`** (the default, and the only disposition every fixture
  but posthog uses) — the structural check requires an actual
  `[services.*]` match (via `matching_service_names`, AI-468). This is
  exactly the pre-AI-470 behavior; every existing fixture's expectations
  are unchanged.
- **`deferred-ok`** (posthog's `clickhouse`) — passes the structural
  check if the service is EITHER wired directly OR deferred WITH AN
  EXPLICIT MECHANISM: the manifest's `[hook]` genuinely invokes
  `docker-compose up`/`docker compose up` with `docker-compose` installed
  (verify.py's own `_manifest_wires_compose`, AI-466's carve-out against
  a repo merely *having* a compose file — reused here, not re-derived).
  Silently dropping a `deferred-ok` service (no wiring, no mechanism)
  still fails the check; `deferred-ok` widens what counts as satisfying
  the expectation, it does not make the expectation optional.

`has_service_<kind>` stays the result key regardless of disposition
(baseline compat — dashboards and prior results keep working). The
richer wired/deferred/missing breakdown behind it lands in each
per-rep result's `service_observed` field. The AI-447 connectivity
probe is unchanged and disposition-agnostic: it already only probes a
kind it finds genuinely wired via the same shared matching rule, which
is exactly "probe only when actually wired" regardless of what the
registry expected.

A bare string (`"postgres"`) is still accepted as shorthand for
`{"name": "postgres", "disposition": "expect-wired"}` — this keeps any
external tooling or ad-hoc registry edits from needing an immediate
schema migration, though the committed `real-world.jsonl` uses the explicit
dict form throughout.

### Current repos

| id | sha | ecosystem | expected runtimes | expected services | status |
|----|-----|-----------|-------------------|--------------------|--------|
| `mastodon` | `52e9ec7814fc` | ruby | ruby_4_0, nodejs_24 | postgres, redis | **run** + golden |
| `posthog` | `55525a19f353` | python | python3 (3.13), nodejs_24 | postgres, redis (expect-wired), clickhouse (deferred-ok) | **run** + golden |
| `sentry` | `68d439d41d66` | python | python3 (3.13), nodejs | postgres, redis | golden, run pending |
| `supabase` | `963182f58e91` | javascript | nodejs_22, deno | postgres | golden, run pending |
| `gitea` | `11363e2f0cd6` | go | go, nodejs | — (embedded sqlite) | golden, run pending |
| `plausible` | `d5af396464c2` | elixir | elixir, nodejs | postgres | golden, run pending |
| `lemmy` | `9311de3b662b` | rust | cargo, rustc | postgres | golden, run pending |
| `firefly-iii` | `a0d70228bc14` | php | php85, nodejs | mariadb, redis | golden, run pending |

All eight have a golden reference manifest under `expected/<id>.toml`
(+ `<id>-notes.md`); see "Golden reference manifests" above. The four original
repos (mastodon, posthog, sentry, supabase) cover Ruby / Python / JS+Deno; the
four added later (gitea, plausible, lemmy, firefly-iii) deliberately reach into
ecosystems the first set never touched — **Go, Elixir, Rust, and PHP** — each of
which surfaced its own idiom (Go cache env + pure-Go-vs-CGO SQLite; Elixir
bundling OTP with no separate erlang; Rust native deps read from Cargo.lock
`*-sys` crates; PHP's fixed-bundle interpreter where `ext-*` come from the
`phpNN` build, not `[install]`).

Only `mastodon` has been run end-to-end so far — it's a single Rails
app (tractable) and Ruby was the ecosystem flagged as highest-risk
going in. The rest register for future runs rather than gating a
baseline; run them individually with `--only <id>` when validating
those ecosystems. Every `gold` characterization was derived by cloning
each repo at its pinned SHA and reading its actual version files
(`.ruby-version`, `.nvmrc`, `pyproject.toml`, `go.mod`, `.tool-versions`,
`rust-toolchain.toml`, `composer.json`) and service manifests
(`docker-compose.yml`, `devservices/config.yml`, Makefile `docker run`
recipes) — not assumed from the ecosystem name — with every catalog
`pkg-path` and version confirmed via `flox show` / `flox search`.

### Mastodon result (initial baseline)

All 7 structural hard-checks passed and the judge scored 5/5: the
skill correctly pinned `ruby_4_0` (from `.ruby-version` 4.0.6) and
`nodejs_24` (from `.nvmrc` 24.18), and wired both `[services.postgres]`
and `[services.redis]`.

But read that result through the "does and does not tell you" note
above. The hook pins `gem install bundler -v 4.0.13` — a **nonexistent
bundler version** (bundler is 2.x). The 7/7 structural + 5/5 judge run
did not catch it, because activation was off and nothing ever ran the
hook. A structural pass confirms the runtimes and services are right; it
says nothing about whether the hook commands work. So this is a genuine
positive for Ruby's *package/service resolution* — contrary to the
assumption that Ruby is a weak ecosystem going in — but it is emphatically
not a clean bill of health for Ruby onboarding. The real pain is in the
hook (the invalid bundler pin), which only an `--activate` run would
surface, and this is one repo at one commit; PostHog/Sentry/Supabase
remain unrun.

### Run

```bash
# Single repo (validated so far):
python3 real_world.py --only mastodon

# All registered repos (heavy — large clones + long skill runs):
python3 real_world.py

# Opt in to activation verification:
python3 real_world.py --only mastodon --activate

# Custom timeouts (defaults: 900s clone, 1800s agent run):
python3 real_world.py --only sentry --clone-timeout 1200 --agent-timeout 2400

# Custom skill dir / output path (same conventions as run_floxify.py):
python3 real_world.py --skill-dir /path/to/flox-plugin --out results/my-run.json
```

Results land in `results/real-world.json` by default (gitignored). One run's
output is committed as `baselines/real-world.json`, but nothing reads it:
unlike synthetic, `real_world.py` has no `--baseline` flag and no regression
diff — with only one of four repos run so far, a diff isn't meaningful. So that
file is a snapshot to compare against by hand, not a baseline the harness
enforces. Wire one up once more repos have a run to compare against.

### Unit tests

`tests/test_real_world.py` covers the deterministic, unit-testable pieces
(structural-conformance regexes, registry loading, the clone-at-SHA
fallback chain) with `unittest` + mocked `subprocess`/clone-strategy
calls. The agentic skill run and LLM judge call are integration-only,
same as synthetic — exercised by an actual `--only <id>` run, not unit
tests.

```bash
python3 -m unittest tests.test_real_world -v
```

### CI

Not wired into `.github/workflows/evals.yml` yet. Like synthetic's
`floxify-evals` job, real-world needs a live `flox` + network + Claude
credentials and is too slow for per-PR gating; unlike synthetic, it isn't
on the weekly schedule either — these are large repos and 4 full runs
would be expensive on every scheduled tick. Run manually via `--only`
per repo until there's a cheaper subset or a case for scheduling it.

## Stretch tier: known-hard & conversion-mode fixtures (`stretch.jsonl` — report-only)

The stretch tier (AI-431) adds the fixtures where the skill is *expected* to
struggle, plus one per dedicated conversion mode. Its whole purpose is
**trend visibility, not a pass/fail bar** — it is tracked and reported but
**never gates the build, in any mode**. It reuses the synthetic runner
(`run_floxify.py`) verbatim — same synthetic `fixtures/<id>/` + `expected/<id>.toml`
layout, same hard-checks, same judge — driven by a separate registry so it
stays out of the default/weekly gated run:

```bash
# Report-only run (NO --gate — every entry is stretch-tier):
python3 run_floxify.py --tasks stretch.jsonl

# One fixture:
python3 run_floxify.py --tasks stretch.jsonl --only ruby-native-gems
```

### Why it never gates (structural, not a flag)

`run_floxify.py`'s `--gate` binds **only `should`-tier** tasks
(`binding = [r for r in scored if r["tier"] == "should"]`). Every stretch
entry is **`stretch`**, so none of them can ever bind the gate — the tier
is report-only by construction, and `by_tier.stretch` in the run summary is
where its hard-pass rate and judge score land for trend-watching. (Passing
`--gate` with only `stretch.jsonl` is a vacuous-gate error by design — there
are no should-tier tasks to gate; run it without `--gate`.) There is no
committed baseline and no rate-refresh batch, same posture as real-world:
record one once the skill has a run worth diffing against.

### Fixtures

| id | class | files | what the skill must handle |
|----|-------|-------|----------------------------|
| `ruby-native-gems` | known-hard | `Gemfile`, `Gemfile.lock`, `.ruby-version`, `Rakefile` | Ruby 3.3.4 + native-extension gems: nokogiri (libxml2/libxslt), pg (libpq), `unset CPATH`, `$GEM_HOME`/`BUNDLE_PATH` vendoring under `$FLOX_ENV_CACHE` |
| `mixed-monorepo` | known-hard | `services/{web,api,worker}` + `pnpm-workspace.yaml` | Phase-1 multi-ecosystem detection: Node 20 (pnpm) + Python 3.12 (uv) + Go 1.22, all three at once |
| `devbox-convert` | conversion-mode | `devbox.json`, `package.json` | DevBox → Flox: `nodejs@20`/`python@3.12`/`jq@latest` map ~1:1; `env`→`[vars]`, `init_hook`→`[hook]` |
| `mise-convert` | conversion-mode | `.mise.toml`, `README.md` | Mise `[tools]` → Flox: node/python/go/terraform independent resolutions; patch pins may sit ahead of the catalog |
| `brewfile-convert` | conversion-mode | `Brewfile`, `README.md` | Brewfile → Flox: `brew "…"` → catalog (`awscli`→`awscli2`); `tap` dropped; Brewfile left in place |
| `devcontainer-convert` | conversion-mode | `.devcontainer/devcontainer.json`, `requirements.txt`, `package.json` | Dev Container full conversion: runtimes from image (`python:3.12`) **and** feature (`node:20`); `containerEnv`→`[vars]`, `postCreateCommand`→`[hook]` (pip into a `$FLOX_ENV_CACHE` venv) |

Same "fixtures ship no `.flox/`" discipline as synthetic — the skill writes it.

### Gold manifests

`expected/<id>.toml` — hand-authored, and **every catalog claim
live-verified via `flox show` on 2026-07-21** (nixpkgs) per the skill's
Phase-2 reading discipline: each `pkg-path` resolves, and the
whole manifest co-resolves (`flox list -c`) on the three declared systems
`["x86_64-linux", "aarch64-linux", "aarch64-darwin"]`. `x86_64-darwin` is
dropped from `[options].systems` because several of these packages
(python3, uv, jq, ripgrep, terraform, …) currently have no `x86_64-darwin`
build in the catalog — the same drop the real-world goldens make. No fixture
was skipped: all six goldens authored and verified clean.

### Deterministic gates (the only things that DO gate — via `python3 -m unittest`)

Two fast, no-`claude` test modules, mirroring the two-tier shape the rest
of this suite uses:

- **`tests/test_stretch.py`** — harness plumbing (pure stdlib, no network): the
  registry is well-formed, every entry is `stretch` (so the tier can never
  gate), ids don't collide with synthetic, every declared check is a real
  `run_floxify.CHECKS` key, and each id has a non-`.flox/` fixture + a
  parseable gold with `[install]`.

  ```bash
  python3 -m unittest tests.test_stretch -v
  ```

- **`tests/test_stretch_golden_lint.py`** — golden lint over the six stretch
  goldens: `verify.py`'s manifest-only checks (`[vars]` literalness,
  hook-mutation, catalog resolution) plus the whole-manifest lock leg,
  reusing `tests/test_real_world_golden_lint.py`'s `_attempt_lock` and sharing its
  `FLOXIFY_GOLDEN_LINT_LIVE_CATALOG` switch. Unlike the real-world goldens
  there is **no `KNOWN_VIOLATIONS` allowlist** — a stretch gold must be
  clean.

  ```bash
  python3 -m unittest tests.test_stretch_golden_lint -v
  FLOXIFY_GOLDEN_LINT_LIVE_CATALOG=0 python3 -m unittest tests.test_stretch_golden_lint -v  # no network
  ```

The agentic outcome run (`run_floxify.py --tasks stretch.jsonl`) is
report-only and manual, exactly like synthetic's `floxify-evals` job and
real-world's `--only` runs — it needs live `flox` + network + Claude
credentials and is never a per-PR gate.
