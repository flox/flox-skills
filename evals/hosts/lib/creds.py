"""Prepare a throwaway credential directory for one matrix run.

Only the Claude subscription block travels into a container. The live
~/.claude/.credentials.json also holds mcpOAuth tokens (Fellow, Linear,
Notion, Slack, Sentry); nothing in this matrix needs MCP, so none of them
leave the host. Codex's auth.json is OAuth too (auth_mode "chatgpt") and is
copied whole — it holds nothing but the Codex login.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

CLAUDE_SRC = Path.home() / ".claude" / ".credentials.json"
CODEX_SRC = Path.home() / ".codex" / "auth.json"


class CredentialError(RuntimeError):
    """Raised when credentials are missing, malformed, or under-minimized."""


def minimize_claude(raw: dict) -> dict:
    """Return only the subscription block. Everything else is dropped."""
    if "claudeAiOauth" not in raw:
        raise CredentialError(
            "no 'claudeAiOauth' block in Claude credentials — is the host logged in?"
        )
    return {"claudeAiOauth": raw["claudeAiOauth"]}


def _write_600(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh)
    os.chmod(path, 0o600)


def assert_minimized(path: Path) -> None:
    """Fail loudly unless the written Claude file carries exactly one key."""
    keys = list(json.loads(path.read_text()))
    if keys != ["claudeAiOauth"]:
        raise CredentialError(
            f"refusing to mount {path}: expected exactly ['claudeAiOauth'], got {keys}"
        )


def prepare(dest: Path, claude_src: Path = CLAUDE_SRC, codex_src: Path = CODEX_SRC) -> None:
    """Populate `dest` with per-run credential copies, mode 600."""
    for src in (claude_src, codex_src):
        if not src.exists():
            raise CredentialError(f"missing credential file: {src}")
    _write_600(dest / "claude" / ".credentials.json",
               minimize_claude(json.loads(claude_src.read_text())))
    _write_600(dest / "codex" / "auth.json", json.loads(codex_src.read_text()))
    assert_minimized(dest / "claude" / ".credentials.json")
