# Golden manifest notes — plausible/analytics

- **Repo:** https://github.com/plausible/analytics
- **Pinned SHA:** `d5af396464c2` (branch `master`, HEAD commit
  "CRM: Trial prospects worker (#6498)")
- **Ecosystem:** Elixir/Phoenix + Postgres + ClickHouse + Node assets
- **Validation level:** static only — files read + every package verified
  with `flox show` / `flox search --all` / `nix eval`. No `flox activate`
  was run (per instructions).

## Provenance table (file → value)

| Fact | Value | Source file (grounding) |
|------|-------|-------------------------|
| Elixir version (pin) | 1.20.2-otp-28 | `.tool-versions` |
| Elixir version (floor) | `~> 1.18` | `mix.exs:11` |
| Elixir/Erlang (build img) | elixir 1.20.2 / erlang 28.5.0.3 | `Dockerfile` (hexpm image) |
| Erlang/OTP (pin) | 28.5.0.3 | `.tool-versions` |
| Node.js version (pin) | 23.2.0 | `.tool-versions` |
| Node.js (build img) | nodejs-current 23.11.1 | `Dockerfile` |
| Phoenix assets pkg | assets/package.json | `assets/package.json` (+ lock) |
| Tracker JS pkg | tracker/package.json | `tracker/package.json` (+ lock) |
| Mix pkg manager | `mix deps.get`, hex, rebar | `Makefile` (install), `Dockerfile` |
| Postgres (dev) | `postgres:latest` | `Makefile` (`postgres` target) |
| Postgres (prod) | `postgres:18` | `Makefile` (`postgres-prod`) |
| Postgres dev DSN | `postgres://postgres:postgres@127.0.0.1:5432/plausible_dev` | `config/.env.dev:3` |
| ClickHouse (dev) | `clickhouse/clickhouse-server:latest-alpine` | `Makefile` (`clickhouse`) |
| ClickHouse (prod) | `...:25.11.5.8-alpine` | `Makefile` (`clickhouse-prod`) |
| ClickHouse dev URL | `http://127.0.0.1:8123/plausible_events_db` | `config/.env.dev:4` |
| ClickHouse config mount | `$PWD/.clickhouse_config:/etc/clickhouse-server/config.d` | `Makefile` (CH_FLAGS run) |
| ClickHouse skip-user | `CLICKHOUSE_SKIP_USER_SETUP=1` | `Makefile` (CH_FLAGS) |
| ClickHouse ulimit | `nofile=262144:262144` | `Makefile` (CH_FLAGS) |
| Dev env loader | `Envy.load("config/.env.dev")` | `config/runtime.exs:10` |
| Documented dev flow | Docker PG+CH, `make install`, `make server` | `CONTRIBUTING.md` |
| No docker-compose | dev services are separate `docker run` in Makefile | (repo has no compose file) |

## Verification log (catalog checks)

Flox CLI 1.13.2. Catalog = nixpkgs.

| Package | Verified via | Result |
|---------|--------------|--------|
| `elixir` | `flox show elixir` | latest **1.18.4** (floor 1.11.x). `.tool-versions` 1.20.2 is not in catalog, but 1.18.4 satisfies mix.exs `~> 1.18`. SUPERSEDED — see AI-457 re-verification below: `elixir_1_19` also exists and is closer to the repo's pin. |
| elixir → OTP | `nix eval nixpkgs#elixir.erlang.version` and buildInputs | **erlang-28.5.0.3** is the elixir buildInput → OTP **28.5.0.3 is bundled**, matching `.tool-versions` `erlang 28.5.0.3` exactly. No separate `erlang` install needed. |
| `erlang` (standalone) | `flox show erlang` | latest 28.5.0.3 — confirms OTP 28 line is current; **not installed** (bundled in elixir). |
| `nodejs_23` | `flox show nodejs_23` | **23.11.0** (range 23.6.0–23.11.0). Same major as pin 23.2.0 / Dockerfile 23.11.1. |
| `postgresql` | `flox show postgresql` | **18.4** (also 17.x, 16.5). Matches prod `postgres:18`. See AI-457 re-verification below: latest 18.4 lacks an x86_64-darwin build. |
| `clickhouse` | `flox search --all clickhouse` / `flox show clickhouse` | **present**, latest **26.6.1.1193-stable** (down to 25.12.x shown). See decision below. |
| `gnumake` | `flox show gnumake` | **4.4.1**. |

### AI-457 re-verification (2026-07-16)

**Elixir pin was wrong — `elixir_1_19` exists and was missed.** The
original pass checked only the bare `elixir` pkg-path and concluded 1.18.4
was the catalog ceiling. `flox show elixir_1_19` shows a separate,
newer-line versioned pkg-path topping out at **1.19.5** — much closer to
the repo's `.tool-versions` pin (1.20.2-otp-28) and still well inside
mix.exs's `~> 1.18` floor (permits any `1.x >= 1.18`). Live-verified the
OTP bundling claim directly rather than via `nix eval` (unavailable in
this sandbox without a pinned nixpkgs flake ref):

```
$ flox run -p elixir_1_19 -- elixir --version
Erlang/OTP 28 [erts-16.4.0.3] ...
Elixir 1.19.5 (compiled with Erlang/OTP 28)
```

Confirms OTP 28 is bundled in `elixir_1_19` too, same as bare `elixir` —
no separate `erlang` install needed either way. Manifest now pins
`elixir_1_19` at `1.19.5`.

**postgresql systems mismatch.** `flox show postgresql`'s latest (18.4,
matching prod's `postgres:18`) carries a systems parenthetical excluding
x86_64-darwin — confirmed live. `elixir_1_19` and `nodejs_23` both build
on all four systems, so rather than dropping x86_64-darwin from the whole
environment (over-constraining two packages that don't need it), the
manifest scopes `postgresql.systems` to the three platforms 18.4 actually
supports.

**Activation validation.** `flox activate` (x86_64-linux, throwaway
directory, no real plausible checkout) failed with `constraints for group
'toplevel' are too tight` even with the elixir fix and postgresql systems
scoping in place -- bisected to `elixir_1_19@1.19.5` specifically (bare
`elixir@1.18.4` in the same slot activates fine; the toplevel group has no
single catalog page containing `elixir_1_19` together with `nodejs_23`/
`postgresql`/`gnumake`). Giving `elixir` its own `pkg-group` (and, for the
same reason, giving the systems-scoped `postgresql` its own group too)
resolved it; the manifest now activates cleanly (`mix` correctly reports
its cache path as `elixir/1-19-otp-28`, confirming elixir_1_19 is what
actually gets installed; hook errors past that point are only from the
missing `assets/package.json`/`tracker/package.json` in the scratch test
directory, expected without a real checkout).

## ClickHouse: Flox service vs Docker (the decision)

**Decision: defer ClickHouse to Docker via the repo's `make clickhouse`
target. Do NOT wire `[services.clickhouse]`.** Install `gnumake` so the
target runs; Docker is an external prerequisite (as it already is upstream).

Reasoning, grounded in the repo:
- **Upstream never runs ClickHouse natively.** `CONTRIBUTING.md` says to use
  Docker "for running both Postgres and Clickhouse", and the only mechanism
  provided is `make clickhouse` (a `docker run`). There is no docker-compose
  file at all — Postgres and ClickHouse are independent `docker run`
  invocations in the `Makefile`.
- **Runtime coupling favours the container image.** The `make clickhouse`
  run sets a file-descriptor ulimit (`nofile=262144:262144`),
  `CLICKHOUSE_SKIP_USER_SETUP=1`, and mounts a `config.d` override dir. A
  native Flox `clickhouse` service would have to reproduce the fd ulimit
  (an OS-level concern a Flox service can't set), a writable config/data
  layout, and skip-user-setup — and could not be validated here (no
  `flox activate`). Shipping that unverified would risk a broken "golden".
- **The config mount is not a hard blocker by itself** — `.clickhouse_config`
  is *not committed* (it's an empty local-override bind mount), so ClickHouse
  needs no repo-provided config files. The blocker is the combination of the
  ulimit + upstream's Docker-only posture + inability to validate a native
  service. On balance, Docker is the faithful, lower-risk golden choice.
- **Postgres is different** and IS nativized: the idiomatic Flox
  `[services.postgres]` pattern is well-trodden, and `config/.env.dev`
  connects over plain TCP `127.0.0.1:5432` with trust-friendly dev creds, so
  a native service is a clean, reliable drop-in for `make postgres`.

Net dev flow with this manifest: `flox activate` (starts native Postgres,
installs mix/node deps) → `make clickhouse` (Docker) → `make install`
(`mix ecto.create` + `ecto.migrate` build both DBs, plus geo DB + tracker) →
`make server` → http://localhost:8000.

## Erlang-bundled-in-elixir note

The Flox/nixpkgs `elixir` package is built on `beam.packages.erlang`, whose
erlang is 28.5.0.3 (confirmed: elixir-1.18.4's buildInput store path is
`erlang-28.5.0.3`). Installing a separate `erlang` would shadow/conflict with
the bundled BEAM. The manifest installs **only `elixir`** — matching the task
guidance and the repo's OTP 28 requirement exactly.

## ✗ Items (gaps / imperfect matches — honest)

- **✗ Exact elixir pin unavailable.** `.tool-versions` wants `elixir 1.20.2`;
  catalog max (across all versioned elixir pkg-paths) is `elixir_1_19@1.19.5`.
  Chosen `elixir_1_19` (1.19.5) satisfies the *governing* constraint
  (`mix.exs: "~> 1.18"`) and provides the required OTP 28, but the exact
  `.tool-versions` patch pin is not reproducible from the catalog.
- **✗ Exact Node pin unavailable.** Pin is 23.2.0; `nodejs_23` resolves to
  23.11.0 (Dockerfile itself uses 23.11.1, so the newer patch is arguably
  more faithful to CI than the `.tool-versions` value).
- **✗ ClickHouse not a Flox service.** Deferred to Docker (see decision).
  `expected_services` therefore lists only `postgres`. Catalog does have
  `clickhouse@26.6.1.1193-stable` if a future golden wants to attempt a
  native service (upstream prod is 25.11.x, so there is also a major gap).
- **✗ DB creation not in the hook.** `mix ecto.create`/`migrate` need
  ClickHouse (Docker) up, which the hook can't guarantee, so they stay a
  documented `make install` step rather than an on-activate action.

## Skill-improvement observations

1. **Multi-runtime `.tool-versions` is the anchor, but catalog ceilings
   diverge from patch pins.** `.tool-versions` cleanly enumerated elixir,
   erlang, and nodejs, but two of three pins (elixir 1.20.2, node 23.2.0)
   were ahead of / off the catalog. A golden-manifest skill should codify:
   read `.tool-versions` for the *major/runtime set*, then fall back to the
   language manifest's own floor (`mix.exs "~> 1.18"`) to pick a catalog
   version, and record the gap as a ✗ rather than forcing an exact pin.

2. **Elixir bundles Erlang/OTP — never install `erlang` alongside it, and
   verify the bundled OTP by introspecting the derivation.**
   `nix eval nixpkgs#elixir.erlang.version` (or reading the elixir
   buildInput) is the reliable way to confirm the bundled OTP matches the
   repo's `erlang` pin. Worth a dedicated rule in a BEAM/Elixir skill:
   OTP comes from the elixir package; a separate `erlang` entry is a smell.

3. **"Docker-compose vs Flox service" guidance must generalize to plain
   `docker run` in Makefiles.** This repo has no compose file — its services
   live as `make postgres` / `make clickhouse` `docker run` recipes. The
   decision rubric (config/init mounts, depends_on, ulimits, upstream's
   native-vs-container posture, and *whether a native service can be
   validated*) applies just as well to Makefile `docker run` targets. A
   heavyweight columnar store like ClickHouse with an fd-ulimit requirement
   is a good archetype for "defer to Docker, nativize the lighter SQL DB."
