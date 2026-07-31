#!/usr/bin/env python3
"""New-feature conformance eval: does /floxify actually run verify.py?

Policy (evals/README.md): every skill change ships with an eval that
verifies the guidance is actually followed. verify.py (AI-461) is wired
into Phase 3c as Step 4 — a rule the model never runs is a rule that
doesn't exist (the same reasoning detect_usage_eval.py already applies to
detect.py). test_verify.py proves the checker is *correct*; THIS eval
proves the *skill* reaches for it — it runs a real, Phase-3-bounded
/floxify against a fixture with a tool-call-visible stream and asserts a
Bash step invoked verify.py against a manifest the skill actually wrote.

Heavier than detect_usage_eval.py (Phase 1-only): this needs the skill to
run all the way through package resolution, `flox init`, and writing
manifest.toml before Step 4 has anything to check. Still opt-in like
detect_usage_eval.py and the real-world harness — spawns a real `claude`
agent, run manually or on a schedule, never in the fast gate.

Usage:
    python3 verify_usage_eval.py                     # default fixture (node-postgres)
    python3 verify_usage_eval.py --fixture ruby
    python3 verify_usage_eval.py --skill-dir /path/to/flox-plugin

Exit 0 if verify.py was invoked against the produced manifest (and, as a
bonus signal, run through `flox run`); exit 1 otherwise. Pure stdlib.
"""
import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from run_floxify import DEFAULT_SKILL_DIR, FIXTURES_DIR, MODEL
from detect_usage_eval import _bash_commands, _is_via_flox_run

PHASE3_PROMPT = (
    "/floxify {target}\n\n"
    "Run non-interactively: complete Phases 1 through 3 (scan the project, "
    "resolve packages in the Flox catalog, initialize Flox, write "
    ".flox/env/manifest.toml, and run Phase 3c's validate-and-verify steps, "
    "including Step 4's deterministic verify.py check). Do NOT produce the "
    "Phase 4 report and do not ask for user input — stop once Step 4 has "
    "run (whether it passes cleanly or you fix a violation and re-run it)."
)

# Same invocation-matching discipline as detect_usage_eval.py's
# _is_analyzer_invocation (AI-455): an interpreter directly followed by
# verify.py within the same command segment, so a `cat verify.py` or
# `grep ... verify.py` in an unrelated segment can't count as a run.
_SEGMENT_SPLIT = re.compile(r"[;&|]+")
_EXEC_FORM = re.compile(r"\bpython3?\b[^\n]*?\bverify\.py\b")


def _is_verifier_invocation(cmd):
    """True only if `cmd` actually executes verify.py."""
    return any(_EXEC_FORM.search(seg) for seg in _SEGMENT_SPLIT.split(cmd or ""))


def run(skill_dir, fixture, model, timeout):
    src = FIXTURES_DIR / fixture
    if not src.exists():
        print(f"ERROR: fixture not found: {src}", file=sys.stderr)
        return 2
    tmp = tempfile.mkdtemp(prefix=f"verify-usage-{fixture}-")
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

        manifest_written = (Path(tmp) / ".flox" / "env" / "manifest.toml").exists()

        commands = list(_bash_commands(stream))
        invoked = [c for c in commands if _is_verifier_invocation(c)]
        via_flox_run = [c for c in invoked if _is_via_flox_run(c)]
        mentioned_only = [
            c for c in commands
            if "verify.py" in c and not _is_verifier_invocation(c)
        ]

        print(
            f"\nmanifest written: {manifest_written} · Bash calls: "
            f"{len(commands)} · verifier invocations: {len(invoked)} · "
            f"via `flox run`: {len(via_flox_run)}"
        )
        if mentioned_only and not invoked:
            print("  NOTE: verify.py was mentioned but never executed "
                  f"(e.g. {mentioned_only[0][:80]!r}) — that is not a pass.")
        if not manifest_written:
            print("  NOTE: no manifest.toml was written — verify.py had "
                  "nothing to check even if it ran.")
        if invoked:
            print("PASS — /floxify invoked verify.py:")
            print(f"  {invoked[0][:200]}")
            if not via_flox_run:
                print("  NOTE: verifier ran but not through `flox run` "
                      "(fallback path) — acceptable, but check flox availability.")
            return 0
        print("FAIL — /floxify did not invoke scripts/verify.py in Phase 3c.")
        return 1
    except subprocess.TimeoutExpired:
        print(f"FAIL — timed out after {timeout}s", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skill-dir", default=str(DEFAULT_SKILL_DIR))
    ap.add_argument("--fixture", default="node-postgres",
                    help="fixture dir under fixtures/ (default: node-postgres)")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()
    skill_dir = Path(args.skill_dir).resolve()
    if not skill_dir.exists():
        print(f"ERROR: skill-dir not found: {skill_dir}", file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(run(skill_dir, args.fixture, args.model, args.timeout))


if __name__ == "__main__":
    main()
