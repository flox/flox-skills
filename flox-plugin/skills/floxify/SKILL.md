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

**Delegating to a cheaper model?** If you are the parent session deciding
whether to hand this conversion to a cheaper-model subagent, read
`references/delegation.md` first. **If you are the delegated subagent, skip
straight to Phase 0** — the verify gate, not this note, carries correctness.

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

**If `HAS_FLOX`:** Switch to audit mode — do NOT initialize or modify anything.
Read `references/conversion-modes.md` § Audit Mode and follow it.

**If `devbox.json` exists (and no .flox/):** Switch to DevBox conversion mode.
Read `references/conversion-modes.md` § DevBox Conversion Mode and follow it.

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

## Phase 1: Read the project

Ground every version and service in a file — never guess. Run the bundled
analyzer first (Step 1a): it reads the pin files, lockfiles, and
docker-compose services deterministically. Then read the high-signal files
yourself (Step 1b) for nuance it only summarizes.

### Step 1a — Run the grounded analyzer (do this first)

The analyzer ships with this skill at `scripts/detect.py` (next to this
SKILL.md). Run it through Flox so you don't depend on a system Python — this is
also the fastest way to run a one-off script. Save its output alongside
printing it — Phase 3c's `verify.py` re-reads these same facts to check the
manifest you eventually write against them:

```bash
DETECT_JSON="/tmp/floxify-detect.json"   # one floxify run at a time; fine to reuse
flox run -p python313 -- python3 "<skill-dir>/scripts/detect.py" "$TARGET_DIR" | tee "$DETECT_JSON"
```

`<skill-dir>` is this skill's own directory — the folder that holds this
SKILL.md (the same place you'd read `scripts/` or a reference file from). Use
its absolute path. `$DETECT_JSON` is a fixed path — remember it verbatim for
Phase 3c, since each Bash call starts a fresh shell and cannot inherit a
variable from this step.

- If `flox run` errors with an unknown-subcommand or usage message, the user's
  Flox predates 1.13 (`flox run` shipped in 1.13). Tell them once, plainly:
  "Your Flox is older than 1.13, so I can't use the fast analyzer — upgrade to
  get `flox run`: https://flox.dev/docs/install-flox/install". Then fall back to
  `python3 "<skill-dir>/scripts/detect.py" "$TARGET_DIR" | tee "$DETECT_JSON"` if
  a `python3` is on PATH, and if neither works, scan manually (Step 1b) and skip
  the `$DETECT_JSON` file entirely — Phase 3c's verify.py runs with reduced
  coverage (no detect facts to cross-check) but never blocks on a missing file.
  The analyzer is an accelerator, not a hard dependency — never block on it.

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

1. `.devcontainer/devcontainer.json` — full conversion: `image`, `features`, `postCreateCommand`, `containerEnv` (see `references/conversion-modes.md` § Dev Container Full Conversion)
2. `devbox.json` — if present, handled via `references/conversion-modes.md` § DevBox Conversion Mode (skip to Phase 3)
3. `Brewfile` — `brew "name"` lines mapped to Flox catalog (see `references/conversion-modes.md` § Brewfile Conversion Mode)
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
- **Mise / asdf** (`.mise.toml` / `.tool-versions`): each `key = "version"` is an independent search; `python = "3.12.3"` → search `"python 3.12"`. These patch pins routinely sit AHEAD of the catalog — resolve the major/minor, and if the exact version isn't available, fall back to the language manifest's floor (`mix.exs "~> 1.18"`, `go.mod`, `requires-python`) and note the gap; never force a nonexistent exact pin. If the exact patch IS available, emit `<id>.version` for it — see "Emitting an exact pin" below
- **Conda** (`environment.yml`): search top-level `dependencies:` binaries only; skip `- pip:` entries (handle via uv)
- **Volta** (`"volta": {"node": "22.4.0"}`): search `"nodejs 22"`
- **packageManager field** (`pnpm@9.x`, `yarn@4.x`): search the package manager name; when the pin is an exact version, see "Pinned package manager" and "Emitting an exact pin" below for whether it resolves directly, needs corepack, or (Yarn Berry) self-delegates via `.yarn/releases/`
- **`bun.lockb` present**: search `"bun"` and use it instead of nodejs

### Picking from search results

1. Prefer the most specific versioned name (`python312` over `python`)
2. For unversioned tools, pick the plain name (`redis`, `cmake`, `jq`)
3. If ambiguous, read the description in results to confirm intent
4. If no match after 1–2 attempts: add to ✗ section, don't install

### Reading `flox show` correctly

Wrong catalog claims in review (AI-451's false-hallucination accusation,
AI-455's per-system misses) all traced to one root cause: reading only the
`Latest:` headline and stopping. `flox show <pkg-path>` prints far more —
read all of it before asserting anything about a package:

1. **Read the FULL version list, not just `Latest:`.** The `Other versions:`
   block is the actual catalog, often 20+ entries deep. A version the project
   pins (`elixir_1_19@1.19.5`, say) can be correct and present even when it
   isn't the newest entry — accusing it of being hallucinated because it
   doesn't match `Latest:` is exactly AI-451's mistake. Scroll the whole list
   before concluding a version doesn't exist.
2. **Check the systems annotation for the SPECIFIC version you're pinning,
   not the top-of-file `Systems:` line.** That top line describes `Latest:`
   only. Each `Other versions:` entry carries its own systems: no
   parenthetical means all four platforms; `(sys1, sys2 only)` restricts it
   to exactly those. A version can lose (or never have had) a build for a
   platform the top-line summary doesn't reflect — e.g. `nodejs_24`'s newest
   build lacks `x86_64-darwin` even when older 24.x builds have it. If
   `options.systems` (or the package's own `systems` override) declares a
   platform, verify the PINNED version's own parenthetical covers it —
   `verify.py`'s catalog check (Phase 3c Step 4) automates exactly this, but
   read it yourself here too rather than relying on it to catch a bad pin
   after the fact. A per-system availability hold discovered here is one of
   the two legitimate recorded-reason categories in "Version-pinning
   discipline" below — record it the same way.
3. **Query the versioned `pkg-path` directly — never infer a ceiling from the
   bare name.** `flox show <versioned>` (`flox show ruby_4_0`, `nodejs_24`,
   `go_1_23`, `python313`) is authoritative for a pinned runtime. The bare
   `flox show ruby` may report a *lower* ceiling that belongs to a different
   catalog entry — trusting it silently downgrades the runtime. Mastodon
   pins Ruby 4.0.6: `flox show ruby` tops out at 3.4.x, but the versioned
   `ruby_4_0` page carries the 4.x line the bare name doesn't reach at
   all — verify live whether it reaches the repo's exact patch or only the
   nearest prior one (same live-verify discipline as "Emitting an exact
   pin" below; the catalog moves forward, so don't trust a number cited
   elsewhere in this guidance over today's `flox show`). Search the
   versioned `pkg-path` first; fall back to the bare name only for
   genuinely unversioned tools.
4. **When the question is package CONTENTS (does this build include a given
   extension/module?), don't infer it from the name — execute it.**
   `flox show` describes outputs and versions; it does not enumerate what's
   compiled into a package. `flox run -p php85 -- php -m` lists PHP's actual
   loaded modules; the equivalent for any interpreter/toolchain is to run it
   and ask, not to guess from the catalog description.

The version-string format matters too: some catalog entries carry a package-
specific prefix that doesn't match the bare version a human would write
(`python313`'s versions read `python3-3.13.13`, not `3.13.13`) — pin the
exact string `flox show` prints, not a normalized guess. `verify.py`'s
catalog check treats a mismatched prefix as a real, non-resolving pin,
because it is one.

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

**HARD FLOOR — every leaf datastore the app needs at runtime gets a
`[services.*]` block.** If the app will not run without a datastore — its
config, `.env.example`, or your own `[vars]` name a `DATABASE_URL` /
`REDIS_URL` / host+port — then that datastore MUST be installed **and** wired
as a Flox service. Not "consider", not "prefer". A manifest that advertises an
endpoint nothing serves is broken: `flox activate` exits 0 and the developer
still has no database. If you are about to emit `[vars]` pointing at a
datastore with no matching `[services.*]`, stop — that is the bug.

**The repo already having a way to start it is NEVER a reason to defer.** A
`scripts/start_dev_db.sh`, a `make postgres` target, a `docker run` recipe, a
compose service, a README step — every project has one of these. That manual
step is *the reason floxify was invoked*; it is not an orchestrator to hand the
work back to.

**Launcher intricacy is not a licence either.** When the repo's launcher does
fiddly setup — a cluster under `./target`, a unix socket inside the repo tree, a
percent-encoded socket path in `DATABASE_URL`, loading `db/schema.sql` — **read
it and port those steps into the service command and hook.** That is the work.
"The script does something clever I can't reproduce" is an argument for reading
it more carefully, not for leaving the developer with nothing. If one detail
genuinely cannot be reproduced, wire the service anyway and note the divergence
in ⚠.

Deferring also **cascades** into the rest of the manifest. Because lemmy's
`start_dev_db.sh` puts its cluster under `$PWD/target`, deferring to it dragged
`CARGO_TARGET_DIR` into the repo tree as well — one deferral, two defects.

**When the app needs a leaf datastore (the HARD FLOOR above), read
`references/service-patterns.md`** for the PostgreSQL (socket-default) and
Redis (TCP+socket) manifest patterns before wiring `[services.*]`.

**Catalog presence does NOT mean "wire it as a Flox service."** The floor above
covers the *leaf* datastores the app depends on directly (usually `postgres`,
`redis`, `mariadb`). Everything else is a judgement call: a service can exist in
the catalog and still be the wrong thing to run as a bare `[services.*]`. Defer
a **non-leaf** service to docker-compose or the project's own orchestrator when
any of these hold:

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

**In a multi-stage Dockerfile, attribute each `RUN apt-get install` to its
`FROM … AS <stage>`.** Packages installed in a *builder* stage are build deps;
packages in the *runner* stage are runtime-only and do NOT imply a build input.
Lemmy's runner-stage `libssl-dev` does not mean openssl is a build dep — its
`Cargo.lock` has no `openssl-sys`. Don't promote a runner-stage lib to `[install]`
on the strength of an `apt-get` line alone.

**A native library's `outputs` are not always installed by default — check
before assuming headers or a shared lib are present.** `flox show <pkg-path>`
prints an `Outputs:` line (`dev, doc, jit, lib, man*, out*, ...` — `*` marks
what installs by default). When a package feeds a native build — a
C-extension gem (`ruby-vips`, `charlock_holmes`), a Rust `*-sys` crate
(`pq-sys`), a Python native wheel built from source (non-binary `psycopg2`,
`lxml`) — the compiler needs headers that live in the `dev` output, and that
output is frequently NOT starred as default. Worse, a package's default
outputs can omit the piece the build actually links against: `vips`'
`Outputs:` line is `bin*, dev, man*, out` — `out` (which holds `libvips.so`)
is NOT starred, so a plain `vips.pkg-path = "vips"` install is missing the
shared library a native build needs. When you find this, add
`<id>.outputs = ["out", "dev"]` (or `"all"`) to the `[install]` entry rather
than assuming the default set is enough. `verify.py`'s outputs heuristic
(Phase 3c Step 4) flags exactly this shape as an ADVISORY note when the
analyzer's `native_hints` name a package with no `outputs` declared — read
the note, but the underlying check (`flox show <pkg-path>`'s `Outputs:`
line) is the same one you'd run by hand.

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

**Curb dev-tooling scope creep — install for BUILD and RUN, not CI parity.**
`[install]` exists to make the project buildable and runnable, plus its
declared native chain (Dockerfile/Aptfile/`*-sys` crates and the like) — not
to replicate everything a CI matrix, `Makefile`, or deferred script happens
to invoke. A judge reviewing a live floxify run called an over-broad
install list "defensible CI parity, but none substitute for the missing
service" — CI parity is not the goal; a working local build+run is.

The carve-out that keeps this from over-correcting: **toolchain-standard
lint/format tooling is in scope; third-party auxiliary tooling is opt-in.**
Lemmy's golden (`evals/floxify/expected/lemmy.toml`) installs seven
packages total — `cargo`/`rustc`/`postgresql_18`/`pkg-config`/`gcc` build
and run `lemmy_server` directly, and `clippy`/`rustfmt` round out the seven
because they're the Rust toolchain's OWN lint/format tools (driven by
`.woodpecker.yml`'s `cargo clippy`/`cargo fmt` steps and `.rustfmt.toml`,
per lemmy-notes.md) — installing them is installing more of the same
toolchain, not scope creep. A third-party formatter or linter with no
toolchain relationship to the runtime being installed (a standalone binary
like `taplo`, `typos`, or `shfmt`) is a different case — CI-only, opt-in,
and belongs in the conversion report rather than `[install]`.

- **CI-only third-party lint/format tooling** (`taplo`, `typos`, `shfmt`,
  `pgformatter`, and similar standalone tools with no toolchain
  relationship to the runtime you're installing) is opt-in per the
  carve-out above — mention it in the conversion report instead of adding
  it to `[install]`, so the developer can bring it in themselves.
- **Don't install `git` as an env dependency** unless a build step
  genuinely shells out to it — a `build.rs` that clones a dependency, or a
  `git+` source in `Cargo.lock`/the lockfile. Lemmy has zero `git+` sources
  in `Cargo.lock` (confirmed in lemmy-notes.md) and modern cargo uses the
  sparse crates.io index, not git — a CI runner installing `git` to check
  out the repo, or a Dockerfile installing it in the runtime image, is not
  evidence the BUILD needs it — same builder-vs-runner-stage distinction as
  the Dockerfile rule above.
- **A tool referenced only by a script the Flox service replaces doesn't
  carry over.** Lemmy's `jq` existed solely to URL-encode a socket path in
  `scripts/start_dev_db.sh` (per lemmy-notes.md); once `[services.postgres]`
  wires the DB directly (the hard floor above), that script — and its
  dependency — is no longer in the loop. Don't install a deferred script's
  own dependencies once you've stopped deferring to it.

### Custom service orchestrators

**This section is about services the orchestrator genuinely owns — never the
leaf datastores.** A Tilt/Skaffold/k8s topology, or a store reached only through
another service's graph, belongs to the orchestrator. A plain `postgres` that a
`devservices/config.yml`, `Makefile` target, or shell script happens to launch
does **not** — that is a leaf datastore and the hard floor above applies: wire
it. Sentry is the worked example: `devservices` owns snuba → ClickHouse/Kafka
(defer those), but `shared-postgres`/`shared-redis` are direct leaf deps and
must still be wired as Flox services. Do not read "the project has an
orchestrator" as "the project's datastores are not my problem."

If the project uses a custom tool to manage its **non-leaf** services and there is
**no `docker-compose.yml`** at the root, do NOT try to wire those via docker-compose.
List them in ⚠ with the tool name and the command to start them. Don't claim these are
a gap.

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
on-activate = '''
  if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
    docker-compose up -d 2>&1 | tail -5 >&2
  else
    echo "⚠  Docker not running — start Docker Desktop then re-activate" >&2
  fi
'''
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
- Services: each service is `[services.<name>]` with `command = '...'` on its own
- `[hook] on-activate` and `[services.*] command` use **literal** strings —
  `'''…'''` for multi-line, `'…'` for one-line — never TOML's basic
  `"""…"""`/`"…"` strings (see "TOML string types" below)

**TOML string types — literal, not basic, for shell content.** Basic
strings (`"""…"""`, `"…"`) are escape-processed: a shell line-continuation
backslash, or a literal `\d`/`\.` inside a path or regex, gets consumed as a
TOML escape sequence before the shell ever sees it, silently truncating or
corrupting the command. Literal strings (`'''…'''`, `'…'`) leave backslashes
and `$` completely inert, so the shell script reads exactly as written.
Every `[hook] on-activate` and `[services.*] command` this skill's own
patterns emit uses `'''…'''` for this reason — e.g.
`evals/floxify/expected/node-postgres.toml`'s `on-activate` and `command` blocks
are both `'''…'''` (that file's postgres service still uses the old TCP
default this same PR replaces — cited here only for its string type, not
its socket/TCP shape; see the PostgreSQL pattern in
`references/service-patterns.md` for the current default). Not every `expected/*.toml` reference
follows the literal-string rule yet — `firefly-iii.toml`, `lemmy.toml`, and
`supabase.toml` still carry a basic-string block each — that gap is a
separate, pre-existing golden defect (tracked outside this guidance-only
change), not something this rule claims is already universal.

**Pkg-group economy — fewest groups possible is a first-order goal.** Every
distinct `pkg-group` is a distinct catalog page, and every page downloads its
own full transitive closure down to libc — an extra group is potential
duplicated download cost, not just an organizational nicety. It also forfeits
version coherence for compiled extensions: a runtime's C extensions compile
against the headers on its OWN page and load libraries from it at runtime, so
splitting a runtime from the native libraries its extensions link against
risks a version mismatch between the two pages that the activation smoke test
cannot catch (nothing there loads the extension and checks the symbols
resolve).

When pins cannot co-resolve in one group ("constraints for group 'X' are too
tight" from `flox activate`), work the escalation ladder in order — do not
jump straight to isolating a package:

1. **FIRST — pin the toolchain, unpin the libraries.** Keep the top-level
   runtime/toolchain pinned exactly (that's usually the one with a real
   provenance source — `.nvmrc`, `rust-toolchain.toml`, `requires-python`) and
   drop the exact `version` pin on the OTHER packages that must stay
   compatible with it, letting them float within the same group. This is the
   cheapest fix and preserves the single-group economy. **The final
   user-facing report MUST carry a caveat naming the libraries left unpinned
   for compatibility** (Phase 4 "Installs" or a dedicated report line) — an
   unpin is a real trade-off the developer should see, not a silent
   workaround.
2. **SECOND — split along dependency seams.** If step 1 still doesn't
   co-resolve, split by seam, not by package: a runtime and ALL of its
   native build deps move together as one cluster into their own group,
   never separated from each other. A second runtime with no native-linkage
   coupling to that cluster (e.g. a Node frontend build alongside a Ruby
   backend with C-extension gems) can get its own group without forfeiting
   anything, since there's no ABI relationship to protect.
3. **LAST — isolate a single package.** Only when co-resolution has
   demonstrably failed even along a dependency seam AND the package has no
   native-linkage coupling to anything else in the manifest (the diesel-cli
   shape — a standalone tool built with its own feature flags, sharing no
   native ABI surface with the rest of the manifest) does it get isolated
   alone. This is the most expensive rung: it guarantees a dedicated closure
   download for that one package.

Every non-default `pkg-group` gets a comment recording the demonstrated
failure that forced it, date-stamped — not "these might conflict," but "flox
activate confirmed X" with the date the resolution was tested:

```toml
[install]
# 2026-07-17: `flox activate` failed ("constraints for group 'toplevel' are
# too tight") with ruby_4_0 + postgresql_14 + vips + icu + libidn all in
# toplevel. Fix: keep them together in one group (ABI-coherent — ruby's
# C-extension gems compile against these) rather than isolating ruby alone.
ruby.pkg-path = "ruby_4_0"
ruby.pkg-group = "runtime-and-native"
postgresql.pkg-path = "postgresql_14"
postgresql.pkg-group = "runtime-and-native"
vips.pkg-path = "vips"
vips.pkg-group = "runtime-and-native"
```

**Version-pinning discipline — pin only when necessary.** Pins keep an
environment historical and reproducible, but continuous upgrade (the catalog
moving forward under an unpinned package) is a core Flox benefit that an
unnecessary pin forfeits. Default to unpinned; add a pin only when something
in the repo, or the catalog itself, requires it.

Gradation, from least to most consequential — treat each step up as needing
more justification, not more syntax:
- **`>=` floors** are cheap and safe — they express a minimum without
  freezing the ceiling.
- **`<=` ceilings** likely encode a deliberate compatibility decision someone
  made — introduce them with care, and say what they're protecting against.
- **Exact pins** (`version = "24.18.0"`) are the most consequential — doubly
  careful, since they freeze the package to one catalog entry.

**Every pin the skill writes carries its recorded reason** — the requirement
is the recording, not pin abstinence. Legitimate reasons include:
- A repo-derived pin: `rust-toolchain.toml`, `.nvmrc`/`.node-version`,
  `packageManager` (see "Pinned package manager" above), `.python-version`,
  `requires-python`.
- A per-system availability hold — the catalog's newest build lacks a
  platform the manifest targets, so an older version (or the whole package)
  is scoped instead; see "Reading `flox show` correctly" item 2 for how to
  check this and step 1 of the escalation ladder above for the group-level
  version of the same trade-off.

A pin with neither kind of reason recorded next to it is worth a second look
before it ships — either find its provenance or drop it.

**Emitting an exact pin: `<id>.version` alongside `<id>.pkg-path`.** When
the repo pins an exact patch — `.nvmrc`/`.node-version` with a full
`X.Y.Z`, a `packageManager` exact version, `rust-toolchain.toml` with a
dated channel, or a `.tool-versions`/`.mise.toml` entry pinning a patch —
`pkg-path` alone isn't enough: it resolves to whatever the catalog's
current default is for that page, which drifts as the catalog moves
forward. Add `<id>.version = "<exact>"` so the manifest is pinned to the
specific patch, not just the major/minor line. Resolve the value the same
way as any other version check ("Reading `flox show` correctly" above):
read the FULL version list for the versioned pkg-path, and use the exact
string `flox show` prints — **always against a live `flox show`, never a
number remembered from this guidance or an earlier run**, since the
catalog moves forward continuously and yesterday's gap can be today's
exact match (or vice versa).

If the catalog doesn't carry the repo's exact patch, pin the closest
available instead of inventing a `version` value that doesn't resolve, and
say so in the same source-attribution comment the install line already
carries (see "Version mismatches" in Phase 2, and the `packageManager` /
Mise-asdf entries under "Search term strategies" above) — the attribution
convention already exists; emitting the `version` field alongside it
closes that gap. `evals/floxify/expected/mastodon.toml` is the worked
example for the mechanism: `.nvmrc` pins Node `24.18`, the catalog carries
that exact patch, so `nodejs.version = "24.18.0"` is a clean exact match
(live-verified 2026-07-18). `.ruby-version` pins Ruby `4.0.6` — check
whether the versioned `ruby_4_0` pkg-path carries that exact patch or only
the nearest prior one at the time you run the skill, pin whichever `flox
show` confirms, and record any gap in the trailing comment the same way
the mastodon golden's comment does (`# catalog max is <X>; repo pins
4.0.6 (<N> patch(es) ahead, verify live)`) — don't copy the specific
numbers from this guidance as if they were current.

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

After resolving packages (Phase 2), for each detected ecosystem read the
matching section of `references/hook-snippets.md` before writing `[hook]` /
`[profile]`: Python · Node (+ pinned package-manager/corepack) · Go · Rust ·
Elixir · .NET · PHP.

---

### 3c. Validate and verify

**Hard gate — the report never appears until all four steps pass.**

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

**Step 4 — Deterministic manifest check (verify.py)**

Steps 1-3 prove the manifest *activates*. They do not prove it matches what
Phase 1 actually found — a missing service, a `[vars]` value that silently
never expands, a hook that re-mutates the repo's git tree on every activation
can all pass activation cleanly. `verify.py` grounds the OUTPUT the same way
`detect.py` grounded the INPUT: it takes the facts captured in `$DETECT_JSON`
(Step 1a) and the manifest you just wrote, and reports concrete violations
instead of leaving that to the Phase 4 report's own judgment.

```bash
flox run -p python313 -- python3 "<skill-dir>/scripts/verify.py" \
  "$DETECT_JSON" "$TARGET_DIR/.flox/env/manifest.toml"
```

(Confirmed live: `flox run -p <pkg> -- ...` inherits the caller's `PATH`
rather than replacing it, so `flox` itself stays reachable inside this
subshell — the catalog leg (`flox show`) genuinely runs here, not just
when verify.py is invoked directly.)

- Exit 0, "No violations" → proceed to Phase 4.
- Any violation printed → **stop**. Each one names the manifest section and
  the fact it disagrees with, e.g. `client 'pg' (package.json) implies
  postgres, but no [services.*] serves it`. Fix `manifest.toml` for every
  violation, then re-run from Step 1 (a fix can reopen an earlier check) and
  this step again. Do not show the report until verify.py reports zero
  violations.
- Advisory notes (heuristics — e.g. a native build input with no `outputs`
  declared) print separately and never block; read them, but they are a
  second look, not a bug report.
- Same fallback as Step 1a: if `flox run` errors (Flox predates 1.13), fall
  back to `python3 "<skill-dir>/scripts/verify.py" ...` with a system Python.
  If `$DETECT_JSON` doesn't exist (Step 1a's fallback to manual scanning
  skipped writing it), still run verify.py with an empty facts file
  (`echo '{}' > /tmp/floxify-detect.json`) — the catalog/vars/hook checks
  that don't need detect facts still run.
- `verify.py` checks *consistency with what detect.py found*, not
  *correctness* — it says so in its own output. A clean run is not a
  certification the manifest is bug-free; it means nothing detect.py grounded
  contradicts it.

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
- ⚠ If the pkg-group economy escalation ladder's step 1 was used (a toolchain
  kept pinned, other packages left unpinned so the group stays together),
  name the unpinned libraries here — a caveat, not a silent workaround. Omit
  if the ladder was never invoked (the common case).
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
- **2 (Migrate):** The user chose migrate — or said "migrate", "I'm
  ready", "commit it", "let's go", "do it", or any clear affirmation
  after the report. Read `references/migration.md` and follow it. Never run
  migration automatically — only on this explicit request.
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
