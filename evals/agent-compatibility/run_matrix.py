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

Exit status. Only two of these can be true of one run at once — something the
cleanup found, and whatever the run itself did — and 3 wins that, because it is
the only outcome here with a security consequence:
    0  everything the run attempted came out green
    1  a cell did not
    2  a bad `--cells`, `--version` or `--timeout` argument
    3  a credential copy, or a container still holding one, survived cleanup
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
# Where OpenCode reads its config, relative to the container HOME. Only an
# OpenCode cell running under `--opencode-model` gets one.
CONTAINER_OPENCODE_CONFIG = ".config/opencode/opencode.json"
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
# Reclaiming a container is two `docker` calls plus an `inspect`, all of which
# talk to a local daemon. Short, because they run in a `finally` too.
KILL_TIMEOUT = 30
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
# `<provider>/<model>`, where the model half may itself contain slashes —
# OpenRouter ids do (`openrouter/z-ai/glm-5.3-flash`). Both halves must be
# non-empty: a bare `glm-5.3-flash` names no provider, and OpenCode answers a
# model it cannot resolve with `UnknownError: Unexpected server error`, which
# is indistinguishable from a provider outage in the transcript.
MODEL_RE = re.compile(r"[^/\s]+/[^\s]+")


def opencode_config(model: str) -> dict:
    """The `opencode.json` an OpenCode cell runs under, for one model.

    Two jobs, and the first is not optional. OpenCode ships a model catalogue
    baked in at build time, and 1.18.8 — the version these images pin — stops
    at `z-ai/glm-5.2`; asking it for `z-ai/glm-5.3-flash` on the command line
    alone fails with `UnknownError`. Registering the model under its provider
    makes it resolvable. The second job is to name it as the default, so the
    `flox-ai` cell gets the same model as the bare one without depending on
    how `flox-ai launch` forwards flags.
    """
    provider, _, name = model.partition("/")
    return {"$schema": "https://opencode.ai/config.json",
            "provider": {provider: {"models": {name: {}}}},
            "model": model}


def docker_cmd(tag: str, creds_dir: Path, script: Path,
               mount_credentials: bool, agent: str | None = None,
               cidfile: Path | None = None,
               stores: tuple[creds.Store, ...] | None = None,
               config: Path | None = None) -> list[str]:
    """Build the `docker run` line for one cell.

    Credentials mount rw when they mount at all — these are OAuth tokens the
    agent refreshes in place. The prompt and the script mount ro. No API keys,
    ever.

    `mount_credentials` is false for the load check, which is documented as
    credential-free and must not hand a live subscription token to `npx --yes
    skills add` — code fetched from the network at run time, running as root.

    `agent` narrows the mount to that agent's own store. Every trigger
    container used to receive both, so `npx --yes skills add` on a Codex cell
    ran as root with the live Claude subscription token readable beside it for
    no reason any cell needed. The premise for keeping it wide was that
    "nothing prepares an OpenCode credential at all, yet both OpenCode cells
    passed authenticated", so gating would redden them; a binary scan of the
    shipped OpenCode (1.18.23) refutes it — its only credential resolver is
    `$XDG_DATA_HOME/opencode/auth.json`, else
    `~/.local/share/opencode/auth.json`, `claudeAiOauth` appears zero times,
    and its only `/.claude` references are skill discovery. An OpenCode cell
    therefore consumes neither mounted directory today, so gating leaves its
    credential state exactly as it is. `agent=None` mounts every store, which
    is what the load check would do if it mounted anything.

    `cidfile` is how a timed-out container is reclaimed at all — see
    `reclaim_containers`.
    """
    cmd = ["docker", "run", "--rm", "-e", f"HOME={CONTAINER_HOME}"]
    if cidfile is not None:
        cmd += ["--cidfile", str(cidfile)]
    if mount_credentials:
        for store in (creds.active_stores() if stores is None else stores):
            if agent is not None and store.agent != agent:
                continue
            cmd += ["-v", f"{creds_dir}/{store.agent}:"
                          f"{CONTAINER_HOME}/{store.container_dir}:rw"]
    if config is not None:
        # ro, unlike the credential mounts: nothing in the container has any
        # business editing which model the run is measuring.
        cmd += ["-v", f"{config}:{CONTAINER_HOME}/{CONTAINER_OPENCODE_CONFIG}:ro"]
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


def model_flag(cell: Cell, model: str | None) -> str:
    """The ` --model ...` fragment this cell's launch carries, if any.

    Gated on the cell's own agent, not just on the flag: `--opencode-model`
    names a model only OpenCode can resolve, and Claude Code and Codex take
    `--model` with a different vocabulary entirely, so handing it to them
    would turn one flag into three silently different meanings.
    """
    if model is None or cell.agent != "opencode":
        return ""
    return f" --model {model}"


def cell_script(cell: Cell, include_launch: bool,
                model: str | None = None) -> str:
    """Assemble the shell a cell runs inside the container."""
    parts = ["set -euo pipefail", ENV_SHIM]
    if cell.install:
        parts.append(cell.install)
    if include_launch:
        parts.append(_emit(LAUNCH_MARKER))
        parts.append(cell.launch.format(prompt=CONTAINER_PROMPT,
                                        model_flag=model_flag(cell, model)))
    else:
        parts.append(_emit(LIST_MARKER))
        parts.append(cell.list_cmd)
    return "\n".join(parts) + "\n"


def after_marker(text: str, marker: str) -> str:
    """The part of a stream produced after `marker` — nothing earlier."""
    if marker not in text:
        return ""
    return text.split(marker, 1)[1]


def list_output(stdout: str, stderr: str = "") -> str:
    """Both streams of the list command — never the install that preceded it.

    It used to slice stdout alone while recording `stdout + stderr` beside the
    verdict, so a `list_cmd` that renders on stderr produced a `fail` whose own
    evidence field carried the expected token. `_emit` writes each marker to
    both streams precisely so either can be sliced; the trigger half already
    joined them.
    """
    return "\n".join((after_marker(stdout, LIST_MARKER),
                      after_marker(stderr, LIST_MARKER)))


def launch_output(stdout: str, stderr: str) -> str:
    """Both streams of the agent's own run — never the install that preceded it."""
    return "\n".join((after_marker(stdout, LAUNCH_MARKER),
                      after_marker(stderr, LAUNCH_MARKER)))


def _run(cell: Cell, tag: str, work: Path, include_launch: bool,
         timeout: int, model: str | None = None,
         stores: tuple[creds.Store, ...] | None = None
         ) -> subprocess.CompletedProcess:
    script = work / "cell.sh"
    script.write_text(cell_script(cell, include_launch, model))
    # Written beside the script, never inside a credential mount, and only for
    # the agent whose catalogue needs it.
    config = None
    if model is not None and cell.agent == "opencode":
        config = work / "opencode.json"
        config.write_text(json.dumps(opencode_config(model)))
    # Docker refuses to start over a cidfile that already exists, and this
    # path is reused when the trigger half follows the load half.
    cidfile = work / ("trigger.cid" if include_launch else "load.cid")
    cidfile.unlink(missing_ok=True)
    try:
        return subprocess.run(
            docker_cmd(tag, work, script, mount_credentials=include_launch,
                       agent=cell.agent, cidfile=cidfile, stores=stores,
                       config=config if include_launch else None),
            capture_output=True, text=True, timeout=timeout)
    except BaseException:
        # A timeout — or a Ctrl-C — kills the docker CLI and leaves the
        # container running. Reclaim it here rather than at the end of the
        # run: the next cell mounts credentials of its own, and a root
        # container still holding this one's is exactly what must not outlive
        # the cell that started it. `cleanup_run_dir` sweeps again as a
        # backstop, for the paths that never reach this handler.
        reclaim_containers(work)
        raise


def _looks_like_auth_failure(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in AUTH_MARKERS)


def _halves(row: dict, verdict: str) -> tuple[str, str]:
    """Apply `verdict` to the half that hit it, and name the other honestly."""
    if row["load"] == NOT_RUN:
        return verdict, "skipped"
    return row["load"], verdict


def run_cell(cell: Cell, tag: str, work: Path, dry_run: bool = False,
             load_only: bool = False, timeout: int = 600,
             model: str | None = None,
             stores: tuple[creds.Store, ...] | None = None) -> dict:
    """Run one cell. Never raises: every failure becomes a recorded verdict."""
    row = {"cell": cell.id, "agent": cell.agent,
           "install_method": cell.install_method,
           "image": cell.image, "load": NOT_RUN, "trigger": NOT_RUN,
           # WHICH model answered. Empty means the agent's own default — for
           # OpenCode, the no-login provider the shipped build falls back to.
           # Without this the two runs are indistinguishable on disk: same
           # cell id, same `answer-shaped`, one free and unpinnable and one
           # paid and reproducible, and merge-by-cell-id would let either
           # silently stand in for the other.
           "model": model_flag(cell, model).replace(" --model ", ""),
           "evidence_class": "", "load_evidence": "", "trigger_evidence": "",
           "notes": ""}
    if dry_run:
        row["load"] = row["trigger"] = "dry-run"
        # THE PLAN. Five surfaces promise a dry run prints what each cell would
        # run — the module docstring, `--dry-run`'s help, the README, the
        # "Adding a cell" checklist and the retained report — and until
        # `summarize` rendered this field it was computed and thrown away. Both
        # halves, because a full run starts two containers and checking a new
        # cell's shell before spending one is the whole point.
        plan = [("load", cell_script(cell, include_launch=False, model=model))]
        if not load_only:
            plan.append(("trigger",
                         cell_script(cell, include_launch=True, model=model)))
        row["notes"] = " | ".join(
            f"{half}: {script.strip().replace(chr(10), ' ; ')}"
            for half, script in plan)
        return row

    work.mkdir(parents=True, exist_ok=True)
    try:
        # Load — install, then prove the agent application can see the skill.
        # Needs no credentials, so it still answers when the trigger half is
        # blocked, and it runs with none mounted.
        a = _run(cell, tag, work, include_launch=False, timeout=timeout,
                 model=model, stores=stores)
        row["load_evidence"] = (a.stdout + a.stderr)[-2000:]
        listed = list_output(a.stdout, a.stderr)
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
        b = _run(cell, tag, work, include_launch=True, timeout=timeout,
                 model=model, stores=stores)
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
        #
        # And when the LOAD half is the one that timed out, the trigger was
        # never launched — `skipped` is the word this file already owns for
        # that, and recording `timeout` against a container that never started
        # is a verdict about something that did not happen.
        row["load"], row["trigger"] = _halves(row, "timeout")
        row["notes"] = f"exceeded {timeout}s for one container"
    except Exception as exc:  # a broken cell must not take the run down
        row["load"], row["trigger"] = _halves(row, "error")
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


def _container_gone(cid: str) -> bool:
    """Is docker sure this container is no longer running?

    Only an answer docker actually gave counts. A daemon that cannot be
    reached, or a call that hangs, says nothing about whether a container is
    still holding a credential mount — and this is the one alarm in the file
    that must not fail open.
    """
    try:
        proc = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", cid],
            capture_output=True, text=True, timeout=KILL_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return True          # docker knows of no such container
    return proc.stdout.strip() != "true"


def reclaim_containers(root: Path) -> list[str]:
    """Kill every container started under `root` that was not seen to exit.

    `subprocess.run(timeout=)` SIGKILLs the docker CLI — but the container is
    a child of the DAEMON, `--sig-proxy` cannot forward SIGKILL
    (docker/cli#5489), and `--rm` fires only when the container exits. So a
    cell that hit `--timeout` (600s by default, and a hung agent is the
    ordinary reason to hit it) used to leave a root container running with the
    OAuth directory bind-mounted read-write, while the run swept the host
    directory, found it empty, and reported no leak. Nothing in the process
    held a handle by which that container could even be named, which is why
    every `docker run` here now writes a `--cidfile`.

    Returns the ids that are still alive, in the words the leak alarm prints.
    An id docker could not be asked about counts as alive: an unanswered
    question is not an answer.
    """
    alive = []
    for cidfile in sorted(_walk(root)[0]):
        if cidfile.suffix != ".cid":
            continue
        try:
            cid = cidfile.read_text().strip()
        except OSError:
            continue
        if not cid:
            continue
        for argv in (["docker", "kill", cid], ["docker", "rm", "-f", cid]):
            try:
                subprocess.run(argv, capture_output=True, text=True,
                               timeout=KILL_TIMEOUT)
            except (OSError, subprocess.TimeoutExpired):
                break
        if not _container_gone(cid):
            alive.append(f"container {cid} still holds this run's mounts")
    return alive


def _walk(root: Path) -> tuple[list[Path], list[Path]]:
    """Every file under `root`, and every directory that could not be read.

    `Path.rglob` silently skips the contents of a directory it cannot read
    rather than raising: measured on CPython 3.14.4, `rglob("*")` over a tree
    holding a mode-000 subdirectory containing `auth.json` returned the
    directory, nothing inside it, and no exception. Containers write into
    these trees as ROOT and root-owned residue is the expected case here, so
    the credential alarm was failing open in precisely the state it exists
    for. `os.walk(onerror=)` hands back the directory it could not descend,
    and the caller counts that as a positive — the same absence-of-evidence
    rule this runner already applies with `NOT_RUN` versus `dry-run`, and
    `no-output` versus "never launched".
    """
    files: list[Path] = []
    unreadable: list[Path] = []

    def note(exc: OSError) -> None:
        unreadable.append(Path(exc.filename or root))

    for dirpath, _dirnames, filenames in os.walk(root, onerror=note):
        files += [Path(dirpath) / name for name in filenames]
    return files, unreadable


def cleanup_run_dir(run_dir: Path, tag: str | None) -> list[str]:
    """Remove the run directory, including files the container wrote as root.

    Containers run as root and leave root-owned trees (codex plugin caches,
    marketplace clones) inside the mounted per-cell dirs. `shutil.rmtree` with
    ignore_errors then leaves the directory behind silently. The credential
    copies themselves are host-owned and do delete — but "silently leaves
    things behind" is not a property this directory should have, so a
    container does the final sweep as root.

    Returns everything that still holds a credential — surviving files, and
    containers that could not be reclaimed. A non-empty list is an alarm.
    """
    # Before the rmtree, which is what removes the cidfiles.
    alive = reclaim_containers(run_dir)
    shutil.rmtree(run_dir, ignore_errors=True)
    if run_dir.exists() and tag:
        try:
            subprocess.run(
                ["docker", "run", "--rm", "-v", f"{run_dir}:/sweep", tag,
                 "bash", "-c", "rm -rf /sweep/* /sweep/.[!.]* 2>/dev/null || true"],
                capture_output=True, text=True, timeout=SWEEP_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            pass       # fall through to the scan: the alarm matters more
        shutil.rmtree(run_dir, ignore_errors=True)
    if not run_dir.exists():
        return alive
    files, unreadable = _walk(run_dir)
    return (alive
            + [str(p) for p in files if p.name in creds.CREDENTIAL_FILENAMES]
            + [f"{p} could not be read, so it may still hold a credential"
               for p in unreadable])


# Trigger verdicts a run records without having MEASURED anything. `NOT_RUN` is
# the value a row is born with; `not-attempted` is what `--load-only` writes on
# purpose. Neither is evidence about the trigger half, so neither may overwrite
# a verdict some earlier run did measure.
UNMEASURED_TRIGGER = (NOT_RUN, "not-attempted")
# The four fields that carry the trigger half's answer. `model` is one of them
# because it names WHICH model gave that answer: a same-day `--load-only` rerun
# is run without `--opencode-model` and records the empty string — the agent's
# own default, which for OpenCode is the free no-login provider. Keeping the
# measured verdict while letting the new row's `model` through would relabel a
# pinned-model `pass` as one the free provider produced, which is precisely the
# confusion the field exists to prevent.
TRIGGER_FIELDS = ("trigger", "evidence_class", "trigger_evidence", "model")


def merge_row(prior: dict, new: dict) -> dict:
    """`new` wins field by field, except where it measured nothing.

    `--dry-run` is guarded a whole run at a time, one `if` in `main`; this is
    the same rule per FIELD, for the half of a run that deliberately measures
    nothing. A `--load-only` run used to replace the whole row by cell id, so
    it turned an authenticated run's `trigger: pass` / `evidence_class:
    answer-shaped` into `not-attempted` / `""` with the transcript emptied —
    exit 0, no warning. `--version` defaults to today, so the `--load-only`
    run the README recommends as the cheap next step was the run that
    destroyed the evidence it would be checked against.
    """
    if not prior:
        return dict(new)
    merged = dict(new)
    if (new.get("trigger") in UNMEASURED_TRIGGER
            and prior.get("trigger") not in UNMEASURED_TRIGGER):
        for field in TRIGGER_FIELDS:
            if field in prior:
                merged[field] = prior[field]
        note = (f"trigger verdict '{prior.get('trigger')}' kept from an earlier "
                f"run; this one did not attempt it")
        merged["notes"] = f"{merged.get('notes', '')} {note}".strip()
    return merged


def write_results(path: Path, rows: list[dict]) -> None:
    """Merge `rows` into the day's results, keyed by cell id.

    A `--cells` subset run used to rewrite the file wholesale, silently
    discarding every cell it did not run. Within a cell the merge is per
    field — see `merge_row`.

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
        prior = merged.get(row["cell"], {})
        merged[row["cell"]] = merge_row(prior, row)
        if merged[row["cell"]].get("trigger") != row.get("trigger"):
            print(f"note: keeping the measured trigger verdict for "
                  f"{row['cell']} from an earlier run", file=sys.stderr)
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
        if dry_run and r.get("notes"):
            # The plan `run_cell` computed. One line per half.
            lines += [f"    {part}" for part in r["notes"].split(" | ")]
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
    ap.add_argument("--opencode-model", metavar="PROVIDER/MODEL",
                    help="run the OpenCode cells against this model, using the "
                         "OpenRouter key at ~/.env-open-router "
                         "(e.g. openrouter/z-ai/glm-5.3-flash). Off by "
                         "default: without it the OpenCode cells run on the "
                         "no-login provider the shipped build falls back to, "
                         "which costs nothing and answers about half the time")
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
    if args.opencode_model is not None and not MODEL_RE.fullmatch(args.opencode_model):
        # Same class as `--version`: an argument that is wrong here is cheap to
        # reject and expensive to discover from a transcript, where an
        # unresolvable model reads as `UnknownError` and looks like an outage.
        print(f"--opencode-model must match {MODEL_RE.pattern} "
              f"(e.g. openrouter/z-ai/glm-5.3-flash)", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        # `--cells` and `--version` are both argument errors here; this was the
        # odd one out. A non-positive budget turns every cell into a `timeout`
        # verdict, indistinguishable in the results file from a real hang.
        print("--timeout must be a positive number of seconds", file=sys.stderr)
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
        except (images.BuildError, OSError) as exc:
            # "docker is not installed" and "the skill did not install" must not
            # be the same number now that the exit status is an interface.
            # `lib/images.py` converts every launch failure into `BuildError`;
            # `OSError` stays here so a future path that forgets cannot turn a
            # missing binary back into exit 1. No run directory exists yet, so
            # returning here cannot skip a leak check.
            print(f"image build failed: {exc}", file=sys.stderr)
            return 5

    # Computed inside the `try` but returned after the `finally`, so that a
    # surviving credential copy — discovered during cleanup, and the one
    # failure mode here with a security consequence — can still change it.
    # One decision, read by `prepare`, the copy loop and `docker_cmd` alike.
    stores = creds.active_stores(opencode=args.opencode_model is not None)

    rc = 0
    run_dir = Path(tempfile.mkdtemp(prefix="agent-compat-"))
    results_file = RESULTS / f"{args.version}.jsonl"
    try:
        started = True
        if not args.dry_run and not args.load_only:
            try:
                creds.prepare(run_dir / "src", stores=stores)
            except creds.CredentialError as exc:
                print(f"credentials unusable: {exc}", file=sys.stderr)
                # ASSIGNED, not returned. `return 5` inside this `try` fixes
                # the return value before the `finally` runs, which made the
                # `rc = 3` below dead on the one path that reaches it most
                # easily: `prepare` writes the Claude copy before it validates
                # the Codex one, so a run that fails validation has already put
                # a credential on disk. The leak alarm has to be able to
                # outrank this.
                rc, started = 5, False
        rows = []
        for cell in selected if started else []:
            work = run_dir / cell.id
            if not args.dry_run:
                for store in stores:
                    target = work / store.agent
                    target.mkdir(parents=True, exist_ok=True)
                    src = run_dir / "src" / store.agent
                    if src.exists():
                        shutil.copytree(src, target, dirs_exist_ok=True)
            row = run_cell(cell, tags.get(cell.image, "dry-run"), work,
                           dry_run=args.dry_run, load_only=args.load_only,
                           timeout=args.timeout, model=args.opencode_model,
                           stores=stores)
            rows.append(row)
            print(f"{cell.id}: load {row['load']} / trigger {row['trigger']}")
            # Written per CELL, not once after the loop. A full authenticated
            # run is sixteen containers over hours, and an interrupt at cell
            # seven used to discard the six already paid for in rate limit.
            # `write_results` merges by cell id, so one row at a time costs a
            # rewrite of a file that holds at most eight.
            if not args.dry_run:
                write_results(results_file, [row])
        if started:
            if args.dry_run:
                # A dry run measured nothing, so it writes nothing. It used to
                # stamp `dry-run` over every cell of an authenticated run from
                # the same day — the data loss the merge rules exist to
                # prevent, arriving through the command the README recommends
                # running first.
                print(f"(dry run: {results_file} not written)")
            print(summarize(rows, load_only=args.load_only, dry_run=args.dry_run))
            if not args.dry_run:
                print(f"results: {results_file}")
                # A dry run measured nothing, so it cannot be red.
                bad = [r for r in rows if not is_green(r, args.load_only)]
                if bad:
                    # `auth-error` is the one failure the runner separates from
                    # a skill failure on purpose, and collapsing it into the
                    # same exit code would undo that at the interface a release
                    # check actually reads.
                    rc = 4 if all(r["trigger"] == "auth-error" for r in bad) else 1
    finally:
        leaked = cleanup_run_dir(run_dir, next(iter(tags.values()), None))
        if leaked:
            # Outranks every other outcome, code 5 included: it is the only one
            # with a security consequence, so a wrapper branching on it must
            # not be able to miss it behind a red cell or a failed start.
            print(f"WARNING: credentials survived cleanup: {leaked}",
                  file=sys.stderr)
            rc = 3
        elif run_dir.exists():
            print(f"note: {run_dir} still holds root-owned container files",
                  file=sys.stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
