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
from urllib.parse import urlsplit

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


def hard_violations(violations_or_result):
    """The HARD-severity subset — from either a `verify()` result dict or
    a raw violations list. Centralizes the severity partition so callers
    (this module's own `main`/`_print_report`, the harness, tests) never
    re-derive it from the raw "hard" string literal, which is exactly the
    kind of scattered duplication that lets a typo silently stop gating.
    """
    violations = (
        violations_or_result["violations"]
        if isinstance(violations_or_result, dict)
        else violations_or_result
    )
    return [v for v in violations if v["severity"] == HARD]


def advisory_violations(violations_or_result):
    """The ADVISORY-severity subset — see `hard_violations`."""
    violations = (
        violations_or_result["violations"]
        if isinstance(violations_or_result, dict)
        else violations_or_result
    )
    return [v for v in violations if v["severity"] == ADVISORY]


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

# NOTE: bare "python" (no `3`) deliberately does NOT match -- confirmed
# live, it resolves to Python 2.7 in the catalog. Matching it would make
# this check pass a manifest that installed Python 2 for a repo needing
# Python 3, which is worse than not checking at all.
RUNTIME_PKG_PATTERNS = {
    "python": re.compile(r"^python3(\d{2})?(Full|FreeThreading)?$"),
    "node": re.compile(r"^nodejs(_\d+)?$"),
    "ruby": re.compile(r"^ruby(_\d+_\d+)?$"),
    "go": re.compile(r"^go(_\d+_\d+)?$"),
    "rust": re.compile(r"^(cargo|rustc|rustup)$"),
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

# detect.py's TOOL_LANG dict (.tool-versions / .mise.toml) can emit a
# canonical language this table has no pattern for -- check_runtimes_installed
# silently skips those (nothing to compare against), which is exactly the
# posthog/AI-453 failure mode this invariant exists to catch. Rather than
# leave that gap implicit, every TOOL_LANG value is either a
# RUNTIME_PKG_PATTERNS key or has a documented reason here for staying out.
# test_verify.py asserts this list is exhaustive against detect.py's own
# TOOL_LANG, so a new language added there fails CI until it's triaged here.
RUNTIME_PATTERNS_DELIBERATELY_EXCLUDED = {
    # Bundled inside the `elixir` catalog package -- the skill's own
    # guidance (SKILL.md) is "do NOT add erlang separately". A standalone
    # (non-Elixir) Erlang project would false-negative here; narrow enough
    # in practice that a dedicated pattern isn't worth the false-positive
    # risk of guessing the wrong catalog name for the bundled case.
    "erlang": "bundled in the elixir package; see SKILL.md",
    # No catalog pkg-path this checker can verify without risking a wrong
    # guess: bare "java" does not resolve (confirmed live); the real name
    # is version-qualified ("jdk", "jdk21", ...) and detect.py doesn't
    # extract a specific JDK version to disambiguate against.
    "java": "catalog name is version-qualified (jdk/jdk21/...); not yet mapped",
    # Not a language runtime the [install]-matching convention above
    # applies to the same way; out of scope for this invariant.
    "terraform": "infra tool, not a language runtime this invariant targets",
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

_CONN_STRING_RE = re.compile(
    r"\b(postgres(?:ql)?|mysql|mariadb|redis|mongodb)://", re.I
)
_CONN_STRING_KIND = {
    "postgres": "postgres", "postgresql": "postgres",
    "mysql": "mariadb", "mariadb": "mariadb",
    "redis": "redis", "mongodb": "mongodb",
}

# detect.py compose `kind` values -> our display kind. Used ONLY as a
# CORROBORATION signal for client evidence (AI-466 I1, generalized to
# every source by AI-467 — see check_leaf_datastore_services) -- a
# repo-level "this datastore genuinely exists" fact, independent of
# whether the manifest actually WIRES it (that separate question is
# `_manifest_wires_compose`, which this table has no part in since #42's
# Hole 1 fix removed the old escape-hatch use of this mapping).
COMPOSE_KIND_MAP = {
    "postgres": "postgres", "postgis": "postgres",
    "redis": "redis", "valkey": "redis",
    "mysql": "mariadb", "mariadb": "mariadb",
    "mongo": "mongodb",
}


def _vars_endpoint_kind_present(manifest, kind):
    """True if any [vars] value is a connection-string endpoint of this
    kind — corroboration for client evidence (AI-466 I1 / AI-467),
    independent of whether check_vars_endpoints finds it already
    served."""
    for value in (manifest.get("vars", {}) or {}).values():
        if not isinstance(value, str):
            continue
        m = _CONN_STRING_RE.search(value)
        if m and _CONN_STRING_KIND[m.group(1).lower()] == kind:
            return True
    return False


def _compose_service_kind_present(detect, kind):
    """True if detect.py found a compose service of this kind in the repo
    — corroboration for client evidence (AI-466 I1 / AI-467)."""
    for svc in (detect or {}).get("services", []):
        if COMPOSE_KIND_MAP.get((svc.get("kind") or "").lower()) == kind:
            return True
    return False

# Global options that can appear BETWEEN `docker-compose`/`docker compose`
# and its `up` subcommand (`docker-compose -f docker-compose.dev.yml up -d
# clickhouse`, `docker-compose --env-file .env -p myproj up`) -- AI-476: the
# bare "compose directly followed by up" match required exact adjacency, so
# the common `-f <file>` form evaded detection entirely. The posthog golden
# is a real instance of the workaround this bug forced: its hook exports
# COMPOSE_FILE and calls the BARE `docker-compose up` specifically to stay
# inside this check's old blind spot (see its [hook] comment) rather than
# using the more direct `docker-compose -f <file> up` invocation it would
# otherwise have written. Modeled on `_GIT_GLOBAL_OPT`'s space-vs-equals
# lesson (AI-466 M1): `--file`/`--project-name`/`--env-file` accept both
# `--opt value` and `--opt=value` in real docker-compose.
_COMPOSE_GLOBAL_OPT = (
    r"(?:-f\s+\S+|--file(?:=\S+|\s+\S+)|"
    r"-p\s+\S+|--project-name(?:=\S+|\s+\S+)|"
    r"--env-file(?:=\S+|\s+\S+))"
)
_DOCKER_COMPOSE_UP_RE = re.compile(
    r"\bdocker(?:-|\s+)compose\s+(?:" + _COMPOSE_GLOBAL_OPT + r"\s+)*up\b"
)


def manifest_wires_compose(manifest):
    """True only if the manifest ITSELF actually invokes docker-compose in
    its on-activate hook (`docker-compose up` / `docker compose up`) AND
    has docker-compose installed.

    Repo-side compose FILE presence (a detect.py fact) is never
    sufficient by itself (AI-466 Hole 1) — SKILL.md's HARD FLOOR: "The
    repo already having a way to start it is NEVER a reason to defer."
    A prior version of this check asked only whether detect.py had found
    a compose service of the right kind, which let the repo simply
    HAVING a compose file silence a manifest that never actually started
    anything — reproduced against a real lemmy re-run where two produced
    manifests advertised a postgres endpoint with no [services.postgres]
    and no compose invocation, and both "passed."

    Comments are stripped before matching (a mention of docker-compose in
    a `#` note doesn't run it — same discipline as
    check_hook_no_mutation). Does not parse WHICH services a named
    invocation (`docker-compose up -d clickhouse kafka`) starts — a hook
    that starts only unrelated services would still read as covering an
    untouched leaf datastore; narrower than that is out of scope here.

    Deliberate asymmetry: the regex accepts both the V1 (`docker-compose
    up`) and V2 (`docker compose up`) spellings, but the install check
    below only recognizes the standalone `docker-compose` package —
    SKILL.md's "Services deferred to docker-compose" pattern prescribes
    installing that V1 package specifically. A hook using V2 via the
    `docker` package would still fail the install check and correctly
    keep firing; this errs toward the stricter, not the more permissive,
    reading rather than an oversight.
    """
    hook = manifest.get("hook", {}) or {}
    script = hook.get("on-activate")
    if not isinstance(script, str):
        return False
    stripped = "\n".join(_strip_comment(line) for line in script.splitlines())
    if not _DOCKER_COMPOSE_UP_RE.search(stripped):
        return False
    install = manifest.get("install", {}) or {}
    pkg_paths = {_pkg_path_str(d) for d in install.values() if isinstance(d, dict)}
    return "docker-compose" in pkg_paths


# Back-compat alias — PR #51 review (AI-470): tier2.py now consumes this as
# a public export directly, same public/private-alias shape
# matching_service_names/_service_covers already uses below. Internal
# callers in this module keep the underscore name.
_manifest_wires_compose = manifest_wires_compose


def matching_service_names(manifest, kind):
    """Names of [services.*] entries whose own name OR command matches
    `kind` via SERVICE_KIND_ALIASES — the single "does a service of this
    kind exist" rule, backing `_service_covers`'s bool here and this
    module's other two consumers (the eval harnesses' structural
    `has_service_<kind>` check and the AI-447 probe's target resolution,
    AI-468). A service can be declared under any name (`[services.db]`
    running postgres) — matching on name alone, the way tier2.py's
    structural check and probe used to, missed that shape and reported
    "not declared" for a service that was both declared and reachable.
    Public (no leading underscore) so external callers import it rather
    than re-deriving the alias table."""
    aliases = SERVICE_KIND_ALIASES.get(kind, (kind,))
    services = manifest.get("services", {}) or {}
    matches = []
    for name, descriptor in services.items():
        haystack = str(name).lower()
        if isinstance(descriptor, dict):
            haystack += " " + str(descriptor.get("command", "")).lower()
        if any(a in haystack for a in aliases):
            matches.append(name)
    return matches


def _service_covers(manifest, kind):
    return bool(matching_service_names(manifest, kind))


def _truncate(value, limit=64):
    return value if len(value) <= limit else value[:limit] + "…"


def check_leaf_datastore_services(detect, manifest):
    """A detected leaf-datastore client (`pg`, `psycopg2`, `redis`, ...) must
    be served by a `[services.*]` block, unless the manifest's own hook
    genuinely starts it via `docker-compose up` (see
    `_manifest_wires_compose` — repo-side compose FILE presence alone
    does not count, AI-466 Hole 1).

    HARD requires BOTH: the client's detect.py-recorded `scope` is
    "runtime" (not "dev" — a dev/test/optional-only dependency, e.g. npm
    `devDependencies`, a Gemfile `group :test do...end` gem, Python's
    `[project.optional-dependencies]` / PEP 735 `[dependency-groups]` /
    poetry's `[tool.poetry.group.*]`), AND an independent same-kind
    signal corroborates it — a [vars] connection-string endpoint, or a
    compose service of that kind in detect facts. Either condition
    failing downgrades to ADVISORY (AI-466 I1 established this for
    Cargo.lock specifically; AI-467 generalizes it to every source).

    Originally (AI-461/#42) any client match fired HARD unconditionally.
    AI-466 found Cargo.lock's evidence unreliable (it reads the full
    resolved dependency graph, not a manifest the developer wrote) and
    required corroboration for that source alone. AI-467 found the same
    failure mode is not Cargo.lock-specific: reproduced live against
    PostHog @ 55525a19f353, whose pyproject.toml declares `pymysql` and
    `pymongo` in the MAIN `[project.dependencies]` list — genuinely
    "runtime" by section placement — yet PostHog runs neither MariaDB nor
    MongoDB locally (those clients back an OPTIONAL data-warehouse-export
    feature connecting to a CUSTOMER's own external database, not
    PostHog's own datastore). Section placement alone was never a
    reliable proxy for "this app needs a live local service" — it only
    proves the package installs by default, not what it is used for.
    Requiring corroboration for EVERY source (not just Cargo.lock) closes
    that gap, and it preserves the AI-449/lemmy incident coverage exactly
    (those manifests carried a [vars] endpoint alongside the client).
    """
    violations = []
    for client in (detect or {}).get("service_clients", []):
        seen_kinds = set()
        for term in client.get("search_terms", []):
            kind = LEAF_DATASTORE_DISPLAY.get(term)
            if not kind or kind in seen_kinds:
                continue
            seen_kinds.add(kind)
            if _manifest_wires_compose(manifest) or _service_covers(manifest, kind):
                continue

            scope = client.get("scope", "runtime")
            corroborated = (
                _vars_endpoint_kind_present(manifest, kind)
                or _compose_service_kind_present(detect, kind)
            )
            source = client.get("source")
            package = client.get("package")

            if scope != "runtime":
                violations.append(violation(
                    "leaf-datastore-not-served",
                    f"client '{package}' ({source}) implies {kind}, but no "
                    f"[services.*] serves it — detected in a dev/test/"
                    f"optional-only dependency section, not proof of a "
                    f"runtime need; confirm whether {kind} is actually used",
                    severity=ADVISORY,
                ))
                continue

            if not corroborated:
                violations.append(violation(
                    "leaf-datastore-not-served",
                    f"client '{package}' ({source}) implies {kind}, but no "
                    f"[services.*] serves it — no independent [vars] "
                    f"endpoint or compose service corroborates it, so a "
                    f"declared dependency alone isn't proof of a runtime "
                    f"need; confirm whether {kind} is actually used",
                    severity=ADVISORY,
                ))
                continue

            violations.append(violation(
                "leaf-datastore-not-served",
                f"client '{package}' ({source}) implies {kind}, but no "
                f"[services.*] serves it",
            ))
    return violations


def _looks_local(host):
    """True for hosts a Flox [services.*] block could plausibly be serving:
    loopback forms, `*.local`, and bare single-label names (no dot) —
    docker-compose/k8s service names like `postgres` or `db-primary`
    almost never carry a dot, unlike a real external FQDN. A host that
    doesn't match any of these (`db.prod.internal.example.com`, a public
    IP) is a strong signal the datastore is intentionally external — see
    check_vars_endpoints' ADVISORY downgrade for that case.
    """
    if not host:
        return True  # unparseable -- don't assume external on no evidence
    host = host.lower()
    if host in ("localhost", "0.0.0.0", "::1"):
        return True
    if re.match(r"^127(?:\.\d{1,3}){3}$", host):
        return True
    if host.endswith(".local"):
        return True
    if "." not in host and ":" not in host:
        return True
    return False


def check_vars_endpoints(detect, manifest):
    """A [vars] value that advertises a datastore connection string must be
    backed by a matching [services.*], or by a hook that genuinely starts
    it via `docker-compose up` (see `_manifest_wires_compose` — repo-side
    compose FILE presence alone does not count, AI-466 Hole 1).

    HARD when the host looks local (a Flox service could plausibly be the
    thing missing); ADVISORY when the host doesn't (`db.prod.internal.
    example.com`, a public IP) — a managed external datastore with no
    local service is a common, often intentional pattern, not necessarily
    a bug (see check_vars_endpoints' _looks_local for the exact rule).
    """
    violations = []
    for key, value in (manifest.get("vars", {}) or {}).items():
        if not isinstance(value, str):
            continue
        m = _CONN_STRING_RE.search(value)
        if not m:
            continue
        kind = _CONN_STRING_KIND[m.group(1).lower()]
        if _manifest_wires_compose(manifest) or _service_covers(manifest, kind):
            continue
        host = urlsplit(value[m.start():]).hostname
        if _looks_local(host):
            violations.append(violation(
                "vars-endpoint-not-served",
                f"[vars] {key}='{_truncate(value)}' advertises {kind} but "
                f"no [services.{kind}] serves it",
            ))
        else:
            violations.append(violation(
                "vars-endpoint-not-served",
                f"[vars] {key}='{_truncate(value)}' advertises {kind} at a "
                f"non-local host ('{host}') with no [services.{kind}] — "
                f"confirm this is an intentionally external/managed "
                f"datastore, not an oversight",
                severity=ADVISORY,
            ))
    return violations


# ---------------------------------------------------------------------------
# invariant 4 — [vars] are literal strings, never `$`-expanded
# ---------------------------------------------------------------------------

# Matches EXPANSION-SHAPED references only: `${VAR}` (braced -- always
# intentional shell syntax) or a bare `$UPPER_SNAKE_CASE` identifier,
# the standard env-var naming convention (`$FLOX_ENV_CACHE`, `$HOME`).
# Deliberately excludes any `$` followed by lowercase or digits, which is
# what a plain "any '$' at all" check used to false-fire HARD on: a
# password (`p@ss$word5`), a bcrypt hash (`$2b$10$...`), an argon2 hash
# (`$argon2id$v=19$...`) -- none of these contain an upper-snake-case
# identifier, so none match.
_VARS_EXPANSION_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Z][A-Z0-9_]*\b")


def check_vars_literal(manifest):
    violations = []
    for key, value in (manifest.get("vars", {}) or {}).items():
        if isinstance(value, str) and _VARS_EXPANSION_RE.search(value):
            violations.append(violation(
                "vars-not-literal",
                f"[vars] {key} contains '{value}' — [vars] are literal; "
                f"move to [hook]",
            ))
    return violations


# ---------------------------------------------------------------------------
# invariant 5 — hooks must not mutate the tracked git tree
# ---------------------------------------------------------------------------

# Global options that can appear BETWEEN `git` and its subcommand
# (`git -C <path> submodule update`, `git --git-dir=<path> checkout`, `git
# -c <name>=<value> commit`) -- AI-466 Hole 3: these let a mutating verb
# evade the plain "`git` directly followed by the verb" match entirely.
# Reproduced live: `git -C "$FLOX_ENV_PROJECT" submodule update --init`
# exited 0 while the bare form correctly fired HARD. Modeled on `git
# help`'s "OPTIONS" section for the global flags that take a path/value.
#
# The long options (--git-dir, --work-tree, --namespace) accept BOTH
# `--opt=value` and `--opt value` in real git -- AI-466 M1: the `=` form
# alone let the space form evade detection (`git --work-tree /tmp reset
# --hard` was a miss).
_GIT_GLOBAL_OPT = (
    r"(?:-C\s+\S+|-c\s+\S+|"
    r"--git-dir(?:=\S+|\s+\S+)|--work-tree(?:=\S+|\s+\S+)|"
    r"--namespace(?:=\S+|\s+\S+)|"
    r"--exec-path(?:=\S+)?|--bare|--no-pager|--paginate|-p)"
)
_GIT_MUTATION_RE = re.compile(
    r"\bgit\s+(?:" + _GIT_GLOBAL_OPT + r"\s+)*"
    r"(?:submodule\s+update|checkout|reset|clean|pull|commit|add|"
    r"stash|rm|mv|apply|cherry-pick|rebase|merge|restore|switch|revert)\b"
)

# Flags that turn an otherwise-mutating git verb into a dry run / read-only
# check in the SAME statement (e.g. `git apply --check patch.diff` validates
# a patch without touching the tree).
_GIT_READ_ONLY_FLAGS_RE = re.compile(r"--check\b|--dry-run\b|--stat\b")

_ECHO_OR_PRINTF_RE = re.compile(r"^\s*(echo|printf)\b")


def _strip_comment(line):
    """Strip a trailing `# ...` comment, respecting simple '/" quoting.

    Not a full shell parser — good enough for hook scripts, which are
    short and rarely nest quoting deeply. A `#` inside a quoted string
    (`echo "price is $5 #1"`) is left alone; an unquoted `#` starts a
    comment, matching how bash itself treats it.
    """
    in_single = in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return line[:i]
    return line


def check_hook_no_mutation(manifest):
    """Hooks run on EVERY activation — a hook that mutates the tracked git
    tree (`git submodule update`, `git checkout`, ...) re-mutates it every
    time the developer activates.

    Comments and `echo`/`printf` text are excluded before matching (a git
    verb *mentioned* in a comment or printed to the user is not executed),
    and a dry-run flag (`--check`, `--dry-run`, `--stat`) in the same
    statement exempts it (`git apply --check` validates without mutating).
    """
    violations = []
    hook = manifest.get("hook", {}) or {}
    script = hook.get("on-activate")
    if not isinstance(script, str):
        return violations

    seen = set()
    for raw_line in script.splitlines():
        line = _strip_comment(raw_line)
        if not line.strip():
            continue
        for stmt in re.split(r"[;&|]+", line):
            stmt = stmt.strip()
            if not stmt or _ECHO_OR_PRINTF_RE.match(stmt):
                continue
            if not _GIT_MUTATION_RE.search(stmt):
                continue
            if _GIT_READ_ONLY_FLAGS_RE.search(stmt):
                continue
            if stmt in seen:
                continue
            seen.add(stmt)
            violations.append(violation(
                "hook-mutates-tree",
                f"[hook] on-activate runs '{stmt}' — hooks run on every "
                f"activation and must not mutate the tracked git tree",
            ))
    return violations


# ---------------------------------------------------------------------------
# heuristic — network-fetching operations in on-activate (ADVISORY) — AI-450
# ---------------------------------------------------------------------------

# git verbs that fetch from a remote. Distinct concern from
# _GIT_MUTATION_RE (that regex is about working-tree SAFETY; this one is
# about NETWORK access on every activation) — `pull` and `submodule
# update` genuinely do both and deliberately appear in both regexes; a
# hook using either gets a HARD hook-mutates-tree violation AND an
# ADVISORY hook-network-fetch note, which is accurate, not redundant: two
# different risks, two different severities. Reuses _GIT_GLOBAL_OPT so
# `git -C <path> clone ...` doesn't evade this the same way AI-466 Hole 3
# found for the mutation check.
_GIT_NETWORK_RE = re.compile(
    r"\bgit\s+(?:" + _GIT_GLOBAL_OPT + r"\s+)*"
    r"(?:clone|fetch|pull|submodule\s+update)\b"
)

# curl/wget as the LEADING command of a statement — anchored the same way
# _ECHO_OR_PRINTF_RE is, so a mention as an argument to something else
# ("--user-agent curl-compatible") isn't mistaken for an invocation.
_CURL_WGET_RE = re.compile(r"^\s*(curl|wget)\b")


def check_hook_network(manifest):
    """ADVISORY: on-activate re-fetches something from the network on
    EVERY activation via a raw `git clone`/`fetch`/`pull`/`submodule
    update`, or a `curl`/`wget` download. Never HARD — hooks legitimately
    do network access in accepted idioms.

    Deliberately excludes ecosystem package-manager bootstraps (`uv
    sync`, `npm`/`pnpm`/`yarn install`, `bundle install`, `composer
    install`, `mix deps.get`, `corepack enable`) — every current golden's
    hook does exactly one of these, and they're the accepted way a
    manifest resolves its own dependencies on activation, not a
    second-guessed network fetch. This check targets the LOWER-level
    primitives underneath that: a raw git/curl/wget invocation usually
    means the hook is reaching outside the ecosystem's own dependency
    step to fetch something a package manager wouldn't (a sibling repo, a
    vendored file, a remote asset) — worth a second look, not a proven
    bug, since some repos genuinely need exactly that.

    Comments and echo/printf text are excluded before matching, same
    discipline as check_hook_no_mutation.
    """
    violations = []
    hook = manifest.get("hook", {}) or {}
    script = hook.get("on-activate")
    if not isinstance(script, str):
        return violations

    seen = set()
    for raw_line in script.splitlines():
        line = _strip_comment(raw_line)
        if not line.strip():
            continue
        for stmt in re.split(r"[;&|]+", line):
            stmt = stmt.strip()
            if not stmt or _ECHO_OR_PRINTF_RE.match(stmt):
                continue
            if not (_GIT_NETWORK_RE.search(stmt) or _CURL_WGET_RE.match(stmt)):
                continue
            if stmt in seen:
                continue
            seen.add(stmt)
            violations.append(violation(
                "hook-network-fetch",
                f"[hook] on-activate runs '{stmt}' — this fetches over the "
                f"network on every activation; confirm this is intentional "
                f"(ecosystem package-manager installs are the accepted "
                f"idiom for dependency fetching and are not flagged)",
                severity=ADVISORY,
            ))
    return violations


# ---------------------------------------------------------------------------
# invariant 6 — catalog resolution (pkg-path / version / per-system)
# ---------------------------------------------------------------------------

# Per-process, per-pkg-path cache -- avoids re-running `flox show` for the
# same pkg-path twice within one `verify()` call (or across the multiple
# goldens test_golden_lint.py checks in a single run). It does NOT persist
# across separate process invocations, and each dynamically-loaded module
# instance (see _skill_module_loader.py) gets its OWN empty cache -- the
# harness's per-task reload in run_floxify.py therefore pays for a fresh
# `flox show` per task rather than sharing a cache across the whole eval
# run. That's an accepted trade-off, not a bug: it keeps each task's
# result independent of load/call order, at the cost of some redundant
# network calls across a multi-task harness run.
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
    needs to mock to keep catalog checks off the network.

    `--` separates the positional pkg-path from option parsing: a
    manifest-derived pkg_path beginning with `-` (accidental or malicious)
    would otherwise be read as a flag by `flox show` instead of yielding a
    clean catalog-unresolved verdict.
    """
    return subprocess.run(
        [flox_bin, "show", "--", pkg_path], capture_output=True, text=True, timeout=timeout,
    )


def _parse_flox_show(text):
    """Parse `flox show <pkg-path>` output.

    Returns `{"latest": version-or-None, "latest_systems": set-or-None,
    "versions": {version: systems-set-or-None}}`.

    `latest_systems` comes from the header `Systems:` line, confirmed
    against live output to describe ONLY the `Latest:` entry (it matches
    that version's own "Other versions" parenthetical exactly) — NOT a
    default for every version, and never treated as one. Each "Other
    versions" entry carries its own systems: no parenthetical means all
    four platforms; "(sys1, sys2 only)" restricts it to exactly those.
    A parenthetical that does NOT end in "only" is a format this parser
    doesn't recognize, so that version's systems is None — genuinely
    unknown, never asserted as either present or absent (see
    check_catalog's handling of `available is None`).
    """
    latest = None
    m = re.search(r"^Latest:\s*\S+@(\S+)", text, re.M)
    if m:
        latest = m.group(1)

    latest_systems = None
    m = re.search(r"^Systems:\s*(.+)$", text, re.M)
    if m:
        latest_systems = {s.strip() for s in m.group(1).split(",") if s.strip()}

    versions = {}
    in_other = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "Other versions:":
            in_other = True
            continue
        if not in_other or not stripped:
            continue
        vm = re.match(r"^\S+@(\S+?)\s*(?:\((?P<paren>[^)]*)\))?$", stripped)
        if not vm:
            continue
        ver, paren = vm.group(1), vm.group("paren")
        if paren is None:
            systems = set(ALL_SYSTEMS)
        elif paren.rstrip().endswith("only"):
            names = paren.rsplit("only", 1)[0]
            systems = {s.strip() for s in names.split(",") if s.strip()}
        else:
            systems = None
        versions[ver] = systems
    return {"latest": latest, "latest_systems": latest_systems, "versions": versions}


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

    Returns `(violations, catalog_checked, unknown)`. `unknown` lists
    install entries whose per-system availability `flox show`'s own text
    didn't establish (see `_parse_flox_show`) — these are NOT asserted
    clean. A caller that reports "every pkg-path was confirmed" (the
    harness's judge note) must exclude this list from that claim, not
    silently fold it into a default-to-all-systems guess.
    """
    if not live or not shutil.which(flox_bin):
        return [], False, []

    violations = []
    unknown = []
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
                pkg_path=pkg_path, install_id=install_id,
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
                    pkg_path=pkg_path, install_id=install_id,
                ))
                continue
            available = show["versions"][matched]
            version_label = matched
        else:
            # Unpinned -> resolves to Latest. Ground truth for Latest's
            # systems is the header `Systems:` line (`latest_systems`),
            # not a guess — falling back to the "Other versions" entry
            # only if the header itself was unparseable.
            available = show.get("latest_systems")
            if available is None:
                available = show["versions"].get(show["latest"])
            version_label = show["latest"] or "latest"

        if available is None:
            # flox show's own text didn't establish this version's
            # systems — genuinely unknown. Never asserted clean OR
            # mismatched; excluded from the harness's "confirmed" table.
            unknown.append({"install_id": install_id, "pkg_path": pkg_path,
                            "version": version_label})
            continue

        missing = entry_systems - available
        if missing:
            violations.append(violation(
                "catalog-systems-mismatch",
                f"[install] {install_id}.pkg-path = \"{pkg_path}\" "
                f"(version {version_label}) has no build for "
                f"{', '.join(sorted(missing))}, but options.systems declares it",
                pkg_path=pkg_path, install_id=install_id,
            ))
    return violations, True, unknown


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
# heuristic — a compiled-extension runtime split from its native build dep
# (ADVISORY) — AI-464
# ---------------------------------------------------------------------------

# A package descriptor with no explicit pkg-group belongs to this group
# (Flox's own default — see the flox skill's SKILL.md). Shared by both
# AI-464 heuristics below.
DEFAULT_PKG_GROUP = "toplevel"

# detect.py's service_clients `source` file -> the RUNTIME_PKG_PATTERNS key
# it feeds gems/packages for. Grounded directly in detect.py's own
# _add_clients() call sites (Cargo.lock, pyproject.toml, requirements*.txt,
# package.json, Gemfile) -- not a guess.
_CLIENT_SOURCE_ECOSYSTEM = {
    "Cargo.lock": "rust",
    "pyproject.toml": "python",
    "requirements.txt": "python",
    "requirements-dev.txt": "python",
    "dev-requirements.txt": "python",
    "requirements-test.txt": "python",
    "requirements/base.txt": "python",
    "package.json": "node",
    "Gemfile": "ruby",
}

# search_terms that denote a library a compiled extension LINKS AGAINST at
# build time (headers/ABI coupling) -- as opposed to a pure runtime/network
# client (postgresql, redis, mariadb, mongodb-ce, elasticsearch, ffmpeg),
# which has no such coupling and is out of scope for this heuristic (that's
# check_leaf_datastore_services' territory). Sourced from the compiled-
# extension entries in detect.py's own SERVICE_CLIENTS table (psycopg2,
# cryptography, cffi, lxml, xmlsec, pillow, sharp, ruby-vips, ...).
NATIVE_LINK_TERMS = {
    "pkg-config", "openssl", "libffi", "libxml2", "libxslt",
    "libjpeg", "zlib", "vips", "cairo", "pango", "imagemagick",
}


def check_native_group_coherence(detect, manifest):
    """ADVISORY: a runtime whose ecosystem has a detected compiled-extension
    client (e.g. Python's psycopg2 needing pkg-config+openssl, Ruby's
    ruby-vips needing vips) should share a pkg-group with that native build
    dependency, not be split from it. A runtime's C extensions compile
    against headers from its own installed page and load libraries at
    runtime; splitting the two across pkg-groups risks each page resolving
    to versions whose ABI doesn't actually match, which the activation
    smoke test cannot catch (AI-464 — the mastodon golden's
    "runtime-and-native" group is the pattern this rewards: ruby_4_0 shares
    a group with postgresql/vips/icu/libidn rather than isolating alone).

    Never HARD: the "same ecosystem, same native term" match is a heuristic,
    not a proof the two are actually ABI-coupled at this specific version —
    worth a second look, not asserted as a bug.

    Requires BOTH the runtime and the native dep to already be present in
    [install] (matched by RUNTIME_PKG_PATTERNS and exact pkg-path,
    respectively) — a runtime or dep that isn't installed at all is a
    coverage gap for check_runtimes_installed / a human to notice, not a
    group-split this heuristic can speak to.
    """
    install = manifest.get("install", {}) or {}

    def _runtime_entry(ecosystem):
        pattern = RUNTIME_PKG_PATTERNS.get(ecosystem)
        if not pattern:
            return None, None
        for install_id, descriptor in install.items():
            if not isinstance(descriptor, dict):
                continue
            pkg_path = _pkg_path_str(descriptor)
            if pkg_path and pattern.match(pkg_path):
                return install_id, descriptor
        return None, None

    def _native_dep_entry(term):
        for install_id, descriptor in install.items():
            if not isinstance(descriptor, dict):
                continue
            if _pkg_path_str(descriptor) == term:
                return install_id, descriptor
        return None, None

    violations = []
    seen = set()
    for client in (detect or {}).get("service_clients", []):
        native_terms = set(client.get("search_terms", [])) & NATIVE_LINK_TERMS
        if not native_terms:
            continue
        ecosystem = _CLIENT_SOURCE_ECOSYSTEM.get(client.get("source"))
        if not ecosystem:
            continue
        runtime_id, runtime_descriptor = _runtime_entry(ecosystem)
        if runtime_id is None:
            continue
        runtime_group = runtime_descriptor.get("pkg-group", DEFAULT_PKG_GROUP)

        for term in sorted(native_terms):
            dep_id, dep_descriptor = _native_dep_entry(term)
            if dep_id is None:
                continue
            dep_group = dep_descriptor.get("pkg-group", DEFAULT_PKG_GROUP)
            if dep_group == runtime_group:
                continue
            key = (runtime_id, dep_id)
            if key in seen:
                continue
            seen.add(key)
            violations.append(violation(
                "native-group-split",
                f"{runtime_id}.pkg-path = \"{_pkg_path_str(runtime_descriptor)}\" "
                f"({ecosystem} runtime) and {dep_id}.pkg-path = \"{term}\" "
                f"(native build dep of {client.get('package')}, from "
                f"{client.get('source')}) are in different pkg-groups "
                f"({runtime_group} vs {dep_group}) — {ecosystem}'s compiled "
                f"extensions link against headers/libs from their own "
                f"installed page; splitting the runtime from its native "
                f"build deps risks an ABI mismatch the activation smoke "
                f"test can't catch",
                severity=ADVISORY,
            ))
    return violations


# ---------------------------------------------------------------------------
# heuristic — manifest fragmentation: too many single-package pkg-groups
# (ADVISORY) — AI-464
# ---------------------------------------------------------------------------

# Derived from the current goldens: posthog is the most fragmented at 5
# single-package groups (python313, nodejs-24, corepack-24, postgresql-15,
# redis-72 — each isolated because its exact pin didn't co-resolve with the
# others in toplevel, AI-457). Set at exactly that ceiling so today's
# goldens don't trip this heuristic while it still catches a manifest that
# fragments further than any current reference does.
#
# This value is COUPLED to posthog's specific shape, not an independent
# judgment call — the AI-464 golden audit flagged posthog's 5-way split as
# a candidate for collapse under the new escalation ladder (pin the
# toolchain, unpin the rest, keep one group). If that follow-up reduces
# posthog's single-package group count, tighten this ceiling to match
# rather than leaving it at a stale high-water mark.
MAX_SINGLE_PKG_GROUPS = 5


def check_group_fragmentation(manifest):
    """ADVISORY: more single-package pkg-groups than MAX_SINGLE_PKG_GROUPS
    signals the manifest reached for per-package isolation — the
    escalation ladder's LAST resort (AI-464) — more often than the
    pkg-group-economy goal prefers: every distinct group downloads its own
    full catalog closure down to libc, on top of forfeiting the version
    coherence a shared group gives compiled extensions. Never HARD — a
    manifest can legitimately need this many isolated pins; this is a
    nudge to re-examine (try pinning the toolchain and unpinning the
    libraries that must track it, or splitting along dependency seams,
    before isolating further), not a violation.
    """
    groups = {}
    for install_id, descriptor in (manifest.get("install", {}) or {}).items():
        if not isinstance(descriptor, dict):
            continue
        group = descriptor.get("pkg-group", DEFAULT_PKG_GROUP)
        if group == DEFAULT_PKG_GROUP:
            continue
        groups.setdefault(group, []).append(install_id)

    single_pkg_groups = sorted(g for g, ids in groups.items() if len(ids) == 1)
    if len(single_pkg_groups) <= MAX_SINGLE_PKG_GROUPS:
        return []

    names = ", ".join(single_pkg_groups)
    return [violation(
        "group-fragmentation",
        f"{len(single_pkg_groups)} single-package pkg-groups ({names}) "
        f"exceed the economy threshold of {MAX_SINGLE_PKG_GROUPS} — each "
        f"isolated group downloads its own full catalog closure; before "
        f"adding another, try the escalation ladder's earlier rungs (pin "
        f"the toolchain and unpin the libraries that must track it, or "
        f"split along dependency seams) instead of isolating further",
        severity=ADVISORY,
    )]


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------

def verify(detect, manifest_text, flox_bin="flox", check_catalog_live=True,
          catalog_timeout=30):
    """Run every check. Returns {"violations": [...], "catalog_checked": bool,
    "catalog_unknown": [...]}.

    `detect` may be None/{} — the detect-cross-check invariants (runtimes
    installed, leaf-datastore clients served, native-group coherence)
    degrade to no-ops when there are no facts to cross-check against; the
    manifest-only invariants ([vars] literal, hook mutation, hook network
    fetch, catalog resolution, outputs heuristic, group fragmentation)
    always run.

    `catalog_unknown` lists install entries the catalog leg could not
    establish per-system availability for (see check_catalog) — these are
    NEITHER violations NOR confirmed-clean; a caller claiming "every
    pkg-path was confirmed" (e.g. the harness's judge note) must exclude
    them from that claim.
    """
    detect = detect or {}
    manifest, parse_error = parse_manifest(manifest_text)
    if manifest is None:
        return {
            "violations": [violation(
                "invalid-toml", f"manifest.toml does not parse: {parse_error}",
            )],
            "catalog_checked": False,
            "catalog_unknown": [],
        }

    violations = []
    violations += check_runtimes_installed(detect, manifest)
    violations += check_leaf_datastore_services(detect, manifest)
    violations += check_vars_endpoints(detect, manifest)
    violations += check_vars_literal(manifest)
    violations += check_hook_no_mutation(manifest)
    violations += check_hook_network(manifest)
    catalog_violations, catalog_checked, catalog_unknown = check_catalog(
        manifest, flox_bin=flox_bin, live=check_catalog_live, timeout=catalog_timeout,
    )
    violations += catalog_violations
    violations += check_outputs_heuristic(detect, manifest)
    violations += check_native_group_coherence(detect, manifest)
    violations += check_group_fragmentation(manifest)

    return {
        "violations": violations,
        "catalog_checked": catalog_checked,
        "catalog_unknown": catalog_unknown,
    }


def _print_report(result):
    print(DISCLAIMER)
    hard = hard_violations(result)
    advisory = advisory_violations(result)

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
    unknown = result.get("catalog_unknown") or []
    if unknown:
        names = ", ".join(u["install_id"] for u in unknown)
        print(
            f"\nNOTE: {len(unknown)} install entr{'y' if len(unknown) == 1 else 'ies'} "
            f"({names}) had UNKNOWN per-system availability — `flox show`'s "
            f"own text didn't establish it, so it was neither confirmed nor "
            f"flagged."
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

    return 1 if hard_violations(result) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
