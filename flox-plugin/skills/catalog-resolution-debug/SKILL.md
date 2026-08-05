---
name: catalog-resolution-debug
description: >-
  Debug Flox catalog package resolution. Use when user
  says "package not resolving", "wrong version installed",
  "old build", "resolution issue", "why is flox picking
  the wrong package", "candidate pages", "base page",
  "build page", "constraints too tight", or any question
  about why a specific package version is or isn't being
  selected by the resolver. Also use when investigating
  publish issues where new publishes don't appear in
  resolution, or when adding a package to an existing
  environment causes a resolution failure.
---

# Catalog Resolution Debugging

Debug why the Flox catalog resolver picks (or skips)
specific package builds, and why adding packages to an
existing environment can fail with constraint errors.

**Resolution failures are page problems, not
version-conflict problems.** The reflex is to read
`constraints_too_tight` as two packages wanting
incompatible versions of a shared dependency. That is
almost never what it means here: it means no single base
page carries every package in the group. Do the page
analysis first, and fall back to version-conflict
reasoning only once the pages rule it out.

## Establish Context

Work it out yourself first. Ask only for what you cannot
determine, and never open with a questionnaire.

1. **Find the environment.** Look for
   `.flox/env/manifest.toml` in the current directory,
   then in any directory the user named. If there is no
   manifest anywhere, you are debugging a standalone
   package — say so and carry on.

2. **Identify the package group.** Read it from the
   manifest: packages with no explicit `pkg-group` are in
   `"default"`. If the manifest declares more than one
   group, take the one containing the package the user is
   complaining about and say which you picked. Every
   package in a group must resolve to the same base page,
   so the group scopes the whole diagnosis.

3. **Identify the packages involved.** Take the installed
   set from the manifest. Only a package the user is
   *adding* may need asking, and only if their message
   did not already name it.

Produce the diagnosis from what you can read, and state
the assumptions you made. A user corrects a wrong
assumption faster than they answer three questions.

## Parse the Manifest

Read the `manifest.toml` from the environment. Extract
all packages in the target package group:

```toml
# Example manifest entries:
[install]
gh.pkg-path = "gh"                        # default group
python3.pkg-path = "python3"              # default group
claude-code.pkg-path = "flox-ai/claude-code"
claude-code.pkg-group = "claude-code"     # separate group
nodejs_22.pkg-path = "nodejs_22"
nodejs_22.version = "22.14.0"
nodejs_22.pkg-group = "nodejs"            # separate group
```

For each package in the target group, build a
PackageDescriptor:
- `install_id`: the TOML key (e.g., `gh`, `python3`)
- `attr_path`: the `pkg-path` value
- `systems`: the environment's declared systems, **not**
  the local platform. Read `[options] systems` from the
  manifest. If a package carries its own `.systems`, use
  that list for that descriptor instead. If `[options]
  systems` is absent the environment targets **all four**
  (`aarch64-darwin`, `aarch64-linux`, `x86_64-darwin`,
  `x86_64-linux`) — confirm against `manifest.lock`,
  where every locked package records its `system`.
- `version`: the `version` value if present, null
  otherwise
- Skip packages with a `flake` attribute — those are
  not resolved through the catalog

**Never narrow `systems` to your own machine.** Two of
the message types below —
`attr_path_not_found.systems_not_on_same_page` and
`attr_path_not_found.not_found_for_all_systems` — are
multi-system failures by definition. A single-system
reproduction resolves cleanly against the very failure
you were asked to debug, and you will report "works
fine" on a broken environment.

Then apply the environment's `[options]` to **every**
descriptor in the group. These change what resolves, so a
reproduction that omits them is not a reproduction:

| Manifest `[options]` | Descriptor field |
|---|---|
| `allow.unfree = true` | `allow_unfree: true` |
| `allow.broken = true` | `allow_broken: true` |
| `allow.licenses = ["MIT", …]` | `allowed_licenses: ["MIT", …]` |

Those three are the only keys `[options].allow` accepts.
The API also accepts `allow_insecure`,
`allow_pre_releases` and `allow_missing_builds` on a
descriptor, but no manifest key sets them — leave them at
their defaults when reproducing an environment.

The `unfree`, `insecure` and `broken` message types below
are exactly what these flags gate, and `allow.licenses`
produces `unacceptable_licenses`. Drop them and a failing
install becomes a clean reproduction.

Then append the user's new packages as additional
descriptors in the same group.

## Core Concepts

### Two Kinds of Page

Every resolved package sits at the intersection of two
axes:

- **Base page** — the nixpkgs revision the package was
  built against. This is the `page` number in the API
  response. Higher = newer nixpkgs.
- **Build page** — the source repository revision. This
  is `rev_count` / `rev` / `rev_date` in the response.
  Higher rev_count = newer source.

**The resolver picks the highest base page where ALL
packages in the group can be satisfied.** A newer build
(high rev_count) on an old base page can drag the entire
group down — or make resolution impossible if no single
base page has all packages.

### Why This Causes Confusion

**Scenario 1: New publish not picked up.**
A user publishes a new version. The publish succeeds. But
`flox install` still picks the old build because the new
build was evaluated against an old nixpkgs pin (low base
page), while an older build on a newer base page wins.

**Scenario 2: Adding a package breaks the group.**
User has an environment with packages A, B, C all
resolving on base page 950000. They try to add package D,
which only exists on base page 780000. No single base
page has all four packages, so resolution fails with
`constraints_too_tight`.

## Authentication

```bash
TOKEN=$(flox auth token)
```

Use as `Authorization: Bearer $TOKEN` header on all
catalog API calls.

**Keep the token in the variable.** Never echo it, never
paste it into a command you show the user, and never let
it reach the diagnostic table or the final report. Refer
to it only as `$TOKEN`.

## Diagnostic Flow

### Step 1: Resolve with Candidate Pages

Call the resolve endpoint with the full set of packages
(existing + new) requesting candidate pages:

```bash
curl -s -X POST \
  "https://api.flox.dev/catalog/api/v1/catalog/resolve\
?candidate_pages=10" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "items": [{
      "name": "<GROUP_NAME>",
      "descriptors": [
        {
          "install_id": "<ID>",
          "attr_path": "<PATH>",
          "systems": ["<SYSTEM>"],
          "version": "<VERSION_OR_NULL>"
        }
      ]
    }]
  }'
```

**Parameters:**
- `name`: The package group name (usually `"default"`)
- `descriptors`: One entry per package in the group
  (existing from manifest + new from user)
- `systems`: Detect from user's platform
- `candidate_pages=10`: Start with 10; increase if
  needed to see more history

### Step 2: Build the Diagnostic Table

From the response, extract the selected page and all
candidate pages. For each page, show each package's
status. Present a table:

| Base page | Package | Version | Build rev | Build date | Messages |
|-----------|---------|---------|-----------|------------|----------|

Sort by base page descending (highest first = winner).

Mark the selected page. For candidate pages, show any
messages explaining why they weren't selected or why
specific packages couldn't be satisfied on that page.

### Step 3: Analyze the Gap

Check for these patterns:

**No common base page (constraints_too_tight):**
If the resolver returns an error or the selected page
is missing some packages, the packages in the group
don't share a common base page. Identify which package
is the outlier — it only exists on base pages where
other packages don't, or vice versa. This is the most
common issue when adding a new package to an existing
environment.

**Stale base page on newest build:**
If the newest build (highest rev_count) has a lower base
page than older builds, the source repo's nixpkgs input
is pinned to an old revision. The fix is to update the
nixpkgs flake input in the source repo and republish.

**Messages explain rejection:**
If candidate pages have messages, report them. Common
message types:
- `constraints_too_tight` — version/license/etc.
  constraints exclude the page
- `missing_builds` — package exists but not for the
  requested system
- `broken` / `insecure` / `unfree` — package metadata
  flags exclude it
- `unacceptable_licenses` — the package's license is not
  in `allowed_licenses`
- `version_not_found` — version constraint doesn't match
- `change_in_version_format` — the version string's
  format changed between builds
- `attr_path_not_found` — package doesn't exist on
  that page
- `attr_path_not_found.not_in_catalog` — the attr_path is
  not in this catalog at all
- `attr_path_not_found.systems_not_on_same_page` —
  package exists but not for all requested systems on
  this page
- `attr_path_not_found.not_found_for_all_systems` —
  package not available for some requested systems
- `resolution_logic` / `general` — resolver commentary
  rather than a specific exclusion

Every message carries a **level** — `trace`, `info`,
`warning` or `error`. Read it before reporting: a
`trace`/`info` message is the resolver narrating its
work, not a reason resolution failed. Only `error` (and
usually `warning`) belongs in the diagnosis.

Pages also carry `complete`. An incomplete page has not
been fully scraped, so its absence of a package is not
evidence the package is missing.

**No messages, just page ordering:**
If all candidate pages have empty messages and the only
difference is base page number, the issue is purely
which nixpkgs base the builds landed on.

### Step 4: Check Individual Package Builds

To see all known builds of a specific package across
all pages:

```bash
curl -s \
  "https://api.flox.dev/catalog/api/v1/catalog/\
packages/<ATTR_PATH>?page=0&pageSize=50" \
  -H "Authorization: Bearer $TOKEN"
```

This shows `total_count` and individual builds. Use this
to answer: "What base pages does this package exist on?"
Compare against the base pages available for other
packages in the group.

### Step 5: Isolate the Constraint

If resolution fails with multiple packages, try
resolving subsets to isolate which package combination
causes the failure:

1. Resolve just the existing packages (without the new
   ones) — this should succeed and shows the current
   base page
2. Resolve just the new package alone — shows what base
   pages it's available on
3. Compare: if there's no overlap in base pages, that
   explains the failure

## Presenting Results

Always present findings as:

1. **What resolved** (or failed): The selected page with
   package details, or the error
2. **Candidate table**: All candidates sorted by base
   page descending, with per-package status
3. **Diagnosis**: Why the selected page won, or why
   resolution failed — identify the specific package(s)
   causing the constraint
4. **Action items**: What the user should do:
   - Update nixpkgs input in source repo and republish
   - Adjust version constraints
   - Move a package to a separate pkg-group
   - Check system coverage
   - Wait for the catalog to index a newer build

## Multiple Packages / Package Groups

When debugging resolution with multiple packages in the
same group, remember that all packages in a group must
resolve to the **same base page**. A single package
pinned to an old base page can drag the entire group
down. Check each package's candidate pages independently
to find the constraint.

## Re-running After Changes

After the user publishes a new build or updates their
nixpkgs pin, re-run the resolve call to verify the fix.
Compare the new results to the previous diagnostic table
to confirm the base page moved as expected.
