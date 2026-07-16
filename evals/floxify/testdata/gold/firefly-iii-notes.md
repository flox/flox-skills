# firefly-iii golden manifest — notes

- Repo: https://github.com/firefly-iii/firefly-iii
- Pinned SHA: `a0d70228bc14` (full: `a0d70228bc1401a80e88e273461ec6af2d739374`)
  - Branch: `main` ("Fix quick build."). This tip is the bleeding-edge
    development line (PHP >=8.5, Laravel ^13).
- Ecosystem: `php` (composer). New to Tier 2.
- Tooling: flox 1.13.2, nixpkgs catalog.

## Provenance (repo file -> value)

| Fact | Value | Source file |
|------|-------|-------------|
| PHP version | `>=8.5` | `composer.json` `require.php` |
| PHP platform pin | `8.5` | `composer.json` `config.platform.php` |
| PHP CI version | `8.5` | `.github/workflows/release.yml` L18,L80 |
| PHP ext-* required | 14 exts (below) | `composer.json` `require` ext-* |
| Composer needed | lockfile present | `composer.lock` (505 KB) |
| Laravel | `^13` | `composer.json` `require.laravel/framework` |
| Node frontends | v1 + v2 workspaces | `package.json` `workspaces` |
| v1 build tool | laravel-mix (webpack) | `resources/assets/v1/package.json` |
| v2 build tool | vite `=8.1.0` | `resources/assets/v2/package.json` |
| Node engine pin | none | (no `.nvmrc`, no `engines`) |
| DB default | `mysql` :3306 db `firefly` | `.env.example` L94-99 |
| DB alternates | pgsql, sqlite | `.env.example` L90-93 comments |
| Redis | optional, tcp :6379 | `.env.example` L129-152 |
| Redis client | `predis/predis` (pure PHP) | `composer.json` `require` |
| Cache/session default | `file` (not redis) | `.env.example` L129-130 |
| Queue default | `sync` | `.env.example` L282 |

No `Dockerfile` / `docker-compose*` in this repo (they live in the separate
`firefly-iii/docker` repo), so DB/Redis facts come from `.env.example`.

## Required PHP extensions (composer.json `require`)

`ext-bcmath ext-curl ext-fileinfo ext-iconv ext-intl ext-json ext-mbstring
ext-openssl ext-pdo ext-session ext-simplexml ext-tokenizer ext-xml
ext-xmlwriter` (14). Note: no ext-gd, no ext-zip, no ext-redis in `require`
(CI's setup-php adds zip/gd for packaging only; predis avoids phpredis).

## Verification log (`flox show` / `flox search --all`)

| Query | Result |
|-------|--------|
| `flox show php` | php@8.4.23 (8.4 series only — too old) |
| `flox show php85` | **php85@8.5.8** ✓ (matches require.php) |
| `flox show php84` | php84@8.4.23 |
| `flox search --all php` | php73..php85 versioned pkgs exist |
| `flox show composer` | ERROR: no such pkg-path (not top-level) |
| `flox show php85Packages.composer` | **php85Packages.composer@2.10.2** ✓ |
| `flox search --all nodejs` | nodejs_14.._26 |
| `flox show nodejs_22` | **nodejs_22@22.23.1** ✓ (LTS, vite-8 ok) |
| `flox show nodejs_24` | nodejs_24@24.18.0 (also fine) |
| `flox show mariadb` | **mariadb@11.4.12** ✓ |
| `flox show postgresql` | postgresql@18.4 (alternate) |
| `flox show redis` | **redis@8.8.0** ✓ |
| `flox show php85Extensions.{bcmath,intl,mbstring,curl,gd,pdo,pdo_mysql,pdo_pgsql,dom,xml}` | all @8.5.8, outputs `dev,out` |
| `flox show php85Extensions.json` | ERROR: no such pkg-path (json is core) |

## KEY LEARNING — how the catalog exposes PHP + extensions

This is the tricky part of the PHP/composer ecosystem.

1. **PHP interpreter is a versioned pkg-path.** The bare `php` is pinned to
   the 8.4 series (php@8.4.23). To get 8.5 you must use `php85` (→ 8.5.8).
   Same shape as `php83`, `php84`. Runtime-detection regex should therefore
   match `php` or `php8x`, not just `php`.

2. **Composer is under the interpreter's package set,** not top-level.
   `composer` alone fails; `php85Packages.composer` (built against php85)
   resolves to 2.10.2. Pair composer with the same php-version prefix.

3. **Extensions are individual catalog packages** at
   `php85Extensions.<name>@8.5.8` (each with `dev` + `out` outputs), but the
   `php85` package is a *prebuilt* `php.buildEnv` with a FIXED default
   extension set compiled in. You get exactly that set from `[install]`; you
   cannot add to it by also installing a standalone `phpXXExtensions.*`
   package — the extra `.so` is not wired into the prebuilt binary's
   `extension_dir`/ini scan, so it will not load.

4. **Default php85 extension set** (verified against nixpkgs
   `interpreters/php/default.nix` `withExtensions [...]` array +
   `top-level/php-packages.nix`):
   bcmath calendar curl ctype dom exif fileinfo filter ftp gd gettext gmp
   iconv intl ldap mbstring mysqli mysqlnd openssl pcntl pdo pdo_mysql
   pdo_odbc pdo_pgsql pdo_sqlite pgsql posix readline session simplexml
   sockets soap sodium sysvsem sqlite3 tokenizer xmlreader xmlwriter zip
   zlib.

5. **firefly coverage: 13 of 14 required ext-* are default-covered.**
   Covered: bcmath, curl, fileinfo, iconv, intl, mbstring, openssl, pdo,
   session, simplexml, tokenizer, xmlwriter (+ pdo_mysql for the default DB,
   pdo_pgsql for the alternate). ext-json = PHP core, always present.

6. **✗ ext-xml is the single gap.** The bare `xml` (expat/SAX) extension is
   absent from the default set AND is not pulled in transitively —
   `dom`/`simplexml`/`xmlwriter` list only `libxml2` as a buildInput (no
   `internalDeps` on `xml`), and `xmlreader`'s only internalDep is `dom`
   (verified in `pkgs/top-level/php-packages.nix`). So enabling the XML DOM
   family does not enable `ext-xml`.
   - The proper fix is a rebuilt interpreter: Flox `[build]` running
     `php85.withExtensions (e: e.enabled ++ [ e.all.xml ])` (or equivalent).
     That's out of scope for an `[install]`-only golden manifest.
     `php85Extensions.xml@8.5.8` exists but does not help standalone (see #3).
   - The golden manifest's bootstrap therefore uses
     `composer install --ignore-platform-req=ext-xml` as a *flagged*
     workaround so the env still bootstraps; runtime XML-import code paths
     (OFX/CAMT import, etc.) may still require a real ext-xml build.

## DB choice

App default `DB_CONNECTION=mysql` → wired `mariadb@11.4.12` (drop-in MySQL)
as `[services.mariadb]` on TCP 127.0.0.1:3306. Alternates documented inline:
`postgresql@18.4` for pgsql, sqlite for file DB — both driver-covered by the
default php85 build (pdo_pgsql, pdo_sqlite). Redis wired as `[services.redis]`
on 6379 (optional, but idiomatic for a Laravel cache/queue backend).

## ✗ / caveats

- **ext-xml**: not in the default php85 build; needs a `withExtensions`
  rebuild for guaranteed runtime XML support (see key learning #6).
- **x86_64-darwin**: `php85` has no build for Intel macOS (systems are
  aarch64-darwin, aarch64-linux, x86_64-linux). `[options].systems` reflects
  this.
- **v3 workspace**: `release.yml` builds `--workspace=v3`, but `package.json`
  only declares v1/v2 workspaces — bleeding-edge inconsistency in this tip;
  the manifest builds the two workspaces that actually exist.
- Services + hook commands could not be executed (`flox activate` is
  out of scope for this task); they follow standard Flox idioms but are
  not activation-verified.

## Skill-improvement observations

1. **PHP is a "fixed-bundle interpreter" ecosystem, unlike Python/Node.**
   Guidance for PHP repos should say: pick the versioned `phpNN` pkg-path
   (bare `php` lags a minor), pair `phpNNPackages.composer`, then diff the
   repo's `ext-*` list against the KNOWN default `php.buildEnv` set. Only the
   *difference* is a problem — and the difference cannot be closed with
   `[install]`; it needs a `withExtensions` `[build]`. A skill should carry
   the default-extension list (or a `flox`-queryable way to get it) so the
   ext-* diff is mechanical rather than requiring a nixpkgs source dive.

2. **ext-\* → catalog mapping has three tiers, worth codifying:**
   (a) PHP-core exts with no package (json, and generally the always-on
   ones) — treat as auto-satisfied; (b) exts in the default `php.buildEnv`
   set — satisfied by installing `phpNN`; (c) exts outside the default set
   (here: `xml`) — NOT satisfiable via `[install]`, require `withExtensions`.
   Standalone `phpNNExtensions.*` packages are a trap: they resolve in
   `flox show` but don't load into the prebuilt interpreter. A skill should
   warn against installing them expecting them to "just work."

3. **ext-\* → system-lib mapping is handled for you here.** Because the
   catalog exposes prebuilt extensions/interpreter, the classic
   "gd → libpng/libjpeg, intl → icu" native-lib wiring is already inside the
   nixpkgs derivations — no manual system-lib installs needed (contrast with
   apt/Docker-based PHP setups). Worth stating explicitly so PHP handling
   doesn't over-provision system libraries.
