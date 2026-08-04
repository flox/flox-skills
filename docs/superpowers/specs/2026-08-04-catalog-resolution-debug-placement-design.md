# Catalog resolution debug: port to flox-skills

**Ticket:** [AI-504](https://linear.app/floxdotdev/issue/AI-504/login-issue-for-ai-pod-to-add-catalog-debug-skill-to-floxskills-repo) (High, 2pts)
**Date:** 2026-08-04
**Status:** design approved, pending implementation plan

## The ask

> Determine whether it should live as a reference in Flox-Skills or as its own
> separate skill to be triggered and invoked.

**Its own skill**, at `flox-plugin/skills/catalog-resolution-debug/SKILL.md`.
`flox/SKILL.md` and `floxify/` are untouched. Four defects are fixed on the way
in (§3).

The skill being ported is `flox-internal-skills/skills/catalog-resolution-debug/SKILL.md`
(277 lines): a runbook explaining why the resolver picks the build it picks —
base page vs build page, the rule that every package in a `pkg-group` must land
on one base page, and a diagnostic flow driven by the catalog resolve API.

## 1. Why standalone

### It is a procedure, not knowledge

This repo's two skills split by what kind of thing they are:

| | `floxify` | `flox/references/` |
|---|---|---|
| Structure | `## Phase 0` → `## Phase 4` | Topical: "Running Services", "Core Commands" |
| Metadata | `argument-hint: "[github-url \| local-path]"` | none |
| Use | You **run** it | You **consult** it |

`catalog-resolution-debug` is the first kind: *Gather Context from the User →
Parse the Manifest → Diagnostic Flow (Step 1–5) → Presenting Results*, invoked
against an environment path. Structurally it is `floxify`, not `services.md`.
AI-504 frames the question the same way — "a reference… or its own separate
skill **to be triggered and invoked**."

### The reference option is the more expensive one

A reference is only opened if the `flox` skill fires first, and that skill's
description covers creating and managing environments with no troubleshooting
vocabulary at all. Making it reachable means widening that description — but
`flox/SKILL.md` is eval-gated (`evals.yml` path-filters on
`flox-plugin/skills/flox/**`), so "prove you didn't regress existing triggering"
becomes part of the job, and it re-inflates a file AI-488 just spent a cycle
culling. The standalone touches nothing with existing coverage.

### Caveat

One precedent is thin, and the line is not perfectly crisp — `publish.md` and
`builds.md` under `flox/references/` do contain step sequences. If the team's
intent is "everything Flox-related lives inside the `flox` skill," then
`floxify` is the anomaly rather than the precedent and this should be bundled.
Worth confirming with Bill, who wrote both AI-94 and this ticket.

It is also cheaply reversible: standalone → reference later is a file move plus
a routing line. The §3 fixes are the durable value and are identical either way.

## 2. Why it belongs in this repo at all

The two repos split by audience, not by value tier. `flox-internal-skills` holds
tools for *working at Flox* — CI builder health, dependabot triage, AWS cost
spikes, Linear migration, sprint demos, and four marketing/design skills.
`flox-skills` is a shipped product artifact teaching people to *use Flox*.

`catalog-resolution-debug` is the only skill in the internal repo about the
product, and the failures it explains ("my publish isn't picked up", "adding a
package broke my environment") are user complaints — AI-409 is an inbound report
of exactly this scenario. Moving it corrects a filing error rather than
promoting an internal tool.

The real cost is that publishing makes it a support surface we commit to keeping
correct as the catalog evolves. Offset: the API is already public and
documented, and the skill lands under eval coverage it has none of today.

## 3. Fixes made on the way in

Checked against the live OpenAPI spec (`api.flox.dev/catalog/api/v1/openapi.json`,
server `1.0.0-446c496`): the endpoint, the `candidate_pages` param, the response
schemas, and every message type the skill names are all current. **The concepts
are right; the reproduction is not.**

That the skill needs fixing at all is explained by its history: commit
`d82cbc3` has a single parent — not a merge commit. Every other skill in that
repo arrived via a PR; this one was pushed directly to main with no PR, no
review, and no ticket. Its sibling `catalog-db-inspect` (AI-123) had a scoped
ticket, explicit non-goals, and six acceptance criteria including TAO review.

**Wrong answers today:**

1. **`systems` must come from the manifest, not `uname`.** The skill derives
   systems from `uname -m`/`uname -s`; resolution uses `[options] systems` plus
   per-package `.systems`. Two message types it teaches —
   `attr_path_not_found.systems_not_on_same_page` and
   `.not_found_for_all_systems` — are by definition multi-system failures, so a
   single-system repro reports green on the exact failure being debugged.

2. **Carry the `allow_*` descriptor fields.** `PackageDescriptor` accepts
   `allow_unfree`, `allow_broken`, `allow_insecure`, `allowed_licenses`,
   `allow_pre_releases`, `allow_missing_builds`. `[options] allow.unfree` and
   `allow.broken` are live manifest keys (verified accepted by flox 1.14.0), and
   three taught message types (`unfree`, `insecure`, `broken`) are exactly what
   they gate. Same failure mode as #1: a clean repro of a dirty install.

**Cheap:**

3. **Complete the message list** — add `attr_path_not_found.not_in_catalog`,
   `unacceptable_licenses`, `change_in_version_format`, `resolution_logic`,
   `general`; mention `MessageLevel`. Transcribed from the enum already pulled.

4. **Token hygiene** — one line: keep `flox auth token` output in an env var,
   never echo it, never let it into the printed table. The flox eval suite
   carries a `no_hardcoded_secret` check (709af77).

The concept section survives intact. It is the part that is right.

## 4. Eval

`evals/README.md` requires an eval with every skill change, written RED first.
Not optional; the minimum satisfies it.

**2–3 prompts** in `evals/flox/tasks/screening.jsonl` with `area: "resolution"` —
that file's header states it is the only screening registry the harness ships
(AI-509 Ticket 3), with subsets selected via `--area`. Cover the two canonical
scenarios plus one exercising the multi-system fix, so defect #1 is measured
rather than asserted.

Screening-only, not promoted to the gated `tasks.jsonl` — keeps per-PR eval cost
flat on a gate `run.py` notes is "defunded until cost is visible."

Run with `--reps 5`: every committed baseline uses it, and the flag defaults
to 1.

## 5. Landing

- New skill directory. No packaging work: `[build.skills-flox]` does
  `cp -R flox-plugin/.` and `flox-agent-layout.sh` globs `"$skillsrc"/*/`, so a
  new skill directory is picked up for Claude, Codex, Pi, and OpenCode
  automatically.
- `flox-plugin/skills/README.md` **and** the top-level `README.md` together —
  the skills README's maintenance note requires both when the inventory changes.
- The build's `description` string, which names "flox, floxify".

## 6. Corrections made during design

Recorded so they are not relitigated:

- **The catalog API is public and documented**, not an internal endpoint. An
  early draft treated publishing it as exposing internal surface area and
  weighed a CLI-only rewrite on that basis. `api.flox.dev/catalog/docs` is a
  public Swagger UI; the concern was void and the rewrite was dropped.
- **The reference is not the cheaper option.** Initially argued as the low-cost
  choice; it is the opposite, because it perturbs an eval-gated file (§1).
- **Procedure-vs-knowledge replaced the eval-gate argument as the primary
  reason.** The eval-gate point is about implementation cost and is incidental
  to what CI happens to path-filter; the better argument is what the thing is.

## 7. Found and deliberately left out

- **An A/B measuring reference vs standalone.** Designed in full, then dropped:
  it cost days of elapsed work to prove a call the ticket asks us to make, and
  at 8 prompts could not have reached significance anyway (McNemar needs ~6 of 8
  discordant pairs). The 2–3 eval prompts we owe regardless will show whether
  the standalone triggers.
- **Worked example** — a captured resolve response and the candidate table it
  produces. Real value; needs a live capture. Follow-up.
- **CLI-first ladder** — teach "read what `flox install` printed, then
  `flox show`, then the API." The skill opens with `curl` today. Genuine
  improvement, judgment-call polish. Follow-up.
- **The `flox-internal-skills` copy.** Separate ticket with a hard sequencing
  dependency: flox-skills must land publicly first, and only then can the
  internal repo point at it. Bundling would couple two repos' merge order to one
  ticket.
- **AI-127 as precedent.** The cancelled "move `catalog-db-inspect` out" ticket
  looks like a direct precedent for leaving this internal, but does not
  transfer: that skill needed a driver-provisioned snapshot connection string to
  a cloned Aurora cluster, so it had no audience outside the pod. This one needs
  `flox auth token`, which every Flox user has.

## 8. Acceptance

- [ ] Defects 1–4 fixed, verified against the live OpenAPI spec.
- [ ] Skill lands at `flox-plugin/skills/catalog-resolution-debug/SKILL.md`;
      `flox/SKILL.md` unchanged.
- [ ] 2–3 screening prompts in `screening.jsonl` with `area: "resolution"`,
      written RED first and observed failing for the stated reason.
- [ ] Both READMEs and the build description updated.
- [ ] Follow-up issue filed for the `flox-internal-skills` copy (§7).
