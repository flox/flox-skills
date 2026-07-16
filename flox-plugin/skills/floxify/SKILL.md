---
name: floxify
description: >
  Onboard any existing repo to Flox. Run from inside a repo (or point to a local path)
  to detect runtimes, services, and build tools, then create .flox/env/manifest.toml
  so `flox activate` becomes the only setup command a new developer needs.
metadata:
  version: 1.0.0
  argument-hint: "[github-url | local-path | empty for cwd]"
---

# floxify

You are setting up a Flox environment for an existing software project. This may be
someone's first time seeing Flox. **Treat this as a first impression.** Be fast,
transparent, and precise. Start immediately — no greeting, no preamble.

**Primary use case:** The developer is already inside their repo — they ran `/floxify`
from within it, or said something like "floxify this project" or "set up Flox for my
sentry repo." Treat the current working directory as the target unless given a specific
local path. The GitHub URL path exists for exploration but is not the normal workflow.

**Core principle:** Flox manages system-level dependencies (language runtimes, system
libraries, databases). It does NOT replace pip, npm, cargo, or composer. The
`on-activate` hook bridges them: `flox activate` loads the pinned runtime AND runs
`pip install` or `npm install` automatically. One command, full environment.

Input: `$ARGUMENTS`

---

## Phase 0: Setup

### Check available tools

Use `flox` via Bash for all operations. If flox-mcp tools are present in your tool list
(`search_packages` / `mcp__flox__search_packages`, `init_new_environment`, etc.), you may
use them as an alternative to the bash equivalents — but bash is the default.

Check flox availability:
```bash
flox --version 2>/dev/null || echo "FLOX_NOT_FOUND"
```

Note: `flox activate` may print `! Your FloxHub token has expired` — this is cosmetic.
Local activation works fine without it. Do NOT surface this warning in your output.
If the user asks, tell them: `flox auth login` silences it permanently.

If flox is not installed: generate the manifest content and print it with exact
instructions. Install URL: `https://flox.dev/docs/install-flox/install`
Then: `flox init && flox activate`

### Resolve the target

Parse `$ARGUMENTS` and assign `TARGET_DIR`:

```bash
ARGUMENTS="$ARGUMENTS"  # the skill input

if [ -z "$ARGUMENTS" ]; then
  TARGET_DIR="$(pwd)"

elif echo "$ARGUMENTS" | grep -q '^https://github.com/'; then
  # GitHub URL — clones into the current directory, same as if you'd run git clone yourself
  REPO_NAME="$(echo "$ARGUMENTS" | sed 's|.*/||')"
  TARGET_DIR="$(pwd)/$REPO_NAME"
  if [ -d "$TARGET_DIR" ]; then
    echo "$REPO_NAME/ already exists here. cd into it and run /floxify with no arguments."
    exit 1
  fi
  echo "Cloning $REPO_NAME into $(pwd)/$REPO_NAME ..."
  git clone --depth=1 "$ARGUMENTS" "$TARGET_DIR" 2>&1
  if [ $? -ne 0 ]; then
    echo "Error: git clone failed. Check the URL, your internet connection, and repo access."
    exit 1
  fi

elif [ -d "$ARGUMENTS" ]; then
  TARGET_DIR="$(realpath "$ARGUMENTS")"

elif [ -f "$ARGUMENTS" ]; then
  echo "Error: '$ARGUMENTS' is a file, not a directory."
  exit 1

else
  # Natural language or unrecognized — treat as hints, use cwd
  TARGET_DIR="$(pwd)"
fi

PROJECT_NAME="$(basename "$TARGET_DIR")"
```

**Print this immediately after TARGET_DIR is set:**
```
Scanning <project-name>/  detecting runtimes, services, and build tools...
```

### Check for existing Flox setup

```bash
test -d "$TARGET_DIR/.flox" && echo "HAS_FLOX" || echo "CLEAN"
```

**If `HAS_FLOX`:** Switch to audit mode. Do NOT initialize or modify anything.
See "Audit Mode" below.

**If `devbox.json` exists (and no .flox/):** Switch to DevBox conversion mode.
See "DevBox Conversion Mode" below.

**If `flake.nix` or `shell.nix` exists (and no .flox/):**
Ask:
```
This project uses Nix directly. Options:
  1  Audit only — show what Flox would add without touching anything
  2  Set up Flox alongside it — they coexist fine
  3  Skip

Which? (default: 1)
```

**If clean (.flox/ absent, no Nix files):** Continue to Phase 1.

---

## Audit Mode (when .flox/ already exists)

Print: `<project-name>/ already uses Flox. Running gap analysis...`

1. Read `.flox/env/manifest.toml` — note installed packages, whether an
   `on-activate` hook exists, and whether a `[profile]` section activates the venv.
2. Run the Phase 1 scan to detect runtimes and services from dep files.
3. Compare: what do dep files imply vs. what's in the manifest?
4. Print:

```
<project-name>/ — Flox gap analysis

Already in manifest.toml (<N> packages):
  python311       ← .python-version
  nodejs_20       ← .nvmrc
  postgresql_16   ← docker-compose.yml

Possibly missing (detected in dep files, not in manifest):
  redis           → detected via requirements.txt (redis package)
  pkg-config      → detected via requirements.txt (psycopg2 non-binary)

on-activate hook: [present / missing]
```

If the hook is missing, suggest:
```
No on-activate hook found. To auto-run pip install on activate:
  flox edit
  # Add in [hook] section:
  # on-activate = """
  #   pip install --quiet -r requirements.txt
  # """
```

Stop here. **Never modify an existing manifest.**

---

## DevBox Conversion Mode

Print: `<project-name>/ uses DevBox. Converting devbox.json → Flox manifest...`

DevBox and Flox both use the Nix catalog — package names map almost 1:1.

1. Read `devbox.json`. Extract:
   - `packages` — DevBox uses `name@version` syntax; search `"name version"` (e.g. `python@3.12` → search `"python 3.12"`)
   - `shell.init_hook` → maps to `[hook] on-activate` (copy verbatim — it's already Bash)
   - `shell.env` / `env` → maps to `[vars]` (copy key/value pairs directly)

2. Resolve each package via `search_packages`. Name quirks:
   - `rust` / `rustup` → search `"cargo"` and `"rustc"` separately
   - `mysql` or `mysql@8` → search `"mariadb"`
   - `pkgconfig` → search `"pkg-config"`
   - `mongodb` → search `"mongodb"` (community edition shows as `mongodb-ce`)
   - No match → add to ✗ section.

3. If `postgresql`, `redis`, or `mysql` appear in packages AND no `docker-compose.yml` exists:
   apply the PostgreSQL-as-service / Redis-as-service patterns from Phase 3.

4. Skip Phase 1 — go straight to Phase 3 with the packages you've extracted.

In the report: `devbox.json → .flox/env/manifest.toml  ·  devbox.json left untouched`

---

## Brewfile Conversion Mode

A `Brewfile` signals `brew bundle` is in use — treat it as a high-priority source in
Phase 1 (not a separate mode).

For each `brew "name"` or `brew "name@version"` line, call `search_packages`:
- `brew "postgresql@16"` → search `"postgresql 16"`
- Skip: `cask "..."` (GUI apps), `tap "..."` (package sources), and system utils:
  `git`, `vim`, `bash`, `grep`, `coreutils`, `wget`, `curl`, `make`
- Naming quirks: `libpq` → `"postgresql"`, `mysql-client` → `"mariadb"`, `openssl@3` → `"openssl"`

No match → add to ✗ section.

---

## Dev Container Full Conversion

When `.devcontainer/devcontainer.json` is present, extract all of:

**`image` field** → parse image name + tag for a runtime hint.
- `devcontainers/python:3.12` → search `"python 3.12"`, `devcontainers/node:22` → search `"nodejs 22"`
- Strip suffixes: `node:22-alpine`, `python:3.12-slim` → strip suffix, search runtime + version
- `ubuntu:22.04`, `debian:bookworm` → base OS only; no runtime to extract
- Rust: search `"cargo"` + `"rustc"`

**`features` field** → for each entry, take the last URI path segment as the tool name;
include `"version"` if present.
- `features/github-cli:1` → search `"gh"`, `features/aws-cli:1` → search `"awscli"`
- `features/kubectl-helm-minikube:1` → search `"kubectl"` + `"helm"` separately
- `features/dotnet:1` with `{"version": "8"}` → search `"dotnet sdk 8"`

**`postCreateCommand`** → copy verbatim into `[hook] on-activate` (it's already shell).
If it's a script reference (e.g. `"scripts/setup.sh"`), note it in the report — don't inline it.

**`containerEnv`** → copy each key/value into `[vars]` directly.

**`forwardPorts`** → note in report as context only; don't configure anything.

**`remoteUser` / `mounts`** → skip (container-specific, no Flox equivalent).

---

## Phase 1: Read the project

Ground every version and service in a file — never guess. Run the bundled
analyzer first (Step 1a): it reads the pin files, lockfiles, and
docker-compose services deterministically. Then read the high-signal files
yourself (Step 1b) for nuance it only summarizes.

### Step 1a — Run the grounded analyzer (do this first)

The analyzer ships with this skill at `scripts/detect.py` (next to this
SKILL.md). Run it through Flox so you don't depend on a system Python — this is
also the fastest way to run a one-off script:

```bash
flox run -p python313 -- python3 "<skill-dir>/scripts/detect.py" "$TARGET_DIR"
```

`<skill-dir>` is this skill's own directory — the folder that holds this
SKILL.md (the same place you'd read `scripts/` or a reference file from). Use
its absolute path.

- If `flox run` errors with an unknown-subcommand or usage message, the user's
  Flox predates 1.13 (`flox run` shipped in 1.13). Tell them once, plainly:
  "Your Flox is older than 1.13, so I can't use the fast analyzer — upgrade to
  get `flox run`: https://flox.dev/docs/install-flox/install". Then fall back to
  `python3 "<skill-dir>/scripts/detect.py" "$TARGET_DIR"` if a `python3` is on
  PATH, and if neither works, scan manually (Step 1b). The analyzer is an
  accelerator, not a hard dependency — never block on it.

The analyzer prints one JSON object; every fact carries the file it came from:

- `runtimes` — each version pin with its `source` file. **Use these versions
  verbatim in Phase 2 — do not round, bump, or substitute a version from
  memory.** If your recollection disagrees with a `source`-tagged fact, the
  file wins.
- `package_managers` — bundler / pnpm / uv / poetry versions read from
  lockfiles (e.g. `Gemfile.lock` BUNDLED WITH, `packageManager` field).
- `services` — docker-compose services with `image`, `tag`, a `kind` guess,
  and a `config_coupled` flag (the service mounts config volumes or `depends_on`
  others — a hint it may not reduce to a single catalog package).
- `service_clients` / `native_hints` — client libraries (`pg`, `psycopg2`, …)
  and apt deps mapped to catalog **`search_terms`**. These are terms to VERIFY
  in Phase 2 with `flox search` / `flox show` — never paste a `search_term` in
  as a `pkg-path`.
- `orchestrators`, `monorepo`, `lockfiles`, `notes` — context for Phase 3.

### Step 1b — Read the high-signal files for nuance

The analyzer covers the deterministic pins and services; you still read the
files below for what it only hints at (Dockerfile `FROM`/`RUN` specifics, CI
`apt-get` lines, README setup steps, monorepo layout) and to print the
recognition lines.

Print each file as you read it — immediately, one line per file, as processed:

```
  .python-version        python 3.11
  .nvmrc                 node 20.11.0
  .github/workflows/ci.yml   python 3.11 · postgres:15 in services
  docker-compose.yml     postgres:15 · redis:7
  requirements.txt       psycopg2 · celery · redis → 503 pip packages
  package.json           react 18 · typescript → npm install
```

This real-time output is the key trust-building moment — the developer recognizes
their own project as each line appears. **Do not buffer — print as each file is read.**

**Files to scan** (priority order — higher sources win for version numbers):

1. `.devcontainer/devcontainer.json` — full conversion: `image`, `features`, `postCreateCommand`, `containerEnv` (see Dev Container Full Conversion above)
2. `devbox.json` — if present, handled via DevBox Conversion Mode (skip to Phase 3)
3. `Brewfile` — `brew "name"` lines mapped to Flox catalog (see Brewfile Conversion Mode above)
4. `.github/workflows/*.yml` — `setup-node`/`setup-python`/`setup-go` action version
   values; `services:` blocks with image names and versions; `apt-get install -y` lines
5. `.gitlab-ci.yml` — `image:` field for runtime hints; `before_script` apt-get installs
6. `.circleci/config.yml` — `image:` and orb version params
7. `Dockerfile` / `Dockerfile.dev` — `FROM` line runtime+version; `RUN apt-get install`
8. `docker-compose.yml` / `docker-compose.dev.yml` — service images with versions
9. `.nvmrc`, `.node-version` — exact Node version
10. `.python-version` — exact Python version
11. `.tool-versions` — asdf/mise multi-runtime pins (supports all languages)
12. `.mise.toml` — mise multi-runtime pins; same format as `.tool-versions`
13. `rust-toolchain.toml` — Rust channel (stable/nightly/1.x)
14. `go.mod` — Go version from `go 1.21` directive
15. `global.json` — .NET SDK version (`sdk.version` field)
16. `mix.exs` — Elixir version from `@minimum_otp_version` or `elixir: "~> X.Y"`
17. `build.sbt` — Scala version from `scalaVersion := "X.Y.Z"`
18. `pubspec.yaml` — Dart/Flutter (`environment.sdk` constraint; `flutter:` key = Flutter)
19. `build.zig` or `build.zig.zon` — presence signals Zig project
20. `Package.swift` — presence signals Swift project; `swift-tools-version` comment
21. `pyproject.toml` — `requires-python` constraint; package names for service signals
22. `requirements.txt`, `Pipfile` — package names for service signals only
23. `environment.yml` — Conda env; extract `dependencies:` for system-level signals
24. `package.json` — `engines.node`, `packageManager`, `volta.node` fields; service signals
25. `Cargo.toml` — presence of `build.rs`, native dep hints
26. `.env.example`, `.env.sample` — confirm services; flag required-but-unset vars

**Never scan:** `.venv/`, `venv/`, `node_modules/`, `__pycache__/`, `.tox/`, `vendor/`,
`.git/`, `dist/`, `build/`, `target/`, `.next/`, `coverage/`, `.cache/`

While scanning manifests (`requirements.txt`, `pyproject.toml`, `package.json`, etc.),
note any database or service client packages (`psycopg2`, `redis`, `pymysql`, `pymongo`,
`pg`, `ioredis`, `celery`, `cryptography`, `lxml`, `Pillow`, etc.). These are NOT
installed via Flox themselves — they signal system-level dependencies to resolve in Phase 2.

**After reading all files, print a detection summary with source attribution:**
```
Found: Python 3.11 (← .python-version) · Node 20.11.0 (← .nvmrc) · PostgreSQL · Redis (← requirements.txt)
```

---

## Phase 2: Resolve packages in the Flox catalog

**Search first — never assume catalog names.** The catalog evolves; hardcoded guesses
go stale. For every runtime, library, and tool detected in Phase 1, search the catalog:

```bash
flox search --all "<term>" 2>/dev/null | head -5
# or if flox-mcp is present: search_packages(search_term="<term>", limit=5)
```

**Batch all searches silently, then print one clean resolution table.** Fire all
independent lookups in parallel (e.g. all system libs in one batch, runtimes in another),
suppress the raw `flox search` output entirely, collect the results, and only then print
the resolution table. Never stream raw search output — it floods the terminal and makes
the output jarring.

```
Resolving packages...
  node 22        → search "nodejs 22"      → nodejs_22 ✓
  python 3.12    → search "python 3.12"    → python312 ✓
  postgresql     → search "postgresql 16"  → postgresql_16 ✓
  wkhtmltopdf    → search "wkhtmltopdf"    → no match ✗
```

### Search term strategies

- **Node.js**: search `"nodejs <major>"` — prefer `nodejs_22` over `nodejs`
- **Python**: search `"python <major.minor>"` — prefer `python312` over `python`
- **Go**: search `"go <major.minor>"` — prefer the versioned `go_1_23` over bare `go`
- **PostgreSQL**: search `"postgresql <major>"` not `"postgres"` — catalog name differs
- **Rust**: search `"cargo"` and `"rustc"` separately; also `"clippy"` and `"rustfmt"` for dev tooling
- **Elixir**: search `"elixir"` only — Erlang/OTP is bundled; do NOT search "erlang" separately
- **PHP**: search `"php <major.minor>"`; the catalog exposes PHP as a versioned
  package (verify the exact name with `flox show`). Extensions come from a `php`
  variant, not separate `ext-*` packages — resolve what you can and list the rest in ✗
- **Deno**: a `deno.json`/`deno.jsonc` or a `*-edge-runtime` compose image (e.g.
  Supabase edge functions) means a SECOND runtime — search `"deno"` and pin it
  alongside `nodejs`. A monorepo that pins only Node silently drops the edge-functions runtime
- **Flutter**: search `"flutter"` only — Dart SDK is bundled; do NOT search "dart" separately
- **Mise / asdf** (`.mise.toml` / `.tool-versions`): each `key = "version"` is an independent search; `python = "3.12.3"` → search `"python 3.12"`
- **Conda** (`environment.yml`): search top-level `dependencies:` binaries only; skip `- pip:` entries (handle via uv)
- **Volta** (`"volta": {"node": "22.4.0"}`): search `"nodejs 22"`
- **packageManager field** (`pnpm@9.x`, `yarn@4.x`): search the package manager name
- **`bun.lockb` present**: search `"bun"` and use it instead of nodejs

### Picking from search results

1. Prefer the most specific versioned name (`python312` over `python`)
2. For unversioned tools, pick the plain name (`redis`, `cmake`, `jq`)
3. If ambiguous, read the description in results to confirm intent
4. If no match after 1–2 attempts: add to ✗ section, don't install

**Resolve the VERSIONED name for a pinned runtime — the bare name can mislead.**
When a runtime is pinned (`.ruby-version`, `.nvmrc`, `go.mod`, `requires-python`),
`flox show <versioned>` (`flox show ruby_4_0`, `nodejs_24`, `go_1_23`, `python313`)
is authoritative. The bare `flox show ruby` may report a *lower* ceiling that
belongs to a different catalog entry — trusting it silently downgrades the
runtime. Mastodon pins Ruby 4.0.6: `flox show ruby` tops out at 3.4.x, but
`ruby_4_0` exists at 4.0.5. Search the versioned `pkg-path` first; fall back to
the bare name only for genuinely unversioned tools.

**Version mismatches:** If the catalog is one patch version behind the project's pin
(e.g. project pins `node 24.14.0`, catalog has `24.13.0`): install the closest available,
note the mismatch in source attribution (`← .nvmrc (project pins 24.14.0; catalog has 24.13.0)`).
Only add to ✗ if the major or minor version differs — patch mismatches rarely cause issues.

### Services and system dependencies

Only install these when docker-compose does NOT already manage them.

| Detected in manifests | Search for | Flox catalog name |
|-----------------------|------------|-------------------|
| `psycopg2` (non-binary) | `"postgresql"` + `"pkg-config"` + `"openssl"` | `postgresql_16`, `pkg-config`, `openssl` |
| `psycopg2-binary`, `psycopg`, `pg` (npm) | `"postgresql"` | `postgresql_16` |
| `redis`, `ioredis`, `celery` | `"redis"` | `redis` |
| `pymysql`, `mysql2`, `mysql-connector-python` | `"mariadb"` | `mariadb` |
| `pymongo`, `motor`, `mongoose` | `"mongodb"` | `mongodb-ce` |
| `cryptography`, `cffi`, `bcrypt`, `pynacl` | `"pkg-config"` + `"openssl"` | `pkg-config`, `openssl` |
| `lxml` | `"libxml2"` + `"libxslt"` | verify names with `flox search` |
| `Pillow`, `PIL` | `"libjpeg"` + `"zlib"` | verify names |
| `fluent-ffmpeg`, `ffmpeg-static`, `@elastic/elasticsearch` | `"ffmpeg"` / `"elasticsearch"` | `ffmpeg`, `elasticsearch` |

Other services in catalog (no specific dependency signal): `rabbitmq` (RabbitMQ).

**Catalog presence does NOT mean "wire it as a Flox service."** A datastore can
exist in the catalog and still be the wrong thing to run as a bare
`[services.*]`. Wire directly only the *leaf* datastores the app depends on
directly (usually `postgres`, `redis`). Defer a service to docker-compose or the
project's own orchestrator when any of these hold:

- the analyzer flags its compose service `config_coupled` — it mounts server
  config files or `depends_on` other services (a bare package can't reproduce that),
- it's reached only *transitively*, through another service's dependency graph, or
- it's a customized image (e.g. `supabase/postgres` ships extensions that stock
  `postgresql` lacks — note the caveat and wire stock postgres for plain dev only).

ClickHouse and Kafka ARE in the catalog now, but PostHog's ClickHouse mounts
server config and depends on kafka/zookeeper, and Sentry's ClickHouse/Kafka
arrive transitively through snuba's `devservices` graph — both belong to their
project's orchestrator, not a Flox `[services.*]`. Start them via docker-compose
(install `docker-compose`, bring them up in the hook when Docker is available)
or hand off to the orchestrator, and say so in ⚠ — never hallucinate a catalog
package for them, and never silently drop them. Truly-absent-from-catalog:
Zookeeper, Cassandra. For Temporal: try `flox search temporal-cli` first.

**Native C-extension system libraries often live in the `Dockerfile`, `Aptfile`,
or `Brewfile` — not the language manifest.** The analyzer scans `Dockerfile`
`RUN apt-get install` and `Aptfile` lines for these and maps them to catalog
search terms; still confirm each with `flox show`. Mastodon's `vips` / `ffmpeg` /
`icu` / `libidn` are in its Aptfile + Dockerfile, not the Gemfile — and `ffmpeg`
never appears as a gem at all. Watch the specific-variant gotchas: `idn-ruby`
needs GNU libidn v1 (`libidn`), not `libidn2`; `charlock_holmes` links system
ICU (`icu`).

### Build tool signals

| File or pattern | Search for |
|-----------------|------------|
| `CMakeLists.txt` | `"cmake"` + `"gcc"` + `"pkg-config"` |
| `Cargo.toml` with `build.rs` | `"pkg-config"` + `"gcc"` |
| `Makefile` with `$(CC)` | `"gcc"` + `"gnumake"` |
| `jq` in CI/scripts | `"jq"` |
| `curl` in CI/scripts | `"curl"` |

**CMakeLists.txt: scan all `pkg_check_modules` and `find_package` calls** — each one
is a potential Flox package. For each dep, search the catalog and apply the
platform-conditional rule above: cross-platform deps get no `systems` filter,
platform-specific deps get the right `systems` scope. Do not silently drop any dep —
if it's in the catalog, add it; if it's not, put it in ✗. The same logic applies to
`Makefile` targets, Dockerfile `RUN apt-get`/`brew install` lines, and any other
build-system dep declaration you encounter.

### Custom service orchestrators

If the project uses a custom tool to manage services and there is **no `docker-compose.yml`**
at the root, do NOT try to wire services via docker-compose. List them in ⚠ with the
tool name and the command to start them. Don't claim these are a gap.

**When there's no root `docker-compose.yml`, the service topology usually lives
elsewhere — probe before concluding a project has no services:**
`devservices/config.yml` (Sentry), `compose.yaml` / `compose.yml`, `Procfile` /
`Procfile.dev`, `.devcontainer/`, `devenv/`, `Tiltfile`, and dev targets in the
`Makefile`. Sentry's entire postgres/redis/clickhouse/kafka topology is
invisible if you only look for `docker-compose.yml`. The analyzer surfaces the
common orchestrators (`orchestrators` field) and any `compose*.yml` it finds.

| Signal | Orchestrator | What to say in ⚠ |
|--------|-------------|-------------------|
| `devservices/` directory | Sentry devservices | `managed by devservices — run: devservices up` |
| `Tiltfile` | Tilt | `managed by Tilt — run: tilt up` |
| `skaffold.yaml` | Skaffold | `managed by Skaffold — run: skaffold dev` |
| `devspace.yaml` | DevSpace | `managed by DevSpace — run: devspace dev` |
| `k3d-*.yaml` or `.k3d/` | k3d (local k8s) | `managed by k3d — run: k3d cluster start` |
| `ctlptl` config | ctlptl | `managed by ctlptl — run: ctlptl apply` |

For Tilt/Skaffold/DevSpace/k3d projects: do NOT install docker-compose via Flox.
Flox's role is the developer toolchain (runtimes, CLI tools) — the orchestrator owns services.

### Services deferred to docker-compose — wire the hook

Applies to services you are NOT wiring as `[services.*]`: the genuinely
absent-from-catalog ones (Zookeeper, Cassandra) and the present-but-coupled
ones deferred by the rules above (e.g. ClickHouse, Kafka). Wire them so
`flox activate` still starts everything:

1. Install `docker-compose` via Flox (it IS in the catalog)
2. Add an on-activate hook that starts those services if Docker is available

```toml
[install]
docker-compose.pkg-path = "docker-compose"

[hook]
on-activate = """
  if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
    docker-compose up -d 2>&1 | tail -5 >&2
  else
    echo "⚠  Docker not running — start Docker Desktop then re-activate" >&2
  fi
"""
```

For selective startup: `docker-compose up -d clickhouse kafka`

Report these in ⚠ (neutral): `<service> starts via docker-compose on activate — requires Docker Desktop running`

### Verify each package

```bash
flox search --all "<pkgname>" 2>/dev/null | head -10
# or if flox-mcp is present: search_packages(search_term="<pkgname>", limit=10)
```

Print one line per package:
```
Checking catalog...
  python311      ✓
  postgresql_16  ✓
  redis          ✓
  clickhouse     –  (docker-compose, correct)
  wkhtmltopdf    ✗  not found → try: flox search weasyprint
```

- Exact match → install, show ✓
- Close match with different name → install the actual name, note it in report
- No match → do NOT install; list in ✗ section with `flox search <name>` as next step
- Docker-managed → show – (not a failure)

---

## Phase 3: Build the environment

### 3a. Initialize

```
init_new_environment(environment_dir="<absolute-target-dir>")
# or: cd "$TARGET_DIR" && flox init --no-auto-setup
```

`--no-auto-setup` skips interactive prompts since we write the manifest ourselves.
If this flag is unsupported, `flox init` works too — we overwrite the manifest next.

### 3b. Write .flox/env/manifest.toml

Write the complete manifest directly. Use only the validated patterns below.
Do not invent syntax.

**Manifest rules:**
- `schema-version` — use whatever `flox init` generated (e.g. `"1.12.0"`); don't hardcode `"1"`
- Always add `# Generated by /floxify` on the line immediately after `schema-version`
- Omit sections that have nothing in them
- Package format: `<install-id>.pkg-path = "<catalog-name>"` (one entry per line)
- `[vars]` values are LITERAL STRINGS — `$HOME` is the literal text "$HOME", not your home dir
- Dynamic values (computed paths, conditionals) belong in `[hook] on-activate`
- `$FLOX_ENV_CACHE` — per-project local cache, not pushed to FloxHub; use for venvs
- `$FLOX_ENV_PROJECT` — the project root directory
- `[hook] on-activate` runs as Bash; its output goes to stderr
- `[profile]` scripts are sourced into the user's interactive shell — keep fast, use for venv activation
- Services: each service is `[services.<name>]` with `command = "..."` on its own

**Platform-conditional packages** — when a dependency is only relevant on certain
platforms, use the per-package `systems` field to scope it. Never skip it or bury it
in a ⚠ warning just because the current machine can't use it. Another developer on a
different OS will need it.

```toml
[install]
some-package.pkg-path = "some-package"
some-package.systems = ["x86_64-linux", "aarch64-linux"]  # Linux-only
other-package.pkg-path = "other-package"
other-package.systems = ["aarch64-darwin", "x86_64-darwin"]  # macOS-only
```

Valid system values: `"aarch64-darwin"`, `"x86_64-darwin"`, `"aarch64-linux"`, `"x86_64-linux"`.
Omit `systems` entirely for packages that work on all platforms.

**How to recognize a platform mismatch:**
- The build system (CMakeLists.txt, Makefile) gates a dep on an OS check
- The dep name signals a platform-specific API (Linux kernel interfaces, macOS frameworks)
- `flox search` finds the package but it only appears in Linux or Darwin catalog entries
- The dep is present in a CI matrix only for certain OS runners

**Decision rule:** Search for the package first. If it's in the catalog, add it with the
appropriate `systems` filter. If it's not in the catalog at all, then put it in ✗.
The only reason a platform-specific dep goes in ⚠ is when there is genuinely no Flox
equivalent (e.g. macOS system frameworks like CoreFoundation or Security.framework that
are provided by the OS itself, not installable).

---

### Hook snippets by ecosystem

The `[install]` section is built from Phase 2 package names. The non-obvious parts are
the `[hook]` and `[profile]` content — use these snippets verbatim and compose them
when the project has multiple stacks (e.g. Python + Node).

**Python**
```toml
[hook]
on-activate = """
  if [ ! -d "$FLOX_ENV_CACHE/venv" ]; then
    uv venv "$FLOX_ENV_CACHE/venv" >&2
  fi
  (
    source "$FLOX_ENV_CACHE/venv/bin/activate"
    uv pip install --quiet -r requirements.txt
  )
"""

[profile]
bash = 'source "$FLOX_ENV_CACHE/venv/bin/activate"'
zsh  = 'source "$FLOX_ENV_CACHE/venv/bin/activate"'
fish = 'source "$FLOX_ENV_CACHE/venv/bin/activate.fish"'
```
- NEVER use `./venv` or `./.venv` — always `$FLOX_ENV_CACHE/venv`
- `uv.lock` present → replace `uv pip install -r requirements.txt` with `uv sync`
- `pyproject.toml` only → `uv pip install -e .`
- `poetry.lock` → add `poetry` to `[install]`, use `poetry install`

**Node**
```toml
[hook]
on-activate = """
  if [ ! -d node_modules ] || { [ -f package-lock.json ] && [ package-lock.json -nt node_modules ]; }; then
    npm install --silent
  fi
"""
```
- pnpm → `pnpm-lock.yaml` staleness check, `pnpm install --frozen-lockfile --silent`
- yarn → `yarn.lock` staleness check, `yarn install --silent`

**Pinned package manager (`packageManager` field / `engines.pnpm`).** When the
repo pins an exact pnpm/yarn (`"packageManager": "pnpm@10.24.0"`), do NOT install
a catalog `pnpm` — the catalog's `pnpm_<major>` floor can exceed the pin, and an
`.npmrc` `engine-strict=true` will then reject it. Provision the exact version
with **corepack** (ships with the `nodejs` package) into a *writable* cache dir
(the Nix node prefix is a read-only store path):

```toml
[hook]
on-activate = """
  export COREPACK_HOME="$FLOX_ENV_CACHE/corepack"
  mkdir -p "$FLOX_ENV_CACHE/node-bin"
  corepack enable --install-directory "$FLOX_ENV_CACHE/node-bin" pnpm
  export PATH="$FLOX_ENV_CACHE/node-bin:$PATH"
  pnpm install --frozen-lockfile
"""
```

**Go**
```toml
[hook]
on-activate = """
  export GOPATH="$FLOX_ENV_CACHE/go"
  export GOCACHE="$FLOX_ENV_CACHE/go/cache"
  export GOMODCACHE="$FLOX_ENV_CACHE/go/pkg/mod"
  mkdir -p "$GOPATH" "$GOCACHE" "$GOMODCACHE"
"""

[profile]
bash = 'export GOPATH="$FLOX_ENV_CACHE/go"; export PATH="$GOPATH/bin:$PATH"'
zsh  = 'export GOPATH="$FLOX_ENV_CACHE/go"; export PATH="$GOPATH/bin:$PATH"'
```

**Rust**
```toml
[hook]
on-activate = """
  export CARGO_HOME="$FLOX_ENV_CACHE/cargo"
  export CARGO_TARGET_DIR="$FLOX_ENV_CACHE/target"
  mkdir -p "$CARGO_HOME" "$CARGO_TARGET_DIR"
"""

[profile]
bash = 'export CARGO_HOME="$FLOX_ENV_CACHE/cargo"; export PATH="$CARGO_HOME/bin:$PATH"'
zsh  = 'export CARGO_HOME="$FLOX_ENV_CACHE/cargo"; export PATH="$CARGO_HOME/bin:$PATH"'
fish = 'set -x CARGO_HOME "$FLOX_ENV_CACHE/cargo"; fish_add_path "$CARGO_HOME/bin"'
```
- `rust-toolchain.toml` is a rustup directive — Flox installs via catalog, not rustup
- Maturin (Python extension): also add `maturin` to `[install]` + Python runtime

**Elixir**
```toml
[hook]
on-activate = """
  export MIX_HOME="$FLOX_ENV_CACHE/mix"
  export HEX_HOME="$FLOX_ENV_CACHE/hex"
  mkdir -p "$MIX_HOME" "$HEX_HOME"
  mix local.hex --force --if-missing >&2
  mix local.rebar --force --if-missing >&2
  [ -f mix.exs ] && mix deps.get --quiet >&2
"""

[profile]
bash = 'export MIX_HOME="$FLOX_ENV_CACHE/mix"; export PATH="$MIX_HOME/escripts:$PATH"'
zsh  = 'export MIX_HOME="$FLOX_ENV_CACHE/mix"; export PATH="$MIX_HOME/escripts:$PATH"'
```
- Erlang/OTP is bundled in the `elixir` package — do NOT add `erlang` separately
- Phoenix: also detect `assets/package.json` and add the Node hook above

**.NET**
```toml
[hook]
on-activate = """
  export DOTNET_ROOT="$FLOX_ENV_CACHE/dotnet"
  export NUGET_PACKAGES="$FLOX_ENV_CACHE/nuget"
  mkdir -p "$DOTNET_ROOT" "$NUGET_PACKAGES"
"""

[profile]
bash = 'export DOTNET_ROOT="$FLOX_ENV_CACHE/dotnet"; export PATH="$HOME/.dotnet/tools:$PATH"'
zsh  = 'export DOTNET_ROOT="$FLOX_ENV_CACHE/dotnet"; export PATH="$HOME/.dotnet/tools:$PATH"'
```
- Don't auto-run `dotnet restore` on activate — it's slow; let the developer run it

---

**Pattern: PostgreSQL as a Flox service**

Use ONLY when docker-compose does NOT already manage postgres.

```toml
[install]
postgresql_16.pkg-path = "postgresql_16"

[vars]
PGHOST = "127.0.0.1"
PGPORT = "5432"
PGUSER = "postgres"
PGDATABASE = "myapp_dev"

[hook]
on-activate = """
  export PGDATA="$FLOX_ENV_CACHE/postgres"
  if [ ! -d "$PGDATA" ]; then
    initdb -D "$PGDATA" \
      --username="$PGUSER" \
      --auth=trust \
      --no-locale \
      --encoding=UTF8 >&2
    pg_ctl -D "$PGDATA" -o "-p $PGPORT -k /tmp" \
      -l "$PGDATA/init.log" start
    until pg_isready -h 127.0.0.1 -p "$PGPORT" -q; do sleep 0.2; done
    createdb -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER" "$PGDATABASE"
    pg_ctl -D "$PGDATA" stop -m fast
  fi
"""

[services.postgres]
command = """
  exec postgres \
    -D "$PGDATA" \
    -p "$PGPORT" \
    -k /tmp \
    -c listen_addresses="$PGHOST"
"""
```

`-k /tmp` puts the Unix socket in `/tmp` (works without root).
`auth=trust` is dev-only — always note in the report: `(dev-only auth — not safe for shared machines)`

Redis service (add alongside postgres when both are needed):

```toml
[install]
redis.pkg-path = "redis"

[vars]
REDIS_PORT = "6379"

[hook]
on-activate = """
  mkdir -p "$FLOX_ENV_CACHE/redis"
"""

[services.redis]
command = """
  exec redis-server \
    --port "$REDIS_PORT" \
    --dir "$FLOX_ENV_CACHE/redis" \
    --save "" \
    --appendonly no
"""
```

`--save ""` and `--appendonly no` disable persistence (dev-appropriate).
Always note in the report: `(no persistence — data resets on service stop; edit manifest if you need durability)`

---

### 3c. Validate and verify

**Hard gate — the report never appears until all three steps pass.**

Print before starting: `Verifying environment... (first run may take 30–60 seconds)`

**Step 1 — Schema + package resolution**

```bash
cd "$TARGET_DIR" && flox activate -c "echo __ok__" 2>&1
```

- `__ok__` in output → proceed to Step 2
- Error → **stop**. Read the error message, fix `manifest.toml`, re-run Step 1.
  Common causes: wrong `pkg-path` name (re-search Phase 2), invalid TOML syntax,
  unknown field name. Do not show the report until this passes.

**Step 2 — Runtime versions + hook execution**

```bash
cd "$TARGET_DIR" && flox activate -c "
  python --version 2>/dev/null || true
  node --version 2>/dev/null || true
  go version 2>/dev/null || true
  cargo --version 2>/dev/null || true
  elixir --version 2>/dev/null | head -1 || true
  dotnet --version 2>/dev/null || true
" 2>&1
```

Read stderr for hook errors (`uv sync` failures, `pnpm install` errors, etc.).
If the hook fails → fix the hook in `manifest.toml`, re-run from Step 1.
Use the version numbers printed here for the ✓ Runtime section of the report.

**Step 3 — Functional import check (Python native deps only)**

Run only when the project has native Python deps (`psycopg2`, `cryptography`, `lxml`,
`xmlsec`, `Pillow`, etc.):

```bash
cd "$TARGET_DIR" && flox activate -c "
  python -c 'import psycopg2' 2>/dev/null && echo 'psycopg2 ✓' || echo 'psycopg2 ✗ — missing native dep'
  python -c 'import cryptography' 2>/dev/null && echo 'cryptography ✓' || echo 'cryptography ✗'
  python -c 'import lxml' 2>/dev/null && echo 'lxml ✓' || echo 'lxml ✗'
" 2>&1
```

A `✗` import means a missing system lib — add it to `[install]` and re-run from Step 1.

---

## Phase 4: The report

Print this immediately after setup. This is what gets shared and screenshotted.

```
╭─────────────────────────────────────────────────────────────────╮
│  <project-name>  is now Flox-enabled                            │
╰─────────────────────────────────────────────────────────────────╯

✓  Flox manages  (<N> packages — pinned, same on every machine)
   python 3.12.13    ← Dockerfile.dev
   node 22.22.3      ← engines.node >=22.18.0
   pnpm 11.3.0       ← packageManager field
   uv · pkg-config · openssl · libxml2 · libxslt · docker-compose

✓  Installs automatically on activate
   uv pip install    → django 4.2.30, celery, psycopg, redis + 39 others
   pnpm install      → all workspaces (web, admin, space, live)

⚠  Services  (start automatically when Docker Desktop is running)
   plane-db     postgres 15.7
   plane-redis  valkey 7.2
   plane-mq     rabbitmq 3.13
   plane-minio  minio

✗  Needs attention
   <item>  → <what to do>

─────────────────────────────────────────────────────────────────

What changed:  .flox/env/manifest.toml  created  ·  nothing else touched
```

**Report rules — all mandatory:**

- ✓ Runtime shows ACTUAL version numbers from Phase 3c verification, not detected versions.
  Source attribution on every line: `← .nvmrc`, `← .python-version`, `← Dockerfile.dev`
  Patch-version mismatches go in the attribution, not ✗: `← .nvmrc (project pins 24.14.0; catalog has 24.13.0)`
- ✓ Installs lists what ecosystem PMs will install (pip/pnpm/cargo) with real package names
  and count. Never say these are "managed by Flox" — they run inside the Flox environment.
- ⚠ Services: for docker-compose-managed services, list what auto-starts and when. For custom
  orchestrators (devservices, tilt, skaffold), list services and name the tool:
  `managed by devservices — run: devservices up`. Neutral tone in both cases — not a gap.
- ✗ Only for things that genuinely need user action. Omit section entirely if empty.
- Omit any section that has nothing in it (no empty ✗ section with placeholder text).
- Exact counts: "47 packages" not "several packages"
- "What changed" is mandatory every time — always the last line of the report box
- Never say `flox login` — correct command is `flox auth login`
- Never say `flox activate <user>/<name>` — correct syntax is `flox activate -r <user>/<name>`
- No Flox-internal jargon: no "derivation", "store path", "attribute path", "Nix"

**After printing the report:**

If a prior tool was detected (DevBox, Mise, Brewfile, Dev Container), print one line:
```
Note: <tool> still works alongside — remove it only when you're ready.
```

Then ask:

```
What would you like to do next?

  1  Try it     →  run flox activate now and see it work
  2  Migrate    →  update the README, create a branch, commit
  3  Leave it   →  I'll come back when I'm ready
  4  Remove it  →  clean slate, nothing else was touched
```

Wait for the user's response, then:

- **1 (Try it):** Run:
  ```bash
  cd "$TARGET_DIR" && flox activate -c "
    echo '✓ environment active'
    python --version 2>/dev/null || true
    node --version 2>/dev/null || true
    which python 2>/dev/null || true
  " 2>&1 | grep -v 'FloxHub token'
  ```
  Show the output so the developer sees their runtimes live inside the environment.
  Then say: "That's your environment working. Run `flox activate` any time to enter it.
  When you're ready to commit this, say **migrate** — but only in this conversation,
  since that's where the context lives."
- **2 (Migrate):** Proceed directly to Phase 5.
- **3 (Leave it):** Say: "No problem — run `flox activate` whenever you're ready. Say 'migrate' to commit it."
- **4 (Remove it):** Run `rm -rf "$TARGET_DIR/.flox/"`, confirm it's gone:
  "Done. Zero trace — nothing else was touched."

---

## Edge cases

**Monorepo** (pnpm-workspace.yaml, nx.json, turborepo.json, multiple go.mod files):
Set up root environment with shared runtimes. Note in report:
`This is a monorepo. Root environment covers shared runtimes.
Individual services may benefit from their own — run /floxify <service-path>`

**Large pip dependency count (100+):**
Note: `First activate installs <N> pip packages — takes a few minutes.
Subsequent activates check the lockfile and skip if nothing changed.`

**Package not found in Flox catalog:**
In ✗ section:
```
<package>   not in Flox catalog
  → Run: flox search <name>  to check for alternative names
  → Or install via system package manager and document it in README
```
Never pretend a package was added if search returned no results.

**Docker-compose-managed services (ClickHouse, Kafka, Temporal, etc.):**
Wire via the docker-compose pattern in Phase 2. List in ⚠ Services section only —
not in ✓ Runtime or ✓ Installs. Never claim these are managed by Flox directly.

---

## Phase 5: Migration (triggered on demand)

**Trigger:** User says "migrate", "I'm ready", "commit it", "let's go", "yes migrate",
"do it", or any clear affirmation after the Phase 4 report.

**Do NOT run automatically.** Only execute when explicitly requested.

### Steps

**1. Create a branch**

```bash
cd "$TARGET_DIR"
# Check if branch already exists
git show-ref --verify --quiet refs/heads/add-flox-environment \
  && echo "BRANCH_EXISTS" || echo "BRANCH_NEW"
```

- `BRANCH_NEW` → `git checkout -b add-flox-environment`
- `BRANCH_EXISTS` → ask the user: "Branch `add-flox-environment` already exists. Switch to it
  and continue, or use a different name?" Do not switch automatically.

**2. Update the README**

Find the README: `README.md` → `README.rst` → `docs/CONTRIBUTING.md` → `CONTRIBUTING.md`
(first one found). If none exist, create a minimal `README.md` with just the Flox section.

**Most repos already have a README — read it first, then make the smallest possible change.**

Look for a heading containing any of these words (case-insensitive):
"getting started", "development", "setup", "local development", "contributing", "install",
"quickstart", "prerequisites", "usage"

- **Section found**: INSERT these two lines at the top of that section's content,
  before any existing text. Do not remove or rewrite existing instructions.

  ```markdown
  Run `flox activate` to set up your development environment — it installs all runtimes
  and dependencies automatically. ([Install Flox](https://flox.dev/docs/install-flox/install))
  ```

  If the old tool (DevBox, Mise, etc.) has a specific install command in that section,
  replace just that command line. Leave everything else untouched.

- **No matching section found**: INSERT a new `## Getting started` section after the
  first paragraph (after the project description, before any other sections).

  ```markdown
  ## Getting started

  Run `flox activate` to set up your development environment — it installs all runtimes
  and dependencies automatically. ([Install Flox](https://flox.dev/docs/install-flox/install))
  ```

Never rewrite or restructure the README beyond the single insertion point.

**3. Remove old tool config** (context-dependent — never auto-remove, always confirm)

| Detected tool | Action |
|---------------|--------|
| `devbox.json` | Ask: "Remove devbox.json? Flox replaces it completely." If yes: `git rm devbox.json`; also remove `.devbox/` if present |
| `.mise.toml` | Ask: "Remove .mise.toml?" If yes: `git rm .mise.toml` |
| `.tool-versions` | Ask: "Remove .tool-versions? (asdf/mise pin file)" If yes: `git rm .tool-versions` |
| `Brewfile` | Do NOT remove — Homebrew is system-wide. Note: "Brewfile left in place (Homebrew is system-wide; Flox handles project deps)" |
| `.devcontainer/` | Do NOT remove — serves Codespaces/cloud CI. Note: ".devcontainer/ left in place for cloud/Codespaces use" |
| None | Nothing to remove |

**4. Update .gitignore**

Check whether `.flox/cache/` is already gitignored:
```bash
grep -q 'flox/cache' "$TARGET_DIR/.gitignore" 2>/dev/null && echo "ALREADY_IGNORED" || echo "NEEDS_ENTRY"
```

If `NEEDS_ENTRY`: append to `.gitignore`:
```
# Flox local cache (venvs, build artifacts — not shared)
.flox/cache/
```

The `.flox/env/` directory (manifest, lockfile) IS committed — that's the point.
The `.flox/cache/` directory (venvs, cargo target, etc.) is machine-local and should not be.

**5. Stage and commit**

```bash
cd "$TARGET_DIR"
git add .flox/env/
git add .gitignore
git add README.md   # (or README.rst, CONTRIBUTING.md — whichever was modified)
# If old tool files were git rm'd, they're already staged
git commit -m "Add Flox development environment"
```

**6. Print migration summary**

```
─────────────────────────────────────────────────────────────────

✓  Committed  (branch: add-flox-environment)

   Modified:  README.md  ← updated dev setup section
   Added:     .flox/env/manifest.toml
   Removed:   devbox.json        ← (or "left in place" for Brewfile / devcontainer)

   Commit:    "Add Flox development environment"

Next steps:
  git push -u origin add-flox-environment
  → open a PR — teammates can try it before it merges

  flox push
  → share environment on FloxHub (optional)
  → teammates: flox activate -r <you>/<project-name>
  → first time? flox auth login

  In CI (GitHub Actions, etc.):
  → install Flox, then: flox activate -- <your-test-command>
  → see: https://flox.dev/docs/install-flox/install

─────────────────────────────────────────────────────────────────
```

Then ask: "Ready to push to origin? I can run `git push -u origin add-flox-environment`."

### Migration rules

- Never `git push` without explicit user confirmation
- Never remove Brewfile or `.devcontainer/` — they serve different purposes
- Always confirm before `git rm` on any file
- If git is not initialized (`git status` fails): skip branch creation, just update
  the README and note: "No git repo found — commit manually when ready"
- Commit message is always exactly `"Add Flox development environment"` — no variations
