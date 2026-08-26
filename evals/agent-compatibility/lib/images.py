"""Build the two matrix images with `flox containerize`.

Every failure leaves this module as `BuildError`, including the ones that
happen before a process exists. `subprocess.run` raises `FileNotFoundError`
when `docker` or `flox` is not installed — before any `CompletedProcess` is
constructed — so the returncode checks below never saw it and the caller,
which catches `BuildError` alone, tracebacked out and exited 1 ("a cell did
not come out green") where the README promises 5 ("the run never started").
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ENVIRONMENTS = Path(__file__).resolve().parent.parent / "environments"


class BuildError(RuntimeError):
    """Raised when `flox containerize` fails, or cannot be started at all."""


def image_tag(name: str, version: str) -> str:
    """Docker reference for image `name`.

    `flox containerize` derives the repository from the ENVIRONMENT name and
    treats `-t` as the tag alone — passing `name:version` there yields the
    invalid reference `agent-compat-base:base:version`. The environments are
    named `agent-compat-base` and `agent-compat-withpkg` (in their
    `.flox/env.json`), so the repository is `agent-compat-<name>` and `-t`
    carries only the version.
    """
    return f"agent-compat-{name}:{version}"


def _run(cmd: list[str], what: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        raise BuildError(f"could not run {what}: {exc}") from exc


def image_exists(tag: str) -> bool:
    """True only when `docker images -q` RAN and printed an id.

    The returncode used to go unread, so a stopped daemon — which exits
    non-zero — was indistinguishable from an absent image and was converted
    into a build attempt that then failed for an unrelated reason.
    """
    proc = _run(["docker", "images", "-q", tag], "docker images")
    return proc.returncode == 0 and bool(proc.stdout.strip())


def build(name: str, version: str, rebuild: bool = False) -> str:
    """Build image `name` unless it already exists. Returns the tag."""
    tag = image_tag(name, version)
    if image_exists(tag) and not rebuild:
        return tag
    cmd = ["flox", "containerize", "-d", str(ENVIRONMENTS / name),
           "--runtime", "docker", "-t", version]
    proc = _run(cmd, "flox containerize")
    if proc.returncode != 0:
        raise BuildError(f"containerize {name} failed: {proc.stderr.strip()}")
    return tag
