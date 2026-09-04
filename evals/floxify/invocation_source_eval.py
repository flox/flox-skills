#!/usr/bin/env python3
"""Conformance eval: does /floxify tag the flox invocations it makes?

AI-597 wires the skill's invocation-source tag in two places. verify.py
sets FLOX_INVOCATION_SOURCE at module load, which is deterministic and
needs no eval. SKILL.md's `flox run` command blocks carry the tag as a
literal prefix, and that half depends on a model copying a line it was
shown — so without a measurement there is no way to tell "the skill did
not comply" from "the skill was not used", which is the ambiguity the
tag exists to remove.

This asserts on the SKILL.md half only. It reads the agent's own Bash
calls out of the stream and asks, of the `flox run` invocations the skill
prescribes, how many carried the prefix.

Compliance is a RATE, not a gate. A model that drops the prefix on one of
two blocks is a real signal about the guidance, not a broken build, and
this suite is opt-in like its siblings: it spawns a real `claude` agent
and never runs in the fast gate.

Usage:
    python3 invocation_source_eval.py                  # default fixture
    python3 invocation_source_eval.py --fixture ruby
    python3 invocation_source_eval.py --skill-dir /path/to/flox-plugin

Exit 0 if every prescribed `flox run` carried the tag, 1 if any did not,
2 on a setup error. Pure stdlib.
"""
import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from run_floxify import DEFAULT_SKILL_DIR, FIXTURES_DIR, MODEL
from detect_usage_eval import _bash_commands

PHASE3_PROMPT = (
    "/floxify {target}\n\n"
    "Run non-interactively: complete Phases 1 through 3 (scan the project, "
    "resolve packages in the Flox catalog, initialize Flox, write "
    ".flox/env/manifest.toml, and run Phase 3c's validate-and-verify steps, "
    "including Step 4's deterministic verify.py check). Do NOT produce the "
    "Phase 4 report and do not ask for user input — stop once Step 4 has run."
)

# The two scripts SKILL.md invokes through `flox run`. A `flox run` for
# anything else (a one-off `php -m`, say) is not prescribed by the tagged
# blocks and is not scored.
_PRESCRIBED = re.compile(r"\bflox\s+run\b[^\n]*?\b(detect|verify)\.py\b")

# Matches the tag on the same command, whether the model copied the
# preserving form or collapsed it to a bare assignment.
_TAGGED = re.compile(r"FLOX_INVOCATION_SOURCE=[^\n]*agentic\.skill\.floxify\.")


def _is_prescribed_flox_run(cmd):
    return bool(_PRESCRIBED.search(cmd or ""))


def _is_tagged(cmd):
    return bool(_TAGGED.search(cmd or ""))


def _preserves_existing(cmd):
    """Did the model keep the append form, or hardcode a bare value?

    Reported, never failed: a bare assignment still attributes the call to
    this skill and only loses an outer context's own tag, which is a
    weaker defect than no tag at all.
    """
    return ":+$FLOX_INVOCATION_SOURCE," in (cmd or "")


def run(skill_dir, fixture, model, timeout):
    src = FIXTURES_DIR / fixture
    if not src.exists():
        print(f"ERROR: fixture not found: {src}", file=sys.stderr)
        return 2
    tmp = tempfile.mkdtemp(prefix=f"invocation-source-{fixture}-")
    stream = Path(tmp) / "_stream.jsonl"
    try:
        shutil.copytree(str(src), tmp, dirs_exist_ok=True)
        cmd = [
            "claude", "-p", PHASE3_PROMPT.format(target=tmp),
            "--model", model,
            "--output-format", "stream-json", "--verbose",
            "--allowedTools", "Bash", "Read", "Write", "Edit", "Skill",
            "--plugin-dir", str(skill_dir),
            "--strict-mcp-config",
        ]
        print(f"running Phase-3 /floxify on fixture '{fixture}' ...", flush=True)
        with open(stream, "w", encoding="utf-8") as fh:
            proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.PIPE,
                                  text=True, timeout=timeout)
        if proc.returncode != 0:
            print(f"agent exited {proc.returncode}: {proc.stderr[:300]}",
                  file=sys.stderr)

        commands = list(_bash_commands(stream))
        prescribed = [c for c in commands if _is_prescribed_flox_run(c)]
        tagged = [c for c in prescribed if _is_tagged(c)]
        preserving = [c for c in tagged if _preserves_existing(c)]

        print(f"\nBash calls: {len(commands)} · prescribed `flox run`: "
              f"{len(prescribed)} · tagged: {len(tagged)} · "
              f"append-preserving: {len(preserving)}")

        if not prescribed:
            # Distinguishable from non-compliance on purpose: the run never
            # reached the tagged blocks, so it measures nothing either way.
            print("INCONCLUSIVE — the run made no `flox run` call against "
                  "detect.py or verify.py, so there was nothing to tag. "
                  "Check the fallback paths (system python3) were not taken.")
            return 2

        for c in prescribed:
            mark = "tagged  " if _is_tagged(c) else "UNTAGGED"
            print(f"  {mark} {c.strip()[:150]}")

        if len(tagged) < len(prescribed):
            print(f"\nFAIL — {len(prescribed) - len(tagged)} of "
                  f"{len(prescribed)} prescribed invocations carried no tag. "
                  "The SKILL.md prefix is being dropped.")
            return 1

        print(f"\nPASS — all {len(prescribed)} prescribed invocations tagged.")
        if len(preserving) < len(tagged):
            print(f"  NOTE: {len(tagged) - len(preserving)} collapsed the "
                  "append form to a bare assignment — attribution still "
                  "works, an outer context's own tag would be lost.")
        return 0
    except subprocess.TimeoutExpired:
        print(f"FAIL — timed out after {timeout}s", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fixture", default="node-postgres")
    ap.add_argument("--skill-dir", type=Path, default=DEFAULT_SKILL_DIR)
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()
    return run(args.skill_dir, args.fixture, args.model, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
