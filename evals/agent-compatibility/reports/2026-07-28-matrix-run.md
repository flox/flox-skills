# Agent installation compatibility — full matrix run, 2026-07-28

**Question:** does the Flox skill install and reach the model in all eight
agent-application × installation-method combinations, on the published
`flox/skills-flox@1.0.0`? (The verification items AI-497 held open for Codex,
OpenCode and `npx skills add`.)

**Run conditions:** images `hosts-base:20260728` / `hosts-withpkg:20260728`
(the environments have since been renamed `agent-compat-*`), both built from
`flox/skills-flox@1.0.0` as published. Claude Code 2.1.220, codex-cli 0.145.0,
OpenCode 1.18.8. Full authenticated run, one prompt per cell.

**Conclusion:** 8/8 install, and 8/8 produced skill-shaped
answers. No cell proves the model *invoked* the skill — see "What
answer-shaped means" below — and that is not what AI-497 asked.

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
| opencode-npx | pass | pass | answer-shaped | ✅ |
| opencode-flox-ai | pass | pass | answer-shaped | ✅ |

**8/8 install. 8/8 produced skill-shaped answers** — the
lone `not-injected` flag proved to be a flox-ai reporting bug, not a failure.

The `not-injected` class no longer exists: that investigation is why the
warning became a `notes` entry instead of a verdict, so a re-run of this matrix
records `codex-flox-ai` as `answer-shaped` with the warning beside it.

## What "answer-shaped" means, and what it does not

None of the three agent applications enumerates its loaded skills in headless
mode. So no cell here proves the model *invoked* the skill. What a green cell
establishes is: the plugin installed by that installation method, the agent
started, the prompt reached the model, and the answer carried guidance the
skill teaches (versioned
`pkg-path`s, `[services]` wiring) rather than the bare `python`/`postgres` an
unguided model tends to emit.

That is good enough to close "does it load and work in this agent", which is
what AI-497 asks. It is **not** a trigger rate, and it should not be quoted as
one — measuring reliability needs many repetitions and belongs in the
screening tier.

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
wrapper indirection) before scanning. Worth an issue against flox-ai — not
against this repo, and not a blocker for 1.0.

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
```

The flag was `--tier-a-only` and the results keys were `tier_a` / `tier_b` when
this run was recorded; both were renamed to `load` / `trigger` in AI-509
Ticket 5.
