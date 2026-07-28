#!/usr/bin/env python3
"""Host-matrix smoke test runner.

One attempt per cell, pass/fail — not a rate. See DESIGN.md.

    python3 run_matrix.py --dry-run          # print the plan, invoke nothing
    python3 run_matrix.py                    # full run (needs credentials)
    python3 run_matrix.py --cells claude-native,codex-npx

Two container facts shape this file, both pinned in PROBE.md:
  * the image's default HOME is /var/empty, so every run sets HOME=/root;
  * `bash -lc '<script>'` is re-quoted by the flox activation entrypoint and
    dies on any `$( )`, so each cell's script is written to a file and
    mounted.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from lib import creds, images
from lib.cells import CELLS, Cell

HERE = Path(__file__).resolve().parent
PROMPT = HERE / "prompts" / "trigger.txt"
RESULTS = HERE / "results"
CONTAINER_PROMPT = "/prompt.txt"
CONTAINER_SCRIPT = "/cell.sh"
CONTAINER_HOME = "/root"
# Precise phrases only. A bare "authentication"/"expired" matched Codex
# answers describing PostgreSQL "trust authentication" and flagged two good
# runs as credential failures — a false negative in the other direction from
# the Tier A false pass.
AUTH_MARKERS = ("/login", "invalid api key", "unauthorized", "not logged in",
                "authentication failed", "token expired", "session expired",
                "please log in", "please run /login", "401 ")


def docker_cmd(cell: Cell, tag: str, creds_dir: Path, script: Path) -> list[str]:
    """Build the `docker run` line for one cell.

    Credentials mount rw — these are OAuth tokens the agent refreshes in
    place. The prompt and the script mount ro. No API keys, ever.
    """
    return [
        "docker", "run", "--rm",
        "-e", f"HOME={CONTAINER_HOME}",
        "-v", f"{creds_dir}/claude:{CONTAINER_HOME}/.claude:rw",
        "-v", f"{creds_dir}/codex:{CONTAINER_HOME}/.codex:rw",
        "-v", f"{PROMPT}:{CONTAINER_PROMPT}:ro",
        "-v", f"{script}:{CONTAINER_SCRIPT}:ro",
        tag, "bash", CONTAINER_SCRIPT,
    ]


# A flox container has no FHS layout, so /usr/bin/env does not exist — but
# npx-installed binaries (skills.sh among them) ship `#!/usr/bin/env node`
# shebangs and die with "bad interpreter". Every ordinary host the skill
# actually ships to has /usr/bin/env, so shimming it here removes a container
# artifact rather than papering over a product defect.
ENV_SHIM = (
    '[ -e /usr/bin/env ] || { mkdir -p /usr/bin && '
    'ln -s "$(command -v env)" /usr/bin/env; }'
)


# Tier A must judge ONLY the list command's output. Judging the whole
# transcript let an installer that merely PRINTS the words "flox" and
# "floxify" (skills.sh renders a picker listing them) satisfy `expect` while
# `claude plugin list` was actually saying "No plugins installed" — a false
# pass. Everything before this marker is discarded before the check.
LIST_MARKER = "@@@FLOX_HOSTMATRIX_LIST@@@"


def cell_script(cell: Cell, include_launch: bool) -> str:
    """Assemble the shell a cell runs inside the container."""
    parts = ["set -euo pipefail", ENV_SHIM]
    if cell.install:
        parts.append(cell.install)
    if include_launch:
        parts.append(cell.launch.format(prompt=CONTAINER_PROMPT))
    else:
        parts.append(f"echo {LIST_MARKER}")
        parts.append(cell.list_cmd)
    return "\n".join(parts) + "\n"


def list_output(stdout: str) -> str:
    """The part of stdout the list command produced — nothing earlier."""
    if LIST_MARKER not in stdout:
        return ""
    return stdout.split(LIST_MARKER, 1)[1]


def _run(cell: Cell, tag: str, creds_dir: Path, work: Path,
         include_launch: bool, timeout: int) -> subprocess.CompletedProcess:
    script = work / "cell.sh"
    script.write_text(cell_script(cell, include_launch))
    return subprocess.run(docker_cmd(cell, tag, creds_dir, script),
                          capture_output=True, text=True, timeout=timeout)


def _looks_like_auth_failure(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in AUTH_MARKERS)


def run_cell(cell: Cell, tag: str, work: Path, dry_run: bool = False,
             tier_a_only: bool = False, timeout: int = 600) -> dict:
    """Run one cell. Never raises: every failure becomes a recorded verdict."""
    row = {"cell": cell.id, "host": cell.host, "method": cell.method,
           "image": cell.image, "tier_a": "dry-run", "tier_b": "dry-run",
           "evidence_class": "", "evidence": "", "notes": ""}
    if dry_run:
        row["notes"] = cell_script(cell, include_launch=False).replace("\n", " ; ")
        return row

    work.mkdir(parents=True, exist_ok=True)
    try:
        # Tier A — install, then prove the host can see the skill. Needs no
        # credentials, so it still answers when Tier B is blocked.
        a = _run(cell, tag, work, work, include_launch=False, timeout=timeout)
        row["evidence"] = (a.stdout or a.stderr)[-2000:]
        listed = list_output(a.stdout)
        row["tier_a"] = "pass" if (a.returncode == 0 and cell.expect in listed) else "fail"

        if row["tier_a"] != "pass":
            row["tier_b"] = "skipped"
            row["notes"] = "tier A did not pass; trigger not attempted"
            return row

        if tier_a_only:
            # Must short-circuit BEFORE the launch: relabelling afterwards
            # would still have spent the call.
            row["tier_b"] = "not-attempted"
            row["notes"] = "--tier-a-only"
            return row

        # Tier B — one prompt, in a fresh container that re-runs the install.
        b = _run(cell, tag, work, work, include_launch=True, timeout=timeout)
        combined = b.stdout + b.stderr
        # A successful exit is never an auth failure, whatever the prose says.
        if b.returncode != 0 and _looks_like_auth_failure(combined):
            row["tier_b"] = "auth-error"
            row["notes"] = "credential problem, not a skill problem"
        elif b.returncode != 0:
            row["tier_b"] = "fail"
        else:
            row["tier_b"] = "pass"
        row["evidence_class"] = classify_trigger(combined)
        if row["tier_b"] == "pass" and row["evidence_class"] != "answer-shaped":
            row["notes"] = (row["notes"] + " " if row["notes"] else "") + \
                f"exit 0 but evidence is '{row['evidence_class']}'"
        row["evidence"] = combined[-4000:]
    except subprocess.TimeoutExpired:
        row["tier_a"], row["tier_b"] = "timeout", "timeout"
        row["notes"] = f"exceeded {timeout}s"
    except Exception as exc:  # a broken cell must not take the run down
        row["tier_a"], row["tier_b"] = "error", "error"
        row["notes"] = f"{type(exc).__name__}: {exc}"
    return row


# Exit 0 does not mean the skill was used. Nothing any of the three hosts
# prints in headless mode enumerates the skills it loaded, so a Tier B pass is
# classified by what the transcript can actually support — never as proof.
NOT_INJECTED = "not the flox-patched build"
# Guidance the skill teaches that an unguided model routinely gets wrong:
# versioned pkg-paths and services wiring rather than bare `python`/`postgres`.
FINGERPRINTS = ("pkg-path", "python312", "postgresql_", "[services]", "flox activate")


def classify_trigger(text: str) -> str:
    """How much does this transcript actually support?"""
    if NOT_INJECTED in text:
        return "not-injected"          # host said outright it ignored the skill
    if not text.strip():
        return "no-output"
    hits = [f for f in FINGERPRINTS if f in text]
    if len(hits) >= 2:
        return "answer-shaped"         # consistent with the skill, not proof of it
    return "weak"                      # ran, but nothing skill-specific surfaced


def write_results(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def summarize(rows: list[dict]) -> str:
    lines = ["", f"{'cell':<20} {'tier A (load)':<14} {'tier B (trigger)':<10} "
             f"{'evidence':<14}", "-" * 62]
    for r in rows:
        lines.append(f"{r['cell']:<20} {r['tier_a']:<14} {r['tier_b']:<10} "
                     f"{r.get('evidence_class', ''):<14}")
    passed = sum(1 for r in rows if r["tier_a"] == "pass" and r["tier_b"] == "pass"
                 and r.get("evidence_class") == "answer-shaped")
    lines += ["-" * 62,
              f"{passed}/{len(rows)} cells green with answer-shaped evidence",
              "(no host enumerates loaded skills headlessly, so none of this is",
              " proof of invocation — see DESIGN.md)", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print the plan, invoke nothing")
    ap.add_argument("--cells", help="comma-separated cell ids (default: all)")
    ap.add_argument("--rebuild", action="store_true", help="force image rebuild")
    ap.add_argument("--tier-a-only", action="store_true",
                    help="skip the authenticated half (no credentials needed)")
    ap.add_argument("--version", default=datetime.now(timezone.utc).strftime("%Y%m%d"))
    args = ap.parse_args(argv)

    wanted = args.cells.split(",") if args.cells else None
    selected = [c for c in CELLS if wanted is None or c.id in wanted]
    if not selected:
        print("no cells selected", file=sys.stderr)
        return 2

    tags = {}
    if not args.dry_run:
        for name in sorted({c.image for c in selected}):
            tags[name] = images.build(name, args.version, rebuild=args.rebuild)

    run_dir = Path(tempfile.mkdtemp(prefix="hostmatrix-"))
    try:
        if not args.dry_run and not args.tier_a_only:
            creds.prepare(run_dir / "src")
        rows = []
        for cell in selected:
            work = run_dir / cell.id
            if not args.dry_run:
                for host_dir in ("claude", "codex"):
                    target = work / host_dir
                    target.mkdir(parents=True, exist_ok=True)
                    src = run_dir / "src" / host_dir
                    if src.exists():
                        shutil.copytree(src, target, dirs_exist_ok=True)
            row = run_cell(cell, tags.get(cell.image, "dry-run"), work,
                           dry_run=args.dry_run, tier_a_only=args.tier_a_only)
            rows.append(row)
            print(f"{cell.id}: {row['tier_a']} / {row['tier_b']}")
        out = RESULTS / f"{args.version}.jsonl"
        write_results(out, rows)
        print(summarize(rows))
        print(f"results: {out}")
        return 0
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
