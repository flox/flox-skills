#!/usr/bin/env sh
# Run one of this skill's Python scripts through Flox.
#
# Two things live here so SKILL.md does not have to carry either: the
# pinned interpreter, and the tag that attributes the flox call to this
# skill. Both are implementation detail. Putting them in a command block
# means every invocation is a line a model has to copy correctly, and a
# skill that spends its instruction budget on an environment variable is
# spending it in the wrong place.
#
# Usage, from SKILL.md:
#   "<skill-dir>/scripts/flox-python.sh" "<skill-dir>/scripts/detect.py" DIR
set -eu

# APPEND, never overwrite: a nested context (flox-mcp-server, CI) sets its
# own tag and both should survive. Matches what verify.py does at module
# load for the `flox show` calls it makes directly.
FLOX_INVOCATION_SOURCE="${FLOX_INVOCATION_SOURCE:+$FLOX_INVOCATION_SOURCE,}agentic.skill.floxify.1-1-0"
export FLOX_INVOCATION_SOURCE

# exec: this is a wrapper, so the child's exit status is the whole result
# and there is nothing to do after it.
exec flox run -p python313 -- python3 "$@"
