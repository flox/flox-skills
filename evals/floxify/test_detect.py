#!/usr/bin/env python3
"""Unit tests for the floxify repo analyzer (flox-plugin/skills/floxify/scripts/detect.py).

The analyzer is what grounds the skill's Phase 1: every fact it emits is read
from a real file, so these tests pin its extraction against the synthetic
fixtures in evals/floxify/fixtures/ (the same repos the outcome eval uses).

Runnable two ways:
    flox activate -- python3 test_detect.py            # standalone, prints PASS/FAIL, exits non-zero on failure
    pytest test_detect.py             # each test_* function is a pytest case

Pure stdlib — no pytest required.
"""
import importlib.util
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
DETECT = HERE.parent.parent / "flox-plugin" / "skills" / "floxify" / "scripts" / "detect.py"


def _load_detect():
    spec = importlib.util.spec_from_file_location("detect", DETECT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


detect = _load_detect()


def _scan(fixture):
    return detect.scan(FIXTURES / fixture)


def _runtimes(result, language):
    return [r for r in result["runtimes"] if r["language"] == language]


def _has_source(result, key, substr):
    return any(substr in item["source"] for item in result[key])


def _client_terms(result):
    terms = set()
    for c in result["service_clients"]:
        terms.update(c["search_terms"])
    return terms


# --------------------------------------------------------------------------
# node-20: .nvmrc pins 20, package.json engines.node >=20; no services
# --------------------------------------------------------------------------

def test_node20_pins_node_from_nvmrc():
    r = _scan("node-20")
    nv = [x for x in _runtimes(r, "node") if x["source"] == ".nvmrc"]
    assert nv and nv[0]["version"] == "20", r["runtimes"]


def test_node20_reads_engines_node():
    r = _scan("node-20")
    assert _has_source(r, "runtimes", "engines.node")


def test_node20_ecosystem_is_node_only():
    r = _scan("node-20")
    assert r["ecosystems"] == ["node"], r["ecosystems"]


def test_node20_no_service_clients():
    r = _scan("node-20")
    assert r["service_clients"] == [], r["service_clients"]


# --------------------------------------------------------------------------
# node-postgres: pg dependency -> postgresql search hint
# --------------------------------------------------------------------------

def test_nodepostgres_detects_pg_client():
    r = _scan("node-postgres")
    pkgs = [c["package"] for c in r["service_clients"]]
    assert "pg" in pkgs, pkgs
    assert "postgresql" in _client_terms(r)


def test_nodepostgres_node_runtime():
    r = _scan("node-postgres")
    assert _runtimes(r, "node"), r["runtimes"]


# --------------------------------------------------------------------------
# go-mod: go 1.21
# --------------------------------------------------------------------------

def test_gomod_pins_go_version():
    r = _scan("go-mod")
    go = _runtimes(r, "go")
    assert go and go[0]["version"] == "1.21", r["runtimes"]
    assert r["ecosystems"] == ["go"], r["ecosystems"]


# --------------------------------------------------------------------------
# python-uv: requires-python >=3.12, uv package manager, uv.lock
# --------------------------------------------------------------------------

def test_pythonuv_requires_python():
    r = _scan("python-uv")
    py = _runtimes(r, "python")
    assert py and ">=3.12" in py[0]["version"], r["runtimes"]


def test_pythonuv_detects_uv_pm():
    r = _scan("python-uv")
    names = [p["name"] for p in r["package_managers"]]
    assert "uv" in names, r["package_managers"]
    assert "uv.lock" in r["lockfiles"], r["lockfiles"]


# --------------------------------------------------------------------------
# ruby: Gemfile ruby "3.3.0", lock RUBY VERSION + BUNDLED WITH 2.5.0
# --------------------------------------------------------------------------

def test_ruby_pins_from_gemfile_and_lock():
    r = _scan("ruby")
    versions = {x["source"]: x["version"] for x in _runtimes(r, "ruby")}
    assert versions.get("Gemfile") == "3.3.0", versions
    assert any("3.3.0" in v for k, v in versions.items() if "lock" in k.lower()), versions


def test_ruby_bundler_version_from_lock():
    r = _scan("ruby")
    bundler = [p for p in r["package_managers"] if p["name"] == "bundler"]
    assert bundler and bundler[0]["version"] == "2.5.0", r["package_managers"]


# --------------------------------------------------------------------------
# rust-cargo: Cargo.toml -> rust ecosystem
# --------------------------------------------------------------------------

def test_rust_detected_from_cargo():
    r = _scan("rust-cargo")
    assert _runtimes(r, "rust"), r["runtimes"]
    assert r["ecosystems"] == ["rust"], r["ecosystems"]


# --------------------------------------------------------------------------
# Cargo.lock: Rust service-client crates -> leaf-datastore search hints
# (AI-466 Hole 2 — Cargo.lock was recorded as "present" (LOCKFILES) but its
# package names were never parsed for clients, so the leaf-datastore
# invariant was inert on every Rust repo: lemmy's pq-sys — pulled in
# transitively by diesel's "postgres" feature — never registered as a
# postgres client, even though the exact same signal from a Python
# requirements.txt or a Ruby Gemfile.lock already did.)
# --------------------------------------------------------------------------

def test_cargo_lock_detects_postgres_client_pq_sys():
    lock = '''
[[package]]
name = "pq-sys"
version = "0.7.5"
source = "registry+https://github.com/rust-lang/crates.io-index"

[[package]]
name = "diesel"
version = "2.3.7"
source = "registry+https://github.com/rust-lang/crates.io-index"
'''
    r = _scan_tmp({"Cargo.toml": '[package]\nname = "x"\n', "Cargo.lock": lock})
    pkgs = [c["package"] for c in r["service_clients"]]
    assert "pq-sys" in pkgs, pkgs
    assert "postgresql" in _client_terms(r)


def test_cargo_lock_detects_tokio_postgres_and_sqlx_postgres():
    lock = '''
[[package]]
name = "tokio-postgres"
version = "0.7.12"

[[package]]
name = "sqlx-postgres"
version = "0.8.2"
'''
    r = _scan_tmp({"Cargo.toml": '[package]\nname = "x"\n', "Cargo.lock": lock})
    pkgs = {c["package"] for c in r["service_clients"]}
    assert {"tokio-postgres", "sqlx-postgres"} <= pkgs, pkgs
    assert "postgresql" in _client_terms(r)


def test_cargo_lock_detects_redis_clients():
    lock = '''
[[package]]
name = "redis"
version = "0.27.0"

[[package]]
name = "fred"
version = "9.2.1"
'''
    r = _scan_tmp({"Cargo.toml": '[package]\nname = "x"\n', "Cargo.lock": lock})
    terms = _client_terms(r)
    assert "redis" in terms, terms


def test_cargo_lock_detects_mysql_clients():
    lock = '''
[[package]]
name = "mysql_async"
version = "0.34.2"

[[package]]
name = "sqlx-mysql"
version = "0.8.2"
'''
    r = _scan_tmp({"Cargo.toml": '[package]\nname = "x"\n', "Cargo.lock": lock})
    terms = _client_terms(r)
    assert "mariadb" in terms, terms


def test_cargo_lock_source_is_attributed():
    lock = '[[package]]\nname = "pq-sys"\nversion = "0.7.5"\n'
    r = _scan_tmp({"Cargo.toml": '[package]\nname = "x"\n', "Cargo.lock": lock})
    sources = {c["source"] for c in r["service_clients"]}
    assert "Cargo.lock" in sources, sources


def test_cargo_lock_unrelated_crates_do_not_false_positive():
    # serde/tokio are ubiquitous and imply nothing about a datastore --
    # a wrong invariant here is worse than none.
    lock = '''
[[package]]
name = "serde"
version = "1.0.210"

[[package]]
name = "tokio"
version = "1.40.0"
'''
    r = _scan_tmp({"Cargo.toml": '[package]\nname = "x"\n', "Cargo.lock": lock})
    assert r["service_clients"] == [], r["service_clients"]


def test_cargo_lock_missing_is_not_an_error():
    r = _scan_tmp({"Cargo.toml": '[package]\nname = "x"\n'})
    assert r["service_clients"] == [], r["service_clients"]


def test_cargo_lock_clients_are_scope_runtime():
    # No sections to distinguish in a lockfile -- always "runtime";
    # verify.py's corroboration requirement (AI-466/AI-467) is the tool
    # for this source, not provenance.
    lock = '[[package]]\nname = "pq-sys"\nversion = "0.7.5"\n'
    r = _scan_tmp({"Cargo.toml": '[package]\nname = "x"\n', "Cargo.lock": lock})
    scopes = {c["scope"] for c in r["service_clients"]}
    assert scopes == {"runtime"}, r["service_clients"]


# --------------------------------------------------------------------------
# AI-467: per-client provenance (runtime vs dev/test/optional) -- the
# leaf-datastore invariant used to pool every dependency section together
# (npm devDependencies, Gemfile `group :test`, Python optional-deps) as if
# they were all equally trustworthy evidence of a runtime need. Reproduced
# live against PostHog @ 55525a19f353's pyproject.toml before any code
# changed: pymysql/pymongo sit in the MAIN [project.dependencies] list
# (not optional-dependencies/dependency-groups), so provenance alone does
# NOT explain that false positive -- confirming the fix has to be
# verify.py's corroboration rule, not just this detect.py change. This
# provenance tracking is still real and valuable on its own: it powers
# corroboration's "dev-scoped evidence never counts" half.
# --------------------------------------------------------------------------

def test_npm_dev_dependencies_are_scope_dev():
    pkg = '{"dependencies": {"pg": "^8"}, "devDependencies": {"pg-native": "^3"}}'
    r = _scan_tmp({"package.json": pkg})
    by_pkg = {c["package"]: c["scope"] for c in r["service_clients"]}
    assert by_pkg == {"pg": "runtime", "pg-native": "dev"}, r["service_clients"]


def test_gemfile_gems_inside_test_group_are_scope_dev():
    text = (
        "gem 'pg'\n"
        "group :test do\n"
        "  gem 'mysql2'\n"
        "end\n"
    )
    r = _scan_tmp({"Gemfile": text})
    by_pkg = {c["package"]: c["scope"] for c in r["service_clients"]}
    assert by_pkg == {"pg": "runtime", "mysql2": "dev"}, r["service_clients"]


def test_gemfile_gems_inside_production_named_group_stay_runtime():
    # An unrecognized/non-dev group name (":production") must not be
    # treated as dev -- erring toward not hiding a real dependency.
    text = "group :production do\n  gem 'pg'\nend\n"
    r = _scan_tmp({"Gemfile": text})
    by_pkg = {c["package"]: c["scope"] for c in r["service_clients"]}
    assert by_pkg == {"pg": "runtime"}, r["service_clients"]


def test_gemfile_multi_name_group_with_one_dev_name_is_scope_dev():
    # Every named group is dev/test -- gem is genuinely dev-only.
    text = "group :development, :test do\n  gem 'pg'\nend\n"
    r = _scan_tmp({"Gemfile": text})
    by_pkg = {c["package"]: c["scope"] for c in r["service_clients"]}
    assert by_pkg == {"pg": "dev"}, r["service_clients"]


def test_gemfile_mixed_group_with_a_production_name_stays_runtime():
    # AI-467 review I1: a gem declared under MULTIPLE group names belongs
    # to all of them simultaneously in Bundler -- `group :production,
    # :test do` genuinely installs the gem in production. Marking it dev
    # (an `any()`-over-names bug) would silently downgrade a corroborated
    # client from HARD to ADVISORY -- a gate-direction false negative.
    # This fixture distinguishes any() from all(): both prior tests use
    # all-dev group lists, which pass under either implementation.
    text = "group :production, :test do\n  gem 'pg'\nend\n"
    r = _scan_tmp({"Gemfile": text})
    by_pkg = {c["package"]: c["scope"] for c in r["service_clients"]}
    assert by_pkg == {"pg": "runtime"}, r["service_clients"]


def test_pyproject_optional_dependencies_are_scope_dev():
    text = '''
[project]
name = "x"
dependencies = ["psycopg2"]

[project.optional-dependencies]
mysql = ["pymysql"]
'''
    r = _scan_tmp({"pyproject.toml": text})
    by_pkg = {c["package"]: c["scope"] for c in r["service_clients"]}
    assert by_pkg == {"psycopg2": "runtime", "pymysql": "dev"}, r["service_clients"]


def test_pyproject_dependency_groups_pep735_are_scope_dev():
    # PostHog's own shape: PEP 735 [dependency-groups], NOT
    # [project.optional-dependencies].
    text = '''
[project]
name = "x"
dependencies = ["psycopg2"]

[dependency-groups]
dev = ["pymysql"]
'''
    r = _scan_tmp({"pyproject.toml": text})
    by_pkg = {c["package"]: c["scope"] for c in r["service_clients"]}
    assert by_pkg == {"psycopg2": "runtime", "pymysql": "dev"}, r["service_clients"]


def test_pyproject_poetry_group_dependencies_are_scope_dev():
    text = '''
[project]
name = "x"

[tool.poetry.dependencies]
python = "^3.12"
psycopg2 = "*"

[tool.poetry.group.dev.dependencies]
pymysql = "*"
'''
    r = _scan_tmp({"pyproject.toml": text})
    by_pkg = {c["package"]: c["scope"] for c in r["service_clients"]}
    assert by_pkg == {"psycopg2": "runtime", "pymysql": "dev"}, r["service_clients"]


def test_posthog_pyproject_shape_pymysql_pymongo_are_scope_runtime():
    """The exact reproduction: pymysql/pymongo verified live to sit in
    PostHog's MAIN [project.dependencies] (pyproject.toml lines 113-114
    at SHA 55525a19f353), not [project.optional-dependencies] or
    [dependency-groups]. This asserts detect.py reports them as
    scope="runtime" -- correctly reflecting the file, and PROVING
    section-provenance alone cannot suppress the false positive. verify.py
    (AI-467's corroboration extension) is what closes the gap for this
    specific shape -- see test_verify.py's posthog reproduction.
    """
    text = '''
[project]
name = "posthog"
requires-python = "==3.13.13"
dependencies = [
    "psycopg2-binary==2.9.10",
    "pymssql==2.3.5",
    "pymysql==1.1.1",
    "pymongo==4.13.2",
    "redis==5.3.1",
]

[dependency-groups]
dev = [
    "types-pymysql==1.1.0.20240524",
]
'''
    r = _scan_tmp({"pyproject.toml": text})
    by_pkg = {c["package"]: c["scope"] for c in r["service_clients"]}
    assert by_pkg["pymysql"] == "runtime", by_pkg
    assert by_pkg["pymongo"] == "runtime", by_pkg


def test_requirements_dev_txt_is_scope_dev():
    r = _scan_tmp({"requirements-dev.txt": "pymysql==1.1.1\n"})
    scopes = {c["scope"] for c in r["service_clients"]}
    assert scopes == {"dev"}, r["service_clients"]


def test_requirements_txt_is_scope_runtime():
    r = _scan_tmp({"requirements.txt": "psycopg2==2.9.10\n"})
    scopes = {c["scope"] for c in r["service_clients"]}
    assert scopes == {"runtime"}, r["service_clients"]


def test_requirements_dev_sibling_filenames_are_scope_dev():
    # AI-467 review M2: requirements-dev.txt was the only dev-signaling
    # filename recognized; sibling pip-tools/pip conventions were unscanned
    # entirely (not just misclassified -- invisible to the analyzer).
    r = _scan_tmp({
        "dev-requirements.txt": "pymysql==1.1.1\n",
        "requirements-test.txt": "pymongo==4.9.0\n",
    })
    by_pkg = {c["package"]: c["scope"] for c in r["service_clients"]}
    assert by_pkg == {"pymysql": "dev", "pymongo": "dev"}, r["service_clients"]


# --------------------------------------------------------------------------
# invariant: the analyzer never asserts catalog names, only search hints
# --------------------------------------------------------------------------

def test_disclaimer_present_and_no_pkg_paths():
    r = _scan("python-uv")
    assert "search" in r["_meta"]["disclaimer"].lower()
    # service_clients / native_hints carry `search_terms`, never `pkg-path`
    for item in r["service_clients"] + r["native_hints"]:
        assert "search_terms" in item
        assert "pkg-path" not in item


def _scan_tmp(files):
    """Build a throwaway repo from {relpath: content} and scan it."""
    import shutil
    d = tempfile.mkdtemp(prefix="detect-test-")
    try:
        for rel, content in files.items():
            p = Path(d) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return detect.scan(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------
# deno: config file or a *-edge-runtime compose image (supabase-motivated)
# --------------------------------------------------------------------------

def test_deno_from_config():
    r = _scan_tmp({"deno.json": '{"tasks":{}}'})
    assert any(x["language"] == "deno" for x in r["runtimes"]), r["runtimes"]
    assert "deno" in r["ecosystems"]


def test_deno_from_edge_runtime_image():
    compose = "services:\n  functions:\n    image: supabase/edge-runtime:v1.67.0\n"
    r = _scan_tmp({"docker-compose.yml": compose})
    assert any(x["language"] == "deno" for x in r["runtimes"]), r["runtimes"]


# --------------------------------------------------------------------------
# Aptfile: native C-ext libs (mastodon-motivated)
# --------------------------------------------------------------------------

def test_aptfile_native_hints():
    r = _scan_tmp({"Aptfile": "libvips\nffmpeg\nlibpq-dev\n# a comment\n"})
    terms = set()
    for n in r["native_hints"]:
        terms.update(n["search_terms"])
    assert {"vips", "ffmpeg", "postgresql"} <= terms, r["native_hints"]


# --------------------------------------------------------------------------
# compose coupling: a config-mounting / depends_on datastore is flagged
# (posthog/sentry rule: catalog presence != wire it as a Flox service)
# --------------------------------------------------------------------------

def test_compose_config_coupled_flags():
    compose = (
        "services:\n"
        "  analytics:\n"
        "    image: clickhouse/clickhouse-server:24.3\n"
        "    depends_on: [kafka]\n"
        "    volumes:\n"
        "      - ./config.xml:/etc/clickhouse-server/config.xml\n"
        "  cache:\n"
        "    image: redis:7\n"
    )
    r = _scan_tmp({"docker-compose.yml": compose})
    byname = {s["name"]: s for s in r["services"]}
    assert byname["analytics"]["config_coupled"] is True, byname["analytics"]
    assert byname["analytics"]["kind"] == "clickhouse"
    assert byname["cache"]["config_coupled"] is False, byname["cache"]


# --------------------------------------------------------------------------
# AI-485: malformed-but-syntactically-valid JSON/TOML shapes. package.json
# and .mise.toml are agent-editable inputs -- both can carry the wrong
# VALUE TYPE for a field detect.py assumes is an object/table while still
# being syntactically valid JSON/TOML, which json.loads/tomllib parse
# without error. detect.py's own docstring promises it "never raises on a
# malformed input file"; these two fields were the gap in that promise.
# --------------------------------------------------------------------------

def test_package_json_non_dict_dependencies_does_not_raise():
    # F6: `"dependencies": [...]` instead of `{...}` -- valid JSON, wrong
    # shape. `(pj.get("dependencies") or {}).keys()` used to crash with
    # AttributeError the moment dependencies was a list.
    r = _scan_tmp({"package.json": '{"name": "app", "dependencies": ["react", "pg"]}'})
    assert r["service_clients"] == [], r["service_clients"]
    assert "package.json" in r["files_scanned"]


def test_package_json_non_dict_dev_dependencies_does_not_raise():
    r = _scan_tmp({
        "package.json": '{"name": "app", "devDependencies": ["jest"]}',
    })
    assert r["service_clients"] == [], r["service_clients"]


def test_package_json_non_object_root_records_a_note_not_a_crash():
    # The whole file can be valid JSON that isn't an object at all
    # (`[1, 2, 3]`) -- same fragility class, one level up.
    r = _scan_tmp({"package.json": "[1, 2, 3]"})
    assert r["service_clients"] == [], r["service_clients"]
    assert any("package.json" in n for n in r["notes"]), r["notes"]


def test_package_json_literal_null_records_a_note():
    # PR #66 review M2: `null` is valid JSON and parses to Python None,
    # the same value the parse-FAILURE branch leaves `pj` as -- without
    # a `parsed_ok` flag to tell the two apart, this degenerate case
    # silently recorded no note at all.
    r = _scan_tmp({"package.json": "null"})
    assert r["service_clients"] == [], r["service_clients"]
    assert any("package.json" in n for n in r["notes"]), r["notes"]


def test_mise_tools_non_dict_does_not_raise():
    # F7: `[tools]` declared as an array instead of a table -- valid TOML,
    # wrong shape. `tools.items()` used to crash with AttributeError.
    r = _scan_tmp({".mise.toml": 'tools = ["node", "python"]\n'})
    assert r["runtimes"] == [], r["runtimes"]
    assert any("tools" in n for n in r["notes"]), r["notes"]


def _all_tests():
    return [(n, f) for n, f in sorted(globals().items())
            if n.startswith("test_") and callable(f)]


if __name__ == "__main__":
    failed = 0
    for name, fn in _all_tests():
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(e).__name__}: {e}")
    total = len(_all_tests())
    print(f"\n{total - failed}/{total} passed")
    raise SystemExit(1 if failed else 0)
