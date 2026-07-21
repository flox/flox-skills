# acme-platform (polyglot multi-runtime repo)

One repository, three runtimes co-located at the root — the case that
exercises floxify's Phase-1 multi-ecosystem detection (all three runtime
manifests are visible at the repo root, not hidden in a build tool):

- **Node 20** (`package.json` + `.nvmrc`, pnpm) — the `web/` dashboard
- **Python 3.12** (`pyproject.toml` + `uv.lock`, uv) — the `api/` service
- **Go 1.22** (`go.mod`) — the `cmd/worker` background worker

A new developer needs Node, Python, and Go all present at once:

```
pnpm install && pnpm --dir web build
uv sync && uv run uvicorn api.src.main:app
go build ./cmd/worker
```
