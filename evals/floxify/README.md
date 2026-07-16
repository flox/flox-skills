# Flox /floxify skill evals

Outcome-based eval suite for the `/floxify` skill. Unlike `../run.py`
(which scores text answers), this harness copies a synthetic fixture repo
to a temp dir, runs the `/floxify` skill headlessly, and scores the
`.flox/env/manifest.toml` it produces.

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

- **`test_detect.py`** — fast, deterministic unit tests. Asserts the analyzer
  extracts the right facts from every `fixtures/` repo (Node from `.nvmrc`,
  Ruby + bundler from `Gemfile`/`Gemfile.lock`, `requires-python` from
  `pyproject.toml`, `pg` → postgres, deno from `deno.json` / an
  `edge-runtime` image, `config_coupled` compose services, …). Pure stdlib,
  no `claude` calls — safe to run anywhere and cheap enough to gate.

  ```bash
  python3 test_detect.py        # standalone; or: pytest test_detect.py
  ```

- **`detect_usage_eval.py`** — behavioral conformance. Runs a real,
  Phase-1-bounded `/floxify` against a fixture with a tool-call-visible stream
  and asserts the skill *actually invoked* `detect.py` (bonus signal: through
  `flox run`). Heavy and opt-in like Tier 2 — it spawns a `claude` agent — so
  it's manual/scheduled, never in the fast gate.

  ```bash
  python3 detect_usage_eval.py                 # default fixture (node-postgres)
  python3 detect_usage_eval.py --fixture ruby
  ```

`test_detect.py` proves the analyzer is *correct*; `detect_usage_eval.py`
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
- **The eval harness** (`run_floxify.py`) — re-scans each fixture and runs
  the checker against the produced manifest as its own deterministic leg,
  reported per-task and in the summary (`verify_checked`, `verify_clean`,
  `verify_hard_violation_rate`); advisory, same reason activation is
  advisory (see "Why verify.py is advisory in the harness" below). Its
  confirmed catalog resolution table is handed to the LLM judge so it
  stops grading catalog facts from memory (AI-451).
- **The goldens** (`testdata/gold/*.toml`) — linted by the same checker as a
  cheap unit-test-tier check (no `claude`, no agent); see `test_golden_lint.py`
  below.

Eval layers, same two-tier shape as `detect.py`'s:

- **`test_verify.py`** — fast, deterministic unit tests. Every invariant
  carries a positive test (fires on the real defect) and a negative test
  against a realistic manifest shape (proves it does NOT false-fire) — "a
  wrong invariant is worse than no invariant." Catalog checks are mocked at
  the `flox show` boundary (`_run_show_command`) so the whole suite runs
  with no network.

  ```bash
  python3 -m unittest test_verify -v
  ```

- **`test_golden_lint.py`** — runs the checker over every
  `testdata/gold/*.toml` reference. Two hand reviews (AI-455) found real
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
  (passes trivially, no network) without flox — set
  `FLOXIFY_GOLDEN_LINT_LIVE_CATALOG=0` to force that mode explicitly rather
  than relying on flox's ambient absence; its real teeth need the
  `golden-lint` CI job's live flox install (see workflow file), not the
  flox-less free-tests step.

  ```bash
  python3 -m unittest test_golden_lint -v
  FLOXIFY_GOLDEN_LINT_LIVE_CATALOG=0 python3 -m unittest test_golden_lint -v  # no network
  ```

  **Caveat:** the LLM judge (in `run_floxify.py` and `tier2.py`) grades
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
4. Scores it with deterministic hard-checks + an LLM judge (vs `gold/`)
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

Each task in `tasks.jsonl` lists which checks apply via the `checks` array.
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

The judge compares the produced manifest against `gold/<id>.toml` and
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

# Skip activation (no flox install / network):
python3 run_floxify.py --skip-activation

# Custom skill dir (skill ships in-repo at flox-plugin/; override if needed):
python3 run_floxify.py --skill-dir /path/to/flox-plugin

# Custom output path:
python3 run_floxify.py --out results/my-run.json

# Diff against a specific committed baseline (default: floxify-baseline.json):
python3 run_floxify.py --baseline floxify-baseline.json
```

Results land in `results/` as JSON with a summary (hard-pass rate,
avg judge score, activation counts). Pure stdlib — no pip install needed.

## Regression detection

After each run the harness diffs the results against the committed
baseline (`results/floxify-baseline.json` by default, override with
`--baseline`) and prints a **regression diff**:

- **hard-check regressions** — a fixture that passed all hard-checks in
  the baseline but fails now (the signal that matters; these are the
  gate-binding checks)
- **hard-check fixes** — the reverse
- **new / removed fixtures** — fixtures added to or dropped from the suite
- **judge-score delta** — advisory only; the judge is noisy run-to-run

When a run writes to the same file it compares against (e.g.
`--out results/floxify-baseline.json`), the baseline is snapshotted
*before* the write, so re-recording the baseline still shows a
meaningful diff against the prior committed version.

To refresh the baseline after an intentional skill change, run the full
suite with `--out results/floxify-baseline.json` and commit the result.

## Prerequisites

1. **`claude` CLI** in `PATH`, logged in (`claude auth login` or
   `ANTHROPIC_API_KEY` set)
2. **The floxify skill** — ships in this repo at
   `flox-plugin/skills/floxify/`, no separate checkout needed. The
   default `--skill-dir` resolves to `flox-plugin/` two levels up from
   this file. Override with `--skill-dir` to point at an alternate
   flox-plugin directory.
3. **`flox` CLI** (optional) — needed for activation checks. Without it,
   activation is recorded as skipped.

## Gate policy

- **Binding (deterministic):** every `should`-tier fixture must pass all
  hard-checks listed in its `tasks.jsonl` entry. A failure exits non-zero
  under `--gate`.
- **Advisory (reported, never blocks):** `avg_judge_score`,
  `judge_correct_rate`, `activation_ok`. These track quality trends —
  watch for a sustained drop, but a single noisy run should not block.

`may` and `stretch` tiers do not exist in this suite yet (all 6 fixtures
are `should`). Add them when non-blocking exploratory fixtures are needed.

## CI: scheduled/manual, NOT per-PR

Unlike the text-answer `skills` eval (`evals/run.py`), which gates on
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

**Exception: `golden-lint`.** The golden-manifest lint (`test_golden_lint.py`,
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

`gold/<id>.toml` — hand-tuned reference manifests seeded from:

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
2. Create `gold/<new-id>.toml` with the ideal manifest
3. Add a line to `tasks.jsonl` with `id`, `tier`, `ecosystem`,
   `checks`, `rubric`
4. Run `python3 run_floxify.py --only <new-id>` to verify end-to-end

## Baseline

`results/floxify-baseline.json` — recorded against the in-repo
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

## Tier 2: real OSS conversion repos (`tier2.py`)

Tier 1 fixtures above are small synthetic dirs vendored into this repo.
Tier 2 (`tier2.py` + `tier2.jsonl`) runs `/floxify` against **real
open-source repos**, which are too large to vendor and too heavy to
fully `flox activate`. It's a sibling harness that imports shared
machinery from `run_floxify.py` (`_run_claude_agent`, `_is_valid_toml`,
`_check_activation`, `_run_judge`, `_stats`, `_skill_identity`,
`DEFAULT_SKILL_DIR`) rather than duplicating it.

### What's different from Tier 1

1. **Fixtures are cloned at a pinned SHA**, not copied from disk.
   `_clone_at_sha` tries three strategies in increasing order of cost: a
   direct fetch of the pinned commit (cheapest, but most hosts reject
   fetching an arbitrary SHA for public repos), a partial clone
   (`--filter=blob:none`, full commit graph with blobs deferred until
   checkout), and a full clone as a last resort. A clone failure is
   recorded as a per-entry error, not a crash.
2. **The primary check is structural conformance**, not full
   activation. Each `tier2.jsonl` entry declares `expected_runtimes`
   (regex patterns matched against `pkg-path` values, e.g.
   `ruby(_[0-9_]+)?`, `nodejs_24`) and `expected_services` (substring
   matched against `[services.*]` section headers, e.g. `postgres`,
   `redis`). `manifest_created`, `valid_toml`, and `no_abs_paths` are
   checked the same way as Tier 1.
3. **Activation is opt-in and off by default** (`--activate`). These
   dev environments (Rails monoliths, pnpm/turbo monorepos) are too
   heavy to reliably activate in CI; when off, activation is recorded
   as `skipped` with a note, same as Tier 1's advisory treatment.
4. **The LLM judge compares against a textual gold characterization**
   (registry `gold.runtimes` / `gold.services` / `gold.notes`), not a
   gold TOML file — there's no hand-tuned reference manifest for a repo
   the size of Sentry or Supabase. The rubric is conformance/
   idiomaticity-focused, not exact-match.
5. **Report-only — this tier never gates the build**, in any mode.
   There's no `--gate` flag.

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

### Golden reference manifests (`testdata/gold/<id>.toml`)

The registry's prose `gold` field characterizes the *right answer* in words.
`testdata/gold/<id>.toml` goes one better: a concrete, hand-curated,
catalog-verified reference manifest for each registered repo — the manifest a
careful engineer would write after reading the repo in full. When one exists,
`_judge_tier2` passes it to the judge alongside the prose characterization, so
grading compares the produced manifest against a real target rather than a
description. It is an **idiomatic reference, not an exact-match target** — a
well-structured produced manifest may differ in layout, comments, or hook style
and still score 5/5.

These are distinct from `testdata/mastodon-manifest.toml`, which is a
*representative capture of actual skill output* used by `test_tier2.py` as a
regex-drift guard (bugs and all). The golden references are the *ideal*.

Each `<id>.toml` has a sibling `<id>-notes.md` recording provenance (every pin
traced to its source file), the catalog verification log (`flox show` / `flox
search` runs that confirmed each `pkg-path` and version), and skill-improvement
observations. The mastodon golden is the direct answer to the bundler over-read
above: where the live skill run emitted `gem install bundler -v 4.0.13` (a
nonexistent version), the golden just runs `bundle install` and lets the
bundler shipped with `ruby_4_0` resolve the lockfile — no hallucinated pin.

Captured so far: `mastodon`, `posthog`, `sentry`, `supabase`, `gitea`,
`plausible`, `lemmy`, `firefly-iii` — grounded + catalog-verified, not
activation-tested (these dev envs are too heavy to activate on the recording
machine; every `pkg-path` and version was confirmed via `flox show` /
`flox search`). Each golden passes its own registry entry's structural checks.

### Registry (`tier2.jsonl`)

One JSON object per line:

| Field | Meaning |
|-------|---------|
| `id` | Short identifier, e.g. `mastodon` |
| `repo_url` | Repo to clone |
| `sha` | Pinned commit (short SHA is fine — passed straight to `git checkout`) |
| `ecosystem` | Primary language, informational |
| `expected_runtimes` | `[{"name": ..., "pattern": ...}]` — `pattern` is matched as `` pkg-path = "<pattern>" `` |
| `expected_services` | `["postgres", "redis", ...]` — substring-matched against `[services.*]` headers |
| `gold` | `{"runtimes": ..., "services": ..., "notes": ...}` — textual characterization for the judge |
| `rubric` | Judge guidance specific to this repo |

### Current repos

| id | sha | ecosystem | expected runtimes | expected services | status |
|----|-----|-----------|-------------------|--------------------|--------|
| `mastodon` | `52e9ec7814fc` | ruby | ruby, nodejs_24 | postgres, redis | **run** + golden |
| `posthog` | `55525a19f353` | python | python3 (3.13), nodejs_24 | postgres, redis | golden, run pending |
| `sentry` | `68d439d41d66` | python | python3 (3.13), nodejs | postgres, redis | golden, run pending |
| `supabase` | `963182f58e91` | javascript | nodejs_22, deno | postgres | golden, run pending |
| `gitea` | `11363e2f0cd6` | go | go, nodejs | — (embedded sqlite) | golden, run pending |
| `plausible` | `d5af396464c2` | elixir | elixir, nodejs | postgres | golden, run pending |
| `lemmy` | `9311de3b662b` | rust | cargo, rustc | postgres | golden, run pending |
| `firefly-iii` | `a0d70228bc14` | php | php85, nodejs | mariadb, redis | golden, run pending |

All eight have a golden reference manifest under `testdata/gold/<id>.toml`
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
python3 tier2.py --only mastodon

# All registered repos (heavy — large clones + long skill runs):
python3 tier2.py

# Opt in to activation verification:
python3 tier2.py --only mastodon --activate

# Custom timeouts (defaults: 900s clone, 1800s agent run):
python3 tier2.py --only sentry --clone-timeout 1200 --agent-timeout 2400

# Custom skill dir / output path (same conventions as run_floxify.py):
python3 tier2.py --skill-dir /path/to/flox-plugin --out results/my-run.json
```

Results land in `results/tier2.json` by default. Unlike Tier 1, there's
no committed baseline or regression diff yet — with only one of four
repos run so far, a diff isn't meaningful. Add one once more repos have
a run to compare against.

### Unit tests

`test_tier2.py` covers the deterministic, unit-testable pieces
(structural-conformance regexes, registry loading, the clone-at-SHA
fallback chain) with `unittest` + mocked `subprocess`/clone-strategy
calls. The agentic skill run and LLM judge call are integration-only,
same as Tier 1 — exercised by an actual `--only <id>` run, not unit
tests.

```bash
python3 -m unittest test_tier2 -v
```

### CI

Not wired into `.github/workflows/evals.yml` yet. Like Tier 1's
`floxify-evals` job, Tier 2 needs a live `flox` + network + Claude
credentials and is too slow for per-PR gating; unlike Tier 1, it isn't
on the weekly schedule either — these are large repos and 4 full runs
would be expensive on every scheduled tick. Run manually via `--only`
per repo until there's a cheaper subset or a case for scheduling it.
