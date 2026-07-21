# Hook snippets by ecosystem

The `[install]` section is built from Phase 2 package names. The non-obvious parts are
the `[hook]` and `[profile]` content — use these snippets verbatim and compose them
when the project has multiple stacks (e.g. Python + Node).

**Python**
```toml
[hook]
on-activate = '''
  if [ ! -d "$FLOX_ENV_CACHE/venv" ]; then
    uv venv "$FLOX_ENV_CACHE/venv" >&2
  fi
  (
    source "$FLOX_ENV_CACHE/venv/bin/activate"
    uv pip install --quiet -r requirements.txt
  )
'''

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
on-activate = '''
  if [ ! -d node_modules ] || { [ -f package-lock.json ] && [ package-lock.json -nt node_modules ]; }; then
    npm install --silent
  fi
'''
```
- pnpm → `pnpm-lock.yaml` staleness check, `pnpm install --frozen-lockfile --silent`
- yarn → `yarn.lock` staleness check, `yarn install --silent`

**Pinned package manager (`packageManager` field / `engines.pnpm`).** When the
repo pins an exact pnpm/yarn (`"packageManager": "pnpm@10.24.0"`), search the
catalog for that EXACT version FIRST — `flox show pnpm_<major>` / `flox show
yarn-berry`, reading the full version list per "Reading `flox show`
correctly" above, not just `Latest:`. If the exact patch resolves, install
it directly with `<id>.pkg-path` + `<id>.version` (see "Emitting an exact
pin" above) — supabase pins `packageManager "pnpm@10.24.0"` and the catalog
carries `pnpm_10@10.24.0` exactly, so the golden installs it directly with
`pnpm_10.version = "10.24.0"`, no corepack involved (live-verified
2026-07-18; `evals/floxify/testdata/gold/supabase.toml`).

Fall back to **corepack** only when the catalog genuinely can't satisfy the
pin — the nearest `pnpm_<major>`/`yarn-berry` version is a gap short of the
exact patch, or an `.npmrc` `engine-strict=true` rejects the nearest
available one. **Verify this against a live `flox show` before trusting any
specific version numbers below** — the catalog moves forward continuously,
so a gap observed on one day can close by the next; treat the mechanism as
the lesson, not the numbers. Two worked cases from the goldens:

- PostHog pins `pnpm@10.29.3`; the nearest catalog patch is `10.29.2` — a
  gap in the version list, not a ceiling (the catalog's `pnpm_10` line
  continues well past `10.29.x`), but still short of the exact repo pin —
  so its golden provisions pnpm through corepack instead (verified
  2026-07-17; `evals/floxify/testdata/gold/posthog.toml`).
- Mastodon pins `packageManager "yarn@4.17.1"`, but its golden installs
  `yarn-berry` with NO `.version` field at all
  (`evals/floxify/testdata/gold/mastodon.toml`) — not because corepack is
  needed here either, but because Yarn Berry doesn't need the catalog
  exact match OR corepack: `.yarn/releases/yarn-<version>.cjs`, checked
  into the repo and referenced by `.yarnrc.yml`, IS the pinned binary — the
  catalog `yarn-berry` package only bootstraps it. When you find a
  checked-in `.yarn/releases/`, install the unpinned catalog package and
  let the repo's own vendored binary self-delegate to the exact version —
  no `<id>.version`, no corepack.

**State the tradeoff when corepack is genuinely needed:** it downloads the
package manager over the network on first activate, unlike a catalog
package (resolved once and reused on every future activation) — an
offline-fragile step that's only worth it when the catalog genuinely can't
serve the exact pin. Provision into a *writable* cache dir (the Nix node
prefix is a read-only store path):

```toml
[hook]
on-activate = '''
  export COREPACK_HOME="$FLOX_ENV_CACHE/corepack"
  mkdir -p "$FLOX_ENV_CACHE/node-bin"
  corepack enable --install-directory "$FLOX_ENV_CACHE/node-bin" pnpm
  export PATH="$FLOX_ENV_CACHE/node-bin:$PATH"
  pnpm install --frozen-lockfile
'''
```

**Go**
```toml
[hook]
on-activate = '''
  export GOPATH="$FLOX_ENV_CACHE/go"
  export GOCACHE="$FLOX_ENV_CACHE/go/cache"
  export GOMODCACHE="$FLOX_ENV_CACHE/go/pkg/mod"
  mkdir -p "$GOPATH" "$GOCACHE" "$GOMODCACHE"
'''

[profile]
bash = 'export GOPATH="$FLOX_ENV_CACHE/go"; export PATH="$GOPATH/bin:$PATH"'
zsh  = 'export GOPATH="$FLOX_ENV_CACHE/go"; export PATH="$GOPATH/bin:$PATH"'
```

**Rust**
```toml
[hook]
on-activate = '''
  export CARGO_HOME="$FLOX_ENV_CACHE/cargo"
  export CARGO_TARGET_DIR="$FLOX_ENV_CACHE/target"
  mkdir -p "$CARGO_HOME" "$CARGO_TARGET_DIR"
'''

[profile]
bash = 'export CARGO_HOME="$FLOX_ENV_CACHE/cargo"; export PATH="$CARGO_HOME/bin:$PATH"'
zsh  = 'export CARGO_HOME="$FLOX_ENV_CACHE/cargo"; export PATH="$CARGO_HOME/bin:$PATH"'
fish = 'set -x CARGO_HOME "$FLOX_ENV_CACHE/cargo"; fish_add_path "$CARGO_HOME/bin"'
```
- `rust-toolchain.toml` is a rustup directive — Flox installs via catalog, not rustup
  (`cargo` and `rustc`, searched separately)
- Maturin (Python extension): also add `maturin` to `[install]` + Python runtime
- **Native deps come from `Cargo.lock`, not crate names.** A `*-sys` crate signals a
  system lib: `pq-sys` → `postgresql` + `pkg-config` (diesel `postgres` feature),
  `openssl-sys` → `openssl`, `zstd-sys` → `zstd`, `libsqlite3-sys` → `sqlite`. Grep the
  lockfile for `-sys` crates and resolve each. **Absence is evidence too** — no
  `openssl-sys` means NO openssl, even if a Dockerfile installs `libssl-dev` (usually a
  runtime-only runner-stage dep). `prost` alone needs no protoc — only `prost-build` /
  `tonic-build` compile `.proto`. `gcc` covers the linker and any cc-built crate.

**Elixir**
```toml
[hook]
on-activate = '''
  export MIX_HOME="$FLOX_ENV_CACHE/mix"
  export HEX_HOME="$FLOX_ENV_CACHE/hex"
  mkdir -p "$MIX_HOME" "$HEX_HOME"
  mix local.hex --force --if-missing >&2
  mix local.rebar --force --if-missing >&2
  [ -f mix.exs ] && mix deps.get --quiet >&2
'''

[profile]
bash = 'export MIX_HOME="$FLOX_ENV_CACHE/mix"; export PATH="$MIX_HOME/escripts:$PATH"'
zsh  = 'export MIX_HOME="$FLOX_ENV_CACHE/mix"; export PATH="$MIX_HOME/escripts:$PATH"'
```
- Erlang/OTP is bundled in the `elixir` package — do NOT add `erlang` separately
- Phoenix: also detect `assets/package.json` and add the Node hook above

**.NET**
```toml
[hook]
on-activate = '''
  export DOTNET_ROOT="$FLOX_ENV_CACHE/dotnet"
  export NUGET_PACKAGES="$FLOX_ENV_CACHE/nuget"
  mkdir -p "$DOTNET_ROOT" "$NUGET_PACKAGES"
'''

[profile]
bash = 'export DOTNET_ROOT="$FLOX_ENV_CACHE/dotnet"; export PATH="$HOME/.dotnet/tools:$PATH"'
zsh  = 'export DOTNET_ROOT="$FLOX_ENV_CACHE/dotnet"; export PATH="$HOME/.dotnet/tools:$PATH"'
```
- Don't auto-run `dotnet restore` on activate — it's slow; let the developer run it

**PHP** — a fixed-bundle interpreter, unlike Python/Node.
```toml
[hook]
on-activate = '''
  composer install --no-interaction
'''
```
- Pick the VERSIONED `phpNN` (from `composer.json` `require.php` / `config.platform.php`);
  the bare `php` pkg-path lags a minor. Pair Composer as `phpNNPackages.composer`
  (interpreter-scoped — there is no top-level `composer` package).
- Extensions come from the `phpNN` build's FIXED default set — you do NOT install `ext-*`
  as packages. Enumerate that set empirically (`flox run -p phpNN -- php -m` — the
  execute-don't-infer rule above) and diff `composer.json` `require`'s `ext-*` against
  THAT output, never against a remembered or source-derived list — the default set is
  broader than source-reading suggests (all 14 of firefly-iii's required ext-*, `xml`
  included, are present in php85). Only an ext genuinely absent from `php -m` needs
  action, and it is NOT `[install]`-closable — it needs a `phpNN.withExtensions`
  `[build]`. Flag that honestly rather than pretend.
- `phpNNExtensions.*` packages are a TRAP: they resolve in `flox show` but a standalone
  install does not load into the prebuilt interpreter. nixpkgs bakes ext→system-lib deps
  (gd→libpng, intl→icu) into the derivation — do NOT over-provision system libs for PHP.
