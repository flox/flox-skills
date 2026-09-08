# Invocation source tags

The flox CLI reads `FLOX_INVOCATION_SOURCE` at startup and reports it on two
channels: the `flox-invocation-source` header on catalog, FloxHub and floxmeta
requests, and the `invocation_sources` array in its telemetry event. It also
infers the agent *host* on its own, emitting tags like `agentic.claude-code.cli`
and `agentic.flox-mcp`.

Those host tags answer "which agent ran flox". They cannot answer "did flox run
*because a skill said to*", which is what this repo sets its own tag for.

This file is the vocabulary. It exists so whoever queries the telemetry knows
what to expect without reading the skills.

## Shape

```
agentic.skill.<skill>[.<path>].<version>
```

| Segment | Value |
| -- | -- |
| `agentic.skill` | fixed prefix, distinguishes a skill tag from a host tag |
| `<skill>` | the skill's directory name under `flox-plugin/skills/` |
| `<path>` | optional, for a reference file within a skill; unused today |
| `<version>` | the plugin version, dots replaced by dashes |

Dot is the hierarchy separator, so the version uses dashes: `1-1-0`, not
`1.1.0`. Truncating a tag to its first three fields coalesces every version of
one skill.

**Extract the version right-anchored** — the last segment matching
`^\d+(-\d+)*$` — not as field four. The optional `<path>` segment varies the
field count, so a positional read breaks the moment one is added.

## Tags emitted today

| Tag | Emitted by |
| -- | -- |
| `agentic.skill.floxify.1-1-0` | `scripts/verify.py` at module load, covering every `flox show` it makes; and the two `flox run` blocks in `floxify/SKILL.md` |

The core `flox` skill emits no tag yet. It is SKILL.md and `references/` with no
scripts, so the module-load approach cannot reach it (AI-597 task 3).

## Version source

`flox-plugin/.claude-plugin/plugin.json`, dots to dashes. Not the SKILL.md
frontmatter: `floxify`'s reads `1.0.0` where `plugin.json` reads `1.1.0`, and
the `flox` skill has no version field at all, so frontmatter is not maintained.

The version is a literal in both `verify.py` and `SKILL.md` rather than read at
runtime, because `.flox/nix/flox-agent-layout.sh` ships `plugin.json` only in
the Claude layout: codex, pi and opencode receive bare skill directories with no
plugin root. `evals/floxify/tests/test_invocation_source.py` reads all three
files and fails when they drift.

## Reading the data

A tag is appended, never substituted, so a value set by an outer context
survives and one invocation can carry several:

```
agentic.claude-code.cli,agentic.skill.floxify.1-1-0
```

Count invocations by membership in the list, not by equality against it.

Two things bound what these numbers mean. Emission is gated on
`config.flox.disable_metrics` like every other tag, so opt-outs are invisible
and this measures mix and trend rather than absolute installs. And the tag on
the `flox run` blocks depends on a model copying a line it was shown;
`evals/floxify/invocation_source_eval.py` measures that compliance separately,
so a fall in tagged invocations is not on its own evidence that skill usage
fell.

The tag carries no prompt, repo content, or file path.
