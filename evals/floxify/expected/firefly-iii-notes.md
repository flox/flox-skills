# firefly-iii golden manifest — notes

- Repo: https://github.com/firefly-iii/firefly-iii
- Pinned SHA: `a0d70228bc14` (full: `a0d70228bc1401a80e88e273461ec6af2d739374`)
  - Branch: `main` ("Fix quick build."). This tip is the bleeding-edge
    development line (PHP >=8.5, Laravel ^13).
- Ecosystem: `php` (composer). New to real-world.
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

4. **Default php85 extension set — verify against the running interpreter,
   not the nixpkgs source.** AI-457 re-verified via `flox run -p php85 --
   php -m` (the exact `flox` idiom SKILL.md prescribes for "does this build
   include a given extension" questions) instead of reading
   `interpreters/php/default.nix`. Live module list:
   bcmath calendar Core ctype curl date dom exif fileinfo filter ftp gd
   gettext gmp hash iconv intl json ldap lexbor libxml mbstring mysqli
   mysqlnd openssl pcntl pcre PDO pdo_mysql PDO_ODBC pdo_pgsql pdo_sqlite
   pgsql Phar posix random readline Reflection session SimpleXML soap
   sockets sodium SPL sqlite3 standard sysvsem tokenizer uri **xml**
   xmlreader xmlwriter Zend OPcache zip zlib.

5. **firefly coverage: 14 of 14 required ext-* are default-covered.**
   Covered: bcmath, curl, fileinfo, iconv, intl, json (PHP core), mbstring,
   openssl, pdo, session, simplexml, tokenizer, **xml**, xmlwriter (+
   pdo_mysql for the default DB, pdo_pgsql for the alternate). There is no
   gap — every extension `composer.json` requires ships in the default
   `php85` build.

6. **CORRECTION (AI-457): ext-xml is NOT a gap — an earlier pass got this
   wrong.** A prior version of this golden claimed `xml` (expat/SAX) was
   absent from the default set based on a *reasoned* nixpkgs source read
   (checking `dom`/`simplexml`/`xmlwriter`'s `buildInputs` for an
   `internalDeps` reference to `xml`, finding none, and concluding it must
   be missing). That reasoning was never checked against the actual running
   interpreter. `flox run -p php85 -- php -m` shows `xml` loaded — it is
   compiled into the default `php85` build directly (not "pulled in" via
   another extension's `internalDeps`, which was the wrong signal to check
   in the first place). Lesson for `flox show`/build-content questions:
   SKILL.md's own guidance is "when the question is package CONTENTS,
   don't infer it from the name or from source-reading — execute it." This
   is exactly that failure mode, corrected the way the skill prescribes.
   The manifest no longer carries `composer install
   --ignore-platform-req=ext-xml` — it was a workaround for a problem that
   does not exist, and the real-world.jsonl rubric that penalized manifests for
   NOT claiming this gap has also been corrected (see AI-457 ticket).

## AI-457 activation validation (resolution-tested, not functionally tested)

`flox activate` (x86_64-linux, throwaway directory, no real firefly-iii
checkout) resolved and activated cleanly, no `pkg-group` split needed —
php85/php85Packages.composer/nodejs_22/mariadb/redis apparently share a
common catalog page. The hook ran through its file-existence checks
correctly (`.env.example` absent in the scratch dir triggered the
expected `cp` failure before `composer install`/`npm install` would even
run) rather than failing on package resolution — confirming the ext-xml
fix didn't introduce a resolution problem. No outputs fix was needed here:
`mariadb`'s only declared outputs are `man`+`out` (no separate `dev`/`lib`
split exists for this package to add), and every PHP extension is
precompiled into the `php85` binary itself rather than linked at our
install time.

## DB choice

App default `DB_CONNECTION=mysql` → wired `mariadb@11.4.12` (drop-in MySQL)
as `[services.mariadb]` on TCP 127.0.0.1:3306. Alternates documented inline:
`postgresql@18.4` for pgsql, sqlite for file DB — both driver-covered by the
default php85 build (pdo_pgsql, pdo_sqlite). Redis wired as `[services.redis]`
on 6379 (optional, but idiomatic for a Laravel cache/queue backend).

## ✗ / caveats

- **x86_64-darwin**: `php85` has no build for Intel macOS (systems are
  aarch64-darwin, aarch64-linux, x86_64-linux). `[options].systems` reflects
  this.
- **v3 workspace**: `release.yml` builds `--workspace=v3`, but `package.json`
  only declares v1/v2 workspaces — bleeding-edge inconsistency in this tip;
  the manifest builds the two workspaces that actually exist.
- Services + hook commands were not executed against a real checkout
  (`flox activate` was out of scope for this task's original capture);
  they follow standard Flox idioms. AI-457 later resolution-tested the
  manifest as a whole (see "AI-457 activation validation" below), but
  that is NOT the same as running the mariadb/redis services or the
  composer/npm install steps against real project files.

## Skill-improvement observations

1. **PHP is a "fixed-bundle interpreter" ecosystem, unlike Python/Node.**
   Guidance for PHP repos should say: pick the versioned `phpNN` pkg-path
   (bare `php` lags a minor), pair `phpNNPackages.composer`, then diff the
   repo's `ext-*` list against the interpreter's ACTUAL default set —
   checked with `flox run -p phpNN -- php -m`, not reasoned from the
   nixpkgs source (see key learning #6: a source-only read produced a false
   gap here). A skill should carry this exact command as the verification
   step so the ext-* diff is mechanical and grounded in the running binary.

2. **ext-\* → catalog mapping has two tiers, worth codifying:**
   (a) PHP-core exts with no package (json, and generally the always-on
   ones) — treat as auto-satisfied; (b) exts in the default `php.buildEnv`
   set — satisfied by installing `phpNN` and confirmed via `php -m`, not
   inferred. If a real gap is ever found this way, closing it needs a
   `withExtensions` `[build]` — standalone `phpNNExtensions.*` packages are
   a trap: they resolve in `flox show` but don't load into the prebuilt
   interpreter. A skill should warn against installing them expecting them
   to "just work."

3. **ext-\* → system-lib mapping is handled for you here.** Because the
   catalog exposes prebuilt extensions/interpreter, the classic
   "gd → libpng/libjpeg, intl → icu" native-lib wiring is already inside the
   nixpkgs derivations — no manual system-lib installs needed (contrast with
   apt/Docker-based PHP setups). Worth stating explicitly so PHP handling
   doesn't over-provision system libraries.

4. **"Reasoning from source" is not a substitute for executing the binary.**
   The original ext-xml claim followed a plausible-looking chain (check
   `internalDeps` on related extensions, find none, conclude `xml` is
   absent) that was simply asking the wrong question — `xml` doesn't need
   to be pulled in via another extension's `internalDeps` if it's compiled
   in directly. `flox run -p php85 -- php -m` answers the actual question
   ("is this extension loaded") in one command with no derivation-graph
   reasoning required. SKILL.md's Phase 2 reading discipline (§4, "package
   CONTENTS... execute it") exists precisely to prevent this failure mode.
