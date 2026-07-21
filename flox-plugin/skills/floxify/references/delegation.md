# Delegating this skill to a cheaper model

**If you are the delegated subagent, skip this section and start at
Phase 0.** This section is written for the parent session deciding
whether to delegate — not for the subagent doing the conversion.

A session running a capable model that has been asked to floxify a repo
may delegate the conversion itself to a cheaper-model subagent —
provided that subagent has this skill loaded, and its result is gated on
the skill's own deterministic verify leg rather than trusted on the
delegate's say-so.

**The verify gate carries correctness, not the model.** Phase 3c grounds
both ends of the conversion outside the model's own judgment:
`detect.py` reads the repo's pin files and lockfiles into `$DETECT_JSON`
before a single manifest line is written, and `verify.py` checks the
finished manifest against those same grounded facts before the report is
allowed to appear. A subagent that reaches Phase 4 has therefore passed
the identical four-step gate a frontier-model run has to pass — there is
no separate, lower bar for a delegated run. That closed loop is what
makes delegating to a cheaper model viable here: the skill has already
moved the source of truth outside the model, so the model only has to
execute the recipe faithfully, not reason its way to correctness
unaided.

**The skill is the enabling component — the model alone is not enough.**
Measured across five fixture repos (Go, Ruby, Rust, Python/uv,
Node+Postgres), n=8 per cell: claude-haiku-4-5-20251001 running WITHOUT
this skill is not viable to delegate to — verify rate 0.25 on the
service-wiring fixture, a 0.80 hard-violation rate across the batch.
The same model WITH this skill loaded reached 40/40 verified, 40/40
verify.py-clean, zero hard violations, and 100% of the golden
hard-checks, at a median $0.21–0.26 per verified conversion — total
cost including the LLM-judge leg, not agent spend alone; agent-only
cost is lower — 4–6× under an Opus-plus-skill run against the identical
gates, at roughly 1.3–1.7× the turns (measured: flox-skills commits
6521466, c11e02b on `bill/ai-442-efficiency-evidence`). Delegate the
skill along with the model — a cheap model without it produced
conversions the batch above would not verify.

**The guarantee is the verify gate, not prose-quality parity.** The
deterministic checks above are equal across models — verified rate,
verify.py-clean rate, and hard-pass rate all match between the Haiku
and Opus arms. The advisory LLM-judge score does not: Haiku-plus-skill
averaged 2.92/5 against Opus-plus-skill's 4.33/5 on the same rubric
(same commits as above). Delegating gets you a manifest that clears
the identical hard bar, not one that reads identically to what a
frontier model would have written.

**Escalate to the parent model on any verify failure — never ship
unverified cheap-model output.** If Phase 3c Step 4 still reports a
violation after a reasonable number of fix-and-recheck cycles, or Steps
1–3 never reach a clean activation, the subagent's job is to stop and
report the failure, not loosen the gate, skip straight to the Phase 4
report, or hand back a manifest nothing has verified. The calling
session then either retries with its own model or surfaces the failure
to the developer. This is not optional: the measured numbers above
describe VERIFIED conversions, not attempted ones, and that distinction
only holds if every delegated run is actually gated.

**Read the measured numbers as a floor, not a guarantee.** The batch
above is real — n=8 per cell, five fixture families, two models — but it
covers one repo shape per ecosystem, not an exhaustive survey. Read
"measured on five fixture families" as the actual scope of this
evidence, not "works on any repo." The measured escalation rate for the
skill-guided cheap-model arm was 0/40 (rule-of-three 95% upper bound
7.5%) — zero escalations were observed in this batch, not zero possible.
The verify gate, not this paragraph, is what decides whether any
individual delegated run shipped correctly.

### Agent-tool recipe

For a Claude Code session with the Agent/Task tool available, the shape
below spawns a haiku-tier subagent with this skill and applies the
verify-gate-then-escalate loop above. Confirmed live: a haiku-tier Task
subagent lists `flox:floxify` in its available skills and can invoke it
normally through the Skill tool — the premise below holds in practice,
not just in principle. This is the one recipe this skill recommends —
adapt the target and model tier, but keep the verify-or-stop
instruction intact:

```
Task(
  subagent_type: "general-purpose",
  model: "haiku",
  description: "Floxify <target> — cheap-model delegation, verify-gated",
  prompt: """
    Before starting, confirm `flox:floxify` appears in your available
    skills listing. If it doesn't, stop immediately and report back to
    the parent session — do not attempt the conversion without it.

    Use the `flox:floxify` skill to set up a Flox environment for
    <target>. Follow every phase in this skill's SKILL.md exactly,
    including Phase 3c's verify.py gate (Step 4) — do not skip it.

    If Step 4 still reports a violation after a reasonable number of
    fix-and-recheck cycles, or Steps 1-3 never reach a clean
    `flox activate`, STOP: do not print the Phase 4 report. Return the
    verify.py output (or activation error) and the manifest as written
    so far, and say plainly that the conversion did not verify.

    Only on a clean verify.py run and clean activation, proceed to the
    Phase 4 report and return it in full.
  """
)
```

On return: a verified result relays straight to the developer as the
Phase 4 report. An escalation is not a retry target for the same
subagent — either run floxify directly with the parent session's own
model, or, if a partial manifest came back, resume from it rather than
starting over.
