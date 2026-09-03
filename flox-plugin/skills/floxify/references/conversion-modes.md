# Conversion modes

## Audit Mode (when .flox/ already exists)

Print: `<project-name>/ already uses Flox. Running gap analysis...`

1. Read `.flox/env/manifest.toml` — note installed packages, whether an
   `on-activate` hook exists, and whether a `[profile]` section activates the venv.
2. Run the Phase 1 scan to detect runtimes and services from dep files.
3. Compare: what do dep files imply vs. what's in the manifest?
4. Check that CI exercises every committed Flox artifact:
   - List build targets: `[build.*]` sections in the manifest, plus any
     `.flox/pkgs/*.nix`. Also grep the README for `flox build` — an advertised
     build command counts as a claim even if you missed the target file.
   - Grep the CI configs (`.github/workflows/*.yml`, `.gitlab-ci.yml`,
     `.circleci/config.yml`) for `flox activate`, `flox build`, and
     `install-flox-action`.
   - An environment no CI job activates, or a build target no CI job builds,
     is a gap — the dev loop keeps passing while the committed artifact rots
     (a stale `vendorHash` after a dep bump breaks `flox build` silently;
     `go test` never notices).
5. Print:

```
<project-name>/ — Flox gap analysis

Already in manifest.toml (<N> packages):
  python311       ← .python-version
  nodejs_20       ← .nvmrc
  postgresql_16   ← docker-compose.yml

Possibly missing (detected in dep files, not in manifest):
  redis           → detected via requirements.txt (redis package)
  pkg-config      → detected via requirements.txt (psycopg2 non-binary)

on-activate hook: [present / missing]

CI coverage:
  environment     [exercised by <workflow file> / no CI job activates it]
  build: <target> [built by <workflow file> / not built in CI ← README advertises `flox build <target>`]
```

Omit the `build:` line when there are no build targets. For each CI gap,
suggest the fix without applying it — the environment job per
`references/migration.md` step 5, the build job per the flox skill's
`references/builds.md` § The Build Job Travels With the Target.

If the hook is missing, suggest:
```
No on-activate hook found. To auto-run pip install on activate:
  flox edit
  # Add in [hook] section:
  # on-activate = '''
  #   pip install --quiet -r requirements.txt
  # '''
```

Stop here. **Never modify an existing manifest.**

---

## DevBox Conversion Mode

Print: `<project-name>/ uses DevBox. Converting devbox.json → Flox manifest...`

DevBox and Flox both use the Nix catalog — package names map almost 1:1.

1. Read `devbox.json`. Extract:
   - `packages` — DevBox uses `name@version` syntax; search `"name version"` (e.g. `python@3.12` → search `"python 3.12"`)
   - `shell.init_hook` → maps to `[hook] on-activate` (copy verbatim — it's already Bash)
   - `shell.env` / `env` → maps to `[vars]` (copy key/value pairs directly)

2. Resolve each package via `search_packages`. Name quirks:
   - `rust` / `rustup` → search `"cargo"` and `"rustc"` separately
   - `mysql` or `mysql@8` → search `"mariadb"`
   - `pkgconfig` → search `"pkg-config"`
   - `mongodb` → search `"mongodb"` (community edition shows as `mongodb-ce`)
   - No match → add to ✗ section.

3. If `postgresql`, `redis`, or `mysql` appear in packages AND no `docker-compose.yml` exists:
   apply the PostgreSQL-as-service / Redis-as-service patterns from
   `references/service-patterns.md`.

4. Skip Phase 1 — go straight to Phase 3 with the packages you've extracted.

In the report: `devbox.json → .flox/env/manifest.toml  ·  devbox.json left untouched`

---

## Brewfile Conversion Mode

A `Brewfile` signals `brew bundle` is in use — treat it as a high-priority source in
Phase 1 (not a separate mode).

For each `brew "name"` or `brew "name@version"` line, call `search_packages`:
- `brew "postgresql@16"` → search `"postgresql 16"`
- Skip: `cask "..."` (GUI apps), `tap "..."` (package sources), and system utils:
  `git`, `vim`, `bash`, `grep`, `coreutils`, `wget`, `curl`, `make`
- Naming quirks: `libpq` → `"postgresql"`, `mysql-client` → `"mariadb"`, `openssl@3` → `"openssl"`

No match → add to ✗ section.

---

## Dev Container Full Conversion

When `.devcontainer/devcontainer.json` is present, extract all of:

**`image` field** → parse image name + tag for a runtime hint.
- `devcontainers/python:3.12` → search `"python 3.12"`, `devcontainers/node:22` → search `"nodejs 22"`
- Strip suffixes: `node:22-alpine`, `python:3.12-slim` → strip suffix, search runtime + version
- `ubuntu:22.04`, `debian:bookworm` → base OS only; no runtime to extract
- Rust: search `"cargo"` + `"rustc"`

**`features` field** → for each entry, take the last URI path segment as the tool name;
include `"version"` if present.
- `features/github-cli:1` → search `"gh"`, `features/aws-cli:1` → search `"awscli"`
- `features/kubectl-helm-minikube:1` → search `"kubectl"` + `"helm"` separately
- `features/dotnet:1` with `{"version": "8"}` → search `"dotnet sdk 8"`

**`postCreateCommand`** → copy verbatim into `[hook] on-activate` (it's already shell).
If it's a script reference (e.g. `"scripts/setup.sh"`), note it in the report — don't inline it.

**`containerEnv`** → copy each key/value into `[vars]` directly.

**`forwardPorts`** → note in report as context only; don't configure anything.

**`remoteUser` / `mounts`** → skip (container-specific, no Flox equivalent).
