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
   - Grep whatever CI configs exist — `.github/workflows/*.yml`,
     `.gitlab-ci.yml`, `.circleci/config.yml`, `.buildkite/`, `Jenkinsfile`,
     `.woodpecker.yml`, `azure-pipelines.yml`, `.drone.yml` — for `flox`
     usage: `flox activate`, `flox build`, `install-flox-action`, the
     `flox/orb` orb, the `ghcr.io/flox/flox` image, or a flox Buildkite
     plugin. Judge coverage against the CI system the repo actually uses,
     not against GitHub Actions.
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

## Nix Conversion Mode

Use when `flake.nix` or `shell.nix` exists and there is no `.flox/` and no
`devbox.json`. detect.py's `nix` block (Phase 1a) has the facts: `kind`,
`pinned`, `has_overlays`, `has_builds`, `dev_shells`, `envrc`.

Print: `<project-name>/ uses Nix (<flake.nix|shell.nix>). Converting the dev shell → Flox manifest. Your Nix files stay untouched.`

The Flox catalog is nixpkgs, so a Nix dev shell converts closely: same
package names, same versions. Flox sits on top of the Nix the team already
uses; frame it that way in everything you print.

**Hard rules for this mode:**
- Never modify, delete, reformat or "replace" `flake.nix`, `flake.lock`,
  `shell.nix`, `default.nix`, `nix/`, or `.envrc`. Never run `nix flake
  lock`/`update`. Every eval below passes `--no-write-lock-file`: without
  it, evaluating a flake that has no `flake.lock` CREATES one.
- Never write a local path into the manifest (`path:`, `git+file:`, an
  absolute directory). It works on this machine only.
- Only the dev shell converts. A flake's `packages`, `apps`, `checks`,
  `nixosModules` and `overlays` outputs stay with the flake.

### 1. Run detect.py, then evaluate the dev shell

Run Phase 1a's detect.py command first (it supplies the `nix` block and the
language facts). Then evaluate the dev shell to get the real package list
and versions. Use the shell named `default` (or the only one); if
`dev_shells` lists others, convert `default` and name the rest in the
report. `<skill-dir>/scripts/flox.sh run -p nix` supplies Nix; no system
Nix is needed.

Pick the systems first: the flake's own (`eachDefaultSystem` means all four,
an explicit `systems = [...]` list means those, restricted to the four Flox
supports), or for `shell.nix` the four Flox defaults. Evaluate once per
system (evaluation does not build, so other systems work from this machine).
`APPLY` is the same for both kinds:

```bash
cd "$TARGET_DIR"
APPLY='d: map (p: { name = p.pname or p.name; version = p.version or ""; }) (builtins.filter builtins.isAttrs (d.buildInputs ++ d.nativeBuildInputs ++ d.propagatedBuildInputs ++ d.propagatedNativeBuildInputs))'
# flake.nix (repeat per system; the flake must be tracked by git to be visible).
# Older flakes use `devShell.<system>` instead: if the attribute is missing, retry with that.
"<skill-dir>/scripts/flox.sh" run -p nix -- nix --extra-experimental-features 'nix-command flakes' \
  eval --no-write-lock-file --json ".#devShells.<system>.default" --apply "$APPLY" > /tmp/floxify-nix-eval-<system>.json
# shell.nix (one run; it has no per-system outputs):
"<skill-dir>/scripts/flox.sh" run -p nix -- nix --extra-experimental-features 'nix-command flakes' \
  eval --impure --json --expr 'let s = import ./shell.nix; in if builtins.isFunction s then s {} else s' \
  --apply "$APPLY" > /tmp/floxify-nix-eval.json
```

Then write the merged list to `/tmp/floxify-nix-eval.json`, one entry per
package: `{"name", "version", "attr", "systems"}`. `attr` is the attribute
name the file uses (`python312`, `nodejs_20`), read from the file;
`systems` lists the systems whose eval contained it. Drop stdenv
plumbing the file does not name itself (`stdenv`, `bash`, `coreutils`,
setup hooks, `*-hook`).

**If the eval fails, or is still running after about 5 minutes** (private flake inputs, no network, a
broken flake): don't retry in a loop and don't try to repair it. Read the
file instead, build the same list without versions, and put this line in
the report: `Nix eval unavailable (<one-line reason>); converted from reading
<file>, versions not checked.`

A flake with no `devShells` (only `packages`) has nothing to convert here:
continue with normal Phase 1, and say in the report that the flake's builds
were left alone.

### 2. Map the dev shell to the manifest

| In the Nix dev shell | Goes to |
|---|---|
| `packages` / `buildInputs` / `nativeBuildInputs` | `[install]`, same nixpkgs attribute name as `pkg-path` |
| `inputsFrom = [ x ]` | expand: x's build inputs are in the eval list; install them too |
| `${pkgs.X}` used inside `shellHook`, env values, or `writeShellScriptBin` helpers | `[install]` too. The eval does not list these (they aren't in `packages`), so read them from the file. In the hook, replace `${pkgs.X}/bin/tool` with plain `tool` |
| `writeShellScriptBin "name" ''...''` helpers in `packages` (`pg-start`, `db-reset`) | a shell function of the same name in `[profile] common`, so the developer keeps the command |
| `shellHook` | `[hook] on-activate`. Keep commands that set up the project; drop `echo` banners and anything that re-enters Nix (`nix develop`, `nix-shell`) |
| Literal env attrs (`FOO = "bar";`, `env = { ... }`) | `[vars]`. A computed value (`"${pkgs.foo}/lib"`) goes in the hook as `export FOO="$FLOX_ENV/lib"` |
| `eachDefaultSystem` / `systems = [...]` | `[options] systems`, restricted to the four Flox systems |
| Postgres / Redis / MySQL started by hand in `shellHook` | `[services]`, using `references/service-patterns.md` |

Only explicitly declared env vars convert. Variables Nix setup hooks set on
their own (`PKG_CONFIG_PATH`, `CMAKE_PREFIX_PATH`, `NIX_CFLAGS_COMPILE`) are
not copied; Flox sets its own.

A package that appears only on some systems' evals gets
`systems = [...]` on its `[install]` entry.

**Language package sets** (`python3.withPackages (ps: [...])`,
`nodePackages.*`, `ghcWithPackages`, `rubyPackages`): if the repo has its
own package-manager files (`requirements.txt`, `pyproject.toml`,
`package.json`, `Gemfile`), install only the interpreter and let Phase 1's
normal hook install the packages. If it doesn't, install each one as a
catalog package (`python312Packages.numpy`).

### 3. Versions

Pin only **runtimes, toolchains and databases** (Python, Node, Ruby, Go,
Rust/cargo/rustc, Java/JDK, Elixir/Erlang, PHP, Deno, Bun, Zig, Postgres,
Redis/Valkey, MySQL/MariaDB, MongoDB) to the version the eval reports,
recorded reason `matches flake.lock` (or `matches the nixpkgs pinned in
shell.nix`). This is a deliberate exception to Phase 2's "default
unpinned" rule: the team pinned these already, and a silent upgrade on
conversion reads as "Flox broke my setup".

- Prefer the versioned attribute the file uses (`python312`, `nodejs_20`,
  `postgresql_16`). If the file uses a bare name (`python3`, `go`), resolve
  the versioned pkg-path that matches the eval's version with `flox.sh show`.
- If the catalog doesn't have the eval's exact version, use the closest
  version it has and list it in the report as a version change (from → to).
  Never write a version the catalog can't serve.
- Everything else (jq, git, ripgrep, linters) stays unpinned; `manifest.lock`
  freezes it. If a floating package's **major** version differs from the
  eval's, list it as a version change too.
- Unpinned `shell.nix` (`pinned: false`): Phase 2's normal rules. In the
  report, say this is the first time the team gets identical versions.

### 4. Overlays and packages from other flakes

An overlay changes the nixpkgs package set. Flox can't change the catalog,
so handle each package by where it comes from:

| Package | Do |
|---|---|
| Plain nixpkgs, untouched | Convert normally |
| Exported by a public flake input (`rust-overlay`, `fenix`, `zig-overlay`) | `<id>.flake = "github:<owner>/<repo>/<rev>#<attr>"`, rev from `flake.lock`'s `locked.rev` for that input. If the input only provides an overlay (for example `rust-bin.stable.<ver>.default` from rust-overlay), use the closest catalog package instead (`cargo` + `rustc` + `rustfmt` + `clippy`, pinned to the eval's version) and record it as a version change if it differs |
| From the repo's OWN overlay, and the repo is on GitHub and the flake exports that package (`packages.<system>.<name>`) for every manifest system | `<id>.flake = "github:<org>/<repo>/<rev>#<name>"` with the pushed rev. Tell the user: it compiles on every machine (no binary cache has it), relock to pick up overlay changes, private repos need git credentials |
| From the repo's own overlay, not exported, not pushed, or not on GitHub | Don't install it. List it under "couldn't convert" with the reason |
| An overlay that swaps something under everything (patched OpenSSL, custom stdenv, `config.allowUnfree`-style settings) | No Flox equivalent. Say plainly the flake keeps doing that part. Convert the rest |

For any customer overlay, the recommended next step is a Flox build:
move the override into `.flox/pkgs/<name>.nix`, `flox build`, then
`flox publish` to the org's catalog, so teammates and CI install it by name,
prebuilt. Offer it as a next step ("Ask me to set that up"); the `flox`
skill does the build work.

### 5. Continue with Phase 1, then Phase 2 and 3

Nix mode supplies the system packages; it does not replace the rest of
floxify. Continue with Phase 1 (language files, package managers,
services) and merge:

- The Nix dev shell wins on runtimes and system tools (it's what the team
  actually runs). If `.python-version`, `.nvmrc`, `go.mod` or similar
  disagree with it, keep the Nix version and report the mismatch.
- Phase 1's hooks (`uv sync`, `npm ci`, `bundle install`) still go in.

In Phase 3c Step 4, pass the eval to verify.py:
`... verify.py "$DETECT_JSON" "$TARGET_DIR/.flox/env/manifest.toml" --nix-eval /tmp/floxify-nix-eval.json`
(skip the flag if the eval was unavailable). Every advisory
`nix-package-not-installed` must either be fixed or appear in the report's
"couldn't convert" list.

### 6. Report additions

- Header: `flake.nix → .flox/env/manifest.toml  ·  flake.nix and flake.lock left untouched`
  (for shell.nix: `shell.nix → .flox/env/manifest.toml  ·  shell.nix left untouched`)
- **Version changes** (from → to), if any. Omit the section if none.
- **Couldn't convert**: each item, why, and the next step.
- Other dev shells not converted, by name, if any.
- If `has_builds`: `Your flake also builds <names>. Those stay with the flake; ask me to move them to flox build when you want.`
- If `envrc` is `use flake` / `use nix`: `.envrc runs <use flake>; swap it for "flox activate" when you're ready (left untouched).`

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
