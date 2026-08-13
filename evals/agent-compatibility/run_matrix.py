#!/usr/bin/env python3
"""Agent installation compatibility smoke-test runner.

One attempt per cell, pass/fail — a load/trigger smoke test, never a trigger
rate. See README.md.

    python3 run_matrix.py --dry-run          # print the plan, invoke nothing
    python3 run_matrix.py                    # full run (needs credentials)
    python3 run_matrix.py --cells claude-native,codex-npx

Two container facts shape this file, both observed against a built image:
  * the image's default HOME is /var/empty, so every run sets HOME=/root;
  * `bash -lc '<script>'` is re-quoted by the flox activation entrypoint and
    dies on any `$( )`, so each cell's script is written to a file and
    mounted.

Exit status, highest number wins when several apply:
    0  everything the run attempted came out green
    1  a cell did not
    2  a bad `--cells` or `--version` argument
    3  a credential copy survived cleanup
    4  every failing cell failed on credentials, not on the skill
    5  the run never started — an image build or credential read failed
"""
from __future__ import annotations

import argparse
import json
import os
import re
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
# the load check's false pass.
AUTH_MARKERS = ("/login", "invalid api key", "unauthorized", "not logged in",
                "authentication failed", "token expired", "session expired",
                "please log in", "please run /login", "401 ")
# Cleanup and image work must not hang a run: the sweep below is what deletes
# the credential copies, and it runs in a `finally`.
SWEEP_TIMEOUT = 120
# The verdict a row carries before either container has answered. Distinct from
# "dry-run", which is a mode a run is deliberately in: the exception handlers
# below use this to tell "the load half never ran" from "the load half ran and
# said something", and one string cannot honestly mean both.
NOT_RUN = "not-run"
# Docker's own tag grammar, which is stricter than "safe path component": a tag
# must start alphanumeric or `_` and is capped at 128 characters. `.` and `-`
# are legal inside but not first, so a bare `.` or `-x` passes a naive
# `[0-9A-Za-z._-]+` and then fails in `docker build` instead.
VERSION_RE = re.compile(r"[0-9A-Za-z_][0-9A-Za-z._-]{0,127}")


def docker_cmd(tag: str, creds_dir: Path, script: Path,
               mount_credentials: bool) -> list[str]:
    """Build the `docker run` line for one cell.

    Credentials mount rw when they mount at all — these are OAuth tokens the
    agent refreshes in place. The prompt and the script mount ro. No API keys,
    ever.

    `mount_credentials` is false for the load check, which is documented as
    credential-free and must not hand a live subscription token to `npx --yes
    skills add` — code fetched from the network at run time, running as root.
    """
    cmd = ["docker", "run", "--rm", "-e", f"HOME={CONTAINER_HOME}"]
    if mount_credentials:
        cmd += ["-v", f"{creds_dir}/claude:{CONTAINER_HOME}/.claude:rw",
                "-v", f"{creds_dir}/codex:{CONTAINER_HOME}/.codex:rw"]
    cmd += ["-v", f"{PROMPT}:{CONTAINER_PROMPT}:ro",
            "-v", f"{script}:{CONTAINER_SCRIPT}:ro",
            tag, "bash", CONTAINER_SCRIPT]
    return cmd


# A flox container has no FHS layout, so /usr/bin/env does not exist — but
# npx-installed binaries (skills.sh among them) ship `#!/usr/bin/env node`
# shebangs and die with "bad interpreter". Every ordinary machine the skill
# actually ships to has /usr/bin/env, so shimming it here removes a container
# artifact rather than papering over a product defect.
ENV_SHIM = (
    '[ -e /usr/bin/env ] || { mkdir -p /usr/bin && '
    'ln -s "$(command -v env)" /usr/bin/env; }'
)


# Every verdict in this file judges the output of ONE step, never the whole
# transcript. Judging everything let an installer that merely PRINTS the words
# "flox" and "floxify" (skills.sh renders a picker listing them) satisfy the
# load check while `claude plugin list` said "No plugins installed".
#
# The same mistake was still live on the trigger half, where the script re-runs
# the install before launching: `codex-native` installs with `git clone`, and a
# failing clone prints "fatal: Authentication failed", which is an AUTH_MARKER.
# A repo or network failure was therefore recorded as "credential problem, not
# a skill problem". Both markers are echoed to stdout AND stderr so each stream
# can be sliced on its own.
LIST_MARKER = "@@@FLOX_AGENT_COMPAT_LIST@@@"
LAUNCH_MARKER = "@@@FLOX_AGENT_COMPAT_LAUNCH@@@"


def _emit(marker: str) -> str:
    return f"echo {marker}; echo {marker} >&2"


def cell_script(cell: Cell, include_launch: bool) -> str:
    """Assemble the shell a cell runs inside the container."""
    parts = ["set -euo pipefail", ENV_SHIM]
    if cell.install:
        parts.append(cell.install)
    if include_launch:
        parts.append(_emit(LAUNCH_MARKER))
        parts.append(cell.launch.format(prompt=CONTAINER_PROMPT))
    else:
        parts.append(_emit(LIST_MARKER))
        parts.append(cell.list_cmd)
    return "\n".join(parts) + "\n"


def after_marker(text: str, marker: str) -> str:
    """The part of a stream produced after `marker` — nothing earlier."""
    if marker not in text:
        return ""
    return text.split(marker, 1)[1]


def list_output(stdout: str) -> str:
    """The part of stdout the list command produced — nothing earlier."""
    return after_marker(stdout, LIST_MARKER)


def launch_output(stdout: str, stderr: str) -> str:
    """Both streams of the agent's own run — never the install that preceded it."""
    return "\n".join((after_marker(stdout, LAUNCH_MARKER),
                      after_marker(stderr, LAUNCH_MARKER)))


def _run(cell: Cell, tag: str, work: Path, include_launch: bool,
         timeout: int) -> subprocess.CompletedProcess:
    script = work / "cell.sh"
    script.write_text(cell_script(cell, include_launch))
    return subprocess.run(
        docker_cmd(tag, work, script, mount_credentials=include_launch),
        capture_output=True, text=True, timeout=timeout)


def _looks_like_auth_failure(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in AUTH_MARKERS)


def run_cell(cell: Cell, tag: str, work: Path, dry_run: bool = False,
             load_only: bool = False, timeout: int = 600) -> dict:
    """Run one cell. Never raises: every failure becomes a recorded verdict."""
    row = {"cell": cell.id, "agent": cell.agent,
           "install_method": cell.install_method,
           "image": cell.image, "load": NOT_RUN, "trigger": NOT_RUN,
           "evidence_class": "", "load_evidence": "", "trigger_evidence": "",
           "notes": ""}
    if dry_run:
        row["load"] = row["trigger"] = "dry-run"
        row["notes"] = cell_script(cell, include_launch=False).replace("\n", " ; ")
        return row

    work.mkdir(parents=True, exist_ok=True)
    try:
        # Load — install, then prove the agent application can see the skill.
        # Needs no credentials, so it still answers when the trigger half is
        # blocked, and it runs with none mounted.
        a = _run(cell, tag, work, include_launch=False, timeout=timeout)
        row["load_evidence"] = (a.stdout + a.stderr)[-2000:]
        listed = list_output(a.stdout)
        row["load"] = "pass" if (a.returncode == 0 and cell.expect in listed) else "fail"

        if row["load"] != "pass":
            row["trigger"] = "skipped"
            row["notes"] = "load did not pass; trigger not attempted"
            return row

        if load_only:
            # Must short-circuit BEFORE the launch: relabelling afterwards
            # would still have spent the call.
            row["trigger"] = "not-attempted"
            row["notes"] = "--load-only"
            return row

        # Trigger — one prompt, in a fresh container that re-runs the install.
        b = _run(cell, tag, work, include_launch=True, timeout=timeout)
        row["trigger_evidence"] = (b.stdout + b.stderr)[-4000:]
        agent_out = launch_output(b.stdout, b.stderr)
        # A successful exit is never an auth failure, whatever the prose says,
        # and only the AGENT's own output can make it one.
        if b.returncode != 0 and _looks_like_auth_failure(agent_out):
            row["trigger"] = "auth-error"
            row["notes"] = "credential problem, not a skill problem"
        elif b.returncode != 0:
            row["trigger"] = "fail"
        else:
            row["trigger"] = "pass"
        row["evidence_class"] = classify_trigger(agent_out)
        if LAUNCH_MARKER not in (b.stdout + b.stderr):
            # `set -euo pipefail` means a failed install exits before the
            # marker, so `no-output` here means "never launched" rather than
            # "launched and said nothing". The class cannot carry both.
            row["notes"] = (row["notes"] + " " if row["notes"] else "") + \
                "the agent never launched; the install step failed first"
        for warning in harness_warnings(agent_out):
            row["notes"] = (row["notes"] + " " if row["notes"] else "") + warning
        if row["trigger"] == "pass" and row["evidence_class"] != "answer-shaped":
            row["notes"] = (row["notes"] + " " if row["notes"] else "") + \
                f"exit 0 but evidence is '{row['evidence_class']}'"
    except subprocess.TimeoutExpired:
        # Only overwrite a verdict that was never reached. The load check runs
        # first and in its own container: when the trigger times out, the load
        # answer is already measured and throwing it away loses the half this
        # suite calls deterministic.
        if row["load"] == NOT_RUN:
            row["load"] = "timeout"
        row["trigger"] = "timeout"
        row["notes"] = f"exceeded {timeout}s for one container"
    except Exception as exc:  # a broken cell must not take the run down
        if row["load"] == NOT_RUN:
            row["load"] = "error"
        row["trigger"] = "error"
        row["notes"] = f"{type(exc).__name__}: {exc}"
    return row


# Exit 0 does not mean the skill was used. None of the three agent
# applications enumerates the skills it loaded in headless mode, so a trigger
# pass is classified by what the transcript can actually support — never as
# proof of invocation.
#
# Guidance the skill teaches that an unguided model routinely gets wrong:
# versioned pkg-paths and services wiring rather than bare `python`/`postgres`.
FINGERPRINTS = ("pkg-path", "python312", "postgresql_", "[services]", "flox activate")

# Noise from the harness, recorded next to the verdict and never allowed to BE
# the verdict. flox-ai prints "not the flox-patched build" against a Flox-
# packaged Codex that is in fact patched: its `codexIsPatched` byte-scans the
# 410-byte Nix wrapper on PATH for CODEX_FLOX_SKILL_ROOTS instead of the ELF
# that wrapper execs, which carries the symbol. Injection is not gated on the
# check — `codexAdapter.Build()` sets the env vars unconditionally and a
# Degraded status only warns — so the warning says nothing about whether the
# skill was injected. It used to be this classifier's highest-priority verdict,
# which permanently excluded a healthy cell from the green count.
HARNESS_WARNINGS = {
    "not the flox-patched build":
        "flox-ai reported an unpatched codex build; this is a known false "
        "alarm (wrapper indirection defeats its byte scan) and does not mean "
        "the skill was not injected — see the README's Harness noise note",
}


def harness_warnings(text: str) -> list[str]:
    """Harness noise worth recording. Never touches `evidence_class`."""
    return [note for marker, note in HARNESS_WARNINGS.items() if marker in text]


def classify_trigger(text: str) -> str:
    """How much does this transcript actually support?"""
    if not text.strip():
        return "no-output"
    hits = [f for f in FINGERPRINTS if f in text]
    if len(hits) >= 2:
        return "answer-shaped"         # consistent with the skill, not proof of it
    return "weak"                      # ran, but nothing skill-specific surfaced


def cleanup_run_dir(run_dir: Path, tag: str | None) -> list[str]:
    """Remove the run directory, including files the container wrote as root.

    Containers run as root and leave root-owned trees (codex plugin caches,
    marketplace clones) inside the mounted per-cell dirs. `shutil.rmtree` with
    ignore_errors then leaves the directory behind silently. The credential
    copies themselves are host-owned and do delete — but "silently leaves
    things behind" is not a property this directory should have, so a
    container does the final sweep as root.

    Returns any credential files that survived; a non-empty list is an alarm.
    """
    shutil.rmtree(run_dir, ignore_errors=True)
    if run_dir.exists() and tag:
        try:
            subprocess.run(
                ["docker", "run", "--rm", "-v", f"{run_dir}:/sweep", tag,
                 "bash", "-c", "rm -rf /sweep/* /sweep/.[!.]* 2>/dev/null || true"],
                capture_output=True, text=True, timeout=SWEEP_TIMEOUT)
        except subprocess.TimeoutExpired:
            pass       # fall through to the scan: the alarm matters more
        shutil.rmtree(run_dir, ignore_errors=True)
    if not run_dir.exists():
        return []
    return [str(p) for p in run_dir.rglob("*")
            if p.name in (".credentials.json", "auth.json")]


def write_results(path: Path, rows: list[dict]) -> None:
    """Merge `rows` into the day's results, keyed by cell id.

    A `--cells` subset run used to rewrite the file wholesale, silently
    discarding every cell it did not run.

    Written 0600 through a temp file: each row carries a transcript tail from a
    session that held live OAuth tokens, and a crash mid-write must not destroy
    both the old file and the new rows.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    merged: dict[str, dict] = {}
    if path.exists():
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                prior = json.loads(line)
                merged[prior["cell"]] = prior
            except (json.JSONDecodeError, KeyError, TypeError):
                print(f"note: ignoring unreadable line {n} in {path}",
                      file=sys.stderr)
    for row in rows:
        merged[row["cell"]] = row
    order = {c.id: i for i, c in enumerate(CELLS)}
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        for cell_id in sorted(merged, key=lambda c: order.get(c, 999)):
            fh.write(json.dumps(merged[cell_id]) + "\n")
    os.replace(tmp, path)


def is_green(row: dict, load_only: bool) -> bool:
    """Did this cell answer the question the run was asking?

    Under `--load-only` the question is the load half alone, so a green cell is
    one whose load passed. Under a full run the trigger must also have passed
    with evidence that supports it — `summarize`'s counter has always required
    `answer-shaped`, and the exit status now agrees with the number it prints.
    """
    if row["load"] != "pass":
        return False
    if load_only:
        return True
    return row["trigger"] == "pass" and row.get("evidence_class") == "answer-shaped"


def summarize(rows: list[dict], load_only: bool = False,
              dry_run: bool = False) -> str:
    lines = ["", f"{'cell':<20} {'load':<14} {'trigger':<14} "
             f"{'evidence':<14}", "-" * 66]
    for r in rows:
        lines.append(f"{r['cell']:<20} {r['load']:<14} {r['trigger']:<14} "
                     f"{r.get('evidence_class', ''):<14}")
    lines.append("-" * 66)
    if dry_run:
        lines.append(f"{len(rows)} cells planned; nothing was run")
    elif load_only:
        passed = sum(1 for r in rows if is_green(r, load_only=True))
        lines.append(f"{passed}/{len(rows)} cells load (trigger not attempted)")
    else:
        passed = sum(1 for r in rows if is_green(r, load_only=False))
        lines += [f"{passed}/{len(rows)} cells green with answer-shaped evidence",
                  "(no agent application enumerates loaded skills headlessly, so none",
                  " of this is proof of invocation, and one attempt per cell is not a",
                  " trigger rate — see README.md)"]
    lines.append("")
    return "\n".join(lines)


def select_cells(wanted: str | None) -> list[Cell]:
    """Resolve `--cells`. An id that matches nothing is an error, not a silent drop.

    A typo beside a valid id used to run a smaller matrix without a word and
    report the smaller denominator as though it were the request — expensive,
    since every cell is a container plus a subscription model call.
    """
    if wanted is None:
        return list(CELLS)
    ids = [c.strip() for c in wanted.split(",") if c.strip()]
    known = {c.id for c in CELLS}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise ValueError(
            f"unknown cell id(s): {', '.join(unknown)}\n"
            f"known cells: {', '.join(c.id for c in CELLS)}")
    if not ids:
        raise ValueError(f"no cells selected\nknown cells: "
                         f"{', '.join(c.id for c in CELLS)}")
    return [c for c in CELLS if c.id in ids]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print the plan, invoke nothing")
    ap.add_argument("--cells", help="comma-separated cell ids (default: all)")
    ap.add_argument("--rebuild", action="store_true", help="force image rebuild")
    ap.add_argument("--load-only", action="store_true",
                    help="load check only; skip the authenticated trigger half "
                         "(no credentials needed)")
    ap.add_argument("--timeout", type=int, default=600,
                    help="seconds per container; a cell runs up to two "
                         "(default: 600)")
    ap.add_argument("--version", default=datetime.now(timezone.utc).strftime("%Y%m%d"),
                    help="run identifier: names results/<version>.jsonl and the "
                         "image tag (default: today, UTC)")
    args = ap.parse_args(argv)

    if not VERSION_RE.fullmatch(args.version):
        # It is both a path component and a Docker tag; a slash escapes
        # results/ and a colon yields the invalid reference `agent-compat-base:
        # base:x` that lib/images.py's docstring exists to warn about.
        print(f"--version must match {VERSION_RE.pattern}", file=sys.stderr)
        return 2
    try:
        selected = select_cells(args.cells)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    tags = {}
    if not args.dry_run:
        try:
            for name in sorted({c.image for c in selected}):
                tags[name] = images.build(name, args.version, rebuild=args.rebuild)
        except images.BuildError as exc:
            # "docker is not installed" and "the skill did not install" must not
            # be the same number now that the exit status is an interface.
            print(f"image build failed: {exc}", file=sys.stderr)
            return 5

    # Computed inside the `try` but returned after the `finally`, so that a
    # surviving credential copy — discovered during cleanup, and the one
    # failure mode here with a security consequence — can still change it.
    rc = 0
    run_dir = Path(tempfile.mkdtemp(prefix="agent-compat-"))
    try:
        if not args.dry_run and not args.load_only:
            try:
                creds.prepare(run_dir / "src")
            except creds.CredentialError as exc:
                print(f"credentials unusable: {exc}", file=sys.stderr)
                return 5
        rows = []
        for cell in selected:
            work = run_dir / cell.id
            if not args.dry_run:
                for agent_dir in ("claude", "codex"):
                    target = work / agent_dir
                    target.mkdir(parents=True, exist_ok=True)
                    src = run_dir / "src" / agent_dir
                    if src.exists():
                        shutil.copytree(src, target, dirs_exist_ok=True)
            row = run_cell(cell, tags.get(cell.image, "dry-run"), work,
                           dry_run=args.dry_run, load_only=args.load_only,
                           timeout=args.timeout)
            rows.append(row)
            print(f"{cell.id}: load {row['load']} / trigger {row['trigger']}")
        # A dry run measured nothing, so it writes nothing. It used to stamp
        # `dry-run` over every cell of an authenticated run from the same day —
        # the data loss the merge-by-cell-id rule exists to prevent, arriving
        # through the command the README recommends running first.
        if args.dry_run:
            print(f"(dry run: {RESULTS / f'{args.version}.jsonl'} not written)")
        else:
            write_results(RESULTS / f"{args.version}.jsonl", rows)
        print(summarize(rows, load_only=args.load_only, dry_run=args.dry_run))
        if not args.dry_run:
            print(f"results: {RESULTS / f'{args.version}.jsonl'}")
        # A dry run measured nothing, so it cannot be red.
        if not args.dry_run:
            bad = [r for r in rows if not is_green(r, args.load_only)]
            if bad:
                # `auth-error` is the one failure the runner separates from a
                # skill failure on purpose, and collapsing it into the same
                # exit code would undo that at the interface a release check
                # actually reads.
                rc = 4 if all(r["trigger"] == "auth-error" for r in bad) else 1
    finally:
        leaked = cleanup_run_dir(run_dir, next(iter(tags.values()), None))
        if leaked:
            # Outranks every other outcome: it is the only one with a security
            # consequence, so a wrapper branching on it must not be able to
            # miss it behind a red cell.
            print(f"WARNING: credential copies survived cleanup: {leaked}",
                  file=sys.stderr)
            rc = 3
        elif run_dir.exists():
            print(f"note: {run_dir} still holds root-owned container files",
                  file=sys.stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
