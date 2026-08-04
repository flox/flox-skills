# Catalog resolution debug: correction and placement

**Ticket:** [AI-504](https://linear.app/floxdotdev/issue/AI-504/login-issue-for-ai-pod-to-add-catalog-debug-skill-to-floxskills-repo)
**Date:** 2026-08-04
**Status:** design approved, pending implementation plan

## 1. What AI-504 asks

> Evaluate how to add the catalog debug skill to the FloxSkills repo for the AI
> team. Determine whether it should live as a reference in Flox-Skills or as its
> own separate skill to be triggered and invoked.

AI-504 has **no parent issue and no project** in Linear. It is a loose High/2pt
issue in the AI team's current cycle. Two things act as its governing context:

- **Project precedent** — *Flox's Skills: Consolidate + Eval-Gate + Retire MCP*.
  AI-94: "we should be shipping a **single skill** with sub-documents and
  references… avoids bloating the list of flox-XXX skills." AI-488: an active
  culling pass because the skills had grown large.
- **Prior art** — AI-123 authored a sibling `catalog-db-inspect` skill in
  `flox/forge`; AI-127 (move it out + add pointers) was **cancelled** as "not
  load-bearing." That precedent does not transfer here: `catalog-db-inspect`
  needed a driver-provisioned snapshot connection string to a cloned Aurora
  cluster, so it had no audience outside the pod. This skill needs
  `flox auth token`, which every Flox user already has.

## 2. The artifact today

`flox-internal-skills/skills/catalog-resolution-debug/SKILL.md` — 277 lines,
one commit (`d82cbc3`, 2026-04-06, Bill LeVine, batched with `clipboard` and
`linear-migration`), untouched since. No README, no eval, no tests.

It is a **runbook, not a wrapper**. It teaches the resolver's model and then
drives the catalog API by hand:

1. Interview the user (env path, which `pkg-group`, which packages to add).
2. Parse `manifest.toml` into `PackageDescriptor`s, skipping `flake` entries.
3. **The mental model** — the payload. Two axes: *base page* (the nixpkgs rev,
   the `page` number) and *build page* (`rev_count`/`rev`/`rev_date`, the source
   rev). The resolver picks the highest base page where every package in the
   group is satisfiable. Two canonical failure stories follow: "my new publish
   isn't picked up" and "adding one package broke my environment"
   (`constraints_too_tight`).
4. `POST /catalog/api/v1/catalog/resolve?candidate_pages=10` with a bearer
   token, build a candidate table sorted by base page descending, classify the
   per-page `messages`, `GET /catalog/packages/{attr_path}` for one package's
   build history, bisect by resolving subsets.
5. Report: what resolved, candidate table, diagnosis, action items.

### 2.1 Verified against the live API

The endpoints are public and documented: Swagger UI at
`api.flox.dev/catalog/docs`, spec at `/catalog/api/v1/openapi.json`
(Floxhub Catalog Server, `1.0.0-446c496`). Checked against the spec:

- `candidate_pages` is a documented query param on `POST /resolve` (default 0).
- `HTTPBearer` security scheme.
- `ResolvedPackageGroup{name, page, candidate_pages, messages}`,
  `CatalogPage{page, url, packages, messages, complete}`, and `rev`/`rev_count`/
  `rev_date` on the resolved descriptor all exist as described.
- Every message type the skill lists is in the live `MessageType` enum.

**The concepts are sound and the endpoints are current.** The defects are in
reproduction fidelity.

### 2.2 Defects

| # | Defect | Consequence |
|---|---|---|
| 1 | `systems` derived from `uname -m`/`uname -s` | Resolution uses the manifest's `[options] systems` plus per-package `.systems`. Two message types the skill teaches — `attr_path_not_found.systems_not_on_same_page` and `.not_found_for_all_systems` — are by definition multi-system failures. A single-system repro reports green on the very failure being debugged. |
| 2 | `allow_*` descriptor fields dropped | `PackageDescriptor` accepts `allow_unfree`, `allow_broken`, `allow_insecure`, `allowed_licenses`, `allow_pre_releases`, `allow_missing_builds`. `[options] allow.unfree` / `allow.broken` are live manifest keys (verified accepted by flox 1.14.0). Three taught message types (`unfree`, `insecure`, `broken`) are exactly what these gate. Same failure mode as #1: a clean repro of a dirty install. |
| 3 | Incomplete message taxonomy | Missing `attr_path_not_found.not_in_catalog`, `unacceptable_licenses`, `change_in_version_format`, `resolution_logic`, `general`. No mention of `MessageLevel` (`trace`/`info`/`warning`/`error`), which is how a fatal is told from a footnote, nor of `CatalogPage.complete`. |
| 4 | No CLI-first ladder | Opens with `curl`. Never says "read what `flox install` already printed, check `flox show`, *then* reach for the API." |
| 5 | No worked example | No sample response, no filled-in candidate table. The agent must build a table from a shape it has never seen. AI-123's sibling skill carried "produces a correct answer against a live target" as an acceptance criterion. |
| 6 | Token hygiene | `TOKEN=$(flox auth token)` with no instruction against echoing it into the transcript or the table the skill is told to print. This repo recently ported a `no_hardcoded_secret` check into the flox suite (709af77). |

### 2.3 Overlap with shipped skills

None. `floxify` teaches pkg-group economy ("every distinct `pkg-group` is a
distinct catalog page") and the `flox` skill documents `pkg-group` syntax, but
**nothing in either explains base page vs build page, or what to do when
resolution fails.** The content is genuinely additive.

## 3. Decisions

| Decision | Choice |
|---|---|
| Placement | **Measure, then decide.** Build both layouts, run an A/B, ship the winner. |
| Decision rule | Content-delivery rate; **ties break toward the reference** (cheaper, AI-94-aligned). |
| Model | **Haiku 4.5.** Trigger-strength gaps show up on weak models (AI-436: the consolidated skill is underused by Haiku even when the facts are present). Opus risks both layouts passing and yielding no signal. |
| Content scope | **Folded into AI-504.** Shipping a known-broken repro publicly is worse than a slightly larger ticket. |
| Ordering | Corrected body written **once**, then wrapped two ways. The prose is identical; only packaging and trigger surface vary. |
| Eval gating | **Screening-only.** The prompts satisfy the eval policy as the shipped eval and the A/B report is the evidence; the gated suite's per-PR cost stays flat. |
| Internal copy | **Out of scope.** See §8. |

## 4. The corrected body

Placement-independent. Six changes; the concept section (base page vs build
page, the one-page-per-group rule, the two failure scenarios) survives intact.

**Fidelity** (these cause wrong answers today):

1. **`systems` comes from the manifest, not `uname`.** Use `[options] systems`
   plus any per-package `.systems`. State explicitly that a single-system repro
   masks `systems_not_on_same_page` and `not_found_for_all_systems`.
2. **Carry the `allow_*` fields.** Map `[options] allow.unfree` /
   `allow.broken` / `allow.licenses` onto the descriptor's `allow_unfree`,
   `allow_broken`, `allow_insecure`, `allowed_licenses`, `allow_pre_releases`,
   `allow_missing_builds`.

**Completeness:**

3. **Full message taxonomy** — add the five missing types, add `MessageLevel`,
   add `CatalogPage.complete`.
4. **CLI-first ladder** — read what `flox install` printed, then `flox show`,
   then the resolve API.
5. **Worked example** — one real captured resolve response and the candidate
   table it produces. Requires running a read-only resolve against the live API
   to capture the shape. Record the catalog server version alongside it.
6. **Token hygiene** — keep `flox auth token` output in an env var, never echo
   it, never let it into the printed table.

## 5. The two candidate layouts

**Layout A — reference under `flox`**

- `flox-plugin/skills/flox/references/resolution-debug.md`
- A routing line in `flox/SKILL.md`
- **The `flox` frontmatter description widened with debug vocabulary**
  ("package not resolving", "wrong version installed", "old build", …)

The frontmatter widening is not optional. The `flox` description today contains
no troubleshooting vocabulary at all, so testing A without it would rig the
experiment against A. It is also A's honest cost: it re-inflates a description
AI-488 spent a cycle culling.

**Layout B — standalone skill**

- `flox-plugin/skills/catalog-resolution-debug/SKILL.md`, own frontmatter
  carrying the existing trigger list
- `flox` skill untouched

Both wrap byte-identical prose.

### 5.1 Packaging requires no build changes

Verified: `.flox/env/manifest.toml`'s `[build.skills-flox]` does
`cp -R flox-plugin/.`, and `.flox/nix/flox-agent-layout.sh` globs
`"$skillsrc"/*/`. A new skill directory is picked up automatically for Claude,
Codex, Pi, and OpenCode. Only the build's `description` string (which names
"flox, floxify") needs a touch, and only if Layout B wins.

## 6. The measurement

### 6.1 Prompts

Eight, added to **`tasks/screening.jsonl`** with `area: "resolution"`. Not a new
file: that registry's header comment states it is "THE registry of active
screening candidates and the only one this harness ships (AI-509 Ticket 3)" —
the historical per-batch files were deliberately removed, and subsets now come
from stable per-entry metadata (`--area`, `--regression`, `--only`). Our subset
is therefore `--area resolution`.

Composition chosen so each fidelity fix has a prompt that can catch it:

| # | Shape | Tests |
|---|---|---|
| 1–2 | The two canonical scenarios | Core concept delivery |
| 3–4 | Implicit-trigger variants that never say "flox" | Trigger strength |
| 5 | Multi-system environment failing on one system | Fix #1 |
| 6 | Unfree/broken package in the group | Fix #2 |
| 7 | "What does `constraints_too_tight` mean?" | Direct concept question, floor case |
| 8 | A failure the CLI output already explains | Fix #4 — does it reach for `curl` prematurely? |

### 6.2 Harness

No harness code required. `screen.py` already takes `--plugin-dir`
("override the skills-arm plugin dir (e.g. a fixed-skill worktree)") and
`--model`, and reads `must_match`/`must_not_match` per candidate from the JSONL,
so **no `CHECKS` registry changes are needed**.

```
screen.py --area resolution --model claude-haiku-4-5-20251001 --plugin-dir <layoutA>
screen.py --area resolution --model claude-haiku-4-5-20251001 --plugin-dir <layoutB>
```

Each invocation runs baseline + skills, giving 4 arms over 8 prompts = 32 agent
calls.

### 6.3 The duplicated baseline is the control

The baseline arm runs twice. That is not waste: two independent baseline arms
over identical prompts are a **variance estimate**. If they disagree on a
prompt, that prompt's noise floor is at least 1, and any layout gap of the same
size is unreadable.

### 6.4 Decision rule, made concrete

The reference (Layout A) wins unless the standalone leads by **more than the
observed baseline disagreement** *and* by **at least 2 of 8**. Anything smaller
is a tie, and A ships.

### 6.5 Layout A's cost is measured, not assumed

Widening the `flox` description changes the skill that fires on everything else.
Layout A's arm therefore also runs `--area triggering`, and the result is
diffed against the **committed `baselines/screen-haiku.json`** — which was
recorded on `claude-haiku-4-5-20251001` and already carries per-candidate
`classification` for 13 of the 14 triggering candidates. Diffing against the
committed baseline rather than re-running one keeps this check to a single
skills arm (13 calls) instead of a full paired run.

If A wins on resolution but drags existing triggering, that is a real loss the
resolution-only numbers would hide. Note the coverage gap honestly: one
triggering candidate is absent from the committed Haiku baseline and cannot be
diffed without re-running it.

### 6.6 Cost

45 agent calls total: 32 for the paired A/B, plus 13 for Layout A's triggering
regression check. The `$1.27`/call and `~$40`/run figures in `run.py` are for
the default model on full tasks; on Haiku this should land well below that.
Actuals get reported — the harness already captures `total_cost_usd` per call,
and `screen.py`'s summary carries `total_cost_usd` directly.

### 6.7 Fallback

If both layouts classify as `skill-gap` on Haiku, the floor is too low to
discriminate rather than packaging being irrelevant. **Re-run once on Sonnet**
to find the tier where the layouts separate. This is pre-registered so the
fallback is not invented after seeing the numbers.

## 7. Landing

- Winner ships; loser layout is deleted.
- `flox-plugin/skills/README.md` **and** the top-level `README.md` updated
  together (the skills README's maintenance note requires both).
- Build `description` string touched only if Layout B wins.
- **The A/B result is committed as a report** under `evals/flox/reports/`,
  alongside `SCREENING-REPORT.md`. AI-504 asks to "evaluate… determine whether";
  the report is the deliverable that closes the ticket, independent of which
  layout won.

## 8. Out of scope

**The `flox-internal-skills` copy stays untouched by this work.** Retiring or
repointing it is a separate task with a hard sequencing dependency: flox-skills
must land publicly first, and only then can the internal repo point at it.
Bundling the two into AI-504 would couple two repos' merge order to one ticket.

File as a follow-up, sequenced after AI-504 lands: remove
`skills/catalog-resolution-debug/` from `flox-internal-skills` and add a README
pointer to the public plugin.

## 9. Risks

- **No placement signal.** Handled by §6.7.
- **Worked-example drift.** Captured against catalog server `1.0.0-446c496`;
  the version is recorded in the example so a future reader knows what it was
  true of.
- **Prompt 8 overfits.** "Does it reach for `curl` prematurely" is a negative
  check, and negative checks are where `must_not_match` regexes overfit. If it
  proves brittle, drop it to 7 prompts rather than tuning the regex until it
  says what we want.

## 10. Acceptance

- [ ] Corrected body fixes all six defects in §2.2, verified against the live
      OpenAPI spec.
- [ ] Both layouts built, wrapping identical prose.
- [ ] Eight screening prompts land in `evals/flox/tasks/screening.jsonl` with
      `area: "resolution"`.
- [ ] A/B run on Haiku 4.5; Layout A additionally diffed against the committed
      `baselines/screen-haiku.json` for triggering regression.
- [ ] Decision rule from §6.4 applied to the numbers as recorded, not
      renegotiated after the fact.
- [ ] Winner ships; both READMEs updated; loser deleted.
- [ ] A/B report committed under `evals/flox/reports/`.
- [ ] Follow-up issue filed for the `flox-internal-skills` copy (§8).
