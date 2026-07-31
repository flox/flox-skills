# Stream-json sample fixtures

`flox-search-sample.jsonl` is a REAL captured `claude -p` stream, used
as the basis for the stream-parser unit tests (`test_run_floxify.py`).
Captured once, manually, as the AI-442 PR 1 sanctioned live
flag-verification call — see the PR description for the full writeup.

## How it was captured

```bash
claude -p "Run the shell command 'flox search hello' using Bash and \
report the first result." \
  --model claude-opus-4-8 \
  --output-format stream-json \
  --verbose \
  --allowedTools Bash \
  --strict-mcp-config \
  > flox-search-sample.jsonl
```

No `--plugin-dir` (skill not needed to exercise the stream shape) and
no other tools than `Bash` (matches the harness's actual `Bash Read
Write Edit Skill` allowlist minus the ones this prompt had no reason
to invoke).

## What it confirmed

- `--output-format stream-json` works headless with `-p` (exit 0,
  well-formed newline-delimited JSON, 13 events for this prompt).
- `--verbose` was included and the combination worked; omitting it was
  NOT tested (out of scope for the single sanctioned call) — the
  harness therefore always passes `--verbose` alongside
  `--output-format stream-json`, matching the verified-working
  combination rather than assuming it is optional.
- Event `type` values seen: `system` (`hook_started` / `hook_response`
  / `init` / `thinking_tokens`), `assistant`, `user`, `rate_limit_event`,
  `result` (`subtype: "success"`).
- `assistant` events carry `message.content`, a list of blocks
  (`thinking`, `text`, `tool_use`). A `tool_use` block has `name`
  (e.g. `"Bash"`) and `input` (e.g.
  `{"command": "flox search hello", "description": "..."}`) — this is
  the extraction point for tool-call counting and `flox search`/`flox
  show` classification.
- `user` events carry the corresponding `tool_result` content — not
  needed for counting, but present for completeness.
- The terminal `result` event carries the SAME fields as the
  non-streaming `--output-format json` envelope (`total_cost_usd`,
  `usage.{input,output,cache_read_input,cache_creation_input}_tokens`,
  `duration_ms`, `num_turns`, `result` text), so the ported
  `_parse_meta` needs no field-name changes — only a different
  extraction site (the last `result`-typed line of the stream, not the
  only line of stdout).

## Anonymization note

No credentials, tokens, or private data — the transcript is a public
`flox search hello` catalog query. The one namespace it surfaces
(`billlevine/hello`) is a published Flox catalog package name, not a
secret.

## C1 fix verification: `skills-arm-setting-sources-sample.jsonl` /
## `baseline-arm-setting-sources-sample.jsonl`

A live PR #57 review caught that `flox-search-sample.jsonl` above is
itself a reproduction of a real bug: it was captured with NEITHER
`--plugin-dir` NOR `--setting-sources`, and its `init` event shows the
`flox` plugin loaded anyway (`slash_commands` includes `flox:floxify`,
`plugins` includes `{"name": "flox", "source": "flox@flox-skills"}`) —
this machine's user-scope `~/.claude/settings.json` has
`flox@flox-skills` enabled in `enabledPlugins`, and
`--strict-mcp-config` only gates MCP servers, not plugins. The
"baseline" arm would have silently run WITH the skill loaded.

Fix: `--setting-sources project,local` on both arms (excludes the
user-scope settings file, so its `enabledPlugins` entry never
applies). Two more sanctioned live calls proved both directions with
the fix in place:

```bash
# Skills arm: --setting-sources PLUS --plugin-dir
claude -p "Say hello, do not use any tools." \
  --model claude-opus-4-8 --output-format stream-json --verbose \
  --allowedTools Bash Read Write Edit Skill --strict-mcp-config \
  --setting-sources project,local \
  --plugin-dir "<repo>/flox-plugin" \
  > skills-arm-setting-sources-sample.jsonl

# Baseline arm: --setting-sources, NO --plugin-dir
claude -p "Say hello, do not use any tools." \
  --model claude-opus-4-8 --output-format stream-json --verbose \
  --allowedTools Bash Read Write Edit Skill --strict-mcp-config \
  --setting-sources project,local \
  > baseline-arm-setting-sources-sample.jsonl
```

Results:

- **Skills arm still loads the plugin** — `slash_commands` still
  includes `flox:flox`/`flox:floxify`, and `plugins` still lists it,
  now with `"source": "flox@inline"` (pointing at the local
  `--plugin-dir` path) instead of `"flox@flox-skills"` (the user-scope
  marketplace cache) — even better than expected: it also proves the
  in-worktree skill code is what actually loads, not some stale cached
  copy.
- **Baseline arm is genuinely clean** — `plugins: []`,
  zero `flox:`-prefixed `slash_commands` (43 total, none of them
  flox-related).
- `_detect_flox_plugin_contamination` (the runtime belt-and-suspenders
  guard) was run directly against both real captured `init` events:
  returns `False` for the baseline sample, `True` for the skills
  sample and for the original `flox-search-sample.jsonl` — matching
  every expectation above.
