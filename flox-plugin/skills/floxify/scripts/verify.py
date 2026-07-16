#!/usr/bin/env python3
"""floxify manifest verifier — deterministic output grounding (stdlib + `flox show`).

detect.py grounds the INPUT (pins, lockfiles, services, clients read straight
from the repo's own files). Nothing grounded the OUTPUT: the produced
`manifest.toml` was checked only by an LLM judge — which has graded catalog
facts from memory and accused a *correct* pin of being hallucinated — and by
prose instructions a model follows most, not all, of the time. This script
closes that gap: given detect.py's JSON facts and a manifest.toml, it reports
concrete violations instead of an opinion.

Two severities:
  - HARD violations are real bugs the manifest can be shown to contain —
    a datastore the manifest advertises but never serves, a `[vars]` value
    that isn't literal, a `[hook]` that mutates the tracked git tree, a
    `pkg-path`/`version`/`systems` combination that doesn't resolve in the
    catalog. These are grounded in detect.py's facts or in `flox show` —
    never a judgment call.
  - ADVISORY notes are heuristics worth a second look (e.g. a native build
    input with no `outputs` declared) but are not asserted as bugs — hard-
    failing on a judgment call reproduces the LLM judge's own failure mode
    in Python, which is the thing this script exists to avoid.

What this script does NOT do: it does not know whether the repo was read
correctly, whether a hook command is *idiomatic*, or whether a deferred
service was the right judgment call. detect.py is root-scoped, so a clean
verify run means "consistent with what detect.py found" — it is NOT a
certification that the manifest is correct. Say so in every report; a script
this narrow that reads as "verified correct" manufactures false confidence.

Usage:
    python3 verify.py <detect.json> <manifest.toml>
    python3 verify.py - <manifest.toml>            # detect facts on stdin
    python3 verify.py <detect.json> <manifest.toml> --no-catalog
    python3 verify.py <detect.json> <manifest.toml> --json

Exit 0 if there are no HARD violations, 1 otherwise (ADVISORY notes never
affect the exit code). Catalog checks (`flox show`) are skipped — not
failed — when `flox` is not on PATH, mirroring the harness's own activation
check.  Pure stdlib for everything except the catalog leg, which shells out
to `flox show` and is fully mockable (see evals/floxify/test_verify.py).
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - fallback for < 3.11
    tomllib = None

HARD = "hard"
ADVISORY = "advisory"

ALL_SYSTEMS = {"aarch64-darwin", "aarch64-linux", "x86_64-darwin", "x86_64-linux"}

DISCLAIMER = (
    "verify.py checks the manifest against what detect.py found in this "
    "repo — a clean result means 'consistent with the grounded facts', "
    "NOT 'correct'. detect.py is root-scoped and never touches the "
    "catalog beyond the checks below; judgment calls (deferring a service "
    "to an orchestrator, choosing a hook idiom) are out of scope."
)


def violation(rule, message, severity=HARD, **extra):
    return {"rule": rule, "severity": severity, "message": message, **extra}


# ---------------------------------------------------------------------------
# manifest parsing
# ---------------------------------------------------------------------------

def parse_manifest(text):
    """Parse manifest.toml text. Returns (manifest_dict, error) — one is None."""
    if tomllib is None:
        return None, "tomllib unavailable (needs Python 3.11+)"
    try:
        return tomllib.loads(text), None
    except Exception as exc:  # noqa: BLE001 - surface any parse failure as a violation
        return None, str(exc)


def _pkg_path_str(descriptor):
    pp = descriptor.get("pkg-path") if isinstance(descriptor, dict) else None
    if isinstance(pp, list):
        return ".".join(str(p) for p in pp)
    return pp


# ---------------------------------------------------------------------------
# invariant 1 — every detected runtime is installed
# ---------------------------------------------------------------------------

RUNTIME_PKG_PATTERNS = {
    "python": re.compile(r"^python\d{2,3}(Full|FreeThreading)?$"),
    "node": re.compile(r"^nodejs(_\d+)?$"),
    "ruby": re.compile(r"^ruby(_\d+_\d+)?$"),
    "go": re.compile(r"^go(_\d+_\d+)?$"),
    "rust": re.compile(r"^(cargo|rustc)$"),
    "elixir": re.compile(r"^elixir$"),
    "php": re.compile(r"^php\d*(Packages\..+)?$"),
    "deno": re.compile(r"^deno$"),
    "dotnet": re.compile(r"^dotnet(-sdk)?(_\d+)?$"),
    "scala": re.compile(r"^scala$"),
    "dart": re.compile(r"^dart$"),
    "flutter": re.compile(r"^flutter$"),
    "swift": re.compile(r"^swift$"),
    "zig": re.compile(r"^zig$"),
    "bun": re.compile(r"^bun$"),
}


def check_runtimes_installed(detect, manifest):
    """Every runtime language detect.py found must have a matching [install] entry.

    Catches the posthog/AI-453 shape: detect.py extracted `requires-python`
    from a Python repo, but the skill installed only Node — a runtime
    detect.py grounded was silently dropped on the floor.
    """
    violations = []
    install = manifest.get("install", {}) or {}
    pkg_paths = {_pkg_path_str(d) for d in install.values() if isinstance(d, dict)}
    pkg_paths.discard(None)

    languages = {r["language"] for r in (detect or {}).get("runtimes", [])
                 if r.get("language")}
    for lang in sorted(languages):
        pattern = RUNTIME_PKG_PATTERNS.get(lang)
        if pattern is None:
            continue  # no known catalog naming convention — nothing to check
        if not any(pattern.match(pp) for pp in pkg_paths):
            sources = sorted({r["source"] for r in detect["runtimes"]
                              if r["language"] == lang})
            violations.append(violation(
                "runtime-not-installed",
                f"detected runtime '{lang}' (from {', '.join(sources)}) has "
                f"no matching [install] entry",
            ))
    return violations


# ---------------------------------------------------------------------------
# invariants 2 & 3 — leaf datastores get [services.*]; [vars] endpoints
# advertise a datastore that is actually served
# ---------------------------------------------------------------------------

LEAF_DATASTORE_DISPLAY = {
    "postgresql": "postgres",
    "redis": "redis",
    "mariadb": "mariadb",
    "mongodb-ce": "mongodb",
}

SERVICE_KIND_ALIASES = {
    "postgres": ("postgres", "postgresql", "pg_ctl", "initdb"),
    "redis": ("redis", "valkey"),
    "mariadb": ("mariadb", "mysql"),
    "mongodb": ("mongo",),
}

COMPOSE_KIND_MAP = {
    "postgres": "postgres", "postgis": "postgres",
    "redis": "redis", "valkey": "redis",
    "mysql": "mariadb", "mariadb": "mariadb",
    "mongo": "mongodb",
}

_CONN_STRING_RE = re.compile(
    r"\b(postgres(?:ql)?|mysql|mariadb|redis|mongodb)://", re.I
)
_CONN_STRING_KIND = {
    "postgres": "postgres", "postgresql": "postgres",
    "mysql": "mariadb", "mariadb": "mariadb",
    "redis": "redis", "mongodb": "mongodb",
}


def _compose_covers(detect, kind):
    for svc in (detect or {}).get("services", []):
        if COMPOSE_KIND_MAP.get((svc.get("kind") or "").lower()) == kind:
            return True
    return False


def _service_covers(manifest, kind):
    aliases = SERVICE_KIND_ALIASES.get(kind, (kind,))
    services = manifest.get("services", {}) or {}
    for name, descriptor in services.items():
        haystack = str(name).lower()
        if isinstance(descriptor, dict):
            haystack += " " + str(descriptor.get("command", "")).lower()
        if any(a in haystack for a in aliases):
            return True
    return False


def _truncate(value, limit=64):
    return value if len(value) <= limit else value[:limit] + "…"


def check_leaf_datastore_services(detect, manifest):
    """A detected leaf-datastore client (`pg`, `psycopg2`, `redis`, ...) must
    be served by a `[services.*]` block, unless docker-compose already
    manages it."""
    violations = []
    for client in (detect or {}).get("service_clients", []):
        seen_kinds = set()
        for term in client.get("search_terms", []):
            kind = LEAF_DATASTORE_DISPLAY.get(term)
            if not kind or kind in seen_kinds:
                continue
            seen_kinds.add(kind)
            if _compose_covers(detect, kind) or _service_covers(manifest, kind):
                continue
            violations.append(violation(
                "leaf-datastore-not-served",
                f"client '{client.get('package')}' ({client.get('source')}) "
                f"implies {kind}, but no [services.*] serves it",
            ))
    return violations


def check_vars_endpoints(detect, manifest):
    """A [vars] value that advertises a datastore connection string must be
    backed by a matching [services.*] (or a compose service)."""
    violations = []
    for key, value in (manifest.get("vars", {}) or {}).items():
        if not isinstance(value, str):
            continue
        m = _CONN_STRING_RE.search(value)
        if not m:
            continue
        kind = _CONN_STRING_KIND[m.group(1).lower()]
        if _compose_covers(detect, kind) or _service_covers(manifest, kind):
            continue
        violations.append(violation(
            "vars-endpoint-not-served",
            f"[vars] {key}='{_truncate(value)}' advertises {kind} but no "
            f"[services.{kind}] serves it",
        ))
    return violations


# ---------------------------------------------------------------------------
# invariant 4 — [vars] are literal strings, never `$`-expanded
# ---------------------------------------------------------------------------

def check_vars_literal(manifest):
    violations = []
    for key, value in (manifest.get("vars", {}) or {}).items():
        if isinstance(value, str) and "$" in value:
            violations.append(violation(
                "vars-not-literal",
                f"[vars] {key} contains '{value}' — [vars] are literal; "
                f"move to [hook]",
            ))
    return violations


# ---------------------------------------------------------------------------
# invariant 5 — hooks must not mutate the tracked git tree
# ---------------------------------------------------------------------------

_GIT_MUTATION_RE = re.compile(
    r"\bgit\s+(?:submodule\s+update|checkout|reset|clean|pull|commit|add|"
    r"stash|rm|mv|apply|cherry-pick|rebase|merge)\b"
)


def _line_containing(text, offset):
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    if end == -1:
        end = len(text)
    return text[start:end]


def check_hook_no_mutation(manifest):
    """Hooks run on EVERY activation — a hook that mutates the tracked git
    tree (`git submodule update`, `git checkout`, ...) re-mutates it every
    time the developer activates."""
    violations = []
    hook = manifest.get("hook", {}) or {}
    script = hook.get("on-activate")
    if not isinstance(script, str):
        return violations
    seen = set()
    for m in _GIT_MUTATION_RE.finditer(script):
        line = _line_containing(script, m.start()).strip()
        if line in seen:
            continue
        seen.add(line)
        violations.append(violation(
            "hook-mutates-tree",
            f"[hook] on-activate runs '{line}' — hooks run on every "
            f"activation and must not mutate the tracked git tree",
        ))
    return violations


# ---------------------------------------------------------------------------
# invariant 6 — catalog resolution (pkg-path / version / per-system)
# ---------------------------------------------------------------------------

_SHOW_CACHE = {}

# Matches a pinned version string ("24.13.0", "14", "python3-3.13.13").
# Semver-range specs ("^1.2", ">=2.0") are legitimate manifest syntax but
# resolve to whichever version satisfies the range — verifying that needs
# full semver resolution, out of scope here, so those are left unchecked
# rather than flagged with a guessed, possibly-wrong match.
_PINNED_VERSION_RE = re.compile(r"^[0-9][\w.+-]*$")


def _is_pinned_version(v):
    return bool(v) and bool(_PINNED_VERSION_RE.match(v)) and not re.search(r"[\^~<>*]", v)


def _pinned_version_match(declared, catalog_versions):
    """Resolve a declared version against `flox show`'s version list.

    Confirmed against a live `flox edit` (not just the doc): a declared
    version with FEWER dot-segments than a catalog version is a prefix
    wildcard — "14" matches "14.9" (flox.md: "partial versions act as
    wildcards ... latest 1.2.X"); a full-length declaration must match a
    catalog version segment-for-segment, which also catches a package
    whose catalog scheme carries a name prefix the manifest omitted
    (posthog's golden pins `python3.version = "3.13.13"` for `python313`,
    but the catalog's real version string is `python3-3.13.13` — that
    declaration does NOT resolve, confirmed live).

    `catalog_versions` must preserve `flox show`'s newest-first order so
    the first prefix match is the *latest* matching version, matching
    "latest 1.2.X" semantics. Returns the matched catalog version, or None.
    """
    declared_parts = declared.split(".")
    for cv in catalog_versions:
        if cv.split(".")[:len(declared_parts)] == declared_parts:
            return cv
    return None


def _run_show_command(pkg_path, flox_bin, timeout):
    """Thin wrapper around `flox show <pkg-path>` — the whole surface a test
    needs to mock to keep catalog checks off the network."""
    return subprocess.run(
        [flox_bin, "show", pkg_path], capture_output=True, text=True, timeout=timeout,
    )


def _parse_flox_show(text):
    """Parse `flox show <pkg-path>` output into {version: {systems}}.

    An "Other versions" line with no "(... only)" annotation supports all
    four systems; an annotated line is restricted to exactly the systems
    listed. `Latest:` gives the version to use when a manifest entry omits
    `version`.
    """
    latest = None
    m = re.search(r"^Latest:\s*\S+@(\S+)", text, re.M)
    if m:
        latest = m.group(1)
    versions = {}
    in_other = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "Other versions:":
            in_other = True
            continue
        if not in_other or not stripped:
            continue
        vm = re.match(r"^\S+@(\S+?)\s*(?:\(([^)]*?)\s+only\))?$", stripped)
        if not vm:
            continue
        ver, sys_group = vm.group(1), vm.group(2)
        systems = {s.strip() for s in sys_group.split(",")} if sys_group else set(ALL_SYSTEMS)
        versions[ver] = systems
    return {"latest": latest, "versions": versions}


def _flox_show(pkg_path, flox_bin="flox", timeout=30):
    if pkg_path in _SHOW_CACHE:
        return _SHOW_CACHE[pkg_path]
    try:
        proc = _run_show_command(pkg_path, flox_bin, timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        result = {"error": str(exc)}
        _SHOW_CACHE[pkg_path] = result
        return result
    if proc.returncode != 0:
        text = (proc.stderr or proc.stdout or "").strip()
        result = {"error": text or f"flox show exited {proc.returncode}"}
    else:
        result = _parse_flox_show(proc.stdout)
    _SHOW_CACHE[pkg_path] = result
    return result


def check_catalog(manifest, flox_bin="flox", live=True, timeout=30):
    """Every pkg-path resolves; every declared version exists; every
    declared/default system is actually built for the resolved version.

    Skipped (not failed) when `flox` is unavailable — same treatment the
    harness gives its own activation check, for the same reason: this needs
    a live catalog and network access neither test environment nor CI
    always has.
    """
    if not live or not shutil.which(flox_bin):
        return [], False

    violations = []
    options = manifest.get("options", {}) or {}
    default_systems = set(options.get("systems") or ALL_SYSTEMS)

    for install_id, descriptor in (manifest.get("install", {}) or {}).items():
        if not isinstance(descriptor, dict):
            continue
        pkg_path = _pkg_path_str(descriptor)
        if not pkg_path:
            continue
        show = _flox_show(pkg_path, flox_bin=flox_bin, timeout=timeout)
        if "error" in show:
            violations.append(violation(
                "catalog-unresolved",
                f"[install] {install_id}.pkg-path = \"{pkg_path}\" does not "
                f"resolve in the catalog ({show['error']})",
            ))
            continue

        version = descriptor.get("version")
        entry_systems = set(descriptor.get("systems") or default_systems)

        if version and _is_pinned_version(version):
            matched = _pinned_version_match(version, show["versions"].keys())
            if matched is None:
                violations.append(violation(
                    "catalog-version-missing",
                    f"[install] {install_id}.version = \"{version}\" does not "
                    f"exist for pkg-path \"{pkg_path}\"",
                ))
                continue
            available = show["versions"][matched]
            version_label = matched
        else:
            available = show["versions"].get(show["latest"], set(ALL_SYSTEMS))
            version_label = show["latest"] or "latest"

        missing = entry_systems - available
        if missing:
            violations.append(violation(
                "catalog-systems-mismatch",
                f"[install] {install_id}.pkg-path = \"{pkg_path}\" "
                f"(version {version_label}) has no build for "
                f"{', '.join(sorted(missing))}, but options.systems declares it",
            ))
    return violations, True


# ---------------------------------------------------------------------------
# heuristic — native build inputs with no `outputs` declared (ADVISORY)
# ---------------------------------------------------------------------------

def check_outputs_heuristic(detect, manifest):
    """ADVISORY: a native-library [install] entry with no `outputs` declared
    may be missing the `dev` output (headers) or a non-default `out` (e.g.
    vips' libvips.so isn't in vips' default outputs) — worth a second look,
    never asserted as a bug (some installs genuinely only need defaults).

    Scoped to detect.py's `native_hints` (Aptfile / Dockerfile apt-get
    evidence of an actual native C-extension build need), not every
    manifest entry that happens to share a name with a library — otherwise
    this would fire on every plain `postgresql` server install, which needs
    no `dev` output at all when nothing links against libpq.
    """
    candidates = {}
    for hint in (detect or {}).get("native_hints", []):
        for term in hint.get("search_terms", []):
            candidates.setdefault(term, hint.get("source", "detected"))
    if not candidates:
        return []

    violations = []
    for install_id, descriptor in (manifest.get("install", {}) or {}).items():
        if not isinstance(descriptor, dict):
            continue
        pkg_path = _pkg_path_str(descriptor)
        if pkg_path not in candidates or "outputs" in descriptor:
            continue
        violations.append(violation(
            "outputs-heuristic",
            f"[install] {install_id}.pkg-path = \"{pkg_path}\" is a native "
            f"build input (from {candidates[pkg_path]}) with no `outputs` "
            f"declared — check `flox show {pkg_path}` for non-default "
            f"outputs (e.g. `dev` headers) and add `{install_id}.outputs = "
            f"[...]` if the build needs them",
            severity=ADVISORY,
        ))
    return violations


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------

def verify(detect, manifest_text, flox_bin="flox", check_catalog_live=True,
          catalog_timeout=30):
    """Run every check. Returns {"violations": [...], "catalog_checked": bool}.

    `detect` may be None/{} — the detect-cross-check invariants (runtimes
    installed, leaf-datastore clients served) degrade to no-ops when there
    are no facts to cross-check against; the manifest-only invariants
    ([vars] literal, hook mutation, catalog resolution, outputs heuristic)
    always run.
    """
    detect = detect or {}
    manifest, parse_error = parse_manifest(manifest_text)
    if manifest is None:
        return {
            "violations": [violation(
                "invalid-toml", f"manifest.toml does not parse: {parse_error}",
            )],
            "catalog_checked": False,
        }

    violations = []
    violations += check_runtimes_installed(detect, manifest)
    violations += check_leaf_datastore_services(detect, manifest)
    violations += check_vars_endpoints(detect, manifest)
    violations += check_vars_literal(manifest)
    violations += check_hook_no_mutation(manifest)
    catalog_violations, catalog_checked = check_catalog(
        manifest, flox_bin=flox_bin, live=check_catalog_live, timeout=catalog_timeout,
    )
    violations += catalog_violations
    violations += check_outputs_heuristic(detect, manifest)

    return {"violations": violations, "catalog_checked": catalog_checked}


def _print_report(result):
    print(DISCLAIMER)
    hard = [v for v in result["violations"] if v["severity"] == HARD]
    advisory = [v for v in result["violations"] if v["severity"] == ADVISORY]

    if not hard and not advisory:
        print("\nNo violations — manifest is consistent with the detected facts.")
    if hard:
        print(f"\n{len(hard)} violation(s):")
        for v in hard:
            print(f"  [{v['rule']}] {v['message']}")
    if advisory:
        print(f"\n{len(advisory)} advisory note(s) (never block, worth a look):")
        for v in advisory:
            print(f"  [{v['rule']}] {v['message']}")
    if not result["catalog_checked"]:
        print(
            "\nNOTE: catalog checks were skipped (flox not on PATH or "
            "--no-catalog) — pkg-path/version/systems were NOT verified."
        )


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("detect_json", help="path to detect.py output JSON, or '-' for stdin")
    ap.add_argument("manifest_toml", help="path to the manifest.toml to verify")
    ap.add_argument("--flox-bin", default="flox")
    ap.add_argument("--no-catalog", action="store_true",
                    help="skip live `flox show` catalog checks")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = ap.parse_args(argv[1:])

    if args.detect_json == "-":
        detect_text = sys.stdin.read()
    else:
        detect_path = Path(args.detect_json)
        if not detect_path.is_file():
            print(f"error: detect JSON not found: {detect_path}", file=sys.stderr)
            return 2
        detect_text = detect_path.read_text(encoding="utf-8")
    try:
        detect = json.loads(detect_text) if detect_text.strip() else {}
    except json.JSONDecodeError as exc:
        print(f"error: could not parse detect JSON: {exc}", file=sys.stderr)
        return 2

    manifest_path = Path(args.manifest_toml)
    if not manifest_path.is_file():
        print(f"error: manifest not found: {manifest_path}", file=sys.stderr)
        return 2
    manifest_text = manifest_path.read_text(encoding="utf-8")

    result = verify(detect, manifest_text, flox_bin=args.flox_bin,
                    check_catalog_live=not args.no_catalog)

    if args.json:
        print(json.dumps({**result, "_meta": {"disclaimer": DISCLAIMER}}, indent=2))
    else:
        _print_report(result)

    return 1 if any(v["severity"] == HARD for v in result["violations"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
