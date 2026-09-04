# Phase 5: Migration (triggered on demand)

**Trigger:** User says "migrate", "I'm ready", "commit it", "let's go", "yes migrate",
"do it", or any clear affirmation after the Phase 4 report.

**Do NOT run automatically.** Only execute when explicitly requested.

### Steps

**1. Create a branch**

```bash
cd "$TARGET_DIR"
# Check if branch already exists
git show-ref --verify --quiet refs/heads/add-flox-environment \
  && echo "BRANCH_EXISTS" || echo "BRANCH_NEW"
```

- `BRANCH_NEW` → `git checkout -b add-flox-environment`
- `BRANCH_EXISTS` → ask the user: "Branch `add-flox-environment` already exists. Switch to it
  and continue, or use a different name?" Do not switch automatically.

**2. Update the README**

Find the README: `README.md` → `README.rst` → `docs/CONTRIBUTING.md` → `CONTRIBUTING.md`
(first one found). If none exist, create a minimal `README.md` with just the Flox section.

**Most repos already have a README — read it first, then make the smallest possible change.**

Look for a heading containing any of these words (case-insensitive):
"getting started", "development", "setup", "local development", "contributing", "install",
"quickstart", "prerequisites", "usage"

- **Section found**: INSERT these two lines at the top of that section's content,
  before any existing text. Do not remove or rewrite existing instructions.

  ```markdown
  Run `flox activate` to set up your development environment — it installs all runtimes
  and dependencies automatically. ([Install Flox](https://flox.dev/docs/install-flox/install))
  ```

  If the old tool (DevBox, Mise, etc.) has a specific install command in that section,
  replace just that command line. Leave everything else untouched.

- **No matching section found**: INSERT a new `## Getting started` section after the
  first paragraph (after the project description, before any other sections).

  ```markdown
  ## Getting started

  Run `flox activate` to set up your development environment — it installs all runtimes
  and dependencies automatically. ([Install Flox](https://flox.dev/docs/install-flox/install))
  ```

Never rewrite or restructure the README beyond the single insertion point.

**3. Remove old tool config** (context-dependent — never auto-remove, always confirm)

| Detected tool | Action |
|---------------|--------|
| `devbox.json` | Ask: "Remove devbox.json? Flox replaces it completely." If yes: `git rm devbox.json`; also remove `.devbox/` if present |
| `.mise.toml` | Ask: "Remove .mise.toml?" If yes: `git rm .mise.toml` |
| `.tool-versions` | Ask: "Remove .tool-versions? (asdf/mise pin file)" If yes: `git rm .tool-versions` |
| `Brewfile` | Do NOT remove — Homebrew is system-wide. Note: "Brewfile left in place (Homebrew is system-wide; Flox handles project deps)" |
| `.devcontainer/` | Do NOT remove — serves Codespaces/cloud CI. Note: ".devcontainer/ left in place for cloud/Codespaces use" |
| None | Nothing to remove |

**4. Update .gitignore**

Check whether `.flox/cache/` is already gitignored:
```bash
grep -q 'flox/cache' "$TARGET_DIR/.gitignore" 2>/dev/null && echo "ALREADY_IGNORED" || echo "NEEDS_ENTRY"
```

If `NEEDS_ENTRY`: append to `.gitignore`:
```
# Flox local cache (venvs, build artifacts — not shared)
.flox/cache/
```

The `.flox/env/` directory (manifest, lockfile) IS committed — that's the point.
The `.flox/cache/` directory (venvs, cargo target, etc.) is machine-local and should not be.

**5. Wire CI to exercise the environment**

The commit above makes a standing promise — the README now says `flox activate`
is the setup command — and nothing in the repo verifies that promise stays true.
Catalog drift, a hook broken by a dependency bump, a manifest a teammate's change
no longer satisfies: each lands silently, and the first person to find out is the
new contributor the environment was meant to protect. The CI job travels with
the artifact: the workflow ships in the same commit as `.flox/env/`, so the
promise and its check never separate.

**Detect and conform — never force a CI system on the repo.** The repo's
existing CI is the place the check belongs; a workflow file for a vendor the
project doesn't use is clutter at best. Detect first:

| Present in repo | CI system |
|---|---|
| `.github/workflows/` | GitHub Actions |
| `.gitlab-ci.yml` | GitLab CI |
| `.circleci/config.yml` | CircleCI |
| `.buildkite/` | Buildkite |
| `Jenkinsfile` | Jenkins |
| `.woodpecker.yml`, `azure-pipelines.yml`, `.drone.yml` | Woodpecker / Azure / Drone |

Then OFFER — never write CI config silently, whatever the system. One
question, in the agent session, naming what was detected and what the job
verifies:

```
Detected <CI system>. Want a CI job that verifies the dev environment —
flox activate + your own test command — on every PR? [y/N]
```

The job verifies the DEV environment only: it activates and runs the
project's own checks inside it. It never runs `flox build` — packaging is a
separate, deeper step that stays out of onboarding. If the user declines,
put the snippet for their system under Next steps in the summary and move on.

On a yes, conform to what you found:

- **GitHub Actions** — the one system where a standalone file conforms
  cleanly: write `.github/workflows/flox.yml` as a NEW file (existing
  workflows belong to the maintainers — leave every one untouched; if
  `flox.yml` itself already exists, stop and ask for a different filename
  rather than overwriting). The job is the flox skill's
  `references/ci.md` § Complete workflow with three deltas: name it
  `Flox`; set `branches:` to the default branch, not an assumed `main` —
  `git symbolic-ref refs/remotes/origin/HEAD` prints a FULL ref
  (`refs/remotes/origin/main`), so use its basename, and ask the user if
  the ref is unset; and make the `run:` block the `<check command>` below.

- **Single-file systems with an official Flox integration** (GitLab CI,
  CircleCI) — there is no standalone file to add; the check goes inside
  config the maintainers own. Compose the job in that system's idiom (the
  `flox/orb` orb for CircleCI; a job on the `ghcr.io/flox/flox` image for
  GitLab — the flox skill's `references/ci.md` § Other CI systems is the
  source of truth), show the exact snippet, and ask before inserting it.
  If the user declines the edit, hand them the snippet in the summary
  instead.

- **Everything else** (Buildkite, Jenkins, Woodpecker, …) — no official
  integration; community ones exist for some (e.g. Buildkite plugins) but
  are unofficial — read one before recommending it. Don't write config
  here: show the generic pattern (Flox in the runner image, then
  `flox activate -- <check command>`, per the flox skill's
  `references/ci.md` § Other CI systems) and let the user place it.

- **No CI config at all** — ask rather than assume: "No CI config detected —
  which CI does this repo use, if any?" If the answer is GitHub Actions (or
  the repo's remote is on github.com and the user wants it), write the
  workflow file above; otherwise leave the "In CI" hint in step 7's summary
  and move on.

Whichever branch you take, `<check command>` is the project's own test or
build invocation, which Phase 1 already read from its existing CI config,
Makefile, or package.json scripts (`go test ./...`, `pnpm test`,
`cargo build`). Prefer the cheapest command that proves the environment
provides the toolchain. If nothing is detectable, fall back to the runtimes'
version flags and say so in the summary. The flox skill's `references/ci.md`
(§ Pinning, § Install is not activation, the custom-shell pattern) is the
source of truth for the mechanics — read it before writing or proposing CI
config.

If the repo already has a build target (a `[build.*]` section or
`.flox/pkgs/*.nix` — floxify doesn't create these, but audit and migrate can
meet one), leave it out of this step: the question above asked about the dev
environment only, and a yes to that is not consent for a packaging job. Name
the target in step 7's summary and point the maintainer at the flox skill's
`references/builds.md` § The Build Job Travels With the Target — wiring
build verification is its own separately consented change.

**6. Stage and commit**

```bash
cd "$TARGET_DIR"
git add .flox/env/
git add .gitignore
git add README.md   # (or README.rst, CONTRIBUTING.md — whichever was modified)
git add .github/workflows/flox.yml   # or whichever CI file step 5 wrote/edited, if any
# If old tool files were git rm'd, they're already staged
git commit -m "Add Flox development environment"
```

**7. Print migration summary**

```
─────────────────────────────────────────────────────────────────

✓  Committed  (branch: add-flox-environment)

   Modified:  README.md  ← updated dev setup section
   Added:     .flox/env/manifest.toml
   Added:     .github/workflows/flox.yml  ← CI runs <check command> inside the environment
              (or: Modified: .gitlab-ci.yml / .circleci/config.yml — whichever step 5 touched)
   Removed:   devbox.json        ← (or "left in place" for Brewfile / devcontainer)

   Commit:    "Add Flox development environment"

Next steps:
  git push -u origin add-flox-environment
  → open a PR — teammates can try it before it merges

  flox push
  → share environment on FloxHub (optional)
  → teammates: flox activate -r <you>/<project-name>
  → first time? flox auth login

─────────────────────────────────────────────────────────────────
```

Include the CI line only when step 5 wrote or edited a file. When step 5
produced a snippet the user will place themselves, put the snippet under Next
steps. When step 5 ended with no CI change at all, append this instead:

```
  In CI (GitHub Actions, GitLab, CircleCI, etc.):
  → install Flox, then: flox activate -- <your-test-command>
  → see: https://flox.dev/docs/install-flox/install
```

Then ask: "Ready to push to origin? I can run `git push -u origin add-flox-environment`."

### Migration rules

- Never `git push` without explicit user confirmation
- Never remove Brewfile or `.devcontainer/` — they serve different purposes
- Always confirm before `git rm` on any file
- CI is offered, never imposed: every CI change starts with step 5's [y/N]
  question and conforms to the system the repo already uses (the detection
  table). Any edit to existing CI config (`.gitlab-ci.yml`,
  `.circleci/config.yml`, existing workflows) additionally requires showing
  the snippet and getting an explicit yes
- If git is not initialized (`git status` fails): skip branch creation, just update
  the README and note: "No git repo found — commit manually when ready"
- Commit message is always exactly `"Add Flox development environment"` — no variations
