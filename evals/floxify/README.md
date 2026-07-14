# Flox /floxify skill evals

Outcome-based eval suite for the `/floxify` skill. Unlike `../run.py`
(which scores text answers), this harness copies a synthetic fixture repo
to a temp dir, runs the `/floxify` skill headlessly, and scores the
`.flox/env/manifest.toml` it produces.

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

# Custom skill dir (if claude-plugins is not a sibling of flox-skills):
python3 run_floxify.py --skill-dir /path/to/claude-plugins

# Custom output path:
python3 run_floxify.py --out results/my-run.json
```

Results land in `results/` as JSON with a summary (hard-pass rate,
avg judge score, activation counts). Pure stdlib — no pip install needed.

## Prerequisites

1. **`claude` CLI** in `PATH`, logged in (`claude auth login` or
   `ANTHROPIC_API_KEY` set)
2. **claude-plugins repo** on the `add-floxify-skill` branch (or main,
   after the PR merges):

   ```bash
   git clone https://github.com/flox/claude-plugins \
     ../claude-plugins   # sibling to flox-skills
   cd ../claude-plugins
   git checkout add-floxify-skill   # or: git checkout main after merge
   ```

   The default `--skill-dir` is `../../../claude-plugins` relative to
   this file, resolving to the sibling directory. Override with
   `--skill-dir` if your layout differs.

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

`results/floxify-baseline.json` — recorded against the `add-floxify-skill`
PR branch. See the file for run conditions (skill-dir, model, activation
availability).

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
