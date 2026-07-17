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


# --- claude invocation --------------------------------------------------------

def _run_claude_agent(prompt, skill_dir, timeout=600, retries=2):
    """Invoke claude headlessly with the floxify skill and required tools.

    The floxify skill needs Bash (flox search/init/activate, ls, etc.),
    Read (project files), Write+Edit (manifest.toml), and Skill (to invoke
    the /floxify skill itself).
    """
    cmd = [
        "claude", "-p", prompt,
        "--model", MODEL,
        "--output-format", "json",
        "--allowedTools", "Bash", "Read", "Write", "Edit", "Skill",
        "--plugin-dir", str(skill_dir),
        "--strict-mcp-config",
    ]
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
                try:
                    return json.loads(proc.stdout).get("result", ""), None
                except json.JSONDecodeError:
                    last = f"BAD_JSON: {proc.stdout[:200]}"
        if attempt < retries - 1:
            time.sleep(3 + attempt * 4)
    return None, last


def _run_judge(prompt, timeout=120):
    """Run the judge call — bare model, no plugin."""
    cmd = [
        "claude", "-p", prompt,
        "--model", MODEL,
        "--output-format", "json",
        "--strict-mcp-config",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            return None, f"EXIT {proc.returncode}"
        return json.loads(proc.stdout).get("result", ""), None
    except subprocess.TimeoutExpired:
        return None, "TIMEOUT"
    except json.JSONDecodeError as exc:
        return None, f"BAD_JSON: {exc}"
    except Exception as exc:
        return None, str(exc)


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
    """Grade produced manifest vs gold — returns {score, correct, issues}."""
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
    result, err = _run_judge(prompt)
    if err:
        return {"score": 0, "correct": False, "issues": [f"judge error: {err}"]}
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
    }


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
    return {"id": task["id"], "tier": task["tier"], "ecosystem": task.get("ecosystem", "")}


def process_task(task, skill_dir, skip_activation=False,
                 activation_timeout=DEFAULT_ACTIVATION_TIMEOUT):
    """Copy fixture to temp dir, run the skill, score the result."""
    fixture_src = FIXTURES_DIR / task["id"]
    if not fixture_src.exists():
        print(f"  [{task['tier']}] {task['id']}: ERROR fixture not found", flush=True)
        return {**_base(task), "error": f"fixture not found: {fixture_src}"}

    with tempfile.TemporaryDirectory(prefix=f"floxify-eval-{task['id']}-") as tmpdir:
        # Copy fixture into temp dir.  No .flox/ in fixtures — skill creates it.
        shutil.copytree(str(fixture_src), tmpdir, dirs_exist_ok=True)
        tmp = Path(tmpdir)

        # Build the headless prompt.  The /floxify prefix invokes the skill by
        # name.  The non-interactive note prevents the skill from blocking on
        # "What would you like to do next?" in the Phase 4 menu.
        prompt = (
            f"/floxify {tmpdir}\n\n"
            "Run non-interactively: complete all phases (scan project files, "
            "resolve packages in the Flox catalog, write .flox/env/manifest.toml). "
            "Do not ask for or wait for user input — produce the best manifest you "
            "can and stop after writing it."
        )

        print(f"  [{task['tier']}] {task['id']}: invoking skill ...", flush=True)
        agent_out, agent_err = _run_claude_agent(prompt, skill_dir)

        if agent_err:
            print(
                f"  [{task['tier']}] {task['id']}: agent error: {agent_err}", flush=True
            )
            return {**_base(task), "error": agent_err}

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

        # Activation check (advisory — skipped when unavailable).
        if skip_activation:
            act_ok, act_skipped, act_notes = None, True, "--skip-activation flag set"
        else:
            act_ok, act_skipped, act_notes = _check_activation(
                tmp, timeout=activation_timeout
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
        verdict = _judge(task, manifest_text, verify_result=verify_result)

        status = "PASS" if hard_pass else "FAIL"
        act_str = "skipped" if act_skipped else ("ok" if act_ok else "FAIL")
        verify_str = f"{len(verify_hard)}H/{len(verify_advisory)}A"
        print(
            f"  [{task['tier']}] {task['id']}: "
            f"hard={status}  judge={verdict['score']}/5  activate={act_str}  "
            f"verify={verify_str}",
            flush=True,
        )

        return {
            **_base(task),
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
    ap.add_argument("--only", help="Run a single fixture id (e.g. node-20)")
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
        tasks = [t for t in tasks if t["id"] == args.only]
        if not tasks:
            print(f"ERROR: no task with id '{args.only}'", file=sys.stderr)
            sys.exit(1)

    concurrency = min(args.concurrency, len(tasks)) or 1
    print(
        f"running {len(tasks)} fixture(s) at concurrency {concurrency} "
        f"(skill-dir: {skill_dir}) ...",
        flush=True,
    )

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(
            pool.map(
                lambda t: process_task(
                    t, skill_dir, skip_activation=args.skip_activation,
                    activation_timeout=args.activation_timeout,
                ),
                tasks,
            )
        )

    scored = [r for r in results if "judge" in r]
    summary = {
        "skill": _skill_identity(skill_dir),
        "model": MODEL,
        "n_tasks": len(results),
        "n_errors": sum(1 for r in results if "error" in r),
        **_stats(results),
        "by_tier": {
            tier: _stats([r for r in results if r["tier"] == tier and "judge" in r])
            for tier in ("should", "may", "stretch")
            if any(r["tier"] == tier and "judge" in r for r in results)
        },
    }

    # Snapshot the baseline BEFORE writing output — otherwise a run that
    # writes to results/floxify-baseline.json would overwrite its own
    # comparison target and the diff would always be empty.
    baseline = _read_baseline(args.baseline)

    out_name = args.out or f"floxify-{int(time.time())}.json"
    if os.path.isabs(out_name):
        out_path = Path(out_name)
    elif os.path.dirname(out_name):
        # Has a directory component (e.g. results/foo.json) — relative to HERE
        out_path = HERE / out_name
    else:
        # Bare filename — place in results/
        out_path = HERE / "results" / out_name
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"summary": summary, "results": results}, indent=2))

    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))
    print(f"written: {out_path}")

    diff_lines = _diff_vs_baseline(summary, results, baseline)
    print("\n=== REGRESSION DIFF ===")
    print("\n".join(diff_lines))

    # Gate: hard-checks on should-tier tasks bind when --gate is set.
    # Judge score and activation are advisory — reported, never block.
    binding = [r for r in scored if r["tier"] == "should"]
    bad = [r for r in binding if not r["hard_pass"]]
    errs = [r for r in results if "error" in r and r.get("tier") == "should"]

    _write_step_summary(summary, results, binding, bad, errs, args.gate, diff_lines)

    if args.gate and (bad or errs):
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

    lines = [
        f"## /floxify skill evals — {verdict}",
        "",
        f"**Model:** `{summary.get('model', '?')}` · "
        f"**{summary['n_tasks']} fixtures** ({summary['n_errors']} errors) · "
        f"**skill:** `{summary.get('skill', '?')}`",
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
