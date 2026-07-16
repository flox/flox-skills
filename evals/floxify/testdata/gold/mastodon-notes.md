# Golden manifest notes — mastodon @ 52e9ec7814fc

Repo: https://github.com/mastodon/mastodon
Pinned SHA: `52e9ec781` ("Update dependency react-easy-crop to v6.2.2 (#39753)")
Ecosystem: Ruby (Rails 8.1 monolith) + Node streaming/API service.

> Note: this checkout is a *future* state of Mastodon (Ruby 4.0.6,
> Rails 8.1, bundler 4.0.13, Node 24.18) that post-dates the assistant's
> training cutoff. Every value below is read from the repo, not memory.

---

## 1. Provenance table (fact -> source file:line -> value)

| Fact | Source | Value |
|------|--------|-------|
| Ruby version | `.ruby-version` | `4.0.6` |
| Ruby version | `Gemfile.lock` (RUBY VERSION, ln 1100) | `ruby 4.0.6` |
| Ruby constraint | `Gemfile:4` | `ruby '>= 3.3.0', '< 4.1.0'` |
| Ruby (Docker) | `Dockerfile` ARG RUBY_VERSION | `4.0.6` |
| Bundler | `Gemfile.lock` (BUNDLED WITH, ln 1103) | `4.0.13` |
| Rails | `Gemfile:8` | `rails '~> 8.1.0'` |
| Node version | `.nvmrc` | `24.18` |
| Node (Docker) | `Dockerfile` ARG NODE_MAJOR_VERSION | `24` |
| Node engines | `package.json` engines.node / `streaming/package.json` | `>=22` |
| Yarn | `package.json` packageManager | `yarn@4.17.1` (Yarn Berry v4) |
| Workspaces | `package.json` workspaces | `["." , "streaming"]` |
| Postgres | `docker-compose.yml` db.image | `postgres:14-alpine` |
| Redis | `docker-compose.yml` redis.image | `redis:7-alpine` |
| Elasticsearch (optional) | `docker-compose.yml` (commented `es:`) | `8.19.15` |
| pg gem | `Gemfile:12` | `pg '~> 1.5'` (lock 1.6.3) -> needs libpq/pg_config |
| ruby-vips gem | `Gemfile:23` | `ruby-vips '~> 2.2'` (lock 2.3.0) -> needs libvips |
| libvips build | `Dockerfile` ARG VIPS_VERSION | `8.18.4` |
| ffmpeg build | `Dockerfile` ARG FFMPEG_VERSION | `8.1.2` |
| charlock_holmes | `Gemfile:28` | `~> 0.7.7` (lock 0.7.9) -> needs system ICU |
| ICU (Docker) | `Dockerfile` apt: `libicu76`, `libicu-dev` | ICU 76 |
| idn-ruby | `Gemfile` (`gem 'idn-ruby'`) | -> needs GNU libidn v1 |
| libidn | `Aptfile` (`libidn12`), `Dockerfile` `libidn-dev` | GNU libidn v1 |
| google-protobuf | `Gemfile.lock` | `4.35.1` (precompiled gem, no system dep) |
| nokogiri | `Gemfile.lock` | `1.19.4` (precompiled gem) |
| DB env vars | `config/database.yml` | `DB_HOST/DB_PORT/DB_USER/DB_PASS/DB_NAME` |
| DB default name (dev) | `config/database.yml` | `mastodon_development` |
| Redis env vars | `lib/tasks/mastodon.rake`, `streaming/redis.js` | `REDIS_URL` (or REDIS_HOST/PORT/PASSWORD) |
| Streaming DB/Redis | `streaming/database.js`, `streaming/redis.js` | reads `DATABASE_URL`/`DB_*` and `REDIS_URL`/`REDIS_HOST` |

---

## 2. Verification log (flox search / flox show)

| Command | Result | Decision |
|---------|--------|----------|
| `flox show ruby` | latest 3.4.9; unversioned attr has **no 4.0** | do not use `ruby` |
| `flox search --all ruby_4` | `ruby_4_0` exists | candidate |
| `flox show ruby_4_0` | versions 4.0.0-preview3 .. **4.0.5** (latest) | pick `ruby_4_0`, pin 4.0.5 |
| `flox show nodejs_24` | latest **24.18.0** | `nodejs_24` @ 24.18.0 (exact `.nvmrc` match) |
| `flox show postgresql_14` | latest 14.23; outputs incl. dev/lib | `postgresql_14` (service + pg gem) |
| `flox show redis` | latest 8.8.0; **7.2.7** newest 7.x | `redis` pinned 7.2.7 (match `redis:7`) |
| `flox show vips` | latest **8.18.3** | `vips` (repo builds 8.18.4 -> 1 patch behind) |
| `flox show ffmpeg` | latest **8.1.2** | `ffmpeg` (exact match to Dockerfile) |
| `flox search/show icu` | `icu` @ **76.1** | `icu` (matches Docker `libicu76`) |
| `flox search/show libidn` | `libidn` @ **1.44** (GNU v1) | `libidn` (matches `libidn12` soname) |
| `flox search --all yarn` | `yarn`, `yarn-berry`, `yarn-berry_4` | `yarn-berry` |
| `flox show yarn-berry` | latest **4.14.1** (Yarn v4) | `yarn-berry` (repo pins 4.17.1) |
| `flox show pkg-config` | 0.29.2 | `pkg-config` |
| `flox show gcc` | 15.2.0 | `gcc` |
| `flox show gnumake` | 4.4.1 | `gnumake` |

### 2a. AI-457 re-verification (per-system availability + outputs, 2026-07-16)

| Command | Result | Decision |
|---------|--------|----------|
| `flox show nodejs_24` | `24.18.0` (aarch64-darwin, aarch64-linux, x86_64-linux only) | missing x86_64-darwin |
| `flox show ffmpeg` | latest `8.1.2` (aarch64-darwin, aarch64-linux, x86_64-linux only) | missing x86_64-darwin |
| `flox show libidn` | latest `1.44` (aarch64-darwin, aarch64-linux, x86_64-linux only) | missing x86_64-darwin |
| `flox show postgresql_14` | `Outputs: dev, doc, jit, lib, man*, out*, ...` | `out`+`man` default; `lib`/`dev` NOT default — added for pg gem |
| `flox show vips` | `Outputs: bin*, dev, man*, out` | `bin`+`man` default; `libvips.so` lives in `out` (not default) |
| `flox show icu` | `Outputs: dev, out*` | `out` default; `dev` (headers) NOT default — added for charlock_holmes |
| `flox show libidn` | `Outputs: bin*, dev, out, info, devdoc` | `bin` only default; `out`/`dev` NOT default — added for idn-ruby |

All three of nodejs_24, ffmpeg, and libidn are HARD runtime dependencies
(Node streaming service, video transcoding, idn-ruby respectively), so
`[options].systems` drops `x86_64-darwin` entirely rather than scoping a
per-package `systems` override — the environment cannot activate on Intel
macOS regardless of which of the three is asked for. `ruby_4_0`,
`postgresql_14`, `icu`, and `yarn-berry` DO have x86_64-darwin builds
(verified, all four systems, no parenthetical restriction) but there is no
reason to keep the whole environment claiming a platform it can't actually
run on for three of its own hard deps.

### AI-457 activation validation

This golden was actually `flox activate`d (x86_64-linux, in a throwaway
directory seeded only with `.flox/env/manifest.toml` -- no real mastodon
checkout, so `bundle install`/`yarn install` fail on a missing Gemfile/
package.json as expected; that is a scratch-test artifact, not a manifest
defect). The systems fix alone was not enough: with only `nodejs_24`,
`ffmpeg`, and `libidn` fixed, resolution failed with `constraints for
group 'toplevel' are too tight` -- the catalog has no single page
containing `ruby_4_0@4.0.5` and `nodejs_24@24.18.0` together with
`postgresql_14`/`redis`/`vips`/`icu`/`libidn`. Confirmed by reverting to
the pre-AI-457 manifest: it failed with a *different*, more specific error
(`No version compatible with '24.18.0' found for 'nodejs_24' on
'x86_64-darwin'`), proving the toplevel conflict was masked behind the
systems bug and not a new regression from this fix. Isolating `ruby_4_0`
and `nodejs_24` each into their own `pkg-group` resolved it -- the
manifest now activates cleanly with `flox activate -c "echo __ok__"`.

---

## 3. Chosen versions + mismatches vs catalog

| Package | Repo wants | Catalog pick | Match |
|---------|-----------|--------------|-------|
| ruby_4_0 | 4.0.6 | 4.0.5 | 1 patch behind (4.0.6 not yet in catalog) |
| nodejs_24 | 24.18 | 24.18.0 | exact |
| postgresql_14 | 14.x | 14.23 | major match |
| redis | 7.x | 7.2.7 | major match (catalog default is 8.x) |
| vips | 8.18.4 | 8.18.3 | 1 patch behind |
| ffmpeg | 8.1.2 | 8.1.2 | exact |
| icu | 76 | 76.1 | exact major |
| libidn | v1 (libidn12) | 1.44 | correct library (v1, not libidn2) |
| yarn-berry | 4.17.1 | 4.14.1 | Yarn v4 line; minor behind |

Only genuinely-behind pins are ruby (4.0.6->4.0.5) and vips
(8.18.4->8.18.3); both are single patch releases and functionally
equivalent for a dev environment. yarn-berry's minor lag is
inconsequential — Mastodon's `packageManager` field lets corepack
(bundled with `nodejs_24`) fetch the exact 4.17.1 on demand.

---

## 4. Services wired and why

- **postgres** (HARD): Rails ActiveRecord + the Node streaming server both
  connect. `docker-compose.yml` makes `db` a `depends_on` for web,
  streaming, and sidekiq. Wired socket-only (`-k /tmp`,
  `listen_addresses=""`), cluster under `$FLOX_ENV_CACHE/postgres`, trust
  auth (dev-only). `DB_HOST=/tmp` in `[vars]` points both runtimes at the
  socket. `postgresql_14` doubles as the build dep for the `pg` gem
  (provides `pg_config`/libpq).
- **redis** (HARD): cache, Sidekiq queue, and streaming pub/sub. Wired with
  a unix socket in `/tmp` plus `localhost:6379` (so `REDIS_URL` stays
  simple), data under `$FLOX_ENV_CACHE/redis`.
- **elasticsearch** (NOT wired): only referenced commented-out in
  `docker-compose.yml` (8.19.15) and via the optional `chewy` gem. Full-text
  search is an opt-in prod extra, not a runtime hard-dep. Deliberately
  omitted from the golden dev manifest.

Both runtimes were confirmed to read the same env surface:
`streaming/database.js` reads `DATABASE_URL`/`DB_HOST`/`DB_NAME`/`DB_PORT`/`DB_USER`;
`streaming/redis.js` reads `REDIS_URL`/`REDIS_HOST`. So a single `[vars]`
block wires the Rails side and the Node side together.

---

## 5. Items needed but NOT in catalog (✗)

- None blocking. Every runtime, service, and native lib resolved.
- Soft ✗: exact `ruby 4.0.6` (catalog max 4.0.5) and exact `libvips 8.18.4`
  (catalog 8.18.3) — both off by one patch, non-blocking.

---

## 6. OBSERVATIONS for improving the floxify skill

1. **Ruby dual-signal + versioned pkg-path.** A naive model sees
   `.ruby-version 4.0.6` and reaches for `ruby`, but `flox show ruby`
   tracks a *different* (3.4.x) attr and has no 4.0 at all. The correct
   move is the versioned pkg-path `ruby_4_0` (like `nodejs_24`). The skill
   should teach: for Ruby/Node/Python, always try the `<lang>_<major>[_<minor>]`
   pkg-path first and only fall back to the bare name. Also: when the exact
   patch isn't in the catalog (4.0.6 vs 4.0.5), pin the closest and record
   it — don't invent `version = "4.0.6"`, which would fail resolution.

2. **The `<runtime>` name lies about versions.** `flox show ruby` shows a
   version ceiling (3.4.9) that has nothing to do with what `ruby_4_0`
   offers. A model that "verifies" ruby only via `flox show ruby` would
   wrongly conclude 4.x is unavailable and downgrade. Verification MUST
   enumerate the versioned pkg-paths, not just the bare name.

3. **Dual-runtime wiring is one env surface, not two.** The instinct is to
   treat the Rails app and the Node streaming service as separate
   environments. They aren't: `config/database.yml` and
   `streaming/database.js` read the *same* `DB_*` vars, and both read
   `REDIS_URL`. The skill should look for a shared env contract before
   splitting a repo into two manifests. One `[vars]` block + both
   `bundle install` and `yarn install` in a single `on-activate` is the
   idiomatic shape for a Rails+Node monolith.

4. **Native gem deps hide in the Dockerfile and Aptfile, not the Gemfile.**
   The Gemfile names `ruby-vips`, `charlock_holmes`, `idn-ruby`, `pg` — but
   the *system* libraries they link (libvips, ICU, GNU libidn, libpq) are
   only visible by cross-referencing the `Dockerfile` apt-get lines and the
   `Aptfile`. A model that scans only the Gemfile misses every C-extension
   system dep. Critical trap: `idn-ruby` needs **GNU libidn v1**
   (`libidn`, soname `libidn12`), NOT the more modern `libidn2` — picking
   `libidn2` compiles but breaks at runtime. Confirmed via `Aptfile`
   (`libidn12`) and `Dockerfile` (`libidn-dev`).

5. **ffmpeg is a runtime dep, not just a build dep.** It never appears in
   the Gemfile; it's compiled in the Dockerfile and shelled out to at
   runtime for video transcoding (kt-paperclip). A Gemfile-only scan would
   omit it entirely.

6. **Precompiled gems are a false-negative for system deps.** `nokogiri`
   (1.19.4) and `google-protobuf` (4.35.1) ship fat/precompiled native
   gems, so they do NOT need system libxml2/protobuf on a glibc host — but a
   model might over-add them. Conversely `charlock_holmes` does *not* vendor
   ICU and genuinely needs it. The skill needs a heuristic for "vendors its
   own C lib" vs "links system": ruby-vips/charlock_holmes/idn-ruby/pg link
   system; nokogiri/google-protobuf/hiredis-client vendor.

7. **Don't wire commented-out compose services.** Elasticsearch is present
   only as a commented `es:` block. A naive scan that greps image tags would
   wire a heavy ES service the dev path doesn't require. Respect comment
   state and `depends_on` graphs when deciding hard vs optional.

8. **Toolchain is required, not optional, for the Ruby side.** Because
   several gems compile from source under Flox (nixpkgs), `gcc`, `gnumake`,
   and `pkg-config` must be in `[install]` or `bundle install` fails. Node's
   side is prebuilt, so it needs none — the asymmetry is easy to miss.

### Process note (environment hazard, not skill content)
This scratchpad's `golden/` dir is shared with concurrent teammate agents
capturing OTHER repos. A generic bookkeeping filename (`.clonedir`) I wrote
was clobbered by another agent cloning getsentry/sentry, silently
redirecting a later read to the wrong repo. Caught it via a HEAD/remote
check. Lesson for any multi-agent golden-capture run: namespace ALL
scratch files per-repo (e.g. `mastodon.clonedir`), or pin the clone path
inline rather than via a shared file.
