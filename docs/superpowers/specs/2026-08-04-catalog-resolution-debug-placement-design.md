# Catalog resolution debug: port to flox-skills

**Ticket:** [AI-504](https://linear.app/floxdotdev/issue/AI-504/login-issue-for-ai-pod-to-add-catalog-debug-skill-to-floxskills-repo) (High, 2pts)
**Date:** 2026-08-04
**Status:** design approved, pending implementation plan

## The ask

> Determine whether it should live as a reference in Flox-Skills or as its own
> separate skill to be triggered and invoked.

**Answer: its own skill.** Reasoning in §3.

AI-504 has no parent issue and no project in Linear. Governing precedent is
AI-94 ("ship a single skill with sub-documents… avoid bloating the list of
flox-XXX skills") and AI-488's culling pass.

## 1. What the skill is

`flox-internal-skills/skills/catalog-resolution-debug/SKILL.md`, 277 lines. A
runbook that teaches why the Flox resolver picks the build it picks:

- **Base page** (the nixpkgs rev, the `page` number) vs **build page**
  (`rev_count`/`rev`/`rev_date`, the source rev).
- The resolver picks the highest base page where *every* package in the
  `pkg-group` is satisfiable. Two user-facing failures follow: "my new publish
  isn't picked up" and "adding one package broke my environment"
  (`constraints_too_tight`).
- A diagnostic flow driven by `POST /catalog/api/v1/catalog/resolve?candidate_pages=N`.

The endpoints are public and documented (`api.flox.dev/catalog/docs`, server
`1.0.0-446c496`). Verified against the live OpenAPI spec: the endpoint, the
`candidate_pages` param, the response schemas, and every message type it names
all check out. **The concepts are right; the reproduction is not.**

Nothing in the shipped `flox` or `floxify` skills covers base page vs build
page or what to do when resolution fails, so the content is additive.

### Why it needs fixing before it ships

`d82cbc3` (2026-04-06) has a **single parent** — not a merge commit. Every other
skill in that repo arrived via a PR (#1, #4, #5, #6, #8, #9, #12, #13); this one
was pushed **directly to main**, no PR, no review, no ticket, trailer
`Co-Authored-By: Claude Opus 4.6`. Its sibling `catalog-db-inspect` (AI-123) had
a scoped ticket, explicit non-goals, six acceptance criteria including TAO
review, and a PR. This one had none of that, which is a sufficient explanation
for the defects below.

## 2. Scope

### Fix — produces wrong answers today

1. **`systems` comes from the manifest, not `uname`.** The skill derives systems
   from `uname -m`/`uname -s`. Resolution uses `[options] systems` plus
   per-package `.systems`. Two message types the skill teaches —
   `attr_path_not_found.systems_not_on_same_page` and
   `.not_found_for_all_systems` — are by definition multi-system failures, so a
   single-system repro reports green on the exact failure being debugged.

2. **Carry the `allow_*` descriptor fields.** `PackageDescriptor` accepts
   `allow_unfree`, `allow_broken`, `allow_insecure`, `allowed_licenses`,
   `allow_pre_releases`, `allow_missing_builds`. `[options] allow.unfree` and
   `allow.broken` are live manifest keys (verified accepted by flox 1.14.0).
   Three taught message types (`unfree`, `insecure`, `broken`) are exactly what
   these gate. Same failure mode as #1: a clean repro of a dirty install.

### Fix — cheap

3. **Complete the message list** — add `attr_path_not_found.not_in_catalog`,
   `unacceptable_licenses`, `change_in_version_format`, `resolution_logic`,
   `general`; mention `MessageLevel` (`trace`/`info`/`warning`/`error`).
   Transcribed from the enum already pulled from the live spec.

4. **Token hygiene** — one line: keep `flox auth token` output in an env var,
   never echo it, never let it into the printed table. The flox eval suite
   carries a `no_hardcoded_secret` check (709af77).

The concept section survives intact. It is the part that is right.

## 3. Placement: standalone skill

`flox-plugin/skills/catalog-resolution-debug/SKILL.md`, own frontmatter carrying
the existing trigger list. The `flox` skill is untouched.

**The reference option is the more expensive one, not the cheaper one.** It
requires widening the `flox` frontmatter with debug vocabulary — that
description contains no troubleshooting words today, so without widening it the
reference would rarely be reached. But `flox/SKILL.md` is under eval gate, so
perturbing it makes "prove you didn't regress existing triggering" part of the
job, and it re-inflates a file AI-488 just spent a cycle culling.

The standalone touches nothing with existing eval coverage. `floxify` is already
the precedent for one skill per job, and this is a third distinct job: reactive
fault diagnosis, not environment authoring. Its trigger vocabulary ("package not
resolving", "wrong version installed", "old build", "constraints too tight")
overlaps neither shipped skill.

**No packaging work.** Verified: `[build.skills-flox]` does
`cp -R flox-plugin/.` and `.flox/nix/flox-agent-layout.sh` globs `"$skillsrc"/*/`,
so a new skill directory is picked up automatically for Claude, Codex, Pi, and
OpenCode. Only the build's `description` string (which names "flox, floxify")
needs a touch.

## 4. Eval

`evals/README.md` requires an eval with every skill change, written RED first.
Not optional, and the minimum satisfies it.

**2–3 prompts** added to `evals/flox/tasks/screening.jsonl` with
`area: "resolution"` — that file's header states it is the only screening
registry the harness ships (AI-509 Ticket 3), with subsets selected via
`--area`. Cover the two canonical scenarios, and one prompt exercising the
multi-system fix so defect #1 is measured rather than asserted.

Screening-only; not promoted to the gated `tasks.jsonl`. Keeps per-PR eval cost
flat on a gate `run.py` notes is "defunded until cost is visible."

Run: `screen.py --area resolution --model claude-haiku-4-5-20251001 --reps 5`.
The committed baselines all use `reps=5`; the flag defaults to 1. Haiku costs
$0.425 per candidate at reps=5 across both arms, so this is a few dollars.

## 5. Landing

- New skill directory.
- `flox-plugin/skills/README.md` **and** the top-level `README.md` together —
  the skills README's maintenance note requires both when the inventory changes.
- Build `description` string.

## 6. Out of scope

Follow-ups if the skill sees use and these turn out to matter:

- **Worked example** — a captured resolve response and the candidate table it
  produces. Needs a live capture; real value, not blocking.
- **CLI-first ladder** — teach "read what `flox install` printed, then
  `flox show`, then the API." The skill currently opens with `curl`. Genuine
  improvement, judgment-call polish.
- **A/B measuring reference vs standalone.** Considered and dropped: it costs
  days to prove a call the ticket asks us to make, and at 8 prompts it could not
  have reached significance anyway (McNemar needs ~6 of 8 discordant pairs).
- **The `flox-internal-skills` copy.** Separate ticket with a hard sequencing
  dependency — flox-skills must land publicly first, and only then can the
  internal repo point at it. Bundling would couple two repos' merge order to one
  ticket.

## 7. Acceptance

- [ ] Defects 1–4 fixed, verified against the live OpenAPI spec.
- [ ] Skill lands at `flox-plugin/skills/catalog-resolution-debug/SKILL.md`;
      `flox/SKILL.md` unchanged.
- [ ] 2–3 screening prompts in `screening.jsonl` with `area: "resolution"`,
      written RED first and observed failing for the stated reason.
- [ ] Both READMEs and the build description updated.
- [ ] Follow-up issue filed for the `flox-internal-skills` copy (§6).
