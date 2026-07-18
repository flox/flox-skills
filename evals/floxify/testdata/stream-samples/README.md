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
