#!/usr/bin/env python3
"""Unit tests for the floxify repo analyzer (flox-plugin/skills/floxify/scripts/detect.py).

The analyzer is what grounds the skill's Phase 1: every fact it emits is read
from a real file, so these tests pin its extraction against the synthetic
fixtures in evals/floxify/fixtures/ (the same repos the outcome eval uses).

Runnable two ways:
    python3 test_detect.py            # standalone, prints PASS/FAIL, exits non-zero on failure
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
