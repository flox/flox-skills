#!/usr/bin/env python3
"""Flox /floxify skill eval harness — outcome-based.

Unlike run.py (which scores text answers to prompts), this harness:
  1. Copies a synthetic fixture repo to a temp dir (no .flox/ — skill creates it)
  2. Runs `claude /floxify <dir>` headlessly with the floxify skill loaded
  3. Reads the produced .flox/env/manifest.toml
  4. Scores it with deterministic hard-checks + an LLM judge (vs gold/)

Hard checks (deterministic — bind the gate for should-tier under --gate):
  manifest_created     .flox/env/manifest.toml exists
  valid_toml           file parses as valid TOML
  has_install_section  [install] section present
  has_services_section [services.*] present (only checked where listed in tasks.jsonl)
  no_abs_paths         no /home/ /Users/ /usr/local/ etc. in manifest values
  no_fake_install_url  no hallucinated Flox install URLs (curl|sh patterns)
  pins_node_20         manifest names nodejs_20 explicitly
  pins_python          manifest references a python package
  pins_go              manifest references the go package
  pins_rust            manifest references cargo
  pins_ruby            manifest references ruby

Activation check (advisory — never gates):
  Runs `flox activate -c "echo __ok__"` in the temp dir.
  Recorded as skipped ONLY when we could not run the check (flox not in PATH,
  --skip-activation, or a harness-side error). A timeout is recorded as a
  FAILURE, not a skip — we ran the check and the env did not come up within
  the budget. Budget is --activation-timeout (default 120s here; Tier 2 uses
  1800s since its first activations realize a full closure).

LLM judge (advisory — reported, never blocks the gate):
  Grades the produced manifest vs gold/<id>.toml 1-5 on package choices,
  hook quality (uses $FLOX_ENV_CACHE, correct ecosystem patterns), and
  idiomatic Flox usage. Handed verify.py's confirmed catalog resolution
  table (below) so it stops grading catalog facts from memory (AI-451).

verify.py check (advisory — never gates, same reason activation doesn't):
  Re-scans the fixture with detect.py and runs the flox-plugin's
  scripts/verify.py against the produced manifest — the same deterministic
  check Phase 3c runs inside the skill itself (AI-461). Its catalog leg
  needs live flox+network, so it is gated by --skip-activation exactly
  like the activation check; the non-catalog invariants (vars literalness,
  hook mutation, leaf-datastore/runtime cross-checks) always run.

Usage:
    python3 run_floxify.py                            # all fixtures, skills mode
    python3 run_floxify.py --only node-20             # single fixture
    python3 run_floxify.py --gate                     # exit 1 on hard-check failures
    python3 run_floxify.py --out results/my-run.json  # custom output path
    python3 run_floxify.py --skill-dir /path/to/flox-plugin
    python3 run_floxify.py --skip-activation          # skip flox activate checks

Pure stdlib — no additional packages required.
"""
import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES_DIR = HERE / "fixtures"
GOLD_DIR = HERE / "gold"


def _default_skill_dir():
    """Return the in-repo flox plugin directory containing the floxify skill.

    The plugin lives at `<repo-root>/flox-plugin`, two levels up from this
    file (`evals/floxify/`). This holds regardless of whether the repo is
    a regular checkout or a git worktree — worktree paths only add
    segments before the repo root, not below it.
    """
    return HERE.parent.parent / "flox-plugin"


DEFAULT_SKILL_DIR = _default_skill_dir()

# Default comparison target for regression detection: the committed baseline.
BASELINE_FILE = "floxify-baseline.json"


def _skill_identity(skill_dir):
    """Portable identity for the skill checkout — never an absolute host path.

    Records `<repo-basename>@<branch>` (or `@<short-sha>` on a detached HEAD)
    so the committed baseline stays reproducible across machines. Falls back
    to the bare basename when the directory is not a git checkout.
    """
    name = Path(skill_dir).name
    try:
        branch = subprocess.run(
            ["git", "-C", str(skill_dir), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        ref = branch.stdout.strip() if branch.returncode == 0 else ""
        if ref == "HEAD":  # detached — use the short SHA instead
            sha = subprocess.run(
                ["git", "-C", str(skill_dir), "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=10,
            )
            ref = sha.stdout.strip() if sha.returncode == 0 else ""
        return f"{name}@{ref}" if ref else name
    except Exception:
        return name


# Pinned model — match run.py for consistency across both harnesses.
MODEL = "claude-opus-4-8"

# Mirrors verify.py's own HARD/ADVISORY severity protocol. Not a static
# import of that constant (verify.py is loaded dynamically per-task, per
# --skill-dir — see _load_detect_and_verify) — these are the stable
# string values of a shared, versioned protocol between the two, not
# module-instance state, so re-declaring them here (instead of every
# caller re-typing the raw "hard"/"advisory" literal) is the right
# centralization without coupling to a specific loaded instance.
VERIFY_HARD = "hard"
VERIFY_ADVISORY = "advisory"


def _hard_verify_violations(violations):
    return [v for v in violations if v["severity"] == VERIFY_HARD]


def _advisory_verify_violations(violations):
    return [v for v in violations if v["severity"] == VERIFY_ADVISORY]

# --- deterministic hard-check patterns (reuse run.py patterns) ----------------

FAKE_INSTALL = re.compile(
    r"install\.flox\.dev|flox\.dev/install|curl[^\n]*flox[^\n]*\|\s*(ba)?sh", re.I
)
# Absolute host paths should never appear in manifest values.
ABS_PATH_IN_MANIFEST = re.compile(
    r'=\s*"(/home/|/Users/|/usr/local/|/opt/|/root/)', re.I
)

# --- TOML validation ----------------------------------------------------------
# tomllib is in stdlib since Python 3.11.  Graceful fallback for older runtimes.
try:
    import tomllib as _tomllib  # Python 3.11+
    _TOML_AVAILABLE = True
except ImportError:
    try:
        import tomli as _tomllib  # optional third-party fallback
        _TOML_AVAILABLE = True
    except ImportError:
        _tomllib = None
        _TOML_AVAILABLE = False


def _is_valid_toml(text):
    if not text:
        return False
    if _TOML_AVAILABLE:
        try:
            _tomllib.loads(text)
            return True
        except Exception:
            return False
    # Fallback heuristic: required keys present and brackets balance.
    opens = text.count("[")
    closes = text.count("]")
    return "schema-version" in text and opens == closes


# --- per-check lambdas --------------------------------------------------------

# Runtime-pin patterns anchor on the full pkg-path *value* (opening and
# closing quote) so an unrelated ecosystem tool cannot satisfy the check:
# "go" must not be matched by gopls/golangci-lint, "python3" not by
# python3Packages.*, "ruby" not by rubyPackages.*/ruby-build.  The optional
# version suffix lets the skill resolve either the generic or a versioned
# catalog name (go / go_1_21, python3 / python312, ruby / ruby_3_3).
# The version suffix can carry multiple underscore-number segments
# (go_1_21, ruby_3_3), so allow `(_[0-9]+)*` rather than a single segment.
PIN_NODE_20 = re.compile(r'pkg-path = "nodejs_20"')
PIN_GO = re.compile(r'pkg-path = "go(_[0-9]+)*"')
PIN_PYTHON = re.compile(r'pkg-path = "python3[0-9]*"')
PIN_RUST = re.compile(r'pkg-path = "cargo"')
PIN_RUBY = re.compile(r'pkg-path = "ruby(_[0-9]+)*"')
PIN_POSTGRES = re.compile(r'pkg-path = "postgresql(_[0-9]+)*"')


CHECKS = {
    "manifest_created":
        lambda m: m is not None,
    "valid_toml":
        lambda m: _is_valid_toml(m),
    "has_install_section":
        lambda m: m is not None and "[install]" in m,
    "has_services_section":
        lambda m: m is not None and bool(re.search(r"^\[services\.", m or "", re.M)),
    # Absence checks must still fail on a missing manifest — a None manifest
    # trivially "contains no absolute paths", which would be a misleading pass.
    "no_abs_paths":
        lambda m: m is not None and not ABS_PATH_IN_MANIFEST.search(m),
    "no_fake_install_url":
        lambda m: m is not None and not FAKE_INSTALL.search(m),
    # Runtime-version pins.
    # nodejs_20: the fixture explicitly pins Node 20 in .nvmrc — the skill
    # should honour the pin, not silently upgrade to the latest catalog version.
    "pins_node_20":
        lambda m: m is not None and bool(PIN_NODE_20.search(m)),
    # For other ecosystems, any version of the runtime is acceptable — the
    # skill may resolve a versioned name (e.g. python312, go_1_21, ruby_3_3)
    # or the generic name; both signal the runtime is installed.
    "pins_python":
        lambda m: m is not None and bool(PIN_PYTHON.search(m)),
    "pins_go":
        lambda m: m is not None and bool(PIN_GO.search(m)),
    "pins_rust":
        lambda m: m is not None and bool(PIN_RUST.search(m)),
    "pins_ruby":
        lambda m: m is not None and bool(PIN_RUBY.search(m)),
    # node-postgres: the postgres dependency is the point of the fixture —
    # hard-check it deterministically rather than leaving it to the judge.
    "pins_postgres":
        lambda m: m is not None and bool(PIN_POSTGRES.search(m)),
}


# --- cost/usage/turn accounting (AI-459 port, AI-442 extension) ---------------
# `claude -p --output-format json` (and stream-json's terminal `result`
# event, which carries the SAME fields — confirmed live, AI-442 PR 1
# flag-verification call) returns total_cost_usd + usage + num_turns on
# EVERY call. run.py's single-turn harness has captured this since
# AI-459; the two agentic spawn points below threw it away. Ported here
# with one addition AI-459's own `_parse_meta` left on the table:
# `num_turns` (see evals/run.py:81 for the sibling this mirrors).

def _parse_meta(envelope):
    """Extract cost/usage/duration/turns from a claude JSON envelope (or
    a stream-json terminal `result` event — same field names). Never
    raises — a cost-accounting detail must not be able to break an eval
    run."""
    try:
        cost = float(envelope.get("total_cost_usd") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    usage = envelope.get("usage")
    try:
        duration = int(envelope.get("duration_ms") or 0)
    except (TypeError, ValueError):
        duration = 0
    try:
        num_turns = int(envelope.get("num_turns") or 0)
    except (TypeError, ValueError):
        num_turns = 0
    return {
        "cost_usd": cost,
        "usage": usage if isinstance(usage, dict) else {},
        "duration_ms": duration,
        "num_turns": num_turns,
    }


ZERO_META = {
    "cost_usd": 0.0, "usage": {}, "duration_ms": 0, "num_turns": 0,
    "tool_calls": {"total": 0, "flox_search": 0, "flox_show": 0},
    "raw_stream": None,
}


# A Bash tool_use's `input.command` counts as `flox search`/`flox show`
# when that verb appears as the leading command, after a shell separator
# (;, &, |), or on its own line of a multiline command block (\n) --
# review-found (I1): a Bash tool_use commonly carries a MULTILINE script
# (e.g. "flox search x\nflox show y"), and without \n in the separator
# class, every line but the first was invisible to this classifier,
# undercounting exactly the reps that issued several catalog lookups in
# one Bash call -- and multiline usage can differ BETWEEN arms, so the
# gap was an asymmetric bias on the core metric, not just noise. Same
# discipline verify.py's hook checks use for "is this genuinely invoked,
# not just mentioned." This is a metric, not a security gate, so it stays
# deliberately permissive otherwise (no global-opts handling like
# verify.py's git/compose regexes) — a missed classification undercounts
# a turns/tool-calls metric, it does not silently pass a bug.
_FLOX_SEARCH_RE = re.compile(r"(?:^|[;&|\n]\s*)flox\s+search\b")
_FLOX_SHOW_RE = re.compile(r"(?:^|[;&|\n]\s*)flox\s+show\b")


def _classify_tool_calls(stream_events):
    """Count total tool_use blocks across an agent's assistant turns, plus
    the two flox subcommands Bill's efficiency thesis is specifically
    about ("the skill saves the search loop") — the sharpest available
    instrument, per AI-442's Q1 decision to go straight to stream-json
    tool-call counting rather than the coarser `num_turns` proxy.

    `stream_events` is a list of parsed stream-json event dicts (one per
    line of `--output-format stream-json` output). Never raises on a
    malformed/missing shape — a bad event is simply not a tool_use.
    """
    total = flox_search = flox_show = 0
    for event in stream_events:
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        content = (event.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            total += 1
            if block.get("name") != "Bash":
                continue
            command = (block.get("input") or {}).get("command")
            if not isinstance(command, str):
                continue
            if _FLOX_SEARCH_RE.search(command):
                flox_search += 1
            if _FLOX_SHOW_RE.search(command):
                flox_show += 1
    return {"total": total, "flox_search": flox_search, "flox_show": flox_show}


def _parse_stream(stdout_text):
    """Parse `--output-format stream-json` output (newline-delimited JSON,
    one event per line) into (result_text, meta, has_result_event).
    `meta` is the same shape `_parse_meta` returns, plus `tool_calls` and
    `raw_stream` (the original text, for per-rep persistence by the
    caller). `has_result_event` tells the caller whether a genuine
    terminal `result` event was found — the caller's own signal for
    "real success" vs "garbled/truncated output", rather than guessing
    from whether num_turns or tool_calls happens to be zero (both are
    legitimately zero on a real, trivial, successful run).

    Never raises: a garbled or partial stream (a killed/timed-out
    process, a truncated pipe) degrades to ("", ZERO_META, False) rather
    than propagating a parse error into the harness. The terminal
    `result` event (the last well-formed `type: "result"` line) supplies
    both the agent's final text answer and cost/usage/duration/turns —
    the same fields the plain-json envelope's top level carries,
    confirmed live (AI-442 PR 1 flag-verification call). Every
    well-formed line contributes to tool-call counting regardless of
    whether a `result` event is ever reached, so a rep that timed out
    mid-stream still yields an honest (if partial) tool-call count
    instead of zero.
    """
    events = []
    result_event = None
    for line in (stdout_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        events.append(event)
        if event.get("type") == "result":
            result_event = event

    meta = _parse_meta(result_event) if result_event is not None else dict(ZERO_META)
    meta["tool_calls"] = _classify_tool_calls(events)
    meta["raw_stream"] = stdout_text
    result_text = result_event.get("result", "") if result_event is not None else ""
    return result_text, meta, result_event is not None


def _find_init_event(stdout_text):
    """The first well-formed `type: "system", subtype: "init"` event in a
    stream-json transcript, or None. That event carries `plugins` and
    `slash_commands` — which plugins genuinely loaded for this call —
    independent of `_parse_stream`'s own tool-call-focused handling.
    Used by the arm-contamination guard below (AI-442 C1)."""
    for line in (stdout_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (isinstance(event, dict) and event.get("type") == "system"
                and event.get("subtype") == "init"):
            return event
    return None


def _detect_flox_plugin_contamination(init_event):
    """True if the flox/floxify plugin loaded for this call despite not
    being requested via `--plugin-dir` (AI-442 C1 — a live review found
    this machine's user-scope `~/.claude/settings.json` has
    `flox@flox-skills` enabled in `enabledPlugins`, and
    `--strict-mcp-config` only gates MCP servers, not plugins: a bare
    `claude -p` call with NEITHER `--plugin-dir` NOR any plugin flag
    still loaded `flox:floxify` — see
    testdata/stream-samples/README.md). `--setting-sources project,local`
    on both arms is the primary fix (suppresses user-scope
    `enabledPlugins` entirely); this is the belt-and-suspenders runtime
    check specifically for the baseline arm, so a leak can never
    silently poison a rep's data even if the primary fix regresses on a
    future Claude Code CLI version or a different machine's settings.

    Checks two independent signals so a change to either the `plugins`
    list's shape or the `slash_commands` list's shape alone doesn't
    blind the guard: a `plugins` entry named `flox` (or whose `source`
    starts with `flox@`, covering both the `flox-marketplace` and
    `flox-skills` marketplace names seen in practice), OR any
    `flox:`-prefixed slash command (`flox:flox`, `flox:floxify`).
    """
    if not isinstance(init_event, dict):
        return False
    plugins = init_event.get("plugins")
    if isinstance(plugins, list):
        for p in plugins:
            if not isinstance(p, dict):
                continue
            if p.get("name") == "flox" or str(p.get("source", "")).startswith("flox@"):
                return True
    slash_commands = init_event.get("slash_commands")
    if isinstance(slash_commands, list):
        if any(str(c).startswith("flox:") for c in slash_commands):
            return True
    return False


# --- claude invocation --------------------------------------------------------

def _run_claude_agent(prompt, skill_dir, arm="skills", timeout=600, retries=2):
    """Invoke claude headlessly with the floxify skill and required tools.
    Returns (result_text, err, meta) — see `_parse_stream`/`ZERO_META`.

    The floxify skill needs Bash (flox search/init/activate, ls, etc.),
    Read (project files), Write+Edit (manifest.toml), and Skill (to invoke
    the /floxify skill itself). `--plugin-dir` is the intended arm switch
    (AI-442 Q7: both arms get the identical tool surface, "skills" vs
    "baseline" differs ONLY in whether the skill is loaded) — `baseline`
    omits it, matching run.py's/screen.py's own arm-isolation mechanism
    ported to the agentic path.

    `--setting-sources project,local` (AI-442 C1, review-found): a live
    review caught that `--plugin-dir` presence/absence is NOT sufficient
    isolation by itself on a machine whose user-scope
    `~/.claude/settings.json` has `flox@flox-skills` enabled in
    `enabledPlugins` — `--strict-mcp-config` only gates MCP servers, not
    plugins, so the "baseline" arm would silently run WITH the skill
    loaded. `--setting-sources project,local` excludes the user-scope
    settings file from consideration (so its `enabledPlugins` entry
    never applies), while `--plugin-dir` itself is a CLI-level plugin
    load independent of the settings-file plugin-enablement mechanism —
    confirmed live it still loads the skill on the skills arm with this
    flag present (testdata/stream-samples/README.md carries both
    directions' verification). Applied unconditionally (both arms): the
    baseline arm needs the leak closed, and there is no reason for the
    skills arm to read this machine's other ambient user-scope settings
    either — reproducibility, not just isolation.

    `--output-format stream-json --verbose` (not plain `json`) — AI-442
    Q1: tool-call counting needs the per-event stream, not just the
    final envelope; the flag combination was verified live (PR body /
    testdata/stream-samples/README.md carry the verification writeup).
    """
    cmd = [
        "claude", "-p", prompt,
        "--model", MODEL,
        "--output-format", "stream-json",
        "--verbose",
        "--allowedTools", "Bash", "Read", "Write", "Edit", "Skill",
        "--strict-mcp-config",
        "--setting-sources", "project,local",
    ]
    if arm != "baseline":
        cmd += ["--plugin-dir", str(skill_dir)]
    last = "unknown"
    for attempt in range(retries):
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            last = "TIMEOUT"
        else:
            if proc.returncode != 0:
                last = f"EXIT {proc.returncode}: {proc.stderr[:300]}"
            else:
                result_text, meta, has_result = _parse_stream(proc.stdout)
                if not has_result:
                    # No terminal `result` event -- genuinely unparseable
                    # or truncated output, not a quiet success.
                    last = f"BAD_STREAM: {proc.stdout[:200]}"
                elif arm == "baseline" and _detect_flox_plugin_contamination(
                    _find_init_event(proc.stdout)
                ):
                    # AI-442 C1: belt-and-suspenders runtime guard. A
                    # leak is a deterministic property of this
                    # environment's settings, not a transient flake --
                    # retrying would just reproduce it, so this returns
                    # immediately rather than consuming a retry attempt.
                    return None, (
                        "arm contamination: baseline arm loaded the flox "
                        "plugin despite --setting-sources project,local -- "
                        "rep discarded, not counted as baseline data"
                    ), dict(ZERO_META)
                elif _detect_harness_misconfiguration(result_text):
                    # AI-442 batch-1 finding: applies to either arm, not
                    # just baseline -- an unrecognized slash command is a
                    # deterministic property of the prompt/harness
                    # mismatch, not a transient flake, so this returns
                    # immediately rather than consuming a retry attempt
                    # (same shape as the C1 guard above).
                    return None, (
                        "harness misconfiguration: agent output shows an "
                        "unrecognized slash command was invoked (e.g. "
                        "'Unknown command: /floxify') -- rep discarded, "
                        "not counted as verify data"
                    ), dict(ZERO_META)
                else:
                    return result_text, None, meta
        if attempt < retries - 1:
            time.sleep(3 + attempt * 4)
    return None, last, dict(ZERO_META)


def _run_judge(prompt, timeout=120):
    """Run the judge call — bare model, no plugin, no tools.
    Returns (result_text, err, meta).

    Plain `--output-format json` (not stream-json): the judge never
    calls a tool (no `--allowedTools`), so there is nothing for a stream
    parse to count — `meta`'s `tool_calls` stays zero and `raw_stream`
    stays None, keeping the return shape symmetric with the agent's
    without pretending there is a stream to persist.
    """
    cmd = [
        "claude", "-p", prompt,
        "--model", MODEL,
        "--output-format", "json",
        "--strict-mcp-config",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            return None, f"EXIT {proc.returncode}", dict(ZERO_META)
        envelope = json.loads(proc.stdout)
        return envelope.get("result", ""), None, _parse_meta(envelope)
    except subprocess.TimeoutExpired:
        return None, "TIMEOUT", dict(ZERO_META)
    except json.JSONDecodeError as exc:
        return None, f"BAD_JSON: {exc}", dict(ZERO_META)
    except Exception as exc:
        return None, str(exc), dict(ZERO_META)


def _catalog_note(verify_result):
    """Render verify.py's catalog leg into a prompt note for the judge —
    AI-451: the judge has graded catalog facts from memory and accused a
    *correct* pin of being hallucinated. Handing it verify.py's confirmed
    resolution table (or an explicit "not checked" note) takes catalog
    facts off the judge's plate instead of asking it to remember them.
    """
    if not verify_result or "error" in verify_result:
        return (
            "\nDETERMINISTIC CATALOG CHECK: not available this run (harness "
            "error) — do not assert catalog facts from memory; grade only "
            "structure, hook quality, and idiomatic Flox usage.\n"
        )
    if not verify_result.get("catalog_checked"):
        return (
            "\nDETERMINISTIC CATALOG CHECK: not run this pass (flox/network "
            "unavailable) — do not assert catalog facts from memory; grade "
            "only structure, hook quality, and idiomatic Flox usage.\n"
        )
    catalog_hard = [
        v for v in _hard_verify_violations(verify_result["violations"])
        if v["rule"].startswith("catalog-")
    ]
    unknown = verify_result.get("catalog_unknown") or []
    if catalog_hard:
        listing = "; ".join(v["message"] for v in catalog_hard[:5])
        note = (
            f"\nDETERMINISTIC CATALOG CHECK (verify.py, via `flox show`): "
            f"{len(catalog_hard)} pkg-path/version/system violation(s) "
            f"CONFIRMED against the live catalog: {listing}\n"
        )
    elif unknown:
        # verify.py excludes these from its confirmed table (check_catalog's
        # `available is None` path) — the judge note must too, rather than
        # rounding "no violation" up to "confirmed clean" for entries the
        # catalog leg genuinely could not evaluate.
        names = ", ".join(u["install_id"] for u in unknown)
        note = (
            f"\nDETERMINISTIC CATALOG CHECK (verify.py, via `flox show`): no "
            f"violations found, but {len(unknown)} install entr"
            f"{'y' if len(unknown) == 1 else 'ies'} ({names}) had UNKNOWN "
            f"per-system availability and were NOT confirmed either way — "
            f"do not assert catalog facts about those specific entries from "
            f"memory. All other installed pkg-path/version/system "
            f"combinations were CONFIRMED to resolve.\n"
        )
    else:
        note = (
            "\nDETERMINISTIC CATALOG CHECK (verify.py, via `flox show`): every "
            "installed pkg-path/version/system combination was CONFIRMED to "
            "resolve in the live catalog. Do not second-guess this from memory "
            "— e.g. do not flag a pin as hallucinated on this basis.\n"
        )
    return note


def _judge(task, produced_toml, verify_result=None):
    """Grade produced manifest vs gold — returns ({score, correct, issues}, meta).

    `meta` (AI-442) is the judge call's own cost/usage/duration/turns —
    captured separately from the agent's (the AI-459 split preserved
    across the port), so judge spend never contaminates the agent
    efficiency number."""
    gold_path = GOLD_DIR / f"{task['id']}.toml"
    gold = gold_path.read_text() if gold_path.exists() else "(no gold available)"

    prompt = (
        "You are grading a Flox manifest produced by an AI agent that onboards "
        "projects to Flox. Be strict and concrete.\n\n"
        f"FIXTURE: {task['id']} (ecosystem: {task.get('ecosystem', 'unknown')})\n"
        f"RUBRIC: {task['rubric']}\n\n"
        f"REFERENCE (gold) manifest:\n```toml\n{gold}\n```\n\n"
        f"PRODUCED manifest:\n```toml\n{produced_toml or '(manifest not produced)'}\n```\n"
        f"{_catalog_note(verify_result)}\n"
        "Grade 1-5 on:\n"
        "  1. Package choices — correct catalog names, version-pinned where fixture signals one. "
        "Do NOT assert from memory whether a pkg-path or version exists in the Flox catalog — "
        "rely on the DETERMINISTIC CATALOG CHECK above; if it is unavailable, do not grade "
        "catalog existence at all\n"
        "  2. Hook quality — uses $FLOX_ENV_CACHE (not ./venv or absolute paths), correct ecosystem patterns\n"
        "  3. Idiomatic Flox usage — no hallucinated install URLs, sections only when needed\n"
        "  4. Service wiring — [services.*] present when detected (node-postgres)\n\n"
        "Score 5 = matches gold closely; 3 = correct but missing idioms; 1 = wrong packages or broken hook.\n"
        'Return ONLY a JSON object: {"score": <int 1-5>, "correct": <true|false>, "issues": [<short strings>]}'
    )
    result, err, meta = _run_judge(prompt)
    if err:
        return {"score": 0, "correct": False, "issues": [f"judge error: {err}"]}, meta
    raw = {}
    m = re.search(r"\{.*\}", result or "", re.S)
    if m:
        try:
            raw = json.loads(m.group(0))
        except json.JSONDecodeError:
            raw = {"issues": ["judge json parse failed"]}
    else:
        raw = {"issues": ["no json in judge response"]}
    try:
        score = int(raw.get("score", 0) or 0)
    except (TypeError, ValueError):
        score = 0
    return {
        "score": score,
        "correct": bool(raw.get("correct", False)),
        "issues": raw.get("issues", []),
    }, meta


# --- activation check ---------------------------------------------------------

DEFAULT_ACTIVATION_TIMEOUT = 120


def _check_activation(target_dir, timeout=DEFAULT_ACTIVATION_TIMEOUT):
    """Attempt `flox activate -c 'echo __ok__'` — returns (ok, skipped, notes).

    `skipped` means we could not run the check at all (flox absent, or the
    harness itself errored). A **timeout is a failure, not a skip**: we ran the
    check and the environment did not come up within the budget. Conflating the
    two silently inflated `activation_skipped` and read as benign — posthog
    exceeded the old hardcoded 120s and was recorded as skipped, so the largest
    repo in the corpus yielded no activation signal at all (AI-454).

    `timeout` is caller-set because the right budget depends on the tier: small
    Tier 1 fixtures activate in seconds, while a Tier 2 monorepo's first
    activation realizes an entire closure.
    """
    if not shutil.which("flox"):
        return None, True, "flox not in PATH"
    try:
        proc = subprocess.run(
            ["flox", "activate", "-c", "echo __ok__"],
            cwd=str(target_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        activated = proc.returncode == 0 and "__ok__" in proc.stdout
        notes = "" if activated else (
            f"exit {proc.returncode}: {(proc.stderr or proc.stdout)[:200]}"
        )
        return activated, False, notes
    except subprocess.TimeoutExpired:
        return False, False, (
            f"TIMEOUT: activation exceeded {timeout}s. This is a finding, not a "
            f"skip — the environment may work but is too slow to verify at this "
            f"budget. Raise --activation-timeout if the budget is wrong."
        )
    except Exception as exc:
        # A harness-side error (fork failure, etc.) is our problem, not the
        # manifest's — that genuinely is 'we could not check'.
        return None, True, str(exc)


# --- verify.py integration (AI-461 deterministic leg) --------------------------

def _load_module_from_path(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_detect_and_verify(skill_dir):
    """Load detect.py / verify.py from the skill dir under test — not a
    hardcoded path — so `--skill-dir` continues to control which skill
    checkout the whole harness exercises."""
    scripts_dir = Path(skill_dir) / "skills" / "floxify" / "scripts"
    detect_mod = _load_module_from_path(scripts_dir / "detect.py", "_floxify_detect")
    verify_mod = _load_module_from_path(scripts_dir / "verify.py", "_floxify_verify")
    return detect_mod, verify_mod


def _run_verify(skill_dir, fixture_src, manifest_text, check_catalog_live):
    """Ground the produced manifest against detect.py facts re-scanned
    from fixture_src — the deterministic leg alongside activation and
    the judge. Whether fixture_src is a pristine fixture (Tier 1) or
    the post-run checkout (Tier 2, which has no pristine copy) is the
    call site's choice, documented there.

    Returns a result dict on success, or {"error": ...} if detect/verify
    could not run at all (a harness-side problem, not a manifest verdict —
    mirrors _check_activation's own skipped/failed distinction).
    """
    if manifest_text is None:
        return {"violations": [], "catalog_checked": False, "catalog_unknown": [],
                "skipped": "no manifest produced"}
    try:
        detect_mod, verify_mod = _load_detect_and_verify(skill_dir)
        detect_facts = detect_mod.scan(fixture_src)
        return verify_mod.verify(
            detect_facts, manifest_text, check_catalog_live=check_catalog_live,
        )
    except Exception as exc:  # noqa: BLE001 - a harness-side failure, not a manifest verdict
        return {"violations": [], "catalog_checked": False, "catalog_unknown": [],
                "error": str(exc)}


# --- per-task runner ----------------------------------------------------------

def _base(task):
    # `.get("tier", ...)` -- a task fed from tier2.jsonl (AI-463: this
    # harness is Tier 1 only) has no "tier" key at all, and this function
    # is called from the fixture-not-found error path below, which is
    # exactly the shape that reaches it for such a task. A bare
    # task["tier"] here would just move the KeyError one line down from
    # the print fix, not actually resolve it.
    return {"id": task["id"], "tier": task.get("tier", "?"),
            "ecosystem": task.get("ecosystem", "")}


# --- verified-anchor strength (AI-442 Q2) --------------------------------------
# A runtime-only `flox activate` proves packages resolve, not that a
# declared service actually serves — the same gap AI-447 closed for
# Tier 2 (tier2.py::_probe_services). Q2's binding decision: use that
# stronger anchor here too, for exactly the fixtures that declare one.

_CHECK_TO_SERVICE_KIND = {
    "pins_postgres": "postgres",
    "pins_redis": "redis",
    "pins_mariadb": "mariadb",
    "pins_mysql": "mysql",
}


def _expected_service_kind(task):
    """The service kind a task's own `checks` declare, or None.
    `has_services_section` says a service is expected; the specific kind
    comes from whichever `pins_<kind>` check accompanies it. None means
    activation is the anchor for this fixture (AI-442 Q2)."""
    checks = task.get("checks", [])
    if "has_services_section" not in checks:
        return None
    for chk in checks:
        kind = _CHECK_TO_SERVICE_KIND.get(chk)
        if kind:
            return kind
    return None


# Same probe-command table and settle-loop technique as tier2.py's AI-447
# probe (`tier2.py::_probe_services`) — NOT imported from there: tier2.py
# already imports `_run_claude_agent`/`_run_judge` from this module, so a
# reverse import would be circular. Small enough to duplicate the idiom
# rather than restructure either module's dependency direction for it;
# a new service kind is a one-line table addition in both places, same
# as it always was.
_SERVICE_PROBE_COMMANDS = {
    "postgres": "pg_isready -q",
    "postgresql": "pg_isready -q",
    "redis": 'redis-cli ${REDIS_PORT:+-p "$REDIS_PORT"} ping',
    "valkey": 'redis-cli ${REDIS_PORT:+-p "$REDIS_PORT"} ping',
    "mariadb": "mariadb-admin ping",
    "mysql": "mysqladmin ping",
}

_SERVICE_PROBE_OK = "__SERVICE_OK__"
_SERVICE_PROBE_DEAD = "__SERVICE_DEAD__"


def _service_probe_script(probe, settle):
    """Poll `probe` for up to `settle` seconds, printing a sentinel
    either way — services start asynchronously, so a single immediate
    probe races the postmaster."""
    return (
        f'for _ in $(seq {settle}); do '
        f'  if {probe} >/dev/null 2>&1; then echo {_SERVICE_PROBE_OK}; exit 0; fi; '
        f'  sleep 1; '
        f'done; '
        f'echo {_SERVICE_PROBE_DEAD}; exit 1'
    )


def _probe_service(target_dir, kind, manifest_text, verify_mod,
                   timeout=300, settle=30):
    """Prove a *declared* service of `kind` actually serves.
    Returns (ok, skipped, notes) — the same three-way shape
    `_check_activation` uses.

    `skipped=True` means "not probeable" (no probe command for this
    kind) or a harness-side failure — never a verdict on the manifest.
    A declared-but-unwired service (no `[services.*]` entry matches
    `kind`) is a genuine, non-skipped failure: Q2's whole point is that
    activation succeeding while the expected service was never wired
    must not read as "verified." Requires flox on PATH and a working
    activation already established by the caller (probing an
    unactivated env errors — services can only start from inside one).

    Deliberate divergence from `tier2.py`'s own `_probe_services`: that
    function leaves an unmatched service at its `skipped=True` default
    (tier2's declared-service gating is advisory-only, never Tier 2's
    own gate). This function returns a real `(False, False, ...)`
    failure for the identical shape instead, because Tier 1's
    efficiency axis needs "unwired" to count as `failed-verify`, not a
    dropped observation (see the censoring table in the module
    docstring / README's "Efficiency axis" section). A future pass that
    aligns the two probes should treat this as an intentional
    difference to preserve, not an inconsistency to fix.
    """
    if not shutil.which("flox"):
        return None, True, "flox not in PATH"
    probe = _SERVICE_PROBE_COMMANDS.get((kind or "").lower())
    if not probe:
        return None, True, f"no connectivity probe for '{kind}'"

    manifest, parse_err = verify_mod.parse_manifest(manifest_text or "")
    if manifest is None:
        return None, True, f"manifest did not parse -- nothing to probe: {parse_err}"
    matches = verify_mod.matching_service_names(manifest, kind)
    if not matches:
        return False, False, (
            f"no [services.*] entry matches kind '{kind}' -- the declared "
            f"service was not wired"
        )

    try:
        proc = subprocess.run(
            ["flox", "activate", "--start-services", "-c",
             _service_probe_script(probe, settle)],
            cwd=str(target_dir), capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, False, f"TIMEOUT: probe exceeded {timeout}s"
    except Exception as exc:  # noqa: BLE001 - a probe failure is our problem, not the manifest's
        return None, True, str(exc)

    out = (proc.stdout or "") + (proc.stderr or "")
    if _SERVICE_PROBE_OK in out:
        return True, False, "connectivity confirmed"
    if _SERVICE_PROBE_DEAD in out:
        return False, False, f"polled {settle}s, service never answered"
    return None, True, f"probe script did not run: exit {proc.returncode}: {out[:200]}"


def _compute_verification(act_ok, act_skipped, service_kind, service_probe):
    """The censoring-relevant verdict for one rep: (verified, verify_method,
    terminal_disposition). AI-442 Q2's anchor rule, made explicit and
    directly testable rather than inlined into `process_task`.

    `service_probe` is None when `service_kind` is None (activation is
    the anchor) or when activation never succeeded (nothing to probe);
    otherwise the `(ok, skipped, notes)` triple `_probe_service` returned.

    terminal_disposition is one of the design doc's four censoring
    categories — "verified" / "failed-verify" / "unverifiable-env" (only
    this function's callers ever assign "agent-error", since that
    disposition is decided before activation is ever attempted).
    """
    verify_method = "services" if service_kind else "activation"
    if act_skipped:
        return False, verify_method, "unverifiable-env"
    if not act_ok:
        return False, verify_method, "failed-verify"
    if service_kind is None:
        return True, "activation", "verified"
    ok, skipped, _notes = service_probe
    if skipped:
        # flox vanished mid-probe or a harness-side error -- not a
        # verdict on the manifest, same discipline as act_skipped above.
        return False, "services", "unverifiable-env"
    return bool(ok), "services", ("verified" if ok else "failed-verify")


def _build_prompt(tmpdir, arm):
    """The headless task prompt for one rep.

    AI-442 batch-1 finding: the original assumption that both arms
    could share the IDENTICAL `/floxify <dir>` prompt was wrong.
    `/floxify` does not resolve to nothing when the plugin is absent —
    it is an unrecognized slash command ("Unknown command: /floxify"),
    so EVERY baseline rep in the first real batch died on turn one
    without attempting the task at all (40/40 reps). The two arms need
    functionally EQUIVALENT prompts, not textually identical ones.

    Fairness line (mined from `screen.py`'s established baseline-vs-
    skills precedent on `bill/ai-435-discriminating-evals` — its two
    arms share one candidate prompt, phrased in plain task language,
    never a skill-specific invocation): naming Flox's own standard
    conventions is fair — `.flox/env/manifest.toml` (what `flox init`
    creates), "`flox activate` succeeds" as the success anchor,
    resolving packages "in the Flox catalog", wiring "services" — this
    is the tool's own vocabulary, the same words a user would put in a
    real request, and `screen.py`'s own candidate prompts use exactly
    this level of Flox-specificity (e.g. "sandbox = \"pure\"",
    "flox activate --start-services"). Embedding anything FROM SKILL.md
    itself — the pkg-group economy escalation ladder, the pin-
    discipline gradation, the service-floor invariant's exact wording,
    a specific hook idiom like `$FLOX_ENV_CACHE` — would be
    contamination: that is the skill teaching the baseline arm its own
    answer, not a neutral task description.
    """
    if arm == "baseline":
        return (
            f"Convert the repository at {tmpdir} into a working Flox "
            f"environment: create .flox/env/manifest.toml such that "
            f"`flox activate` succeeds, wiring any services the "
            f"project needs to run locally (e.g. a database) as Flox "
            f"services.\n\n"
            f"Run non-interactively: scan the project files, resolve "
            f"packages in the Flox catalog, and write "
            f".flox/env/manifest.toml. Do not ask for or wait for "
            f"user input — produce the best manifest you can and stop "
            f"after writing it."
        )
    # The /floxify prefix invokes the skill by name. The non-interactive
    # note prevents the skill from blocking on "What would you like to
    # do next?" in the Phase 4 menu.
    return (
        f"/floxify {tmpdir}\n\n"
        "Run non-interactively: complete all phases (scan project files, "
        "resolve packages in the Flox catalog, write .flox/env/manifest.toml). "
        "Do not ask for or wait for user input — produce the best manifest you "
        "can and stop after writing it."
    )


# A rep whose agent result text shows Claude Code rejected an
# unrecognized slash command -- AI-442 batch-1's actual failure mode
# (every baseline rep hit "Unknown command: /floxify" before the
# baseline prompt fix above). The prompt fix is the real fix; this is
# the belt-and-suspenders guard, same shape as the C1 arm-contamination
# guard, so a FUTURE regression that reintroduces a skill-specific
# slash command in a prompt fails loudly and specifically instead of
# silently recording a `failed-verify` rep that never attempted the
# task at all.
_UNKNOWN_COMMAND_RE = re.compile(r"^Unknown command:", re.MULTILINE)


def _detect_harness_misconfiguration(result_text):
    return bool(_UNKNOWN_COMMAND_RE.search(result_text or ""))


def _write_stream_file(stream_dir, task_id, arm, rep, role, raw_stream):
    """Persist one rep's raw stream-json transcript to disk (AI-442:
    "persist raw streams per rep"). Returns the path relative to
    `stream_dir`'s parent (portable across machines, embeddable in the
    committed results JSON), or None if there was nothing to write.
    Never raises — losing a stream file is not a reason to fail a rep
    whose measurement already completed.
    """
    if not stream_dir or not raw_stream:
        return None
    try:
        stream_dir = Path(stream_dir)
        stream_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{task_id}__{arm}__rep{rep}__{role}.jsonl"
        (stream_dir / filename).write_text(raw_stream, encoding="utf-8")
        return f"{stream_dir.name}/{filename}"
    except Exception:  # noqa: BLE001 - persistence is best-effort
        return None


def process_task(task, skill_dir, skip_activation=False,
                 activation_timeout=DEFAULT_ACTIVATION_TIMEOUT,
                 arm="skills", rep=1, stream_dir=None):
    """Copy fixture to temp dir, run the skill, score the result.

    `arm` ("skills" / "baseline") and `rep` are recorded on the result
    (AI-442 §1.1's per-rep record shape) so `_efficiency_summary` can
    group by (fixture, arm) and report distributions across reps.
    `stream_dir`, when set, is where this rep's raw agent stream-json
    transcript is written (AI-442: "persist raw streams per rep").
    """
    task_id = task["id"]
    fixture_src = FIXTURES_DIR / task_id
    if not fixture_src.exists():
        tier = task.get("tier", "?")
        print(f"  [{tier}] {task_id}: ERROR no fixtures/{task_id} directory", flush=True)
        return {**_base(task), "arm": arm, "rep": rep,
                "terminal_disposition": "unverifiable-env", "error": (
            f"no fixtures/{task_id} directory -- this harness (run_floxify.py) "
            f"is Tier 1 only and needs a local fixture checked into "
            f"fixtures/{task_id}. Real-repo entries (cloned at a pinned SHA) "
            f"live in tier2.jsonl and run via tier2.py, not this script: "
            f"python3 tier2.py --only {task_id}"
        )}

    with tempfile.TemporaryDirectory(prefix=f"floxify-eval-{task['id']}-") as tmpdir:
        # Copy fixture into temp dir.  No .flox/ in fixtures — skill creates it.
        shutil.copytree(str(fixture_src), tmpdir, dirs_exist_ok=True)
        tmp = Path(tmpdir)

        prompt = _build_prompt(tmpdir, arm)

        print(f"  [{task.get('tier', '?')}] {task_id} ({arm}#{rep}): invoking skill ...",
              flush=True)
        agent_out, agent_err, agent_meta = _run_claude_agent(prompt, skill_dir, arm=arm)
        stream_file = _write_stream_file(
            stream_dir, task_id, arm, rep, "agent", agent_meta.get("raw_stream")
        )

        if agent_err:
            print(
                f"  [{task.get('tier', '?')}] {task_id} ({arm}#{rep}): "
                f"agent error: {agent_err}", flush=True
            )
            return {**_base(task), "arm": arm, "rep": rep,
                    "terminal_disposition": "agent-error", "error": agent_err,
                    "num_turns": {"agent": agent_meta["num_turns"]},
                    "tool_calls": {"agent": agent_meta["tool_calls"]},
                    "stream_file": {"agent": stream_file}}

        # Read produced manifest (if skill wrote it).
        manifest_path = tmp / ".flox" / "env" / "manifest.toml"
        manifest_text = (
            manifest_path.read_text(encoding="utf-8")
            if manifest_path.exists()
            else None
        )

        # Hard checks.
        hard = {chk: CHECKS[chk](manifest_text) for chk in task["checks"]}
        hard_pass = all(hard.values())

        # Activation check (advisory to hard_pass/--gate — skipped when
        # unavailable; feeds the efficiency-axis verified anchor below).
        if skip_activation:
            act_ok, act_skipped, act_notes = None, True, "--skip-activation flag set"
        else:
            act_ok, act_skipped, act_notes = _check_activation(
                tmp, timeout=activation_timeout
            )

        # AI-442 Q2: activation alone is not proof a declared service
        # serves. Probe only when the fixture expects one AND activation
        # actually succeeded (probing an unactivated env errors).
        service_kind = _expected_service_kind(task)
        service_probe = None
        if service_kind and act_ok and not act_skipped:
            _, probe_verify_mod = _load_detect_and_verify(skill_dir)
            service_probe = _probe_service(
                tmp, service_kind, manifest_text, probe_verify_mod,
            )
        verified, verify_method, terminal_disposition = _compute_verification(
            act_ok, act_skipped, service_kind, service_probe,
        )

        # Deterministic manifest check (AI-461 — advisory, same reason
        # activation is advisory: the catalog leg needs live flox+network,
        # which neither a test environment nor every CI run has).
        verify_result = _run_verify(
            skill_dir, fixture_src, manifest_text,
            check_catalog_live=not skip_activation,
        )
        verify_hard = _hard_verify_violations(verify_result["violations"])
        verify_advisory = _advisory_verify_violations(verify_result["violations"])

        # LLM judge (advisory) — hand it verify.py's confirmed catalog
        # resolution table so it stops grading catalog facts from memory.
        # Captured separately from the agent (AI-459 split, preserved
        # across the AI-442 port) so judge spend never distorts the
        # agent efficiency number.
        verdict, judge_meta = _judge(task, manifest_text, verify_result=verify_result)

        status = "PASS" if hard_pass else "FAIL"
        act_str = "skipped" if act_skipped else ("ok" if act_ok else "FAIL")
        verify_str = f"{len(verify_hard)}H/{len(verify_advisory)}A"
        cost_total = agent_meta["cost_usd"] + judge_meta["cost_usd"]
        print(
            f"  [{task.get('tier', '?')}] {task_id} ({arm}#{rep}): "
            f"hard={status}  judge={verdict['score']}/5  activate={act_str}  "
            f"verify={verify_str}  verified={verified} ({verify_method})  "
            f"turns={agent_meta['num_turns']}  "
            f"tools={agent_meta['tool_calls']['total']}  "
            f"${cost_total:.2f}",
            flush=True,
        )

        return {
            **_base(task),
            "arm": arm,
            "rep": rep,
            "hard_checks": hard,
            "hard_pass": hard_pass,
            "activation": {
                "ok": act_ok,
                "skipped": act_skipped,
                "notes": act_notes,
            },
            "verify": {
                "violations": verify_result["violations"],
                "hard_count": len(verify_hard),
                "advisory_count": len(verify_advisory),
                "catalog_checked": verify_result.get("catalog_checked", False),
            },
            "judge": verdict,
            "verified": verified,
            "verify_method": verify_method,
            "terminal_disposition": terminal_disposition,
            "cost": {
                "agent_usd": agent_meta["cost_usd"],
                "judge_usd": judge_meta["cost_usd"],
                "total_usd": cost_total,
            },
            "usage": {"agent": agent_meta["usage"], "judge": judge_meta["usage"]},
            "num_turns": {"agent": agent_meta["num_turns"], "judge": judge_meta["num_turns"]},
            "duration_ms": {"agent": agent_meta["duration_ms"],
                            "judge": judge_meta["duration_ms"]},
            "tool_calls": {"agent": agent_meta["tool_calls"]},
            "stream_file": {"agent": stream_file},
            # Full text persisted alongside the excerpt (AI-468, aligning
            # Tier 1 with Tier 2's own fix) — a truncated excerpt has
            # blocked forensics on a failing Tier 2 rep twice; Tier 1
            # fixtures are the same order of magnitude, so the same gap
            # exists here even though it hasn't bitten yet. manifest_excerpt
            # stays for anything that still displays a short preview.
            "manifest": manifest_text or "",
            "manifest_excerpt": (manifest_text or "")[:3000],
            "agent_output_excerpt": (agent_out or "")[:800],
        }


# --- summary helpers ----------------------------------------------------------

def _read_baseline(name):
    """Load a committed results/<name> baseline snapshot, or None if absent/bad."""
    try:
        return json.loads((HERE / "results" / name).read_text())
    except Exception:
        return None


def _diff_vs_baseline(summary, results, baseline):
    """Regression report vs the committed baseline.

    Signal: per-fixture hard-check flips (a fixture that passed in the
    baseline but fails now is a regression; the reverse is a fix).
    Advisory: per-fixture judge-score delta and the overall average, which
    are noisy run-to-run and never gate.
    """
    if not baseline:
        return [
            f"### Regression diff vs baseline (`{BASELINE_FILE}`)",
            f"_No committed baseline found — record one with "
            f"`--out results/{BASELINE_FILE}` to enable regression diffs._",
            "",
        ]

    prev = {r["id"]: r for r in baseline.get("results", []) if "judge" in r}
    cur = {r["id"]: r for r in results if "judge" in r}

    regressed, fixed = [], []
    for tid in cur.keys() & prev.keys():
        if cur[tid]["hard_pass"] and not prev[tid]["hard_pass"]:
            fixed.append(tid)
        elif not cur[tid]["hard_pass"] and prev[tid]["hard_pass"]:
            failed = ", ".join(
                k for k, v in cur[tid]["hard_checks"].items() if not v
            )
            regressed.append(f"`{tid}` (failed: {failed})")

    added = sorted(cur.keys() - prev.keys())
    removed = sorted(prev.keys() - cur.keys())
    prev_summary = baseline.get("summary", {})

    lines = [
        f"### Regression diff vs baseline "
        f"(skill `{prev_summary.get('skill', '?')}`, "
        f"model `{prev_summary.get('model', '?')}`)",
    ]
    lines.append(
        f"- hard-check regressions ({len(regressed)}): " + ", ".join(regressed)
        if regressed else "- no hard-check regressions"
    )
    if fixed:
        lines.append(
            f"- hard-check fixes ({len(fixed)}): "
            + ", ".join(f"`{t}`" for t in fixed)
        )
    if added:
        lines.append(
            f"- new fixtures ({len(added)}): " + ", ".join(f"`{t}`" for t in added)
        )
    if removed:
        lines.append(
            f"- removed fixtures ({len(removed)}): "
            + ", ".join(f"`{t}`" for t in removed)
        )
    if "avg_judge_score" in prev_summary:
        delta = round(summary["avg_judge_score"] - prev_summary["avg_judge_score"], 2)
        lines.append(
            f"- judge avg {summary['avg_judge_score']} vs "
            f"{prev_summary['avg_judge_score']} (delta {delta:+}) "
            f"— advisory, judge is noisy run-to-run"
        )
    lines.append("")
    return lines


def _stats(results):
    scored = [r for r in results if "judge" in r]
    n = max(len(scored), 1)
    activated = [r for r in results if r.get("activation", {}).get("ok") is True]
    skipped = [r for r in results if r.get("activation", {}).get("skipped") is True]
    verify_checked = [r for r in results if r.get("verify", {}).get("catalog_checked")]
    verify_clean = [
        r for r in results
        if "verify" in r and r["verify"]["catalog_checked"] and r["verify"]["hard_count"] == 0
    ]
    # Over ALL fixtures with a verify result, not just catalog_checked ones
    # — the network-free invariants (runtime installed, leaf-datastore
    # served, vars literal, hook non-mutation) run regardless of catalog
    # availability, and this rate is the one place that signal surfaces
    # even though it never gates (see README's "Why verify.py is advisory
    # in the harness" for the reasoning).
    verify_results = [r for r in results if "verify" in r]
    verify_hard_violation_rate = (
        round(sum(1 for r in verify_results if r["verify"]["hard_count"] > 0)
              / len(verify_results), 3)
        if verify_results else None
    )
    return {
        "n": len(scored),
        "hard_pass_rate": round(sum(r["hard_pass"] for r in scored) / n, 3),
        "avg_judge_score": round(sum(r["judge"]["score"] for r in scored) / n, 2),
        "judge_correct_rate": round(sum(bool(r["judge"]["correct"]) for r in scored) / n, 3),
        "activation_ok": len(activated),
        "activation_skipped": len(skipped),
        # verify.py (AI-461) — advisory, same reason activation is advisory:
        # the catalog leg needs live flox+network. verify_checked counts
        # fixtures where that leg actually ran; verify_clean is the subset
        # with zero HARD violations. verify_hard_violation_rate is the
        # headline number to watch for a sustained regression even though
        # nothing here gates the build.
        "verify_checked": len(verify_checked),
        "verify_clean": len(verify_clean),
        "verify_hard_violation_rate": verify_hard_violation_rate,
    }


# --- efficiency-axis aggregation (AI-442 §1.1) --------------------------------

def _median(values):
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def _percentile(values, p):
    """Linear-interpolation percentile (0<=p<=1) over `values` (need not
    be pre-sorted). A single-element sample returns that value for any
    p — the AI-438 n>=5 policy means this is rarely hit for real batches,
    but a censored slice can still be this small and must not raise."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    idx = p * (len(s) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return s[lo] + (s[hi] - s[lo]) * frac


_TERMINAL_DISPOSITIONS = ("verified", "failed-verify", "unverifiable-env", "agent-error")


def _efficiency_summary(results):
    """Censoring-aware efficiency aggregation for one (fixture, arm)
    slice of `results` (AI-442 §1.1). Per-fixture DISTRIBUTIONS only —
    median/p25-p75/n, never a pooled mean or a single scalar (AI-442 Q5)
    — and NEVER cost/turns/tokens pooled across terminal dispositions.
    That censoring rule is the highest-value thing in this function to
    get right: an unconditioned mean would let a giving-up arm look
    cheap (a rep that never verified still "spent" tokens finding that
    out, but reporting that spend as if it bought a working result is
    exactly the lie censoring exists to prevent).

    Terminal dispositions, from the design doc's censoring table:
      verified          — feeds verify_rate (numerator) AND is the ONLY
                           disposition whose cost/turns/tokens/tool-calls
                           feed the "to verify" distributions
      failed-verify      — feeds verify_rate (denominator) only; its
                           spend is right-censored into `unverified_spend`
                           ("spent at least this much and never arrived"),
                           never pooled with verified cost
      unverifiable-env   — DROPPED entirely: a missing observation (flox
                           absent / harness error / --skip-activation),
                           not a failure — keeps verify_rate honest
      agent-error        — DROPPED entirely: claude call failed, no
                           manifest produced
    """
    by_disposition = {d: [] for d in _TERMINAL_DISPOSITIONS}
    other = []
    for r in results:
        d = r.get("terminal_disposition")
        if d in by_disposition:
            by_disposition[d].append(r)
        else:
            other.append(r)

    verified = by_disposition["verified"]
    failed = by_disposition["failed-verify"]

    denom = len(verified) + len(failed)
    verify_rate = round(len(verified) / denom, 3) if denom else None

    def _field(records, *path):
        out = []
        for r in records:
            v = r
            for key in path:
                if not isinstance(v, dict) or key not in v:
                    v = None
                    break
                v = v[key]
            if v is not None:
                out.append(v)
        return out

    turns = _field(verified, "num_turns", "agent")
    tool_total = _field(verified, "tool_calls", "agent", "total")
    tool_search = _field(verified, "tool_calls", "agent", "flox_search")
    tool_show = _field(verified, "tool_calls", "agent", "flox_show")
    output_tokens = _field(verified, "usage", "agent", "output_tokens")
    cache_read_tokens = _field(verified, "usage", "agent", "cache_read_input_tokens")
    verified_cost = _field(verified, "cost", "total_usd")
    unverified_cost = _field(failed, "cost", "total_usd")

    def _round_or_none(x, digits=4):
        return round(x, digits) if x is not None else None

    return {
        "reps": len(results),
        "env_skipped": len(by_disposition["unverifiable-env"]),
        "agent_errors": len(by_disposition["agent-error"]),
        "other_disposition": len(other),
        "verify_rate": verify_rate,
        "turns_to_verify": {
            "median": _median(turns), "p25": _percentile(turns, 0.25),
            "p75": _percentile(turns, 0.75), "n": len(turns),
        },
        "tool_calls_to_verify": {
            "median_total": _median(tool_total),
            "median_flox_search": _median(tool_search),
            "median_flox_show": _median(tool_show),
            "n": len(tool_total),
        },
        "tokens_to_verify": {
            "median_output": _median(output_tokens),
            "median_cache_read": _median(cache_read_tokens),
            "n": len(output_tokens),
        },
        "cost_to_verify": {
            "median_usd": _round_or_none(_median(verified_cost)), "n": len(verified_cost),
        },
        "unverified_spend": {
            "median_usd": _round_or_none(_median(unverified_cost)), "n": len(unverified_cost),
        },
    }


def _vacuous_run_message(results):
    """AI-463 I1(a): a run where EVERY task errored (e.g. `--only lemmy
    --tasks tier2.jsonl` — every id in that file is a real-repo entry with
    no local fixtures/<id> directory) produces a results.json with
    nothing in it worth reporting, yet exits 0 like a genuine measurement
    run. Returns the hint to print before exiting nonzero, or None if at
    least one task actually ran and got scored.

    Every result dict carries either "error" (an early-return failure) or
    "judge" (full happy-path completion) — never neither — so "no result
    has judge" is exactly "every task errored," not an approximation of
    it. A run where SOME tasks errored among others that scored fine
    returns None here — that's the existing record-error-and-continue
    discipline, not this failure mode.
    """
    if not results or any("judge" in r for r in results):
        return None
    errors = [(r["id"], r.get("error", "?")) for r in results]
    return (
        f"ERROR: 0 of {len(results)} task(s) actually ran (all errored) — "
        f"nothing to report. If these are real-repo entries, they belong "
        f"in tier2.jsonl and run via tier2.py, not this script. "
        f"Errors: {errors}"
    )


def _gate_should_fail(binding, bad, errs):
    """AI-463 I1(b): --gate must fail when `binding` (the scored should-
    tier fixtures) is EMPTY, not just when some of them failed. "GATE
    PASSED: all 0 should-tier fixtures pass hard-checks" is vacuous truth
    — the same failure class as the golden-lint vacuous-pass PR #42
    fixed (a check that can't find anything to check is not evidence of
    correctness). A run whose should-tier subset genuinely ran and
    passed still returns False here, same as before.
    """
    return bool(bad) or bool(errs) or not binding


# --- main ---------------------------------------------------------------------

def main():
    global MODEL

    ap = argparse.ArgumentParser(
        description="Flox /floxify skill eval harness (outcome-based)"
    )
    ap.add_argument(
        "--skill-dir",
        default=str(DEFAULT_SKILL_DIR),
        help=(
            "Path to the flox plugin directory containing the floxify skill "
            f"(default: {DEFAULT_SKILL_DIR}, the in-repo flox-plugin/)."
        ),
    )
    ap.add_argument("--model", default=MODEL, help=f"Claude model (default {MODEL})")
    ap.add_argument(
        "--tasks",
        default=str(HERE / "tasks.jsonl"),
        help="Path to tasks.jsonl (default: tasks.jsonl alongside this script)",
    )
    ap.add_argument(
        "--only",
        help=(
            "Run one or more fixture ids, comma-separated (e.g. node-20 or "
            "ruby,python-uv,node-postgres,rust-cargo,go-mod)"
        ),
    )
    ap.add_argument(
        "--gate",
        action="store_true",
        help="Exit non-zero if any should-tier hard-check fails",
    )
    ap.add_argument(
        "--out",
        help="Output filename under results/ (default: floxify-<timestamp>.json)",
    )
    ap.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="Parallel claude calls (default 2; lower if you hit rate limits)",
    )
    ap.add_argument(
        "--skip-activation",
        action="store_true",
        help="Skip flox activate verification (records as skipped, not failed)",
    )
    ap.add_argument(
        "--activation-timeout",
        type=int,
        default=DEFAULT_ACTIVATION_TIMEOUT,
        help=(
            f"Seconds allowed for `flox activate` (default "
            f"{DEFAULT_ACTIVATION_TIMEOUT}). Exceeding it is recorded as a "
            f"FAILURE, not a skip — raise this if the budget is wrong for the "
            f"fixture rather than reading a timeout as 'unchecked'."
        ),
    )
    ap.add_argument(
        "--baseline",
        default=BASELINE_FILE,
        help=(
            f"Committed results/<file> to diff against for regression "
            f"detection (default: {BASELINE_FILE}). Reports per-fixture "
            "hard-check flips + judge delta."
        ),
    )
    ap.add_argument(
        "--arm",
        choices=["skills", "baseline"],
        default="skills",
        help=(
            "AI-442: which arm to run. 'skills' loads the floxify plugin "
            "(the harness's only mode before AI-442); 'baseline' omits "
            "--plugin-dir for the unassisted-model efficiency comparison. "
            "Both arms get the IDENTICAL tool surface (Bash Read Write "
            "Edit Skill, AI-442 Q7) -- 'baseline' simply has no skill to "
            "invoke. Not the same flag as --baseline above (the "
            "regression-diff file), which keeps its pre-existing, "
            "unrelated meaning."
        ),
    )
    ap.add_argument(
        "--reps",
        type=int,
        default=1,
        help=(
            "AI-442: repetitions per (fixture, arm) — default 1. Tier 2 "
            "already has --reps natively; this brings Tier 1 to parity "
            "for the efficiency axis's n>=5 distribution policy (AI-438). "
            "Note: --baseline's regression-diff and the CI step summary's "
            "per-fixture table both key on fixture id and are not "
            "rep-aware — with --reps > 1 they reflect only the LAST rep "
            "per fixture, which is fine for the reps=1 CI/gate path this "
            "flag defaults to, but not a multi-rep report in its own "
            "right (read the JSON's per-rep records directly for that)."
        ),
    )
    args = ap.parse_args()

    MODEL = args.model
    skill_dir = Path(args.skill_dir).resolve()

    if not skill_dir.exists():
        print(
            f"ERROR: skill-dir not found: {skill_dir}\n"
            "The floxify skill ships in this repo at "
            "flox-plugin/skills/floxify/ — check your checkout, or pass "
            "--skill-dir to point at an alternate flox-plugin directory.",
            file=sys.stderr,
        )
        sys.exit(1)

    tasks_path = Path(args.tasks)
    tasks = [
        json.loads(line)
        for line in tasks_path.read_text().splitlines()
        if line.strip()
    ]
    if args.only:
        # AI-442 Q3: one or more ids, comma-separated -- selects exactly
        # the batch fixtures (e.g. ruby,python-uv,node-postgres,
        # rust-cargo,go-mod) without five separate invocations.
        only_ids = [s.strip() for s in args.only.split(",") if s.strip()]
        tasks = [t for t in tasks if t["id"] in only_ids]
        found_ids = {t["id"] for t in tasks}
        missing = [i for i in only_ids if i not in found_ids]
        if missing:
            print(f"ERROR: no task with id(s): {', '.join(missing)}", file=sys.stderr)
            sys.exit(1)

    # Output path computed BEFORE running tasks (not after, as it used to
    # be) -- AI-442 needs it early to derive the raw-stream persistence
    # directory, keyed to the exact run that produced the streams so
    # they stay discoverable from the summary file's own name.
    out_name = args.out or f"floxify-{int(time.time())}.json"
    if os.path.isabs(out_name):
        out_path = Path(out_name)
    elif os.path.dirname(out_name):
        out_path = HERE / out_name
    else:
        out_path = HERE / "results" / out_name
    stream_dir = out_path.parent / "streams" / out_path.stem

    reps = max(args.reps, 1)
    task_reps = [(t, rep) for t in tasks for rep in range(1, reps + 1)]

    concurrency = min(args.concurrency, len(task_reps)) or 1
    print(
        f"running {len(tasks)} fixture(s) x {reps} rep(s) = "
        f"{len(task_reps)} run(s) at concurrency {concurrency} "
        f"(skill-dir: {skill_dir}, arm: {args.arm}) ...",
        flush=True,
    )

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(
            pool.map(
                lambda tr: process_task(
                    tr[0], skill_dir, skip_activation=args.skip_activation,
                    activation_timeout=args.activation_timeout,
                    arm=args.arm, rep=tr[1], stream_dir=stream_dir,
                ),
                task_reps,
            )
        )

    scored = [r for r in results if "judge" in r]
    costed = [r["cost"] for r in results if "cost" in r]
    agent_cost = sum(c.get("agent_usd", 0.0) for c in costed)
    judge_cost = sum(c.get("judge_usd", 0.0) for c in costed)
    total_cost = sum(c.get("total_usd", 0.0) for c in costed)
    summary = {
        "skill": _skill_identity(skill_dir),
        "model": MODEL,
        "arm": args.arm,
        "reps": reps,
        "n_tasks": len(results),
        "n_errors": sum(1 for r in results if "error" in r),
        **_stats(results),
        "by_tier": {
            tier: _stats([r for r in results if r["tier"] == tier and "judge" in r])
            for tier in ("should", "may", "stretch")
            if any(r["tier"] == tier and "judge" in r for r in results)
        },
        # AI-459-style cost rollup, ported to the agentic path (AI-442).
        "cost": {
            "total_usd": round(total_cost, 4),
            "agent_usd": round(agent_cost, 4),
            "judge_usd": round(judge_cost, 4),
            "mean_per_task_usd": round(total_cost / len(costed), 4) if costed else 0.0,
            "n_costed_tasks": len(costed),
        },
        # AI-442 §1.1: per-fixture censored efficiency distributions,
        # this run's arm only -- never a pooled cross-fixture scalar
        # (Q5). Nested by arm (even though a single run has just one) so
        # a caller merging a separate skills-arm and baseline-arm run
        # for the two-arm comparison gets a stable, mergeable shape.
        "efficiency": {
            fixture_id: {
                args.arm: _efficiency_summary(
                    [r for r in results if r.get("id") == fixture_id]
                )
            }
            for fixture_id in sorted({r["id"] for r in results})
        },
    }

    # Snapshot the baseline BEFORE writing output — otherwise a run that
    # writes to results/floxify-baseline.json would overwrite its own
    # comparison target and the diff would always be empty.
    baseline = _read_baseline(args.baseline)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"summary": summary, "results": results}, indent=2))

    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))
    print(f"written: {out_path}")

    diff_lines = _diff_vs_baseline(summary, results, baseline)
    print("\n=== REGRESSION DIFF ===")
    print("\n".join(diff_lines))

    # AI-463 I1(a): a run where EVERY task errored (results written above
    # for visibility/debugging) has nothing to report — exit nonzero
    # unconditionally, not just under --gate. A per-task rejection among
    # a larger run (some scored, some errored) is unaffected — see
    # _vacuous_run_message's docstring.
    vacuous_message = _vacuous_run_message(results)
    if vacuous_message:
        print(f"\n{vacuous_message}", file=sys.stderr)
        sys.exit(1)

    # Gate: hard-checks on should-tier tasks bind when --gate is set.
    # Judge score and activation are advisory — reported, never block.
    binding = [r for r in scored if r["tier"] == "should"]
    bad = [r for r in binding if not r["hard_pass"]]
    errs = [r for r in results if "error" in r and r.get("tier") == "should"]

    _write_step_summary(summary, results, binding, bad, errs, args.gate, diff_lines)

    if args.gate and _gate_should_fail(binding, bad, errs):
        if not binding:
            # AI-463 I1(b): zero should-tier fixtures ran at all — a gate
            # over nothing is not a pass. See _gate_should_fail.
            print(
                "\nGATE FAILED: 0 should-tier fixtures ran — a gate over "
                "zero fixtures is vacuous truth, not a pass",
                file=sys.stderr,
            )
        else:
            failed_ids = [r["id"] for r in bad]
            failed_checks = {
                r["id"]: [k for k, v in r["hard_checks"].items() if not v] for r in bad
            }
            print(
                f"\nGATE FAILED: {len(bad)} should-tier fixture(s) failed hard-checks: "
                f"{failed_checks}; errors: {[r['id'] for r in errs]}",
                file=sys.stderr,
            )
        sys.exit(1)
    if args.gate:
        print(
            f"\nGATE PASSED: all {len(binding)} should-tier fixtures pass hard-checks. "
            f"(advisory: judge correct {summary['judge_correct_rate']}, "
            f"avg {summary['avg_judge_score']}, "
            f"activation ok {summary['activation_ok']}/"
            f"{summary['activation_ok'] + summary['activation_skipped']} checked)"
        )


def _write_step_summary(summary, results, binding, bad, errs, gate_enabled,
                        diff_lines=None):
    """Write a markdown report to $GITHUB_STEP_SUMMARY if running in CI."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return

    scored = [r for r in results if "judge" in r]
    if gate_enabled:
        verdict = "GATE FAILED" if (bad or errs) else "GATE PASSED"
    else:
        verdict = "measurement run (gate off)"

    # AI-442: mirrors run.py's own step-summary cost line (AI-459).
    cost = summary.get("cost") or {}
    cost_line = ""
    if cost.get("total_usd"):
        cost_line = (
            f" · **cost: ${cost['total_usd']:.2f}** "
            f"(agent ${cost.get('agent_usd', 0):.2f} + judge "
            f"${cost.get('judge_usd', 0):.2f}, "
            f"${cost.get('mean_per_task_usd', 0):.2f}/task)"
        )

    lines = [
        f"## /floxify skill evals — {verdict}",
        "",
        f"**Model:** `{summary.get('model', '?')}` · "
        f"**{summary['n_tasks']} fixtures** ({summary['n_errors']} errors) · "
        f"**skill:** `{summary.get('skill', '?')}`{cost_line}",
        "",
        "### Hard-check results",
        "| fixture | tier | hard | judge | activate | verify |",
        "|---|---|:--:|:--:|:--:|:--:|",
    ]
    for r in results:
        if "error" in r:
            lines.append(f"| {r['id']} | {r['tier']} | ERROR | — | — | — |")
            continue
        hp = "PASS" if r["hard_pass"] else "FAIL"
        js = f"{r['judge']['score']}/5"
        act = r.get("activation", {})
        a = "skipped" if act.get("skipped") else ("ok" if act.get("ok") else "FAIL")
        ver = r.get("verify")
        if ver is None:
            v = "—"
        elif ver["hard_count"] > 0:
            v = f"{ver['hard_count']} HARD"
        elif not ver["catalog_checked"]:
            v = "skipped"
        else:
            v = "clean"
        lines.append(f"| {r['id']} | {r['tier']} | {hp} | {js} | {a} | {v} |")

    verify_rate = summary.get("verify_hard_violation_rate")
    verify_rate_str = f"{verify_rate:.0%}" if verify_rate is not None else "n/a"
    lines += [
        "",
        f"**hard-pass rate:** {summary['hard_pass_rate']:.0%} · "
        f"**avg judge:** {summary['avg_judge_score']:.2f}/5 · "
        f"**judge-correct:** {summary['judge_correct_rate']:.0%} · "
        f"**verify hard-violation rate:** {verify_rate_str}",
        "",
        "_Activation and verify.py are both advisory — recorded as skipped "
        "when flox/network is unavailable, never gating the build. See "
        "README's \"Why verify.py is advisory in the harness\" for why._",
    ]

    if bad or errs:
        lines += ["", "### Failures"]
        for r in bad:
            failed = ", ".join(k for k, v in r["hard_checks"].items() if not v)
            lines.append(f"- `{r['id']}`: hard-check failed — {failed}")
        for r in errs:
            lines.append(f"- `{r['id']}`: run error — {r['error'][:80]}")

    if diff_lines:
        lines += ["", *diff_lines]

    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
