#!/usr/bin/env python3
"""Flox /floxify skill eval harness — Tier 2, real OSS conversion repos.

Tier 1 (run_floxify.py) copies small synthetic fixtures from disk and grades
the manifest against a hand-tuned gold TOML. Tier 2 fixtures are real
open-source repos — too large to vendor and too heavy to fully `flox
activate` — so this harness differs in two ways:

  1. Fixtures are shallow-cloned at a pinned SHA (see tier2.jsonl), not
     copied from evals/floxify/fixtures/.
  2. The primary check is structural conformance, derived per-entry from
     the registry's expected_runtimes/expected_services: does the produced
     manifest pin the right runtimes and wire the right services. There is
     no gold TOML to diff against — the LLM judge grades against a textual
     characterization instead (registry `gold` field).

Activation is off by default (`--activate` to opt in) and is always
advisory. This tier never gates the build — it is report-only, run
manually or on a schedule, and intended to surface real-world regressions
(e.g. a weak ecosystem like Ruby) that the small Tier 1 fixtures can't.
Its activation budget defaults to 1800s (`--activation-timeout`): these
environments realize a full closure on first activation, and the Tier 1
budget of 120s silently recorded posthog as "skipped" rather than measuring
it. A timeout is now a FAILURE, not a skip.

Reuses `_run_claude_agent`, `_is_valid_toml`, `_check_activation`,
`_run_judge`, `_stats`, `_skill_identity`, and `DEFAULT_SKILL_DIR` from
run_floxify.py rather than duplicating that machinery.

Usage:
    python3 tier2.py --only mastodon             # single repo
    python3 tier2.py                              # all registered repos
    python3 tier2.py --activate                   # opt in to flox activate
    python3 tier2.py --skill-dir /path/to/flox-plugin
    python3 tier2.py --out results/my-run.json

Pure stdlib — no additional packages required.
"""
import argparse
import json
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from run_floxify import (
    ABS_PATH_IN_MANIFEST,
    DEFAULT_SKILL_DIR,
    DEFAULT_ACTIVATION_TIMEOUT,
    MODEL,
    _check_activation,
    _is_valid_toml,
    _run_claude_agent,
    _run_judge,
    _skill_identity,
    _stats,
)

HERE = Path(__file__).resolve().parent

# Tier 2 repos are heavy by definition — a first activation realizes an
# entire closure (posthog: 33 packages incl. rust/go/emscripten). The Tier 1
# budget of 120s is not a sane default here; it silently produced 'skipped'
# on the largest repo in the corpus (AI-454).
TIER2_ACTIVATION_TIMEOUT = 1800


# --- git clone-at-SHA (fallback chain) -----------------------------------

def _run_git(args, timeout):
    """Run a git command, returning (ok, error_snippet)."""
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "unknown error").strip()
        return False, detail[:300]
    return True, ""


def _try_direct_fetch(url, sha, dest, timeout):
    """Fetch only the pinned commit — cheapest, but most hosts (incl.
    GitHub) reject fetching an arbitrary SHA that isn't advertised as a ref
    tip for public repos, so this is expected to fail more often than not.
    """
    ok, err = _run_git(["git", "init", "-q", str(dest)], 30)
    if not ok:
        return err
    ok, err = _run_git(["git", "-C", str(dest), "remote", "add", "origin", url], 30)
    if not ok:
        return err
    ok, err = _run_git(
        ["git", "-C", str(dest), "fetch", "--depth", "1", "origin", sha], timeout
    )
    if not ok:
        return err
    ok, err = _run_git(["git", "-C", str(dest), "checkout", "-q", "FETCH_HEAD"], 60)
    return None if ok else err


def _try_partial_clone(url, sha, dest, timeout):
    """Partial clone (blob:none) — full commit graph, blobs deferred until
    checkout. Much cheaper than a full clone for large repos and works
    against any host that supports partial clone (GitHub does).
    """
    ok, err = _run_git(
        ["git", "clone", "--filter=blob:none", "--no-checkout", url, str(dest)],
        timeout,
    )
    if not ok:
        return err
    ok, err = _run_git(["git", "-C", str(dest), "checkout", "-q", sha], timeout)
    return None if ok else err


def _try_full_clone(url, sha, dest, timeout):
    """Last resort — full clone including all blob history."""
    ok, err = _run_git(["git", "clone", url, str(dest)], timeout)
    if not ok:
        return err
    ok, err = _run_git(["git", "-C", str(dest), "checkout", "-q", sha], timeout)
    return None if ok else err


def _clone_at_sha(url, sha, dest, timeout=900):
    """Clone `url` and check out `sha` into `dest`.

    Tries three strategies in increasing order of cost: a direct fetch of
    the pinned commit, a partial clone that defers blob content until
    checkout, and a full clone. Returns None on success, or a combined
    error string describing why every strategy failed (the caller records
    this as a per-entry error rather than crashing the run).
    """
    dest = Path(dest)
    strategies = [
        ("direct-fetch", _try_direct_fetch, timeout),
        ("partial-clone", _try_partial_clone, timeout),
        ("full-clone", _try_full_clone, timeout * 3),
    ]
    errors = []
    for name, fn, per_attempt_timeout in strategies:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
        err = fn(url, sha, dest, per_attempt_timeout)
        if err is None:
            return None
        errors.append(f"{name}: {err}")
    return "clone failed — " + "; ".join(errors)


# --- structural conformance checks (data-driven from the registry) -------
# Runtime patterns are matched against the pkg-path *value*, anchored on
# both quotes (same technique as Tier 1's PIN_* regexes) so an unrelated
# ecosystem tool cannot satisfy the check — e.g. a "ruby" pin must not be
# satisfied by rubyPackages.rubocop.

def _runtime_pinned(manifest_text, pattern):
    if manifest_text is None:
        return False
    return bool(re.search(r'pkg-path = "' + pattern + r'"', manifest_text))


_SERVICE_HEADER = re.compile(r"^\[services\.[^\]]*\]", re.M)


def _service_present(manifest_text, service_name):
    """True if any [services.*] header contains `service_name` as a
    substring (case-insensitive) — covers naming variants like postgres/
    postgresql without requiring an exact section name."""
    if manifest_text is None:
        return False
    return any(
        service_name.lower() in header.lower()
        for header in _SERVICE_HEADER.findall(manifest_text)
    )


def _structural_checks(entry, manifest_text):
    """Per-entry hard-checks derived from the registry's expected_runtimes/
    expected_services, rather than Tier 1's fixed CHECKS dict — each
    real-world repo pins different runtimes and wires different services.
    """
    checks = {
        "manifest_created": manifest_text is not None,
        "valid_toml": _is_valid_toml(manifest_text),
        "no_abs_paths": (
            manifest_text is not None
            and not ABS_PATH_IN_MANIFEST.search(manifest_text)
        ),
    }
    for runtime in entry.get("expected_runtimes", []):
        checks[f"pins_{runtime['name']}"] = _runtime_pinned(
            manifest_text, runtime["pattern"]
        )
    for service in entry.get("expected_services", []):
        checks[f"has_service_{service}"] = _service_present(manifest_text, service)
    return checks


# --- Tier 2 LLM judge ------------------------------------------------------
# Tier 1's _judge diffs the produced manifest against a hand-tuned gold
# TOML file. Tier 2 has no gold manifest for these repos — the reference is
# a textual characterization (registry `gold` field: expected runtimes,
# services, and notes). This reuses _run_judge (the bare claude invocation)
# with a conformance-focused rubric and the same JSON-response parsing
# pattern as Tier 1's _judge.

GOLD_DIR = HERE / "testdata" / "gold"


def _golden_manifest(entry_id):
    """A hand-curated, catalog-verified reference manifest for this repo, if
    one has been captured under testdata/gold/<id>.toml. It is an *idiomatic*
    reference (right runtimes/services, correct hook idioms), not an exact-match
    target — a well-structured produced manifest may legitimately differ."""
    path = GOLD_DIR / f"{entry_id}.toml"
    return path.read_text() if path.exists() else None


def _judge_tier2(entry, manifest_text):
    """Grade produced manifest vs the registry's gold characterization, plus a
    concrete golden reference manifest when one exists (testdata/gold/<id>.toml)."""
    gold = entry.get("gold", {})
    gold_manifest = _golden_manifest(entry["id"])
    reference_block = (
        "REFERENCE golden manifest — a catalog-verified, idiomatic reference "
        "(NOT an exact-match target; a well-structured manifest may differ in "
        "layout, comments, or hook style):\n"
        f"```toml\n{gold_manifest}\n```\n\n"
        if gold_manifest else ""
    )
    prompt = (
        "You are grading a Flox manifest produced by an AI agent that onboards "
        "a large real-world open-source project to Flox. This is NOT an "
        "exact-match comparison — grade for structural conformance and "
        "idiomatic Flox usage on a big, real repo.\n\n"
        f"REPO: {entry['id']} ({entry['repo_url']} @ {entry['sha']})\n"
        f"RUBRIC: {entry.get('rubric', '')}\n\n"
        f"EXPECTED RUNTIMES: {gold.get('runtimes', 'unknown')}\n"
        f"EXPECTED SERVICES: {gold.get('services', 'unknown')}\n"
        f"NOTES: {gold.get('notes', '')}\n\n"
        f"{reference_block}"
        f"PRODUCED manifest:\n```toml\n{manifest_text or '(manifest not produced)'}\n```\n\n"
        "Grade 1-5 on:\n"
        "  1. Runtime conformance — pins the expected runtime(s) at a "
        "reasonable version, not a substitute or generic fallback\n"
        "  2. Service wiring — wires each expected service as a Flox "
        "[services.*] block with sane defaults\n"
        "  3. Idiomatic Flox usage — uses $FLOX_ENV_CACHE, no absolute "
        "paths, no hallucinated install URLs\n"
        "  4. Monorepo/workspace handling — resolves the primary runtime(s) "
        "rather than getting lost in subpackages\n\n"
        "Score 5 = fully conformant and idiomatic; 3 = right runtimes/"
        "services but missing idioms; 1 = wrong or missing runtimes/"
        "services.\n"
        'Return ONLY a JSON object: {"score": <int 1-5>, "correct": '
        '<true|false>, "issues": [<short strings>]}'
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


# --- registry --------------------------------------------------------------

def _load_registry(path):
    return [
        json.loads(line)
        for line in Path(path).read_text().splitlines()
        if line.strip()
    ]


def _base(entry):
    return {"id": entry["id"], "repo_url": entry["repo_url"], "sha": entry["sha"]}


# --- per-entry runner --------------------------------------------------------

def process_entry(entry, skill_dir, activate=False, clone_timeout=900,
                  agent_timeout=1800, activation_timeout=TIER2_ACTIVATION_TIMEOUT):
    """Clone the repo at its pinned SHA, run /floxify against it, and score
    the produced manifest with structural conformance + LLM judge."""
    tmpdir = tempfile.mkdtemp(prefix=f"floxify-tier2-{entry['id']}-")
    try:
        print(f"  {entry['id']}: cloning {entry['repo_url']} @ {entry['sha']} ...", flush=True)
        clone_err = _clone_at_sha(
            entry["repo_url"], entry["sha"], tmpdir, timeout=clone_timeout
        )
        if clone_err:
            print(f"  {entry['id']}: ERROR {clone_err}", flush=True)
            return {**_base(entry), "error": clone_err}

        tmp = Path(tmpdir)
        prompt = (
            f"/floxify {tmpdir}\n\n"
            "This is a large real-world open-source repository (not a small "
            "fixture) — take the time to scan it thoroughly: check version "
            "pin files (.ruby-version, .nvmrc, .python-version, go.mod, "
            "pyproject.toml), workspace/monorepo config, and "
            "docker-compose.yml for services. Run non-interactively: "
            "complete all phases (scan project files, resolve packages in "
            "the Flox catalog, write .flox/env/manifest.toml). Do not ask "
            "for or wait for user input — produce the best manifest you "
            "can and stop after writing it."
        )

        print(f"  {entry['id']}: invoking skill (this may take a while) ...", flush=True)
        agent_out, agent_err = _run_claude_agent(prompt, skill_dir, timeout=agent_timeout)

        if agent_err:
            print(f"  {entry['id']}: agent error: {agent_err}", flush=True)
            return {**_base(entry), "error": agent_err}

        manifest_path = tmp / ".flox" / "env" / "manifest.toml"
        manifest_text = (
            manifest_path.read_text(encoding="utf-8")
            if manifest_path.exists()
            else None
        )

        hard = _structural_checks(entry, manifest_text)
        hard_pass = all(hard.values())

        if activate:
            act_ok, act_skipped, act_notes = _check_activation(
                tmp, timeout=activation_timeout
            )
        else:
            act_ok, act_skipped, act_notes = (
                None,
                True,
                "--activate not set (Tier 2 activation is opt-in — these "
                "dev envs are too heavy to reliably activate)",
            )

        verdict = _judge_tier2(entry, manifest_text)

        status = "PASS" if hard_pass else "FAIL"
        act_str = "skipped" if act_skipped else ("ok" if act_ok else "FAIL")
        print(
            f"  {entry['id']}: hard={status}  judge={verdict['score']}/5  "
            f"activate={act_str}",
            flush=True,
        )

        return {
            **_base(entry),
            "hard_checks": hard,
            "hard_pass": hard_pass,
            "activation": {"ok": act_ok, "skipped": act_skipped, "notes": act_notes},
            "judge": verdict,
            "manifest_excerpt": (manifest_text or "")[:3000],
            "agent_output_excerpt": (agent_out or "")[:800],
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def process_task(entry, skill_dir, reps=1, activate=False, clone_timeout=900,
                  agent_timeout=1800, activation_timeout=TIER2_ACTIVATION_TIMEOUT):
    """Run `reps` repetitions of an entry. A single rep returns the plain
    per-entry result (dashboard-compatible with a Tier-1-shaped result);
    multiple reps return an aggregate with each run kept under "runs"."""
    runs = [
        process_entry(
            entry, skill_dir, activate=activate,
            clone_timeout=clone_timeout, agent_timeout=agent_timeout,
            activation_timeout=activation_timeout,
        )
        for _ in range(reps)
    ]
    if reps == 1:
        return runs[0]
    hard_passes = [r["hard_pass"] for r in runs if "error" not in r]
    return {
        **_base(entry),
        "reps": reps,
        "runs": runs,
        "hard_pass_rate_across_reps": (
            round(sum(hard_passes) / len(hard_passes), 3) if hard_passes else None
        ),
    }


# --- summary --------------------------------------------------------------

def _flatten_runs(results):
    """Flatten per-repo results to a flat list of individual runs.

    A reps>1 entry is an aggregate carrying its individual runs under
    "runs" and no top-level "judge" key; a reps==1 entry is itself a
    single run. `_stats` only counts entries with a "judge" key, so an
    unflattened aggregate would be silently dropped and the summary would
    report all-zeros even when every run passed. Flattening makes every
    run visible to the stats regardless of reps.
    """
    return [run for entry in results for run in entry.get("runs", [entry])]


def _summarize(results, skill_id):
    flat = _flatten_runs(results)
    return {
        "skill": skill_id,
        "model": MODEL,
        "n_repos": len(results),
        "n_errors": sum(1 for r in flat if "error" in r),
        **_stats(flat),
    }


# --- main ---------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Flox /floxify Tier 2 eval harness (real OSS conversion repos)"
    )
    ap.add_argument(
        "--skill-dir",
        default=str(DEFAULT_SKILL_DIR),
        help=(
            "Path to the flox plugin directory containing the floxify skill "
            f"(default: {DEFAULT_SKILL_DIR}, the in-repo flox-plugin/)."
        ),
    )
    ap.add_argument(
        "--registry",
        default=str(HERE / "tier2.jsonl"),
        help="Path to tier2.jsonl (default: tier2.jsonl alongside this script)",
    )
    ap.add_argument("--only", help="Run a single registered repo id (e.g. mastodon)")
    ap.add_argument(
        "--reps", type=int, default=1,
        help="Repetitions per repo (default 1; >1 aggregates hard-pass rate)",
    )
    ap.add_argument(
        "--out", default="tier2.json",
        help="Output filename under results/ (default: results/tier2.json)",
    )
    ap.add_argument(
        "--activate", action="store_true",
        help=(
            "Opt in to `flox activate` verification (off by default — Tier "
            "2 dev envs are too heavy to reliably activate in CI)"
        ),
    )
    ap.add_argument(
        "--concurrency", type=int, default=1,
        help="Parallel repo runs (default 1 — clones + skill runs are heavy)",
    )
    ap.add_argument(
        "--clone-timeout", type=int, default=900,
        help="Seconds allowed per clone/checkout attempt (default 900)",
    )
    ap.add_argument(
        "--agent-timeout", type=int, default=1800,
        help="Seconds allowed for the /floxify skill run (default 1800)",
    )
    ap.add_argument(
        "--activation-timeout", type=int, default=TIER2_ACTIVATION_TIMEOUT,
        help=(
            f"Seconds allowed for `flox activate` (default "
            f"{TIER2_ACTIVATION_TIMEOUT}; Tier 2 first activations realize a "
            f"full closure). Exceeding it is recorded as a FAILURE, not a skip."
        ),
    )
    args = ap.parse_args()

    skill_dir = Path(args.skill_dir).resolve()
    if not skill_dir.exists():
        print(
            f"ERROR: skill-dir not found: {skill_dir}\n"
            "The floxify skill ships in this repo at "
            "flox-plugin/skills/floxify/ — check your checkout, or pass "
            "--skill-dir to point at an alternate flox-plugin directory.",
        )
        raise SystemExit(1)

    entries = _load_registry(args.registry)
    if args.only:
        entries = [e for e in entries if e["id"] == args.only]
        if not entries:
            print(f"ERROR: no registry entry with id '{args.only}'")
            raise SystemExit(1)

    concurrency = min(args.concurrency, len(entries)) or 1
    print(
        f"running {len(entries)} repo(s) at concurrency {concurrency} "
        f"(skill-dir: {skill_dir}) ...",
        flush=True,
    )

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(
            pool.map(
                lambda e: process_task(
                    e, skill_dir, reps=args.reps, activate=args.activate,
                    clone_timeout=args.clone_timeout,
                    agent_timeout=args.agent_timeout,
                    activation_timeout=args.activation_timeout,
                ),
                entries,
            )
        )

    summary = _summarize(results, _skill_identity(skill_dir))

    out_name = args.out
    if Path(out_name).is_absolute():
        out_path = Path(out_name)
    elif Path(out_name).parent != Path("."):
        out_path = HERE / out_name
    else:
        out_path = HERE / "results" / out_name
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"summary": summary, "results": results}, indent=2))

    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))
    print(f"written: {out_path}")


if __name__ == "__main__":
    main()
