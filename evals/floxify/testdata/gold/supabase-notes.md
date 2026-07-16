# Golden manifest notes — supabase/supabase @ 963182f58e91

Repo: https://github.com/supabase/supabase
Pinned SHA: 963182f58e91 (HEAD: "fix: add hasLightIcon field to
ContentListings; revert getting-started-overview to GlassPanel (#47467)")
Ecosystem: pnpm + turbo JS/TS monorepo **plus** a Deno edge-functions runtime.

## Provenance table (file -> value)

| Fact | Value | Source file |
|------|-------|-------------|
| Node major | `22` | `.nvmrc` (bare "22") |
| Node engine | `>=22` | `package.json` engines.node |
| Node types | `^22.0.0` | `pnpm-workspace.yaml` catalog `@types/node` |
| Package manager | `pnpm@10.24.0` | `package.json` packageManager |
| pnpm engine | `10.24` | `package.json` engines.pnpm |
| pnpm enforced | `engine-strict=true` | `.npmrc` |
| Only-pnpm guard | `npx only-allow pnpm` | `package.json` scripts.preinstall |
| Turbo | `2.9.14` | `package.json` devDependencies.turbo |
| Workspaces | `apps/*`, `packages/*`, `blocks/*`, `e2e/*` | `pnpm-workspace.yaml` |
| Workspace dirs | 24 (7 apps, 15 packages, 1 block, 1 e2e) | `apps/ packages/ blocks/ e2e/` |
| Deno config | present (`{}`) | `supabase/functions/deno.json` |
| Deno usage | `Deno.serve` / `Deno.env.get`, `deno.land/std`, `jsr:` | `supabase/functions/*/index.ts` |
| Edge runtime img | `supabase/edge-runtime:v1.74.0` | `docker/docker-compose.yml` (functions) |
| Local db (docker) | `supabase/postgres:17.6.1.136` (PG17 default) | `docker/docker-compose.yml` `db` |
| Local db (pg15) | `supabase/postgres:15.8.1.085` (legacy override) | `docker/docker-compose.pg15.yml` |
| Local db (CLI) | `major_version = 15`, port `54322` | `supabase/config.toml` `[db]` |
| Default db port | `5432` | `docker/.env.example` POSTGRES_PORT |
| Default db user/name | `postgres` / `postgres` | `docker/.env.example` |

apps/: design-system, docs, learn, lite-studio, studio, ui-library, www
packages/ (15): ai-commands, api-types, build-icons, common, config,
dev-tools, eslint-config-supabase, generator, icons, marketing, pg-meta,
shared-data, tsconfig, ui, ui-patterns
edge functions: og-images, search-embeddings (+ common/, deno.json)

## Verification log (live Flox catalog — flox 1.13.2)

| Candidate | Command | Result | Chosen |
|-----------|---------|--------|--------|
| `nodejs_22` | `flox show nodejs_22` | nixpkgs, latest 22.23.1 | YES |
| `deno` | `flox show deno` | nixpkgs, latest 2.9.2 | YES |
| `postgresql_17` | `flox show postgresql_17` | nixpkgs, latest 17.10 | YES |
| `postgresql_15` | `flox show postgresql_15` | nixpkgs, latest 15.18 | ref only |
| `postgresql_{15,16,17,18}` | `flox search --all postgresql` | all present | — |
| `pnpm_10` | `flox show pnpm_10` | nixpkgs, 10.25.0..10.34.5 | NOT installed (see below) |
| `pnpm` | `flox show pnpm` | nixpkgs, latest 11.11.0 | ✗ (wrong major) |

All package names/versions were confirmed against the live catalog with
`flox search --all` / `flox show`. No `flox activate` was run. No name or
version was guessed.

## Chosen runtimes + caveats

- **nodejs_22** — the monorepo's single pinned Node major (three independent
  signals: `.nvmrc`, engines.node, catalog `@types/node`).
- **deno** — second runtime for `supabase/functions`. Catalog Deno is 2.x;
  the edge functions use the stable `Deno.serve` / `Deno.env` API and remote
  ESM/JSR imports, all supported by Deno 2. The docker edge-runtime image
  (`v1.74.0`) is Deno-based; stock `deno` covers local `deno run` / function
  development, not the exact edge-runtime sandbox.
- **postgresql_17** — closest Flox stock match to the docker-compose default
  `db` image `supabase/postgres:17.6.1.136`.

### Custom-Postgres caveat (important)
Supabase does NOT ship stock Postgres. `supabase/postgres` is a heavily
customized build bundling pgsodium, pg_graphql, pgvector, pg_net, wrappers,
supautils, pg_cron, and more, plus Supabase roles/migrations. Stock
`postgresql_17` from the Flox catalog provides a plain local Postgres 17 for
ordinary application development ONLY. Any app code depending on those
extensions will not find them in the Flox stock server. There is also an
internal version split: the self-hosted docker stack now defaults to PG17,
the `docker-compose.pg15.yml` override and the Supabase CLI
(`config.toml` major_version = 15) still target PG15. PG17 was chosen to
match the current docker default; swap to `postgresql_15` if pinning to the
CLI/legacy path.

### pnpm: why it is NOT in [install]
The repo pins `packageManager: pnpm@10.24.0` AND sets `.npmrc
engine-strict=true` with `engines.pnpm "10.24"` (i.e. the 10.24.x range).
The catalog `pnpm_10` package starts at 10.25.0 — outside that range — so a
catalog-pinned pnpm would be rejected by strict-engines on install. The
faithful, functional path is to let **corepack** (bundled with `nodejs_22`)
provision the exact pinned `pnpm@10.24.0` from the packageManager field.
The on-activate hook enables corepack into a writable `$FLOX_ENV_CACHE`
shim dir (the Node prefix is a read-only Nix store path, so `corepack
enable` cannot write there by default) and then runs
`pnpm install --frozen-lockfile` with the store under `$FLOX_ENV_CACHE`.

## Why deno is included
The monorepo has two runtimes, not one. `supabase/functions/{og-images,
search-embeddings}/index.ts` are Deno programs (`Deno.serve`,
`Deno.env.get`, imports from `https://deno.land/std@0.170.0` and
`jsr:@supabase/supabase-js`), governed by `supabase/functions/deno.json`,
and run in production via `supabase/edge-runtime:v1.74.0` (Deno). A manifest
that pins only Node would silently drop the entire edge-function surface —
the classic "monorepo only sees npm" failure mode this fixture guards
against.

## Why Supabase's own services are NOT wired as Flox services
`docker/docker-compose.yml` defines the Supabase platform itself:
studio (2026.07.07), kong 3.9.1, gotrue/auth v2.189.0, postgrest v14.12,
realtime v2.102.3, storage-api v1.60.4, imgproxy v3.30.1, postgres-meta
v0.96.6, edge-runtime v1.74.0, postgres 17.6.1.136, supavisor 2.9.5.
These are the **application under test / the product being built**, not
dev-time infrastructure a developer would recreate with Flox `[services]`.
They are prebuilt container images with deep interdependencies (JWT secrets,
shared network aliases, init SQL, healthcheck ordering) and belong to
`docker compose` / the Supabase CLI, which already orchestrate them. Wiring
them as Flox services would be wrong on two counts: it would duplicate the
app's own deployment, and no equivalent stock Flox packages exist for most
(gotrue, realtime, storage-api, kong, supavisor are Supabase/third-party
server binaries, not catalog dev tools). The only Flox service wired is a
plain local Postgres — genuine dev infrastructure a developer might want
without spinning up the full container stack.

## ✗ items (considered, rejected, or not applied)
- ✗ `pnpm_10` in `[install]` — catalog floor 10.25.0 violates the repo's
  `engines.pnpm "10.24"` under `engine-strict=true`; corepack used instead.
- ✗ `pnpm` (unversioned) — resolves to pnpm 11, wrong major for a pnpm-10
  lockfile.
- ✗ Wiring gotrue/postgrest/realtime/storage/kong/studio/supavisor as Flox
  services — these are the app, not dev infra (see above); no stock catalog
  equivalents.
- ✗ `postgresql_15` as the primary db — deferred to PG17 to match the current
  docker default; recorded as the CLI/legacy alternative.
- ✗ Deno dep pre-caching (`deno cache`) in the hook — omitted to keep the
  hook lean; Deno resolves remote/JSR imports on first run.
- ✗ Empty `[build]` / extra sections — omitted per "omit empty sections".
- ✗ No hallucinated flake/URL installs — every package is a verified catalog
  `pkg-path`.

## Validation level
Static + catalog-verified. Every package name and the existence of every
pinned version were confirmed with `flox search --all` / `flox show` against
the live catalog. The manifest was NOT activated (`flox activate` explicitly
out of scope), so runtime behavior of the hook/service is reasoned, not
executed. The service block follows the canonical Flox PostgreSQL pattern
(self-initializing data dir under `$FLOX_ENV_CACHE`, `is-daemon`, fast-stop
shutdown).

## OBSERVATIONS for improving the floxify skill

1. **pnpm/turbo monorepo runtime resolution.** Node major has several
   independent signals (`.nvmrc`, `engines.node`, catalog `@types/node`,
   the pnpm major implied by `packageManager`); floxify should cross-read
   all of them, not stop at the first. Critically, the package manager
   itself is version-locked two ways — `packageManager: pnpm@X.Y.Z` AND
   `engines.pnpm` under `.npmrc engine-strict=true`. When the catalog's
   `pnpm_<major>` floor is above the pinned patch, a catalog pin fails
   strict-engines; the skill should detect `packageManager` + engine-strict
   and prefer **corepack from the pinned Node** over a catalog pnpm pin.
   Corollary Nix gotcha the skill must encode: `corepack enable` writes
   shims into the Node prefix (read-only Nix store) and fails unless
   `--install-directory "$FLOX_ENV_CACHE/..."` (added to PATH) is used.

2. **Deno-alongside-Node detection.** A JS monorepo can hide a second
   runtime. Signals floxify should scan for beyond `package.json`:
   `deno.json`/`deno.jsonc`/`import_map.json`, source using `Deno.*` globals
   or `https://deno.land` / `jsr:` / `npm:` import specifiers, and
   compose/image references to `supabase/edge-runtime` (or any `denoland/*`
   image). Pinning only Node when a Deno runtime exists is a silent
   correctness failure, not a nice-to-have.

3. **Distinguish app-services from generic dev services.** A `docker/`
   directory full of compose files is NOT a menu of Flox `[services]`.
   Heuristic: images that are the project's OWN releases (namespace matches
   the repo org, e.g. `supabase/*`) or third-party server products
   (kong, postgrest) are the **application under test** and should be left
   to `docker compose`/the project CLI. Only wire generic, developer-facing
   datastores/tools (a plain Postgres/Redis) that have real catalog
   equivalents AND that a developer would plausibly run standalone. The
   skill should default to NOT wiring, and only promote a service when it
   maps cleanly to a stock catalog package and is dev infra, not product.
   Also flag the custom-image caveat: `supabase/postgres` != stock postgres
   (extensions/roles), so the Flox equivalent is a partial stand-in.
