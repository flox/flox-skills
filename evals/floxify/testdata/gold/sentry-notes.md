# Golden manifest notes — getsentry/sentry @ 68d439d41d66

Reference notes for the golden Flox manifest at `sentry-manifest.toml`.
Everything below is grounded in files read from the pinned checkout and
verified against the live Flox catalog (`flox show` / `flox search`).

## Provenance table (file → value)

| Value | Source file | Detail |
|-------|-------------|--------|
| Python 3.13.1 | `.python-version` | literal `3.13.1` (3.13.x confirmed) |
| Node 24.14.0 | `.node-version` | literal `24.14.0` (no `.nvmrc`) |
| Node 24.14.0 (2nd source) | `devenv/config.ini` | `[node] version = v24.14.0` + per-arch tarballs |
| pnpm 10.30.2 | `package.json` | `packageManager = "pnpm@10.30.2"` (no `engines`/`volta`) |
| Python pkg mgr = uv | `pyproject.toml` `[tool.uv]`, `uv.lock`, `devenv/sync.py` | `uv sync --frozen --inexact --active` |
| uv default index | `pyproject.toml` `[[tool.uv.index]]` | `https://pypi.devinfra.sentry.io/simple`, `default = true` (private) |
| Editable install | `devenv/sync.py` | `python3 -m tools.fast_editable --path .` (no build backend) |
| Frontend install | `devenv/sync.py` | `pnpm install --frozen-lockfile --reporter=append-only` |
| postgres (direct dep) | `devservices/config.yml` | `dependencies.postgres` → remote `sentry-shared-postgres` |
| redis (direct dep) | `devservices/config.yml` | `dependencies.redis` → remote `sentry-shared-redis` |
| snuba (→ ClickHouse+Kafka) | `devservices/config.yml` | `dependencies.snuba` remote `getsentry/snuba` (containerized) |
| Kafka (transitive) | `devservices/config/taskbroker.yml` | `address: kafka-kafka-1:9093`, `kafka_clusters:` |
| Linux apt deps | `devenv/post_fetch.py` | `REQUIRED_APT_PKGS = ["watchman", "chromium-chromedriver"]` |
| macOS brew deps | `Brewfile` | `uv`, `docker`, `docker-buildx`, `watchman` |
| Migrations flow | `devenv/sync.py` | `devservices up --mode migrations` → `make apply-migrations` |

## Catalog verification log (`flox` 1.13.2, nixpkgs)

| Package (manifest) | Command | Result |
|--------------------|---------|--------|
| `python313` | `flox show python313` | ✓ `python313@python3-3.13.1` present (matches `.python-version` exactly; latest 3.13.14) |
| `nodejs_24` | `flox show nodejs_24` | ✓ present; nearest to 24.14.0 is `24.14.1` (also 24.15/24.16/24.18) |
| `uv` | `flox show uv` | ✓ `uv@0.11.26` |
| `pnpm_10` | `flox show pnpm_10` | ✓ `pnpm_10@10.34.5` (major 10 matches pnpm@10.30.2; lockfile v9 compatible) |
| `postgresql` | `flox show postgresql` | ✓ present; `14.x` available (latest 18.4) |
| `redis` | `flox show redis` | ✓ present; `7.2.x` available (latest 8.8.0) |
| `xmlsec` | `flox show xmlsec` | ✓ `xmlsec@1.3.7` ("XML Security Library in C based on libxml2") |
| `libxml2` | `flox show libxml2` | ✓ `2.15.3` (outputs bin/dev/out) |
| `libxslt` | `flox show libxslt` | ✓ `1.1.45` |
| `openssl` | `flox show openssl` | ✓ `3.6.3` |
| `pkg-config` | `flox show pkg-config` | ✓ `0.29.2` |
| `watchman` | `flox show watchman` | ✓ `2026.01.19.00` |
| `clickhouse` | `flox search --all clickhouse` | PRESENT `clickhouse@26.6.1.1193-stable` — but deferred (see below) |
| `apacheKafka` | `flox search --all kafka` | PRESENT `apacheKafka@2.13-4.3.1` (+ `apacheKafka_3_x`, `rdkafka`) — but deferred |

## Direct-vs-transitive service map

Sentry's service graph is declared in `devservices/config.yml` under
`x-sentry-service-config.dependencies` and consumed via named `modes`.

**Direct dependencies** (named in Sentry's own config):
- `postgres` → remote repo `getsentry/sentry-shared-postgres`
- `redis` → remote repo `getsentry/sentry-shared-redis`
- `snuba`, `relay`, `symbolicator`, `vroom`, `taskbroker`, `objectstore`,
  `launchpad`, `uptime-checker`, `chartcuterie`, `spotlight`, etc.
- Supervisor "programs": `taskworker*` and the Kafka consumers
  (`ingest-events`, `ingest-transactions`, `post-process-forwarder-*`,
  `monitors-clock-*`, `process-spans`, `ingest-metrics`, ...).

**Transitive dependencies** (never named by Sentry; pulled in by `snuba`):
- **ClickHouse** — lives entirely inside `snuba`'s own devservices graph.
  No ClickHouse reference exists anywhere in the sentry repo. It arrives
  only because `default`/`full`/most modes include `snuba`.
- **Kafka** — surfaces indirectly: `devservices/config/taskbroker.yml`
  points at `kafka-kafka-1:9093` (the devservices `<svc>-<svc>-1` container
  naming ⇒ a `kafka` service defined in snuba's graph), and every
  Kafka-consumer supervisor program (`sentry run consumer ...`) requires
  that broker. Sentry declares the consumers, not the broker.

The default mode is `default: [snuba, postgres, relay, spotlight,
objectstore]`; the migrations mode `devenv sync` uses is
`migrations: [postgres, redis]`.

## How devservices-managed services are represented in the manifest

- **postgres + redis → native Flox services** (`[services.postgres]`,
  `[services.redis]`). They are direct deps, catalog-available, trivially
  standalone (a single server process each), and match the idiomatic Flox
  datastore pattern: state under `$FLOX_ENV_CACHE`, unix socket in `/tmp`,
  trust/no auth (dev only). Postgres also bootstraps the `sentry` database
  on first boot (Sentry connects as `psql sentry postgres`, per `sync.py`).
- **Everything else → deferred to `devservices up`.** The manifest does
  NOT try to wire snuba/ClickHouse/Kafka/relay/symbolicator as Flox
  services. A trailing comment block documents this and gives the two
  commands (`devservices up`, `devservices up --mode migrations`).
  `devservices` is a dev dependency (`pyproject.toml` `[dependency-groups]
  dev`), so the `uv sync` in the hook installs it into the venv — the
  orchestrator is available once activation completes.

### Why NOT wire ClickHouse / Kafka from the catalog (premise correction)

The task briefing assumed ClickHouse and Kafka are absent from the Flox
catalog. **They are actually present** (`clickhouse@26.6.1.1193-stable`,
`apacheKafka@2.13-4.3.1`, `apacheKafka_3_x`, plus `rdkafka`). The correct
reason to defer them is stronger than availability:

1. **Transitive + owned by snuba.** Sentry never configures a raw
   ClickHouse or Kafka. snuba owns the pinned versions, the ClickHouse
   schema/migrations, and the Kafka topic layout. A bare catalog server
   would start but be non-functional for Sentry (no tables, no topics).
2. **Container-shaped.** They ship as getsentry-pinned container images in
   snuba's devservices graph, not as processes Flox would supervise well.

So: catalog availability ≠ correct to wire. Deferring to `devservices` is
the idiomatic and functionally-correct answer.

## Native dependencies — how they were found

Sourced from `pyproject.toml` `dependencies` (C-extension packages) and
cross-checked against `Brewfile` / `devenv/post_fetch.py`:

- `xmlsec>=1.3.17` and `python3-saml>=1.15.0` → need the **xmlsec** C lib,
  **libxml2**, **libxslt**, **openssl**, **pkg-config** to build.
- `lxml>=6.1.0` → **libxml2**, **libxslt**.
- `cryptography>=49.0.0` → **openssl** (+ Rust to build from source).
- `psycopg2-binary` → binary wheel, no build deps (no libpq needed).
- `confluent-kafka` → bundles librdkafka in its wheel.
- `watchman` → from `Brewfile` and `REQUIRED_APT_PKGS`.

**Key subtlety:** Sentry's real dev flow needs almost NO system native
libs — `REQUIRED_APT_PKGS` is just `watchman` + `chromium-chromedriver`,
and the `Brewfile` is `uv`/`docker`/`docker-buildx`/`watchman`. That is
because `pyproject.toml` pins uv's default index to Sentry's **private
wheel mirror** (`pypi.devinfra.sentry.io`), which serves prebuilt wheels
with native libs bundled. The golden manifest includes the native build
deps anyway so the environment is source-buildable against public PyPI;
they are harmless when wheels are used.

## ✗ / caveats

- **✗ ClickHouse / Kafka not wired** — deferred to `devservices up` by
  design (transitive via snuba; snuba owns schema/topics). Present in
  catalog but intentionally not installed.
- **✗ Exact node 24.14.0 not in catalog** — nearest is `nodejs_24@24.14.1`.
  Pinned the `nodejs_24` series; patch drift is immaterial for dev.
- **✗ postgres/redis versions not repo-pinned** — the versions live in the
  remote `sentry-shared-postgres` / `sentry-shared-redis` repos, not in
  this checkout. Manifest pins `postgresql=14` / `redis=7.2` as
  Sentry-aligned reference majors and labels them as such (not scraped
  from the repo).
- **⚠ Private PyPI index** — `uv sync` targets Sentry's private mirror by
  default; outside Sentry's network it needs index access or an override
  (`UV_INDEX_URL` / `--index`). Hook logs a hint rather than hard-failing.
- **⚠ No C toolchain installed** — building C extensions from source
  (rather than using wheels) would also need `gcc`/`clang`, `gnumake`, and
  Rust for `cryptography`/`orjson`. Omitted to match Sentry's wheel-based
  reality; add them only for a from-source build.
- **Not wired:** chromium-chromedriver (Selenium acceptance tests only),
  memcached/bigtable/redis-cluster (test-only devservices modes).

## OBSERVATIONS for the floxify skill

1. **Discover `devservices/config.yml` when there is no docker-compose.**
   floxify should not stop at "no root `docker-compose.yml` ⇒ no services."
   It should probe a small set of alternative service manifests:
   `devservices/config.yml` (getsentry), `docker-compose.override.yml`,
   `compose.yaml`, `Procfile`, `Tiltfile`, `.devcontainer/`, Nix/`devenv`
   files. Sentry's entire topology is invisible unless you read
   `devservices/config.yml` — a compose-only scanner would emit an env with
   zero services and silently miss Postgres/Redis/Kafka/ClickHouse.

2. **Distinguish direct from transitive services, and don't equate
   "in the catalog" with "wire it."** The hard call here was NOT whether
   ClickHouse/Kafka exist in the catalog (they do) — it was recognizing
   they arrive *transitively* through `snuba` and are owned by that
   subgraph (schema, topics, pinned images). floxify needs a rule:
   *a service reached only through another repo's dependency graph, or one
   that requires companion schema/topic/migration setup, should be deferred
   to the project's own orchestrator rather than wired as a bare Flox
   service — even when a catalog package exists.* Wire only leaf datastores
   the project depends on directly (here: postgres, redis).

3. **Trust the project's own bootstrapper over generic heuristics.**
   `devenv/sync.py` spelled out the exact, ordered install flow
   (`pnpm install --frozen-lockfile` → `uv sync --frozen --inexact
   --active` → `fast_editable` → `devservices up --mode migrations` →
   `make apply-migrations`) and `devenv/config.ini` / `.node-version`
   pinned the runtimes. floxify should locate and read the repo's bootstrap
   script (`devenv/`, `scripts/bootstrap*`, `Makefile` install targets,
   `.envrc`) and mirror its commands verbatim in `on-activate`, rather than
   inventing a generic `pip install -r requirements.txt`. Corollary: detect
   the Python installer from evidence (`uv.lock` + `[tool.uv]` ⇒ uv; not
   pip/pip-tools) and honor a private index pin instead of assuming public
   PyPI.
