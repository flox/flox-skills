#!/usr/bin/env python3
"""floxify repo analyzer — deterministic fact extraction (stdlib only).

Scans a project directory and emits a single JSON object of *grounded facts*:
runtime version pins (with the file each came from), package-manager versions
taken from lockfiles, docker-compose services, service-client dependencies, and
monorepo/orchestrator markers.

Why this exists: the floxify skill's Phase 1 (reading the project) is the step
where a model is most tempted to *guess* — infer a Ruby version it half-remembers,
assume `pg` means Postgres 15, invent a bundler version. Every fact this script
emits is read straight from a file and tagged with its source, so the skill
resolves packages from evidence instead of memory.

What it deliberately does NOT do: it never touches the Flox catalog. Mapping a
detected runtime or client library to a catalog `pkg-path` (and picking a
version that actually exists) stays with the model via `flox search` / `flox
show`. Fields named `*_hint` / `search_terms` are search *suggestions to verify*,
never asserted package names.

Pure stdlib — no third-party imports — so it runs under a bare interpreter:
    flox run -p python313 -- python3 detect.py <dir>
    python3 detect.py <dir>          # if a python is already on PATH

Output: one JSON object on stdout. Never raises on a malformed input file — a
parse failure is recorded under "notes" and scanning continues.
"""
import json
import os
import re
import sys
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - fallback for < 3.11
    tomllib = None

# Directories that never hold source-of-truth pins — pruned during the walk.
IGNORE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__", ".tox",
    "vendor", "dist", "build", "target", ".next", "coverage", ".cache",
    ".mypy_cache", ".pytest_cache", ".gradle", ".idea", ".vscode",
    "site-packages", ".terraform", ".serverless",
}

# Client library  ->  catalog SEARCH TERMS to verify (never asserted names).
# Mirrors the skill's own "Services and system dependencies" table so the
# grounded scan and the skill's guidance agree on what a client implies.
SERVICE_CLIENTS = {
    # PostgreSQL
    "psycopg2": ["postgresql", "pkg-config", "openssl"],
    "psycopg2-binary": ["postgresql"],
    "psycopg": ["postgresql"],
    "asyncpg": ["postgresql"],
    "pg8000": ["postgresql"],
    "pg": ["postgresql"],            # npm
    "pg-native": ["postgresql"],
    "postgres": ["postgresql"],      # npm (porsager/postgres)
    # Redis
    "redis": ["redis"],
    "ioredis": ["redis"],
    "hiredis": ["redis"],
    "celery": ["redis"],
    # MySQL / MariaDB
    "pymysql": ["mariadb"],
    "mysqlclient": ["mariadb", "pkg-config"],
    "mysql-connector-python": ["mariadb"],
    "mysql2": ["mariadb"],           # ruby / npm
    "mysql": ["mariadb"],
    # MongoDB
    "pymongo": ["mongodb-ce"],
    "motor": ["mongodb-ce"],
    "mongoose": ["mongodb-ce"],
    "mongodb": ["mongodb-ce"],
    # PostgreSQL (Rust — Cargo.lock package names)
    "pq-sys": ["postgresql"],            # native libpq binding; diesel's "postgres" feature pulls this in
    "tokio-postgres": ["postgresql"],
    "sqlx-postgres": ["postgresql"],
    # Redis (Rust)
    "fred": ["redis"],
    # MySQL / MariaDB (Rust)
    "mysql_async": ["mariadb"],
    "sqlx-mysql": ["mariadb"],
    # Native crypto / parsing / imaging
    "cryptography": ["pkg-config", "openssl"],
    "cffi": ["pkg-config", "libffi"],
    "bcrypt": ["openssl"],
    "pynacl": ["openssl"],
    "lxml": ["libxml2", "libxslt"],
    "xmlsec": ["libxml2", "libxslt", "openssl", "pkg-config"],
    "python-xmlsec": ["libxml2", "libxslt", "openssl", "pkg-config"],
    "pillow": ["libjpeg", "zlib"],
    "sharp": ["vips"],               # npm image processing
    "canvas": ["cairo", "pango", "libjpeg"],  # npm
    "image_processing": ["vips"],    # ruby
    "ruby-vips": ["vips"],
    "mini_magick": ["imagemagick"],
    # Search / media
    "elasticsearch": ["elasticsearch"],
    "@elastic/elasticsearch": ["elasticsearch"],
    "fluent-ffmpeg": ["ffmpeg"],
    "ffmpeg-static": ["ffmpeg"],
    "streamio-ffmpeg": ["ffmpeg"],
}

# apt package (Dockerfile RUN apt-get install ...)  ->  catalog search terms.
APT_NATIVE = {
    "libpq-dev": ["postgresql"], "libpq": ["postgresql"], "postgresql-client": ["postgresql"],
    "libvips": ["vips"], "libvips-dev": ["vips"], "libvips42": ["vips"],
    "ffmpeg": ["ffmpeg"],
    "libxml2-dev": ["libxml2"], "libxml2": ["libxml2"],
    "libxslt1-dev": ["libxslt"], "libxslt-dev": ["libxslt"], "libxslt1.1": ["libxslt"],
    "libjpeg-dev": ["libjpeg"], "libjpeg62-turbo-dev": ["libjpeg"],
    "zlib1g-dev": ["zlib"],
    "libssl-dev": ["openssl"], "openssl": ["openssl"],
    "pkg-config": ["pkg-config"], "pkgconf": ["pkg-config"],
    "imagemagick": ["imagemagick"], "libmagickwand-dev": ["imagemagick"],
    "libmagic-dev": ["file"], "libmagic1": ["file"],
    "libyaml-dev": ["libyaml"], "libffi-dev": ["libffi"],
    "libsqlite3-dev": ["sqlite"], "libcurl4-openssl-dev": ["curl"],
    "gcc": ["gcc"], "g++": ["gcc"], "build-essential": ["gcc", "gnumake"],
    "cmake": ["cmake"], "make": ["gnumake"],
    "libpcre3-dev": ["pcre"], "libgmp-dev": ["gmp"], "libidn11-dev": ["libidn"],
    "libprotobuf-dev": ["protobuf"], "protobuf-compiler": ["protobuf"],
}

LOCKFILES = [
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
    "uv.lock", "poetry.lock", "Pipfile.lock", "requirements.txt",
    "Gemfile.lock", "Cargo.lock", "go.sum", "composer.lock",
    "mix.lock", "pubspec.lock",
]

# .tool-versions / .mise language keys -> canonical runtime language.
TOOL_LANG = {
    "nodejs": "node", "node": "node", "python": "python", "ruby": "ruby",
    "golang": "go", "go": "go", "rust": "rust", "erlang": "erlang",
    "elixir": "elixir", "java": "java", "dotnet": "dotnet", "deno": "deno",
    "bun": "bun", "php": "php", "terraform": "terraform",
}


# --------------------------------------------------------------------------
# small IO helpers
# --------------------------------------------------------------------------

def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def _first_line(text):
    if not text:
        return None
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            return s
    return None


def _rel(target, path):
    try:
        return str(Path(path).relative_to(target))
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------
# walk (depth-limited, IGNORE-pruned) to find nested config files
# --------------------------------------------------------------------------

def _walk(target, max_depth=4):
    """Yield file paths under target, pruning IGNORE_DIRS and capping depth."""
    target = Path(target)
    base_depth = len(target.parts)
    for root, dirs, files in os.walk(target):
        depth = len(Path(root).parts) - base_depth
        if depth >= max_depth:
            dirs[:] = []
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        for f in files:
            yield Path(root) / f


# --------------------------------------------------------------------------
# per-source extractors — each appends to the shared accumulator dicts
# --------------------------------------------------------------------------

def _runtime(lang, version, source, raw=None):
    return {"language": lang, "version": version, "source": source,
            "raw": raw if raw is not None else version}


def _add_clients(dep_names, source, acc, scope="runtime"):
    """Map dependency names to service-client search hints.

    `scope` records WHERE in the dependency manifest a name came from —
    "runtime" (installed unconditionally: npm `dependencies`, a Gemfile
    gem outside any `group do...end` block, Python's `[project.
    dependencies]`) or "dev" (dev/test/optional-only: npm
    `devDependencies`, a Gemfile gem inside a `:test`/`:development`
    group, Python's `[project.optional-dependencies]` / PEP 735
    `[dependency-groups]` / poetry's `[tool.poetry.group.*]`). verify.py
    uses this to decide HARD vs ADVISORY severity for the leaf-datastore
    invariant (AI-467) — a dev-only client is not evidence the app needs
    a live service in the environment being set up.
    """
    for name in dep_names:
        key = name.strip().lower()
        if key in SERVICE_CLIENTS:
            acc.append({"package": name, "search_terms": SERVICE_CLIENTS[key],
                        "source": source, "scope": scope})


def _parse_toml(text):
    if not text or tomllib is None:
        return None
    try:
        return tomllib.loads(text)
    except Exception:
        return None


def _dep_names_from_pep508(items):
    """['fastapi>=0.1', 'uvicorn[standard]>=0'] -> ['fastapi','uvicorn']."""
    out = []
    for item in items or []:
        if not isinstance(item, str):
            continue
        m = re.match(r"^\s*([A-Za-z0-9._-]+)", item)
        if m:
            out.append(m.group(1))
    return out


# Gemfile `group :name, ... do ... end` names that signal the gems inside
# are not installed in a production/runtime environment by default (the
# common Rails/Bundler convention). A group with an unrecognized name
# (`:production`, or a custom one) defaults to "runtime" -- erring toward
# not missing a real dependency over erring toward hiding one.
_GEMFILE_DEV_GROUP_NAMES = {"test", "development", "dev", "cucumber", "rspec"}

# requirements.txt sibling filenames that signal a dev/test-only file by
# their name alone (the pip-tools/pip ecosystem has no single convention).
_REQUIREMENTS_DEV_NAMES = {
    "requirements-dev.txt", "dev-requirements.txt", "requirements-test.txt",
}


def _gemfile_gems_by_scope(text):
    """Returns (runtime_gems, dev_gems) -- gems inside a `group ... do
    ... end` block naming a dev/test group are "dev"; everything else
    (top-level gems, and gems inside a non-dev-named group like
    `:production`) is "runtime". Simple `do`/`end` depth tracking, not a
    full Ruby parser -- good enough for Gemfile's narrow DSL.
    """
    runtime_gems, dev_gems = [], []
    group_is_dev_stack = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0]
        stripped = line.strip()
        if not stripped:
            continue
        gm = re.match(r"^group\s+(.+?)\s+do\b", stripped)
        if gm:
            names = re.findall(r":(\w+)", gm.group(1))
            # A gem declared under multiple group names (`group :production,
            # :test do`) belongs to ALL of them simultaneously in Bundler --
            # if ANY named group is non-dev (e.g. :production), the gem IS
            # installed in that context, so it must NOT be marked dev-scoped.
            # Only mark dev when EVERY named group is dev/test-only.
            group_is_dev_stack.append(
                bool(names) and all(n.lower() in _GEMFILE_DEV_GROUP_NAMES for n in names)
            )
            continue
        if re.match(r"^end\b", stripped) and group_is_dev_stack:
            group_is_dev_stack.pop()
            continue
        m = re.match(r'^gem\s+["\']([^"\']+)["\']', stripped)
        if m:
            (dev_gems if any(group_is_dev_stack) else runtime_gems).append(m.group(1))
    return runtime_gems, dev_gems


def scan(target):
    target = Path(target).resolve()
    runtimes, pkg_mgrs, services = [], [], []
    clients, native, orchestrators, monorepo_markers = [], [], [], []
    lockfiles, scanned, notes = [], [], []
    ecosystems = set()

    def note(msg):
        if msg not in notes:
            notes.append(msg)

    root_files = {p.name for p in target.iterdir() if p.is_file()} if target.is_dir() else set()
    root_dirs = {p.name for p in target.iterdir() if p.is_dir()} if target.is_dir() else set()

    # ---- root dotfile version pins -------------------------------------
    def mark(fname):
        scanned.append(fname)

    if ".python-version" in root_files:
        v = _first_line(_read(target / ".python-version"))
        if v:
            runtimes.append(_runtime("python", v, ".python-version"))
            ecosystems.add("python")
        mark(".python-version")

    for nf in (".nvmrc", ".node-version"):
        if nf in root_files:
            v = _first_line(_read(target / nf))
            if v:
                runtimes.append(_runtime("node", v.lstrip("vV"), nf, raw=v))
                ecosystems.add("node")
            mark(nf)

    if ".ruby-version" in root_files:
        v = _first_line(_read(target / ".ruby-version"))
        if v:
            runtimes.append(_runtime("ruby", v.replace("ruby-", ""), ".ruby-version", raw=v))
            ecosystems.add("ruby")
        mark(".ruby-version")

    # ---- .tool-versions / .mise.toml (multi-runtime) -------------------
    if ".tool-versions" in root_files:
        text = _read(target / ".tool-versions") or ""
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 2 and not line.strip().startswith("#"):
                lang = TOOL_LANG.get(parts[0].lower())
                if lang:
                    runtimes.append(_runtime(lang, parts[1], ".tool-versions"))
                    ecosystems.add(lang)
        mark(".tool-versions")

    for mf in (".mise.toml", "mise.toml", ".config/mise/config.toml"):
        p = target / mf
        if p.is_file():
            data = _parse_toml(_read(p))
            tools = (data or {}).get("tools", {}) if isinstance(data, dict) else {}
            if not isinstance(tools, dict):
                # AI-485 F7: `tools = [...]` (array) instead of a table --
                # valid TOML, but tomllib happily hands back a list here;
                # `.items()` on it used to crash with AttributeError.
                note(f"{mf}: [tools] is not a table")
                tools = {}
            for k, v in tools.items():
                lang = TOOL_LANG.get(k.lower())
                ver = v if isinstance(v, str) else (v.get("version") if isinstance(v, dict) else None)
                if lang and ver:
                    runtimes.append(_runtime(lang, str(ver), mf))
                    ecosystems.add(lang)
            if data is None and _read(p):
                note(f"{mf}: could not parse TOML")
            mark(mf)

    # ---- go.mod --------------------------------------------------------
    if "go.mod" in root_files:
        text = _read(target / "go.mod") or ""
        m = re.search(r"^go\s+(\d+\.\d+(?:\.\d+)?)", text, re.M)
        if m:
            runtimes.append(_runtime("go", m.group(1), "go.mod"))
            ecosystems.add("go")
        mm = re.search(r"^module\s+(\S+)", text, re.M)
        if mm:
            note(f"go module: {mm.group(1)}")
        mark("go.mod")

    # ---- rust-toolchain(.toml) + Cargo.toml ----------------------------
    for rt in ("rust-toolchain.toml", "rust-toolchain"):
        if rt in root_files:
            text = _read(target / rt) or ""
            data = _parse_toml(text) if rt.endswith(".toml") else None
            chan = None
            if isinstance(data, dict):
                chan = (data.get("toolchain") or {}).get("channel")
            if not chan:
                m = re.search(r'channel\s*=\s*"([^"]+)"', text) or re.search(r"^(\S+)", text.strip())
                chan = m.group(1) if m else None
            if chan:
                runtimes.append(_runtime("rust", chan, rt))
                ecosystems.add("rust")
            mark(rt)
    if "Cargo.toml" in root_files:
        if not any(r["language"] == "rust" for r in runtimes):
            runtimes.append(_runtime("rust", None, "Cargo.toml", raw="(edition/toolchain default)"))
        ecosystems.add("rust")
        if (target / "build.rs").is_file():
            native.append({"trigger": "build.rs", "search_terms": ["pkg-config", "gcc"],
                           "source": "Cargo.toml + build.rs"})
        mark("Cargo.toml")

    # ---- Cargo.lock: resolved crate names -> service-client search hints
    # `[[package]]` is TOML array-of-tables, so this parses the same way as
    # any other TOML file here (no custom lockfile grammar). Cargo.lock does
    # not record which crate FEATURES were enabled (e.g. diesel's "postgres"
    # feature), so this deliberately keys off crate names that are only
    # pulled into the lockfile when a matching feature/driver is actually in
    # use (pq-sys arrives via diesel's postgres feature; sqlx-postgres is its
    # own published crate, not a feature flag) -- the same "evidence read
    # from a file, not inferred" discipline as every other extractor here.
    if "Cargo.lock" in root_files:
        data = _parse_toml(_read(target / "Cargo.lock"))
        names = []
        if isinstance(data, dict):
            for pkg in data.get("package") or []:
                if isinstance(pkg, dict) and pkg.get("name"):
                    names.append(pkg["name"])
        elif _read(target / "Cargo.lock"):
            note("Cargo.lock: could not parse TOML")
        _add_clients(names, "Cargo.lock", clients)
        mark("Cargo.lock")

    # ---- pyproject.toml / Pipfile --------------------------------------
    if "pyproject.toml" in root_files:
        data = _parse_toml(_read(target / "pyproject.toml"))
        if isinstance(data, dict):
            proj = data.get("project", {}) if isinstance(data.get("project"), dict) else {}
            rp = proj.get("requires-python")
            deps = _dep_names_from_pep508(proj.get("dependencies"))
            tool = data.get("tool", {}) if isinstance(data.get("tool"), dict) else {}
            poetry = tool.get("poetry", {}) if isinstance(tool.get("poetry"), dict) else {}
            if not rp and isinstance(poetry.get("dependencies"), dict):
                rp = poetry["dependencies"].get("python")
                deps += [k for k in poetry["dependencies"].keys() if k.lower() != "python"]
            if rp:
                runtimes.append(_runtime("python", str(rp), "pyproject.toml (requires-python)"))
                ecosystems.add("python")
            _add_clients(deps, "pyproject.toml", clients, scope="runtime")

            # ---- dev/optional-scoped deps: PEP 621 optional-dependencies
            # (extras, opt-in), PEP 735 [dependency-groups] (dev/test
            # tooling), and poetry's [tool.poetry.group.*.dependencies] ----
            dev_deps = []
            optional = proj.get("optional-dependencies")
            if isinstance(optional, dict):
                for extra_deps in optional.values():
                    if isinstance(extra_deps, list):
                        dev_deps += _dep_names_from_pep508(extra_deps)
            dep_groups = data.get("dependency-groups")
            if isinstance(dep_groups, dict):
                for group_deps in dep_groups.values():
                    if isinstance(group_deps, list):
                        dev_deps += _dep_names_from_pep508(
                            [d for d in group_deps if isinstance(d, str)]
                        )
            poetry_groups = poetry.get("group") if isinstance(poetry.get("group"), dict) else {}
            for group_data in poetry_groups.values():
                group_deps = group_data.get("dependencies") if isinstance(group_data, dict) else None
                if isinstance(group_deps, dict):
                    dev_deps += [k for k in group_deps.keys() if k.lower() != "python"]
            _add_clients(dev_deps, "pyproject.toml", clients, scope="dev")
            # package manager
            if "uv.lock" in root_files or "uv" in tool:
                pkg_mgrs.append({"name": "uv", "version": None,
                                 "source": "uv.lock" if "uv.lock" in root_files else "pyproject [tool.uv]"})
            elif poetry or "poetry.lock" in root_files:
                pkg_mgrs.append({"name": "poetry", "version": None,
                                 "source": "poetry.lock" if "poetry.lock" in root_files else "pyproject [tool.poetry]"})
            if proj.get("name"):
                note(f"python project: {proj['name']}")
        elif _read(target / "pyproject.toml"):
            note("pyproject.toml: could not parse TOML")
        mark("pyproject.toml")

    if "Pipfile" in root_files:
        text = _read(target / "Pipfile") or ""
        m = re.search(r'python_version\s*=\s*"([^"]+)"', text)
        if m:
            runtimes.append(_runtime("python", m.group(1), "Pipfile [requires]"))
            ecosystems.add("python")
        mark("Pipfile")

    for reqf in (
        "requirements.txt", "requirements-dev.txt", "dev-requirements.txt",
        "requirements-test.txt", "requirements/base.txt",
    ):
        p = target / reqf
        if p.is_file():
            names = []
            for line in (_read(p) or "").splitlines():
                s = line.strip()
                if s and not s.startswith(("#", "-")):
                    mm = re.match(r"^([A-Za-z0-9._-]+)", s)
                    if mm:
                        names.append(mm.group(1))
            # "dev"/"test" in the filename is the project's own signal this
            # file is not installed in a runtime environment by default.
            scope = "dev" if reqf in _REQUIREMENTS_DEV_NAMES else "runtime"
            _add_clients(names, reqf, clients, scope=scope)
            if "python" not in ecosystems and names:
                ecosystems.add("python")
            mark(reqf)

    # ---- package.json --------------------------------------------------
    if "package.json" in root_files:
        pj = None
        parsed_ok = True
        try:
            pj = json.loads(_read(target / "package.json") or "{}")
        except Exception:
            parsed_ok = False
            note("package.json: could not parse JSON")
        if parsed_ok and not isinstance(pj, dict):
            # Valid JSON that isn't an object at all -- an array
            # (`[1, 2, 3]`) or, degenerately, the literal `null` (which
            # parses to Python None just like the parse-failure branch
            # above, so `parsed_ok` -- not an `is not None` check -- is
            # what tells the two apart and avoids double-noting).
            note("package.json: not a JSON object")
        if not isinstance(pj, dict):
            pj = {}
        eng = pj.get("engines", {}) if isinstance(pj.get("engines"), dict) else {}
        if eng.get("node"):
            runtimes.append(_runtime("node", str(eng["node"]), "package.json (engines.node)"))
            ecosystems.add("node")
        volta = pj.get("volta", {}) if isinstance(pj.get("volta"), dict) else {}
        if volta.get("node"):
            runtimes.append(_runtime("node", str(volta["node"]), "package.json (volta.node)"))
            ecosystems.add("node")
        pm = pj.get("packageManager")
        if isinstance(pm, str) and "@" in pm:
            nm, _, ver = pm.partition("@")
            pkg_mgrs.append({"name": nm, "version": ver, "source": "package.json (packageManager)"})
            ecosystems.add("node")
        # AI-485 F6: `"dependencies": [...]` (array) instead of `{...}` --
        # valid JSON, wrong shape. `(pj.get(...) or {}).keys()` used to
        # crash with AttributeError the moment either field was a list.
        deps_raw = pj.get("dependencies")
        dev_deps_raw = pj.get("devDependencies")
        deps = list(deps_raw.keys()) if isinstance(deps_raw, dict) else []
        dev_deps = list(dev_deps_raw.keys()) if isinstance(dev_deps_raw, dict) else []
        _add_clients(deps, "package.json", clients, scope="runtime")
        _add_clients(dev_deps, "package.json", clients, scope="dev")
        if pj.get("workspaces"):
            monorepo_markers.append("package.json workspaces")
        if pj.get("name"):
            note(f"node package: {pj['name']}")
        mark("package.json")

    # ---- Ruby: Gemfile + Gemfile.lock ----------------------------------
    if "Gemfile" in root_files:
        text = _read(target / "Gemfile") or ""
        m = re.search(r'^\s*ruby\s+["\']([^"\']+)["\']', text, re.M)
        if m:
            runtimes.append(_runtime("ruby", m.group(1), "Gemfile"))
            ecosystems.add("ruby")
        runtime_gems, dev_gems = _gemfile_gems_by_scope(text)
        _add_clients(runtime_gems, "Gemfile", clients, scope="runtime")
        _add_clients(dev_gems, "Gemfile", clients, scope="dev")
        ecosystems.add("ruby")
        mark("Gemfile")
    if "Gemfile.lock" in root_files:
        text = _read(target / "Gemfile.lock") or ""
        m = re.search(r"BUNDLED WITH\s*\n\s*([0-9][0-9.]*)", text)
        if m:
            pkg_mgrs.append({"name": "bundler", "version": m.group(1),
                             "source": "Gemfile.lock (BUNDLED WITH)"})
        rv = re.search(r"RUBY VERSION\s*\n\s*ruby\s+([0-9][0-9.]*)", text)
        if rv:
            runtimes.append(_runtime("ruby", rv.group(1), "Gemfile.lock (RUBY VERSION)"))
            ecosystems.add("ruby")
        mark("Gemfile.lock")

    # ---- other single-signal manifests ---------------------------------
    if "global.json" in root_files:
        try:
            gj = json.loads(_read(target / "global.json") or "{}")
            ver = (gj.get("sdk") or {}).get("version")
            if ver:
                runtimes.append(_runtime("dotnet", ver, "global.json"))
                ecosystems.add("dotnet")
        except Exception:
            note("global.json: could not parse JSON")
        mark("global.json")
    if "mix.exs" in root_files:
        text = _read(target / "mix.exs") or ""
        m = re.search(r'elixir:\s*"([^"]+)"', text)
        runtimes.append(_runtime("elixir", m.group(1) if m else None, "mix.exs"))
        ecosystems.add("elixir")
        mark("mix.exs")
    if "build.sbt" in root_files:
        text = _read(target / "build.sbt") or ""
        m = re.search(r'scalaVersion\s*:=\s*"([^"]+)"', text)
        runtimes.append(_runtime("scala", m.group(1) if m else None, "build.sbt"))
        ecosystems.add("scala")
        mark("build.sbt")
    if "pubspec.yaml" in root_files:
        text = _read(target / "pubspec.yaml") or ""
        lang = "flutter" if re.search(r"^\s*flutter:", text, re.M) else "dart"
        m = re.search(r"sdk:\s*['\"]?([^'\"\n]+)", text)
        runtimes.append(_runtime(lang, m.group(1).strip() if m else None, "pubspec.yaml"))
        ecosystems.add(lang)
        mark("pubspec.yaml")
    if "Package.swift" in root_files:
        runtimes.append(_runtime("swift", None, "Package.swift"))
        ecosystems.add("swift")
        mark("Package.swift")
    if "build.zig" in root_files or "build.zig.zon" in root_files:
        runtimes.append(_runtime("zig", None, "build.zig"))
        ecosystems.add("zig")
        mark("build.zig")
    if "composer.json" in root_files:
        try:
            cj = json.loads(_read(target / "composer.json") or "{}")
            ver = (cj.get("require") or {}).get("php")
            runtimes.append(_runtime("php", ver, "composer.json (require.php)"))
        except Exception:
            note("composer.json: could not parse JSON")
        ecosystems.add("php")
        mark("composer.json")

    # ---- Aptfile (heroku-buildpack-apt / Mastodon: one apt pkg per line)
    # Native C-extension system libs frequently live here, NOT in the
    # language manifest (e.g. Mastodon's vips/ffmpeg/icu/libidn).
    if "Aptfile" in root_files:
        found = set()
        for line in (_read(target / "Aptfile") or "").splitlines():
            tok = line.split("#")[0].strip()
            if tok in APT_NATIVE:
                found.add(tok)
        for tok in sorted(found):
            native.append({"trigger": f"Aptfile {tok}",
                           "search_terms": APT_NATIVE[tok], "source": "Aptfile"})
        mark("Aptfile")

    # ---- Deno (config files; edge-runtime compose image handled below) -
    deno_srcs = sorted({_rel(target, p) for p in _walk(target, max_depth=3)
                        if p.name in ("deno.json", "deno.jsonc")})
    if deno_srcs:
        runtimes.append(_runtime("deno", None, deno_srcs[0]))
        ecosystems.add("deno")
        if len(deno_srcs) > 1:
            note(f"deno config found in {len(deno_srcs)} locations")

    # ---- Dockerfiles (FROM runtime hints + apt native deps) ------------
    for p in _walk(target, max_depth=3):
        if p.name in ("Dockerfile", "Dockerfile.dev") or p.name.startswith("Dockerfile."):
            text = _read(p) or ""
            rel = _rel(target, p)
            for fm in re.finditer(
                r"^FROM\s+(?:--platform=\S+\s+)?([A-Za-z0-9._/-]+):([A-Za-z0-9._-]+)",
                text, re.M,
            ):
                img, tag = fm.group(1), fm.group(2)
                base = img.split("/")[-1]
                lang = {"ruby": "ruby", "python": "python", "node": "node",
                        "golang": "go", "rust": "rust", "php": "php",
                        "eclipse-temurin": "java", "openjdk": "java"}.get(base)
                if lang:
                    note(f"Dockerfile FROM {base}:{tag} (runtime hint, {rel})")
            apt = re.findall(r"apt-get\s+install[^\n&|]*", text)
            found = set()
            for chunk in apt:
                for tok in re.split(r"[\s\\]+", chunk):
                    tok = tok.strip()
                    if tok in APT_NATIVE:
                        found.add(tok)
            for tok in sorted(found):
                native.append({"trigger": f"apt install {tok}",
                               "search_terms": APT_NATIVE[tok], "source": rel})
            scanned.append(rel)

    # ---- docker-compose services ---------------------------------------
    for p in _walk(target, max_depth=3):
        if re.match(r"^(docker-)?compose(\.[\w.-]+)?\.ya?ml$", p.name):
            rel = _rel(target, p)
            for svc in _parse_compose(_read(p) or ""):
                svc["source"] = rel
                services.append(svc)
            scanned.append(rel)

    # A `*-edge-runtime` service image (Supabase et al.) implies a Deno runtime
    # even when there's no root deno.json — pinning only Node would be wrong.
    if not any(r["language"] == "deno" for r in runtimes):
        for s in services:
            if "edge-runtime" in (s.get("image_base") or ""):
                runtimes.append(_runtime("deno", None, f"{s['source']} ({s['image']})"))
                ecosystems.add("deno")
                note("deno runtime implied by an edge-runtime compose image")
                break

    # ---- CI workflows: setup-* action versions + service images --------
    wf_dir = target / ".github" / "workflows"
    if wf_dir.is_dir():
        for p in wf_dir.glob("*.y*ml"):
            text = _read(p) or ""
            rel = _rel(target, p)
            for lang, key in (("node", "node-version"), ("python", "python-version"),
                              ("go", "go-version")):
                m = re.search(key + r":\s*['\"]?([0-9][0-9.xX*]*)", text)
                if m:
                    runtimes.append(_runtime(lang, m.group(1), f"{rel} (setup-{lang})"))
                    ecosystems.add(lang)

    # ---- orchestrators (custom service managers) -----------------------
    ORCH = [
        ("devservices", target / "devservices", "run: devservices up"),
        ("tilt", target / "Tiltfile", "run: tilt up"),
        ("skaffold", target / "skaffold.yaml", "run: skaffold dev"),
        ("devspace", target / "devspace.yaml", "run: devspace dev"),
        ("ctlptl", target / "ctlptl.yaml", "run: ctlptl apply"),
    ]
    for name, path, hint in ORCH:
        if path.exists():
            orchestrators.append({"tool": name, "signal": _rel(target, path), "hint": hint})
    for f in root_files:
        if f.startswith("k3d-") and f.endswith(".yaml"):
            orchestrators.append({"tool": "k3d", "signal": f, "hint": "run: k3d cluster start"})

    # ---- monorepo markers ----------------------------------------------
    for marker in ("pnpm-workspace.yaml", "turbo.json", "nx.json", "lerna.json", "rush.json"):
        if marker in root_files:
            monorepo_markers.append(marker)
    gomods = [_rel(target, p) for p in _walk(target, max_depth=3) if p.name == "go.mod"]
    if len(gomods) > 1:
        monorepo_markers.append(f"{len(gomods)} go.mod files")

    # ---- lockfiles present ---------------------------------------------
    for lf in LOCKFILES:
        if (target / lf).is_file():
            lockfiles.append(lf)

    # ---- de-dupe runtimes (keep all provenance, drop exact dups) -------
    seen, uniq_rt = set(), []
    for r in runtimes:
        k = (r["language"], r["version"], r["source"])
        if k not in seen:
            seen.add(k)
            uniq_rt.append(r)

    return {
        "target": str(target),
        "ecosystems": sorted(ecosystems),
        "runtimes": uniq_rt,
        "package_managers": pkg_mgrs,
        "services": services,
        "service_clients": clients,
        "native_hints": native,
        "orchestrators": orchestrators,
        "monorepo": {"detected": bool(monorepo_markers), "markers": sorted(set(monorepo_markers))},
        "lockfiles": sorted(set(lockfiles)),
        "files_scanned": sorted(set(scanned)),
        "notes": notes,
        "_meta": {
            "analyzer": "floxify/detect.py",
            "disclaimer": "search_terms are catalog hints to verify with `flox "
                          "search` / `flox show` — never assert them as pkg-paths.",
        },
    }


# --------------------------------------------------------------------------
# docker-compose service extractor (best-effort, indentation-based)
# --------------------------------------------------------------------------

def _parse_compose(text):
    """Extract [{name, image, image_base, tag, kind}] from a compose file.

    Best-effort and stdlib-only: tracks the top-level `services:` block and the
    first indent level beneath it (service names), pulling each service's
    `image:` value. Services built from a local `build:` have image=None.
    Anchors, `x-` extension fields, and multi-doc files degrade gracefully.
    """
    out = []
    in_services = False
    svc_indent = None
    cur = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        stripped = raw.strip()
        if indent == 0:
            in_services = stripped.split("#")[0].strip() == "services:"
            svc_indent, cur = None, None
            continue
        if not in_services:
            continue
        if svc_indent is None:
            svc_indent = indent
        if indent == svc_indent and stripped.endswith(":"):
            name = stripped[:-1].strip().strip("'\"")
            if name.startswith(("x-", "<<", "&")):
                cur = None
                continue
            cur = {"name": name, "image": None, "depends_on": False, "volumes": False}
            out.append(cur)
        elif cur is not None and indent > svc_indent:
            m = re.match(r"image:\s*(.+)", stripped)
            if m:
                cur["image"] = m.group(1).split("#")[0].strip().strip("'\"")
            elif re.match(r"depends_on:", stripped):
                cur["depends_on"] = True
            elif re.match(r"volumes:", stripped):
                cur["volumes"] = True

    result = []
    for s in out:
        img = s["image"]
        base, tag = None, None
        if img:
            name_part, sep, rest = img.partition(":")
            base = name_part
            # a ':' that belongs to a registry port (host:port/img) is not a tag
            tag = rest if (sep and "/" not in rest) else None
        blob = f"{s['name']} {base or ''}".lower()
        kind = None
        for k in ("postgres", "postgis", "redis", "valkey", "mysql", "mariadb",
                  "mongo", "clickhouse", "kafka", "redpanda", "zookeeper",
                  "elasticsearch", "opensearch", "rabbitmq", "minio", "memcached",
                  "cassandra", "temporal", "nats"):
            if k in blob:
                kind = k
                break
        # A datastore that mounts config volumes or depends on other services
        # usually can't be reproduced by a bare catalog package — flag it so the
        # skill prefers docker-compose over a Flox [services.*] block even when
        # the package exists in the catalog.
        config_coupled = bool(s["volumes"] or s["depends_on"])
        result.append({"name": s["name"], "image": img, "image_base": base,
                       "tag": tag, "kind": kind, "depends_on": s["depends_on"],
                       "volumes": s["volumes"], "config_coupled": config_coupled})
    return result


def main(argv):
    target = argv[1] if len(argv) > 1 else "."
    if not Path(target).is_dir():
        print(json.dumps({"error": f"not a directory: {target}"}))
        return 2
    print(json.dumps(scan(target), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
