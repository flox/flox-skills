# Host-matrix smoke test — design

Status: proposed (2026-07-27). Closes the three open verification items in
[AI-497](https://linear.app/floxdotdev/issue/AI-497/ship-floxflox-skills-10-public-repo-verified-announced):
Codex, OpenCode, and `npx skills add`.

## Goal

Prove the `flox` and `floxify` skills **load** and **trigger** in Codex,
OpenCode, and Claude Code (as control), across the install methods we
document — without mutating the developer's machine.

Everything runs inside a container built by `flox containerize` from a
dedicated Flox environment. Host CLIs, `npm`/`npx`, and every agent config
directory live and die inside that image. The only thing crossing the
boundary is a copy of two credential files, in a directory destroyed at the
end of the run.

## Non-goals

- **Measured trigger rates.** This is a smoke test: one attempt per cell,
  pass/fail. A pass proves the plumbing works; a fail proves it's broken.
  Neither measures reliability — that's the AI-435/AI-439 screening tier, and
  it belongs in its own issue if we want it for non-Claude hosts.
- **CI integration.** The matrix needs live credentials and spends tokens. It
  runs by hand, a handful of times, to close AI-497.
- **Non-Linux systems.** The image is `x86_64-linux`. Darwin cells would need
  a Mac runner.
- **The `pi` host.** Packaged by `flox-agent-layout.sh`, not in AI-497's scope.

## The matrix — 8 cells

| | native plugin | `npx skills add` | flox-ai + skills-flox |
|---|---|---|---|
| **Claude Code** | `claude plugin marketplace add flox/flox-skills` | ✓ | `flox-ai launch claude` |
| **Codex** | `codex plugin marketplace add .` (needs a clone in-image) | ✓ | `flox-ai launch codex` |
| **OpenCode** | — (no plugin-marketplace concept; README routes it via skills.sh) | ✓ | `flox-ai launch opencode` |

Claude Code is the control: it is known to work, so a Claude cell failing
means the harness is wrong, not the skill.

## Architecture

### 1. Two images, for isolation

A single image containing every install method would let a host discover the
skill through a path other than the one under test. So:

- **`base`** — the three host CLIs (`flox/claude-code`, `flox/codex`,
  `flox/opencode`), `nodejs` (for `npx`), `git`, `jq`. No skills present.
  Serves the *native plugin* and *npx* cells.
- **`withpkg`** — `base` plus `flox/flox-ai` and the published
  `flox/skills-flox@1.0.0`. Serves the *flox-ai* cells, and dogfoods the
  exact artifact consumers install.

Two standalone environments, `evals/hosts/base/.flox/` and
`evals/hosts/withpkg/.flox/`. `withpkg`'s manifest is `base`'s plus two
`[install]` lines (`flox/flox-ai`, `flox/skills-flox`). Composition via
`[include]` would avoid the duplication but adds a mechanism to debug for two
duplicated lines — not worth it here. Build:

```bash
flox containerize -d evals/hosts/base    --runtime docker -t flox-skills-hosts-base:<date>
flox containerize -d evals/hosts/withpkg --runtime docker -t flox-skills-hosts-withpkg:<date>
```

Flox containers auto-activate the environment, so `docker run <img> <cmd>`
behaves like `flox activate -- <cmd>`. Cells are plain commands.

### 2. Credentials — throwaway copies, never the originals

Per run, on the host:

```bash
RUNDIR=$(mktemp -d)                 # 0700 by default
mkdir -p "$RUNDIR/claude" "$RUNDIR/codex"
# ONLY the Claude subscription block — see "Credential minimization" below.
jq '{claudeAiOauth}' ~/.claude/.credentials.json > "$RUNDIR/claude/.credentials.json"
install -D -m600 ~/.codex/auth.json "$RUNDIR/codex/auth.json"
chmod 600 "$RUNDIR/claude/.credentials.json"
```

**Credential minimization — required, not optional.**
`~/.claude/.credentials.json` holds two top-level keys: `claudeAiOauth` (the
subscription login) and `mcpOAuth`, which on this machine carries live OAuth
tokens for Fellow, Linear, Notion, Slack, and Sentry. Copying the file
verbatim would hand every one of those to whatever runs in the container. The
runner copies `.claudeAiOauth` only, and asserts the written file has exactly
one top-level key before any cell starts. Nothing in this matrix needs MCP.

Both hosts authenticate by OAuth, not API keys — Codex's `auth.json` reports
`auth_mode: "chatgpt"`. So **no `ANTHROPIC_API_KEY`, no `OPENAI_API_KEY`**:
the copied files are the login. Runs draw on subscription rate limits rather
than per-token billing, and 8 prompts is negligible against them.

Each cell gets its **own copy** of `$RUNDIR`, mounted read-write at the
agents' expected config paths. Read-write matters: these are OAuth tokens and
the agent refreshes them in place. Mounting the live files would let a
container's refresh race the developer's own session; mounting them read-only
would break refresh. A per-cell copy can go stale, which is an accepted
trade — a stale copy fails loudly and re-copying is free.

`trap 'rm -rf "$RUNDIR"' EXIT` on the runner. Credentials are **never** baked
into an image layer — mount only. `ANTHROPIC_API_KEY` is never set: the
subscription-only rule forbids it.

**OpenCode has no credentials at all** — nothing exists locally to copy. Plan
is `opencode-claude-auth` from the catalog ("OpenCode plugin that uses your
existing Claude Code credentials"). This is unverified; see Risks.

### 3. Two tiers per cell

**Tier A — load** (auth-free, deterministic). After performing the install
method, assert the host actually sees the skill:

| Host | Assertion |
|---|---|
| Claude Code | `claude plugin list` shows `flox@flox-skills`; both `SKILL.md` files present under the plugin dir |
| Codex | `codex plugin list` shows the plugin (the README already documents this as the verify step) |
| OpenCode | skills directory populated at OpenCode's discovery path — **path to be confirmed in-image** |
| flox-ai cells | `flox-ai doctor` reports launch-ready; `flox-ai search flox` lists both skills |

Tier A needs no credentials, so it runs even when Tier B is blocked.

**Tier B — trigger** (needs credentials). One scripted headless prompt per
cell, phrased so a loaded skill answers differently from a bare model — e.g.
asking for a manifest that pins a runtime, where the skill's guidance produces
`[install]` entries with versioned `pkg-path`s.

The assertion is **skill invocation, not answer shape**. For Claude the
evidence is the `-p --output-format json` envelope. Codex and OpenCode need
their own transcript/JSON flag; those are to be identified during
implementation. If a host cannot prove invocation, that cell is recorded as
`answer-shaped evidence only` — explicitly weaker, and reported as such rather
than as a verification.

### 4. Runner

`evals/hosts/run-matrix.sh` — plain shell, no framework:

1. Build both images (skip if tag exists, `--rebuild` to force).
2. Prepare `$RUNDIR`; set the cleanup trap.
3. For each cell: fresh `docker run --rm`, own creds copy, run Tier A then
   Tier B, capture stdout/stderr to `evals/hosts/results/<date>/<cell>.log`.
4. Append one JSON line per cell to `evals/hosts/results/<date>.jsonl`:
   `{cell, host, method, tier_a, tier_b, evidence, notes}`.
5. Print a summary table.

Cells are independent: a failure records and continues, never aborts the run.
`--dry-run` prints the plan and the exact `docker run` lines without invoking
any host — the cheap way to review the matrix before spending tokens.

The repo convention is that changes ship with a test. Here that means the
assertion helpers (parse `plugin list` output, detect invocation in a
transcript) are shell functions exercised against committed fixture output, so
the parsing logic is tested without a live host. The harness as a whole is
manual by design.

## Deliverable

The run's summary table is the evidence for AI-497's three checklist items.
Cells that pass close their item; cells that fail become their own issue.

## Risks and open questions

1. **OpenCode is the weakest leg.** No local credentials, discovery path
   inferred from `flox-agent-layout.sh` rather than observed, and
   `opencode-claude-auth` unverified. OpenCode Tier B may be blocked; Tier A
   should still produce a real answer.
2. **Invocation evidence for Codex/OpenCode is unknown.** If neither can prove
   skill invocation, two cells degrade to answer-shaped evidence. Decide then
   whether that's enough to tick AI-497 or whether the item stays open.
3. **flox-ai is a PoC** (v0.8.0, "PoC for agentic guidance delivery"), and its
   Codex support is described as "env-var seams". A failing flox-ai cell is
   ambiguous between launcher and skill. Native cells are ground truth;
   flox-ai cells are additional signal, not a substitute.
4. **Image size and build time are unmeasured.** Three agent CLIs plus Node
   is not small, and `flox containerize` has not yet been run against this
   package set.
5. **Token spend is real but bounded** — 8 cells × 1 prompt. Claude cells use
   the subscription credentials, so they count against the developer's own
   limits.
6. **Credential copies expire.** A long gap between copy and run can fail a
   cell for auth reasons that look like skill failures. The runner should
   distinguish auth failure from load/trigger failure in its output.
