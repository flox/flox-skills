#!/usr/bin/env sh
# Run flox with this skill's invocation-source tag, so every call a floxify
# run makes (search, show, init, activate, services, the Nix eval) is
# attributed to the skill, not just the two scripts flox-python.sh runs.
#
# Usage, from SKILL.md:
#   "<skill-dir>/scripts/flox.sh" <flox args...>
#
# Named flox.sh, not flox, so it can never shadow the real binary on PATH.
set -eu

# APPEND, never overwrite (see flox-python.sh).
FLOX_INVOCATION_SOURCE="${FLOX_INVOCATION_SOURCE:+$FLOX_INVOCATION_SOURCE,}agentic.skill.floxify.1-1-0"
export FLOX_INVOCATION_SOURCE

exec flox "$@"
