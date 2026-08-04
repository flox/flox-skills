# Catalog Resolution Debug Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the `catalog-resolution-debug` skill from `flox-internal-skills` into this repo as a standalone skill, fixing four defects on the way in, with eval coverage and user-facing documentation.

**Architecture:** A new third top-level skill at `flox-plugin/skills/catalog-resolution-debug/SKILL.md`, alongside `flox` and `floxify`. It is a procedure (gather inputs → ordered diagnostic steps → report), which is why it gets its own trigger surface rather than becoming a file under `flox/references/`. No packaging changes: the `skills-flox` build copies `flox-plugin/` wholesale and `flox-agent-layout.sh` globs skill directories.

**Tech Stack:** Markdown skill definition; Python eval harness (`evals/flox/screen.py`); TOML manifests; GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-04-catalog-resolution-debug-placement-design.md`

## Global Constraints

- **`flox-plugin/skills/flox/SKILL.md`, `flox/references/*`, and `flox-plugin/skills/floxify/` must remain unchanged.** Touching them pulls the existing eval gate into scope (`evals.yml` path-filters on `flox-plugin/skills/flox/**`).
- **Eval prompts are screening-only.** They go in `evals/flox/tasks/screening.jsonl`, never `tasks/tasks.jsonl`, to keep the per-PR gate cost flat.
- **`evals/flox/tasks/screening.jsonl` is the only screening registry.** Do not create a new `.jsonl` file; subsets are selected with `--area` / `--only`.
- **Every eval run uses `--reps 5`.** The flag defaults to 1; every committed baseline uses 5.
- **Eval model is `claude-haiku-4-5-20251001`.** `screen.py` defaults to `claude-opus-4-8`, so the flag is never optional.
- **Anything importing the harness or `tomllib` must run under `flox activate --`.** System `python3` here is 3.9.6 and `run.py` imports `tomllib` (3.11+); the repo's Flox environment provides 3.11.15. Verified pattern: `flox activate -- python3 screen.py …` and `flox activate -- python3 - <<'EOF' … EOF`. Plain `python3` is fine only for stdlib-only scripts using `json`/`pathlib`/`collections`.
- **RED first.** Every eval is written and observed failing *for the stated reason* before the corresponding fix is written.
- **The skill's concept section is correct and must survive intact** — base page vs build page, the one-page-per-group rule, and the two failure scenarios are not being rewritten.
- Run all eval commands from `evals/flox/`.

## File Structure

| File | Responsibility |
|---|---|
| `flox-plugin/skills/catalog-resolution-debug/SKILL.md` | **Create.** The skill: frontmatter trigger vocabulary, concept model, diagnostic procedure. |
| `evals/flox/tasks/screening.jsonl` | **Modify.** Append 4 records with `area: "resolution"`. |
| `README.md` | **Modify.** User-facing: skill count, inventory bullet, "Using it" example. |
| `flox-plugin/skills/README.md` | **Modify.** Skill library inventory. |
| `.flox/env/manifest.toml` | **Modify.** One line: the `[build.skills-flox]` description string. |

---

### Task 1: Port the skill verbatim

Deliberately unmodified. This is the RED baseline that Task 2's evals must fail against — porting and fixing in one step would make it impossible to observe the failures the fixes claim to solve.

**Files:**
- Create: `flox-plugin/skills/catalog-resolution-debug/SKILL.md`

**Interfaces:**
- Consumes: nothing.
- Produces: the skill directory path `flox-plugin/skills/catalog-resolution-debug/`, used by every later task. Frontmatter key `name: catalog-resolution-debug` is the skill's invocation name.

- [ ] **Step 1: Copy the source file unchanged**

```bash
cd /Users/alantorres/Projects/flox-skills
mkdir -p flox-plugin/skills/catalog-resolution-debug
cp ~/Projects/flox-internal-skills/skills/catalog-resolution-debug/SKILL.md \
   flox-plugin/skills/catalog-resolution-debug/SKILL.md
```

- [ ] **Step 2: Verify it is byte-identical to the source**

```bash
diff ~/Projects/flox-internal-skills/skills/catalog-resolution-debug/SKILL.md \
     flox-plugin/skills/catalog-resolution-debug/SKILL.md && echo "IDENTICAL"
```

Expected: `IDENTICAL`, no diff output.

- [ ] **Step 3: Verify the frontmatter parses and carries a name + description**

```bash
python3 - <<'EOF'
import re, pathlib
p = pathlib.Path("flox-plugin/skills/catalog-resolution-debug/SKILL.md")
text = p.read_text()
m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
assert m, "no YAML frontmatter block"
fm = m.group(1)
assert re.search(r"^name:\s*catalog-resolution-debug\s*$", fm, re.M), "name missing/wrong"
assert re.search(r"^description:", fm, re.M), "description missing"
print("frontmatter OK")
EOF
```

Expected: `frontmatter OK`.

- [ ] **Step 4: List the TOML snippets the doc contains**

The doc carries an illustrative manifest fragment. `skill_toml_lint.py` defaults to the `flox` skill dir, so point it at the new one. `--list` needs no `flox` and no network.

```bash
cd evals/flox
flox activate -- python3 skill_toml_lint.py \
  --skill-dir ../../flox-plugin/skills/catalog-resolution-debug \
  --list
cd ../..
```

Expected: it prints the extracted block(s) — one `[install]` fragment. Record what it finds in the commit message.

Note: do **not** rewrite the doc's snippets to satisfy the structural tier. They are illustrative fragments, this skill is not wired into the `skill-toml-lint` CI job (which path-gates on the `flox` skill), and reshaping them is out of scope. If the lint reports problems, note them and move on.

- [ ] **Step 5: Verify the build picks the new skill up**

```bash
flox build skills-flox
find result*/share/flox -maxdepth 3 -name 'catalog-resolution-debug' | sort
```

Expected: the directory appears under the claude, codex, pi, and opencode trees — four paths. This confirms the `flox_agent_layout` glob picked it up with no manifest change.

- [ ] **Step 6: Commit**

```bash
git add flox-plugin/skills/catalog-resolution-debug/SKILL.md
git commit -m "feat(skills): port catalog-resolution-debug verbatim (AI-504)

Unmodified copy of the skill from flox-internal-skills, as the RED
baseline for the eval prompts that follow. The four defects it carries
are fixed in subsequent commits, each after an eval observes the
failure it claims to fix.

Build needs no changes: skills-flox copies flox-plugin wholesale and
flox-agent-layout.sh globs skill directories, so the new skill appears
in all four agent trees.

Refs: AI-504"
```

---

### Task 2: Write the eval prompts and observe them RED

**Files:**
- Modify: `evals/flox/tasks/screening.jsonl` (append 4 lines)

**Interfaces:**
- Consumes: the skill at `flox-plugin/skills/catalog-resolution-debug/SKILL.md` from Task 1.
- Produces: four candidate ids used by later tasks — `res-stale-publish`, `res-add-breaks-group`, `res-multi-system`, `res-unfree-group`. Later tasks re-run individual ones with `screen.py --only <id>`.

The spec called for "2–3 prompts"; this is 4. The extra one is `res-unfree-group`, without which defect #2 ships with no eval at all — which the `evals/README.md` policy forbids. One prompt per fidelity fix, plus the two canonical user scenarios.

- [ ] **Step 1: Append the four records**

Each must be a single line of JSON. Run this to append them safely:

```bash
cd /Users/alantorres/Projects/flox-skills/evals/flox
python3 - <<'PYEOF'
import json, pathlib

records = [
  {
    "id": "res-stale-publish",
    "area": "resolution",
    "tier": "stretch",
    "prompt": "I published a new version of my package to FloxHub about an hour ago and it succeeded, but when I run flox install in my project I still get the old build. What's going on and how do I confirm it?",
    "rubric": "Correct answer distinguishes the BASE page (the nixpkgs revision a build was evaluated against, the `page` number) from the BUILD page (rev_count/rev/rev_date, the source revision), and explains that a newer build evaluated against an older nixpkgs pin loses to an older build on a newer base page. Should point at the catalog resolve endpoint with candidate_pages to confirm. Penalize cache-clearing, `flox upgrade`, or 'wait for indexing' as the primary explanation.",
    "target": "base blames caching or indexing delay; misses the base-page/build-page distinction entirely",
    "must_match": ["(?i)base\\s+page", "(?i)rev_count|build\\s+page"],
    "must_not_match": []
  },
  {
    "id": "res-add-breaks-group",
    "area": "resolution",
    "tier": "stretch",
    "prompt": "My Flox environment has four packages that all install fine. When I add one more, the install fails saying the constraints are too tight. Why would adding one package break the four that already worked?",
    "rubric": "Correct answer explains that every package in a pkg-group must resolve on a SINGLE common base page, so a new package that only exists on base pages the others don't makes the whole group unsatisfiable. Should suggest isolating the outlier by resolving subsets, and moving it to its own pkg-group as one remedy. Penalize generic version-conflict advice that never mentions pages or pkg-groups.",
    "target": "base gives generic dependency-conflict advice; misses the single-common-base-page rule",
    "must_match": ["(?i)pkg-group|package\\s+group", "(?i)(same|one|common|single)\\s+base\\s+page"],
    "must_not_match": []
  },
  {
    "id": "res-multi-system",
    "area": "resolution",
    "tier": "stretch",
    "prompt": "A package resolves fine for me on my Mac, but the same Flox environment fails to lock for our Linux CI. Walk me through reproducing the resolver's decision so I can see exactly which system is the problem.",
    "rubric": "Correct answer takes the system list from the environment (the manifest's [options] systems, or a package's own .systems, or manifest.lock where each locked package records its system) and NOT from the local machine. Must reproduce against the full declared system set, because narrowing to one system resolves cleanly and hides the failure. Bonus for naming attr_path_not_found.systems_not_on_same_page or .not_found_for_all_systems. Penalize any instruction to detect the platform with uname.",
    "target": "reproduces against the local platform only, so the multi-system failure disappears and the answer reports success",
    "must_match": ["(?i)\\[options\\]|manifest\\.lock", "(?i)systems_not_on_same_page|not_found_for_all_systems|all\\s+(four\\s+)?(declared\\s+)?systems"],
    "must_not_match": ["(?i)uname"]
  },
  {
    "id": "res-unfree-group",
    "area": "resolution",
    "tier": "stretch",
    "prompt": "My Flox environment sets allow.unfree = true and installs a proprietary toolchain. Adding another package makes resolution fail. How do I reproduce what the resolver is doing so I can see which package is at fault?",
    "rubric": "Correct answer carries the manifest's [options] allow settings into the resolve request as the descriptor fields allow_unfree / allow_broken / allowed_licenses. Without them the reproduction is against different inputs than the real install and can succeed where the install failed. Penalize a reproduction that only sends install_id/attr_path/systems/version and never mentions the allow options.",
    "target": "reproduces without the allow_* fields, so the repro succeeds while the real install fails",
    "must_match": ["allow_unfree", "(?i)resolve"],
    "must_not_match": []
  },
]

path = pathlib.Path("tasks/screening.jsonl")
existing = {json.loads(l)["id"] for l in path.read_text().splitlines() if l.strip()}
with path.open("a") as fh:
    for r in records:
        assert r["id"] not in existing, f"duplicate id {r['id']}"
        fh.write(json.dumps(r) + "\n")
print(f"appended {len(records)} records")
PYEOF
```

Expected: `appended 4 records`.

- [ ] **Step 2: Verify the registry still parses and the ids are unique**

```bash
python3 - <<'EOF'
import json, collections, pathlib
lines = [l for l in pathlib.Path("tasks/screening.jsonl").read_text().splitlines() if l.strip()]
recs = [json.loads(l) for l in lines]
dupes = [i for i, n in collections.Counter(r["id"] for r in recs).items() if n > 1]
assert not dupes, f"duplicate ids: {dupes}"
res = [r for r in recs if r["area"] == "resolution"]
assert len(res) == 4, f"expected 4 resolution records, got {len(res)}"
for r in res:
    for k in ("id", "area", "tier", "prompt", "rubric", "target", "must_match", "must_not_match"):
        assert k in r, f"{r['id']} missing {k}"
print(f"{len(recs)} records OK, {len(res)} in area=resolution")
EOF
```

Expected: `50 records OK, 4 in area=resolution`.

- [ ] **Step 3: Confirm the harness selects exactly these four**

`screen.py` has no list-only flag, so call its selector directly:

```bash
flox activate -- python3 -c "
import sys; sys.path.insert(0,'.')
import json, screen
recs=[json.loads(l) for l in open('tasks/screening.jsonl') if l.strip()]
sel=screen.select(recs, areas=['resolution'])
print('selected:', [r['id'] for r in sel])
assert len(sel) == 4, f'expected 4, got {len(sel)}'
"
```

Expected: the four ids listed, no assertion error.

- [ ] **Step 4: Run the RED screen**

```bash
flox activate -- python3 screen.py --area resolution \
  --model claude-haiku-4-5-20251001 \
  --reps 5 \
  --out results/red-resolution.json
```

Expected: `res-multi-system` and `res-unfree-group` **FAIL** the skills arm. Specifically:
- `res-multi-system` fails `must_not_match: uname` — the ported skill instructs the agent to detect systems with `uname -m` + `uname -s`.
- `res-unfree-group` fails `must_match: allow_unfree` — the ported skill's descriptor list has no `allow_*` fields.

`res-stale-publish` and `res-add-breaks-group` should PASS: they test the concept section, which is already correct.

**If the two failures do not appear for those reasons, stop.** The eval is not measuring what it claims, and the fixes in Tasks 4 and 5 would be unverifiable.

> **Outcome (2026-08-04):** this gate tripped, and the run was worth more than
> the prediction. All four candidates failed, at $1.28. The skill triggers
> correctly — the answers carry "nixpkgs base page", "Query the Flox catalog
> API", "build a diagnostic table" — and then obeys its own opening instruction
> to ask the user three questions, so the measured answer is a questionnaire
> with no analysis in it. A fifth defect, masking the other two. Task 3 was
> added to fix it, with `results/red-resolution.json` as its RED evidence.

- [ ] **Step 5: Record the RED evidence**

```bash
python3 -c "
import json
d=json.load(open('results/red-resolution.json'))
for r in d['results']:
    print(r['id'], '| skills hard_pass:', r['skills'].get('hard_pass'), '| class:', r['classification'])
print('cost \$', d['summary'].get('total_cost_usd'))
"
```

Copy this output into the Step 6 commit message.

- [ ] **Step 6: Commit**

```bash
cd /Users/alantorres/Projects/flox-skills
git add evals/flox/tasks/screening.jsonl
git commit -m "test(evals): RED prompts for catalog-resolution-debug (AI-504)

Four screening candidates in area=resolution: the two canonical user
scenarios plus one per fidelity defect, so neither fix ships unmeasured.

Observed RED against the verbatim port, each failing for its stated
reason:
  - res-multi-system   fails must_not_match uname (the skill derives
                       systems from the local platform)
  - res-unfree-group   fails must_match allow_unfree (the descriptor
                       list drops the allow_* fields)
  - res-stale-publish, res-add-breaks-group pass -- they exercise the
    concept section, which is correct as ported.

<paste the Step 5 output here>

Screening-only; not promoted to the gated tasks.jsonl.

Refs: AI-504"
```

---

### Task 3: Fix defect 5 — read the environment, don't interrogate the user

**Added mid-execution.** Task 2's RED run showed all four candidates failing, but not for the predicted reasons. The skill triggers correctly, then obeys its own opening instruction — *"Before making any API calls, ask the user"* — and emits a three-question questionnaire instead of a diagnosis. There is no analysis for any hard check to match, so this defect masks the other two and must be fixed before they can be measured. `results/red-resolution.json` from Task 2 is this task's RED evidence.

It is also a real usability defect: an agent sitting in the user's repo should read `.flox/env/manifest.toml` rather than interrogate them about its contents.

**Files:**
- Modify: `flox-plugin/skills/catalog-resolution-debug/SKILL.md` (the "Gather Context from the User" section)

**Interfaces:**
- Consumes: candidate ids `res-stale-publish` and `res-add-breaks-group` from Task 2. These two exercise the concept section, which is already correct, so they should go green on this fix alone.
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Replace the whole "Gather Context from the User" section**

Find the section that begins with this heading and runs to just before `## Parse the Manifest`:

```
## Gather Context from the User

Before making any API calls, ask the user:
```

Replace the **entire section**, heading included, with:

```
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
```

- [ ] **Step 2: Verify the interrogation instruction is gone**

```bash
cd /Users/alantorres/Projects/flox-skills
! grep -n "ask the user" flox-plugin/skills/catalog-resolution-debug/SKILL.md && echo "no interrogation opener: OK"
grep -n "^## Establish Context" flox-plugin/skills/catalog-resolution-debug/SKILL.md
grep -c "Gather Context from the User" flox-plugin/skills/catalog-resolution-debug/SKILL.md
```

Expected: `no interrogation opener: OK`, the `## Establish Context` heading found, and a count of `0` for the old heading.

- [ ] **Step 3: Confirm the rest of the skill is untouched**

```bash
diff <(sed -n '/^## Parse the Manifest/,$p' ~/Projects/flox-internal-skills/skills/catalog-resolution-debug/SKILL.md) \
     <(sed -n '/^## Parse the Manifest/,$p' flox-plugin/skills/catalog-resolution-debug/SKILL.md) \
  && echo "everything from 'Parse the Manifest' onward is still verbatim: OK"
```

Expected: `OK`. This task changes exactly one section; the defects Tasks 4 and 5 fix must still be present and unfixed.

- [ ] **Step 4: Re-run the full resolution area**

Running all four (rather than only the two controls) costs the same order of money and tells us whether the remaining two now fail for their *predicted* reasons — which is the gate Task 2 could not reach.

```bash
cd evals/flox
flox activate -- python3 screen.py --area resolution \
  --model claude-haiku-4-5-20251001 \
  --reps 5 \
  --out results/red2-resolution.json

flox activate -- python3 -c "
import json
d=json.load(open('results/red2-resolution.json'))
for r in d['results']:
    print(r['id'], '| skills hard rate:', r['skills'].get('hard_pass_rate'), '| pass:', r['skills'].get('hard_pass'), '|', r['classification'])
print('cost \$', d['summary'].get('total_cost_usd'))
"
```

Expected:
- `res-stale-publish` — **PASSES** the skills arm.
- `res-add-breaks-group` — **PASSES** the skills arm.
- `res-multi-system` — still fails, and now demonstrably on `must_not_match: uname` (the skill still tells the agent to derive systems from `uname`).
- `res-unfree-group` — still fails, and now demonstrably on `must_match: allow_unfree`.

- [ ] **Step 5: Confirm the two remaining failures are for the predicted reasons**

Print the stored answer excerpts and check them by eye:

```bash
cd evals/flox
flox activate -- python3 -c "
import json, re
d=json.load(open('results/red2-resolution.json'))
for r in d['results']:
    if r['id'] in ('res-multi-system','res-unfree-group'):
        ex = r['skills'].get('answer_excerpt') or ''
        print('='*60); print(r['id'])
        print('  contains uname       :', bool(re.search(r'(?i)uname', ex)))
        print('  contains allow_unfree:', 'allow_unfree' in ex)
        print(ex[:900])
"
```

Expected: `res-multi-system`'s excerpt mentions `uname`, and `res-unfree-group`'s does not mention `allow_unfree`. If instead both now produce a real diagnosis and still fail on something unrelated, report **DONE_WITH_CONCERNS** with the excerpts — do not edit the prompts to force the expected failure.

- [ ] **Step 6: Commit**

```bash
cd /Users/alantorres/Projects/flox-skills
git add flox-plugin/skills/catalog-resolution-debug/SKILL.md
git commit -m "fix(skills): establish context by reading, not by interrogating

The skill opened with 'Before making any API calls, ask the user' and
three questions. Measured effect: the answer under evaluation is a
questionnaire rather than a diagnosis -- all four eval candidates
scored 0-1/5 on their hard checks with nothing to match against, and
one answer asked the user to run \`uname -m && uname -s\` by hand.

An agent sitting in the user's repo should read
.flox/env/manifest.toml, not interrogate them about its contents. It
now works out the environment, the pkg-group and the installed set
itself, asks only for what is genuinely absent, and states its
assumptions.

Found by the Task 2 RED run, which this fix turns green for the two
candidates exercising the concept section. Everything from 'Parse the
Manifest' onward is still the verbatim port; the systems and allow_*
defects remain, unfixed and now measurable.

Refs: AI-504"
```

---

### Task 4: Fix defect 1 — take `systems` from the manifest

**Files:**
- Modify: `flox-plugin/skills/catalog-resolution-debug/SKILL.md` (the "Parse the Manifest" section)

**Interfaces:**
- Consumes: candidate id `res-multi-system` from Task 2.
- Produces: nothing later tasks depend on.

Verified against flox 1.14.0: when `[options] systems` is absent the environment locks for **all four** systems (`aarch64-darwin`, `aarch64-linux`, `x86_64-darwin`, `x86_64-linux`), so the default case is precisely the case the ported skill gets wrong.

- [ ] **Step 1: Replace the `systems` bullet**

Find this exact text in `flox-plugin/skills/catalog-resolution-debug/SKILL.md`:

```
- `systems`: detect from user's platform (`uname -m`
  + `uname -s` -> e.g., `x86_64-linux`)
```

Replace it with:

```
- `systems`: the environment's declared systems, **not**
  the local platform. Read `[options] systems` from the
  manifest. If a package carries its own `.systems`, use
  that list for that descriptor instead. If `[options]
  systems` is absent the environment targets **all four**
  (`aarch64-darwin`, `aarch64-linux`, `x86_64-darwin`,
  `x86_64-linux`) — confirm against `manifest.lock`,
  where every locked package records its `system`.
```

- [ ] **Step 2: Add the warning after the descriptor list**

Find this exact text (the last bullet of the descriptor list):

```
- Skip packages with a `flake` attribute — those are
  not resolved through the catalog
```

Insert immediately after it (leaving a blank line between):

```
**Never narrow `systems` to your own machine.** Two of
the message types below —
`attr_path_not_found.systems_not_on_same_page` and
`attr_path_not_found.not_found_for_all_systems` — are
multi-system failures by definition. A single-system
reproduction resolves cleanly against the very failure
you were asked to debug, and you will report "works
fine" on a broken environment.
```

- [ ] **Step 3: Verify `uname` no longer appears anywhere in the skill**

```bash
cd /Users/alantorres/Projects/flox-skills
! grep -n -i "uname" flox-plugin/skills/catalog-resolution-debug/SKILL.md && echo "no uname: OK"
```

Expected: `no uname: OK`.

- [ ] **Step 4: Re-run just this candidate to verify GREEN**

```bash
cd evals/flox
flox activate -- python3 screen.py --only res-multi-system \
  --model claude-haiku-4-5-20251001 \
  --reps 5 \
  --out results/green-multi-system.json
```

Expected: the skills arm now passes. Confirm:

```bash
python3 -c "
import json
d=json.load(open('results/green-multi-system.json'))
r=d['results'][0]
print(r['id'], '| skills hard_pass:', r['skills'].get('hard_pass'), '| class:', r['classification'])
assert r['skills'].get('hard_pass'), 'STILL RED -- do not commit'
print('GREEN')
"
```

- [ ] **Step 5: Commit**

```bash
cd /Users/alantorres/Projects/flox-skills
git add flox-plugin/skills/catalog-resolution-debug/SKILL.md
git commit -m "fix(skills): take resolve systems from the manifest, not uname

The skill derived the descriptor's \`systems\` from \`uname -m\`/\`uname -s\`.
Resolution uses the environment's declared systems -- \`[options] systems\`,
per-package \`.systems\`, or manifest.lock. Verified against flox 1.14.0:
with \`[options] systems\` absent an environment locks for all four
systems, so the DEFAULT case was the broken one.

This matters because attr_path_not_found.systems_not_on_same_page and
.not_found_for_all_systems are multi-system failures by definition: a
single-system repro resolves cleanly and reports success on the exact
environment the user cannot lock.

res-multi-system: RED -> GREEN.

Refs: AI-504"
```

---

### Task 5: Fix defect 2 — carry the `allow_*` fields

**Files:**
- Modify: `flox-plugin/skills/catalog-resolution-debug/SKILL.md` (the "Parse the Manifest" section)

**Interfaces:**
- Consumes: candidate id `res-unfree-group` from Task 2.
- Produces: nothing later tasks depend on.

Verified against flox 1.14.0: `[options].allow` accepts exactly `unfree`, `broken`, and `licenses` — an `insecure` key is rejected with *"unknown field `insecure`, expected one of `unfree`, `broken`, `licenses`"*. The API descriptor additionally accepts `allow_insecure`, `allow_pre_releases`, and `allow_missing_builds`, which have **no manifest equivalent** and must therefore be left at their defaults when reproducing a manifest. Also verified live: setting `allow.licenses = ["MIT"]` blocks `hello` from resolving, and removing it lets it through — the mechanism is real.

- [ ] **Step 1: Add the options-mapping block**

Insert immediately after the warning block added in Task 4, Step 2 (leave a blank line between):

```
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
```

- [ ] **Step 2: Verify the fields are present**

```bash
cd /Users/alantorres/Projects/flox-skills
for f in allow_unfree allow_broken allowed_licenses; do
  grep -q "$f" flox-plugin/skills/catalog-resolution-debug/SKILL.md \
    && echo "$f: OK" || { echo "$f: MISSING"; exit 1; }
done
```

Expected: three `OK` lines.

- [ ] **Step 3: Re-run just this candidate to verify GREEN**

```bash
cd evals/flox
flox activate -- python3 screen.py --only res-unfree-group \
  --model claude-haiku-4-5-20251001 \
  --reps 5 \
  --out results/green-unfree-group.json
```

Confirm:

```bash
python3 -c "
import json
d=json.load(open('results/green-unfree-group.json'))
r=d['results'][0]
print(r['id'], '| skills hard_pass:', r['skills'].get('hard_pass'), '| class:', r['classification'])
assert r['skills'].get('hard_pass'), 'STILL RED -- do not commit'
print('GREEN')
"
```

- [ ] **Step 4: Commit**

```bash
cd /Users/alantorres/Projects/flox-skills
git add flox-plugin/skills/catalog-resolution-debug/SKILL.md
git commit -m "fix(skills): carry the manifest's allow options into the resolve request

The descriptor the skill built sent only install_id/attr_path/systems/
version, so a reproduction ran against different inputs than the real
install and could succeed where the install failed.

Verified against flox 1.14.0: [options].allow accepts exactly unfree,
broken and licenses (an 'insecure' key is rejected by name). The API
descriptor also takes allow_insecure/allow_pre_releases/
allow_missing_builds, which no manifest key sets, so the skill now says
to leave those at their defaults. Confirmed live that the mechanism is
real: allow.licenses = [\"MIT\"] blocks hello from resolving.

res-unfree-group: RED -> GREEN.

Refs: AI-504"
```

---

### Task 6: Complete the message taxonomy and add token hygiene

The two cheap fixes. Grouped because neither has a dedicated eval prompt and both are transcription from sources already verified: the live `MessageType` enum, and this repo's existing secret-handling posture.

**Files:**
- Modify: `flox-plugin/skills/catalog-resolution-debug/SKILL.md` (the "Authentication" and "Messages explain rejection" sections)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Complete the message list**

Find the bullet list under **Messages explain rejection:** and replace the whole list with this (the five additions are `not_in_catalog`, `unacceptable_licenses`, `change_in_version_format`, `resolution_logic`, `general`; the rest are unchanged):

```
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
```

- [ ] **Step 2: Add the message-level note**

Insert immediately after that list (blank line between):

```
Every message carries a **level** — `trace`, `info`,
`warning` or `error`. Read it before reporting: a
`trace`/`info` message is the resolver narrating its
work, not a reason resolution failed. Only `error` (and
usually `warning`) belongs in the diagnosis.

Pages also carry `complete`. An incomplete page has not
been fully scraped, so its absence of a package is not
evidence the package is missing.
```

- [ ] **Step 3: Add token hygiene to the Authentication section**

Find this exact text:

```
```bash
TOKEN=$(flox auth token)
```

Use as `Authorization: Bearer $TOKEN` header on all
catalog API calls.
```

Replace with:

```
```bash
TOKEN=$(flox auth token)
```

Use as `Authorization: Bearer $TOKEN` header on all
catalog API calls.

**Keep the token in the variable.** Never echo it, never
paste it into a command you show the user, and never let
it reach the diagnostic table or the final report. Refer
to it only as `$TOKEN`.
```

- [ ] **Step 4: Verify all five new message types are present**

```bash
cd /Users/alantorres/Projects/flox-skills
for m in not_in_catalog unacceptable_licenses change_in_version_format resolution_logic MessageLevel; do
  grep -qi "$m" flox-plugin/skills/catalog-resolution-debug/SKILL.md \
    && echo "$m: OK" || echo "$m: check wording"
done
grep -q "Never echo it" flox-plugin/skills/catalog-resolution-debug/SKILL.md && echo "token hygiene: OK"
```

Expected: `OK` for the message types present as literal strings, and `token hygiene: OK`. `MessageLevel` appears as the prose word "level" — `check wording` is acceptable there.

- [ ] **Step 5: Run the full resolution area to confirm nothing regressed**

```bash
cd evals/flox
flox activate -- python3 screen.py --area resolution \
  --model claude-haiku-4-5-20251001 \
  --reps 5 \
  --out results/green-resolution.json

python3 -c "
import json
d=json.load(open('results/green-resolution.json'))
for r in d['results']:
    print(r['id'], '| skills hard_pass:', r['skills'].get('hard_pass'), '| class:', r['classification'])
print('cost \$', d['summary'].get('total_cost_usd'))
assert all(r['skills'].get('hard_pass') for r in d['results']), 'a candidate is RED'
print('ALL GREEN')
"
```

Expected: `ALL GREEN` across all four candidates.

- [ ] **Step 6: Commit**

```bash
cd /Users/alantorres/Projects/flox-skills
git add flox-plugin/skills/catalog-resolution-debug/SKILL.md
git commit -m "fix(skills): complete the resolution message taxonomy, add token hygiene

Adds the five MessageType values the skill omitted
(attr_path_not_found.not_in_catalog, unacceptable_licenses,
change_in_version_format, resolution_logic, general), transcribed from
the live enum at api.flox.dev/catalog/api/v1/openapi.json. Adds the
message level (trace/info/warning/error) so resolver commentary is not
reported as a failure cause, and CatalogPage.complete so an unscraped
page is not read as a missing package.

Token hygiene: keep \`flox auth token\` output in \$TOKEN, never echo it
into a shown command, the diagnostic table, or the report.

Full area=resolution screen green.

Refs: AI-504"
```

---

### Task 7: User-facing documentation

Everything a user needs to know the skill exists and how to reach it. Three files, one commit, because they describe one change and a reviewer would accept or reject them together.

**Files:**
- Modify: `README.md` (3 edits)
- Modify: `flox-plugin/skills/README.md` (1 edit)
- Modify: `.flox/env/manifest.toml` (1 edit)

**Interfaces:**
- Consumes: the skill name `catalog-resolution-debug` from Task 1.
- Produces: nothing.

- [ ] **Step 1: `README.md` — update the skill count**

Find (line 14):

```
Two skills covering the Flox lifecycle, from a blank directory to a published build:
```

Replace with:

```
Three skills covering the Flox lifecycle, from a blank directory to a published
build — and diagnosing it when the catalog doesn't give you the build you expected:
```

- [ ] **Step 2: `README.md` — add the inventory bullet**

Find the end of the `floxify` bullet (line 36-37):

```
  - **Re-runs safely** — on a repo that already uses Flox, it audits for gaps
    instead of overwriting your manifest.
```

Insert immediately after it:

```
- **`catalog-resolution-debug`** — Work out why the catalog gave you the build it
  did. Reach for it when a package won't resolve, when `flox install` keeps
  picking an old build after you published a new one, or when adding one package
  makes a working environment fail with "constraints too tight". It explains what
  the resolver is actually choosing between and walks the diagnosis to a specific
  cause and a fix.
```

- [ ] **Step 3: `README.md` — add a "Using it" example**

Find (lines 106-107):

```
- *"Get this repo running with Flox"* → the **floxify** skill inspects the repo and
  writes a manifest you can `flox activate`.
```

Insert immediately after it:

```
- *"I published a new version but flox install still gives me the old build"* → the
  **catalog-resolution-debug** skill works out which build the resolver picked and
  why.
```

- [ ] **Step 4: `flox-plugin/skills/README.md` — add the inventory entry**

Find the end of the Skill Inventory list:

```
- `floxify`: Convert an existing repository to a verified working Flox
  environment, with detection and verification scripts in `scripts/`.
```

Insert immediately after it:

```
- `catalog-resolution-debug`: Diagnose Flox catalog resolution — why a given
  build was selected, why a new publish isn't picked up, and why adding a
  package can make a working `pkg-group` unsatisfiable. A procedure rather
  than reference material: it gathers the environment's packages, reproduces
  the resolve call, and reports a cause.
```

- [ ] **Step 5: `.flox/env/manifest.toml` — update the build description**

Find:

```
description = "Flox skills (flox, floxify) packaged for Claude Code, Codex, Pi, and OpenCode"
```

Replace with:

```
description = "Flox skills (flox, floxify, catalog-resolution-debug) packaged for Claude Code, Codex, Pi, and OpenCode"
```

- [ ] **Step 6: Verify the manifest still parses and the docs are consistent**

```bash
cd /Users/alantorres/Projects/flox-skills
flox activate -- python3 -c "import tomllib; tomllib.load(open('.flox/env/manifest.toml','rb')); print('manifest parses OK')"
grep -c "catalog-resolution-debug" README.md flox-plugin/skills/README.md .flox/env/manifest.toml
grep -n "Two skills" README.md && echo "STALE COUNT -- fix step 1" || echo "skill count OK"
```

Expected: `manifest parses OK`; counts of 2, 1, 1 respectively; `skill count OK`.

- [ ] **Step 7: Commit**

```bash
git add README.md flox-plugin/skills/README.md .flox/env/manifest.toml
git commit -m "docs: document catalog-resolution-debug for users (AI-504)

Top-level README: skill count, an inventory entry written for someone
hitting the problem rather than someone browsing features, and a
'Using it' example phrased the way a user would actually ask
('I published a new version but flox install still gives me the old
build').

Skill library README: inventory entry noting it is a procedure rather
than reference material, per the placement rationale in the spec.

Build description: names the third skill.

Refs: AI-504"
```

---

### Task 8: File the sequenced follow-ups

The spec's §7 exclusions become real tickets, or they are lost.

**Files:** none.

**Interfaces:**
- Consumes: nothing.
- Produces: nothing.

- [ ] **Step 1: File the `flox-internal-skills` retirement follow-up**

Create a Linear issue on team AI:

- **Title:** `Retire the catalog-resolution-debug copy in flox-internal-skills`
- **Description:**

```
Follow-up to AI-504, which ported catalog-resolution-debug into
flox/flox-skills as a standalone skill with four defects fixed.

**Blocked until AI-504's PR merges.** This is the sequencing dependency
that kept it out of AI-504: flox-skills must land publicly first, and
only then can the internal repo point at it. Bundling would couple two
repos' merge order to one ticket.

Scope:
- Remove skills/catalog-resolution-debug/ from flox/flox-internal-skills.
- Add a README pointer to the public plugin so internal users are not
  quietly left without the tool.

Note the internal copy carries the two fidelity defects AI-504 fixed
(systems taken from uname; the allow_* descriptor fields dropped), so
leaving it in place means internal users run the broken version.
```

- **Blocked by:** AI-504

- [ ] **Step 2: File the deferred content improvements**

Create a second Linear issue on team AI:

- **Title:** `catalog-resolution-debug: worked example and CLI-first ladder`
- **Description:**

```
Deferred from AI-504 (see docs/superpowers/specs/2026-08-04-catalog-
resolution-debug-placement-design.md §7). Both are real improvements,
neither blocks shipping.

1. **Worked example.** The skill asks the agent to build a candidate
   table from a response shape it has never seen. Capture one real
   resolve response and the table it produces, recording the catalog
   server version alongside it (the port was verified against
   1.0.0-446c496).

2. **CLI-first ladder.** The skill opens with curl. It should teach:
   read what `flox install` already printed, then `flox show`, and only
   then reach for the resolve API. Likely the single highest-value
   change for a real user hitting this.

Each needs an eval per the evals/README.md policy.
```

- [ ] **Step 3: Record the issue numbers**

Append the two issue identifiers to the spec's §7 entries so the spec points at its own follow-ups, then commit:

```bash
cd /Users/alantorres/Projects/flox-skills
git add docs/superpowers/specs/2026-08-04-catalog-resolution-debug-placement-design.md
git commit -m "docs(specs): link AI-504's deferred work to its follow-up issues

Refs: AI-504"
```

---

## Final verification

- [ ] **All four eval candidates green**

```bash
cd evals/flox
python3 -c "
import json
d=json.load(open('results/green-resolution.json'))
assert len(d['results']) == 4
assert all(r['skills'].get('hard_pass') for r in d['results'])
print('4/4 green')
"
```

- [ ] **The `flox` and `floxify` skills are untouched**

```bash
cd /Users/alantorres/Projects/flox-skills
git diff --name-only main...HEAD | grep -E 'skills/(flox|floxify)/' && echo "CONSTRAINT VIOLATED" || echo "flox + floxify untouched: OK"
```

Expected: `flox + floxify untouched: OK`. A hit here means the eval gate is now in scope and the placement rationale is broken.

- [ ] **Nothing was added to the gated task registry**

```bash
git diff --name-only main...HEAD | grep 'tasks/tasks.jsonl' && echo "CONSTRAINT VIOLATED" || echo "gated registry untouched: OK"
```

- [ ] **The build still produces all four agent trees**

```bash
flox build skills-flox
find result*/share/flox -maxdepth 3 -name 'catalog-resolution-debug' | wc -l
```

Expected: `4`.

- [ ] **Full changed-file review**

```bash
git diff --stat main...HEAD
```

Expected exactly: the new `SKILL.md`, `screening.jsonl`, `README.md`, `flox-plugin/skills/README.md`, `.flox/env/manifest.toml`, and the two spec/plan docs.
