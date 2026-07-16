#!/usr/bin/env python3
"""Unit tests for the floxify manifest verifier
(flox-plugin/skills/floxify/scripts/verify.py).

detect.py grounds the INPUT; verify.py grounds the OUTPUT. Every rule here
gets two tests: one proving it FIRES on a manifest with the real defect, one
proving it does NOT fire on a known-good manifest — "a wrong invariant is
worse than no invariant" (evals/floxify/README.md policy). Catalog checks
are mocked (`_run_show_command`) so the whole suite runs with no network.

Runnable two ways:
    python3 test_verify.py            # standalone, prints PASS/FAIL
    pytest test_verify.py             # each test_* function is a pytest case
"""
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
VERIFY = HERE.parent.parent / "flox-plugin" / "skills" / "floxify" / "scripts" / "verify.py"


def _load_verify():
    spec = importlib.util.spec_from_file_location("verify", VERIFY)
    mod = importlib.util.module_from_spec(spec)
    # Register under sys.modules *before* exec so `unittest.mock.patch`
    # (which resolves string targets via importlib.import_module) finds
    # this in-memory module instead of failing to locate a "verify" package
    # on sys.path — verify.py lives outside this directory, unlike the
    # sibling-module imports test_detect_usage_eval.py relies on.
    sys.modules["verify"] = mod
    spec.loader.exec_module(mod)
    return mod


verify_mod = _load_verify()
verify = verify_mod.verify


def _violations(detect, manifest_text, **kw):
    kw.setdefault("check_catalog_live", False)
    return verify(detect, manifest_text, **kw)["violations"]


def _rules(violations):
    return {v["rule"] for v in violations}


def _hard(violations):
    return [v for v in violations if v["severity"] == "hard"]


# ---------------------------------------------------------------------------
# AI-449 worked example: a Python+Node repo with an undeclared postgres dep.
# BAD -> exactly the three violations the ticket specifies; GOOD -> zero.
# ---------------------------------------------------------------------------

AI449_DETECT = {
    "ecosystems": ["python", "node"],
    "runtimes": [
        {"language": "python", "version": ">=3.12", "source": "pyproject.toml (requires-python)"},
        {"language": "node", "version": "20", "source": ".nvmrc"},
    ],
    "services": [],
    "service_clients": [
        {"package": "pg", "search_terms": ["postgresql"], "source": "package.json"},
    ],
    "native_hints": [],
}

AI449_BAD_MANIFEST = '''
schema-version = "1.13.0"

[install]
python3.pkg-path = "python312"
nodejs.pkg-path = "nodejs_20"

[vars]
DATABASE_URL = "postgres://postgres:postgres@127.0.0.1:5432/app_dev"
UV_PROJECT_ENVIRONMENT = "$FLOX_ENV_CACHE/venv"

[hook]
on-activate = """
  uv sync
  npm install
"""
'''

AI449_GOOD_MANIFEST = '''
schema-version = "1.13.0"

[install]
python3.pkg-path = "python312"
nodejs.pkg-path = "nodejs_20"
postgresql.pkg-path = "postgresql"

[vars]
DATABASE_URL = "postgres://postgres:postgres@127.0.0.1:5432/app_dev"

[hook]
on-activate = """
  export PGDATA="$FLOX_ENV_CACHE/postgres"
  if [ ! -d "$PGDATA" ]; then
    initdb -D "$PGDATA" --auth=trust --encoding=UTF8
  fi
  uv sync
  npm install
"""

[services.postgres]
command = "postgres -D \\"$FLOX_ENV_CACHE/postgres\\" -p 5432 -k /tmp"
'''


class TestAI449WorkedExample(unittest.TestCase):
    def test_bad_manifest_has_exactly_three_violations(self):
        v = _violations(AI449_DETECT, AI449_BAD_MANIFEST)
        hard = _hard(v)
        self.assertEqual(len(hard), 3, hard)
        messages = {h["message"] for h in hard}
        self.assertIn(
            "client 'pg' (package.json) implies postgres, but no "
            "[services.*] serves it",
            messages,
        )
        self.assertTrue(
            any(
                m.startswith("[vars] DATABASE_URL=") and "advertises postgres"
                in m and "no [services.postgres] serves it" in m
                for m in messages
            ),
            messages,
        )
        self.assertIn(
            "[vars] UV_PROJECT_ENVIRONMENT contains '$FLOX_ENV_CACHE/venv' "
            "— [vars] are literal; move to [hook]",
            messages,
        )

    def test_good_manifest_has_zero_violations(self):
        v = _violations(AI449_DETECT, AI449_GOOD_MANIFEST)
        self.assertEqual(v, [])


# ---------------------------------------------------------------------------
# invariant 1 — every detected runtime is installed (AI-453)
# ---------------------------------------------------------------------------

class TestRuntimesInstalled(unittest.TestCase):
    def test_fires_when_a_detected_runtime_is_missing(self):
        detect = {"runtimes": [
            {"language": "python", "version": "3.13", "source": "pyproject.toml"},
        ]}
        manifest = '''
[install]
nodejs.pkg-path = "nodejs_24"
'''
        v = _violations(detect, manifest)
        self.assertEqual(_rules(v), {"runtime-not-installed"})
        self.assertIn("python", v[0]["message"])

    def test_does_not_fire_when_runtime_is_installed(self):
        detect = {"runtimes": [
            {"language": "python", "version": "3.13", "source": "pyproject.toml"},
        ]}
        manifest = '''
[install]
python3.pkg-path = "python313"
'''
        self.assertEqual(_violations(detect, manifest), [])

    def test_versioned_and_bare_pkg_paths_both_satisfy(self):
        detect = {"runtimes": [{"language": "go", "version": "1.21", "source": "go.mod"}]}
        for pkg_path in ("go", "go_1_21"):
            manifest = f'[install]\ngo.pkg-path = "{pkg_path}"\n'
            self.assertEqual(_violations(detect, manifest), [], pkg_path)


# ---------------------------------------------------------------------------
# invariant 2 — leaf-datastore client gets a [services.*] entry
# ---------------------------------------------------------------------------

class TestLeafDatastoreServices(unittest.TestCase):
    def test_fires_when_pg_client_has_no_service(self):
        detect = {"service_clients": [
            {"package": "pg", "search_terms": ["postgresql"], "source": "package.json"},
        ]}
        v = _violations(detect, "[install]\n")
        self.assertEqual(_rules(v), {"leaf-datastore-not-served"})

    def test_does_not_fire_when_service_wired(self):
        detect = {"service_clients": [
            {"package": "pg", "search_terms": ["postgresql"], "source": "package.json"},
        ]}
        manifest = '''
[install]
postgresql.pkg-path = "postgresql"

[services.postgres]
command = "postgres"
'''
        self.assertEqual(_violations(detect, manifest), [])

    def test_does_not_fire_when_docker_compose_already_manages_it(self):
        detect = {
            "service_clients": [
                {"package": "psycopg2", "search_terms": ["postgresql"], "source": "requirements.txt"},
            ],
            "services": [{"name": "db", "kind": "postgres", "config_coupled": True}],
        }
        self.assertEqual(_violations(detect, "[install]\n"), [])

    def test_non_leaf_client_terms_are_ignored(self):
        # cryptography -> pkg-config/openssl: not a leaf datastore, no service expected.
        detect = {"service_clients": [
            {"package": "cryptography", "search_terms": ["pkg-config", "openssl"],
             "source": "requirements.txt"},
        ]}
        self.assertEqual(_violations(detect, "[install]\n"), [])


# ---------------------------------------------------------------------------
# invariant 3 — [vars] endpoint implies a service
# ---------------------------------------------------------------------------

class TestVarsEndpoints(unittest.TestCase):
    def test_fires_on_unserved_connection_string(self):
        manifest = '[vars]\nDATABASE_URL = "postgres://u:p@127.0.0.1:5432/app"\n'
        v = _violations({}, manifest)
        self.assertEqual(_rules(v), {"vars-endpoint-not-served"})

    def test_does_not_fire_when_service_serves_it(self):
        manifest = '''
[vars]
DATABASE_URL = "postgres://u:p@127.0.0.1:5432/app"

[services.postgres]
command = "postgres"
'''
        self.assertEqual(_violations({}, manifest), [])

    def test_realistic_lemmy_vars_do_not_false_positive(self):
        # Real golden shape: connection string + PGHOST/PGPORT alongside a
        # matching [services.postgres] block.
        manifest = '''
[vars]
LEMMY_DATABASE_URL = "postgres://lemmy:password@localhost:5432/lemmy"
PGHOST = "/tmp/lemmy-postgres"
PGPORT = "5432"

[services.postgres]
command = "postgres -k /tmp/lemmy-postgres"
'''
        self.assertEqual(_violations({}, manifest), [])

    def test_non_connection_string_vars_are_ignored(self):
        manifest = '[vars]\nRAILS_ENV = "development"\n'
        self.assertEqual(_violations({}, manifest), [])


# ---------------------------------------------------------------------------
# invariant 4 — [vars] are literal, never `$`-expanded
# ---------------------------------------------------------------------------

class TestVarsLiteral(unittest.TestCase):
    def test_fires_on_dollar_in_vars(self):
        manifest = '[vars]\nUV_PROJECT_ENVIRONMENT = "$FLOX_ENV_CACHE/venv"\n'
        v = _violations({}, manifest)
        self.assertEqual(_rules(v), {"vars-not-literal"})

    def test_does_not_fire_on_plain_literal_vars(self):
        manifest = '[vars]\nPGDATABASE = "myapp_dev"\nPGPORT = "5432"\n'
        self.assertEqual(_violations({}, manifest), [])


# ---------------------------------------------------------------------------
# invariant 5 — hook must not mutate the tracked git tree (AI-450)
# ---------------------------------------------------------------------------

class TestHookNoMutation(unittest.TestCase):
    def test_fires_on_git_submodule_update(self):
        manifest = '''
[hook]
on-activate = """
  git submodule update --init --recursive
"""
'''
        v = _violations({}, manifest)
        self.assertEqual(_rules(v), {"hook-mutates-tree"})

    def test_fires_on_git_checkout(self):
        manifest = '[hook]\non-activate = "git checkout main"\n'
        v = _violations({}, manifest)
        self.assertEqual(_rules(v), {"hook-mutates-tree"})

    def test_does_not_fire_on_realistic_rust_hook(self):
        # Real golden shape (lemmy): env exports + echoes, no git mutation.
        manifest = '''
[hook]
on-activate = """
  export CARGO_HOME="$FLOX_ENV_CACHE/cargo"
  export CARGO_TARGET_DIR="$FLOX_ENV_CACHE/target"
  export PATH="$CARGO_HOME/bin:$PATH"
  echo "lemmy env ready."
"""
'''
        self.assertEqual(_violations({}, manifest), [])

    def test_does_not_fire_when_no_hook_present(self):
        self.assertEqual(_violations({}, "[install]\n"), [])


# ---------------------------------------------------------------------------
# invariant 6 — catalog resolution (mocked flox show, no network)
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


POSTGRESQL_SHOW = """postgresql - Powerful, open source object-relational database system
Catalog: nixpkgs
Latest:  postgresql@18.4
License: PostgreSQL
Outputs: dev, doc, jit, lib, man*, out*, pltcl, plperl, plpython3 (* installed by default)
Systems: aarch64-darwin, x86_64-linux, aarch64-linux

Other versions:
    postgresql@18.4  (aarch64-darwin, aarch64-linux, x86_64-linux only)
    postgresql@17.10
    postgresql@16.5
"""

NODEJS_24_SHOW = """nodejs_24 - Event-driven I/O framework for the V8 JavaScript engine
Catalog: nixpkgs
Latest:  nodejs_24@24.18.0
License: MIT
Outputs: out* (* installed by default)
Systems: aarch64-darwin, x86_64-linux, aarch64-linux

Other versions:
    nodejs_24@24.18.0 (aarch64-darwin, aarch64-linux, x86_64-linux only)
    nodejs_24@24.13.0 (aarch64-darwin, aarch64-linux, x86_64-linux only)
    nodejs_24@24.2.0  (x86_64-linux only)
"""


PYTHON313_SHOW = """python313 - High-level dynamically-typed programming language
Catalog: nixpkgs
Latest:  python313@python3-3.13.14
License: Python-2.0
Outputs: out* (* installed by default)
Systems: aarch64-darwin, x86_64-linux, aarch64-linux

Other versions:
    python313@python3-3.13.14   (aarch64-darwin, aarch64-linux, x86_64-linux only)
    python313@python3-3.13.13
"""


def _mock_show(pkg_path, flox_bin, timeout):
    if pkg_path == "postgresql":
        return _FakeProc(stdout=POSTGRESQL_SHOW)
    if pkg_path == "nodejs_24":
        return _FakeProc(stdout=NODEJS_24_SHOW)
    if pkg_path == "python313":
        return _FakeProc(stdout=PYTHON313_SHOW)
    return _FakeProc(returncode=1, stderr=f"✘ ERROR: no packages matched this pkg-path: '{pkg_path}'")


class TestCatalog(unittest.TestCase):
    def setUp(self):
        verify_mod._SHOW_CACHE.clear()

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch("verify._run_show_command", side_effect=_mock_show)
    def test_unresolved_pkg_path_fires(self, mock_run, mock_which):
        manifest = '[install]\nghost.pkg-path = "nonexistent-pkg-zzz"\n'
        v = verify({}, manifest, check_catalog_live=True)["violations"]
        self.assertEqual(_rules(v), {"catalog-unresolved"})

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch("verify._run_show_command", side_effect=_mock_show)
    def test_missing_version_fires(self, mock_run, mock_which):
        manifest = '[install]\npg.pkg-path = "postgresql"\npg.version = "99.99"\n'
        v = verify({}, manifest, check_catalog_live=True)["violations"]
        self.assertEqual(_rules(v), {"catalog-version-missing"})

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch("verify._run_show_command", side_effect=_mock_show)
    def test_systems_mismatch_fires_for_mastodon_shape(self, mock_run, mock_which):
        # Real AI-455 shape: nodejs_24@24.18.0 has no x86_64-darwin build,
        # but options.systems declares it.
        manifest = '''
[install]
nodejs.pkg-path = "nodejs_24"
nodejs.version = "24.18.0"

[options]
systems = ["x86_64-linux", "aarch64-linux", "x86_64-darwin", "aarch64-darwin"]
'''
        v = verify({}, manifest, check_catalog_live=True)["violations"]
        self.assertEqual(_rules(v), {"catalog-systems-mismatch"})
        self.assertIn("x86_64-darwin", v[0]["message"])

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch("verify._run_show_command", side_effect=_mock_show)
    def test_resolved_pkg_and_version_within_declared_systems_is_clean(
        self, mock_run, mock_which,
    ):
        manifest = '''
[install]
postgresql.pkg-path = "postgresql"
postgresql.version = "17.10"

[options]
systems = ["x86_64-linux", "aarch64-linux", "x86_64-darwin", "aarch64-darwin"]
'''
        v = verify({}, manifest, check_catalog_live=True)["violations"]
        self.assertEqual(v, [])

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch("verify._run_show_command", side_effect=_mock_show)
    def test_unpinned_version_checks_against_latest(self, mock_run, mock_which):
        # nodejs_24 with no .version pinned -> latest (24.18.0), which is
        # missing x86_64-darwin; default systems (no [options]) = all four.
        manifest = '[install]\nnodejs.pkg-path = "nodejs_24"\n'
        v = verify({}, manifest, check_catalog_live=True)["violations"]
        self.assertEqual(_rules(v), {"catalog-systems-mismatch"})

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch("verify._run_show_command", side_effect=_mock_show)
    def test_per_package_systems_override_default(self, mock_run, mock_which):
        # A package explicitly scoped to Linux-only never trips the darwin gap.
        manifest = '''
[install]
nodejs.pkg-path = "nodejs_24"
nodejs.version = "24.18.0"
nodejs.systems = ["x86_64-linux", "aarch64-linux"]
'''
        v = verify({}, manifest, check_catalog_live=True)["violations"]
        self.assertEqual(v, [])

    @patch("shutil.which", return_value=None)
    def test_skips_cleanly_when_flox_unavailable(self, mock_which):
        manifest = '[install]\nghost.pkg-path = "nonexistent-pkg-zzz"\n'
        result = verify({}, manifest, check_catalog_live=True)
        self.assertEqual(result["violations"], [])
        self.assertFalse(result["catalog_checked"])

    def test_no_catalog_flag_skips_without_touching_flox(self):
        manifest = '[install]\nghost.pkg-path = "nonexistent-pkg-zzz"\n'
        result = verify({}, manifest, check_catalog_live=False)
        self.assertEqual(result["violations"], [])
        self.assertFalse(result["catalog_checked"])

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch("verify._run_show_command", side_effect=_mock_show)
    def test_partial_version_matches_as_prefix_wildcard(self, mock_run, mock_which):
        # Confirmed against live `flox edit`: "17" for postgresql resolves
        # to the latest 17.x (17.10 here), not a literal "17" catalog entry.
        manifest = (
            '[install]\npg.pkg-path = "postgresql"\npg.version = "17"\n'
            'pg.systems = ["x86_64-linux"]\n'
        )
        v = verify({}, manifest, check_catalog_live=True)["violations"]
        self.assertEqual(v, [])

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch("verify._run_show_command", side_effect=_mock_show)
    def test_prefixed_catalog_scheme_requires_the_full_string(self, mock_run, mock_which):
        # Real posthog golden defect, confirmed against live `flox edit`:
        # python313's catalog version is "python3-3.13.13" — pinning the
        # bare "3.13.13" does NOT resolve.
        manifest = '[install]\npy.pkg-path = "python313"\npy.version = "3.13.13"\n'
        v = verify({}, manifest, check_catalog_live=True)["violations"]
        self.assertEqual(_rules(v), {"catalog-version-missing"})

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch("verify._run_show_command", side_effect=_mock_show)
    def test_range_versions_are_not_exact_matched(self, mock_run, mock_which):
        # "^16" is a legitimate semver range, not an exact pin — must not
        # false-fire catalog-version-missing (it will never equal a literal
        # key in the versions map). Scoped to systems the resolved latest
        # actually supports, isolating this from the separate
        # catalog-systems-mismatch check exercised elsewhere.
        manifest = (
            '[install]\npg.pkg-path = "postgresql"\npg.version = "^16"\n'
            'pg.systems = ["x86_64-linux"]\n'
        )
        v = verify({}, manifest, check_catalog_live=True)["violations"]
        self.assertEqual(_rules(v), set())


# ---------------------------------------------------------------------------
# heuristic — native build input with no `outputs` (ADVISORY, never hard)
# ---------------------------------------------------------------------------

NATIVE_HINT_DETECT = {
    "native_hints": [
        {"trigger": "Aptfile libvips", "search_terms": ["vips"], "source": "Aptfile"},
    ],
}


class TestOutputsHeuristic(unittest.TestCase):
    def test_fires_advisory_for_native_hint_without_outputs(self):
        manifest = '[install]\nvips.pkg-path = "vips"\n'
        v = _violations(NATIVE_HINT_DETECT, manifest)
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0]["rule"], "outputs-heuristic")
        self.assertEqual(v[0]["severity"], "advisory")

    def test_never_contributes_to_hard_violations(self):
        manifest = '[install]\nvips.pkg-path = "vips"\n'
        self.assertEqual(_hard(_violations(NATIVE_HINT_DETECT, manifest)), [])

    def test_does_not_fire_when_outputs_declared(self):
        manifest = '[install]\nvips.pkg-path = "vips"\nvips.outputs = ["out", "dev"]\n'
        self.assertEqual(_violations(NATIVE_HINT_DETECT, manifest), [])

    def test_does_not_fire_without_a_native_hint(self):
        # No native_hints in detect (e.g. golden-lint has no cloned repo to
        # scan) -> nothing to cross-check, heuristic stays silent rather
        # than guessing from the pkg-path name alone.
        manifest = '[install]\nvips.pkg-path = "vips"\n'
        self.assertEqual(_violations({}, manifest), [])

    def test_does_not_fire_for_plain_service_install_like_postgresql(self):
        # postgresql installed purely as a [services.*] server (no native
        # C-extension linking against it) must not be flagged.
        manifest = '''
[install]
postgresql.pkg-path = "postgresql"

[services.postgres]
command = "postgres"
'''
        self.assertEqual(_violations({}, manifest), [])


# ---------------------------------------------------------------------------
# invalid manifest handling + output framing
# ---------------------------------------------------------------------------

class TestInvalidManifest(unittest.TestCase):
    def test_invalid_toml_reports_single_violation(self):
        result = verify({}, "this is not [ valid toml", check_catalog_live=False)
        self.assertEqual(len(result["violations"]), 1)
        self.assertEqual(result["violations"][0]["rule"], "invalid-toml")


class TestOutputFraming(unittest.TestCase):
    def test_disclaimer_says_consistent_not_correct(self):
        self.assertIn("consistent", verify_mod.DISCLAIMER.lower())
        self.assertIn("not", verify_mod.DISCLAIMER.lower())


if __name__ == "__main__":
    unittest.main()
