# AI-512 — Skill drift detection (working doc / paused design)

**Status:** 🟡 ON HOLD — brainstorming done, design agreed, spec not yet finalized. Switching gears; resume from "Open questions / resume point" at the bottom.
**Ticket:** [AI-512](https://linear.app/floxdotdev/issue/AI-512) — "Set up automation to detect drift between Flox docs, features, and skills" (Todo, 2 pts, project "Flox's Skills: Consolidate + Eval-Gate + Retire MCP", assignee Alan).
**Last updated:** 2026-08-04.

---

## 1. Scope boundary (decided)

AI-512 is scoped to **this repo (flox-skills) only**: keep `flox-plugin/skills/**` aligned with current Flox, checked against the installed `flox` CLI and the `flox/docs` repo as **read-only** sources of truth.

**Explicitly NOT in scope** — the ecosystem-wide release checklist. That is the separate, broader ticket **[AI-8](https://linear.app/floxdotdev/issue/AI-8) "AI content stays current with Flox releases"** (Backlog/Medium, different project "Flox in AI Marketplaces"), which spans flox-agentic skills, floxdocs `llms.txt`, `flox/forge`, and `flox/forge-plugin` and is framed as a release-process checklist. AI-512 does **not** touch forge/forge-plugin, does not watch the flox release pipeline, and does not file cross-repo issues.

## 2. What actually drifts

The `flox` SKILL.md is dense with hard, checkable claims about the product — these are what go stale:
- Command/flag surface: `flox run -p … --`, `flox activate -m dev|run`, `flox activate allow/deny`, `flox containerize --runtime docker`, `flox show`, …
- Manifest schema-version table (which schema gates what: `1.12.0`→`auto-start`, `1.13.0`→sandbox modes, `1.14.0`→`[plugins]`).
- Manifest keys & enums: `runtime-packages`, `sandbox = "off|warn|enforce|pure"`, `is-daemon`.
- Install methods (brew/pkg/deb/rpm; "no `curl | bash` installer exists").
- Package-name idioms: `gbenchmark`, `catch2_3`.

The existing `skill_toml_lint.py` only checks that TOML *snippets parse* — it does not check that prose CLI claims still hold. That gap is AI-512.

## 3. Sources of truth (confirmed)

| Source | Gives us | Notes |
|---|---|---|
| Installed `flox` CLI (v1.14.0 here) | Real command + flag surface via `flox <cmd> --help`, recursively | Deterministic, offline, free. Strongest signal. |
| `flox/docs` (GitHub, MDX/Mintlify, actively maintained) | The "docs" half: `man/`, `concepts/`, `languages/`, `tutorials/`, a `FLOX_VERSION` pin | Light clone; high value for the (deferred) LLM docs layer. |
| `flox/flox` (GitHub) | CLI source in `cli/` (Rust serde = schema ground truth), `VERSION`, changelog | Heavy clone; only if we go after schema/changelog drift. |

## 4. Decision log (agreed with Alan)

1. **Drift scope:** BOTH — a deterministic surface gate PLUS a scheduled LLM docs-comparison report. Surface-first. (Matches repo's free-per-PR-gate / dispatch-only-paid culture.)
2. **Mechanism = Approach C (curated "canary facts" registry):** a small committed JSONL of load-bearing checkable claims, each `{claim, how-to-verify, skill-back-link}`, verified against live flox. Chosen over A (golden CLI-surface diff — too indirect) and B (parse all prose — too fuzzy/broad). Verified the registry pattern is genuinely maintained (see §7).
3. **Shift left + suggest the fix.** The real value is catching drift **at edit time (pre-commit / pre-PR)** with near-zero overhead and **proposing the correction**, not an after-the-fact report. Goals: (a) accuracy on every update, (b) reduce mental+actual workload, (c) suggest changes so the fix rides the PR / a pre-commit hook proposes it.
4. **v1 = POC, local-first, layered & tested.** Ship the deterministic core locally, harden it, add layers with tests each time. Immediate value over perfection.
5. **Suggest ambition v1 = (a)+(b):** detect + point at the stale line, plus deterministic auto-suggestions where derivable. Defer (c) LLM prose rewrites until proven valuable.

## 5. The design

### Architecture — three small pieces
- **Registry** `evals/drift/tasks/claims.jsonl` — one line per checkable fact, back-linked to the skill text.
- **Checker** `evals/drift/check.py` — reads registry, verifies each claim against local `flox`, reports PASS/DRIFT naming skill file + line. Pure-stdlib, offline.
- **Suggester** — `--suggest` mode on the checker; deterministic proposed edit where derivable.

**Placement:** new `evals/drift/` suite (own `check.py`, `tasks/`, `tests/`), per the repo's "one directory per suite" convention. Sibling of `skill_toml_lint` (which checks shipped TOML; this checks shipped CLI claims) but spans both `flox` and `floxify` skills, so it earns its own dir. *(OPEN — see §8.)*

### Registry format (example)
```jsonl
{"id": "activate-mode-flag", "kind": "flag_exists", "command": ["activate"], "flag": "--mode", "skill_ref": {"file": "flox-plugin/skills/flox/SKILL.md", "quote": "flox activate -m dev|run"}}
{"id": "run-p-command", "kind": "command_exists", "command": ["run"], "skill_ref": {"file": "flox-plugin/skills/flox/SKILL.md", "quote": "flox run -p <pkg> -- <cmd>"}}
{"id": "containerize-runtime-flag", "kind": "flag_exists", "command": ["containerize"], "flag": "--runtime", "skill_ref": {"file": "flox-plugin/skills/flox/SKILL.md", "quote": "flox containerize --runtime docker"}}
```
The `quote` is verified to still exist in the skill file — if it's gone, the registry entry itself is stale and gets flagged (mirrors the repo's pinned "known limitations" discipline). This two-way link makes suggestions targetable and keeps the registry from rotting silently.

### Check kinds in v1 (only the two that provably work offline)
| kind | verifies against `flox <cmd> --help` | on drift |
|---|---|---|
| `command_exists` | subcommand present in the help tree | flag the skill line; print current command list |
| `flag_exists` | flag present in that subcommand's `--help` | flag the skill line; print that command's current flags |

**Honest limit on auto-suggest (b):** confident for "this is simply gone" / "a new thing exists". A confident **rename** substitution is a guess → v1 suggests by *surfacing the current surface next to the stale line*, not silently rewriting. Confident rewrites are what deferred layer (c) is for.

**Deferred check kinds** (later layers, each added + tested on its own): `schema_version_current` (needs a non-obvious probe — see §6), `package_resolves` (catalog tier, needs network), `enum_values`.

### Local dev UX + layering
- **CLI:** `python3 evals/drift/check.py` (offline, non-zero exit on drift); `--suggest` prints proposed edits. Runs after `flox activate`, like every suite here.
- **Hook:** lightweight committed git-hook (`.githooks/pre-commit` + documented `git config core.hooksPath .githooks`), runs the checker **only when a skill file is staged**. Deliberately NOT adopting the pre-commit framework (repo has none — see §7).
- **Layer plan** (each shippable + tested before the next):
  - **L0 (this POC):** registry + checker + `command_exists`/`flag_exists` + `--suggest` + unit tests + seed registry (~8–10 real claims).
  - **L1:** git-hook + README docs.
  - **L2:** PR CI gate (path-filtered, beside `skill_toml_lint`) + catalog-tier `package_resolves`.
  - **L3 (decide, don't assume):** LLM suggestions (c) + scheduled sweep for flox-shipped drift on untouched lines.

### Testing (RED-first, per repo policy)
Checker is a pure function of *(registry, flox-help-output)*. Unit tests mock the flox probe at its subprocess boundary (repo's established pattern), feed canned `--help` text → assert drift caught + suggestion correct; plus one live smoke test. Written RED first: seed a deliberately-wrong claim, watch it flag, then the real registry.

## 6. Verified facts / gotchas (probed on 2026-08-04)

- ✅ Flag surface parses cleanly: `flox activate --help` lists flags as `-m, --mode=ARG`. `command_exists`/`flag_exists` are solid, offline.
- ✅ `flox` v1.14.0 installed at `/usr/local/bin/flox`. Top-level `flox --help` gives a clean command list.
- ❌ **Schema-version probe via `flox init` does NOT work** — a fresh `flox init` writes a *commented* template with no visible `version =`/`schema-version =` line near the top. So "read a fresh init's first line" is a dead end. `schema_version_current` needs a different derivation (candidate ideas: attempt `flox edit` with a too-new schema and parse the max-supported version from the error; or read `flox/flox` source) — deferred out of v1 for this reason.
- ⚠️ CI installs flox via `flox/install-flox-action@…main` = **latest**, so CI's flox version floats. A per-PR surface gate would go red for every PR whenever flox releases a surface change (that IS the drift signal, but it blocks unrelated work). Implication: the PR gate (L2) should be path-filtered to skill changes and/or the "flox-shipped drift on untouched lines" case belongs to the scheduled sweep, not the per-PR gate.

## 7. Repo context worth remembering

- **Culture:** CI gates are **deterministic + free**; paid/LLM checks are **dispatch-only, never block** (see `evals/README.md`, `.github/workflows/evals.yml`). Our design mirrors this exactly.
- **Registry pattern is real:** `screening.jsonl` = 46 entries, `tasks.jsonl` = 31, grown across ~15 eval-jsonl commits / multiple tickets, under an enforced "every skill change ships an eval, RED first" policy. Caveat: curation is concentrated in one maintainer (Bill = 162/194 commits) — an argument FOR the shift-left/auto-suggest goal (less reliance on a human remembering).
- **Skills change often:** 79 commits touched skill content → drift is a live risk.
- **Closest cousin to copy patterns from:** `evals/flox/skill_toml_lint.py` (structural/catalog two-tier, offline mode, throwaway-env driving, subprocess-boundary mocking in tests) and its CI job `skill-toml-lint` in `evals.yml` (path-filtered, zero API spend).
- **No pre-commit framework / no `core.hooksPath`** currently — the local hook is net-new.
- **Runtime:** `flox activate` once from anywhere in the repo supplies `python3` (pinned `python311`) and `claude`; suites run as plain `python3 …`.

## 8. Open questions / resume point

When we pick this back up, the brainstorming skill's next step is **write the finalized spec → self-review → user review → invoke `writing-plans`**. Two questions were still open:

1. **Placement:** `evals/drift/` as its own suite (recommended), or fold into `evals/flox/` next to `skill_toml_lint`?
2. **Seed registry size:** start with ~8–10 hand-picked load-bearing claims (recommended) and grow, or aim for fuller CLI-claim coverage from day one?

Answer those two, then this doc becomes the spec (drop the "ON HOLD" banner, resolve the OPEN markers) and we hand off to `writing-plans`.
