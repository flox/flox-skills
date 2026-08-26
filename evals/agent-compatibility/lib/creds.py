"""Prepare a throwaway credential directory for one matrix run.

Only the Claude subscription block travels into a container. The live
~/.claude/.credentials.json also holds mcpOAuth tokens (Fellow, Linear,
Notion, Slack, Sentry); nothing in this matrix needs MCP, so none of them
leave the host. Codex's auth.json is OAuth too (auth_mode "chatgpt") and is
copied whole — it holds nothing but the Codex login.

Every failure here leaves as `CredentialError`, because the caller catches
exactly that one type and turns it into exit 5, "the credentials could not be
read". A `JSONDecodeError` escaping instead exits 1, which means "a cell did
not come out green" — and a half-written `.credentials.json` is a realistic
state on a machine where Claude Code is refreshing tokens concurrently, which
is exactly when someone runs this.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

CLAUDE_SRC = Path.home() / ".claude" / ".credentials.json"
CODEX_SRC = Path.home() / ".codex" / "auth.json"


class CredentialError(RuntimeError):
    """Raised when credentials are missing, malformed, or under-minimized."""


def minimize_claude(raw: dict) -> dict:
    """Return only the subscription block. Everything else is dropped."""
    if "claudeAiOauth" not in raw:
        raise CredentialError(
            "no 'claudeAiOauth' block in Claude credentials — is Claude Code "
            "logged in on this machine?"
        )
    return {"claudeAiOauth": raw["claudeAiOauth"]}


def minimize_codex(raw: dict) -> dict:
    """Drop `OPENAI_API_KEY` from the Codex login.

    `~/.codex/auth.json` carries an `OPENAI_API_KEY` field alongside the
    ChatGPT OAuth tokens. It is null on a subscription login — but if it were
    ever set, copying the file verbatim would hand a per-token-billed API key
    to every container. The matrix runs on OAuth only, so the field never
    travels.
    """
    return {k: v for k, v in raw.items() if k != "OPENAI_API_KEY"}


@dataclass(frozen=True)
class Store:
    """One agent application's login, and every place this suite touches it.

    The runner used to carry this layout in four places — `prepare` here, the
    mount list in `docker_cmd`, the per-cell copy loop in `main`, and the
    filename allowlist the leak scan greps for — so a third credential store
    was four coordinated edits, and the easy one to miss was the scan, whose
    failure mode is exit 0 and silence. This table is the one copy; the other
    three sites read it.
    """
    agent: str                        # the matrix agent whose login this is
    src: Path                         # where it lives on the host
    container_dir: str                # mount point, relative to the container HOME
    filename: str                     # the file inside it
    minimize: Callable[[dict], dict]  # what may travel


STORES: tuple[Store, ...] = (
    Store("claude", CLAUDE_SRC, ".claude", ".credentials.json", minimize_claude),
    Store("codex", CODEX_SRC, ".codex", "auth.json", minimize_codex),
)

# The names the leak scan greps for. Derived, never re-typed.
CREDENTIAL_FILENAMES = frozenset(s.filename for s in STORES)


def _write_600(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh)
    os.chmod(path, 0o600)


def read_json(path: Path) -> dict:
    """Parse `path`, or raise `CredentialError` naming it.

    `json.loads(path.read_text())` raises three different ways that are all
    the same fact to a caller — the credential file could not be read:
    `PermissionError` (an OSError), `UnicodeDecodeError`, and
    `JSONDecodeError`. None of them is `CredentialError`, so each used to
    traceback out of `main` and exit 1.
    """
    try:
        raw = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CredentialError(f"could not read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise CredentialError(
            f"{path} is not a JSON object (got {type(raw).__name__})")
    return raw


def assert_minimized(path: Path) -> None:
    """Fail loudly unless the written Claude file carries exactly one key."""
    keys = list(read_json(path))
    if keys != ["claudeAiOauth"]:
        raise CredentialError(
            f"refusing to mount {path}: expected exactly ['claudeAiOauth'], got {keys}"
        )


def prepare(dest: Path, claude_src: Path = CLAUDE_SRC, codex_src: Path = CODEX_SRC) -> None:
    """Populate `dest` with per-run credential copies, mode 600."""
    overrides = {"claude": claude_src, "codex": codex_src}
    stores = [replace(s, src=overrides.get(s.agent, s.src)) for s in STORES]
    for store in stores:
        if not store.src.exists():
            raise CredentialError(f"missing credential file: {store.src}")
    for store in stores:
        _write_600(dest / store.agent / store.filename,
                   store.minimize(read_json(store.src)))
    assert_minimized(dest / "claude" / ".credentials.json")
    if "OPENAI_API_KEY" in read_json(dest / "codex" / "auth.json"):
        raise CredentialError("refusing to mount a Codex file carrying OPENAI_API_KEY")
