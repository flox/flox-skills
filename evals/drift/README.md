# Drift evals

Are the skills' claims about the flox CLI still true of the flox that is
installed?

`evals/flox/skill_toml_lint.py` checks that TOML snippets in the skills parse.
Nothing checked the prose. That is where the skills actually go stale: a flag
renamed upstream leaves a skill confidently telling an agent to run something
that no longer exists, and the skill keeps passing every gate.

## Run

```bash
flox activate
cd evals/drift
python3 check.py                     # verify the shipped registry
python3 check.py --suggest           # print the current surface beside drift
python3 -m unittest discover -s tests -t .
```

Deterministic, offline, free. Exit 0 when every claim holds, 1 on drift, 2 on
a setup error.

## How it works

`tasks/claims.jsonl` holds one load-bearing claim per line, each linked in
both directions:

```json
{"id": "activate-mode-flag", "kind": "flag_exists", "command": ["activate"], "flag": "--mode", "skill_ref": {"file": "skills/flox/SKILL.md", "quote": "flox activate -m dev|run"}}
```

`check.py` verifies two things per entry. The claim holds against
`flox <cmd> --help`, and the `quote` still exists in the skill file it names.
The second is what stops the registry rotting: an entry whose quote has left
the skill guards nothing, and its id will never lead anyone to a real line, so
it is reported rather than passed.

Drift in either direction is a finding, not a verdict. The claim may be right
and the registry stale, so the output says to check both ends before editing.

## What it does not check

Two kinds only, `command_exists` and `flag_exists`, because those are the two
that provably work offline. Read the coverage narrowly:

- **Manifest semantics are out.** Flox 1.15 dropped `x86_64-darwin` from the
  default enabled set, which broke a claim in `flox/SKILL.md`. That surfaced
  through a failing build eval, and this checker would not have caught it:
  it is neither a command nor a flag. That class is the deferred `enum_values`
  kind.
- **`schema_version_current` has no working probe.** A fresh `flox init`
  writes a commented template with no readable version line, so the
  schema-version table in `flox/SKILL.md` stays unchecked.

## Suggestions are not rewrites

`--suggest` prints the current surface next to the stale line. It does not
propose a substitution. "This is gone" and "this exists now" are confident;
"this was renamed to that" is a guess, and a wrong auto-suggestion in a
pre-commit hook costs more than no suggestion at all.

## Adding a claim

Pick something load-bearing and quote it from the skill exactly. The registry
is meant to stay around ten entries and grow slowly: ten claims that stay true
are worth more than forty that rot, and every entry is maintenance in both
directions.

`tests/test_check.py` asserts that every shipped quote still resolves, so a
claim that drifts from its skill fails the offline suite with no flox needed.
