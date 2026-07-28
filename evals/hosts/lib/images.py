"""Build the two matrix images with `flox containerize`."""
from __future__ import annotations

import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


class BuildError(RuntimeError):
    """Raised when `flox containerize` fails."""


def image_tag(name: str, version: str) -> str:
    """Docker reference for image `name`.

    `flox containerize` derives the repository from the ENVIRONMENT name and
    treats `-t` as the tag alone — passing `name:version` there yields the
    invalid reference `hosts-base:base:version`. The environments are named
    `hosts-base` and `hosts-withpkg` (see `flox init -n`), so the repository
    is `hosts-<name>` and `-t` carries only the version.
    """
    return f"hosts-{name}:{version}"


def image_exists(tag: str) -> bool:
    proc = subprocess.run(["docker", "images", "-q", tag],
                          capture_output=True, text=True)
    return bool(proc.stdout.strip())


def build(name: str, version: str, rebuild: bool = False) -> str:
    """Build image `name` unless it already exists. Returns the tag."""
    tag = image_tag(name, version)
    if image_exists(tag) and not rebuild:
        return tag
    cmd = ["flox", "containerize", "-d", str(HERE / name),
           "--runtime", "docker", "-t", version]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise BuildError(f"containerize {name} failed: {proc.stderr.strip()}")
    return tag
