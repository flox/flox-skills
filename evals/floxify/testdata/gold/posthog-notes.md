# Golden manifest notes — PostHog @ 55525a19f353

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

`flox activate` deliberately NOT run (heavy closure / disk). Verified via
search/show only.

## Chosen versions + mismatches

| Package | Pinned | Rationale / mismatch |
|---------|--------|----------------------|
| python313 | 3.13.13 | Exact `requires-python ==3.13.13` |
| nodejs_24 | 24.13.0 | Exact `.nvmrc v24.13.0` |
| postgresql_15 | 15.12 | Exact compose image `postgres:15.12-alpine` |
| redis | 7.2.7 | Latest 7.2.x; compose uses `redis:7.2-alpine` |
| uv | (unpinned) | Latest catalog uv fine; version not pinned by repo |
| docker-compose | (unpinned) | Only used as launcher for ClickHouse |
| pnpm | NOT installed from catalog | packageManager pins `pnpm@10.29.3`; catalog only has pnpm@11.x → **version mismatch**. Honor the pin via corepack instead of installing catalog pnpm. |

## Services: Flox vs docker-compose

| Service | Wiring | Why |
|---------|--------|-----|
| **postgres** | Flox service | Config-light. initdb data under `$FLOX_ENV_CACHE/postgres`, socket in `/tmp`, trust auth (dev), creates `posthog` db. Native = fast, no Docker needed. |
| **redis** | Flox service | Config-light. Data under `$FLOX_ENV_CACHE/redis`, socket in `/tmp`, mirrors compose maxmemory 200mb / allkeys-lru. |
| **clickhouse** | docker-compose (`docker-compose.dev.yml up -d clickhouse`) | **In catalog at exact version, but NOT wired as a Flox package.** PostHog mounts 5+ server config files (config.xml, config.d/default.xml, config.d/dev-memory.xml, users-dev.xml, user_defined_function.xml) + `docker-entrypoint-initdb.d` init scripts + `user_scripts` (Python UDFs), and ClickHouse `depends_on: [kafka, zookeeper]`. A bare catalog clickhouse-server would require replicating all of that plus standing up Kafka/ZooKeeper. Using PostHog's own configured image is the functional, honest golden choice. |

Bringing up `clickhouse` via compose transitively starts **kafka (redpanda)**
and **zookeeper** through `depends_on` — so those two arrive via docker-compose,
not as standalone Flox services.

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

- ✗ pnpm NOT installed from catalog — pinned `pnpm@10.29.3` unavailable (catalog
  is pnpm@11.x). Resolved via corepack honoring the packageManager pin.
- ✗ ClickHouse NOT a Flox package/service despite catalog availability — config +
  Kafka/ZooKeeper coupling make docker-compose the only functional path.
- ✗ Kafka, ZooKeeper, object storage, Temporal, ingestion services NOT wired —
  full ingestion pipeline out of scope; app + migrations + queries run on
  postgres + redis + clickhouse.
- ✗ `flox activate` not executed — manifest validated by grounding + catalog
  verification, not by a live activation (disk/closure constraints).
- ✗ Native compiler/system-lib packages NOT added to [install] — every
  native-signalling dep (psycopg[binary], psycopg2-binary, cryptography, lxml,
  pillow, numpy, pyarrow, grpcio, brotli) ships manylinux binary wheels, so
  `uv sync` needs no gcc/libpq/libxml2 at build time. `postgresql_15` is present
  anyway (for the postgres service) and supplies libpq/pg_config as a bonus.

## Validation level

Grounded (every value traced to a repo file) + catalog-verified (every pkg-path
and version confirmed via `flox show`/`flox search --all`). NOT activation-tested.

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
