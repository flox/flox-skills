#!/usr/bin/env python3
"""Conformance eval: does /floxify run its scripts through the wrapper?

This suite used to ask a different question. When the invocation-source
tag was a literal prefix in SKILL.md's command blocks, whether a flox call
got attributed to this skill depended on a model copying that line, and
this measured how often it did.

`scripts/flox-python.sh` moved the tag inside a script, so that question
is answered by construction. What remains is one step out: the model has
to invoke the wrapper rather than reaching for `flox run` or a bare
`python3` itself. A hand-written invocation runs the same script and
produces the same manifest, so nothing else in the suite would notice,
and the flox call it makes carries no tag.

So this reports how the prescribed scripts were actually reached. Reaching
them any other way is not a defect on its own -- the documented fallback
for a Flox older than 1.13 does exactly that -- which is why this reports
a rate rather than gating.

Opt-in like its siblings: spawns a real `claude`, never in the fast gate.

Usage:
    python3 invocation_source_eval.py                  # default fixture
    python3 invocation_source_eval.py --fixture ruby

Exit 0 if every reach went through the wrapper, 1 if any bypassed it, 2
if the run never reached the scripts at all. Pure stdlib.
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

# The two scripts SKILL.md prescribes. A `flox run` for anything else (the
# `php -m` example, say) is not one of these and is not scored.
_PRESCRIBED = re.compile(r"\b(detect|verify)\.py\b")
_VIA_WRAPPER = re.compile(r"\bflox-python\.sh\b")
# Reading the file is not running it.
_EXECUTES = re.compile(r"(?:\bflox-python\.sh\b|\bpython3?\b|\bflox\s+run\b)")


def _reaches_a_prescribed_script(cmd):
    c = cmd or ""
    return bool(_PRESCRIBED.search(c) and _EXECUTES.search(c))


def _via_wrapper(cmd):
    return bool(_VIA_WRAPPER.search(cmd or ""))


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
        reaches = [c for c in commands if _reaches_a_prescribed_script(c)]
        wrapped = [c for c in reaches if _via_wrapper(c)]

        print(f"\nBash calls: {len(commands)} · reached a prescribed script: "
              f"{len(reaches)} · through the wrapper: {len(wrapped)}")

        if not reaches:
            print("INCONCLUSIVE — the run never reached detect.py or "
                  "verify.py, so there was nothing to attribute either way.")
            return 2

        for c in reaches:
            mark = "wrapper " if _via_wrapper(c) else "BYPASSED"
            print(f"  {mark} {c.strip()[:150]}")

        if len(wrapped) < len(reaches):
            print(f"\n{len(reaches) - len(wrapped)} of {len(reaches)} reached a "
                  "prescribed script without the wrapper, so those flox calls "
                  "carry no skill attribution. Check whether the run took the "
                  "documented pre-1.13 fallback before reading this as a "
                  "compliance failure.")
            return 1

        print(f"\nPASS — all {len(reaches)} went through the wrapper.")
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
