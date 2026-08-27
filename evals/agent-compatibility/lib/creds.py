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
# An OpenRouter key, in dotenv form, for the OpenCode cells. Not a file any
# agent application writes: OpenCode stores its own logins as JSON under
# `~/.local/share/opencode/auth.json`, and this suite deliberately does not
# read a developer's live OpenCode session — it mints a container-only
# credential from a key kept for the purpose. Optional, and off unless
# `--opencode-model` asks for it: without it the OpenCode cells run exactly as
# they always have, on the no-login provider the shipped build falls back to.
OPENROUTER_SRC = Path.home() / ".env-open-router"


class CredentialError(RuntimeError):
    """Raised when credentials are missing, malformed, or under-minimized."""


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


def read_dotenv(path: Path) -> dict:
    """Parse `KEY=VALUE` lines, or raise `CredentialError` naming the file.

    Same contract as `read_json` — every way of failing to read a credential
    source leaves this module as `CredentialError`, so the caller's exit 5
    ("the run never started") stays distinguishable from exit 1 ("a cell did
    not come out green"). A dotenv has no parse errors to speak of, so the
    realistic failures are the OSErrors: absent, unreadable, a directory.
    """
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        raise CredentialError(f"could not read {path}: {exc}") from exc
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("\'\"")
    return out


def minimize_claude(raw: dict) -> dict:
    """Return only the subscription block. Everything else is dropped."""
    if "claudeAiOauth" not in raw:
        raise CredentialError(
            "no 'claudeAiOauth' block in Claude credentials — is Claude Code "
            "logged in on this machine?"
        )
    return {"claudeAiOauth": raw["claudeAiOauth"]}


def minimize_openrouter(raw: dict) -> dict:
    """Turn the dotenv into the one credential OpenCode reads, and nothing else.

    An allowlist like the Claude path rather than a denylist like the Codex
    one: the source is a shell env file, so anything at all could be sitting
    beside the key — and unlike the other two sources, this one was not written
    by an agent application and has no schema to constrain it. Only
    `OPENROUTER_API_KEY` is named, so only `OPENROUTER_API_KEY` can travel.

    The output shape is OpenCode's own, observed against 1.18.8: a provider
    keyed by id, `type` "api", the key under `key`. `opencode auth list` reads
    a file in this shape as "OpenRouter api, 1 credentials" with no environment
    variable set — which is the point. Passing the key as `-e
    OPENROUTER_API_KEY` also works and is how OpenCode documents it, but an env
    var is readable by every process in the container (`/proc/*/environ`) and
    shows up in `docker inspect`, while a mounted file is exactly the shape the
    leak scan, the mode-600 write and the end-of-run sweep already police.
    """
    key = raw.get("OPENROUTER_API_KEY", "")
    if not key:
        raise CredentialError(
            "no non-empty 'OPENROUTER_API_KEY' in the OpenRouter env file")
    return {"openrouter": {"type": "api", "key": key}}


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
    parse: Callable[[Path], dict] = read_json   # how the SOURCE is read
    optional: bool = False            # skipped unless a flag asks for it


STORES: tuple[Store, ...] = (
    Store("claude", CLAUDE_SRC, ".claude", ".credentials.json", minimize_claude),
    Store("codex", CODEX_SRC, ".codex", "auth.json", minimize_codex),
    # Off by default, and that is the whole design. Preparing this store
    # silently whenever the key file happened to exist would change what the
    # two OpenCode cells MEAN — from "the shipped build reaches its no-login
    # provider" to "a paid provider answers" — without anything in the run
    # saying so, which is the class of silent redefinition the merge rules and
    # the `--dry-run` guard in run_matrix.py already exist to prevent.
    Store("opencode", OPENROUTER_SRC, ".local/share/opencode", "auth.json",
          minimize_openrouter, parse=read_dotenv, optional=True),
)

# The names the leak scan greps for. Derived, never re-typed — and derived from
# EVERY store, not the active ones, because a store that ran on the previous
# invocation is exactly the residue the scan exists to find.
CREDENTIAL_FILENAMES = frozenset(s.filename for s in STORES)


def active_stores(opencode: bool = False) -> tuple[Store, ...]:
    """The stores one run uses. Optional ones join only when asked for.

    Threaded from `main` into all three sites that read the layout — `prepare`,
    the per-cell copy loop and `docker_cmd` — so "which credentials does this
    run touch" is one decision made once, rather than three that can disagree.
    """
    return tuple(s for s in STORES if opencode or not s.optional)


def _write_600(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh)
    os.chmod(path, 0o600)


def assert_minimized(path: Path) -> None:
    """Fail loudly unless the written Claude file carries exactly one key."""
    keys = list(read_json(path))
    if keys != ["claudeAiOauth"]:
        raise CredentialError(
            f"refusing to mount {path}: expected exactly ['claudeAiOauth'], got {keys}"
        )


def assert_only_openrouter(path: Path) -> None:
    """Fail loudly unless the written OpenCode file carries exactly one key.

    The same refusal `assert_minimized` performs for Claude, for the same
    reason: `minimize_openrouter` is the only thing standing between a shell
    env file — which may hold anything — and a container, and a guard that
    reads the file actually written is the only one that cannot be skipped by
    a future edit to the minimizer.
    """
    keys = list(read_json(path))
    if keys != ["openrouter"]:
        raise CredentialError(
            f"refusing to mount {path}: expected exactly ['openrouter'], got {keys}")


def prepare(dest: Path, claude_src: Path = CLAUDE_SRC, codex_src: Path = CODEX_SRC,
            openrouter_src: Path = OPENROUTER_SRC,
            stores: tuple[Store, ...] | None = None) -> None:
    """Populate `dest` with per-run credential copies, mode 600.

    `stores` defaults to the always-on pair, so a caller that has not opted
    into an optional store cannot be surprised by one — including every
    existing caller and test.
    """
    overrides = {"claude": claude_src, "codex": codex_src,
                 "opencode": openrouter_src}
    chosen = active_stores() if stores is None else stores
    prepared = [replace(s, src=overrides.get(s.agent, s.src)) for s in chosen]
    for store in prepared:
        if not store.src.exists():
            raise CredentialError(f"missing credential file: {store.src}")
    for store in prepared:
        _write_600(dest / store.agent / store.filename,
                   store.minimize(store.parse(store.src)))
    agents = {s.agent for s in prepared}
    if "claude" in agents:
        assert_minimized(dest / "claude" / ".credentials.json")
    if "codex" in agents and "OPENAI_API_KEY" in read_json(dest / "codex" / "auth.json"):
        raise CredentialError("refusing to mount a Codex file carrying OPENAI_API_KEY")
    if "opencode" in agents:
        assert_only_openrouter(dest / "opencode" / "auth.json")
