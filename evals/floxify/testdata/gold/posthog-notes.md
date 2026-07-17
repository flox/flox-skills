# Golden manifest notes — PostHog @ 55525a19f353

**AI-478 (2026-07-17):** Applied the pkg-group economy escalation ladder
(SKILL.md) to the AI-457 5-way single-package isolation. Collapsed 4 of
5 (python313/nodejs_24/corepack_24/postgresql_15 now share one group);
redis stays isolated — the one holdout, with live failure evidence — see
"Pkg-group economy" below. Service disposition (AI-470) and every
`[services.*]`/`[hook]` block are unchanged.

**AI-470 (2026-07-17):** Rebuilt on Bill's service-disposition adjudication
— "does a developer need this service running locally to develop against?"
Dev-time services (postgres, redis) are wired as Flox services; runtime-
oriented infrastructure (clickhouse + its kafka/zookeeper backing) is
deferred WITH AN EXPLICIT MECHANISM, never silently dropped. See "Service
disposition" and "Upstream technique adoption" below. Package pins,
provenance, and the pre-existing services themselves were already correct
(AI-457) and are carried forward unchanged except for the docker-compose
invocation shape (see "Service disposition") and the corepack_24 addition
(see "Upstream technique adoption").

Repo: https://github.com/PostHog/posthog
Pinned SHA: `55525a19f353d9b20752bde79285d7e23d94da8e`
(HEAD subject: "fix(ci): preserve selective storybook file filters (#71254)")
Ecosystem: Python (uv) + Node (pnpm monorepo).

## Provenance table (file → value)

| Signal | Source file | Value |
|--------|-------------|-------|
| Python version | `pyproject.toml` L6 | `requires-python = "==3.13.13"` |
| Python pkg manager | `uv.lock` (root), `pyproject.toml` L292 `[tool.uv]`, L298 `[tool.uv.workspace]` | uv |
| Node version | `.nvmrc` | `v24.13.0` |
| Node engines | `package.json` L123-125 | `"node": ">=24 <25"` |
| Node pkg manager | `package.json` L126 | `packageManager = "pnpm@10.29.3"` |
| pnpm workspace | `pnpm-workspace.yaml` | 30+ workspace packages (monorepo) |
| Postgres | `docker-compose.base.yml` L153 `db` | `postgres:15.12-alpine`, user/db/pass = posthog |
| Redis | `docker-compose.base.yml` L170 `redis7` | `redis:7.2-alpine`, maxmemory 200mb allkeys-lru |
| ClickHouse | `docker-compose.base.yml` L180 `clickhouse` | `clickhouse/clickhouse-server:26.6.1.1193`, SKIP_USER_SETUP, KAFKA_HOSTS |
| ClickHouse config | `docker/clickhouse/` + `docker-compose.dev.yml` L419-455 | config.xml, config.d/*, users-dev.xml, user_defined_function.xml, init scripts; depends_on kafka+zookeeper |
| Native-dep signals | `pyproject.toml` deps | psycopg[binary]==3.2.4, psycopg2-binary==2.9.10, cryptography==46.0.7, lxml==6.0.2, pillow==12.2.0, numpy~=2.1.0, pyarrow==23.0.1, grpcio~=1.71.0, brotli==1.2.0 (all ship manylinux binary wheels) |

Version dotfiles checked: `.nvmrc` present. NO `.python-version`,
`.tool-versions`, or `mise.toml` — **the Python pin lives ONLY in
`pyproject.toml` requires-python**. (Ticket's earlier 3.11 guess was WRONG;
verified 3.13.13.)

## Verification log (live catalog — flox show/search)

| Command | Result / confirmed pkg-path |
|---------|------------------------------|
| `flox show python313` | ✓ has `python3-3.13.13` (matches requires-python exactly) |
| `flox show nodejs_24` | ✓ has `24.13.0` (matches .nvmrc exactly) |
| `flox show uv` | ✓ `uv@0.11.26` latest |
| `flox show postgresql_15` | ✓ `15.12` present (exact match to compose image) |
| `flox show redis` | ✓ `7.2.7` present (7.2 line matches compose) |
| `flox search --all clickhouse` | ✓ **`clickhouse` IS in catalog** |
| `flox show clickhouse` | ✓ `clickhouse@26.6.1.1193-stable` — **EXACT match to PostHog's image** |
| `flox show docker-compose` | ✓ `docker-compose@5.3.1` (catalog-reported version, used verbatim) |
| `flox search --all corepack` | ✓ `corepack`, `corepack_20` exist (nodejs_24 also bundles corepack) |

`flox activate` deliberately NOT run at capture time (heavy closure /
disk). Verified via search/show only. AI-457 later resolution-tested this
manifest — see "AI-457 activation validation" below.

### AI-457 re-verification (2026-07-16)

The manifest previously pinned `python3.version = "3.13.13"`, but `flox
show python313`'s "Other versions" list carries a catalog-specific prefix
(`python313@python3-3.13.13`, not `python313@3.13.13`) — the bare-semver
pin does not resolve at all (`catalog-version-missing`). Fixed to the exact
string `flox show` prints: `python3.version = "python3-3.13.13"`.

Live re-check of per-system availability (flox 1.13.2, 2026-07-16):

| pkg-path | Result | Note |
|----------|--------|------|
| `flox show python313` | `python3-3.13.13` has **no** systems parenthetical | present on all four systems |
| `flox show uv` | latest `0.11.26` (aarch64-darwin, aarch64-linux, x86_64-linux only) | missing x86_64-darwin |
| `flox show docker-compose` | latest `5.3.1` (aarch64-darwin, aarch64-linux, x86_64-linux only) | missing x86_64-darwin |
| `flox show nodejs_24` | `24.13.0` (pinned) — no parenthetical | present on all four systems |
| `flox show postgresql_15` | `15.12` (pinned) — no parenthetical | present on all four systems |
| `flox show redis` | `7.2.7` (pinned) — no parenthetical | present on all four systems |

Correction to an earlier claim: the fixed python313 pin (`python3-3.13.13`)
is available on all four systems, not "x86_64-linux only" — that was an
unverified assumption from the ticket that this pass corrected against
live `flox show` output before writing it here. The actual blockers for
x86_64-darwin are `uv` (installs every Python dependency) and
`docker-compose` (brings up ClickHouse, a hard dependency per the file
header), both unpinned and both missing an x86_64-darwin build at latest.
`[options].systems` now drops `x86_64-darwin` for that reason.

Also added `postgresql.outputs = ["out", "lib", "dev"]` (default is
`out`+`man` only) — not strictly required since psycopg ships binary
wheels, but exposes `pg_config`/`libpq.so`/headers for anyone who does
need them, consistent with the mastodon golden's pattern.

**Activation validation (resolution-tested, not functionally tested).** `[options].systems` alone was not enough to
activate: `flox activate` (x86_64-linux, throwaway directory, no real
posthog checkout) failed with `constraints for group 'toplevel' are too
tight` even after the systems fix -- `python313@python3-3.13.13`,
`nodejs_24@24.13.0`, `postgresql_15@15.12`, and `redis@7.2.7` have no
single shared catalog page as a group. Giving each of the four exact pins
its own `pkg-group` resolved it; the manifest now activates cleanly (hook
errors are only from the missing `pyproject.toml`/`package.json`/
`docker-compose.dev.yml` in the scratch test directory, expected without a
real checkout).

## Chosen versions + mismatches

Versions below marked "(unpinned, AI-478)" are the pkg-group-economy
trade-off: the pin was dropped to let the package co-resolve with
python313's exact pin in a shared group, since nothing in the repo
demanded that exact patch. The "resolved live" value is what the
catalog happened to produce on 2026-07-17 — expected to drift forward
over time, by design (see "Pkg-group economy" below).

| Package | Pinned | Rationale / mismatch |
|---------|--------|----------------------|
| python313 | 3.13.13 | Exact `requires-python ==3.13.13` — the one genuinely repo-demanded equality constraint; kept exact as the group's toolchain anchor |
| nodejs_24 | (unpinned, AI-478) | Resolved live to 24.16.0, 2026-07-17. Was exact `.nvmrc v24.13.0`; the patch pin wasn't load-bearing (package.json's own `engines.node` only floors at `>=24`), and `nodejs_24`'s pkg-path already confines resolution to the 24.x line |
| postgresql_15 | (unpinned, AI-478) | Resolved live to 15.18, 2026-07-17. Was exact compose image match (`postgres:15.12-alpine`); the pkg-path confines resolution to the 15.x line regardless |
| redis | 7.2.7 | Latest 7.2.x; compose uses `redis:7.2-alpine`. Isolated in its own group (AI-478) — the one holdout, see "Pkg-group economy" below |
| uv | (unpinned) | Latest catalog uv fine; version not pinned by repo |
| docker-compose | (unpinned) | Launcher for the deferred-with-mechanism ClickHouse stack |
| corepack_24 | (unpinned, AI-478) | Resolved live to 24.16.0, 2026-07-17 (matches nodejs_24 — both resolve from the same shared group/page). AI-470 adopted this package from PostHog's own upstream `.flox/env/manifest.toml` — see "Upstream technique adoption"; nothing requires corepack's own version to match anything exactly, since its job (provisioning the exact `pnpm@10.29.3` pin) is version-independent |
| pnpm | NOT installed from catalog | packageManager pins `pnpm@10.29.3` exactly; `pnpm_10` has 42 versions in the 10.x line (confirmed live, AI-457, 2026-07-16), but not that exact patch -- nearest is `pnpm_10@10.29.2`. Honor the byte-exact pin via corepack instead of settling for the nearest catalog version. |

## Pkg-group economy (AI-478, 2026-07-17)

AI-464's golden audit flagged this golden's 5-way single-package
isolation (`python313`, `nodejs-24`, `corepack-24`, `postgresql-15`,
`redis-72` — one group per exact pin) as a candidate for collapse under
the SKILL.md escalation ladder ("Pkg-group economy — fewest groups
possible is a first-order goal"). Step 1 was never attempted at the time
(AI-457 went straight to per-package isolation, the ladder's LAST rung).
This pass attempted it empirically against the live catalog, working
progressively less aggressive candidates via `flox edit -f` (loads a
candidate manifest into a scratch env, surfaces resolution failures
immediately) followed by `flox activate -c "echo __ok__"` (full
resolution-test) whenever `flox edit -f` succeeded:

| Candidate | Shape tried | Result |
|-----------|-------------|--------|
| A | Full collapse, ALL FIVE exact pins kept, one shared group | **Failed** — `constraints for group 'toplevel' are too tight` (the same conflict AI-457 originally hit, unchanged by group name) |
| B | python313 exact; nodejs/postgresql/redis floored to their major line (`"24"`/`"15"`/`"7"`); corepack unpinned; one shared group | **Failed** — same "too tight" error. A partial/floor pin is still a real constraint the resolver has to satisfy; floors alone weren't enough |
| C | python313 exact; nodejs/corepack/postgresql/redis FULLY unpinned (no `version` field at all); one shared group | **Resolved and activated** — but `redis` resolved to **8.8.0**, a silent major-version jump away from what this golden (and PostHog's own `docker-compose.base.yml redis7: redis:7.2-alpine`) is actually verified against. Rejected: an unverified functional change disguised as a pkg-group optimization is not an economy win |
| D | Same as C but `redis.version = "7"` (floor to major) | **Failed** — same "too tight" error. Confirms this is not a "looser pin resolves, tighter doesn't" oddity: no catalog page carrying `python313@python3-3.13.13` has ANY redis 7.x build, floored or exact — only 8.x |
| E | python313 exact; nodejs/corepack/postgresql fully unpinned, sharing python313's group; redis kept isolated, exact `7.2.7` | **Resolved and activated** — adopted |

Candidate E is the most economical shape that actually resolves without
silently changing which major version of anything gets installed.
Resolved versions confirmed via the scratch environment's
`.flox/env/manifest.lock` after a successful `flox activate`:
`nodejs_24@24.16.0`, `corepack_24@24.16.0`, `postgresql_15@15.18`, all
still within their pkg-path's guaranteed major line (`nodejs_24`/
`postgresql_15` can only ever resolve within the 24.x/15.x lines
regardless of pin, by pkg-path name) — the trade-off is losing the exact
patch match to what PostHog's own `docker-compose.base.yml` runs, not
losing major-version compatibility.

**Step-1 caveat (SKILL.md report rule):** `nodejs_24`, `corepack_24`,
and `postgresql_15` are no longer pinned to the exact patches
docker-compose runs (were `24.13.0`/`24.13.0`/`15.12`) — they now float
within their major line, resolving to whatever the shared group's
catalog page carries.

**Redis isolation, the failure evidence kept:** the escalation ladder's
LAST rung (isolate a single package) applies here because co-resolution
demonstrably failed even with the version constraint relaxed to a bare
major floor (candidate D), and redis has no native-linkage coupling to
python/node/postgres in this manifest (PostHog's Python redis client
ships prebuilt wheels — nothing compiles against `libredis`). Isolating
it costs one dedicated catalog-page download; the alternative (candidate
C) would have silently shipped Redis 8 in a golden whose services,
rubric, and this very notes file describe Redis 7.

**Result:** posthog's single-package pkg-group count drops from 5 to 1
(`redis-72` only). Service disposition tags (AI-470) and every
`[services.*]`/`[hook]` block are unchanged by this pass — only
`[install]`'s version/group fields moved.

## Service disposition (AI-470)

The test: **does a developer need this service running locally to develop
against?** Dev-time services are wired as Flox services; runtime-oriented
infrastructure a developer doesn't directly poke at is deferred WITH A
MECHANISM (never silently dropped). This is Bill's AI-470 adjudication,
applied here; the SDLC build/runtime split floxify may eventually need is
future surface (AI-475), out of scope for this fixture.

| Service | Disposition | Wiring | Why |
|---------|--------------|--------|-----|
| **postgres** | dev-time | Flox service (`[services.postgres]`) | A developer runs the app and queries the DB directly. Config-light: initdb data under `$FLOX_ENV_CACHE/postgres`, socket in `/tmp`, trust auth (dev), creates `posthog` db. Native = fast, no Docker needed. |
| **redis** | dev-time | Flox service (`[services.redis]`) | Cache/queue backend the app talks to directly in dev. Config-light: data under `$FLOX_ENV_CACHE/redis`, socket in `/tmp`, mirrors compose maxmemory 200mb / allkeys-lru. |
| **clickhouse** | runtime-oriented | deferred-with-mechanism: `[hook]` runs `docker-compose up -d clickhouse` when Docker is reachable, else prints the exact command to run manually | **In catalog at PostHog's exact version, but not wired as a Flox package.** PostHog mounts 5+ server config files (config.xml, config.d/default.xml, config.d/dev-memory.xml, users-dev.xml, user_defined_function.xml) + `docker-entrypoint-initdb.d` init scripts + `user_scripts` (Python UDFs), and ClickHouse `depends_on: [kafka, zookeeper]`. A bare catalog clickhouse-server would require replicating all of that plus standing up Kafka/ZooKeeper — a developer doesn't run queries against ClickHouse directly the way they do postgres/redis; the app's ingestion layer does. Deferring with an explicit, discoverable mechanism (not silently dropping it) is the honest choice for infrastructure this shape. |

**"Deferred-with-mechanism" is a structural claim, not just a docstring
promise.** The hook invokes the launcher as a bare `docker-compose up`
(via `COMPOSE_FILE=docker-compose.dev.yml`, not an inline `-f <file>`
between "compose" and "up") specifically so the harness's own
manifest-wired-compose check (`verify.py`'s `_manifest_wires_compose`,
AI-466's carve-out, reused by tier2.py's disposition-aware structural
check, AI-470) recognizes it as a real, invoked mechanism — not merely a
repo that happens to ship a compose file. An earlier version of this
golden used `docker-compose -f docker-compose.dev.yml up -d clickhouse`,
which is functionally identical but does NOT match that check's regex
(`-f <file>` sits between "compose" and "up"); this rebuild fixes that.

Bringing up `clickhouse` via compose transitively starts **kafka (redpanda)**
and **zookeeper** through `depends_on` — so those two arrive via docker-compose,
not as standalone Flox services. Both are runtime-oriented infrastructure
by the same test above (a developer doesn't talk to Kafka/ZooKeeper
directly either) — they inherit clickhouse's disposition rather than
needing one of their own, since they're pulled in transitively, not
independently expected by `tier2.jsonl`.

## Upstream technique adoption (AI-470)

PostHog's own `.flox/env/manifest.toml` (the in-tree env AI-469 strips
before the conversion task runs, but captures as `upstream_manifest`) is
a real signal — a known-working environment the project's own maintainers
use — mined here for techniques worth adopting. Each candidate verified
live against the catalog before any adoption (2026-07-17, flox 1.13.2):

| Technique | Source | Decision | Why |
|-----------|--------|----------|-----|
| `corepack_24` installed explicitly, `priority = 4` | `corepack = { pkg-path = "corepack_24", ..., priority = 4 }` | **Adopt** | Confirmed live: `corepack_24@24.13.0` exists in the catalog, matching the pinned Node version exactly. Without it, `corepack enable` uses whatever corepack ships bundled inside the `nodejs_24` derivation — an unpinned, unverified version. Installing `corepack_24` explicitly with a lower priority (4 < the manifest default of 5, per `flox.md` §5) than `nodejs_24`'s implicit default guarantees corepack's own binary wins any PATH collision. Resolution-tested (2026-07-17): activates cleanly in its own `pkg-group`, consistent with this golden's exact-pin-isolation convention (AI-457). |
| `sccache` + `RUSTC_WRAPPER=sccache` (Rust compile cache) | `[install]` sccache; `[vars]` RUSTC_WRAPPER | Keep-ours | Not applicable — this golden's scope is the Python(uv)+Node(pnpm) app surface `tier2.jsonl`'s `expected_runtimes` targets (python3, nodejs_24); it does not model PostHog's separate Go/Rust workspace (a materially larger fixture-scope question, not part of this ticket's service-disposition adjudication). |
| `watchman` (fast file watching for Django/Celery autoreload) | `[install]` watchman | Note | Confirmed live: `watchman@2026.01.19.00` exists, all four systems. A dev-experience nicety (faster autoreload), not required for `uv sync`/activation to succeed — out of this golden's functional-minimum scope. Worth reconsidering if a future ticket targets dev-experience parity rather than functional correctness. |
| `ffmpeg_5` (media processing) | `[install]` ffmpeg | Note | Present in upstream but not evidenced as required by anything in this golden's Python(uv)+Node(pnpm) scope (no produced Tier 2 rep, including the "base shape" rep, installed it either). Likely serves a specific subsystem (e.g. session-replay processing) outside what this fixture models; not adopted without that investigation. |
| `xmlsec`, `freetds` (SAML / MSSQL client libs) | `[install]` xmlsec, freetds | Note | Niche enterprise-integration dependencies (SAML SSO, MSSQL `pymssql`), not part of the core dev path this golden targets. Not adopted. |

## Compose services deliberately NOT wired (extras)

From `docker-compose.base.yml`:

| Service | Reason (one word) |
|---------|-------------------|
| caddy / proxy | reverse-proxy |
| kafka (redpanda) | event-bus (pulled in transitively by clickhouse) |
| zookeeper | coordination (pulled in transitively by clickhouse) |
| kafka_ui | tooling |
| objectstorage (seaweedfs) | s3-emulation |
| objectstorage-azure (azurite) | azure-emulation |
| maildev | email-testing |
| flower | celery-ui |
| capture / capture-logs | rust-ingestion |
| property-defs-rs | rust-service |
| feature-flags | rust-service |
| personhog-replica / personhog-router | rust-service |
| hypercache-server | rust-service |
| cyclotron-janitor | rust-service |
| livestream | go-service |
| elasticsearch | legacy-search |
| opensearch / opensearch-dashboards | search |
| temporal (auto-setup/admin-tools/ui) | workflow-engine |
| otel-collector | telemetry |
| jaeger | tracing |
| localstack | aws-emulation |
| duckgres | experimental |

These are prebuilt-image application/tooling services, not language runtimes or
core datastores — correctly left to `docker-compose` when a fuller stack is
needed, out of scope for a minimal functional Flox env.

## ✗ Items (not done / caveats)

- ✗ pnpm NOT installed from catalog — pinned `pnpm@10.29.3` unavailable exactly
  (nearest catalog version is `pnpm_10@10.29.2`, confirmed live). Resolved via
  corepack honoring the byte-exact packageManager pin.
- ✗ ClickHouse NOT a Flox package/service despite catalog availability — config +
  Kafka/ZooKeeper coupling make docker-compose the only functional path;
  deferred WITH the mechanism wired in `[hook]`, not silently omitted (AI-470
  service disposition).
- ✗ Kafka, ZooKeeper, object storage, Temporal, ingestion services NOT wired —
  full ingestion pipeline out of scope; app + migrations + queries run on
  postgres + redis + clickhouse.
- ✗ Native compiler/system-lib packages NOT added to [install] — every
  native-signalling dep (psycopg[binary], psycopg2-binary, cryptography, lxml,
  pillow, numpy, pyarrow, grpcio, brotli) ships manylinux binary wheels, so
  `uv sync` needs no gcc/libpq/libxml2 at build time. `postgresql_15` is present
  anyway (for the postgres service) and supplies libpq/pg_config as a bonus.

## Validation level

Grounded (every value traced to a repo file) + per-package verified (every
pkg-path and version confirmed via `flox show`/`flox search --all`) +
resolution-tested (AI-457, 2026-07-16, re-confirmed AI-470, 2026-07-17:
`flox activate -c "echo __ok__"` succeeds in a throwaway directory on
x86_64-linux, including the AI-470 rebuild's `corepack_24` addition) +
golden-lint clean (AI-470, 2026-07-17: `test_golden_lint.py` passes with
the EMPTY `KNOWN_VIOLATIONS` allowlist and the live catalog leg). NOT
functionally tested — no real posthog checkout, so `uv sync`/`pnpm install`
never ran against real lockfiles and no native build was exercised.

### AI-470 re-verification (2026-07-17, flox 1.13.2)

Every pin this rebuild touches was re-confirmed live before being written:

| pkg-path | Result |
|----------|--------|
| `flox show python313` | `python3-3.13.13` present (unchanged from AI-457) |
| `flox show nodejs_24` | `24.13.0` present (unchanged) |
| `flox show uv` | present, unpinned (unchanged) |
| `flox show postgresql_15` | `15.12` present (unchanged) |
| `flox show redis` | `7.2.7` present (unchanged) |
| `flox show docker-compose` | `docker-compose@5.3.1` latest (unchanged) |
| `flox show corepack_24` | `24.13.0` present — **new pin, this rebuild** |

`_manifest_wires_compose` (verify.py) confirmed `True` against this exact
manifest text (not just the general shape) — the bare `docker-compose up`
invocation via `COMPOSE_FILE` genuinely satisfies the check, not just
"a compose file happens to exist somewhere in the repo."

## OBSERVATIONS for improving the floxify skill

1. **Python pin lives in `pyproject.toml requires-python`, not a dotfile.**
   PostHog has NO `.python-version`/`.tool-versions`/`mise.toml`; the only pin is
   `requires-python = "==3.13.13"`. floxify must parse `requires-python` from
   `pyproject.toml` (handle `==`, `~=`, `>=,<` forms) and map the major.minor to
   the catalog `pythonNNN` pkg-path (3.13 → `python313`), not assume a dotfile.
   The ticket's own 3.11 guess shows how easily this is gotten wrong.

2. **pnpm monorepo: honor `packageManager` via corepack, don't install pnpm from
   the catalog.** The catalog pnpm (11.x) will drift from the repo's pinned
   `packageManager` (10.29.3) and can break `--frozen-lockfile`. floxify should
   detect `packageManager = "pnpm@X"` in package.json and generate a corepack
   activation into a **writable** dir (`corepack enable --install-directory
   $FLOX_ENV_CACHE/node-bin pnpm`) — the Nix nodejs store path is read-only, so
   plain `corepack enable` fails. Also detect `pnpm-workspace.yaml` to know it's a
   monorepo (single root `pnpm install` covers all packages).

3. **"clickhouse-not-in-catalog" handling is a red herring — catalog presence is
   necessary but NOT sufficient.** ClickHouse IS in the catalog at PostHog's exact
   version (26.6.1.1193-stable), yet the correct wiring is still docker-compose,
   because the service needs mounted config files (config.xml/users-dev.xml/UDFs +
   init scripts) and a Kafka/ZooKeeper backing. floxify's decision rule should be:
   "does this datastore need bespoke server config or hard service dependencies
   the app project bakes into a docker image?" If yes → docker-compose even when
   the package exists; if no (postgres, redis) → native Flox service. Detect the
   signal by checking whether the compose service has `volumes:` mounting config
   files and/or `depends_on:` other services.

4. **uv venv placement.** Set `UV_PROJECT_ENVIRONMENT=$FLOX_ENV_CACHE/venv` (in
   `[vars]` and re-export in `[profile]`) so `uv sync` never creates a repo-local
   `.venv`. This is cleaner than `uv venv` + manual activate and is uv's native
   knob.

5. **Binary-wheel deps rarely need system libs.** Modern Python pins
   (psycopg[binary], psycopg2-binary, pillow, lxml, numpy, pyarrow, grpcio) ship
   manylinux wheels; floxify should NOT reflexively add gcc/libpq/libxml2/zlib to
   [install] on seeing those package names. Only add native libs when the lockfile
   forces a source build (no matching wheel) — otherwise it's dead weight.

6. **(AI-470) The service-disposition test sharpens observation 3.** "Does this
   datastore need bespoke server config or hard service dependencies" is a
   necessary condition but not the cleanest one to apply directly — the crisper
   test Bill's adjudication settled on is **"does a developer need this service
   running locally to develop against?"** Dev-time services (postgres, redis:
   the app queries them directly) get wired as native Flox services; runtime-
   oriented infrastructure a developer doesn't poke at directly (clickhouse,
   and anything it pulls in transitively) gets deferred WITH AN EXPLICIT
   MECHANISM — never silently dropped. This note records the sharpened test for
   whoever eventually restates it in SKILL.md itself (tracked separately,
   AI-475 — floxify's SDLC build/runtime split is a larger surface than one
   golden's notes file should decide unilaterally).
