# Probe results — `hosts-base` image

Captured 2026-07-28 from `hosts-base:20260728` (1.49 GB). Every constant in
`lib/cells.py` and `run_matrix.py` traces back to something on this page.
Re-run the probe when the image is rebuilt from newer packages.

## Container identity

| Fact | Value |
|---|---|
| Default `$HOME` | `/var/empty` — **not usable**, pass `-e HOME=/root` |
| `$HOME` override | `-e HOME=/root` works; `/root` exists and is writable (verified) |
| User | `root` |
| Claude Code | 2.1.220 |
| Codex | codex-cli 0.145.0 |
| OpenCode | 1.18.8 |
| Node / npx | v24.15.0 / 11.12.1 |

## Building the image

Two things bite:

1. **A hand-written `.flox/env/manifest.toml` is not an environment.**
   `flox containerize` fails with "Found a '.flox' directory but unable to
   locate an 'env.json' in it." Scaffold with `flox init -d <dir> -n <name>`,
   then apply the real manifest with `flox edit -d <dir> -f <file>`.
2. **`-t` is the tag alone, not `name:tag`.** The repository name comes from
   the *environment* name, so the environment `hosts-base` plus `-t 20260728`
   gives `hosts-base:20260728`. Passing `-t base:20260728` produces
   `hosts-base:base:20260728` → `ERROR: invalid reference format`.

## Running commands in the container

`docker run <img> bash -lc '<script>'` is **not** safe for non-trivial
scripts. The flox activation entrypoint re-quotes the payload and sources it,
so anything containing `$( )` or parentheses dies with:

```
bash: syntax error near unexpected token `('
```

**Mount a script file and run `bash /cell.sh` instead.** Command
substitution inside a mounted script works normally — verified with a nested
`$(echo yes)`.

## Headless invocation

| Host | Command |
|---|---|
| Claude Code | `claude -p "<prompt>" --output-format json` |
| Codex | `codex exec "<prompt>" --json` (add `--skip-git-repo-check` outside a git repo) |
| OpenCode | `opencode run "<message>" --format json` |

Codex `exec` also offers `--output-schema <FILE>`, `-m/--model`, and
`--dangerously-bypass-approvals-and-sandbox` if approval prompts block a run.

## Listing what a host has loaded

- **Claude Code:** `claude plugin list`.
- **Codex:** `codex plugin list` (the top-level README documents this as the
  verify step).
- **OpenCode: there is no plugin or skills list subcommand.** The full set is
  `completion, acp, mcp, attach, run, debug, providers|auth, agent, upgrade,
  uninstall, serve, web, models, stats, export, import, github`. So OpenCode's
  Tier A assertion has to be a filesystem check, not a CLI query.

## Config dirs on a fresh image (`HOME=/root`)

All four are absent/empty before any install — the baseline a post-install
diff is read against:

```
/root/.claude
/root/.codex
/root/.config/opencode
/root/.local/share/opencode
```

## Still open

- **OpenCode's skill-discovery path** is still unobserved. It gets pinned in
  Task 6 by diffing these dirs after a real `npx skills add` and a real
  `flox-ai launch opencode`.
- **Whether `codex exec --json` and `opencode run --format json` expose
  evidence of skill invocation**, as opposed to only the final answer. If they
  don't, those cells report `answer-shaped evidence only` per the design.
