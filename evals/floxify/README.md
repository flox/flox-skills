# `/floxify` evals

Outcome evals for the `/floxify` skill. Where [`../flox/`](../README.md#the-flox-suite)
scores text answers, this suite scores **what the skill produced**: it points
`/floxify` at a repository, then grades the `.flox/env/manifest.toml` that comes
out.

**Enter the Flox environment first** — see
[the runtime note in `../README.md`](../README.md#run-this-first-flox-activate).
`python3` and `claude` both come from this repo's own environment.

```bash
flox activate
cd evals/floxify        # this suite's root; run every command below from here
```

Note the two senses of "activate" in this file. Your own activation is how you
get a Python and a `claude`. The **activation check** below is the harness
activating a manifest the skill produced, in a temp dir, as a scored outcome.
They are unrelated.

## What it measures

Three registries, three levels of realism, three gate policies.

| Registry | Runner | Input | Gate policy |
|---|---|---|---|
| `synthetic.jsonl` (7) | `run_floxify.py` | Small fixture repos vendored under `fixtures/` | `--gate` binds on `should`-tier deterministic checks |
| `stretch.jsonl` (6) | `run_floxify.py --tasks stretch.jsonl` | Known-hard and conversion-mode fixtures under `fixtures/` | never gates — every entry is `stretch`-tier |
| `real-world.jsonl` (8) | `real_world.py` | Real OSS repos cloned at a pinned SHA | never gates — the runner has no `--gate` flag |

Alongside the outcome runs, this suite owns the evals for the two deterministic
scripts the skill itself bundles: `detect.py` (grounds the skill's input) and
`verify.py` (grounds its output). Those are covered under
[The skill's own scripts](#the-skills-own-scripts).

## How a run works

For each entry, the runner:

1. Stages the repo — copies the fixture to a temp dir (synthetic and stretch),
   or clones at the pinned SHA (real-world). A cloned checkout that ships its
   own in-tree `.flox/` has it stripped, so the skill starts from a clean slate
   instead of scoring an upstream manifest. The upstream env is captured in the
   result (`had_upstream_flox`, `upstream_manifest`, `upstream_flox_files`), not
   discarded. Vendored fixtures deliberately ship no `.flox/` at all — the skill
   creates it.
2. Runs `claude /floxify <dir>` headlessly with the skill loaded.
3. Reads `.flox/env/manifest.toml` from the staged directory.
4. Scores it with deterministic checks, re-runs `verify.py` against it, and
   asks an LLM judge to grade it against the reference in `expected/`.
5. Optionally attempts `flox activate`, and optionally probes declared services
   for real connectivity.

## Deterministic vs probabilistic

The checks are deterministic functions, but the manifest they inspect comes from
a non-deterministic agent run. So a single outcome run reports a **rate**, not a
state — this is why almost everything here is advisory and why the only things
that gate per-PR are the unit tests and the golden lints, which never spawn an
agent.

**Deterministic checks** (`hard_checks` in the JSON; these bind `--gate` on
`should`-tier synthetic fixtures):

| Check | Verifies |
|---|---|
| `manifest_created` | `.flox/env/manifest.toml` was written |
| `valid_toml` | the file parses as TOML |
| `has_install_section` | `[install]` is present |
| `has_services_section` | a `[services.*]` section is present |
| `no_abs_paths` | no `/home/`, `/Users/` etc. in manifest values |
| `no_fake_install_url` | no hallucinated Flox install URL |
| `pins_node_20`, `pins_python`, `pins_go`, `pins_rust`, `pins_ruby`, `pins_postgres` | the manifest names the specific expected package, not a generic one |

Each registry entry lists which checks apply in its `checks` array; a check not
listed for an entry is not evaluated. Definitions are in `CHECKS` in
[`run_floxify.py`](run_floxify.py).

The real-world tier's primary check is **structural conformance** instead: each
entry declares `expected_runtimes` (regex patterns matched against `pkg-path`
values) and `expected_services` (matched against `[services.*]` section
headers), plus the shared `manifest_created` / `valid_toml` / `no_abs_paths`.

**Advisory** (reported, never blocks): `avg_judge_score`, `judge_correct_rate`,
`activation_ok`, and `verify_hard_violation_rate`. Watch these for a sustained
trend rather than reacting to a single run.

### What a structural pass does not tell you

A structural pass says the manifest **pins the right runtimes and wires the
right services**. That is all. It does not say the manifest builds or activates,
or that the commands inside `[hook]` / `[services.*]` are valid — with
activation off, nothing runs them. A manifest can pin the right Ruby, wire
`[services.postgres]`, pass every structural check, score 5/5 from the judge,
and still contain a hook command that fails the moment you run it. Use
`--activate` to check that.

The three rungs, each of which looks green from the rung above:

| Check | Proves | Blind to |
|---|---|---|
| `has_services_section` | a `[services.*]` header exists | whether the command in it is valid |
| `flox activate` | the packages resolve and build | whether the service ever starts |
| `--services` | the service starts and answers | whether the app's queries succeed |

Validating that a hook command resolves is activation's job, not structural
conformance's. Reading a green structural row as "the environment works" is the
specific over-read this note exists to prevent.

## Run

### Synthetic fixtures

```bash
python3 run_floxify.py --only node-20              # one fixture (fastest inner loop)
python3 run_floxify.py --only ruby,python-uv       # several, comma-separated
python3 run_floxify.py                             # the whole registry
python3 run_floxify.py --gate                      # exit non-zero on a should-tier failure
python3 run_floxify.py --skip-activation           # no flox activate attempt
python3 run_floxify.py --concurrency 2             # parallel claude calls (default 2)
python3 run_floxify.py --reps 5                    # repetitions per fixture
python3 run_floxify.py --arm baseline              # omit --plugin-dir: the unassisted model
python3 run_floxify.py --skill-dir /path/to/flox-plugin
python3 run_floxify.py --out my-run.json           # under results/
python3 run_floxify.py --baseline synthetic.json   # comparison point, under baselines/
```

`--arm` and `--baseline` are unrelated flags despite the shared word: `--arm`
selects whether the skill is loaded, `--baseline` names the committed file to
diff against.

### Stretch fixtures

```bash
python3 run_floxify.py --tasks stretch.jsonl
python3 run_floxify.py --tasks stretch.jsonl --only ruby-native-gems
```

Same runner, same fixture and `expected/` layout, same checks — a separate
registry so it stays out of the default run. It can never gate: `--gate` binds
only `should`-tier entries and every stretch entry is `stretch`-tier. Passing
`--gate` with only `stretch.jsonl` is rejected as a vacuous gate rather than
reported as a pass. Its numbers land in `by_tier.stretch` for trend-watching.

### Real-world repos

```bash
python3 real_world.py --only mastodon              # one repo
python3 real_world.py                              # all registered repos (heavy)
python3 real_world.py --only mastodon --activate   # opt in to activation
python3 real_world.py --only mastodon --activate --services  # + connectivity probe
python3 real_world.py --only sentry --clone-timeout 1200 --agent-timeout 2400
python3 real_world.py --reps 5
```

Activation is **off by default** here — these dev environments (Rails monoliths,
pnpm/turbo monorepos) are too heavy to reliably activate — and is recorded as
`skipped` when off. Clones try three strategies in increasing order of cost: a
direct fetch of the pinned commit, a partial clone (`--filter=blob:none`), then
a full clone. A clone failure is recorded as a per-entry error, not a crash.
Default timeouts: 900s per clone attempt, 1800s per skill run, 1800s per
activation.

`--services` (advisory, needs `--activate`) runs one
`flox activate --start-services -c <script>` per expected service, polling a
connectivity probe. It has three deliberately distinct outcomes: `ok`
(answered), `fail` (polled to exhaustion — a real verdict on the manifest), and
`skipped` (flox absent, no probe for that service kind, or flox errored). A
harness problem must never be reported as a broken service, and an unprobeable
service must never read as a failed one. The postgres probe passes no host or
port on purpose: bare `pg_isready` reads `PGHOST`/`PGPORT` from the environment,
which is exactly what the manifest's own `[vars]` set, so it asserts the service
is reachable *at the address this manifest advertises*.

Services can only be started from *inside* an activation — `flox services start`
on an unactivated environment errors out.

### Unit tests

```bash
python3 -m unittest discover -s tests -t . -v   # the whole suite
python3 -m unittest tests.test_verify -v        # one module
python3 tests/test_detect.py                    # also runs standalone
```

Pure stdlib, no `claude`, no network, no credentials. Subprocess boundaries
(`flox show`, clone strategies) are mocked. This is the suite that runs on every
PR.

## Output

Results land in `results/` as JSON with a per-entry record and a summary
(hard-pass rate, avg judge score, activation counts, `verify_checked` /
`verify_clean` / `verify_hard_violation_rate`). `results/` is **gitignored**;
committed comparison points live in `baselines/`, which the runners read and
never write.

Raw per-rep agent streams persist under
`results/streams/<out-basename>/<id>__<arm>__rep<N>__agent.jsonl`, keyed to the
summary file's own name so a rep stays traceable back to the run that produced
it.

### Regression diff

After a synthetic run the harness diffs against the committed baseline
(`baselines/synthetic.json` by default, override with `--baseline`) and prints:

- **deterministic-check regressions** — a fixture that passed in the baseline
  and fails now. This is the signal that matters.
- **deterministic-check fixes** — the reverse.
- **new / removed fixtures**.
- **judge-score delta** — advisory; the judge is noisy run-to-run.

To refresh a baseline after an intentional skill change, run the suite and copy
its output over — a deliberate, reviewable act rather than a side effect:

```bash
python3 run_floxify.py --out refresh.json
cp results/refresh.json baselines/synthetic.json
```

`summary.skill` records a portable identity (`<dir-name>@<branch>`) rather than
an absolute host path, and `summary.model` records the pinned model, so a
committed baseline stays reproducible across machines. Where activation was not
available when the baseline was recorded, it is stored as `"skipped": true` with
a note; deterministic checks and judge scores are still populated.

The real-world tier has a committed `baselines/real-world.json` but no
regression diff — too few repos have been run for a diff to mean anything yet.

## Where things live

| Path | What |
|---|---|
| `synthetic.jsonl`, `stretch.jsonl`, `real-world.jsonl` | The three registries |
| `fixtures/<id>/` | Input repos, shipping no `.flox/` |
| `expected/<id>.toml` | Reference manifest for the judge |
| `expected/<id>-notes.md` | Provenance for a real-world reference: every pin traced to its source file, plus the `flox show` / `flox search` log that confirmed it |
| `samples/` | Captured agent stream transcripts and one real run's manifest, parsed by tests. See [`samples/README.md`](samples/README.md) for how each was captured |
| `baselines/` | `synthetic.json`, `real-world.json` — read, never written by a run |
| `results/` | Generated output, **gitignored** |
| `tests/` | Deterministic unit tests |

`expected/*.toml` are **references, not byte-exact match targets**. A
well-structured manifest that differs in layout, comments, or hook style can
still score 5/5. They are hand-curated and per-package verified: every
`pkg-path` and version confirmed via `flox show` / `flox search`, and the whole
manifest lock-tested so the group actually co-resolves. That is not the same as
functionally tested — no real repo is checked out, so no native gem or wheel
ever compiles, and hook commands that touch project files (`bundle install`,
`composer install`) fail on missing inputs by design.

`samples/mastodon-manifest.toml` is distinct from the references: it is a
representative capture of **actual skill output**, bugs and all, used by
`tests/test_real_world.py` as a regex-drift guard. The `expected/` files are the
ideal.

### Registry fields

`synthetic.jsonl` / `stretch.jsonl`: `id`, `tier`, `ecosystem`, `checks`,
`rubric` (stretch entries also carry a `class` — `known-hard` or
`conversion-mode`).

`real-world.jsonl`:

| Field | Meaning |
|---|---|
| `id` | Short identifier, e.g. `mastodon` |
| `repo_url` | Repo to clone |
| `sha` | Pinned commit (short SHA is fine) |
| `ecosystem` | Primary language, informational |
| `expected_runtimes` | `[{"name": ..., "pattern": ...}]` — `pattern` matched as `` pkg-path = "<pattern>" `` |
| `expected_services` | `[{"name": ..., "disposition": ...}]` — see below |
| `gold` | `{"runtimes": ..., "services": ..., "notes": ...}` — textual characterization for the judge |
| `rubric` | Judge guidance specific to this repo |

Each `expected_services` entry carries a **disposition**, answering: does a
developer need this service running locally to develop against?

- **`expect-wired`** (the default) — the structural check requires an actual
  `[services.*]` match.
- **`deferred-ok`** — also satisfied by deferring the service **with an explicit
  mechanism**: the manifest's `[hook]` genuinely invokes `docker-compose up` /
  `docker compose up` with `docker-compose` installed. Silently dropping the
  service still fails. `deferred-ok` widens what satisfies the expectation; it
  does not make the expectation optional.

`has_service_<kind>` stays the result key either way; the wired/deferred/missing
breakdown lands in each per-rep result's `service_observed`. A bare string
(`"postgres"`) is accepted as shorthand for `{"name": "postgres", "disposition":
"expect-wired"}`, though the committed registry uses the explicit dict form.

## The skill's own scripts

The `/floxify` skill bundles two deterministic Python scripts. Both live under
`flox-plugin/skills/floxify/scripts/` and both are evaluated from here.

### `detect.py` — grounds the INPUT

Phase 1 runs the analyzer before the skill reads anything by hand. It scans the
repo and emits grounded JSON: runtime version pins (each tagged with the file it
came from), package-manager versions from lockfiles, docker-compose services
(with a `config_coupled` flag), service-client dependencies, and
monorepo/orchestrator markers. It never touches the catalog — mapping a detected
runtime to a `pkg-path` stays with the model via `flox search` / `flox show`, so
every `search_terms` value is a hint to verify, not an asserted package. The
skill invokes it through Flox, so it needs no system Python:

```bash
flox run -p python313 -- python3 "<skill-dir>/scripts/detect.py" "$TARGET_DIR"
```

Two eval layers:

- **`tests/test_detect.py`** — deterministic unit tests asserting the analyzer
  extracts the right facts from every fixture. Pure stdlib, no `claude`, cheap
  enough to gate.
- **`detect_usage_eval.py`** — behavioral conformance. Runs a real,
  Phase-1-bounded `/floxify` with a tool-call-visible stream and asserts the
  skill *actually invoked* `detect.py` (bonus signal: through `flox run`).
  Spawns an agent, so it is manual and never in the fast gate.

  ```bash
  python3 detect_usage_eval.py                 # default fixture (node-postgres)
  python3 detect_usage_eval.py --fixture ruby
  ```

One proves the analyzer is correct; the other proves the skill reaches for it.

### `verify.py` — grounds the OUTPUT

Takes `detect.py`'s facts plus a produced `manifest.toml` and reports concrete
violations: every detected runtime installed, every leaf-datastore client and
`[vars]` connection-string endpoint actually served, `[vars]` staying literal,
hooks never mutating the tracked git tree, and every `pkg-path` / `version` /
`systems` combination resolving in the live catalog (via `flox show`,
advisory-skipped when flox or the network is unavailable). The
native-build-input-with-no-`outputs` heuristic is advisory by design —
hard-failing a judgment call would reproduce the LLM judge's own failure mode in
Python. The skill runs it as Phase 3c Step 4 and blocks its report on any HARD
violation.

Three consumers, one checker:

- **The skill** — violations stop the flow; fix and re-run.
- **The eval harnesses** — `run_floxify.py` and `real_world.py` each re-scan
  their staged repo and run the checker against the produced manifest as a
  deterministic leg, reported per-entry and in the summary. Its confirmed
  catalog-resolution table is handed to the LLM judge so the judge stops grading
  catalog facts from memory.
- **The references** — `expected/*.toml` are linted by the same checker.

Eval layers:

- **`tests/test_verify.py`** — deterministic unit tests. Every invariant carries
  a positive test (fires on the real defect) *and* a negative test against a
  realistic manifest (proves it does not false-fire) — a wrong invariant is
  worse than no invariant. Catalog checks are mocked at the `flox show`
  boundary, so the suite runs with no network.
- **`tests/test_real_world_golden_lint.py`** — runs the checker over every
  real-world `expected/*.toml`, plus one `test_<fixture>_locks_cleanly` per
  reference that attempts a real `flox list -c` in a throwaway environment.
  `flox list -c` is resolution-only: it locks via a catalog-API resolve and
  never builds or fetches store paths.
- **`tests/test_stretch_golden_lint.py`** — the same lint over the six stretch
  references, reusing the real-world module's lock helper.
- **`verify_usage_eval.py`** — behavioral conformance, Phase-3-bounded: runs a
  real `/floxify` through package resolution and manifest-writing and asserts
  the skill *actually invoked* `verify.py`. Spawns an agent; manual only.

```bash
python3 -m unittest tests.test_verify -v
python3 -m unittest tests.test_real_world_golden_lint -v
python3 -m unittest tests.test_stretch_golden_lint -v
FLOXIFY_GOLDEN_LINT_LIVE_CATALOG=0 python3 -m unittest tests.test_real_world_golden_lint -v  # no network
python3 verify_usage_eval.py
```

`FLOXIFY_GOLDEN_LINT_LIVE_CATALOG=0` forces the offline mode explicitly rather
than relying on flox being absent — flox is always on PATH now, because it
supplies `python3`. The free per-PR CI step pins that switch to `0`; the
`golden-lint` job leaves it at its default, which is where the lint gets its
real teeth.

The real-world lint carries an explicit `KNOWN_VIOLATIONS` allowlist, one entry
per current reference defect, matched against a violation's structured
`pkg_path` field exactly rather than as a substring of the message. A dedicated
test asserts every allowlist entry still matches a live violation, so fixing a
reference without removing its entry cannot leave a stale slot absorbing a
future regression. The stretch lint has **no allowlist** — a stretch reference
must be clean.

A lock failure is classified before it is reported: a genuine resolver defect
(`resolution failed:`) is a real finding on that reference, while a catalog-API
communication error gets one retry and, if it persists, is reported as likely
transient rather than as a resolution defect.

### Why `verify.py` is advisory in the harness

The live skill hard-gates on these invariants, but the harness leg never fails
`--gate` on them — including the network-free ones that could gate reliably. Two
reasons:

1. **Evals report rates, not states.** The check is deterministic; the manifest
   being checked is not. `verify_hard_violation_rate` is tracked as a trend,
   the same treatment every other advisory metric here gets.
2. **There is no per-PR skill run to gate.** The jobs that would produce these
   manifests are dispatch-only, so binding the leg would protect no PR.

Watch `verify_hard_violation_rate` in the run summary and the per-entry `verify`
column for a sustained rise. That is the signal this leg exists to surface.

## CI

Full trigger and cost table: [`../README.md`](../README.md#what-binds-ci). In
short, for this suite:

| Job | Runs on | Anthropic spend | Blocks the build? |
|---|---|---|---|
| `evals` (free unit tests from `tests/`) | every PR | none | **yes** |
| `golden-lint` | PRs touching `evals/floxify/**` or `flox-plugin/skills/**`; plus dispatch | none — `flox show` / `flox list -c` only | **yes** |
| `floxify-evals` (synthetic, `--gate`) | dispatch only, `run_floxify=true` | yes | within that run only |
| `floxify-real-world` (report-only) | dispatch only, `run_floxify=true` | yes | never |

The outcome evals are dispatch-only because they need a live `flox`, a reachable
catalog, network, and Anthropic credentials — too slow and too
environment-dependent to gate a PR. `golden-lint` is the exception: it needs
flox and the catalog but spawns no `claude`, so it runs per-PR in its own job,
keeping the flox install off the critical path of the flox-less unit tests.
Neither stretch nor real-world is wired into CI at all; run them by hand.

Per-PR regression catching for this suite is therefore the **unit tests and the
golden lints**, not the outcome runs.

## Prerequisites

1. **`flox`** — the only thing you install by hand. It supplies `python3` and
   `claude` through this repo's environment, and the activation checks need it
   anyway.
2. **Credentials for `claude`** — a logged-in CLI or `ANTHROPIC_API_KEY`. The
   deterministic unit tests need neither.
3. **The skill** — ships in this repo at `flox-plugin/skills/floxify/`; no
   separate checkout. `--skill-dir` defaults to the in-repo `flox-plugin/` two
   levels up. Override it to score an alternate checkout.

Running an outcome eval in full also needs network access to the Flox catalog
(the skill runs `flox search` during conversion) and enough API budget: a full
synthetic run is 7 fixtures × (1 agent + 1 judge) = 14 Opus calls.

Without catalog access, the skill may still produce a manifest from its own
knowledge, but the deterministic checks will reflect whether it actually
searched; activation is skipped and recorded as such. Use `--skip-activation` to
suppress the attempt entirely.

## Adding or updating an eval

**A synthetic or stretch fixture:**

1. Create `fixtures/<new-id>/` with the project files and **no `.flox/`**.
2. Create `expected/<new-id>.toml` — the manifest a careful engineer would
   write. Verify every `pkg-path` and version with `flox show` / `flox search`,
   and confirm the whole manifest co-resolves.
3. Append one line to `synthetic.jsonl` (or `stretch.jsonl`) with `id`, `tier`,
   `ecosystem`, `checks`, and `rubric`. Every declared check must be a real key
   in `run_floxify.CHECKS`; `tests/test_stretch.py` enforces this for the
   stretch registry.
4. Run `python3 run_floxify.py --only <new-id>` to confirm it scores end to end.

**A real-world repo:** append a line to `real-world.jsonl` with the fields
above. Derive the `gold` characterization by cloning the repo at its pinned SHA
and reading its actual version files (`.ruby-version`, `.nvmrc`,
`pyproject.toml`, `go.mod`, `.tool-versions`, `rust-toolchain.toml`,
`composer.json`) and service manifests (`docker-compose.yml`, Makefile `docker
run` recipes) — never assumed from the ecosystem name. Add
`expected/<id>.toml` + `<id>-notes.md` recording provenance and the catalog
verification log.

**A new deterministic check:** add it to `CHECKS` in `run_floxify.py`, give it
positive and negative tests in `tests/test_run_floxify.py`, then name it in the
registry entries it applies to.

The repo-wide policy — every skill change ships with an eval, written RED first
— is in [`../README.md`](../README.md#policy-every-skill-change-ships-with-an-eval--written-red-first)
and applies here.
