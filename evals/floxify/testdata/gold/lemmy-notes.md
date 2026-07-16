# Golden manifest notes — lemmy @ 9311de3b662b

Repo: https://github.com/LemmyNet/lemmy
Pinned SHA: `9311de3b662b` ("Add report resolve reason (#6612)")
Ecosystem: Rust (single Cargo workspace, edition 2024, 24 member crates,
builds one `lemmy_server` binary). Backend only — the TypeScript UI lives
in the separate LemmyNet/lemmy-ui repo.

> Every value below is read from the checked-out repo at this SHA and
> every catalog name/version is verified with `flox show` / `flox search`.
> Nothing is from memory.

---

## 1. Provenance table (fact -> source file:line -> value)

| Fact | Source | Value |
|------|--------|-------|
| Rust channel | `rust-toolchain.toml` | `channel = "1.95"` |
| Rust (Docker) | `docker/Dockerfile:2` | `ARG RUST_VERSION=1.95` |
| Edition | `Cargo.toml:3` (workspace.package) | `edition = "2024"` |
| MSRV | `Cargo.toml:9` | `rust-version = "1.92"` |
| Resolver | `Cargo.toml:70` | `resolver = "3"` |
| Clippy in CI | `.woodpecker.yml:156-163` | `cargo clippy --workspace --tests --all-targets --all-features -- -D warnings` |
| Clippy lints | `Cargo.toml:72-109` | large `[workspace.lints.clippy]` deny list |
| Rustfmt config | `.rustfmt.toml` | `edition = "2024"`, custom style |
| diesel | `Cargo.toml:154-160` | `diesel 2.3.7` features incl. **`postgres`** |
| native libpq | `Cargo.lock` | `pq-sys 0.7.5` (pulled by `diesel` `postgres` feature) |
| diesel-async | `Cargo.toml:162` | `diesel-async 0.8.0` (pure-Rust pool over tokio-postgres) |
| migrations | `Cargo.toml:161` | `diesel_migrations 2.3.1` (embedded; runs on server start) |
| TLS stack | `Cargo.toml:165-215` | actix `rustls-0_23`, reqwest `rustls-no-provider`, `rustls "ring"` |
| ring (C build) | `Cargo.lock` | `ring` present -> needs `cc` |
| zstd (C build) | `Cargo.lock` | `zstd-sys` (actix `compress-zstd`) -> needs `cc` |
| prost | `Cargo.lock` | `prost` + `prost-derive`, **no `prost-build`** (runtime only, via `extism`) |
| Postgres image | `docker/docker-compose.yml:84` | `pgautoupgrade/pgautoupgrade:18-alpine` (PG 18) |
| DB default conn | `config/defaults.hjson:9` | `postgres://lemmy:password@localhost:5432/lemmy` |
| DB dev bootstrap | `scripts/start_dev_db.sh` | `CREATE USER lemmy ... SUPERUSER; CREATE DATABASE lemmy OWNER lemmy;` |
| DB env override | `crates/utils/src/settings/mod.rs:50` | `LEMMY_DATABASE_URL` overrides config |
| Config path | `crates/utils/src/settings/mod.rs:11` | `DEFAULT_CONFIG_FILE = "config/config.hjson"` |
| pict-rs | `docker/docker-compose.yml:63` + `config/defaults.hjson:16-22` | `asonix/pictrs:0.5`, `Option<PictrsConfig>` (optional) |
| Runner apt pkgs | `docker/Dockerfile:56` | `libssl-dev libpq-dev ca-certificates curl git` (runtime image) |
| Git deps | `Cargo.lock` | **0** `source = "git+..."` entries (git not needed to build) |
| No Redis | codebase | cache is `moka` (in-memory), no redis dep |

---

## 2. Verification log (flox show / flox search)

| Command | Result | Decision |
|---------|--------|----------|
| `flox show cargo` | latest 1.96.1; **1.95.0** present | `cargo` @ 1.95.0 (matches rust-toolchain "1.95") |
| `flox show rustc` | latest 1.96.1; **1.95.0** present | `rustc` @ 1.95.0 |
| `flox show clippy` | latest 1.96.1; **1.95.0** present | `clippy` @ 1.95.0 |
| `flox show rustfmt` | latest 1.96.1; **1.95.0** present | `rustfmt` @ 1.95.0 |
| `flox search --all postgresql` | `postgresql_18` exists | candidate (matches compose PG 18) |
| `flox show postgresql_18` | latest **18.4**; outputs incl. `lib`, `dev` | `postgresql_18` (service + libpq build dep) |
| `flox show pkg-config` | 0.29.2 | `pkg-config` |
| `flox show gcc` | 15.2.0 | `gcc` |

`cargo` and `rustc` were searched SEPARATELY (they are distinct catalog
attrs, not one "rust" package). Both, plus `clippy`/`rustfmt`, expose an
exact `1.95.0` that matches the repo's pinned channel.

---

## 3. Chosen versions + mismatches vs catalog

| Package | Repo wants | Catalog pick | Match |
|---------|-----------|--------------|-------|
| cargo | 1.95 (channel) | 1.95.0 | exact |
| rustc | 1.95 (channel) | 1.95.0 | exact |
| clippy | (toolchain) | 1.95.0 | matches rustc |
| rustfmt | (toolchain) | 1.95.0 | matches rustc |
| postgresql_18 | PG 18 (compose) | 18.4 | major match |
| pkg-config | — | 0.29.2 | only version |
| gcc | — | 15.2.0 | latest |

No behind-catalog pins. The repo's channel `1.95` maps cleanly to catalog
`1.95.0`. (Catalog latest is 1.96.1, which would also satisfy edition
2024 + MSRV 1.92, but the golden pins the exact declared channel.)

Note: `.woodpecker.yml` runs `rustfmt` via nightly (`rustup component add
rustfmt --toolchain nightly`); Flox has no rustup, so the golden installs
stable `rustfmt 1.95.0`, which supports `edition = "2024"` and is correct
for local formatting. The nightly pin is a CI nicety, not a build need.

---

## 4. rust-toolchain.toml -> catalog mapping (the key Rust move)

`rust-toolchain.toml` is a **rustup** directive. Flox does NOT run rustup;
it installs Rust from the CATALOG as the discrete attrs `cargo` + `rustc`
(+ `clippy`/`rustfmt`). The mapping is:

```
rust-toolchain.toml  channel = "1.95"
        |
        v
[install] cargo   @ 1.95.0
          rustc   @ 1.95.0
          clippy  @ 1.95.0   (from CI clippy step)
          rustfmt @ 1.95.0   (from .rustfmt.toml)
```

The channel string `"1.95"` (no patch) resolves to catalog `1.95.0`. If a
repo pins a full `x.y.z` not yet in the catalog, pin the nearest and
record it — never invent a `version` that fails resolution.

---

## 5. Native-dependency chain (the crux for a Rust-at-scale repo)

Rust crates that need SYSTEM libraries are invisible in a naive scan; they
surface only by reading `Cargo.toml` features and the `*-sys` / cc-using
crates in `Cargo.lock`:

| Rust crate (feature) | Native `-sys` / build crate | System need | Manifest package |
|----------------------|-----------------------------|-------------|------------------|
| `diesel` **`postgres`** | `pq-sys 0.7.5` | libpq + headers/pg_config | `postgresql_18` |
| `pq-sys` build script | `pkg-config` | locate `libpq.pc` | `pkg-config` |
| `rustls` **`ring`** | `ring` (cc) | C compiler | `gcc` |
| actix **`compress-zstd`** | `zstd-sys` (cc, bundled) | C compiler | `gcc` |
| any Rust binary | — | linker (`cc`) | `gcc` |

So the whole native surface collapses to **libpq (via postgresql_18) +
pkg-config + gcc**. `PQ_LIB_DIR="$FLOX_ENV/lib"` is exported in the hook so
`pq-sys` links deterministically even if the `libpq.pc` discovery path
differs. A single `postgresql_18` install serves double duty: the build
dep (libpq/pg_config) and the runtime service.

---

## 6. Service wired and why

- **postgres** (HARD, wired): the only datastore. diesel-async connects at
  runtime; `diesel_migrations` (embedded) builds the schema on first
  `lemmy_server` start, so the `lemmy` DB must exist first. Wired to
  self-init a cluster under `$FLOX_ENV_CACHE/postgres/data`, listen on a
  `/tmp/lemmy-postgres` unix socket **and** `127.0.0.1:5432`, trust auth
  (dev-only), and bootstrap the `lemmy` role + `lemmy` database via a
  background `pg_isready`->`createdb` loop (sentry-golden idiom).
  Listening on TCP loopback lets `LEMMY_DATABASE_URL` stay byte-identical
  to the repo default `postgres://lemmy:password@localhost:5432/lemmy`
  (config/defaults.hjson), avoiding fragile socket-URL encoding across
  lemmy's two DB drivers (libpq for migrations, tokio-postgres for the
  async pool).

### pict-rs decision (NOT wired)
`pict-rs` (docker-compose `asonix/pictrs:0.5`) is the image host. In lemmy
it is `Option<PictrsConfig>` (crates/utils/src/settings/structs.rs:19) with
a placeholder default URL — the server runs fine without it; only image
upload/hosting is disabled. It is a standalone binary in another ecosystem,
not a catalog dev-tool trivially wired here. Left as a documented OPTIONAL
external service in the hook comment, not a Flox service. This matches the
"don't wire it unless trivial" guidance.

---

## 7. Items considered but NOT installed (✗) — and why

- **openssl / openssl-sys** ✗ — the task hinted at it, but `Cargo.lock`
  has **zero** `openssl-sys`, `openssl`, or `native-tls`. The stack is
  rustls end-to-end (actix `rustls-0_23`, reqwest `rustls-no-provider`,
  `rustls "ring"`). The Dockerfile installs `libssl-dev` only in the
  RUNTIME image (Dockerfile:56), not the builder — it is defensive/legacy,
  not a build linkage. Installing openssl would add an unused package.
- **protobuf / protoc** ✗ — the task hinted at it, but `prost` appears in
  `Cargo.lock` WITHOUT `prost-build`. `prost` here is runtime-only
  (encode/decode for the `extism` wasm-plugin protocol); with no
  `prost-build` there is **no `.proto` compilation at build time**, so no
  `protoc` / `protobuf` package is needed. Verified by grep: 0
  `prost-build` entries.
- **git** ✗ — 0 `git+` sources in `Cargo.lock`, and modern cargo uses the
  sparse crates.io index (no git). The runner image installs `git` for
  runtime, not the build.
- **redis** ✗ — lemmy caches with `moka` (in-memory); no redis dependency.
- **gnumake / cmake / jq** ✗ — not required by the Rust build. (`jq` is
  only used by `scripts/start_dev_db.sh` to URL-encode a socket path; the
  Flox service wires the DB directly and does not need it.)

Net: contrary to two of the task's native-dep hints (openssl, protobuf),
the grounded evidence says NEITHER is required. The golden reflects the
repo, not the hint.

---

## 8. OBSERVATIONS for improving the floxify skill

1. **Rust native deps live in `Cargo.lock`'s `*-sys` crates, not the
   dependency names.** The high-signal move for a Rust repo is: read
   `Cargo.toml` for feature flags (`diesel` + `postgres`), then grep
   `Cargo.lock` for the `-sys` and cc-using crates (`pq-sys`, `*-sys`,
   `ring`, `cc`). The presence of `pq-sys` — not the string "diesel" — is
   what proves a native libpq linkage. The skill should teach a canonical
   `-sys` grep (`pq-sys|openssl-sys|libz-sys|zstd-sys|*-sys|ring|cc`) and
   map each hit to a system package. Absence is as informative as presence:
   no `openssl-sys` => no openssl, even if a Dockerfile installs libssl-dev.

2. **`rust-toolchain.toml` is a rustup file; Flox uses the catalog.** The
   skill must translate `channel = "1.95"` into catalog `cargo` + `rustc`
   (searched SEPARATELY — they are distinct attrs, there is no single
   "rust" package), each pinned to `1.95.0`, and add `clippy`/`rustfmt`
   only when the repo actually uses them (CI clippy step, `.rustfmt.toml`).
   A model must NOT try to reproduce rustup semantics.

3. **`prost` without `prost-build` means NO protoc.** protobuf is a
   build-time need ONLY when a `build.rs` compiles `.proto` via
   `prost-build`/`tonic-build`. Runtime-only `prost` (here, transitively
   via `extism`) needs no `protoc` and no `protobuf` package. The skill
   should gate "add protobuf" on the presence of `prost-build` /
   `tonic-build` / `protobuf-src` in `Cargo.lock`, not on `prost` alone —
   otherwise it over-adds a heavyweight build tool. Same shape as the
   Mastodon "precompiled gem" false-negative, inverted: a runtime crate is
   a false-POSITIVE for a build tool.

4. **A Dockerfile's RUNTIME apt list is not the build dep list.** lemmy's
   `libssl-dev`/`libpq-dev` appear only in the final `runner` stage; the
   `builder` stage (cargo-chef) installs nothing extra. Reading apt lines
   without tracking which multi-stage target they belong to over-adds
   packages (openssl) the Rust build never links. The skill should attribute
   each `apt install` to its `FROM ... AS <stage>` and treat runner-stage
   packages as runtime-only.
