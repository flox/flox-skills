# gitea — golden manifest notes

- **Repo**: https://github.com/go-gitea/gitea
- **Pinned SHA**: `11363e2f0cd6`
  (full: `11363e2f0cd61ce79965ccded3dcfe2f0324270a`,
  `refs/heads/main`, committed 2026-07-15 22:10 +0200,
  "chore(renovate): bundle major updates, use chore commit type (#38470)")
- **Ecosystem**: go (Go backend + Vite/pnpm TypeScript frontend)
- **Validation level**: static + per-package verified. Every pkg-path
  below confirmed with `flox show` / `flox search --all` on flox 1.13.2.
  The manifest was NOT `flox activate`d at capture time (per that task's
  constraint); TOML validated with `tomllib`. Resolution-tested by AI-457
  (2026-07-16) — see "Activation validation" below. NOT functionally
  tested — no real gitea checkout, so `pnpm install` never ran against
  the real lockfile.

> Note on `git ls-remote ... main`: the remote exposes **two** refs matching
> `main` — `refs/for/main` (`4afec63b1306`, a Gerrit-style review ref) and
> `refs/heads/main` (`11363e2f0cd6`). `cut` on the raw output grabbed both;
> the real branch HEAD is `refs/heads/main` = **`11363e2f0cd6`**. This is a
> real footgun worth guarding against in the skill (see observations).

## Provenance table (file → value)

| Fact | Source file | Value |
|------|-------------|-------|
| Go version (directive) | `go.mod` | `go 1.26.5` |
| Go toolchain (dev) | `flake.nix` | `pkgs.go_1_26` |
| Go image | `Dockerfile` | `golang:1.26-alpine3.24` |
| Node version (dev) | `flake.nix` | `pkgs.nodejs_26` |
| Node floor | `package.json` engines.node | `>= 22.18.0` |
| Node dotfile | (none) | no `.nvmrc` / `.node-version` |
| pnpm (pinned) | `package.json` packageManager | `pnpm@11.10.0` |
| pnpm floor | `package.json` engines.pnpm | `>= 11.0.0` |
| pnpm (dev, STALE) | `flake.nix` | `pkgs.pnpm_10` |
| Build driver | `Makefile`, `AGENTS.md` | `make build` / `make deps` |
| Frontend build | `vite.config.ts`, Makefile `FRONTEND_CONFIGS` | Vite |
| Frontend install | `Makefile` node_modules target | `pnpm install --frozen-lockfile` |
| package.json scripts | `package.json` | empty (all via `make` + `pnpm exec`) |
| CGO default | `Makefile:46` | `CGO_ENABLED ?= 0` (pure-Go) |
| CGO trigger | `Makefile:47` | TAGS `sqlite_mattn` or `pam` → CGO=1 |
| Default DB | `Makefile:39`, `app.example.ini:385` | SQLite (`sqlite3`) |
| DB alternatives | `custom/conf/app.example.ini:355` | mysql / postgres / mssql |
| git / git-lfs | `flake.nix` | `pkgs.git`, `pkgs.git-lfs` |
| C toolchain | `flake.nix` | `pkg-config`, stdenv cc, `gnumake` |
| SQLite lib/CLI | `flake.nix` | `pkgs.sqlite` |
| Lint tooling (optional) | `flake.nix`, `pyproject.toml`, `uv.lock` | `python314`, `uv`, `gofumpt` |
| Frontend native (optional) | `flake.nix` | `cairo`, `pixman` |
| Compose file | (none) | no `docker-compose*.yml` in repo |

## `flox show` / `flox search` verification log

| pkg-path | Result (flox 1.13.2) | Used? |
|----------|----------------------|-------|
| `go` (bare) | Latest **1.26.4** | no (versioned preferred) |
| `go_1_26` | Latest **1.26.4** (1.26.1–1.26.4) | **yes** |
| `nodejs_26` | Latest **26.5.0** | **yes** |
| `nodejs_22` | Latest **22.23.1** | no (floor-satisfying alt) |
| `pnpm` (bare) | Latest **11.11.0**; 11.10.0 present | **yes** (pinned 11.10.0) |
| `pnpm_11` | **11.11.0** only (no 11.10.0) | no (can't hit exact pin) |
| `pnpm_10` | Latest **10.34.5** | no (violates engines >= 11) |
| `corepack` | **0.35.0** | no (noted as alt) |
| `git` | **2.54.0** | **yes** |
| `git-lfs` | **3.7.1** | **yes** |
| `gcc` | **15.2.0** | **yes** |
| `gnumake` | **4.4.1** | **yes** |
| `pkg-config` | **0.29.2** | **yes** |
| `sqlite` | **3.53.1** | **yes** |
| `postgresql` (search) | bare + `postgresql_10`..`_19`, `_jit` variants | no (optional, not wired) |

### AI-457 re-verification (per-system availability, 2026-07-16)

| pkg-path | Result | Decision |
|----------|--------|----------|
| `flox show nodejs_26` | `26.5.0` (aarch64-darwin, aarch64-linux, x86_64-linux only) | missing x86_64-darwin |
| `flox show pnpm` | `11.10.0` present (aarch64-darwin, aarch64-linux, x86_64-linux only) | missing x86_64-darwin |
| `flox show sqlite` | `3.53.1` (aarch64-darwin, aarch64-linux, x86_64-linux only) | missing x86_64-darwin |
| `flox show go_1_26`, `git`, `git-lfs`, `gcc`, `gnumake`, `pkg-config` | all four systems, no restriction | unaffected |

All three of nodejs_26 (frontend runtime), pnpm (frontend package
manager), and sqlite (zero-config default DB) are hard dependencies for
the documented dev flow, so `[options].systems` drops `x86_64-darwin`
rather than scoping per-package overrides — the env can't build or run
gitea's default path on Intel macOS regardless of which one is asked
for.

**Activation validation (resolution-tested, not functionally tested).** Unlike mastodon/posthog/sentry/plausible, this
manifest resolved and activated cleanly on the first `flox activate`
(x86_64-linux, throwaway directory, no real gitea checkout) with no
`pkg-group` split needed — go_1_26/nodejs_26/pnpm/sqlite/git/git-lfs/gcc/
gnumake/pkg-config apparently share a common catalog page. Hook output
confirmed: `gitea env ready. Build: 'make build' ...` printed as expected.

## Versioned-vs-bare pkg-path finding

The task flagged that versioned pkg-paths can expose a different ceiling than
bare. What the catalog actually shows for gitea's runtimes:

- **go**: bare `go` and `go_1_26` are **identical** here (both 1.26.4). No
  ceiling difference — the 1.26 series is the current top of the catalog.
- **nodejs**: bare `nodejs` tracks the latest major (26.x); `nodejs_26` pins
  that major explicitly at 26.5.0. Using the versioned path is the safe,
  reproducible choice and matches the flake.
- **pnpm**: the interesting inversion. Bare `pnpm` → **11.11.0** (latest
  major line), while the versioned `pnpm_10` → **10.34.5** — i.e. the
  *versioned* path is OLDER than bare, because `pnpm_10` is frozen to the 10
  series. gitea needs pnpm 11 (packageManager `pnpm@11.10.0`, engines
  `>= 11.0.0`), so the versioned `pnpm_10` from the flake is exactly wrong.
  `pnpm_11` exists but only carries 11.11.0 — one patch above the pin — so to
  land the exact `11.10.0` I used the **bare `pnpm` pkg-path + `version =
  "11.10.0"`**. Lesson: "versioned pkg-path = newer" is not a rule; for
  major-lines that have moved on, the bare path is newer.

## Go toolchain gap (1.26.5 vs 1.26.4)

`go.mod` declares `go 1.26.5`, but the catalog's newest Go is **1.26.4**
(both `go` and `go_1_26`). This is a bleeding-edge repo (HEAD dated today,
2026-07-15). Consequences:

- With the default `GOTOOLCHAIN=auto`, `go build` will download the exact
  `go1.26.5` toolchain on first use (needs network). This is Go's normal
  self-upgrade behavior and keeps the build correct.
- `GOTOOLCHAIN=local` would force the flox-provided 1.26.4 and error on the
  directive. The manifest documents both options and leaves the default
  (auto) so the env is functional the moment network is available. Not a
  hard failure — a one-patch catalog lag.

## SQLite vs Postgres — service decision

**Kept SQLite as the zero-config default; wired NO Flox service.** Grounds:

1. gitea's embedded default is SQLite (`custom/conf/app.example.ini` shows
   `DB_TYPE = sqlite3` as the serverless option; Makefile CI default is
   `GITEA_TEST_DATABASE = sqlite`). A fresh `./gitea web` boots against a
   SQLite file under `./data` with no external process.
2. There is **no `docker-compose.yml`** in the repo — nothing declares a
   Postgres/MySQL container as the expected dev topology (unlike mastodon /
   posthog / supabase fixtures, which do).
3. The flake.nix devShell includes `sqlite` and **no** postgres server.

Postgres/MySQL/MSSQL are first-class *production* options but opt-in for dev.
The manifest documents the exact steps to switch to Postgres (add
`postgresql` to install + a `[services.postgres]` block + `DB_TYPE=postgres`)
in a comment, so `expected_services` is empty by design.

## Native / CGO dependency reality

- **Default build is pure-Go** (`CGO_ENABLED ?= 0`). No C compiler is
  required for the zero-config `make build`. gitea's default sqlite path uses
  a pure-Go driver; the CGO `mattn/go-sqlite3` driver is opt-in via
  `TAGS='sqlite sqlite_unlock_notify'` (which sets `sqlite_mattn` →
  `CGO_ENABLED=1`), and `pam` also forces CGO.
- `gcc` + `pkg-config` are included so the CGO build mode works too, matching
  the flake's stdenv cc + `pkg-config`. `gnumake` is mandatory (every dev
  task is a make target).
- **Frontend native**: the flake also lists `cairo` + `pixman`. Inspection of
  `pnpm-lock.yaml` shows the napi runtimes are WASM-based
  (`@emnapi/*`, `@napi-rs/wasm-runtime`) and `canvas` appears only as an
  OPTIONAL peer dep (jsdom). `pnpm install --frozen-lockfile` does not compile
  a native canvas, so cairo/pixman are treated as optional and left out to
  keep the manifest lean.
- **git** is a genuine runtime dependency (gitea shells out to git);
  **git-lfs** backs LFS support. Both included.

## Items NOT included (✗) and why

- ✗ **postgres / mysql services** — optional, opt-in; SQLite is zero-config
  default and no compose file declares them. Documented as a comment.
- ✗ **python314 + uv** — flake includes them, but only for *linting*
  (`make lint-templates`, markdown/spectral) and `deps-py`. Not needed to
  build or run gitea. Noted as optional.
- ✗ **cairo / pixman** — flake frontend extras; not exercised by a
  frozen-lockfile install (WASM napi, canvas optional). Noted as optional.
- ✗ **gofumpt** — flake dev formatter, not a build/run dep.
- ✗ **exact go 1.26.5** — not in catalog (ceiling 1.26.4); handled via
  GOTOOLCHAIN=auto. Documented.
- ✗ **exact pnpm via `pnpm_11`** — `pnpm_11` only has 11.11.0; used bare
  `pnpm` + `version` to hit 11.10.0.

## Skill-improvement observations

1. **`git ls-remote <url> main` is ambiguous** — it also matches
   `refs/for/main` (Gerrit review refs) and other `*main*` refs, so naive
   `cut -c1-12` yields multiple SHAs and can silently pin the WRONG commit.
   The skill should resolve `refs/heads/main` explicitly (e.g.
   `git ls-remote <url> refs/heads/main` or filter on the exact ref) rather
   than the bare branch name.
2. **flake.nix is a high-value but not always authoritative source.** gitea
   ships a Nix devShell that pins the toolchain precisely — a goldmine for
   floxify. But its `pnpm_10` pin *contradicts* the repo's own
   `package.json` (packageManager `pnpm@11.10.0`, engines `>= 11.0.0`). The
   skill should treat `package.json.packageManager` as authoritative for the
   JS package manager and reconcile against flake.nix, flagging the conflict
   rather than blindly trusting either. General rule: when two in-repo
   sources disagree, prefer the one the tooling itself enforces at runtime.
3. **"Versioned pkg-path = newer ceiling" is not a rule.** For pnpm the
   versioned `pnpm_10` is *older* than bare `pnpm` (11.x). The skill should
   query both and reason about which the repo actually needs (engines/
   packageManager) instead of assuming the `_N` path is the higher ceiling —
   and know that hitting an exact minor sometimes requires the bare path +
   `version` because the `_N` path may carry only its latest patch.
