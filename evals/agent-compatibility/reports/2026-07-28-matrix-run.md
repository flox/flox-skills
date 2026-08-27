# Agent installation compatibility — full matrix run, 2026-07-28

**Question:** does the Flox skill install and reach the model in all eight
agent-application × installation-method combinations, on the published
`flox/skills-flox@1.0.0`? (The verification items AI-497 held open for Codex,
OpenCode and `npx skills add`.)

**Run conditions:** images `hosts-base:20260728` / `hosts-withpkg:20260728`
(the environments have since been renamed `agent-compat-*`), both built from
`flox/skills-flox@1.0.0` as published. Claude Code 2.1.220, codex-cli 0.145.0,
OpenCode 1.18.8. Full authenticated run, one prompt per cell. **Both credential
directories were mounted into every container of this run, including the load
half** — the runner mounts nothing into the load container now, and mounts only
the cell's own agent's store into the trigger one, so the 8/8 load column here
is not evidence that the two native cells install without a login. That is the
caveat the README's *What leaves your machine* section raises about these
numbers, and it belongs in the conditions rather than only beside them.

**Conclusion:** 8/8 stand. 8/8 installed and 8/8 produced skill-shaped
answers as recorded. The two OpenCode rows were flagged unexplained on
2026-08-26 and were **settled by rerun on 2026-08-27**: OpenCode needs no login
to answer, so those two cells were never authenticated passes and nothing on the
credential path is unaccounted for. What they may and may not be cited for
is narrower than the other six — see below. No cell proves the model
*invoked* the skill — and a control arm run on 2026-08-27 shows the trigger
column's evidence class does not establish installation either, which is
weaker than this file originally claimed. The `load` column is unaffected and
still carries 8/8. See "A pinned model for OpenCode" below.

**Owner:** Bill LeVine (bill@flox.dev). **Lifecycle:** superseded by the next
full authenticated run of the same matrix, at which point this file is deleted
rather than archived — see the README's *Retained evidence* section.

| cell | load | trigger | evidence | verdict |
|---|---|---|---|---|
| claude-native | pass | pass | answer-shaped | ✅ |
| claude-npx | pass | pass | answer-shaped | ✅ |
| claude-flox-ai | pass | pass | answer-shaped | ✅ |
| codex-native | pass | pass | answer-shaped | ✅ |
| codex-npx | pass | pass | answer-shaped | ✅ |
| codex-flox-ai | pass | pass | **not-injected** | ✅ false alarm — see below |
| opencode-npx | pass | pass | answer-shaped | ✅ unauthenticated — see below |
| opencode-flox-ai | pass | pass | answer-shaped | ✅ unauthenticated — see below |

**8/8 install. 8/8 produced skill-shaped answers as recorded** — the
lone `not-injected` flag proved to be a flox-ai reporting bug, not a failure.
The two OpenCode rows carry a narrower meaning than the other six, established
by rerun rather than re-scored: see below.

The `not-injected` class no longer exists: that investigation is why the
warning became a `notes` entry instead of a verdict, so a re-run of this matrix
records `codex-flox-ai` as `answer-shaped` with the warning beside it.

## The two OpenCode rows — settled by rerun, 2026-08-27

Flagged unexplained 2026-08-26 while addressing review of the runner. **Rerun
on 2026-08-27; the flag is cleared.** The finding that raised it was correct
about the code and wrong about what followed from it.

What still holds, unchanged: nothing in the runner prepares or mounts an
OpenCode credential. `creds.prepare` writes the Claude and Codex files only,
and `docker_cmd` mounts only the cell's own agent's store — which for an
OpenCode cell is nothing at all. The binary scan stands too: the shipped
OpenCode resolves credentials solely from `$XDG_DATA_HOME/opencode/auth.json`,
else `~/.local/share/opencode/auth.json`.

What does not hold is the inference: that an exit-0 trigger therefore recorded
an *authenticated* pass on a path this repository does not implement.
**OpenCode does not need a login to answer.** It looks for a credential exactly
where the scan said it would, finds none, and serves the prompt from its own
built-in provider regardless.

**Rerun conditions.** `--cells opencode-npx,opencode-flox-ai` on this branch,
images `agent-compat-base:20260827` / `agent-compat-withpkg:20260827` rebuilt
from the pinned environments — OpenCode 1.18.8, flox-ai 0.8.0,
`flox/skills-flox@1.1.0`. No credential directory mounted into either trigger
container, and none on the host to mount: `~/.local/share/opencode/auth.json`
does not exist on the machine that ran this.

| observation | result |
|---|---|
| `opencode auth list` inside the container | `0 credentials` |
| `~/.local/share/opencode/auth.json` | absent before and after |
| `opencode models` | 7 models, all provider `opencode`, five suffixed `-free` |
| bare `opencode run <prompt> --format json`, nothing mounted | exit 0, 21,201 bytes |
| every `step_finish` in every transcript | `"cost":0` |

So the two rows record OpenCode answering on its free built-in provider. They
are not authenticated passes, they never were, and no credential is
unaccounted for. **The rows stand with their meaning corrected:** they
establish that the skill installed and that OpenCode reached *a* model. They do
**not** establish that a developer's own OpenCode login works — no run in this
file has ever exercised one, and none can until the runner grows a third
credential store.

This also settles the deferred per-agent credential gate in its favour. It was
held back on the premise that gating would redden two cells; with nothing
mounted, both cells pass. The premise is refuted by execution now, not only by
a binary scan.

### The free provider is bimodal, and that is the live risk to these two cells

Not a credential problem. Eight OpenCode launches were observed on 2026-08-27,
all with zero credentials, across both images and both the bare and `flox-ai`
launch paths:

- **Four answered**, each well inside two minutes — the whole second matrix
  run, both cells and all four containers, took 3m38s.
- **Four produced zero bytes** and had to be killed, at caps of 240s, 600s,
  600s and 900s.

A hung attempt emits *nothing*, not a slow stream, so **a larger `--timeout`
does not buy a verdict** — the 900s attempt was as empty as the 240s one. That
makes the first attempt's `opencode-npx` timeout a property of the provider,
not of the skill or the installation method, and it means a `timeout` on either
OpenCode cell should be read as free-tier queueing before it is read as a
failure. A bounded retry is the shape that would fix it; a bigger budget is
not. Neither is done here.

### OpenCode can prove invocation, and the window discards the proof

`opencode run --format json` emits skill use as a tool call, observed in a full
transcript captured from these images:

```json
{"type":"tool_use","part":{"tool":"skill",
  "state":{"status":"completed","input":{"name":"customize-opencode"}}}}
```

So the blanket claim in the next section — that no cell proves the model
*invoked* the skill — is true of Claude Code and Codex but **not** of
OpenCode. A `"tool":"skill"` part naming `flox` would be direct proof,
strictly stronger than `answer-shaped`.

The runner cannot see it. `trigger_evidence` keeps the last 4000 characters,
and OpenCode calls its tools early and answers at length, so the tool call is
precisely what the window truncates away: both rerun rows retain the
fingerprints (five of five for `opencode-npx`, four of five for
`opencode-flox-ai`) and zero tool calls. Scoring an OpenCode cell on invocation
needs the head of the transcript, not the tail.

## A pinned model for OpenCode, and what it exposed — 2026-08-27

The free provider above cannot be held to anything: it is unpinned, it answers
about half the time, and nothing in a row said which model produced it. So the
runner grew `--opencode-model`, an opt-in that gives the OpenCode cells an
OpenRouter key (`~/.env-open-router`, minimized to
`~/.local/share/opencode/auth.json`) and a named model — **GLM 5.3 Flash**,
`openrouter/z-ai/glm-5.3-flash`, for this run. **Off by default** —
without the flag the OpenCode cells run exactly as they did above, and the
file is never read.

Both cells, `--opencode-model openrouter/z-ai/glm-5.3-flash`, images rebuilt
from the pinned environments:

| cell | load | trigger | evidence | model recorded |
|---|---|---|---|---|
| opencode-npx | pass | pass | answer-shaped (5/5) | `openrouter/z-ai/glm-5.3-flash` |
| opencode-flox-ai | pass | pass | answer-shaped (5/5) | `openrouter/z-ai/glm-5.3-flash` |

Exit 0, four containers, 6m03s including both image rebuilds — against four
hangs in eight launches on the free provider. Two things had to change for the
verdict to mean anything: OpenCode 1.18.8 bakes its model catalogue in at build
time and stops at GLM 5.2 (`z-ai/glm-5.2`), so an unregistered id fails with
`UnknownError: Unexpected server error` that reads as a provider outage — the
cell now mounts an `opencode.json` registering the model under its provider.
And the results row now carries `model`, because without it a free run and a
paid one are the same cell id with the same `answer-shaped` on disk, and
merge-by-cell-id would let either stand in for the other.

### The control arm, run at last — and `answer-shaped` does not survive it

A pinned model made the control arm cheap, so it was run. Same prompt, same
model (GLM 5.3 Flash), **no skill installed**:

| arm | tools | fingerprints | class |
|---|---|---|---|
| control, no skill | webfetch available | 4 of 5 | answer-shaped |
| control, no skill | webfetch disabled | 4 of 5 | answer-shaped |
| opencode-npx, skill installed | full | 5 of 5 | answer-shaped |
| opencode-flox-ai, skill installed | full | 5 of 5 | answer-shaped |

**Both controls clear the bar.** The class separates nothing, and the section
below understates the problem: it says `answer-shaped` is not proof of
invocation, and the truth is that it is not evidence of installation either.

Two details make it worse rather than better. The web-enabled control answered
by fetching `raw.githubusercontent.com/flox/flox-skills` — it went and
downloaded the very skill whose installation the cell exists to test, after
reading five pages of `flox.dev/docs`. And the offline control, with webfetch
turned off and nothing but the model's own knowledge, still cleared the bar
with the same four fingerprints. The one fingerprint both controls missed is
`[services]`, and not for a reason worth keeping: each wrote
`[services.postgres]` without a bare `[services]` table, so the miss is
substring matching rather than an absence of guidance. Raising the threshold to
5/5 would move the numbers without making them mean more.

What this does **not** overturn: the `load` column, which is a filesystem check
and answers a different question. All eight cells still install. What it
overturns is the trigger column's evidence class as a signal about the skill —
it says the agent started and produced a plausible manifest, and no more.

## What "answer-shaped" means, and what it does not

None of the three agent applications enumerates its loaded skills in headless
mode. So no cell here proves the model *invoked* the skill — with one
exception the runner does not yet exploit: OpenCode reports tool calls, so a
`"tool":"skill"` part naming `flox` would be proof, and the 4000-character
evidence window truncates it away. See the rerun section above.

What a green cell establishes is: the plugin installed by that installation
method, the agent started, the prompt reached the model, and the answer
carried guidance the skill teaches (versioned `pkg-path`s, `[services]`
wiring) rather than the bare `python`/`postgres` an unguided model tends to
emit.

That was taken as good enough to close "does it load and work in this agent",
which is what AI-497 asks. Half of it no longer holds: the control arm above
clears the same bar with no skill installed at all, so what closes the
installation question is the `load` column — a filesystem check, and
unaffected — not this class. It is also **not** a trigger rate, and should
not be quoted as one: measuring reliability needs many repetitions and belongs
in the screening tier.

## The one flagged cell — the warning is a false alarm

`codex-flox-ai` is classified `not-injected` because Codex prints:

```
warning: codex is not the flox-patched build; skills and rules will not be
injected
```

**That warning is wrong, and injection works.** Investigated 2026-07-28:

1. **The catalog build IS patched.** `flox/codex` 0.145.0's real binary carries
   both patch symbols — `CODEX_FLOX_SKILL_ROOTS` (9 occurrences) and
   `CODEX_FLOX_INSTRUCTIONS_FILE` (7).
2. **flox-ai's detector is defeated by Nix wrapping.** `codexIsPatched()`
   (`internal/launch/codex.go`) byte-scans the binary it is handed for the
   `CODEX_FLOX_SKILL_ROOTS` symbol. On a Nix install, `codex` on PATH is a
   410-byte bash wrapper that `exec`s `.codex-wrapped`; the marker lives one
   level down, so the scan misses it and `Check()` returns Degraded.
3. **Injection is not gated on that check.** `codexAdapter.Build()` sets both
   env vars unconditionally — only `Check()` / `flox-ai doctor` consults the
   marker.
4. **Proven live.** Asked through `flox-ai launch codex`, Codex listed its
   available skills as: `imagegen, openai-docs, plugin-creator, skill-creator,
   skill-installer, flox, floxify`. Both of ours are there, warning
   notwithstanding.

So all three Codex paths work. The defect is a cosmetic false alarm in
flox-ai's patch detection, which should resolve the real binary (follow Nix
wrapper indirection) before scanning. Worth an issue against
[`flox/flox-agent`](https://github.com/flox/flox-agent) — where this code
actually lives; `flox-ai` is not a repository — not against this repo, and not
a blocker for 1.0. No issue has been filed yet.

Background: flox-ai ADR-0011 ("Inject codex fragments via a downstream env-var
patch") explains the seams. Codex exposes none of the injection flags flox-ai
uses for Claude (`--plugin-dir`, `--append-system-prompt-file`), and its skill
discovery is location-fixed (`.agents/skills/`, `$HOME/.agents/skills`), so
Flox carries a ~5-line downstream patch adding the two env-var reads.

## Harness bugs this run exposed

Recorded because each was caught by evidence, not by exit status — and any of
them would have put a wrong tick on AI-497.

1. **False pass (load).** The check grepped the whole transcript, so
   skills.sh's picker printing "flox"/"floxify" satisfied it while
   `claude plugin list` said "No plugins installed". Fixed with a post-install
   marker; only text after it is judged.
2. **False pass (trigger).** `flox-ai launch claude -- claude -p …` ran the
   binary twice; the prompt was dropped and the model replied "I'm here and
   ready. What would you like to work on?" — exit 0. flox-ai forwards args
   after `--` verbatim, so the binary name must be omitted.
3. **False negative (trigger).** Codex answers describing PostgreSQL "trust
   authentication" tripped a bare `authentication` marker and two good runs
   were scored `auth-error`, on exit-0 runs. Markers are precise phrases now,
   and a zero exit is never an auth failure.
4. **Data loss.** A `--cells` subset run rewrote the results file wholesale,
   discarding the cells it did not run. Results now merge by cell id.

Each has a regression test built from the transcript that produced it — bug 2's
was added in AI-509 Ticket 5, having been claimed before it existed.

## Reproducing

```bash
cd evals/agent-compatibility
python3 run_matrix.py --dry-run     # plan only, no containers, no spend
python3 run_matrix.py --load-only   # load half, no credentials needed
python3 run_matrix.py               # full run
python3 run_matrix.py --cells opencode-npx,opencode-flox-ai --timeout 1200
```

The flag was `--tier-a-only` and the results keys were `tier_a` / `tier_b` when
this run was recorded; both were renamed to `load` / `trigger` in AI-509
Ticket 5.
