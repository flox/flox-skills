# Host-matrix smoke test

Does the Flox plugin actually load and trigger in **Claude Code**, **Codex**,
and **OpenCode**, across the install methods the top-level README documents?
This answers that, inside disposable containers, without installing an agent
or an `npx` package onto your machine.

Design: [DESIGN.md](DESIGN.md) · Plan: [PLAN.md](PLAN.md) · Probe results:
[PROBE.md](PROBE.md)

## What it proves — and what it does not

This is a **smoke test**: one attempt per cell, pass/fail. A pass proves the
plumbing works end to end. A fail proves something is broken. Neither
measures *how reliably* a model reaches for the skill — that's a trigger
**rate**, it needs many repetitions, and it lives in the screening tier
(`evals/screen.py`), not here.

Two tiers per cell:

- **Tier A — load.** Install the plugin, then ask the host to list what it
  has. No credentials needed, fully deterministic. Runs even when Tier B
  can't.
- **Tier B — trigger.** One prompt, checked for evidence the skill was
  actually invoked. Needs a logged-in host.

## The matrix

|  | native plugin | `npx skills add` | flox-ai + skills-flox |
|---|---|---|---|
| **Claude Code** | ✓ | ✓ | ✓ |
| **Codex** | ✓ | ✓ | ✓ |
| **OpenCode** | — | ✓ | ✓ |

OpenCode has no plugin-marketplace concept, so it has no native cell. Claude
Code is the control: if a Claude cell fails, suspect the harness before the
skill.

Two images keep the methods honest — `base` has the host CLIs and no skills
(so the native and npx cells install into a clean host), `withpkg` adds
`flox-ai` and the *published* `flox/skills-flox` package.

## Running it

```bash
cd evals/hosts
python3 run_matrix.py --dry-run              # prints the plan, invokes nothing
python3 run_matrix.py                        # full run
python3 run_matrix.py --cells claude-native  # one cell
python3 run_matrix.py --rebuild              # force the images to rebuild
```

Start with `--dry-run`. It costs nothing and shows exactly which containers
would start and what each would run.

Results land in `results/<date>.jsonl`, one JSON object per cell, plus a
summary table on stdout.

## What a full run needs from you

- **Docker**, and the two images (built automatically on first run).
- **A logged-in Claude Code and Codex on this machine.** The container cannot
  do an interactive login. Check with
  `jq -r 'keys[]' ~/.claude/.credentials.json` and
  `jq -r .auth_mode ~/.codex/auth.json`.
- **Subscription rate limit, not dollars.** Both hosts authenticate by OAuth,
  so no `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` is involved and nothing is
  billed per token. Eight prompts is negligible against the limits.

## What leaves your machine

Only your **Claude subscription token** and your **Codex token**, copied into
a temp directory that is deleted when the run ends.

`~/.claude/.credentials.json` holds more than your Claude login — it also
carries OAuth tokens for every MCP server you've authenticated (Linear,
Notion, Slack, Sentry, and friends). None of that is needed here, so
`lib/creds.py` copies **only** the `claudeAiOauth` block and refuses to
proceed unless the file it wrote has exactly that one key. Credentials are
mounted, never baked into an image layer.

If a cell reports `auth-error`, that's a credential problem — a stale copy or
a logged-out host — not a skill failure. The runner separates the two on
purpose.

## Why this isn't in CI

It needs live credentials and spends rate limit. CI runs this directory's
unit tests only:

```bash
cd evals/hosts && python3 -m unittest discover -v
```

Those tests mock every subprocess — no Docker, no flox, no network, no API
spend.
