#!/usr/bin/env python3
"""Flox skills eval harness.

Runs each task in tasks/tasks.jsonl through `claude` headless, in one of two arms:

  --mode skills       skills only, MCP disabled (--strict-mcp-config, no --mcp-config)
  --mode baseline     bare model: no plugin, MCP disabled (unassisted baseline)

Each answer is scored with deterministic hard-checks plus an LLM judge.
Results are written to results/<mode>.json. Pure stdlib (no node/uv needed).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import skill_toml_lint

HERE = Path(__file__).resolve().parent  # evals/flox — this suite's root
REPO_ROOT = HERE.parent.parent
PLUGIN_DIR = REPO_ROOT / "flox-plugin"
MODEL = "claude-opus-4-8"  # pinned for reproducible scores; override with --model

# Setting-source isolation (screening only). When set (e.g. "project,local"),
# each `claude` call is invoked with `--setting-sources <value>`, which drops
# USER-level settings — most importantly `enabledPlugins`. On a machine where
# the Flox plugin is globally enabled in ~/.claude/settings.json (as it is on
# the dev/night-shift hosts), the plain baseline arm would otherwise load that
# plugin and stop being a bare model — the baseline answers "Based on the Flox
# guide" and the discrimination signal collapses to zero. Excluding "user"
# suppresses the global plugin while OAuth credentials (a separate file) still
# load, so `flox run`/`flox activate allow` etc. are only known to the skills
# arm (which re-adds exactly one plugin via --plugin-dir). None = load all
# sources (run.py's original behavior; the gate is unaffected by default).
SETTING_SOURCES = None

ANSWER_SUFFIX = (
    "\n\nProvide the COMPLETE solution as your written answer: the manifest "
    "(in a ```toml code block) and the exact flox commands, with a brief "
    "explanation. Do not execute commands — just give the answer."
)

# Neutral suffix for trigger tests: must NOT mention flox/manifest, so the run
# genuinely tests whether the skill fires on its own (implicit triggering).
NEUTRAL_SUFFIX = (
    "\n\nProvide the complete solution as your written answer (setup steps and "
    "any config). Do not execute commands — just give the answer."
)

# ---- deterministic hard-checks ---------------------------------------------
# Flags hallucinated *Flox* install methods (the ai-13 bug). Only a curl|sh that
# mentions flox counts — a legit `curl … | sh` for some other tool is fine.
FAKE_INSTALL = re.compile(
    r"install\.flox\.dev|flox\.dev/install|curl[^\n]*flox[^\n]*\|\s*(ba)?sh", re.I
)
ABS_PATH = re.compile(r'=\s*"(/home/|/Users/|/usr/local/|/opt/|/root/)', re.I)

# --- hardcoded-secret detection ---------------------------------------------
# Ported from flox/flox-agentic#18 (@imkarrer), rehomed onto this suite's fence
# extraction and tomllib parsing (AI-509 Ticket 6).
#
# The skill's rule is emphatic — SKILL.md "Configuration & Secrets": *never*
# store secrets in the manifest; use environment variables, `~/.config/<env>/`,
# or an existing credentials file. Nothing in the suite exercised it. A
# secret-NAMED key assigned a real literal inside a fenced manifest is a leak,
# as is a connection URL with an inline credential under ANY key name; env
# references (`$VAR`, `${VAR}`, `$(...)`), placeholders, and values that merely
# NAME or POINT AT a secret are not.
#
# Two views of the same fenced blocks are scanned, because neither alone is
# enough and the union is what the check owes the gate:
#
#   text   — `SECRET_ASSIGN` over the raw block. Reaches leaks the parsed view
#            structurally cannot: a block that is not valid TOML (dropped
#            wholesale by `_parsed_manifests`), and an assignment written inside
#            a `[hook] on-activate = '''…'''` shell body, which tomllib sees as
#            one opaque string under a non-secret key.
#   parsed — `_secret_leaks_in` over the tomllib dict. Reaches shapes no
#            single-line regex does: multi-line arrays, nested tables, dotted
#            keys.
#
# Either view finding a leak fails the check. Known limits, inherent to
# name-based detection and pinned by a test: a secret under a NON-secret-named
# key is not caught (except the connection-URL form below, which is caught by
# value shape), an unquoted/bare value is not inspected, and a real value that
# happens to *begin* with a placeholder token ("example…") reads as one.
_SECRET_NAME = (
    r"(?:SECRET|TOKEN|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL|API[_-]?KEY|"
    r"ACCESS[_-]?KEY|PRIVATE[_-]?KEY)"
)
# The same name test applied to a parsed key (which carries no quoting).
SECRET_KEY = re.compile(_SECRET_NAME, re.I)
# Matches a secret-named key (optionally quoted, at a line start or inside an
# inline table / after a comma) and captures its value token: an array, a
# triple-quoted string, or a single-/double-quoted string. The quote-specific
# alternatives let a value contain the *other* quote char.
#
# The name test applies to the key's LAST dotted segment, with any number of
# dotted parents allowed in front (`vars.API_KEY` is a secret assignment). The
# runs either side of the name deliberately do not span `.`: when they did,
# `[install]` + `vault-token.pkg-path = "vault"` read as a secret-named key,
# because the prefix run swallowed `vault-token.` and matched TOKEN inside it.
SECRET_ASSIGN = re.compile(
    r"(?im)(?:^|[{,])[ \t]*(?:export[ \t]+)?"
    r"[\"']?(?:[\w-]+\.)*[\w-]*" + _SECRET_NAME + r"[\w-]*[\"']?[ \t]*=[ \t]*"
    r"(\[[^\]\n]*\]|\"\"\".*?\"\"\"|'''.*?'''|\"[^\"\n]*\"|'[^'\n]*')"
)

# --- what a value IS, not what it starts with --------------------------------
# A secret-named key is only a leak when its value HOLDS a credential. It does
# not when the value merely names, points at, or stands in for one — and those
# are exactly what a *good* answer to `env-secrets-api-key` writes, so each
# false positive here reddens a correct answer on a gate-binding task.
#
# This is a value-SHAPE test, not a first-token test. The predecessor anchored
# every allowance at `^`, which flagged `AUTH_TOKEN = "Bearer $TOKEN"`,
# `API_KEY = "sk-${SUFFIX}"`, `TOKEN_FILE = "secrets/token"`,
# `PASSWORD_FILE = ".env"`, `API_KEY = "op://vault/item/field"`,
# `PASSWORD_COMMAND = "pass show api"` and `API_KEY_ENV = "MY_APP_KEY"`.
#
# An env reference or command substitution ANYWHERE in the value.
_ENV_REF = re.compile(r"\$\{?\w|\$\(")
# A placeholder spelling, still anchored: a value that merely *contains* the
# word "example" can be a real key. The path forms moved to `_PATH_VALUE`.
PLACEHOLDER_VALUE = re.compile(
    r"(?i)^\s*(?:<|\{\{|\*{3,}|x{3,}|changeme|change_me|"
    r"placeholder|your[_-]|example|dummy|redact|todo|fixme|replace|sample|"
    r"fake|none|null)"
)
# The whole value is a filesystem path: `~/…`, `./…`, `../…`, `/…`, a dotfile
# (`.env`), or a bare relative path (`secrets/token`, `keys/id_rsa`). Pointing
# at a credentials file is the fix the skill teaches, not the leak. (An
# absolute path in a manifest is still caught — by `no_abs_paths`, the check
# that owns that question.)
_PATH_VALUE = re.compile(
    r"(?:~|\.{1,2})?/[\w.\-/~]*"      # ~/x  ./x  ../x  /x
    r"|\.[\w.-]+"                     # .env  .envrc
    r"|[\w.-]+(?:/[\w.-]+)+"          # secrets/token  keys/id_rsa
)
# …unless the "path" is a base64 credential wearing a path's clothes. `/` is in
# the base64 alphabet and `+` is not in `[\w.-]`, so every base64 key that
# happens to contain a slash and no plus is exactly the bare-relative-path
# shape: AWS's own documented example secret key,
# `wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY`, read as `secrets/token` did, and
# so did 24% of random 40-char keys of that shape (#84 review). That is a false
# NEGATIVE — it cannot redden a correct answer — but it made this check read as
# covering the most commonly cited secret format when it did not.
#
# The discriminator is what a path is SPELLED with, not how long it is. Every
# path form the skill teaches either anchors (`~/`, `./`, `/`) or separates its
# segments with `.`, `-` or `_` (`secrets/token`, `keys/id_rsa`,
# `~/.config/myapp/secrets`) — none of which a base64 body can contain. So a
# value written in nothing but letters, digits and `/`, long enough to be a
# key and carrying BOTH letter cases and a digit, is credential material.
# `path/to/secrets/token` (one case, no digits) stays a path.
_BASE64_ISH_VALUE = re.compile(r"[A-Za-z0-9/]{20,}")
# The whole value is a URI-style external secret reference: `op://vault/item/
# field`, `vault://…`, `gopass://…`. Naming where a secret lives is not leaking
# it — unless the URI carries the credential inline, which `_URL_CREDENTIALS`
# below decides.
_URI_REF = re.compile(r"(?i)^[a-z][\w.+-]*://")
# The whole value is an environment-variable NAME (`MY_APP_KEY`): uppercase
# segments joined by underscores. A credential of this shape is not something a
# model writes; `AKIAIOSFODNN7REALKEYX` and `ghp_realtoken…` are not it.
_ENV_VAR_NAME = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")
# A PEM body is a credential even though it contains spaces, so it is tested
# before the whitespace rule below.
_PEM_BODY = re.compile(r"-----BEGIN[ \t]")
# A connection URL carrying inline credentials — `postgres://user:hunter2@host`,
# `https://abc:def@sentry.io/1`. This is the one leak shape that is decided by
# VALUE and not by key name: the canonical spelling is `DATABASE_URL`, `DSN` or
# `WEBHOOK_URL`, none of which a name test can reach, and it is what a model
# writes unprompted on `indirect-secrets-no-commit` ("a database password and an
# API token"). The password group is classified by the same value test as any
# other, so `postgres://user:$PGPASSWORD@host` is not a leak. `git@github.com`
# and `host:5432/db` have no `user:password@` authority and do not match.
_URL_CREDENTIALS = re.compile(r"(?i)[a-z][\w.+-]*://([^\s:@/\"']+):([^\s@/\"']*)@")
# Extracts the inner text of each quoted string in a value token (for arrays).
_QUOTED_INNER = re.compile(r"\"([^\"\n]*)\"|'([^'\n]*)'")


def _fenced_manifests(text):
    """Every fenced ```toml block in `text`, in document order.

    Delegates fence handling to `skill_toml_lint.extract_blocks`, which is
    indent- and info-string-aware and unit-tested in this same package. The
    regex this replaced (`` ```(?:toml)?\\n(.*?)``` ``) matched an empty info
    string, so a bare *closing* fence read as an opening one: in an answer
    whose ```bash block preceded its ```toml block, the manifest was silently
    lost. Since ANSWER_SUFFIX asks for the manifest *and* the commands, that
    is the expected shape of a correct answer.
    """
    try:
        return skill_toml_lint.extract_blocks(text, "<answer>")
    except ValueError:
        # A model answer can end mid-fence. Close it and retry rather than
        # dropping every block in the answer.
        try:
            return skill_toml_lint.extract_blocks(text + "\n```\n", "<answer>")
        except ValueError:
            return []


def toml_blocks(text):
    return "\n".join(b.body for b in _fenced_manifests(text))


def _parsed_manifests(text):
    """Each fenced ```toml block parsed with `tomllib`, as a dict.

    Blocks that are not valid TOML are dropped: a check cannot certify a
    manifest flox would refuse to read.
    """
    out = []
    for block in _fenced_manifests(text):
        try:
            out.append(tomllib.loads(block.body))
        except (tomllib.TOMLDecodeError, ValueError):
            continue
    return out


def _looks_like_base64_credential(value):
    """True if `value` is base64 key material rather than a relative path."""
    return bool(
        _BASE64_ISH_VALUE.fullmatch(value)
        and re.search(r"[a-z]", value)
        and re.search(r"[A-Z]", value)
        and re.search(r"\d", value)
    )


def _points_at_a_secret(value):
    """True if `value` NAMES, POINTS AT, or STANDS IN FOR a secret.

    The shapes a correct answer writes: an env reference, a placeholder, a
    path, an external secret-store reference, an env-var name, or a command.
    Anything else under a secret-named key is treated as the credential itself.
    """
    if _ENV_REF.search(value):
        return True
    if PLACEHOLDER_VALUE.match(value):
        return True
    if _PATH_VALUE.fullmatch(value) and not _looks_like_base64_credential(value):
        return True
    if _URI_REF.match(value) and not _URL_CREDENTIALS.search(value):
        return True
    if _ENV_VAR_NAME.fullmatch(value):
        return True
    # A value with whitespace in it is a command or a sentence (`pass show
    # api`), not an opaque credential. A credential that contains a space
    # (other than a PEM body, tested by the caller) is a shape no model writes.
    return bool(re.search(r"\s", value))


def _is_real_secret_value(value):
    """True if a string value holds a credential rather than pointing at one."""
    value = value.strip()
    if not value:
        return False
    if _PEM_BODY.search(value):
        return True
    return not _points_at_a_secret(value)


def _has_url_credentials(text):
    """True if `text` contains a URL with a real credential in its authority.

    `postgres://user:hunter2@localhost:5432/mydb` is a leak wherever it is
    written and whatever key it is written under; the same URL with
    `$PGPASSWORD` in the password position is not.
    """
    return any(
        _is_real_secret_value(m.group(2)) for m in _URL_CREDENTIALS.finditer(text)
    )


def _real_literal(token):
    """True if a captured value token holds at least one real (non-empty,
    non-placeholder) literal. `token` is an array or a quoted string."""
    if token.startswith("["):
        values = [dq or sq for dq, sq in _QUOTED_INNER.findall(token)]
    else:
        values = [token.strip("\"'")]
    return any(_is_real_secret_value(v) for v in values)


def has_hardcoded_secret(text):
    """True if manifest *text* leaks a credential, by name or by value shape.

    Two `finditer` passes over the text, neither of which slices or rescans it:
    `SECRET_ASSIGN` captures each secret-named key's value token (classified by
    `_real_literal` in time proportional to that token's own small length), and
    `_URL_CREDENTIALS` captures any inline `user:password@` authority whatever
    key it sits under.

    Not O(n): both key-name runs flanking `_SECRET_NAME` are an ambiguous
    decomposition of one unbroken `[\\w-]` run, so matching is quadratic in the
    length of any such run that contains a secret-name substring ("TOKEN" * n
    is 4x per doubling). Bounded in practice because real manifest lines break
    those runs with spaces and `=`: a 40k-char manifest of ordinary lines
    measures ~2.5ms.
    """
    return any(
        _real_literal(m.group(1)) for m in SECRET_ASSIGN.finditer(text)
    ) or _has_url_credentials(text)


def _secret_leaks_in(node, key=""):
    """True if a *parsed* manifest leaks a credential.

    Recurses into tables and arrays. `key` is the name the value was assigned
    to, so an inline table (`db = { password = "…" }`) and a nested one
    (`[services.db] password = "…"`) are the same fact. The name context does
    NOT propagate down a table: a secret-named table with innocently-named
    leaves (`[vars.secrets] db = "…"`) is not inspected, deliberately —
    `[install]` keys are package names, so carrying the parent down would read
    `vault-token.pkg-path = "vault"` as a leaked token. An array keeps its
    parent key because a list has no names of its own. Only strings are
    inspected for that name test: an unquoted `API_KEY = 12345678` parses as an
    int, and treating a number as a leaked credential would redden port and
    replica settings. Any string is checked for an inline URL credential, which
    is a leak by value shape under any key name.
    """
    if isinstance(node, dict):
        return any(_secret_leaks_in(v, k) for k, v in node.items())
    if isinstance(node, list):
        return any(_secret_leaks_in(v, key) for v in node)
    if not isinstance(node, str):
        return False
    if _has_url_credentials(node):
        return True
    return bool(SECRET_KEY.search(key)) and _is_real_secret_value(node)


def _hardcodes_secret(answer):
    """The `no_hardcoded_secret` check's negation: text view OR parsed view."""
    return has_hardcoded_secret(toml_blocks(answer)) or any(
        _secret_leaks_in(m) for m in _parsed_manifests(answer)
    )


# `services.auto-start` (AI-503). Two things are checkable and both are things a
# model gets wrong without the skill:
#
#   1. Placement. The key belongs to the `[services]` table itself, alongside the
#      service names — under a `[services.<name>]` block flox rejects it
#      ("unknown field `auto-start`"). A plain string grep would pass that wrong
#      manifest, so _sets_auto_start tracks the enclosing table.
#   2. Schema version. The key was introduced in schema 1.12.0; in a
#      `version = 1` manifest it fails to parse ("invalid type: boolean `true`,
#      expected struct ServiceDescriptor"). An answer that never mentions
#      `schema-version` hands the user a manifest that cannot be loaded.
# Both facts are asserted against the *parsed* manifest rather than its text.
# The line scanner this replaced tracked the enclosing table with a regex and
# had no `'''`/`\"\"\"` state, so it both over- and under-reported: an
# `auto-start = true` line inside a multiline command body counted as a real
# key, and a `[ -d node_modules ] || npm ci` line inside one set the current
# table to `-d node_modules`, hiding a correct key that followed. Asking
# `tomllib` for `services["auto-start"]` makes both impossible.
# Full `X.Y.Z` only — flox matches the value against a literal list, so a
# two-component `"1.12"` is rejected outright (`manifest had invalid schema
# version '1.12'`, verified on flox 1.13.2).
_SCHEMA_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_MIN_AUTO_START_SCHEMA = (1, 12)


def _auto_start_manifests(answer):
    """Parsed manifests that set `auto-start = true` on the `[services]` table.

    Scoped per block (each fenced block is its own manifest) so a `[services]`
    header in one snippet can't vouch for a stray `auto-start` line in another,
    and `[services.<name>] auto-start = true` — which flox rejects with
    ``unknown field `auto-start` `` — lands under the service, not `services`,
    so it correctly does not count.
    """
    return [
        m for m in _parsed_manifests(answer)
        if isinstance(m.get("services"), dict) and m["services"].get("auto-start") is True
    ]


def _sets_auto_start(answer):
    return bool(_auto_start_manifests(answer))


def _schema_at_least(value, minimum):
    """True iff `value` is a version string at or above `minimum` (major, minor)."""
    if not isinstance(value, str):
        return False
    m = _SCHEMA_VERSION.match(value.strip())
    return bool(m) and (int(m.group(1)), int(m.group(2))) >= minimum


def _auto_start_schema_version(answer):
    """True iff the block that carries `auto-start` also carries a new-enough schema.

    All three facts are asserted against the *same* manifest. Searching the
    whole answer certified manifests it never inspected: an answer whose prose
    said `schema-version = "1.12.0"` while its only fenced manifest kept
    `version = 1` passed every check in the task, and that manifest does not
    load (``invalid type: boolean `true`, expected struct ServiceDescriptor``)
    — which is the exact RED failure this task exists to catch.
    """
    return any(
        _schema_at_least(m.get("schema-version"), _MIN_AUTO_START_SCHEMA)
        # `version` and `schema-version` are mutually exclusive in flox; a
        # surviving `version = 1` line means the manifest is still rejected.
        and "version" not in m
        for m in _auto_start_manifests(answer)
    )


# Build sandbox modes (AI-503, second half). `sandbox = "warn"|"enforce"` and
# `sandbox-allow` all arrived with schema 1.13.0, so an answer that uses them
# under `version = 1` hands the user a manifest that will not load:
# ``unknown variant `warn`, expected `off` or `pure` ``. Same shape as the
# auto-start pair: placement, then the version line that makes it parse.
_MIN_SANDBOX_MODE_SCHEMA = (1, 13)
_GATED_SANDBOX_MODES = {"warn", "enforce"}


def _sandbox_mode_manifests(answer):
    """Parsed manifests using a 1.13.0-gated build sandbox field."""
    out = []
    for m in _parsed_manifests(answer):
        builds = m.get("build")
        if not isinstance(builds, dict):
            continue
        for descriptor in builds.values():
            if isinstance(descriptor, dict) and (
                descriptor.get("sandbox") in _GATED_SANDBOX_MODES
                or "sandbox-allow" in descriptor
            ):
                out.append(m)
                break
    return out


def _sets_sandbox_mode(answer):
    return bool(_sandbox_mode_manifests(answer))


def _sandbox_schema_version(answer):
    """True iff the block using the gated sandbox field also declares schema 1.13.0+."""
    return any(
        _schema_at_least(m.get("schema-version"), _MIN_SANDBOX_MODE_SCHEMA)
        and "version" not in m
        for m in _sandbox_mode_manifests(answer)
    )


# --- CI activation shape (AI-511) -------------------------------------------
# Two questions about a generated GitHub Actions workflow: did the answer get
# INTO the environment by a sanctioned route, and does any single step re-enter
# it once per command (slow, and each line loses the state the previous one
# set).
#
# Both are answered against parsed *steps*, not by grepping fence text. The
# first cut of these checks grepped, and PR #100's review showed what that
# costs: a commented-out `# uses: flox/activate-action` greened a broken
# answer, while a quoted `shell:` scalar and the plain `run: flox activate --`
# form both redded correct ones.
#
# `evals/README.md` ("Designing a check") governs the shape:
#
#   - "Prefer positive `must_match` ... Assert the correct construction rather
#     than detecting the wrong one."
#   - "A correct answer often illustrates the anti-pattern as a labeled
#     counter-example, so a negative or proximity check false-fires on good
#     answers."
#
# The second rule is not hypothetical here: `references/ci.md` teaches the
# model to show the per-line form under a `# WRONG` heading, so the skill
# actively trains answers that a naive negative check would fail. Hence
# `_is_counter_example`, and hence the negative check additionally requiring
# the correct construction to be present.

_YAML_INFO = {"yaml", "yml", ""}
# Deliberately looser than skill_toml_lint's fence regex, which anchors the
# info string to end-of-line: a model writes ```yaml title="ci.yml" often
# enough that treating it as "no fence here" silently drops the whole answer.
_FENCE_LINE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>```+)(?P<info>[^\s`]*)")
_FULL_LINE_COMMENT = re.compile(r"^[ \t]*#")
# Markers a document uses to label a block as the thing NOT to do.
_COUNTER_EXAMPLE = re.compile(
    r"\bWRONG\b|\bBAD\b|\bavoid\b|\banti-?pattern\b|\bdo\s*n[o']?t\b|\bnever\b|"
    r"\binstead\s+of\b|\bbroken\b|❌", re.I
)


def _yaml_blocks(answer):
    """Yield (body, is_counter_example) for every YAML-ish fenced block.

    Handles the four shapes the regex this replaced returned nothing for:
    CRLF answers, ```yml, an attributed info string, and an answer that ends
    mid-fence (taken as running to the end rather than dropped, the same
    choice `_fenced_manifests` documents for the TOML path).
    """
    lines = answer.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    while i < len(lines):
        m = _FENCE_LINE.match(lines[i])
        if not m or m.group("info").lower() not in _YAML_INFO:
            i += 1
            continue
        indent, fence = m.group("indent"), m.group("fence")
        # Prose immediately above the fence is where a counter-example is
        # usually labelled ("Do NOT do this:"), so it is part of the verdict.
        preamble = "\n".join(lines[max(0, i - 3):i])
        body, i = [], i + 1
        while i < len(lines):
            close = _FENCE_LINE.match(lines[i])
            if close and close.group("info") == "" and len(close.group("fence")) >= len(fence):
                break
            body.append(lines[i][len(indent):] if indent and lines[i].startswith(indent)
                        else lines[i])
            i += 1
        text = "\n".join(body)
        comments = "\n".join(ln for ln in body if _FULL_LINE_COMMENT.match(ln))
        yield text, bool(_COUNTER_EXAMPLE.search(preamble + "\n" + comments))
        i += 1


_STEP_DASH = re.compile(r"^(?P<indent>[ \t]*)-[ \t]+(?=\S)")
_KEY = re.compile(r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z_][\w-]*):[ \t]*(?P<val>.*)$")
_BLOCK_SCALAR = re.compile(r"^[|>][-+]?[0-9]*[ \t]*$")


def _unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


class _Step:
    """One YAML list item, reduced to what these checks ask about."""

    def __init__(self):
        self.keys = {}       # step key -> scalar value, comments excluded
        self.run = []        # `run:` body lines, comments excluded


def _parse_steps(block):
    """Every list item in `block` that carries step keys, comments stripped.

    A deliberate half-parser: enough structure to tell a step key from a
    lookalike inside a comment or a shell line, without a YAML dependency the
    stdlib-only harness does not have.
    """
    lines = [ln for ln in block.split("\n") if not _FULL_LINE_COMMENT.match(ln)]
    steps, cur, cur_indent = [], None, None
    i = 0
    while i < len(lines):
        ln = lines[i]
        dash = _STEP_DASH.match(ln)
        if dash:
            cur = _Step()
            steps.append(cur)
            cur_indent = len(dash.group("indent")) + len(dash.group(0)) - len(dash.group("indent"))
            ln = " " * len(dash.group(0)) + ln[len(dash.group(0)):]
        if cur is None:
            i += 1
            continue
        km = _KEY.match(ln)
        if km and len(km.group("indent")) >= cur_indent:
            key, val = km.group("key"), km.group("val").strip()
            if key == "run" and _BLOCK_SCALAR.match(val):
                body_indent = len(km.group("indent"))
                i += 1
                while i < len(lines):
                    b = lines[i]
                    if b.strip() and (len(b) - len(b.lstrip())) <= body_indent:
                        break
                    cur.run.append(b)
                    i += 1
                continue
            if key == "run":
                cur.run.append(val)
            else:
                cur.keys[key] = _unquote(val)
        i += 1
    return steps


_FLOX_ACTIVATE = re.compile(r"\bflox\s+activate\b")
_ACTIVATE_ACTION = re.compile(r"\bflox/activate-action\b")
# Job-level `defaults: run: shell:`, which ci.md recommends for a job whose
# steps mostly need the environment. Not a step, so _parse_steps never sees it.
_DEFAULTS_SHELL = re.compile(
    r"^[ \t]*defaults:[ \t]*$\n(?:^[ \t]*$\n)*^[ \t]*run:[ \t]*$\n(?:^[ \t]*$\n)*"
    r"^[ \t]*shell:[ \t]*(?P<val>.+)$",
    re.M,
)


def _count_activations(run_lines):
    """How many times a `run:` body enters the environment.

    Counts occurrences rather than matching line starts: `cd api && flox
    activate --`, `time flox activate --`, `env CI=1 flox activate --`, and two
    activations chained with `&&` on one line are all the violation, and a
    `^\\s*flox` anchor counts none of them.
    """
    return sum(len(_FLOX_ACTIVATE.findall(ln)) for ln in run_lines)


def _uses_activate_mechanism(answer):
    """True iff some step actually wires up an activation.

    Anchored to step keys, so a mechanism named in prose or commented out
    inside the fence does not count. Existential by nature: the question is
    whether the answer reached for a sanctioned route at all. It does not
    assert that EVERY step needing the environment activates, because nothing
    here can tell which steps need it.
    """
    for block, _ in _yaml_blocks(answer):
        no_comments = "\n".join(
            ln for ln in block.split("\n") if not _FULL_LINE_COMMENT.match(ln)
        )
        m = _DEFAULTS_SHELL.search(no_comments)
        if m and _FLOX_ACTIVATE.search(_unquote(m.group("val"))):
            return True
        for st in _parse_steps(block):
            if _ACTIVATE_ACTION.search(st.keys.get("uses", "")):
                return True
            if _FLOX_ACTIVATE.search(st.keys.get("shell", "")):
                return True
            # The once-per-script form: a step whose run: enters the
            # environment exactly once (`run: flox activate -- pytest`).
            if _count_activations(st.run) == 1:
                return True
    return False


def _repeats_activate(answer):
    """True iff a step's `run:` enters the environment more than once.

    Blocks labelled as counter-examples are skipped: `ci.md` teaches the model
    to show this exact shape under `# WRONG`, so counting it would fail the
    answers the skill is trying to produce.
    """
    return any(
        _count_activations(st.run) > 1
        for block, is_counter in _yaml_blocks(answer)
        if not is_counter
        for st in _parse_steps(block)
    )


# --- unverifiable version pins ------------------------------------------------
# SKILL.md's `[install]` `version` ladder puts a semver range LAST, behind a
# versioned pkg-path and a literal pin. The reason is not style: a range names
# a constraint rather than a catalog version, so `verify.py` cannot tell which
# version applies and records the entry as unchecked rather than confirmed.
#
# The gate mirrors `verify.py`'s `_is_version_literal` deliberately, so this
# check and the verifier agree on what "unverifiable" means. It asks "is this a
# literal", NOT "is this a range" -- those are not complements, and the set of
# specs flox's resolver accepts is defined server-side. A `v`-prefixed semver
# and an `x`/`X` wildcard segment are alphanumeric but are range syntax, so
# both are excluded.
_VERSION_LITERAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_VERSION_RANGE_SHAPES_RE = re.compile(r"^[vV]\d|^[xX]$|\.[xX](\.|$)")


def _is_version_literal(v):
    """A `version` this suite could look up in the catalog's version list."""
    return bool(_VERSION_LITERAL_RE.match(v)) and not _VERSION_RANGE_SHAPES_RE.search(v)


def _no_range_version_pin(answer):
    """No `[install]` entry pins a version this suite cannot resolve.

    An ABSENT or empty `version` passes: flox treats it as unconstrained, and
    an unpinned versioned pkg-path is the ladder's FIRST rung, not a failure.
    Only a present, non-literal spec fails.
    """
    for manifest in _parsed_manifests(answer):
        install = manifest.get("install")
        if not isinstance(install, dict):
            continue
        for entry in install.values():
            if not isinstance(entry, dict):
                continue
            version = entry.get("version")
            if not isinstance(version, str) or not version:
                continue
            if not _is_version_literal(version):
                return False
    return True


CHECKS = {
    "no_fake_install_url": lambda a: not FAKE_INSTALL.search(a),
    "no_abs_paths": lambda a: not ABS_PATH.search(toml_blocks(a)),
    # Every `version` pin is a literal the catalog can be asked about — a
    # semver range is the ladder's last rung and is not verifiable.
    "no_range_version_pin": _no_range_version_pin,
    # No secret hardcoded into the manifest — secrets belong in env vars,
    # `~/.config/<env_name>/`, or an existing credentials file, never in the
    # committed manifest (SKILL.md "Configuration & Secrets").
    "no_hardcoded_secret": lambda a: not _hardcodes_secret(a),
    "has_install_section": lambda a: "[install]" in a,
    "has_services_section": lambda a: "[services" in a,
    "has_build_section": lambda a: "[build" in a,
    "mentions_containerize": lambda a: "flox containerize" in a,
    "uses_flox_publish": lambda a: "flox publish" in a,
    "uses_include_or_layer": lambda a: "[include]" in a or "flox activate -r" in a,
    "uses_search_show": lambda a: "flox search" in a or "flox show" in a,
    "sets_services_auto_start": _sets_auto_start,
    "auto_start_schema_version": _auto_start_schema_version,
    "sets_build_sandbox_mode": _sets_sandbox_mode,
    "build_sandbox_schema_version": _sandbox_schema_version,
    "uses_remote_env": lambda a: "flox push" in a or "flox pull" in a or "flox activate -r" in a,
    # AI-511: one activation per step, and an actual activation mechanism —
    # `install-flox-action` on its own does not activate anything.
    # An answer that shows the per-line form must also show a sanctioned one:
    # correct answers illustrate the anti-pattern, broken ones only commit it.
    "ci_no_repeated_activate": lambda a: (
        not _repeats_activate(a) or _uses_activate_mechanism(a)
    ),
    "ci_uses_activate_mechanism": _uses_activate_mechanism,
    # Implicit-trigger check: did the skill fire and produce Flox guidance even
    # though the prompt never said "flox"?
    "invokes_flox": lambda a: bool(
        re.search(r"\bflox\b", a, re.I)
        and (re.search(r"flox (init|install|search|show|containerize|publish|build|activate|push|edit)", a)
             or "[install]" in a or "manifest.toml" in a)),
}


# --- cost accounting (AI-459) ----------------------------------------------
# `claude -p --output-format json` returns total_cost_usd + usage on EVERY
# call. This harness read only `.result` and dropped the rest, so we could not
# answer "what does a run cost?" from our own data — while spending it on every
# PR. Measured: one agent call on a real task is $1.27 (18.6k output, ~957k
# cache-read, 406s); a trivial 4-token reply is still $0.088. At 27 tasks x
# (agent + judge) that is ~$40/run, which is why CI is defunded until this is
# visible.

def _parse_meta(envelope):
    """Extract cost/usage from a claude JSON envelope. Never raises — a
    cost-accounting detail must not be able to break an eval run."""
    try:
        cost = float(envelope.get("total_cost_usd") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    usage = envelope.get("usage")
    try:
        duration = int(envelope.get("duration_ms") or 0)
    except (TypeError, ValueError):
        duration = 0
    return {
        "cost_usd": cost,
        "usage": usage if isinstance(usage, dict) else {},
        "duration_ms": duration,
    }


ZERO_META = {"cost_usd": 0.0, "usage": {}, "duration_ms": 0}


def _cost_summary(results):
    """Roll per-task cost into a run total, split agent vs judge.

    The judge split matters: it is half of every run's calls, pinned to the
    same frontier model as the agent, for a grading job AI-451 shows it does
    badly. That trade is invisible without this number.
    """
    costed = [r["cost"] for r in results if "cost" in r]
    agent = sum(c.get("agent_usd", 0.0) for c in costed)
    judge_total = sum(c.get("judge_usd", 0.0) for c in costed)
    total = sum(c.get("total_usd", 0.0) for c in costed)
    return {
        "total_usd": round(total, 4),
        "agent_usd": round(agent, 4),
        "judge_usd": round(judge_total, 4),
        "mean_per_task_usd": round(total / len(costed), 4) if costed else 0.0,
        "n_costed_tasks": len(costed),
    }


def run_claude(prompt, mode, allow_tools, timeout=420, retries=3):
    cmd = ["claude", "-p", prompt, "--model", MODEL, "--output-format", "json"]
    if allow_tools:
        cmd += ["--allowedTools", *allow_tools]
    # Isolate from globally-enabled plugins so the baseline stays a bare model
    # (see SETTING_SOURCES above). Applies to every arm — baseline, skills, and
    # judge — so the only Flox context in the skills arm is the --plugin-dir one.
    if SETTING_SOURCES:
        cmd += ["--setting-sources", SETTING_SOURCES]
    if mode == "skills":
        cmd += ["--plugin-dir", str(PLUGIN_DIR), "--strict-mcp-config"]
    elif mode == "baseline":
        # Bare model: no plugin loaded, MCP disabled. Measures the unassisted baseline.
        cmd += ["--strict-mcp-config"]
    elif mode == "judge":
        cmd += ["--strict-mcp-config"]
    last = "unknown"
    for attempt in range(retries):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            last = "TIMEOUT"
        else:
            if out.returncode != 0:
                last = f"EXIT {out.returncode}: {out.stderr[:300]}"
            else:
                try:
                    envelope = json.loads(out.stdout)
                except json.JSONDecodeError:
                    last = f"BAD JSON: {out.stdout[:300]}"
                else:
                    return (
                        envelope.get("result", ""),
                        None,
                        _parse_meta(envelope),
                    )
        # transient (rate limit / overload / blip) -> backoff and retry
        if attempt < retries - 1:
            time.sleep(2 + attempt * attempt * 3)
    # A failed call may still have burned tokens, but the envelope is gone.
    # Return a zeroed meta so callers can sum unconditionally.
    return None, last, dict(ZERO_META)


def judge(task, answer):
    prompt = (
        "You are grading an AI assistant's answer about the Flox package "
        "manager. Be strict and concrete.\n\n"
        f"TASK: {task['prompt']}\n\nRUBRIC: {task['rubric']}\n\n"
        f"ANSWER:\n{answer}\n\n"
        'Return ONLY a JSON object: {"score": <int 1-5>, "correct": <true|false>, '
        '"issues": [<short strings>]}'
    )
    result, err, meta = run_claude(prompt, "judge", allow_tools=None)
    if err:
        return {"score": 0, "correct": False,
                "issues": [f"judge error: {err}"]}, meta
    raw, m = {}, re.search(r"\{.*\}", result, re.S)
    if m:
        try:
            raw = json.loads(m.group(0))
        except json.JSONDecodeError:
            raw = {"issues": ["judge json parse fail"]}
    else:
        raw = {"issues": ["no json"]}
    # Normalize — the model occasionally omits a key; never let that KeyError later.
    try:
        score = int(raw.get("score", 0) or 0)
    except (TypeError, ValueError):
        score = 0
    return {"score": score, "correct": bool(raw.get("correct", False)),
            "issues": raw.get("issues", [])}, meta


def _run_check(name, answer, task_id):
    """Run one hard-check, converting a crash into a FAIL rather than a lost run.

    Every check here is pure logic over the answer text, but the answer is
    model-written and unbounded: a 600-component dotted key parses fine under
    `tomllib` and then hits Python's recursion limit in `_secret_leaks_in`.
    Unguarded, that exception propagates out of `list(ex.map(...))` in `main`
    and kills the process BEFORE `out_path.write_text` — losing every agent and
    judge call the run had already paid for. A check that cannot answer is
    recorded as a failure of that check, with the reason on stdout.
    """
    try:
        return CHECKS[name](answer)
    except Exception as e:  # noqa: BLE001 — one bad answer must not cost a run
        print(f"    [check error] {task_id}/{name}: {type(e).__name__}: {e}",
              flush=True)
        return False


def process_task(t, mode, allow):
    """Run + score one task (agent call, hard-checks, judge). Thread-safe."""
    suffix = NEUTRAL_SUFFIX if t.get("trigger_test") else ANSWER_SUFFIX
    tier = t.get("tier", "should")
    base = {"id": t["id"], "area": t["area"], "tier": tier,
            "trigger_test": bool(t.get("trigger_test"))}
    answer, err, agent_meta = run_claude(t["prompt"] + suffix, mode, allow)
    if err:
        print(f"    [{tier}] {t['id']}: run error: {err}", flush=True)
        return {**base, "error": err, "cost": {
            "agent_usd": agent_meta["cost_usd"], "judge_usd": 0.0,
            "total_usd": agent_meta["cost_usd"]}}
    hard = {c: _run_check(c, answer, t["id"]) for c in t["checks"]}
    hard_pass = all(hard.values())
    verdict, judge_meta = judge(t, answer)
    cost = {
        "agent_usd": round(agent_meta["cost_usd"], 4),
        "judge_usd": round(judge_meta["cost_usd"], 4),
        "total_usd": round(agent_meta["cost_usd"] + judge_meta["cost_usd"], 4),
    }
    print(f"    [{tier}] {t['id']}: hard={'PASS' if hard_pass else 'FAIL'} "
          f"judge={verdict.get('score')}/5  ${cost['total_usd']:.2f}", flush=True)
    return {**base, "hard_checks": hard, "hard_pass": hard_pass,
            "judge": verdict, "cost": cost,
            "usage": {"agent": agent_meta["usage"], "judge": judge_meta["usage"]},
            "duration_ms": {"agent": agent_meta["duration_ms"],
                            "judge": judge_meta["duration_ms"]},
            "answer_excerpt": answer[:1200]}


def _read_baseline(name):
    """Load a committed baselines/<name> snapshot, or None if absent/bad.

    Comparison points come from baselines/ (committed, versioned evidence)
    and NEVER from results/, which is gitignored generated output. Before
    AI-509 Ticket 3 both lived in results/, so this run's own `--out` file
    was also its comparison target: a second local run silently diffed
    against the first instead of against what is on main.
    """
    try:
        return json.loads((HERE / "baselines" / name).read_text())
    except Exception:
        return None


def build_parser():
    """The CLI parser, extracted so a test can render every help string.

    argparse percent-expands help text lazily, so a bare `%` is only caught
    when the help is *formatted* — `--gate`'s "< 100%)" made the harness die
    on import under Python 3.14 (`ValueError: badly formed help string`) and
    on `--help` under 3.11, which CI pins. Nothing in the suite constructed
    the parser, so no test could have caught it. `test_run.py` now calls
    `format_help()` on this, which covers every help string in the file.
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["skills", "baseline"], default="skills")
    ap.add_argument("--model", default=MODEL,
                    help=f"model id for both agent and judge (default {MODEL})")
    ap.add_argument("--tasks", default=str(HERE / "tasks" / "tasks.jsonl"))
    ap.add_argument("--only", help="run a single task id")
    ap.add_argument("--gate", action="store_true",
                    # `%%` — argparse percent-expands help strings, and a bare
                    # `%)` raises "badly formed help string" on Python 3.14.
                    help="exit non-zero if binding gates fail (functional should-tier < 100%%)")
    ap.add_argument("--plugin-dir", help="override the plugin dir (e.g. a pre-consolidation checkout)")
    ap.add_argument("--out", help="output filename under results/ (default: <mode>.json)")
    ap.add_argument("--concurrency", type=int, default=6,
                    help="parallel claude calls (default 6; lower if you hit rate limits)")
    return ap


def main():
    global MODEL, PLUGIN_DIR
    args = build_parser().parse_args()

    MODEL = args.model
    if args.plugin_dir:
        PLUGIN_DIR = Path(args.plugin_dir).resolve()

    allow = ["Skill", "Read"]

    tasks = [json.loads(l) for l in Path(args.tasks).read_text().splitlines() if l.strip()]
    if args.only:
        tasks = [t for t in tasks if t["id"] == args.only]

    n = min(args.concurrency, len(tasks)) or 1
    print(f"running {len(tasks)} tasks at concurrency {n} ({args.mode}) ...", flush=True)
    with ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(lambda t: process_task(t, args.mode, allow), tasks))

    scored = [r for r in results if "judge" in r]

    def stats(rs):
        n = max(len(rs), 1)
        return {
            "n": len(rs),
            "hard_pass_rate": round(sum(r["hard_pass"] for r in rs) / n, 3),
            "avg_judge_score": round(sum(r["judge"]["score"] for r in rs) / n, 2),
            "judge_correct_rate": round(sum(bool(r["judge"]["correct"]) for r in rs) / n, 3),
        }

    triggers = [r for r in scored if r["trigger_test"]]
    should_triggers = [r for r in triggers if r["tier"] == "should"]
    summary = {
        "mode": args.mode,
        "model": MODEL,
        "n_tasks": len(results),
        "n_errors": sum(1 for r in results if "error" in r),
        **stats(scored),
        "by_tier": {tier: stats([r for r in scored if r["tier"] == tier])
                    for tier in ("should", "may", "stretch") if any(r["tier"] == tier for r in scored)},
        # triggering is probabilistic — measured, not gated
        "n_trigger_tasks": len(triggers),
        "trigger_invokes_flox_rate": round(
            sum(r["hard_checks"].get("invokes_flox", False) for r in triggers) / max(len(triggers), 1), 3),
        "should_trigger_rate": round(
            sum(r["hard_checks"].get("invokes_flox", False) for r in should_triggers) / max(len(should_triggers), 1), 3),
        "cost": _cost_summary(results),
    }
    out = {"summary": summary, "results": results}
    out_path = HERE / "results" / (args.out or f"{args.mode.replace('+', '_')}.json")
    # This arm's committed baseline, for the report's hard-check-flip diff. It
    # lives under baselines/ and this run writes under results/, so the two
    # can no longer be the same file. The cross-arm metrics table reads the
    # other arm's baseline the same way.
    prev_baseline = _read_baseline(out_path.name)
    # results/ is gitignored (AI-509 Ticket 3), so it does not exist on a
    # fresh checkout the way it did when baselines were committed into it.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))
    print(f"written: {out_path}")

    # Deterministic gate status: hard-checks on functional should-tier tasks.
    # The LLM judge's correctness/score is noisy run-to-run and triggering is
    # probabilistic, so both are *reported* but never fail the build — only the
    # structural checks do.
    binding = [r for r in scored if r["tier"] == "should" and not r["trigger_test"]]
    bad = [r for r in binding if not r["hard_pass"]]
    errs = [r for r in results if "error" in r and r.get("tier", "should") == "should"]

    write_step_summary(summary, results, binding, bad, errs, args.gate, prev_baseline)

    if args.gate and (bad or errs):
        print(f"\nGATE FAILED: {len(bad)} functional should-tier task(s) failed hard-checks: "
              f"{[(r['id'], [k for k, v in r['hard_checks'].items() if not v]) for r in bad]}; errors: {[r['id'] for r in errs]}")
        sys.exit(1)
    if args.gate:
        print(f"\nGATE PASSED: all {len(binding)} functional should-tier tasks pass hard-checks. "
              f"(advisory: judge correct {summary['judge_correct_rate']}, avg {summary['avg_judge_score']}, "
              f"should-trigger {summary['should_trigger_rate']}).")


def _diff_vs_baseline(summary, results, prev_baseline):
    """Δ vs the of-record same-arm snapshot: hard-check flips (signal) + judge Δ (advisory)."""
    fname = f"{summary['mode'].replace('+', '_')}.json"
    if not prev_baseline:
        return [f"### Δ vs main (`{fname}`)",
                f"_No committed baseline for this arm — commit `evals/flox/baselines/{fname}` to enable per-PR diffs._", ""]
    prev = {r["id"]: r for r in prev_baseline.get("results", []) if "judge" in r}
    cur = {r["id"]: r for r in results if "judge" in r}
    regressed, fixed = [], []
    for tid in cur.keys() & prev.keys():
        if cur[tid]["hard_pass"] and not prev[tid]["hard_pass"]:
            fixed.append(tid)
        elif not cur[tid]["hard_pass"] and prev[tid]["hard_pass"]:
            regressed.append(f"`{tid}` ({cur[tid]['area']})")
    added = sorted(cur.keys() - prev.keys())
    removed = sorted(prev.keys() - cur.keys())
    ps = prev_baseline.get("summary", {})
    lines = [f"### Hard-check diff vs main (of-record `{fname}`, model `{ps.get('model', '?')}`)"]
    lines.append(f"- ❌ **hard-check regressions ({len(regressed)}):** " + ", ".join(regressed)
                 if regressed else "- ✅ no hard-check regressions")
    if fixed:
        lines.append(f"- ✅ hard-check fixes ({len(fixed)}): " + ", ".join(f"`{t}`" for t in fixed))
    if added:
        lines.append(f"- ➕ new tasks ({len(added)}): " + ", ".join(f"`{t}`" for t in added))
    if removed:
        lines.append(f"- ➖ removed tasks ({len(removed)}): " + ", ".join(f"`{t}`" for t in removed))
    if "avg_judge_score" in ps:
        dj = round(summary["avg_judge_score"] - ps["avg_judge_score"], 2)
        lines.append(f"- judge avg {summary['avg_judge_score']} vs {ps['avg_judge_score']} "
                     f"(Δ {dj:+}) — _advisory, judge is noisy run-to-run_")
    lines.append("")
    return lines


def _metrics_table(summary):
    """Cross-arm metrics: this run (live) for its arm, committed baseline for the other."""
    arms = [("baseline", "baseline.json"), ("skills", "skills.json")]
    summ = {}
    for arm, fn in arms:
        if arm == summary["mode"]:
            summ[arm] = summary
        else:
            g = _read_baseline(fn)
            summ[arm] = g.get("summary") if g else None

    def cell(arm, key, pct):
        s = summ[arm]
        if not s or key not in s:
            return "—"
        return f"{s[key]:.0%}" if pct else f"{s[key]:.2f}"

    def delta(key, pct):
        b, s = summ["baseline"], summ["skills"]
        if not b or not s or key not in b or key not in s:
            return "—"
        d = s[key] - b[key]
        return f"{d * 100:+.1f}pp" if pct else f"{d:+.2f}"

    def hdr(arm):
        return f"**{arm}**" if arm == summary["mode"] else arm

    metrics = [("Hard-pass", "hard_pass_rate", True), ("Avg judge", "avg_judge_score", False),
               ("Judge-correct", "judge_correct_rate", True), ("Should-trigger", "should_trigger_rate", True)]
    rows = [f"| metric | {hdr('baseline')} | {hdr('skills')} | Δ skills−baseline |",
            "|---|--:|--:|--:|"]
    for label, key, pct in metrics:
        rows.append(f"| {label} | {cell('baseline', key, pct)} | {cell('skills', key, pct)} "
                    f"| {delta(key, pct)} |")
    return rows


def write_step_summary(summary, results, binding, bad, errs, gate_enabled, prev_baseline=None):
    """Render a markdown report to $GITHUB_STEP_SUMMARY (the Actions run page)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    scored = [r for r in results if "judge" in r]
    if gate_enabled:
        verdict = "❌ **GATE FAILED**" if (bad or errs) else "✅ **GATE PASSED**"
    else:
        verdict = "ℹ️ measurement run (gate off)"

    cost = summary.get("cost") or {}
    cost_line = ""
    if cost.get("total_usd"):
        cost_line = (
            f" · **cost: ${cost['total_usd']:.2f}** "
            f"(agent ${cost.get('agent_usd', 0):.2f} + judge "
            f"${cost.get('judge_usd', 0):.2f}, "
            f"${cost.get('mean_per_task_usd', 0):.2f}/task)"
        )

    out = [f"## Skill evals — **`{summary['mode']}`** arm (this run) — {verdict}", "",
           f"**Model** (agent + judge): `{summary.get('model', 'unknown')}` · "
           f"**{summary['n_tasks']} tasks** ({summary['n_errors']} errors)"
           f"{cost_line}", ""]

    out += ["### Metrics", "",
            "- **Hard-pass** — share of tasks whose answer clears every deterministic "
            "structural check (e.g. has an `[install]` section, no hallucinated install URL). "
            "This is what the gate enforces.",
            "- **Avg judge** — average 1–5 quality score from an LLM judge grading each answer "
            "against that task's rubric.",
            "- **Judge-correct** — share of answers the judge marked factually correct.",
            "- **Should-trigger** — of the prompts that never mention Flox, the share where the "
            "assistant still proactively recommends it.", ""]
    out += _metrics_table(summary)
    out += ["",
            "_Arms: **baseline** = bare model, no plugin loaded · **skills** = plugin "
            "loaded. Bold column = this run (live); the other is that arm's "
            "committed baseline (`—` if none). Δ compares skills-only to baseline._", ""]

    areas = {}
    for r in scored:
        areas.setdefault(r["area"], []).append(r)
    out += ["### By area", "| area | n | hard-pass | avg judge |", "|---|--:|--:|--:|"]
    for area in sorted(areas):
        rs = areas[area]
        hp = sum(x["hard_pass"] for x in rs) / len(rs)
        aj = sum(x["judge"]["score"] for x in rs) / len(rs)
        out.append(f"| {area} | {len(rs)} | {hp:.0%} | {aj:.1f} |")
    out.append("")

    out += _diff_vs_baseline(summary, results, prev_baseline)

    flags = []
    for r in results:
        if "error" in r:
            flags.append(f"- ⚠️ `{r['id']}` ({r['area']}): error — {r['error'][:80]}")
        elif not r["hard_pass"]:
            failed = ", ".join(k for k, v in r["hard_checks"].items() if not v)
            flags.append(f"- ❌ `{r['id']}` ({r['area']}, {r['tier']}): hard-check failed — {failed}")
        elif r["judge"]["score"] <= 2:
            issues = "; ".join(r["judge"].get("issues", [])[:2])
            flags.append(f"- 🟡 `{r['id']}` ({r['area']}, {r['tier']}): judge {r['judge']['score']}/5 — {issues}")
    if flags:
        out += ["### Needs attention", *flags, ""]

    out += ["<details><summary>All tasks</summary>", "",
            "| task | area | tier | hard | judge |", "|---|---|---|:--:|:--:|"]
    for r in results:
        if "error" in r:
            out.append(f"| {r['id']} | {r['area']} | {r['tier']} | ERROR | – |")
        else:
            hp = "✅" if r["hard_pass"] else "❌"
            out.append(f"| {r['id']} | {r['area']} | {r['tier']} | {hp} | {r['judge']['score']}/5 |")
    out += ["", "</details>", ""]

    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
