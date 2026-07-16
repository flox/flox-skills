#!/usr/bin/env python3
"""New-feature conformance eval: does /floxify actually run the analyzer?

Policy (evals/README.md): every skill feature ships with an eval that verifies
the guidance is followed. The feature here is the Phase 1 grounded analyzer
(scripts/detect.py) the skill is told to run via `flox run`. `test_detect.py`
proves the analyzer extracts the right facts; THIS eval proves the *skill*
reaches for it — it runs a real, Phase-1-bounded /floxify against a fixture with
a tool-call-visible stream and asserts a Bash step invoked `detect.py`.

Heavy and opt-in (spawns a real `claude` agent), like the Tier 2 harness — run
manually or on a schedule, never in the fast gate. Bounded to Phase 1 (no flox
init / activate) so it stays cheap.

Usage:
    python3 detect_usage_eval.py                     # default fixture (node-postgres)
    python3 detect_usage_eval.py --fixture ruby
    python3 detect_usage_eval.py --skill-dir /path/to/flox-plugin

Exit 0 if the analyzer was invoked (and, as a bonus signal, run through
`flox run`); exit 1 otherwise. Pure stdlib.
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from run_floxify import DEFAULT_SKILL_DIR, FIXTURES_DIR, MODEL

PHASE1_PROMPT = (
    "/floxify {target}\n\n"
    "Run non-interactively. Do ONLY Phase 1 (run the bundled analyzer, then "
    "scan files) and print the detection summary. Do NOT initialize Flox, "
    "resolve packages, or write a manifest — stop after the Phase 1 detection "
    "summary."
)


def _bash_commands(stream_path):
    """Yield every Bash tool-call command string from a stream-json log."""
    for line in Path(stream_path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = ev.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "tool_use" \
                    and blk.get("name") == "Bash":
                yield blk.get("input", {}).get("command", "")


def run(skill_dir, fixture, model, timeout):
    src = FIXTURES_DIR / fixture
    if not src.exists():
        print(f"ERROR: fixture not found: {src}", file=sys.stderr)
        return 2
    tmp = tempfile.mkdtemp(prefix=f"detect-usage-{fixture}-")
    stream = Path(tmp) / "_stream.jsonl"
    try:
        shutil.copytree(str(src), tmp, dirs_exist_ok=True)
        cmd = [
            "claude", "-p", PHASE1_PROMPT.format(target=tmp),
            "--model", model,
            "--output-format", "stream-json", "--verbose",
            "--allowedTools", "Bash", "Read", "Write", "Edit", "Skill",
            "--plugin-dir", str(skill_dir),
            "--strict-mcp-config",
        ]
        print(f"running Phase-1 /floxify on fixture '{fixture}' ...", flush=True)
        with open(stream, "w", encoding="utf-8") as fh:
            proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.PIPE,
                                  text=True, timeout=timeout)
        if proc.returncode != 0:
            print(f"agent exited {proc.returncode}: {proc.stderr[:300]}",
                  file=sys.stderr)

        commands = list(_bash_commands(stream))
        invoked = [c for c in commands if "detect.py" in c]
        via_flox_run = [c for c in invoked if "flox run" in c]

        print(f"\nBash calls: {len(commands)} · analyzer invocations: "
              f"{len(invoked)} · via `flox run`: {len(via_flox_run)}")
        if invoked:
            print("PASS — /floxify invoked the analyzer:")
            print(f"  {invoked[0][:200]}")
            if not via_flox_run:
                print("  NOTE: analyzer ran but not through `flox run` "
                      "(fallback path) — acceptable, but check flox availability.")
            return 0
        print("FAIL — /floxify did not invoke scripts/detect.py in Phase 1.")
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
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()
    skill_dir = Path(args.skill_dir).resolve()
    if not skill_dir.exists():
        print(f"ERROR: skill-dir not found: {skill_dir}", file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(run(skill_dir, args.fixture, args.model, args.timeout))


if __name__ == "__main__":
    main()
