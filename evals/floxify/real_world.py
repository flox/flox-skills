#!/usr/bin/env python3
"""Flox /floxify skill eval harness — real-world, real OSS conversion repos.

The synthetic tier (run_floxify.py) copies small fixtures from disk and grades
the manifest against a hand-tuned gold TOML. real-world fixtures are real
open-source repos — too large to vendor and too heavy to fully `flox
activate` — so this harness differs in two ways:

  1. Fixtures are shallow-cloned at a pinned SHA (see real-world.jsonl), not
     copied from evals/floxify/fixtures/.
  2. The primary check is structural conformance, derived per-entry from
     the registry's expected_runtimes/expected_services: does the produced
     manifest pin the right runtimes and wire the right services. There is
     no gold TOML to diff against — the LLM judge grades against a textual
     characterization instead (registry `gold` field).

Activation is off by default (`--activate` to opt in) and is always
advisory. This tier never gates the build — it is report-only, run
manually or on a schedule, and intended to surface real-world regressions
(e.g. a weak ecosystem like Ruby) that the small synthetic fixtures can't.
Its activation budget defaults to 1800s (`--activation-timeout`): these
environments realize a full closure on first activation, and the synthetic
budget of 120s silently recorded posthog as "skipped" rather than measuring
it. A timeout is now a FAILURE, not a skip.

Service probing is off by default too (`--services`, and only meaningful with
`--activate`). It exists because the three tiers below it can all be green
while the environment is useless: `has_service_postgres` matches a section
header, `valid_toml` parses the file, and `flox activate` proves the packages
resolve — none of them ever *run* the service command. `--services` starts the
services and asks each one for a connection.

Every clone is stripped of any in-tree `.flox/` before the conversion task
runs (AI-469): a real repo can ship its own hand-maintained env at the
pinned SHA (PostHog does), and the skill must start from a clean slate
rather than being anchored by — or refusing to overwrite — an existing
one. The upstream env is never silently discarded, though: it's a known-
working answer worth comparing against this fixture's golden route, so
it's captured as `had_upstream_flox`/`upstream_manifest`/
`upstream_flox_files`/`upstream_flox_note` in the per-rep result before
being removed. Never follows a symlink out of the checkout: a symlinked
`.flox` or `.flox/env/manifest.toml` is unlinked/skipped rather than
recursed into or read through, with `upstream_flox_note` saying why.

Reuses `_run_claude_agent`, `_is_valid_toml`, `_check_activation`,
`_run_judge`, `_stats`, `_skill_identity`, `DEFAULT_SKILL_DIR`, and the
verify.py deterministic leg (`_run_verify`, `_hard_verify_violations`,
`_advisory_verify_violations`, `_catalog_note`) from run_floxify.py
rather than duplicating that machinery. Also reuses run_floxify's
`_load_detect_and_verify` to load the SAME verify.py under test
(`--skill-dir`-controlled) for `matching_service_names`/`_service_covers`
— the shared "does a service of this kind exist" rule (AI-468) behind
both the structural `has_service_<kind>` check and the AI-447 probe's
target resolution, so a `[services.db]` running postgres is recognized
the same way the deterministic verify leg already recognizes it,
instead of real_world's own narrower name-only match.

`expected_services` registry entries carry a per-service disposition
(AI-470): `expect-wired` (the default — every fixture but posthog) means
the structural check requires an actual `[services.*]` match; `deferred-
ok` (posthog's clickhouse) also accepts the service being deferred WITH
AN EXPLICIT MECHANISM — the manifest's `[hook]` genuinely invoking
`docker-compose up`, reusing verify.py's own `manifest_wires_compose`
(AI-466's carve-out) rather than re-deriving it. Silently dropping a
`deferred-ok` service (no wiring, no mechanism) still fails the check.
`has_service_<kind>` stays the result key regardless of disposition
(baseline compat); `service_observed` in the per-rep result records the
honest wired/deferred/missing outcome behind it. The AI-447 probe is
unchanged and disposition-agnostic — it already only probes a kind it
finds genuinely wired, which is exactly "probe only when actually
wired" regardless of what the registry expected.

Usage:
    python3 real_world.py --only mastodon             # single repo
    python3 real_world.py                              # all registered repos
    python3 real_world.py --activate                   # opt in to flox activate
    python3 real_world.py --activate --services        # ...and prove services serve
    python3 real_world.py --skill-dir /path/to/flox-plugin
    python3 real_world.py --out results/my-run.json

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
    _advisory_verify_violations,
    _catalog_note,
    _check_activation,
    _hard_verify_violations,
    _is_valid_toml,
    _load_detect_and_verify,
    _run_claude_agent,
    _run_judge,
    _run_verify,
    _skill_identity,
    _stats,
)

HERE = Path(__file__).resolve().parent

# real-world repos are heavy by definition — a first activation realizes an
# entire closure (posthog: 33 packages incl. rust/go/emscripten). The synthetic
# budget of 120s is not a sane default here; it silently produced 'skipped'
# on the largest repo in the corpus (AI-454).
REAL_WORLD_ACTIVATION_TIMEOUT = 1800


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
# both quotes (same technique as synthetic's PIN_* regexes) so an unrelated
# ecosystem tool cannot satisfy the check — e.g. a "ruby" pin must not be
# satisfied by rubyPackages.rubocop.

def _runtime_pinned(manifest_text, pattern):
    if manifest_text is None:
        return False
    return bool(re.search(r'pkg-path = "' + pattern + r'"', manifest_text))


_REQUIRED_VERIFY_EXPORTS = ("matching_service_names", "manifest_wires_compose")


def _load_verify_module(skill_dir):
    """Load verify.py from the skill dir under test (never raises) so the
    structural `has_service_<kind>` check and the AI-447 probe use the
    SAME kind-matching rule (`matching_service_names`/`_service_covers`,
    AI-468) and the same manifest-wired-compose rule (`manifest_wires_compose`,
    AI-466/AI-470) the deterministic verify leg and the live skill's
    Phase 3c Step 4 already enforce, instead of real_world's own separate,
    narrower checks.

    Mirrors run_floxify.py's `_load_detect_and_verify` per-call reload
    discipline — never cached across reps, so `--skill-dir` keeps
    controlling which checkout is under test. Returns None on a load
    failure: a harness-side problem, not a manifest verdict (same
    treatment `_run_verify` gives an unloadable skill dir) — callers that
    depend on it fail closed, the same way a missing manifest already
    fails every check that needs one. A `--skill-dir` checkout that loads
    fine but predates one of `_REQUIRED_VERIFY_EXPORTS` is treated the
    same way: the module loaded, but a call this harness needs from it
    doesn't exist, which is exactly the "can't use this checkout for this
    check" case the None return already models. PR #51 review found the
    guard checking only `matching_service_names` left `_compose_wired`'s
    dependency on `manifest_wires_compose` unguarded — an old checkout
    with the former but not the latter passed the guard, then
    AttributeError'd inside `_structural_checks` for every fixture,
    crashing the whole run before any results are written. Both exports
    are now required together.
    """
    try:
        _detect_mod, verify_mod = _load_detect_and_verify(skill_dir)
    except Exception:  # noqa: BLE001 - harness-side load failure, not a manifest verdict
        return None
    if not all(hasattr(verify_mod, name) for name in _REQUIRED_VERIFY_EXPORTS):
        return None
    return verify_mod


def _parsed_manifest(verify_mod, manifest_text):
    if verify_mod is None or manifest_text is None:
        return None
    manifest, _err = verify_mod.parse_manifest(manifest_text)
    return manifest


def _matching_service_names(verify_mod, manifest_dict, kind):
    """Names of [services.*] entries covering `kind`, via verify.py's
    shared rule — empty when nothing matches, `verify_mod` failed to
    load, or the manifest didn't parse. Never re-derives the alias table
    itself; see `_load_verify_module`."""
    if verify_mod is None or manifest_dict is None:
        return []
    return verify_mod.matching_service_names(manifest_dict, kind)


# --- per-service disposition (AI-470) ---------------------------------------
# Bill's adjudication: the test is "does a developer need this service
# running locally to develop against?" Dev-time services (postgres, redis)
# must be wired as Flox services (expect-wired, the pre-AI-470 default and
# the only disposition every fixture but posthog uses). Runtime-oriented
# infrastructure (posthog's clickhouse, plus the kafka/zookeeper it pulls in
# transitively) may instead be deferred WITH AN EXPLICIT MECHANISM — never
# silently dropped — which this tier recognizes via verify.py's own
# manifest-wired-compose check (AI-466's carve-out), reused here rather than
# re-derived, exactly like SERVICE_KIND_ALIASES/matching_service_names.

_DEFAULT_DISPOSITION = "expect-wired"
KNOWN_DISPOSITIONS = {"expect-wired", "deferred-ok"}


def _service_expectation(service):
    """Normalize one `expected_services` registry entry to (name, disposition).

    Accepts either a bare string (implicit "expect-wired" — the shape
    every fixture used before AI-470) or a {"name": ..., "disposition":
    ...} dict. `disposition` defaults to "expect-wired" when the dict
    omits it."""
    if isinstance(service, str):
        return service, _DEFAULT_DISPOSITION
    return service["name"], service.get("disposition", _DEFAULT_DISPOSITION)


def _compose_wired(verify_mod, manifest_dict):
    """True if the manifest itself invokes `docker-compose up`/`docker
    compose up` from [hook] with docker-compose installed — verify.py's
    own public `manifest_wires_compose` (AI-466's carve-out against the
    repo merely HAVING a compose file, promoted to a public export by
    AI-470/PR #51 review), reused rather than duplicated. A manifest-wide
    signal, not per-service: it does not know WHICH service(s) a compose
    invocation actually starts, the same accepted scope limit
    `manifest_wires_compose`'s own docstring documents."""
    if verify_mod is None or manifest_dict is None:
        return False
    return bool(verify_mod.manifest_wires_compose(manifest_dict))


def _service_disposition_results(entry, manifest_text, verify_mod):
    """Per expected_services entry: "wired" (a matching [services.*] entry
    exists, via `_matching_service_names`), "deferred" (disposition is
    deferred-ok and the manifest wires compose per `_compose_wired`), or
    "missing" (neither) — the honest record of what actually happened,
    independent of whether that satisfies `has_service_<kind>`."""
    manifest_dict = _parsed_manifest(verify_mod, manifest_text)
    compose_wired = _compose_wired(verify_mod, manifest_dict)
    results = {}
    for service in entry.get("expected_services", []):
        name, disposition = _service_expectation(service)
        wired = bool(_matching_service_names(verify_mod, manifest_dict, name))
        if wired:
            results[name] = "wired"
        elif disposition == "deferred-ok" and compose_wired:
            results[name] = "deferred"
        else:
            results[name] = "missing"
    return results


def _structural_checks(entry, manifest_text, verify_mod=None):
    """Per-entry hard-checks derived from the registry's expected_runtimes/
    expected_services, rather than synthetic's fixed CHECKS dict — each
    real-world repo pins different runtimes and wires different services.

    `has_service_<kind>` means "a service of this kind exists" (name OR
    command matches a kind alias, via `verify_mod`), not "a service named
    this exists" (AI-468) — a `[services.db]` running postgres now
    satisfies `has_service_postgres` the same way it already satisfies
    verify.py's own leaf-datastore invariant. `verify_mod=None` (skill
    dir failed to load) fails every `has_service_*` check closed, the
    same way a missing manifest already does.

    Disposition-aware (AI-470): an `expect-wired` service (the default —
    every pre-AI-470 fixture) must be actually wired; a `deferred-ok`
    service (posthog's clickhouse) passes if EITHER wired OR deferred
    with a mechanism (`_compose_wired`) — silently dropping it (neither)
    still fails the check either way. The result key stays
    `has_service_<kind>` regardless of disposition (baseline compat);
    `process_entry` records the richer wired/deferred/missing distinction
    separately via `_service_disposition_results`.
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
    manifest_dict = _parsed_manifest(verify_mod, manifest_text)
    compose_wired = _compose_wired(verify_mod, manifest_dict)
    for service in entry.get("expected_services", []):
        name, disposition = _service_expectation(service)
        wired = bool(_matching_service_names(verify_mod, manifest_dict, name))
        if disposition == "deferred-ok":
            checks[f"has_service_{name}"] = wired or compose_wired
        else:
            checks[f"has_service_{name}"] = wired
    return checks


# --- service startup + connectivity probe (AI-447) -------------------------
# `has_service_postgres` only proves a [services.*] section header exists, and
# `flox activate` only proves the packages resolve — neither runs the service
# command. A manifest can pass both and still hand the developer a dead
# database: lemmy wired `[services.postgres]` whose command referenced $PGDATA,
# which was exported only in [hook] (service commands do NOT inherit hook
# exports), so `postgres -D ""` would fail at start while every check we had
# reported green.
#
# The postgres probe deliberately passes NO host/port. `pg_isready` reads
# PGHOST/PGPORT from the environment, and the environment is what the
# manifest's own [vars] set — so a bare `pg_isready` asserts the service is
# reachable *at the address the manifest advertises*. That is what catches a
# manifest whose [vars] point at a datastore nothing serves (plausible).

PROBE_COMMANDS = {
    "postgres": "pg_isready -q",
    "postgresql": "pg_isready -q",
    "redis": 'redis-cli ${REDIS_PORT:+-p "$REDIS_PORT"} ping',
    "valkey": 'redis-cli ${REDIS_PORT:+-p "$REDIS_PORT"} ping',
    "mariadb": "mariadb-admin ping",
    "mysql": "mysqladmin ping",
}


def _probe_command_for(kind):
    """Connectivity probe for a service kind, or None if we can't probe it.

    None means "not probeable" (e.g. clickhouse), never "broken" — the caller
    records those as skipped so an unprobeable service can't fail a run.
    """
    return PROBE_COMMANDS.get((kind or "").lower())


def _run_flox(args, cwd=None, timeout=120):
    """Run a flox subcommand -> (ok, combined stdout+stderr)."""
    try:
        proc = subprocess.run(
            ["flox", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001 - never crash a run on a probe
        return False, str(exc)
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


SERVICE_OK = "__SERVICE_OK__"
SERVICE_DEAD = "__SERVICE_DEAD__"


def _probe_script(probe, settle):
    """Poll `probe` for up to `settle` seconds, printing a sentinel either way.

    Services start asynchronously, so an immediate single probe races the
    postmaster. The sentinels let the caller tell a real verdict ("polled and
    nothing ever answered") apart from flox erroring before the script ever
    ran — those must not look the same.
    """
    return (
        f'for _ in $(seq {settle}); do '
        f'  if {probe} >/dev/null 2>&1; then echo {SERVICE_OK}; exit 0; fi; '
        f'  sleep 1; '
        f'done; '
        f'echo {SERVICE_DEAD}; exit 1'
    )


def _probe_services(target_dir, expected_services, manifest_text=None,
                    verify_mod=None, timeout=300, settle=30):
    """Prove each *declared* service actually serves. -> {svc: {ok, skipped, notes}}

    Services can only be started from *inside* an activation (`flox services
    start` on an unactivated env errors), so this is a single
    `flox activate --start-services -c <polling script>` per service. The
    activation owns the service lifetime, so there is nothing to stop
    afterwards.

    Only services the manifest actually declares are probed — resolved via
    the aligned name-or-command rule (`_matching_service_names`, AI-468),
    the same one the structural `has_service_<kind>` check uses, so a
    `[services.db]` running postgres is found here too. Probing an
    undeclared service is a false-positive machine: lemmy shipped a manifest
    with no [services.*] whose [hook] started postgres to bootstrap the DB, a
    bare `pg_isready` answered, and the probe credited it — for an environment
    with no service at all. `has_service_*` owns "did you wire it"; this owns
    "does the wired service work". If several declared services match a kind,
    the first (manifest declaration order) is probed and the ambiguity is
    noted in the result rather than silently picked.

    Declared-service gating only runs when BOTH `manifest_text` and
    `verify_mod` are supplied (the real shape `process_entry` calls with).
    Without either, gating is skipped entirely rather than guessed at —
    same as the prior behavior when `manifest_text` alone was omitted,
    which callers testing pure probe mechanics rely on.

    Advisory, like activation. Outcomes, deliberately distinct:
      ok=True             the declared service answered at the advertised address
      ok=False            polled to exhaustion, nothing answered (real verdict)
      skipped, ok=None    flox absent / service not declared / no probe for this
                          kind / flox errored before the script ran — never a
                          verdict on the manifest
    """
    results = {
        svc: {"ok": None, "skipped": True, "notes": ""} for svc in expected_services
    }
    if not shutil.which("flox"):
        for svc in results:
            results[svc]["notes"] = "flox not in PATH"
        return results

    manifest_dict = _parsed_manifest(verify_mod, manifest_text)

    for svc in expected_services:
        matches = _matching_service_names(verify_mod, manifest_dict, svc)
        if manifest_dict is not None and not matches:
            results[svc]["notes"] = (
                f"no [services.*] entry matches kind '{svc}' by name or "
                f"command (see has_service_{svc}) — nothing to probe. A "
                f"hook-started process is not a Flox-managed service."
            )
            continue

        note_prefix = ""
        if len(matches) > 1:
            note_prefix = (
                f"multiple [services.*] entries match kind '{svc}' "
                f"({', '.join(str(m) for m in matches)}) — probing "
                f"'{matches[0]}'; "
            )

        probe = _probe_command_for(svc)
        if not probe:
            results[svc]["notes"] = note_prefix + (
                f"no connectivity probe for '{svc}' — not probeable, not failed"
            )
            continue

        ok, out = _run_flox(
            ["activate", "--start-services", "-c", _probe_script(probe, settle)],
            cwd=str(target_dir),
            timeout=timeout,
        )
        if SERVICE_OK in out:
            results[svc].update(ok=True, skipped=False, notes=note_prefix.rstrip())
        elif SERVICE_DEAD in out:
            results[svc].update(
                ok=False, skipped=False,
                notes=note_prefix + (
                    f"service declared but never answered `{probe}` within "
                    f"{settle}s: {out.strip()[:200]}"
                ),
            )
        else:
            # No sentinel => the script never ran. That is a harness/env
            # problem, not a verdict on the service.
            results[svc].update(
                ok=None, skipped=True,
                notes=note_prefix + (
                    f"could not be probed (flox error, not a service verdict): "
                    f"{out.strip()[:200]}"
                ),
            )
    return results


# --- real-world LLM judge ------------------------------------------------------
# synthetic's _judge diffs the produced manifest against a hand-tuned gold
# TOML file. real-world has no gold manifest for these repos — the reference is
# a textual characterization (registry `gold` field: expected runtimes,
# services, and notes). This reuses _run_judge (the bare claude invocation)
# with a conformance-focused rubric and the same JSON-response parsing
# pattern as synthetic's _judge.

EXPECTED_DIR = HERE / "expected"


def _golden_manifest(entry_id):
    """A hand-curated, catalog-verified reference manifest for this repo, if
    one has been captured under expected/<id>.toml. It is an *idiomatic*
    reference (right runtimes/services, correct hook idioms), not an exact-match
    target — a well-structured produced manifest may legitimately differ."""
    path = EXPECTED_DIR / f"{entry_id}.toml"
    return path.read_text() if path.exists() else None


def _judge_real_world(entry, manifest_text, verify_result=None):
    """Grade produced manifest vs the registry's gold characterization, plus a
    concrete golden reference manifest when one exists (expected/<id>.toml).

    `verify_result` (AI-465) is the deterministic verify.py leg's confirmed
    catalog resolution table — handed to the judge the same way synthetic's
    `_judge` does, so it stops grading catalog facts from memory (AI-451)."""
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
        f"PRODUCED manifest:\n```toml\n{manifest_text or '(manifest not produced)'}\n```\n"
        f"{_catalog_note(verify_result)}\n"
        "Grade 1-5 on:\n"
        "  1. Runtime conformance — pins the expected runtime(s) at a "
        "reasonable version, not a substitute or generic fallback. Do NOT "
        "assert from memory whether a pkg-path or version exists in the "
        "Flox catalog — rely on the DETERMINISTIC CATALOG CHECK above; if "
        "it is unavailable, do not grade catalog existence at all\n"
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
    # AI-442: _run_judge is now a 3-tuple (adds cost/usage meta for the
    # efficiency axis). real_world.py doesn't record judge cost itself yet
    # (out of AI-442 PR 1's scope) -- mechanical unpack only, so this
    # call site doesn't break under the new shared-function signature.
    result, err, _meta = _run_judge(prompt)
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


# --- upstream .flox/ strip + capture (AI-469) -------------------------------

def _capture_and_strip_upstream_flox(target_dir):
    """Strip an in-tree .flox/ before the conversion task runs, but
    capture it first as data — never silently discard it.

    An upstream .flox/ is a real signal: a known-working environment, what
    the project's own maintainers actually run — not noise to throw away.
    But the conversion task must start from a clean slate: PostHog ships a
    git-tracked, hand-maintained manifest.toml at its pinned SHA, and one
    produced rep simply refused to overwrite it, so the harness scored the
    UPSTREAM manifest instead of anything the skill wrote (the other four
    reps floxified anchored by its presence). Capturing rather than
    discarding it feeds the golden-vs-upstream adoption review this
    fixture needs — whether the repo's own answer agrees with or
    contradicts this fixture's golden route is the point, not a nicety.

    Never follows a symlink out of the checkout. A `.flox` that is itself
    a symlink is unlinked directly rather than treated as a directory to
    recurse into or delete through — `is_dir()` follows symlinks, so a
    naive directory check would reach `shutil.rmtree`, which refuses to
    operate on a symlinked root (`OSError`) and would otherwise abort the
    whole run with nothing between here and `main`'s `pool.map` to catch
    it. A `.flox/env/manifest.toml` that is itself a symlink is left
    unread — `exists()`/`read_text()` also follow symlinks, which would
    land arbitrary host-file contents in `upstream_manifest`, a value
    that persists to the results JSON and uploads as a CI artifact. Both
    cases still report `had_upstream_flox=True` (something was there);
    they just aren't captured, and `note` says why.

    Returns (had_upstream_flox, upstream_manifest, upstream_flox_files, note):
      had_upstream_flox   True if a .flox/ path existed pre-strip, real
                          directory or symlink alike
      upstream_manifest   full text of .flox/env/manifest.toml if it
                          existed and was a real file, else None — never
                          truncated, same full-text discipline as the
                          produced manifest's own "manifest" field
                          (AI-468); None (not read) when it was a symlink
      upstream_flox_files sorted list of every file path under .flox/,
                          relative to it (e.g. "env/manifest.toml",
                          "env/on-activate.sh") — what else shipped
                          alongside the manifest, for repos with a
                          separate on-activate script or multiple env
                          files; empty when .flox itself was a symlink
      note                 non-empty only when a symlink was found where
                          a real file/dir was expected and therefore
                          skipped rather than followed
    """
    flox_dir = Path(target_dir) / ".flox"

    if flox_dir.is_symlink():
        flox_dir.unlink()
        return True, None, [], "upstream .flox was a symlink — not captured"

    if not flox_dir.is_dir():
        return False, None, [], ""

    files = sorted(
        str(p.relative_to(flox_dir)) for p in flox_dir.rglob("*") if p.is_file()
    )
    manifest_path = flox_dir / "env" / "manifest.toml"
    note = ""
    if manifest_path.is_symlink():
        upstream_manifest = None
        note = "upstream .flox/env/manifest.toml was a symlink — not captured"
    elif manifest_path.exists():
        upstream_manifest = manifest_path.read_text(encoding="utf-8")
    else:
        upstream_manifest = None
    shutil.rmtree(flox_dir)
    return True, upstream_manifest, files, note


# --- per-entry runner --------------------------------------------------------

def process_entry(entry, skill_dir, activate=False, services=False,
                  clone_timeout=900, agent_timeout=1800,
                  activation_timeout=REAL_WORLD_ACTIVATION_TIMEOUT):
    """Clone the repo at its pinned SHA, run /floxify against it, and score
    the produced manifest with structural conformance + LLM judge."""
    tmpdir = tempfile.mkdtemp(prefix=f"floxify-real_world-{entry['id']}-")
    try:
        print(f"  {entry['id']}: cloning {entry['repo_url']} @ {entry['sha']} ...", flush=True)
        clone_err = _clone_at_sha(
            entry["repo_url"], entry["sha"], tmpdir, timeout=clone_timeout
        )
        if clone_err:
            print(f"  {entry['id']}: ERROR {clone_err}", flush=True)
            return {**_base(entry), "error": clone_err}

        tmp = Path(tmpdir)

        # Strip any in-tree .flox/ before the skill ever sees this
        # checkout — a repo shipping its own known-working env must not
        # anchor or short-circuit the conversion task — but capture it
        # first as data (AI-469). Must run before the prompt is built:
        # the prompt points the agent at this same tmpdir.
        had_upstream_flox, upstream_manifest, upstream_flox_files, upstream_flox_note = (
            _capture_and_strip_upstream_flox(tmp)
        )
        if had_upstream_flox:
            note_suffix = f" ({upstream_flox_note})" if upstream_flox_note else ""
            print(
                f"  {entry['id']}: stripped upstream .flox/ "
                f"({len(upstream_flox_files)} file(s)) before conversion"
                f"{note_suffix}",
                flush=True,
            )

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
        # AI-442: _run_claude_agent is now a 3-tuple (adds cost/usage/
        # tool-call meta for the efficiency axis) and switched its
        # transport to --output-format stream-json. real_world.py doesn't
        # record agent cost itself yet (out of AI-442 PR 1's scope) --
        # mechanical unpack only. `arm` defaults to "skills", preserving
        # real-world's existing always-skill-loaded behavior exactly.
        agent_out, agent_err, _meta = _run_claude_agent(
            prompt, skill_dir, timeout=agent_timeout
        )

        if agent_err:
            print(f"  {entry['id']}: agent error: {agent_err}", flush=True)
            return {**_base(entry), "error": agent_err}

        manifest_path = tmp / ".flox" / "env" / "manifest.toml"
        manifest_text = (
            manifest_path.read_text(encoding="utf-8")
            if manifest_path.exists()
            else None
        )

        # Loaded once per rep and shared by the structural has_service_*
        # check and the AI-447 probe below, so both resolve "does a
        # service of this kind exist" through the same rule (AI-468).
        # Separate from the deterministic verify leg's own load further
        # down — mirrors run_floxify.py's documented per-call reload
        # trade-off rather than threading one module handle through both.
        verify_mod = _load_verify_module(skill_dir)

        hard = _structural_checks(entry, manifest_text, verify_mod=verify_mod)
        hard_pass = all(hard.values())

        # Honest wired/deferred/missing record per service (AI-470) —
        # independent of whether that outcome satisfies has_service_<kind>
        # (a deferred-ok service that's genuinely deferred still passes
        # the structural check but is recorded as "deferred", not "wired").
        service_observed = _service_disposition_results(
            entry, manifest_text, verify_mod
        )

        # Flat kind-name list for the probe — expected_services entries
        # are {"name", "disposition"} dicts (AI-470); _probe_services
        # itself stays disposition-agnostic and unchanged: it already
        # only probes a kind it finds wired (_matching_service_names),
        # which is exactly "probe only when actually wired" regardless
        # of what the registry expected.
        expected_service_names = [
            _service_expectation(s)[0] for s in entry.get("expected_services", [])
        ]

        if activate:
            act_ok, act_skipped, act_notes = _check_activation(
                tmp, timeout=activation_timeout
            )
        else:
            act_ok, act_skipped, act_notes = (
                None,
                True,
                "--activate not set (real-world activation is opt-in — these "
                "dev envs are too heavy to reliably activate)",
            )

        # Service probe (AI-447). Requires a working activation — probing a
        # environment that can't even activate would report a misleading
        # service failure rather than the real (activation) one.
        if services and act_ok:
            svc_results = _probe_services(
                tmp, expected_service_names,
                manifest_text=manifest_text, verify_mod=verify_mod,
            )
        elif services:
            svc_results = {
                svc: {"ok": None, "skipped": True,
                      "notes": "activation did not succeed — service probe not attempted"}
                for svc in expected_service_names
            }
        else:
            svc_results = {}

        # Deterministic manifest check (AI-461's leg, wired into real-world by
        # AI-465) — advisory, same reason activation is advisory: the
        # catalog sub-leg needs live flox+network. Re-scans `tmp`, the same
        # checkout the agent wrote into — unlike synthetic's small vendored
        # fixtures, there is no separate pristine copy to preserve at real-world
        # scale (re-cloning per rep just to get one would be its own cost).
        # The catalog sub-leg is tied to --activate, same opt-in gate the
        # rest of real-world's live-flox behavior already uses; it degrades to
        # a clean skip when flox is unavailable regardless (check_catalog's
        # own shutil.which guard). The re-scan also walks .flox/ and any
        # activation artifacts; detect's verdict-bearing parsers read only
        # committed input files, so those extras are inert.
        verify_result = _run_verify(
            skill_dir, tmp, manifest_text, check_catalog_live=activate,
        )
        verify_hard = _hard_verify_violations(verify_result["violations"])
        verify_advisory = _advisory_verify_violations(verify_result["violations"])

        # LLM judge (advisory) — hand it verify.py's confirmed catalog
        # resolution table so it stops grading catalog facts from memory
        # (AI-451), same treatment synthetic's _judge already gets.
        verdict = _judge_real_world(entry, manifest_text, verify_result=verify_result)

        status = "PASS" if hard_pass else "FAIL"
        act_str = "skipped" if act_skipped else ("ok" if act_ok else "FAIL")
        verify_str = f"{len(verify_hard)}H/{len(verify_advisory)}A"
        svc_str = ""
        if svc_results:
            failed = [s for s, r in svc_results.items() if r["ok"] is False]
            served = [s for s, r in svc_results.items() if r["ok"] is True]
            svc_str = (
                f"  services={'FAIL:' + ','.join(failed) if failed else 'ok'}"
                f"({len(served)}/{len(svc_results)} serving)"
            )
        print(
            f"  {entry['id']}: hard={status}  judge={verdict['score']}/5  "
            f"activate={act_str}  verify={verify_str}{svc_str}",
            flush=True,
        )

        return {
            **_base(entry),
            # Captured pre-strip, not a verdict on the produced manifest —
            # feeds the golden-vs-upstream adoption review (AI-470), not
            # this harness's own scoring (AI-469).
            "had_upstream_flox": had_upstream_flox,
            "upstream_manifest": upstream_manifest,
            "upstream_flox_files": upstream_flox_files,
            "upstream_flox_note": upstream_flox_note,
            "hard_checks": hard,
            "hard_pass": hard_pass,
            # Honest wired/deferred/missing record per expected service
            # (AI-470) — has_service_<kind> in hard_checks stays a single
            # boolean for baseline compat; this is the richer breakdown of
            # WHY it's true or false for a deferred-ok kind.
            "service_observed": service_observed,
            "activation": {"ok": act_ok, "skipped": act_skipped, "notes": act_notes},
            "services": svc_results,
            "verify": {
                "violations": verify_result["violations"],
                "hard_count": len(verify_hard),
                "advisory_count": len(verify_advisory),
                "catalog_checked": verify_result.get("catalog_checked", False),
            },
            "judge": verdict,
            # Full text persisted alongside the excerpt (AI-468) — forensics
            # on a failing/ambiguous rep has twice now needed the actual
            # manifest and found only a 3000-char excerpt in the committed
            # results (the lemmy rep-3 [services.*] naming this ticket
            # exists to explain). manifest_excerpt stays for anything that
            # still displays a short preview; manifests here are a few KB,
            # not a size concern for the results JSON.
            "manifest": manifest_text or "",
            "manifest_excerpt": (manifest_text or "")[:3000],
            "agent_output_excerpt": (agent_out or "")[:800],
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def process_task(entry, skill_dir, reps=1, activate=False, services=False,
                  clone_timeout=900, agent_timeout=1800,
                  activation_timeout=REAL_WORLD_ACTIVATION_TIMEOUT):
    """Run `reps` repetitions of an entry. A single rep returns the plain
    per-entry result (dashboard-compatible with a synthetic-shaped result);
    multiple reps return an aggregate with each run kept under "runs"."""
    runs = [
        process_entry(
            entry, skill_dir, activate=activate, services=services,
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
        description="Flox /floxify real-world eval harness (real OSS conversion repos)"
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
        default=str(HERE / "real-world.jsonl"),
        help="Path to real-world.jsonl (default: real-world.jsonl alongside this script)",
    )
    ap.add_argument("--only", help="Run a single registered repo id (e.g. mastodon)")
    ap.add_argument(
        "--reps", type=int, default=1,
        help="Repetitions per repo (default 1; >1 aggregates hard-pass rate)",
    )
    ap.add_argument(
        "--out", default="real-world.json",
        help="Output filename under results/ (gitignored; default: results/real-world.json). Committed baselines live in baselines/ and are never written by a run.",
    )
    ap.add_argument(
        "--activate", action="store_true",
        help=(
            "Opt in to `flox activate` verification (off by default — "
            "real-world dev envs are too heavy to reliably activate in CI)"
        ),
    )
    ap.add_argument(
        "--services", action="store_true",
        help=(
            "Opt in to service startup + connectivity probing (AI-447): after a "
            "successful activation, `flox services start`, probe each expected "
            "service for real connectivity (pg_isready / redis-cli ping), then "
            "stop. Advisory. Implies --activate to be meaningful."
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
        "--activation-timeout", type=int, default=REAL_WORLD_ACTIVATION_TIMEOUT,
        help=(
            f"Seconds allowed for `flox activate` (default "
            f"{REAL_WORLD_ACTIVATION_TIMEOUT}; real-world first activations realize a "
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
                    services=args.services,
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
