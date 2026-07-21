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

**5. Stage and commit**

```bash
cd "$TARGET_DIR"
git add .flox/env/
git add .gitignore
git add README.md   # (or README.rst, CONTRIBUTING.md — whichever was modified)
# If old tool files were git rm'd, they're already staged
git commit -m "Add Flox development environment"
```

**6. Print migration summary**

```
─────────────────────────────────────────────────────────────────

✓  Committed  (branch: add-flox-environment)

   Modified:  README.md  ← updated dev setup section
   Added:     .flox/env/manifest.toml
   Removed:   devbox.json        ← (or "left in place" for Brewfile / devcontainer)

   Commit:    "Add Flox development environment"

Next steps:
  git push -u origin add-flox-environment
  → open a PR — teammates can try it before it merges

  flox push
  → share environment on FloxHub (optional)
  → teammates: flox activate -r <you>/<project-name>
  → first time? flox auth login

  In CI (GitHub Actions, etc.):
  → install Flox, then: flox activate -- <your-test-command>
  → see: https://flox.dev/docs/install-flox/install

─────────────────────────────────────────────────────────────────
```

Then ask: "Ready to push to origin? I can run `git push -u origin add-flox-environment`."

### Migration rules

- Never `git push` without explicit user confirmation
- Never remove Brewfile or `.devcontainer/` — they serve different purposes
- Always confirm before `git rm` on any file
- If git is not initialized (`git status` fails): skip branch creation, just update
  the README and note: "No git repo found — commit manually when ready"
- Commit message is always exactly `"Add Flox development environment"` — no variations
