#!/usr/bin/env python3
"""Unit tests for the floxify manifest verifier
(flox-plugin/skills/floxify/scripts/verify.py).

detect.py grounds the INPUT; verify.py grounds the OUTPUT. Every rule here
gets two tests: one proving it FIRES on a manifest with the real defect, one
proving it does NOT fire on a known-good manifest — "a wrong invariant is
worse than no invariant" (evals/floxify/README.md policy). Catalog checks
are mocked (`_run_show_command`) so the whole suite runs with no network.

Run from the suite root (`evals/floxify/`) — that is what puts
`_skill_module_loader` on `sys.path`. Running the file by path
(`python3 tests/test_verify.py`) fails with `ModuleNotFoundError` instead:
    python3 -m unittest tests.test_verify -v   # this module only
    python3 -m tests.test_verify               # same, via the __main__ block
    pytest tests/test_verify.py                # each test_* method is a pytest case
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _skill_module_loader import load_module

HERE = Path(__file__).resolve().parent
SUITE = HERE.parent          # evals/floxify
REPO_ROOT = SUITE.parent.parent
VERIFY = REPO_ROOT / "flox-plugin" / "skills" / "floxify" / "scripts" / "verify.py"
DETECT = REPO_ROOT / "flox-plugin" / "skills" / "floxify" / "scripts" / "detect.py"

# Unique sys.modules key so @patch("...") resolves THIS file's instance —
# test_real_world_golden_lint.py loads the same verify.py under its OWN unique key.
# Sharing a key (both used to register under the bare "verify") let
# whichever file's import ran second silently steal the other's @patch
# target when both run in one interpreter, as CI's free-tests step does.
# See _skill_module_loader.py and test_skill_module_loader.py.
_MODULE_KEY = "verify_under_test_verify"

verify_mod = load_module(VERIFY, sys_modules_key=_MODULE_KEY)
verify = verify_mod.verify
# No @patch target needed for detect.py here -- private instance, no key.
detect = load_module(DETECT)


def _violations(detect, manifest_text, **kw):
    kw.setdefault("check_catalog_live", False)
    return verify(detect, manifest_text, **kw)["violations"]


def _rules(violations):
    return {v["rule"] for v in violations}


def _hard(violations):
    return [v for v in violations if v["severity"] == "hard"]


# Minimal wired postgres service block, repeated across many fixture
# manifests below that only need "postgres is served" and don't care
# about the exact command.
POSTGRES_SERVICE = '[services.postgres]\ncommand = "postgres"\n'


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
# AI-466 forensic reproduction: the lemmy x5 re-run came back 3/5, and
# forensics traced both failures to false negatives in verify.py -- the
# model ran the gate honestly and it exited 0 on a defective manifest.
# This exercises the REAL detect.py against a lemmy-shaped fixture
# (fixtures/lemmy-shaped/: a compose file with a postgres service, and a
# Cargo.lock with pq-sys/diesel) rather than a hand-built detect dict, so
# it proves the fix at the same integration boundary the incident lived
# at, not just each invariant in isolation.
# ---------------------------------------------------------------------------

LEMMY_SHAPED_REP3_MANIFEST = '''
schema-version = "1.13.0"

[install]
cargo.pkg-path = "cargo"
rustc.pkg-path = "rustc"

[vars]
LEMMY_DATABASE_URL = "postgres://lemmy:password@localhost:5433/lemmy"

[hook]
on-activate = """
  export PGDATA="$FLOX_ENV_CACHE/postgres"
  initdb -D "$PGDATA" --auth=trust
"""
'''

LEMMY_SHAPED_GOOD_MANIFEST = '''
schema-version = "1.13.0"

[install]
cargo.pkg-path = "cargo"
rustc.pkg-path = "rustc"
postgresql.pkg-path = "postgresql_18"

[vars]
LEMMY_DATABASE_URL = "postgres://lemmy:password@localhost:5433/lemmy"

[hook]
on-activate = """
  export PGDATA="$FLOX_ENV_CACHE/postgres"
  initdb -D "$PGDATA" --auth=trust
"""

[services.postgres]
command = "postgres -D \\"$FLOX_ENV_CACHE/postgres\\" -p 5433"
'''


class TestAI466LemmyForensicReproduction(unittest.TestCase):
    def _detect(self):
        return detect.scan(str(SUITE / "fixtures" / "lemmy-shaped"))

    def test_rep3_shaped_manifest_fires_holes_1_and_2(self):
        # No [services.*] at all: HARD-fires both the pq-sys client
        # (Hole 2 -- Cargo.lock now parsed) and the [vars] postgres
        # endpoint (Hole 1 -- the repo's compose file no longer silences
        # this just because detect.py happened to find it).
        detected = self._detect()
        v = _hard(_violations(detected, LEMMY_SHAPED_REP3_MANIFEST))
        rules = _rules(v)
        self.assertEqual(rules, {"leaf-datastore-not-served", "vars-endpoint-not-served"})

    def test_good_manifest_with_wired_service_is_clean(self):
        detected = self._detect()
        v = _hard(_violations(detected, LEMMY_SHAPED_GOOD_MANIFEST))
        self.assertEqual(v, [])

    def test_good_manifest_plus_git_dash_c_hook_mutation_fires_hole_3_only(self):
        mutated = LEMMY_SHAPED_GOOD_MANIFEST.replace(
            'initdb -D "$PGDATA" --auth=trust',
            'initdb -D "$PGDATA" --auth=trust\n'
            '  git -C "$FLOX_ENV_PROJECT" submodule update --init',
        )
        detected = self._detect()
        v = _hard(_violations(detected, mutated))
        self.assertEqual(_rules(v), {"hook-mutates-tree"})

    def test_genuine_docker_compose_up_hook_satisfies_the_floor_without_a_service_block(self):
        manifest = '''
schema-version = "1.13.0"

[install]
cargo.pkg-path = "cargo"
rustc.pkg-path = "rustc"
docker-compose.pkg-path = "docker-compose"

[vars]
LEMMY_DATABASE_URL = "postgres://lemmy:password@localhost:5433/lemmy"

[hook]
on-activate = """
  docker-compose up -d
"""
'''
        detected = self._detect()
        v = _hard(_violations(detected, manifest))
        self.assertEqual(v, [])

    def test_compose_service_that_is_not_a_leaf_datastore_never_triggers_anything(self):
        # pictrs (image hosting) is in the fixture's compose file but is
        # not a leaf datastore client or a [vars] endpoint -- must never
        # be the reason anything fires.
        detected = self._detect()
        pictrs_only_clients = [
            c for c in detected["service_clients"] if c["package"] != "pq-sys"
        ]
        manifest_with_no_pictrs_signal = '[install]\ncargo.pkg-path = "cargo"\n'
        v = _violations({"service_clients": pictrs_only_clients}, manifest_with_no_pictrs_signal)
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

    # --- Important review finding: bare "python3" and "rustup" are real,
    # resolvable pkg-paths (confirmed live: `flox show python3` ->
    # python3@3.14.6, `flox show rustup` -> rustup@1.29.0) that the
    # original patterns rejected, false-firing on correct manifests.

    def test_bare_python3_satisfies_python_runtime(self):
        detect = {"runtimes": [{"language": "python", "version": "3.13", "source": "pyproject.toml"}]}
        manifest = '[install]\npython3.pkg-path = "python3"\n'
        self.assertEqual(_violations(detect, manifest), [])

    def test_bare_python_without_a_3_does_not_satisfy(self):
        # Confirmed live: bare "python" resolves to Python 2.7 -- must NOT
        # be accepted as satisfying a detected Python 3 runtime.
        detect = {"runtimes": [{"language": "python", "version": "3.13", "source": "pyproject.toml"}]}
        manifest = '[install]\npython.pkg-path = "python"\n'
        v = _violations(detect, manifest)
        self.assertEqual(_rules(v), {"runtime-not-installed"})

    def test_rustup_satisfies_rust_runtime(self):
        detect = {"runtimes": [{"language": "rust", "version": None, "source": "Cargo.toml"}]}
        manifest = '[install]\nrustup.pkg-path = "rustup"\n'
        self.assertEqual(_violations(detect, manifest), [])

    def test_runtime_pattern_coverage_matches_detects_tool_lang(self):
        """Every language detect.py's TOOL_LANG can emit is either checked
        here or has a documented reason it's deliberately excluded — a new
        TOOL_LANG entry must be triaged into one list or the other, not
        silently fall through check_runtimes_installed unnoticed (the
        posthog/AI-453 failure mode this whole invariant exists to catch).
        """
        detect_langs = set(detect.TOOL_LANG.values())
        covered = set(verify_mod.RUNTIME_PKG_PATTERNS)
        excluded = set(verify_mod.RUNTIME_PATTERNS_DELIBERATELY_EXCLUDED)
        missing = detect_langs - covered - excluded
        self.assertEqual(
            missing, set(),
            f"detect.py can emit these languages but verify.py neither "
            f"checks them nor documents why not: {missing}",
        )


# ---------------------------------------------------------------------------
# invariant 2 — leaf-datastore client gets a [services.*] entry
# ---------------------------------------------------------------------------

class TestLeafDatastoreServices(unittest.TestCase):
    def test_uncorroborated_client_downgrades_to_advisory(self):
        # AI-467 INTENTIONAL CHANGE from the original #42 behavior (this
        # test used to assert HARD here): a package.json `dependencies`
        # entry with NOTHING else in the manifest confirming a runtime
        # postgres need now downgrades to ADVISORY, same as every other
        # source after AI-467 generalized AI-466's Cargo.lock-specific
        # corroboration rule. Reproduced live against PostHog @
        # 55525a19f353: pymysql/pymongo sit in pyproject.toml's MAIN
        # [project.dependencies] (unambiguously "runtime" by section
        # placement) yet PostHog runs neither MariaDB nor MongoDB locally
        # -- section placement alone was never reliable proof of a live
        # local service need. See test_fires_hard_when_client_is_
        # corroborated below for the case this invariant still catches.
        detect = {"service_clients": [
            {"package": "pg", "search_terms": ["postgresql"], "source": "package.json",
             "scope": "runtime"},
        ]}
        v = _violations(detect, "[install]\n")
        self.assertEqual(_rules(v), {"leaf-datastore-not-served"})
        self.assertEqual(v[0]["severity"], "advisory")
        self.assertEqual(
            v[0]["message"],
            "client 'pg' (package.json) implies postgres, but no "
            "[services.*] serves it — no independent [vars] endpoint or "
            "compose service corroborates it, so a declared dependency "
            "alone isn't proof of a runtime need; confirm whether postgres "
            "is actually used",
        )

    def test_fires_hard_when_client_is_corroborated(self):
        # The realistic, incident-motivating shape (AI-449/lemmy): a
        # runtime-scoped client PLUS an independent [vars] endpoint of
        # the same kind. This is what the invariant still catches.
        detect = {"service_clients": [
            {"package": "pg", "search_terms": ["postgresql"], "source": "package.json",
             "scope": "runtime"},
        ]}
        manifest = '[vars]\nDATABASE_URL = "postgres://u:p@localhost:5432/app"\n'
        v = _hard(_violations(detect, manifest))
        rules = {x["rule"] for x in v}
        self.assertIn("leaf-datastore-not-served", rules)

    def test_dev_scoped_client_downgrades_to_advisory_even_when_corroborated(self):
        # scope="dev" is disqualifying on its own: a devDependencies-only
        # client is not evidence the DEV environment being set up needs a
        # live service, even if some OTHER (main-scoped) signal happens
        # to corroborate the same kind elsewhere in the manifest.
        detect = {"service_clients": [
            {"package": "pg-native", "search_terms": ["postgresql"], "source": "package.json",
             "scope": "dev"},
        ]}
        manifest = '[vars]\nDATABASE_URL = "postgres://u:p@localhost:5432/app"\n'
        v = _violations(detect, manifest)
        leaf = [x for x in v if x["rule"] == "leaf-datastore-not-served"]
        self.assertEqual(len(leaf), 1)
        self.assertEqual(leaf[0]["severity"], "advisory")
        self.assertEqual(
            leaf[0]["message"],
            "client 'pg-native' (package.json) implies postgres, but no "
            "[services.*] serves it — detected in a dev/test/optional-only "
            "dependency section, not proof of a runtime need; confirm "
            "whether postgres is actually used",
        )

    def test_missing_scope_key_defaults_to_runtime(self):
        # Backward compatibility: detect facts predating AI-467 (or a
        # hand-built dict in another test) carry no "scope" key at all --
        # must default to "runtime", not silently downgrade.
        detect = {"service_clients": [
            {"package": "pg", "search_terms": ["postgresql"], "source": "package.json"},
        ]}
        manifest = '[vars]\nDATABASE_URL = "postgres://u:p@localhost:5432/app"\n'
        v = _hard(_violations(detect, manifest))
        rules = {x["rule"] for x in v}
        self.assertIn("leaf-datastore-not-served", rules)

    # --- AI-467 forensic reproduction: PostHog @ 55525a19f353's
    # pyproject.toml declares pymysql/pymongo in the MAIN
    # [project.dependencies] list (verified live, see detect.py's
    # test_posthog_pyproject_shape_pymysql_pymongo_are_scope_runtime) --
    # section-provenance alone reports them as scope="runtime" correctly,
    # but PostHog runs neither MariaDB nor MongoDB locally. Without
    # corroboration, this used to HARD-fail the real_world posthog eval in ALL
    # FIVE reps, including against PostHog's own upstream manifest. ---

    def test_posthog_shape_pymysql_pymongo_produce_no_hard_violation(self):
        detect = {"service_clients": [
            {"package": "pymysql", "search_terms": ["mariadb"], "source": "pyproject.toml",
             "scope": "runtime"},
            {"package": "pymongo", "search_terms": ["mongodb-ce"], "source": "pyproject.toml",
             "scope": "runtime"},
        ]}
        # PostHog's own manifest: postgres + redis wired, no mariadb/mongodb
        # [vars] endpoint or compose service anywhere -- the real_world registry
        # expects neither service for posthog.
        manifest = f'''
[install]
postgresql.pkg-path = "postgresql_15"
redis.pkg-path = "redis"

{POSTGRES_SERVICE}
[services.redis]
command = "redis-server"
'''
        v = _hard(_violations(detect, manifest))
        rules = {x["rule"] for x in v}
        self.assertNotIn("leaf-datastore-not-served", rules)

    def test_does_not_fire_when_service_wired(self):
        detect = {"service_clients": [
            {"package": "pg", "search_terms": ["postgresql"], "source": "package.json"},
        ]}
        manifest = f'''
[install]
postgresql.pkg-path = "postgresql"

{POSTGRES_SERVICE}'''
        self.assertEqual(_violations(detect, manifest), [])

    def test_repo_side_compose_presence_alone_does_not_satisfy_the_floor(self):
        # AI-466 Hole 1: repo-side compose FILE presence (a detect.py fact)
        # is NOT the same as the MANIFEST serving the datastore. SKILL.md's
        # HARD FLOOR: "The repo already having a way to start it is NEVER
        # a reason to defer." A manifest with no [services.*] AND no hook
        # that actually invokes docker-compose must still fire, even
        # though detect.py found a compose file with a matching service.
        detect = {
            "service_clients": [
                {"package": "psycopg2", "search_terms": ["postgresql"], "source": "requirements.txt"},
            ],
            "services": [{"name": "db", "kind": "postgres", "config_coupled": True}],
        }
        v = _violations(detect, "[install]\n")
        self.assertEqual(_rules(v), {"leaf-datastore-not-served"})

    def test_manifest_hook_genuinely_running_docker_compose_up_satisfies_the_floor(self):
        # The fix's positive case: the manifest itself (not just the repo)
        # actually starts the compose service via `docker-compose up`,
        # with docker-compose installed.
        detect = {
            "service_clients": [
                {"package": "psycopg2", "search_terms": ["postgresql"], "source": "requirements.txt"},
            ],
            "services": [{"name": "db", "kind": "postgres", "config_coupled": True}],
        }
        manifest = '''
[install]
docker-compose.pkg-path = "docker-compose"

[hook]
on-activate = """
  docker-compose up -d
"""
'''
        self.assertEqual(_violations(detect, manifest), [])

    def test_manifest_hook_with_f_flag_satisfies_the_floor(self):
        # AI-476: `-f <file>` between `docker-compose` and `up` (the common
        # real-world form -- posthog's golden hook works around this exact
        # gap today by using COMPOSE_FILE instead) must not evade detection.
        detect = {
            "service_clients": [
                {"package": "psycopg2", "search_terms": ["postgresql"], "source": "requirements.txt"},
            ],
            "services": [{"name": "db", "kind": "postgres", "config_coupled": True}],
        }
        manifest = '''
[install]
docker-compose.pkg-path = "docker-compose"

[hook]
on-activate = """
  docker-compose -f docker-compose.dev.yml up -d clickhouse
"""
'''
        self.assertEqual(_violations(detect, manifest), [])

    def test_manifest_hook_with_file_equals_flag_satisfies_the_floor(self):
        # `--file=<file>` equals-form -- same space-vs-equals lesson as
        # _GIT_GLOBAL_OPT (AI-466 M1).
        detect = {
            "service_clients": [
                {"package": "psycopg2", "search_terms": ["postgresql"], "source": "requirements.txt"},
            ],
            "services": [{"name": "db", "kind": "postgres", "config_coupled": True}],
        }
        manifest = '''
[install]
docker-compose.pkg-path = "docker-compose"

[hook]
on-activate = """
  docker-compose --file=docker-compose.dev.yml up -d
"""
'''
        self.assertEqual(_violations(detect, manifest), [])

    def test_manifest_hook_with_multiple_compose_opts_satisfies_the_floor(self):
        # Multiple global opts before `up` (repeated -f, plus -p/--env-file).
        detect = {
            "service_clients": [
                {"package": "psycopg2", "search_terms": ["postgresql"], "source": "requirements.txt"},
            ],
            "services": [{"name": "db", "kind": "postgres", "config_coupled": True}],
        }
        manifest = '''
[install]
docker-compose.pkg-path = "docker-compose"

[hook]
on-activate = """
  docker-compose -f base.yml -f dev.yml -p myproj --env-file .env up -d
"""
'''
        self.assertEqual(_violations(detect, manifest), [])

    def test_manifest_hook_docker_compose_down_does_not_satisfy_the_floor(self):
        # `down` is not `up` -- must still fire even with compose installed.
        detect = {
            "service_clients": [
                {"package": "psycopg2", "search_terms": ["postgresql"], "source": "requirements.txt"},
            ],
            "services": [{"name": "db", "kind": "postgres", "config_coupled": True}],
        }
        manifest = '''
[install]
docker-compose.pkg-path = "docker-compose"

[hook]
on-activate = """
  docker-compose down
"""
'''
        v = _violations(detect, manifest)
        self.assertEqual(_rules(v), {"leaf-datastore-not-served"})

    def test_manifest_hook_docker_compose_f_build_does_not_satisfy_the_floor(self):
        # `-f <file> build` -- a global opt is present but the subcommand
        # is `build`, not `up`; must not be mistaken for wiring compose.
        detect = {
            "service_clients": [
                {"package": "psycopg2", "search_terms": ["postgresql"], "source": "requirements.txt"},
            ],
            "services": [{"name": "db", "kind": "postgres", "config_coupled": True}],
        }
        manifest = '''
[install]
docker-compose.pkg-path = "docker-compose"

[hook]
on-activate = """
  docker-compose -f docker-compose.dev.yml build
"""
'''
        v = _violations(detect, manifest)
        self.assertEqual(_rules(v), {"leaf-datastore-not-served"})

    def test_manifest_hook_compose_f_mention_in_comment_does_not_satisfy_the_floor(self):
        # A `-f`-form mention in a `#` comment is not an invocation --
        # same discipline as the existing bare-form comment handling.
        detect = {
            "service_clients": [
                {"package": "psycopg2", "search_terms": ["postgresql"], "source": "requirements.txt"},
            ],
            "services": [{"name": "db", "kind": "postgres", "config_coupled": True}],
        }
        manifest = '''
[install]
docker-compose.pkg-path = "docker-compose"

[hook]
on-activate = """
  # run docker-compose -f docker-compose.dev.yml up -d clickhouse manually
"""
'''
        v = _violations(detect, manifest)
        self.assertEqual(_rules(v), {"leaf-datastore-not-served"})

    def test_manifest_hook_compose_mention_only_inside_echo_does_not_satisfy_the_floor(self):
        # AI-476 M1: an ENTIRE compose mention living inside `echo` (no
        # real invocation anywhere in the hook) must not read as wiring
        # compose -- reproduced live: this used to silently satisfy the
        # leaf-datastore floor with zero services actually started.
        detect = {
            "service_clients": [
                {"package": "psycopg2", "search_terms": ["postgresql"], "source": "requirements.txt"},
            ],
            "services": [{"name": "db", "kind": "postgres", "config_coupled": True}],
        }
        manifest = '''
[install]
docker-compose.pkg-path = "docker-compose"

[hook]
on-activate = """
  echo "run docker-compose -f docker-compose.dev.yml up -d clickhouse manually"
"""
'''
        v = _violations(detect, manifest)
        self.assertEqual(_rules(v), {"leaf-datastore-not-served"})

    def test_manifest_hook_real_compose_invocation_alongside_an_echo_still_satisfies_the_floor(self):
        # The echo-stripping fix must not be so aggressive that a REAL
        # invocation on a separate statement gets masked by an echo
        # elsewhere in the same hook.
        detect = {
            "service_clients": [
                {"package": "psycopg2", "search_terms": ["postgresql"], "source": "requirements.txt"},
            ],
            "services": [{"name": "db", "kind": "postgres", "config_coupled": True}],
        }
        manifest = '''
[install]
docker-compose.pkg-path = "docker-compose"

[hook]
on-activate = """
  echo "bringing up clickhouse via docker-compose"
  docker-compose -f docker-compose.dev.yml up -d clickhouse
"""
'''
        self.assertEqual(_violations(detect, manifest), [])

    def test_docker_compose_up_without_the_package_installed_does_not_satisfy_the_floor(self):
        # The hook TEXT alone isn't enough either -- docker-compose must
        # actually be installed for the invocation to be real.
        detect = {
            "service_clients": [
                {"package": "psycopg2", "search_terms": ["postgresql"], "source": "requirements.txt"},
            ],
            "services": [{"name": "db", "kind": "postgres", "config_coupled": True}],
        }
        manifest = '[hook]\non-activate = "docker-compose up -d"\n'
        v = _violations(detect, manifest)
        self.assertEqual(_rules(v), {"leaf-datastore-not-served"})

    def test_non_leaf_client_terms_are_ignored(self):
        # cryptography -> pkg-config/openssl: not a leaf datastore, no service expected.
        detect = {"service_clients": [
            {"package": "cryptography", "search_terms": ["pkg-config", "openssl"],
             "source": "requirements.txt"},
        ]}
        self.assertEqual(_violations(detect, "[install]\n"), [])

    # --- AI-466 I1: Cargo.lock reads the FULL transitive dependency graph
    # (dev-deps, build-deps, feature-unified workspace deps -- Cargo.lock
    # doesn't distinguish), unlike every other client source here, which
    # reads direct/declared deps only. Uncorroborated lock-only evidence
    # must not HARD-block a correct manifest. Reviewer's exact
    # reproduction: a sqlite-only Rust manifest whose Cargo.lock
    # transitively carries pq-sys used to HARD-fail. ---

    def test_uncorroborated_cargo_lock_evidence_downgrades_to_advisory(self):
        detect = {"service_clients": [
            {"package": "pq-sys", "search_terms": ["postgresql"], "source": "Cargo.lock"},
        ]}
        # sqlite-only manifest: no [vars] postgres endpoint, no compose
        # service, no [services.postgres] -- nothing corroborates pq-sys
        # actually being a runtime need (it could be a dev-dependency's
        # transitive pull, or a feature-unified workspace member the
        # built binary never links).
        manifest = '[install]\nsqlite.pkg-path = "sqlite"\n'
        v = _violations(detect, manifest)
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0]["rule"], "leaf-datastore-not-served")
        self.assertEqual(v[0]["severity"], "advisory")

    def test_uncorroborated_lock_evidence_never_contributes_to_hard_violations(self):
        detect = {"service_clients": [
            {"package": "pq-sys", "search_terms": ["postgresql"], "source": "Cargo.lock"},
        ]}
        manifest = '[install]\nsqlite.pkg-path = "sqlite"\n'
        self.assertEqual(_hard(_violations(detect, manifest)), [])

    def test_cargo_lock_evidence_corroborated_by_vars_endpoint_still_hard_fires(self):
        # Preserves the lemmy incident coverage exactly: reps 3/4 carried a
        # [vars] postgres URL alongside the Cargo.lock pq-sys signal.
        detect = {"service_clients": [
            {"package": "pq-sys", "search_terms": ["postgresql"], "source": "Cargo.lock"},
        ]}
        manifest = '[vars]\nDATABASE_URL = "postgres://u:p@localhost:5433/app"\n'
        v = _violations(detect, manifest)
        self.assertEqual(_rules(v), {"leaf-datastore-not-served", "vars-endpoint-not-served"})
        hard = _hard(v)
        self.assertEqual(len(hard), 2)

    def test_cargo_lock_evidence_corroborated_by_compose_service_still_hard_fires(self):
        # Corroboration via a repo-level compose service of the same kind
        # (independent of whether the manifest WIRES it -- that's the
        # separate _manifest_wires_compose question).
        detect = {
            "service_clients": [
                {"package": "pq-sys", "search_terms": ["postgresql"], "source": "Cargo.lock"},
            ],
            "services": [{"name": "db", "kind": "postgres", "config_coupled": True}],
        }
        manifest = '[install]\ncargo.pkg-path = "cargo"\n'
        v = _violations(detect, manifest)
        self.assertEqual(_rules(v), {"leaf-datastore-not-served"})
        self.assertEqual(v[0]["severity"], "hard")

    def test_non_cargo_lock_source_also_needs_corroboration_now(self):
        # AI-467 SUPERSEDES this test's original AI-466 assertion (it used
        # to require corroboration for Cargo.lock only, and HARD-fire
        # every other source unconditionally). Renamed and inverted to
        # reflect the generalized rule: an uncorroborated
        # requirements.txt client downgrades to ADVISORY exactly like an
        # uncorroborated Cargo.lock client does.
        detect = {"service_clients": [
            {"package": "psycopg2", "search_terms": ["postgresql"], "source": "requirements.txt",
             "scope": "runtime"},
        ]}
        manifest = '[install]\n'
        v = _violations(detect, manifest)
        self.assertEqual(_rules(v), {"leaf-datastore-not-served"})
        self.assertEqual(v[0]["severity"], "advisory")

    # --- AI-482: a socket-shaped [vars] entry must corroborate exactly
    # like a connection-string one -- parity with the TCP path above. ---

    def test_socket_shaped_vars_entry_corroborates_hard(self):
        # Same shape as test_fires_hard_when_client_is_corroborated, but
        # the corroborating [vars] entry is a socket dir (PGHOST), not a
        # postgres:// URL -- the real shape SKILL.md's postgres pattern
        # now emits by default (PR #59).
        detect = {"service_clients": [
            {"package": "pg", "search_terms": ["postgresql"], "source": "package.json",
             "scope": "runtime"},
        ]}
        manifest = '[vars]\nPGHOST = "/tmp/myapp-postgres"\n'
        v = _hard(_violations(detect, manifest))
        rules = {x["rule"] for x in v}
        self.assertIn("leaf-datastore-not-served", rules)

    def test_dev_scoped_client_stays_advisory_even_with_socket_corroboration(self):
        # Parity with test_dev_scoped_client_downgrades_to_advisory_even_
        # when_corroborated: the scope guard must not be defeated just
        # because the corroborating evidence is now socket-shaped instead
        # of a connection string.
        detect = {"service_clients": [
            {"package": "pg-native", "search_terms": ["postgresql"], "source": "package.json",
             "scope": "dev"},
        ]}
        manifest = '[vars]\nPGHOST = "/tmp/myapp-postgres"\n'
        v = _violations(detect, manifest)
        leaf = [x for x in v if x["rule"] == "leaf-datastore-not-served"]
        self.assertEqual(len(leaf), 1)
        self.assertEqual(leaf[0]["severity"], "advisory")


# ---------------------------------------------------------------------------
# invariant 3 — [vars] endpoint implies a service
# ---------------------------------------------------------------------------

class TestVarsEndpoints(unittest.TestCase):
    def test_fires_on_unserved_connection_string(self):
        manifest = '[vars]\nDATABASE_URL = "postgres://u:p@127.0.0.1:5432/app"\n'
        v = _violations({}, manifest)
        self.assertEqual(_rules(v), {"vars-endpoint-not-served"})

    def test_does_not_fire_when_service_serves_it(self):
        manifest = f'''
[vars]
DATABASE_URL = "postgres://u:p@127.0.0.1:5432/app"

{POSTGRES_SERVICE}'''
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

    # --- host-blindness fix (Minor review finding): an external managed
    # datastore with no local service is a common, often-intentional
    # pattern -- must be ADVISORY, not HARD. ---

    def test_external_managed_host_downgrades_to_advisory(self):
        manifest = (
            '[vars]\nDATABASE_URL = '
            '"postgres://u:p@db.prod.internal.example.com:5432/app"\n'
        )
        v = _violations({}, manifest)
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0]["rule"], "vars-endpoint-not-served")
        self.assertEqual(v[0]["severity"], "advisory")

    def test_advisory_endpoint_never_contributes_to_hard_violations(self):
        manifest = (
            '[vars]\nDATABASE_URL = '
            '"postgres://u:p@db.prod.internal.example.com:5432/app"\n'
        )
        self.assertEqual(_hard(_violations({}, manifest)), [])

    # --- AI-466 Hole 1: repo-side compose presence must not silence this
    # invariant. This is the exact forensic reproduction from the lemmy x5
    # re-run: reps 3+4 both had a [vars] postgres URL and a hook, no
    # [services.postgres], and both "passed" because the repo's own
    # docker-compose.yml (a detect.py fact, not anything the manifest
    # wired) was read as sufficient. ---

    def test_fires_even_when_detect_found_a_matching_compose_service(self):
        detect = {"services": [{"name": "db", "kind": "postgres", "config_coupled": True}]}
        manifest = '[vars]\nDATABASE_URL = "postgres://u:p@localhost:5433/app"\n'
        v = _violations(detect, manifest)
        self.assertEqual(_rules(v), {"vars-endpoint-not-served"})
        self.assertEqual(v[0]["severity"], "hard")

    def test_stays_clean_when_hook_genuinely_runs_docker_compose_up(self):
        detect = {"services": [{"name": "db", "kind": "postgres", "config_coupled": True}]}
        manifest = '''
[install]
docker-compose.pkg-path = "docker-compose"

[vars]
DATABASE_URL = "postgres://u:p@localhost:5433/app"

[hook]
on-activate = """
  docker-compose up -d
"""
'''
        self.assertEqual(_violations(detect, manifest), [])

    def test_docker_compose_mentioned_in_a_comment_does_not_satisfy_the_floor(self):
        # A hook that only TALKS about docker-compose (doesn't run it) must
        # not be read as wiring the service -- same discipline as the
        # comment-stripping already applied to hook-mutation detection.
        detect = {"services": [{"name": "db", "kind": "postgres", "config_coupled": True}]}
        manifest = '''
[install]
docker-compose.pkg-path = "docker-compose"

[vars]
DATABASE_URL = "postgres://u:p@localhost:5433/app"

[hook]
on-activate = """
  # run `docker-compose up` manually if you need postgres
  echo "ready"
"""
'''
        v = _violations(detect, manifest)
        self.assertEqual(_rules(v), {"vars-endpoint-not-served"})

    def test_bare_compose_service_name_host_stays_hard(self):
        # A dotless hostname ("db") is almost certainly a docker-compose /
        # k8s service name, not a real external FQDN -- stays HARD.
        manifest = '[vars]\nDATABASE_URL = "postgres://u:p@db:5432/app"\n'
        v = _violations({}, manifest)
        self.assertEqual(_rules(v), {"vars-endpoint-not-served"})
        self.assertEqual(v[0]["severity"], "hard")

    def test_non_connection_string_vars_are_ignored(self):
        manifest = '[vars]\nRAILS_ENV = "development"\n'
        self.assertEqual(_violations({}, manifest), [])

    # --- AI-482: socket-shaped [vars] endpoints -- SKILL.md's postgres
    # and redis patterns default to a unix socket, not TCP (PR #59), so
    # a connection-string-only check is blind to exactly the shape the
    # skill itself now emits by default. ---

    def test_fires_hard_on_unserved_socket_dir_pghost(self):
        # Socket-dir PGHOST (libpq convention) with no [services.postgres].
        manifest = '[vars]\nPGHOST = "/tmp/myapp-postgres"\n'
        v = _violations({}, manifest)
        self.assertEqual(_rules(v), {"vars-endpoint-not-served"})
        self.assertEqual(v[0]["severity"], "hard")

    def test_socket_dir_pghost_served_by_service_does_not_fire(self):
        manifest = '''
[vars]
PGHOST = "/tmp/myapp-postgres"

[services.postgres]
command = "postgres -k /tmp/myapp-postgres"
'''
        self.assertEqual(_violations({}, manifest), [])

    def test_fires_hard_on_socket_named_var_no_dot_sock_suffix(self):
        # *_SOCKET-named var whose value is a bare directory, not a
        # `.sock` file -- name alone is enough to recognize the shape.
        manifest = '[vars]\nREDIS_SOCKET = "/tmp/myapp-redis"\n'
        v = _violations({}, manifest)
        self.assertEqual(_rules(v), {"vars-endpoint-not-served"})
        self.assertEqual(v[0]["severity"], "hard")

    def test_fires_hard_on_dot_sock_value_without_socket_named_var(self):
        # `.sock`-suffixed value recognized even when the var name itself
        # doesn't end in `_SOCKET`.
        manifest = '[vars]\nREDIS_CONN = "/tmp/myapp-redis.sock"\n'
        v = _violations({}, manifest)
        self.assertEqual(_rules(v), {"vars-endpoint-not-served"})
        self.assertEqual(v[0]["severity"], "hard")

    def test_redis_socket_served_by_service_does_not_fire(self):
        manifest = '''
[vars]
REDIS_SOCKET = "/tmp/myapp-redis.sock"

[services.redis]
command = "redis-server --unixsocket /tmp/myapp-redis.sock"
'''
        self.assertEqual(_violations({}, manifest), [])

    def test_fires_hard_on_unix_scheme_url(self):
        manifest = '[vars]\nREDIS_URL = "unix:///tmp/myapp-redis.sock"\n'
        v = _violations({}, manifest)
        self.assertEqual(_rules(v), {"vars-endpoint-not-served"})
        self.assertEqual(v[0]["severity"], "hard")

    def test_unix_scheme_url_served_by_service_does_not_fire(self):
        manifest = '''
[vars]
REDIS_URL = "unix:///tmp/myapp-redis.sock"

[services.redis]
command = "redis-server --unixsocket /tmp/myapp-redis.sock"
'''
        self.assertEqual(_violations({}, manifest), [])

    def test_socket_dir_served_by_genuine_compose_up_does_not_fire(self):
        # Parity with the connection-string compose-coverage case.
        detect = {"services": [{"name": "db", "kind": "postgres", "config_coupled": True}]}
        manifest = '''
[install]
docker-compose.pkg-path = "docker-compose"

[vars]
PGHOST = "/tmp/myapp-postgres"

[hook]
on-activate = """
  docker-compose up -d
"""
'''
        self.assertEqual(_violations(detect, manifest), [])

    def test_socket_shaped_var_with_dev_scoped_client_downgrades_to_advisory(self):
        # Provenance guard: the only corroborating detect.py evidence for
        # this kind is a dev/test/optional-scoped client -- the same
        # "not proven to be a live local need" signal
        # check_leaf_datastore_services already uses (AI-467's
        # section-provenance scope), applied here so a socket-shaped var
        # doesn't unconditionally HARD-fire when the manifest's own
        # client evidence says this is client-side config, not a service
        # this environment is expected to run itself.
        detect = {"service_clients": [
            {"package": "pymysql", "search_terms": ["mariadb"], "source": "pyproject.toml",
             "scope": "dev"},
        ]}
        manifest = '[vars]\nMYSQL_SOCKET = "/var/run/mysqld/mysqld.sock"\n'
        v = _violations(detect, manifest)
        # The dev-scoped client itself also independently fires an
        # ADVISORY leaf-datastore-not-served (check_leaf_datastore_
        # services' own pre-existing scope guard) -- expected, not what
        # this test is about. Isolate the vars-endpoint rule.
        self.assertEqual(_hard(v), [])
        endpoint = [x for x in v if x["rule"] == "vars-endpoint-not-served"]
        self.assertEqual(len(endpoint), 1)
        self.assertEqual(endpoint[0]["severity"], "advisory")

    def test_socket_shaped_var_with_no_client_evidence_stays_hard(self):
        # No detect facts at all (the common case) -- default is HARD,
        # not a free pass just because there's no corroborating client.
        v = _violations(None, '[vars]\nPGHOST = "/tmp/myapp-postgres"\n')
        self.assertEqual(_rules(v), {"vars-endpoint-not-served"})
        self.assertEqual(v[0]["severity"], "hard")

    def test_tcp_pghost_value_is_not_socket_shaped(self):
        # A loopback/hostname PGHOST is the existing TCP pattern, not a
        # socket dir -- must not be swept in by the new socket check
        # (out of this ticket's scope; verify.py's connection-string
        # check doesn't cover bare PGHOST=host forms either).
        self.assertEqual(_violations({}, '[vars]\nPGHOST = "127.0.0.1"\n'), [])

    def test_unrelated_socket_var_is_not_recognized_as_a_datastore(self):
        # unix:// scheme + absolute path, but neither the var name nor
        # the value names any of the four recognized leaf-datastore
        # kinds -- must not false-fire (the Docker daemon socket is
        # client-side config for Docker, not something a [services.*]
        # entry would ever serve).
        manifest = '[vars]\nDOCKER_HOST = "unix:///var/run/docker.sock"\n'
        self.assertEqual(_violations({}, manifest), [])

    # --- Code review finding (AI-482 PR #65 C1): the bare `PG*`-prefix
    # rule HARD-fired on standard libpq file/dir-path vars that are not
    # endpoints -- narrowed to the exact `PGHOST` var name. These lock
    # the narrowing in. ---

    def test_pgdata_absolute_path_is_not_socket_shaped(self):
        # PGDATA is the server-side data directory, not a socket -- the
        # exact false positive the review reproduced.
        self.assertEqual(
            _violations({}, '[vars]\nPGDATA = "/var/lib/postgresql/data"\n'), [],
        )

    def test_pgsslrootcert_absolute_path_is_not_socket_shaped(self):
        # A TLS cert path for an external managed postgres connection --
        # the worst-case reproduction: this var alongside a correctly
        # ADVISORY non-local DATABASE_URL must not itself turn HARD.
        manifest = '''
[vars]
DATABASE_URL = "postgres://prod.rds.amazonaws.com:5432/app?sslmode=verify-full"
PGSSLROOTCERT = "/etc/ssl/certs/rds-ca.pem"
'''
        v = _violations({}, manifest)
        self.assertEqual(_hard(v), [])
        rules = _rules(v)
        self.assertEqual(rules, {"vars-endpoint-not-served"})


# ---------------------------------------------------------------------------
# invariant 4 — [vars] are literal, never `$`-expanded
# ---------------------------------------------------------------------------

class TestVarsLiteral(unittest.TestCase):
    def test_fires_on_dollar_in_vars(self):
        manifest = '[vars]\nUV_PROJECT_ENVIRONMENT = "$FLOX_ENV_CACHE/venv"\n'
        v = _violations({}, manifest)
        self.assertEqual(_rules(v), {"vars-not-literal"})

    def test_fires_on_braced_expansion(self):
        manifest = '[vars]\nDATA_DIR = "${FLOX_ENV_CACHE}/data"\n'
        v = _violations({}, manifest)
        self.assertEqual(_rules(v), {"vars-not-literal"})

    def test_does_not_fire_on_plain_literal_vars(self):
        manifest = '[vars]\nPGDATABASE = "myapp_dev"\nPGPORT = "5432"\n'
        self.assertEqual(_violations({}, manifest), [])

    # --- false-positive shapes a plain "any '$'" check used to HARD-fire on
    # (Important review finding: a password, a bcrypt hash, or an argon2
    # hash each contain a literal '$' with no shell-expansion intent) ---

    def test_does_not_fire_on_password_containing_dollar(self):
        manifest = '[vars]\nPGPASSWORD = "p@ss$word5"\n'
        self.assertEqual(_violations({}, manifest), [])

    def test_does_not_fire_on_bcrypt_hash(self):
        manifest = '[vars]\nADMIN_HASH = "$2b$10$N9qo8uLOickgx2ZMRZoMyeIjZAgcfl7p92ldGxad68LJZdL17lhWy"\n'
        self.assertEqual(_violations({}, manifest), [])

    def test_does_not_fire_on_argon2_hash(self):
        manifest = ('[vars]\nARGON_HASH = '
                    '"$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$hash"\n')
        self.assertEqual(_violations({}, manifest), [])

    def test_does_not_fire_on_price_template_with_lowercase_after_dollar(self):
        manifest = '[vars]\nNOTE = "cost is $5 per unit, see $doc for details"\n'
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
        # AI-450: `submodule update` is also a network fetch, so it
        # co-fires the ADVISORY hook-network-fetch note alongside this
        # HARD rule -- see TestHookNetwork.
        self.assertEqual(_rules(_hard(v)), {"hook-mutates-tree"})

    def test_fires_on_git_checkout(self):
        manifest = '[hook]\non-activate = "git checkout main"\n'
        v = _violations({}, manifest)
        self.assertEqual(_rules(v), {"hook-mutates-tree"})

    # --- previously-missed verbs (Important review finding) --------------

    def test_fires_on_git_restore(self):
        manifest = '[hook]\non-activate = "git restore ."\n'
        v = _violations({}, manifest)
        self.assertEqual(_rules(v), {"hook-mutates-tree"})

    def test_fires_on_git_switch(self):
        manifest = '[hook]\non-activate = "git switch main"\n'
        v = _violations({}, manifest)
        self.assertEqual(_rules(v), {"hook-mutates-tree"})

    def test_fires_on_git_revert(self):
        manifest = '[hook]\non-activate = "git revert HEAD --no-edit"\n'
        v = _violations({}, manifest)
        self.assertEqual(_rules(v), {"hook-mutates-tree"})

    # --- false-positive shapes (Important review finding: comments,
    # echoed/printed text, and read-only dry-run forms must not fire) ----

    def test_does_not_fire_on_git_verb_inside_a_comment(self):
        manifest = '''
[hook]
on-activate = """
  # if things break, git reset --hard and start over
  uv sync
"""
'''
        self.assertEqual(_violations({}, manifest), [])

    def test_does_not_fire_on_git_verb_inside_echoed_text(self):
        manifest = '''
[hook]
on-activate = """
  echo "Tip: run git checkout main to switch branches"
"""
'''
        self.assertEqual(_violations({}, manifest), [])

    def test_does_not_fire_on_git_verb_inside_printf_text(self):
        manifest = '[hook]\non-activate = "printf \\"see: git commit --amend\\\\n\\""\n'
        self.assertEqual(_violations({}, manifest), [])

    def test_does_not_fire_on_git_apply_check_dry_run(self):
        manifest = '[hook]\non-activate = "git apply --check patch.diff"\n'
        self.assertEqual(_violations({}, manifest), [])

    def test_still_fires_on_git_apply_without_check(self):
        # The exemption above must be narrow -- a real `git apply` (no
        # dry-run flag) still mutates the tree and must still fire.
        manifest = '[hook]\non-activate = "git apply patch.diff"\n'
        v = _violations({}, manifest)
        self.assertEqual(_rules(v), {"hook-mutates-tree"})

    def test_comment_and_real_mutation_on_separate_lines_both_handled(self):
        # A comment mentioning a git verb must not mask a REAL mutation
        # elsewhere in the same hook.
        manifest = '''
[hook]
on-activate = """
  # note: git checkout is sometimes needed manually
  git reset --hard origin/main
"""
'''
        v = _violations({}, manifest)
        self.assertEqual(_rules(v), {"hook-mutates-tree"})
        self.assertEqual(len(v), 1)
        self.assertIn("git reset --hard origin/main", v[0]["message"])

    # --- AI-466 Hole 3: `git [global options] <verb>` forms must not
    # evade detection. Reproduced: `git -C "$FLOX_ENV_PROJECT" submodule
    # update --init` exited 0 (false negative) while the bare form fired
    # HARD. ---

    def test_fires_on_git_dash_c_submodule_update(self):
        manifest = (
            '[hook]\non-activate = '
            '\'git -C "$FLOX_ENV_PROJECT" submodule update --init\'\n'
        )
        v = _violations({}, manifest)
        # AI-450: co-fires the ADVISORY hook-network-fetch note too -- see
        # the sibling test above.
        self.assertEqual(_rules(_hard(v)), {"hook-mutates-tree"})

    def test_fires_on_git_dash_c_checkout(self):
        manifest = '[hook]\non-activate = "git -C /repo checkout main"\n'
        v = _violations({}, manifest)
        self.assertEqual(_rules(v), {"hook-mutates-tree"})

    def test_fires_on_git_dash_c_config_option_reset(self):
        manifest = (
            '[hook]\non-activate = '
            '"git -c user.name=flox -C /repo reset --hard"\n'
        )
        v = _violations({}, manifest)
        self.assertEqual(_rules(v), {"hook-mutates-tree"})

    def test_fires_on_git_dir_option_commit(self):
        # Deliberately does NOT end the path in "/.git" -- that would let
        # the OLD regex accidentally match on the unrelated "git" inside
        # the path value itself (a coincidental true positive that proves
        # nothing about the actual --git-dir= handling).
        manifest = (
            '[hook]\non-activate = '
            '"git --git-dir=/repo/custom-gitdir commit -am wip"\n'
        )
        v = _violations({}, manifest)
        self.assertEqual(_rules(v), {"hook-mutates-tree"})

    def test_does_not_fire_on_read_only_git_dash_c_log(self):
        manifest = '[hook]\non-activate = "git -C /repo log --oneline -5"\n'
        self.assertEqual(_violations({}, manifest), [])

    def test_does_not_fire_on_read_only_git_dash_c_apply_check(self):
        manifest = '[hook]\non-activate = "git -C /repo apply --check patch.diff"\n'
        self.assertEqual(_violations({}, manifest), [])

    def test_git_dash_c_verb_inside_a_comment_still_does_not_fire(self):
        # Comment-stripping discipline must hold for the -C form too.
        manifest = '''
[hook]
on-activate = """
  # if things break: git -C "$FLOX_ENV_PROJECT" reset --hard
  uv sync
"""
'''
        self.assertEqual(_violations({}, manifest), [])

    # --- AI-466 M1: the long global options (--git-dir, --work-tree,
    # --namespace) accept BOTH `--opt=value` and `--opt value` forms in
    # real git -- the `=` form alone let the space form evade detection. ---

    def test_fires_on_git_work_tree_space_form_reset(self):
        manifest = '[hook]\non-activate = "git --work-tree /tmp reset --hard"\n'
        v = _violations({}, manifest)
        self.assertEqual(_rules(v), {"hook-mutates-tree"})

    def test_fires_on_git_dir_space_form_checkout(self):
        # Deliberately does not end in "/.git" -- see the equals-form test
        # above for why that would be a coincidental (not genuine) match.
        manifest = '[hook]\non-activate = "git --git-dir /repo/custom-gitdir checkout main"\n'
        v = _violations({}, manifest)
        self.assertEqual(_rules(v), {"hook-mutates-tree"})

    def test_fires_on_git_namespace_space_form_commit(self):
        manifest = '[hook]\non-activate = "git --namespace foo commit -am wip"\n'
        v = _violations({}, manifest)
        self.assertEqual(_rules(v), {"hook-mutates-tree"})

    def test_does_not_fire_on_read_only_git_work_tree_space_form_log(self):
        manifest = '[hook]\non-activate = "git --work-tree /tmp log --oneline"\n'
        self.assertEqual(_violations({}, manifest), [])

    def test_equals_form_still_works_alongside_space_form(self):
        # Regression guard: adding the space alternative must not break
        # the already-fixed `=` form.
        manifest = '[hook]\non-activate = "git --work-tree=/tmp reset --hard"\n'
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
# heuristic — network-fetching operations in on-activate (ADVISORY) — AI-450
# ---------------------------------------------------------------------------

class TestHookNetwork(unittest.TestCase):
    def test_fires_on_git_clone(self):
        manifest = '[hook]\non-activate = "git clone https://example.com/x.git vendor/x"\n'
        v = _violations({}, manifest)
        fired = [x for x in v if x["rule"] == "hook-network-fetch"]
        self.assertEqual(len(fired), 1, v)
        self.assertEqual(fired[0]["severity"], "advisory")

    def test_fires_on_git_fetch(self):
        manifest = '[hook]\non-activate = "git fetch origin"\n'
        v = _violations({}, manifest)
        self.assertEqual({x["rule"] for x in v if x["severity"] == "advisory"},
                          {"hook-network-fetch"})

    def test_fires_on_curl(self):
        manifest = '[hook]\non-activate = "curl -fsSL https://example.com/install.sh | sh"\n'
        v = _violations({}, manifest)
        self.assertIn("hook-network-fetch", {x["rule"] for x in v})

    def test_fires_on_wget(self):
        manifest = '[hook]\non-activate = "wget -O out.tar.gz https://example.com/a.tar.gz"\n'
        v = _violations({}, manifest)
        self.assertIn("hook-network-fetch", {x["rule"] for x in v})

    def test_never_contributes_to_hard_violations(self):
        manifest = '[hook]\non-activate = "git clone https://example.com/x.git"\n'
        self.assertEqual(_hard(_violations({}, manifest)), [])

    def test_fires_on_git_dash_c_clone(self):
        # Reuses _GIT_GLOBAL_OPT -- a `-C <path>` between `git` and the
        # verb must not evade this the same way AI-466 Hole 3 found for
        # the mutation check.
        manifest = (
            '[hook]\non-activate = '
            '"git -C /workspace clone https://example.com/x.git"\n'
        )
        v = _violations({}, manifest)
        self.assertIn("hook-network-fetch", {x["rule"] for x in v})

    def test_git_pull_fires_both_mutation_hard_and_network_advisory(self):
        # Deliberate dual-classification: `pull` is both a tree mutation
        # AND a network fetch -- two distinct, both-true concerns.
        manifest = '[hook]\non-activate = "git pull"\n'
        v = _violations({}, manifest)
        rules = {x["rule"] for x in v}
        self.assertEqual(rules, {"hook-mutates-tree", "hook-network-fetch"})

    def test_git_submodule_update_fires_both_mutation_hard_and_network_advisory(self):
        # AI-476/AI-450 review M2: `submodule update` dual-fires the same
        # way `pull` does above -- pinned separately since only pull's
        # co-firing was asserted before this.
        manifest = '[hook]\non-activate = "git submodule update --init"\n'
        v = _violations({}, manifest)
        rules = {x["rule"] for x in v}
        self.assertEqual(rules, {"hook-mutates-tree", "hook-network-fetch"})

    def test_git_dash_c_submodule_update_fires_both_mutation_hard_and_network_advisory(self):
        # Same dual-fire through the `-C <path>` global-opt reuse form.
        manifest = (
            '[hook]\non-activate = '
            '\'git -C "$FLOX_ENV_PROJECT" submodule update --init\'\n'
        )
        v = _violations({}, manifest)
        rules = {x["rule"] for x in v}
        self.assertEqual(rules, {"hook-mutates-tree", "hook-network-fetch"})

    def test_does_not_fire_on_git_verb_inside_a_comment(self):
        manifest = '''
[hook]
on-activate = """
  # if you need a fresh clone: git clone https://example.com/x.git
  uv sync
"""
'''
        self.assertNotIn(
            "hook-network-fetch",
            {x["rule"] for x in _violations({}, manifest)},
        )

    def test_does_not_fire_on_curl_inside_echoed_text(self):
        manifest = '''
[hook]
on-activate = """
  echo "Tip: curl -fsSL https://example.com/install.sh | sh"
"""
'''
        self.assertEqual(_violations({}, manifest), [])

    def test_does_not_fire_on_curl_mentioned_as_an_argument(self):
        # "curl" appearing mid-statement (not as the leading command) must
        # not be mistaken for an invocation.
        manifest = '[hook]\non-activate = "some-tool --user-agent curl-compatible"\n'
        self.assertEqual(_violations({}, manifest), [])

    # --- calibration bar: accepted bootstrap idioms across every current
    # golden must never fire. ---

    def test_does_not_fire_on_uv_sync(self):
        manifest = '[hook]\non-activate = "uv sync --frozen || uv sync"\n'
        self.assertEqual(_violations({}, manifest), [])

    def test_does_not_fire_on_npm_install(self):
        manifest = '[hook]\non-activate = "npm install"\n'
        self.assertEqual(_violations({}, manifest), [])

    def test_does_not_fire_on_pnpm_install(self):
        manifest = '[hook]\non-activate = "pnpm install --frozen-lockfile"\n'
        self.assertEqual(_violations({}, manifest), [])

    def test_does_not_fire_on_yarn_install(self):
        manifest = '[hook]\non-activate = "yarn install"\n'
        self.assertEqual(_violations({}, manifest), [])

    def test_does_not_fire_on_bundle_install(self):
        manifest = '[hook]\non-activate = "bundle install"\n'
        self.assertEqual(_violations({}, manifest), [])

    def test_does_not_fire_on_composer_install(self):
        manifest = '[hook]\non-activate = "composer install --no-interaction"\n'
        self.assertEqual(_violations({}, manifest), [])

    def test_does_not_fire_on_mix_deps_get(self):
        manifest = '[hook]\non-activate = "mix deps.get"\n'
        self.assertEqual(_violations({}, manifest), [])

    def test_does_not_fire_on_corepack_enable(self):
        manifest = (
            '[hook]\non-activate = '
            '"corepack enable --install-directory \\"$FLOX_ENV_CACHE/node-bin\\" pnpm"\n'
        )
        self.assertEqual(_violations({}, manifest), [])

    def test_does_not_fire_when_no_hook_present(self):
        self.assertEqual(_violations({}, "[install]\n"), [])


class TestMalformedHookNeverRaises(unittest.TestCase):
    """AI-484: manifest_wires_compose, check_hook_no_mutation, and
    check_hook_network share a single tokenizer (_hook_statements). A
    `hook` section declared as a non-table TOML value (F1/F2 shape,
    same fragility class AI-485 covers for install/vars) is valid TOML
    but not a dict -- must degrade to "no hook" for all three callers,
    the same never-raises contract AI-485 established elsewhere,
    rather than raising AttributeError on `.get("on-activate")`.
    """

    def _manifest(self, hook_text):
        manifest, error = verify_mod.parse_manifest(hook_text)
        self.assertIsNone(error, error)
        return manifest

    def test_scalar_hook_never_raises_across_all_three_checks(self):
        manifest = self._manifest('hook = "echo hi"\n')
        self.assertFalse(verify_mod.manifest_wires_compose(manifest))
        self.assertEqual(verify_mod.check_hook_no_mutation(manifest), [])
        self.assertEqual(verify_mod.check_hook_network(manifest), [])

    def test_array_hook_never_raises_across_all_three_checks(self):
        manifest = self._manifest('hook = ["x"]\n')
        self.assertFalse(verify_mod.manifest_wires_compose(manifest))
        self.assertEqual(verify_mod.check_hook_no_mutation(manifest), [])
        self.assertEqual(verify_mod.check_hook_network(manifest), [])


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


# Header `Systems:` line restricted to 3 platforms, but the "Other versions"
# block does NOT repeat an entry for Latest -- the shape that used to make
# check_catalog silently default an unpinned install to ALL_SYSTEMS instead
# of the (restricted) header line.
MISSING_LATEST_ENTRY_SHOW = """flaky-pkg - a package whose Other-versions list omits Latest
Catalog: nixpkgs
Latest:  flaky-pkg@9.9.9
License: MIT
Outputs: out* (* installed by default)
Systems: x86_64-linux, aarch64-linux

Other versions:
    flaky-pkg@8.0.0
"""

# An "Other versions" annotation that isn't the recognized "(... only)"
# form -- must be treated as genuinely unknown, not silently as either
# "all four systems" or "exactly the parenthetical contents".
UNRECOGNIZED_ANNOTATION_SHOW = """weird-pkg - a package with an unrecognized annotation format
Catalog: nixpkgs
Latest:  weird-pkg@2.0.0
License: MIT
Outputs: out* (* installed by default)
Systems: x86_64-linux

Other versions:
    weird-pkg@2.0.0 (deprecated, use weird-pkg2)
"""


# Every declared system IS built somewhere, but never all on ONE version:
# 3.0.0 has the three non-x86-darwin platforms, 2.0.0 has x86_64-darwin
# but not the aarch64 pair. An unpinned entry declaring all four cannot
# co-resolve onto any single version -- a genuine catalog-systems-mismatch,
# and a different failure (and a different fix) from "nothing is ever
# built for this system", which is why _uncovered_msg has two shapes.
SPLIT_COVERAGE_SHOW = """split-pkg - a package no single version builds everywhere
Catalog: nixpkgs
Latest:  split-pkg@3.0.0
License: MIT
Outputs: out* (* installed by default)
Systems: x86_64-linux, aarch64-linux, aarch64-darwin

Other versions:
    split-pkg@3.0.0 (aarch64-darwin, aarch64-linux, x86_64-linux only)
    split-pkg@2.0.0 (x86_64-darwin, x86_64-linux only)
"""

# The newest version row is readable and does NOT cover all four; the row
# below it carries an unrecognized annotation. Nothing readable covers
# the declared set and nothing readable rules it out either -- genuinely
# unknown. (Note this needs the unreadable row to be an OLDER one: an
# unrecognized annotation on the LATEST entry is still settled by the
# header `Systems:` line, which _parse_flox_show documents as that
# entry's ground truth.)
UNREADABLE_OLDER_ROW_SHOW = """murky-pkg - a package with an unreadable older row
Catalog: nixpkgs
Latest:  murky-pkg@3.0.0
License: MIT
Outputs: out* (* installed by default)
Systems: x86_64-linux, aarch64-linux

Other versions:
    murky-pkg@3.0.0 (aarch64-linux, x86_64-linux only)
    murky-pkg@2.0.0 (deprecated, use murky-pkg3)
"""

# The newest version covers everything; the row BELOW it carries an
# unrecognized annotation. The unreadable row must not drag the verdict
# to "unknown" -- the question is whether SOME version covers the declared
# systems, and a covering one found first settles it.
COVERING_ABOVE_UNREADABLE_SHOW = """settled-pkg - newest version covers everything
Catalog: nixpkgs
Latest:  settled-pkg@5.0.0
License: MIT
Outputs: out* (* installed by default)
Systems: x86_64-linux, aarch64-linux, x86_64-darwin, aarch64-darwin

Other versions:
    settled-pkg@5.0.0
    settled-pkg@4.0.0 (deprecated, use settled-pkg5)
"""


# Two version rows share the "18" prefix and only the older one builds
# everywhere -- the shape a partial pin has to descend through. Modeled
# on the live `flox show postgresql_18`, where 18.6 sheds x86_64-darwin
# and 18.4 below it does not.
PREFIX_PIN_SHOW = """prefixed-pkg - a package whose newest 18.x sheds a platform
Catalog: nixpkgs
Latest:  prefixed-pkg@18.6
License: MIT
Outputs: out* (* installed by default)
Systems: aarch64-darwin, aarch64-linux, x86_64-linux

Other versions:
    prefixed-pkg@18.6 (aarch64-darwin, aarch64-linux, x86_64-linux only)
    prefixed-pkg@18.4
    prefixed-pkg@17.2 (aarch64-linux, x86_64-linux only)
"""

# One "Other versions" row the row regex cannot read at all -- trailing
# text after the parenthetical. Every OTHER row is readable and none
# covers all four, so without counting the unreadable row this listing
# would support a hard "no build at ANY version" claim over a reading
# that silently lost a row.
UNPARSEABLE_ROW_SHOW = """torn-pkg - a package with a row this parser cannot read
Catalog: nixpkgs
Latest:  torn-pkg@3.0.0
License: MIT
Outputs: out* (* installed by default)
Systems: aarch64-linux, x86_64-linux

Other versions:
    torn-pkg@3.0.0 (aarch64-linux, x86_64-linux only)
    torn-pkg@2.0.0 (all systems) [deprecated]
"""

# Both failures at once: x86_64-darwin is built by no row at all, while
# aarch64-darwin is built by 2.0.0 but not by the newest row. Reporting
# only the first would send the reader to drop x86_64-darwin and then
# meet the aarch64-darwin failure on the next run.
BOTH_FAILURES_SHOW = """doubly-pkg - a package failing both ways at once
Catalog: nixpkgs
Latest:  doubly-pkg@3.0.0
License: MIT
Outputs: out* (* installed by default)
Systems: aarch64-linux, x86_64-linux

Other versions:
    doubly-pkg@3.0.0 (aarch64-linux, x86_64-linux only)
    doubly-pkg@2.0.0 (aarch64-darwin, x86_64-linux only)
"""

# A fully readable version list followed by a trailer line -- output
# `flox show` does not emit today. It must not be counted as an
# unreadable VERSION row, because `unparsed_rows` gates whether an
# absence may be concluded at all: miscounting one footer line would
# turn the catalog check into a permanent `unknown` for the package.
TRAILING_LINE_SHOW = """chatty-pkg - a package whose output has a footer
Catalog: nixpkgs
Latest:  chatty-pkg@3.0.0
License: MIT
Outputs: out* (* installed by default)
Systems: aarch64-linux, x86_64-linux

Other versions:
    chatty-pkg@3.0.0 (aarch64-linux, x86_64-linux only)
    chatty-pkg@2.0.0 (aarch64-linux, x86_64-linux only)

Use 'flox install chatty-pkg' to install.
"""

# A package served from a single version row: a `Latest:` line and no
# "Other versions" section at all. `_catalog_version_rows` documents that
# this still yields one entry rather than an empty list.
SINGLE_ROW_SHOW = """lonely-pkg - a package with only a Latest line
Catalog: nixpkgs
Latest:  lonely-pkg@1.0.0
License: MIT
Outputs: out* (* installed by default)
Systems: aarch64-darwin, aarch64-linux, x86_64-darwin, x86_64-linux
"""

# A one-row package with a real platform gap: `len(candidates) > 1` is
# false here, so this is the shape that lost the "at ANY version"
# wording.
SINGLE_ROW_GAP_SHOW = """only-one-pkg - one version, linux only
Catalog: nixpkgs
Latest:  only-one-pkg@1.0.0
Systems: aarch64-linux, x86_64-linux
"""


# Every row linux-only, so `never_built` over the whole listing is
# {aarch64-darwin, x86_64-darwin} whichever subset a constraint picks.
LINUX_ONLY_SHOW = """linux-only-pkg - built for linux only
Catalog: nixpkgs
Latest:  linux-only-pkg@3.0.0
Systems: aarch64-linux, x86_64-linux

Other versions:
    linux-only-pkg@3.0.0 (aarch64-linux, x86_64-linux only)
    linux-only-pkg@2.0.0 (aarch64-linux, x86_64-linux only)
"""

# `flox show` answered, and this parser recovered nothing from its text --
# the shape a renamed header or a moved output format produces. Not a
# package with no versions, and the two must not read the same.
RENAMED_HEADERS_SHOW = """renamed-pkg - output format moved
Catalog: nixpkgs
Newest:  renamed-pkg@1.2.3
Platforms: aarch64-linux, x86_64-linux

Additional versions:
    renamed-pkg@1.2.3 (aarch64-linux, x86_64-linux only)
"""

# The second shape, and the one `not rows` alone does not catch: `Latest:`
# still parses, so a row survives, but the "Other versions" block lost its
# indentation and ends at its first line -- every version below the latest
# silently vanished with `unparsed_rows` at zero.
UNINDENTED_BLOCK_SHOW = """flat-pkg - version rows stopped being indented
Catalog: nixpkgs
Latest:  flat-pkg@18
Systems: aarch64-linux, x86_64-linux

Other versions:
flat-pkg@18.4 (aarch64-linux, x86_64-linux only)
flat-pkg@18 (aarch64-linux, x86_64-linux only)
"""


def _mock_show(pkg_path, flox_bin, timeout):
    if pkg_path == "postgresql":
        return _FakeProc(stdout=POSTGRESQL_SHOW)
    if pkg_path == "prefixed-pkg":
        return _FakeProc(stdout=PREFIX_PIN_SHOW)
    if pkg_path == "torn-pkg":
        return _FakeProc(stdout=UNPARSEABLE_ROW_SHOW)
    if pkg_path == "lonely-pkg":
        return _FakeProc(stdout=SINGLE_ROW_SHOW)
    if pkg_path == "doubly-pkg":
        return _FakeProc(stdout=BOTH_FAILURES_SHOW)
    if pkg_path == "chatty-pkg":
        return _FakeProc(stdout=TRAILING_LINE_SHOW)
    if pkg_path == "nodejs_24":
        return _FakeProc(stdout=NODEJS_24_SHOW)
    if pkg_path == "python313":
        return _FakeProc(stdout=PYTHON313_SHOW)
    if pkg_path == "flaky-pkg":
        return _FakeProc(stdout=MISSING_LATEST_ENTRY_SHOW)
    if pkg_path == "weird-pkg":
        return _FakeProc(stdout=UNRECOGNIZED_ANNOTATION_SHOW)
    if pkg_path == "murky-pkg":
        return _FakeProc(stdout=UNREADABLE_OLDER_ROW_SHOW)
    if pkg_path == "split-pkg":
        return _FakeProc(stdout=SPLIT_COVERAGE_SHOW)
    if pkg_path == "settled-pkg":
        return _FakeProc(stdout=COVERING_ABOVE_UNREADABLE_SHOW)
    if pkg_path == "linux-only-pkg":
        return _FakeProc(stdout=LINUX_ONLY_SHOW)
    if pkg_path == "renamed-pkg":
        return _FakeProc(stdout=RENAMED_HEADERS_SHOW)
    if pkg_path == "flat-pkg":
        return _FakeProc(stdout=UNINDENTED_BLOCK_SHOW)
    if pkg_path == "only-one-pkg":
        return _FakeProc(stdout=SINGLE_ROW_GAP_SHOW)
    return _FakeProc(returncode=1, stderr=f"✘ ERROR: no packages matched this pkg-path: '{pkg_path}'")


class TestRunShowCommand(unittest.TestCase):
    """Minor review finding: a leading-dash pkg_path must not be read as a
    flag by `flox show` — `--` separates the positional argument."""

    @patch(f"{_MODULE_KEY}.subprocess.run")
    def test_pkg_path_passed_after_double_dash_separator(self, mock_run):
        verify_mod._run_show_command("-weird-pkg", "flox", 30)
        args = mock_run.call_args[0][0]
        self.assertIn("--", args)
        self.assertEqual(args[args.index("--") + 1], "-weird-pkg")


class TestCatalog(unittest.TestCase):
    def setUp(self):
        verify_mod._SHOW_CACHE.clear()

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_unresolved_pkg_path_fires(self, mock_run, mock_which):
        manifest = '[install]\nghost.pkg-path = "nonexistent-pkg-zzz"\n'
        v = verify({}, manifest, check_catalog_live=True)["violations"]
        self.assertEqual(_rules(v), {"catalog-unresolved"})

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_missing_version_fires(self, mock_run, mock_which):
        manifest = '[install]\npg.pkg-path = "postgresql"\npg.version = "99.99"\n'
        v = verify({}, manifest, check_catalog_live=True)["violations"]
        self.assertEqual(_rules(v), {"catalog-version-missing"})

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
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
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
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
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_unpinned_fires_when_no_version_builds_a_declared_system(
        self, mock_run, mock_which,
    ):
        # nodejs_24 with no .version pinned; default systems (no
        # [options]) = all four. NONE of its three version rows has an
        # x86_64-darwin build, so there is no version the resolver could
        # co-resolve onto -- a real violation, and the case the unpinned
        # fix must NOT suppress.
        manifest = '[install]\nnodejs.pkg-path = "nodejs_24"\n'
        v = verify({}, manifest, check_catalog_live=True)["violations"]
        self.assertEqual(_rules(v), {"catalog-systems-mismatch"})
        self.assertIn("x86_64-darwin", v[0]["message"])
        self.assertIn("at ANY version", v[0]["message"])

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_unpinned_resolves_to_newest_version_covering_declared_systems(
        self, mock_run, mock_which,
    ):
        # The lemmy/sentry/supabase shape. In this fixture Latest is
        # `postgresql@18.4` and it has no x86_64-darwin build, while
        # 17.10 below it does -- and `flox` does not pin an unpinned
        # package to Latest, it co-resolves onto the newest version
        # covering every declared system. Confirmed by real resolution
        # with flox 1.13.2 on a DIFFERENT pkg-path: `flox init` +
        # `flox install postgresql_18` locks 18.4 while `flox show
        # postgresql_18` reports Latest 18.6. (The two 18.4s are a
        # coincidence of two packages' version schemes and assert
        # opposite things -- this fixture's 18.4 is the row that FAILS
        # to cover, the live 18.6/18.4 pair is where the resolver
        # descended.) Checking the bare `Systems:` header line reported
        # a mismatch on three goldens whose environments really do
        # resolve everywhere they claim.
        manifest = '''
[install]
postgresql.pkg-path = "postgresql"

[options]
systems = ["x86_64-linux", "aarch64-linux", "x86_64-darwin", "aarch64-darwin"]
'''
        result = verify({}, manifest, check_catalog_live=True)
        self.assertEqual(result["violations"], [])
        self.assertEqual(result["catalog_unknown"], [])

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_exact_pin_is_not_rescued_by_an_older_covering_version(
        self, mock_run, mock_which,
    ):
        # The boundary of the descent, stated as a test. The SAME package
        # and the SAME declared systems as the case above, differing only
        # by an EXACT `version`: 18.4 has no x86_64-darwin build and
        # 17.10 does, but 17.10 is not a version "18.4" matches, so it is
        # not a candidate and there is nothing to descend to. The
        # constraint narrows the candidates -- it does not stop the walk.
        # A regression that let this pin fall through to 17.10 would
        # report a manifest as clean that cannot build where it says it
        # can. Contrast test_prefix_pin_descends_within_its_constraint,
        # where the constraint matches more than one version and the
        # descent is exactly what the real resolver does.
        manifest = '''
[install]
postgresql.pkg-path = "postgresql"
postgresql.version = "18.4"

[options]
systems = ["x86_64-linux", "aarch64-linux", "x86_64-darwin", "aarch64-darwin"]
'''
        v = verify({}, manifest, check_catalog_live=True)["violations"]
        self.assertEqual(_rules(v), {"catalog-systems-mismatch"})
        self.assertIn("x86_64-darwin", v[0]["message"])
        self.assertIn("version 18.4", v[0]["message"])

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_pinned_with_own_systems_override_is_unchanged(
        self, mock_run, mock_which,
    ):
        # A pinned entry carrying its own `systems` override is scoped to
        # what that exact version builds, and the entry's override -- not
        # [options].systems -- is what it is checked against. (Not the
        # sentry-golden pattern, despite an earlier comment here saying
        # so: sentry's five `systems` overrides are all on UNPINNED
        # entries and its two pinned entries carry none. That shape is
        # test_unpinned_entry_systems_override_beats_the_manifest_default
        # below.)
        clean = '''
[install]
postgresql.pkg-path = "postgresql"
postgresql.version = "18.4"
postgresql.systems = ["x86_64-linux", "aarch64-linux", "aarch64-darwin"]

[options]
systems = ["x86_64-linux", "aarch64-linux", "x86_64-darwin", "aarch64-darwin"]
'''
        self.assertEqual(verify({}, clean, check_catalog_live=True)["violations"], [])

        # ... and the override still FIRES when it names a system that
        # pinned version genuinely lacks.
        dirty = clean.replace(
            'postgresql.systems = ["x86_64-linux", "aarch64-linux", "aarch64-darwin"]',
            'postgresql.systems = ["x86_64-linux", "x86_64-darwin"]',
        )
        v = verify({}, dirty, check_catalog_live=True)["violations"]
        self.assertEqual(_rules(v), {"catalog-systems-mismatch"})
        self.assertIn("x86_64-darwin", v[0]["message"])

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_unpinned_fires_when_coverage_is_split_across_versions(
        self, mock_run, mock_which,
    ):
        # Every declared system is built SOMEWHERE, but no single version
        # builds them all -- still a real mismatch (an unpinned entry
        # co-resolves onto one version), and it gets its own message,
        # because the fix is a pin or a pkg-group split rather than
        # dropping a platform nothing ever builds.
        manifest = '''
[install]
split.pkg-path = "split-pkg"

[options]
systems = ["x86_64-linux", "aarch64-linux", "x86_64-darwin", "aarch64-darwin"]
'''
        v = verify({}, manifest, check_catalog_live=True)["violations"]
        self.assertEqual(_rules(v), {"catalog-systems-mismatch"})
        # Exact text, the convention `_leaf_msg` states for this file's
        # message helpers -- a substring check on a system name is
        # already satisfied by the "all of {declared}" join earlier in
        # the same string, so it cannot see the half that matters. The
        # "(x86_64-darwin on 2.0.0)" clause is the version row that DOES
        # build the missing system: a fact about rows already read, not a
        # version to pin (by construction none covers everything).
        self.assertEqual(
            v[0]["message"],
            '[install] split.pkg-path = "split-pkg" has no single catalog '
            'version building all of aarch64-darwin, aarch64-linux, '
            'x86_64-darwin, x86_64-linux — the newest (3.0.0) has no build '
            'for x86_64-darwin (x86_64-darwin on 2.0.0) and no older version '
            'covers every declared system, so no single version satisfies '
            'options.systems',
        )
        self.assertNotIn("at ANY version", v[0]["message"])

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_unpinned_is_unknown_when_no_readable_version_covers(
        self, mock_run, mock_which,
    ):
        # murky-pkg's newest version row is readable and does not cover
        # all four; its only other row carries an unrecognized
        # annotation. So nothing readable covers the declared systems and
        # nothing readable rules them out either. Excluded from both
        # "confirmed clean" and "violation" -- an empty reading is only
        # evidence of absence if something actually looked, which is the
        # same rule the rest of this module already applies.
        manifest = '''
[install]
murky.pkg-path = "murky-pkg"

[options]
systems = ["x86_64-linux", "aarch64-linux", "x86_64-darwin", "aarch64-darwin"]
'''
        result = verify({}, manifest, check_catalog_live=True)
        self.assertEqual(result["violations"], [])
        self.assertEqual(len(result["catalog_unknown"]), 1)
        self.assertEqual(result["catalog_unknown"][0]["install_id"], "murky")
        # None, not "3.0.0". 3.0.0's systems parse cleanly -- the row
        # nothing could read is 2.0.0 -- so naming 3.0.0 would label the
        # record with a row that WAS established. What is unknown here is
        # which row applies, and no row is the honest answer to that.
        self.assertIsNone(result["catalog_unknown"][0]["version"])

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_unpinned_resolution_names_the_newest_covering_version(
        self, mock_run, mock_which,
    ):
        # The sentence in this change's own title, asserted directly.
        # Through `verify()` it is invisible: both 17.10 and 16.5 cover
        # all four, so walking oldest-first is equally clean and the
        # chosen version is never emitted on a resolved entry. Only the
        # resolver's own return value distinguishes them.
        show = verify_mod._parse_flox_show(POSTGRESQL_SHOW)
        resolution = verify_mod._resolve_rows(
            verify_mod._catalog_version_rows(show), set(verify_mod.ALL_SYSTEMS),
            incomplete=bool(show["unparsed_rows"]))
        self.assertEqual(resolution["status"], "resolved")
        self.assertEqual(resolution["version"], "17.10")

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_prefix_pin_descends_within_its_constraint(
        self, mock_run, mock_which,
    ):
        # A partial version is a range, not a pin on one row: `flox`
        # narrows the candidate rows to those the constraint matches and
        # then descends among them exactly as it does with no constraint
        # at all. Confirmed by real resolution -- `gcc.version = "15"`
        # locks 15.2.0, not the non-covering 15.3.0 its constraint also
        # matches. prefixed-pkg has the same shape: "18" matches 18.6
        # (no x86_64-darwin) and 18.4 (all four).
        manifest = '''
[install]
prefixed.pkg-path = "prefixed-pkg"
prefixed.version = "18"

[options]
systems = ["x86_64-linux", "aarch64-linux", "x86_64-darwin", "aarch64-darwin"]
'''
        result = verify({}, manifest, check_catalog_live=True)
        self.assertEqual(result["violations"], [])
        self.assertEqual(result["catalog_unknown"], [])

        # ... and the constraint is still a constraint: "17" matches only
        # 17.2, which builds nothing on darwin, so the entry really
        # cannot resolve and the check still says so.
        v = verify({}, manifest.replace('"18"', '"17"'),
                   check_catalog_live=True)["violations"]
        self.assertEqual(_rules(v), {"catalog-systems-mismatch"})
        self.assertIn("version 17.2", v[0]["message"])
        self.assertIn("x86_64-darwin", v[0]["message"])

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_an_unreadable_row_blocks_an_absence_claim(
        self, mock_run, mock_which,
    ):
        # torn-pkg's 2.0.0 row carries trailing text the row regex cannot
        # read. Every row it CAN read is restricted to linux, so dropping
        # the unreadable one would leave a listing that looks complete
        # and supports "no catalog build ... at ANY version" -- an
        # absence claim over a reading that lost a row which might have
        # covered everything.
        manifest = '''
[install]
torn.pkg-path = "torn-pkg"

[options]
systems = ["x86_64-linux", "aarch64-linux", "x86_64-darwin", "aarch64-darwin"]
'''
        result = verify({}, manifest, check_catalog_live=True)
        self.assertEqual(result["violations"], [])
        self.assertEqual(len(result["catalog_unknown"]), 1)
        self.assertEqual(result["catalog_unknown"][0]["install_id"], "torn")
        self.assertEqual(
            verify_mod._parse_flox_show(UNPARSEABLE_ROW_SHOW)["unparsed_rows"], 1)

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_an_unreadable_row_blocks_a_version_missing_claim(
        self, mock_run, mock_which,
    ):
        # The other absence claim on this path, and it is reached before
        # the systems walk: "this version does not exist". torn-pkg's
        # 2.0.0 row is the one the parser could not read, so it is absent
        # from `versions` and a pin to 2.0.0 matches no candidate -- but a
        # row carrying no readable version could be exactly the one asked
        # for, so an incomplete reading cannot establish that it is
        # missing any more than it can establish a missing build.
        manifest = '''
[install]
torn.pkg-path = "torn-pkg"
torn.version = "2.0.0"

[options]
systems = ["x86_64-linux", "aarch64-linux"]
'''
        result = verify({}, manifest, check_catalog_live=True)
        self.assertEqual(result["violations"], [])
        self.assertEqual(len(result["catalog_unknown"]), 1)
        self.assertEqual(result["catalog_unknown"][0]["version"], "2.0.0")

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_an_unreadable_row_suppresses_even_an_exact_pin_finding(
        self, mock_run, mock_which,
    ):
        # The incomplete-reading rule is NOT asymmetric, and the reprieve
        # this test used to assert rested on a step that does not
        # follow. `flox show` emits one row per distinct version, so an
        # unreadable row does carry a DIFFERENT version string than the
        # `3.0.0` that parsed -- but a different version string can
        # still be a candidate, because `_version_constraint_matches`
        # truncates the catalog version to the declared length: "3.0.0"
        # matches "3.0.0.1" exactly as "18.4" matches "18.4.1". Nothing
        # reachable from `flox show`'s text says how many segments this
        # package's scheme uses, so this module cannot establish that a
        # declaration is full-length, which is the only thing that would
        # have made equality with one readable row decisive. Both pins
        # therefore route to `unknown`.
        manifest = '''
[install]
torn.pkg-path = "torn-pkg"
torn.version = "3.0.0"

[options]
systems = ["x86_64-linux", "aarch64-linux", "x86_64-darwin", "aarch64-darwin"]
'''
        result = verify({}, manifest, check_catalog_live=True)
        self.assertEqual(result["violations"], [])
        self.assertEqual(len(result["catalog_unknown"]), 1)
        self.assertEqual(result["catalog_unknown"][0]["version"], "3.0.0")

        # A PREFIX pin behaves identically, for the reason that was
        # already stated: an unreadable row could be 3.1.0.
        result = verify({}, manifest.replace('"3.0.0"', '"3"'),
                        check_catalog_live=True)
        self.assertEqual(result["violations"], [])
        self.assertEqual(len(result["catalog_unknown"]), 1)

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_a_trailing_line_is_not_counted_as_an_unreadable_version(
        self, mock_run, mock_which,
    ):
        # A line with no `@` is not a version row, so it ends the block
        # rather than counting as one this parser could not read. The
        # distinction is worth a test because the consequence is not
        # local: `unparsed_rows` gates whether an absence may be
        # concluded, so counting a footer would silently turn the whole
        # catalog check into `unknown` for the package.
        self.assertEqual(
            verify_mod._parse_flox_show(TRAILING_LINE_SHOW)["unparsed_rows"], 0)
        self.assertEqual(
            sorted(verify_mod._parse_flox_show(TRAILING_LINE_SHOW)["versions"]),
            ["2.0.0", "3.0.0"])

        # ... and the mirror case, which must NOT be treated the same
        # way. An INDENTED line the parser cannot read sits inside the
        # block, so ending the block there would drop every row below it
        # while still reporting the reading complete -- a truncated list
        # supporting a hard "no build at ANY version" claim, which is the
        # failure the whole `unparsed_rows` mechanism exists to prevent.
        # It is counted, and the rows below it are still read.
        interrupted = TRAILING_LINE_SHOW.replace(
            "    chatty-pkg@2.0.0 (aarch64-linux, x86_64-linux only)",
            "    -- older releases --\n"
            "    chatty-pkg@2.0.0 (aarch64-linux, x86_64-linux only)",
        )
        parsed = verify_mod._parse_flox_show(interrupted)
        self.assertEqual(parsed["unparsed_rows"], 1)
        self.assertEqual(sorted(parsed["versions"]), ["2.0.0", "3.0.0"])
        manifest = '''
[install]
chatty.pkg-path = "chatty-pkg"

[options]
systems = ["x86_64-linux", "aarch64-linux", "x86_64-darwin", "aarch64-darwin"]
'''
        result = verify({}, manifest, check_catalog_live=True)
        self.assertEqual(result["catalog_unknown"], [])
        self.assertEqual(_rules(result["violations"]),
                         {"catalog-systems-mismatch"})

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_both_failure_shapes_are_reported_together(
        self, mock_run, mock_which,
    ):
        # A declared set can fail both ways at once, and the two have
        # different fixes -- drop the platform nothing builds, pin or
        # split the group for the one built only on an older row.
        # Reporting the first and stopping fixes half the problem.
        manifest = '''
[install]
doubly.pkg-path = "doubly-pkg"

[options]
systems = ["x86_64-linux", "aarch64-linux", "x86_64-darwin", "aarch64-darwin"]
'''
        v = verify({}, manifest, check_catalog_live=True)["violations"]
        self.assertEqual(_rules(v), {"catalog-systems-mismatch"})
        self.assertEqual(
            v[0]["message"],
            '[install] doubly.pkg-path = "doubly-pkg" has no catalog build '
            'for x86_64-darwin at ANY version (newest is 3.0.0), but '
            'options.systems includes it; and no single catalog version '
            'building all of aarch64-darwin, aarch64-linux, x86_64-darwin, '
            'x86_64-linux — the newest (3.0.0) has no build for '
            'aarch64-darwin (aarch64-darwin on 2.0.0) and no older version '
            'covers every declared system, so no single version satisfies '
            'options.systems',
        )

    def test_only_a_plain_literal_may_be_looked_up_in_the_catalog(self):
        # The gate that decides which entries this module may say a
        # version does not EXIST for. Both directions are asserted
        # because each fails differently and each has bitten:
        #
        #   - too narrow a notion of "literal" drops a real pin out of
        #     the checked set silently -- `python3-3.13.13` is the exact
        #     pin expected/posthog.toml documents as deliberate, since
        #     the catalog's own version string for `python313` carries a
        #     `python3-` prefix, and it has no leading digit;
        #   - too narrow a notion of "range" turns a spec flox accepts
        #     into a false `catalog-version-missing`. Every entry in the
        #     second list below was confirmed accepted and resolved by a
        #     live `flox edit` against postgresql_18, which is why the
        #     rule is a positive literal test with everything else
        #     unknown, rather than an operator table.
        for literal in ("python3-3.13.13", "24.13.0", "14", "18.4",
                        "18rc1", "18beta2"):
            with self.subTest(literal):
                self.assertTrue(verify_mod._is_version_literal(literal))
        for spec in ("^16", ">=2.0", "~17.0", "1.2.*", "<3",
                     "=18.4", "v18.4", "18.4 || 18.3", "18.2 - 18.5",
                     "X", "18.x"):
            with self.subTest(spec):
                self.assertFalse(verify_mod._is_version_literal(spec))
        # `latest` IS a literal by this rule, and that is deliberate:
        # live flox rejects it outright ("No version compatible with
        # 'latest'"), so a typo of this shape should get the loud
        # catalog-version-missing rather than disappear into `unknown`.
        self.assertTrue(verify_mod._is_version_literal("latest"))
        # TOML allows an unquoted `version = 18.4`, which parses as a
        # float -- and `18.10` parses as `18.1`, so text-coercing it
        # would check a version the manifest does not name.
        self.assertFalse(verify_mod._is_version_literal(18.4))
        self.assertFalse(verify_mod._is_version_literal(None))

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_an_empty_version_is_checked_as_an_unpinned_entry(
        self, mock_run, mock_which,
    ):
        # `flox` accepts `version = ""` and treats the entry as
        # unconstrained, so this module has to as well -- routing it to
        # `unknown` because the empty string is not a literal would stop
        # verifying an entry flox itself considers unpinned.
        manifest = '''
[install]
postgresql.pkg-path = "postgresql"
postgresql.version = ""

[options]
systems = ["x86_64-linux", "aarch64-linux", "x86_64-darwin", "aarch64-darwin"]
'''
        result = verify({}, manifest, check_catalog_live=True)
        self.assertEqual(result["violations"], [])
        self.assertEqual(result["catalog_unknown"], [])

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_a_prefixed_catalog_version_string_is_still_checked(
        self, mock_run, mock_which,
    ):
        # The posthog-golden shape end to end: a pin whose catalog
        # version string carries a name prefix stays on the checked path
        # and its systems are really compared, rather than being routed
        # to `unknown` and silently dropped out of verification.
        clean = '''
[install]
python3.pkg-path = "python313"
python3.version = "python3-3.13.13"

[options]
systems = ["x86_64-linux", "aarch64-linux", "x86_64-darwin", "aarch64-darwin"]
'''
        result = verify({}, clean, check_catalog_live=True)
        self.assertEqual(result["violations"], [])
        self.assertEqual(result["catalog_unknown"], [])

        # ... and it is really being checked, not merely passing: the
        # version above it sheds x86_64-darwin and pinning that one
        # fires. A gate that routed either to `unknown` would make both
        # halves of this test silently vacuous.
        dirty = clean.replace("python3-3.13.13", "python3-3.13.14")
        result = verify({}, dirty, check_catalog_live=True)
        self.assertEqual(result["catalog_unknown"], [])
        v = result["violations"]
        self.assertEqual(_rules(v), {"catalog-systems-mismatch"})
        self.assertIn("x86_64-darwin", v[0]["message"])

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_a_version_only_present_as_latest_is_not_reported_missing(
        self, mock_run, mock_which,
    ):
        # flaky-pkg's Latest (9.9.9) is absent from its "Other versions"
        # block. Looking the pin up in the raw `versions` map reported it
        # as not existing; `_catalog_version_rows` inserts Latest, so the
        # candidate filter finds it and the systems check runs on the
        # header's ground truth.
        manifest = '''
[install]
flaky.pkg-path = "flaky-pkg"
flaky.version = "9.9.9"

[options]
systems = ["x86_64-linux", "aarch64-linux"]
'''
        result = verify({}, manifest, check_catalog_live=True)
        self.assertEqual(result["violations"], [])
        self.assertEqual(result["catalog_unknown"], [])

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_single_row_package_resolves_without_an_other_versions_block(
        self, mock_run, mock_which,
    ):
        # `_catalog_version_rows` promises a `Latest:` with no "Other
        # versions" section still yields one row. Driven through
        # `verify()` so the promise is checked where it is relied on.
        manifest = '''
[install]
lonely.pkg-path = "lonely-pkg"

[options]
systems = ["x86_64-linux", "aarch64-linux", "x86_64-darwin", "aarch64-darwin"]
'''
        result = verify({}, manifest, check_catalog_live=True)
        self.assertEqual(result["violations"], [])
        self.assertEqual(result["catalog_unknown"], [])

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_a_discarded_systems_list_is_not_named_as_the_source(
        self, mock_run, mock_which,
    ):
        # `systems = [1]` is not a systems declaration and
        # `_coerce_systems` throws it away, so the mismatch below is
        # about the manifest default -- naming `nodejs.systems` would
        # send the reader to a line whose value was never used and which
        # does not contain x86_64-darwin at all. The malformed-systems
        # finding is what reports that field.
        manifest = '''
[install]
nodejs.pkg-path = "nodejs_24"
nodejs.systems = [1]
'''
        v = verify({}, manifest, check_catalog_live=True)["violations"]
        self.assertEqual(_rules(v),
                         {"malformed-systems", "catalog-systems-mismatch"})
        mismatch = [x for x in v if x["rule"] == "catalog-systems-mismatch"][0]
        self.assertIn("the all-systems default", mismatch["message"])
        self.assertNotIn("nodejs.systems includes it", mismatch["message"])

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_unpinned_entry_systems_override_beats_the_manifest_default(
        self, mock_run, mock_which,
    ):
        # The real golden shape (sentry, supabase): an entry with no
        # version but its own `systems`. Checking the manifest default
        # instead would read x86_64-linux here, which every split-pkg row
        # builds, and report clean.
        manifest = '''
[install]
split.pkg-path = "split-pkg"
split.systems = ["x86_64-linux", "aarch64-linux", "x86_64-darwin", "aarch64-darwin"]

[options]
systems = ["x86_64-linux"]
'''
        v = verify({}, manifest, check_catalog_live=True)["violations"]
        self.assertEqual(_rules(v), {"catalog-systems-mismatch"})
        # The set named in the message is the entry's four, not the
        # manifest default's one, and the message says which line
        # declared it -- the `<id>.systems` branch of `_systems_source`,
        # which every other assertion in this file would pass against the
        # hardcoded "options.systems" string it replaced.
        self.assertIn(
            "all of aarch64-darwin, aarch64-linux, x86_64-darwin, x86_64-linux",
            v[0]["message"])
        self.assertIn("so no single version satisfies split.systems",
                      v[0]["message"])

    def test_catalog_version_rows_shape(self):
        # `_catalog_version_rows` is what every verdict above is computed
        # over, and its two documented joins had no direct test.
        single = verify_mod._catalog_version_rows(
            verify_mod._parse_flox_show(SINGLE_ROW_SHOW))
        self.assertEqual(single, [("1.0.0", set(verify_mod.ALL_SYSTEMS))])

        # Latest absent from the "Other versions" list is inserted first,
        # carrying the header `Systems:` line as its own.
        missing_latest = verify_mod._catalog_version_rows(
            verify_mod._parse_flox_show(MISSING_LATEST_ENTRY_SHOW))
        self.assertEqual(missing_latest[0],
                         ("9.9.9", {"x86_64-linux", "aarch64-linux"}))
        self.assertEqual([v for v, _ in missing_latest], ["9.9.9", "8.0.0"])

        # Nothing readable at all is an empty list, and the walk over it
        # returns unknown rather than vacuously resolving.
        self.assertEqual(verify_mod._catalog_version_rows({}), [])
        self.assertEqual(
            verify_mod._resolve_rows([], {"x86_64-linux"}),
            {"status": "unknown", "version": None, "systems": None,
             "never_built": set(), "elsewhere": {}},
        )

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_never_built_message_is_exact(self, mock_run, mock_which):
        # The other half of `_uncovered_msg`, held to the same exact-text
        # convention as the split-coverage half above. `nodejs_24` has no
        # x86_64-darwin build on any row, and no manifest line declares
        # x86_64-darwin -- the all-systems default does, which the
        # message now says rather than blaming `options.systems`.
        manifest = '[install]\nnodejs.pkg-path = "nodejs_24"\n'
        v = verify({}, manifest, check_catalog_live=True)["violations"]
        self.assertEqual(
            v[0]["message"],
            '[install] nodejs.pkg-path = "nodejs_24" has no catalog build for '
            'x86_64-darwin at ANY version (newest is 24.18.0), but the '
            'all-systems default (no systems declared anywhere) includes it',
        )

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_unpinned_covering_version_above_an_unreadable_one_is_clean(
        self, mock_run, mock_which,
    ):
        # An unreadable row only matters when nothing readable covers
        # the declared set. settled-pkg's newest version builds all four, so
        # the unrecognized annotation on the row below it changes
        # nothing -- reporting "unknown" here would be the check refusing
        # to answer a question it has already answered.
        manifest = '''
[install]
settled.pkg-path = "settled-pkg"

[options]
systems = ["x86_64-linux", "aarch64-linux", "x86_64-darwin", "aarch64-darwin"]
'''
        result = verify({}, manifest, check_catalog_live=True)
        self.assertEqual(result["violations"], [])
        self.assertEqual(result["catalog_unknown"], [])

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
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

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_unpinned_install_uses_header_systems_not_all_systems_default(
        self, mock_run, mock_which,
    ):
        # Regression for the "overclaims" finding: flaky-pkg's Latest
        # (9.9.9) isn't relisted under "Other versions" (only 8.0.0 is),
        # so an unpinned install used to fall through to
        # `.get(show["latest"], set(ALL_SYSTEMS))` and silently claim all
        # four systems. The header `Systems:` line (x86_64-linux,
        # aarch64-linux only) is the ground truth for Latest instead.
        #
        # Asserted through `_resolve_rows` rather than the violation
        # list, because since the unpinned fix that ground truth changes
        # WHICH PAGE is chosen rather than whether one fires: honoring
        # the header rules 9.9.9 out and resolves down to 8.0.0, while
        # the old ALL_SYSTEMS default would have stopped at 9.9.9. Both
        # verdicts are clean, so only the chosen version distinguishes them.
        show = verify_mod._parse_flox_show(MISSING_LATEST_ENTRY_SHOW)
        self.assertEqual(show["latest_systems"], {"x86_64-linux", "aarch64-linux"})

        resolution = verify_mod._resolve_rows(
            verify_mod._catalog_version_rows(show), set(verify_mod.ALL_SYSTEMS),
            incomplete=bool(show["unparsed_rows"]))
        self.assertEqual(resolution["status"], "resolved")
        self.assertEqual(resolution["version"], "8.0.0")

        manifest = '''
[install]
flaky.pkg-path = "flaky-pkg"

[options]
systems = ["x86_64-linux", "aarch64-linux", "x86_64-darwin", "aarch64-darwin"]
'''
        result = verify({}, manifest, check_catalog_live=True)
        self.assertEqual(result["violations"], [])
        self.assertEqual(result["catalog_unknown"], [])

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_unrecognized_annotation_is_unknown_not_asserted_either_way(
        self, mock_run, mock_which,
    ):
        # murky-pkg@2.0.0's "Other versions" parenthetical isn't the
        # recognized "(... only)" form, and 2.0.0 is not Latest, so the
        # header `Systems:` line says nothing about it either. Must be
        # excluded from both "confirmed clean" and "violation" -- never
        # guessed.
        manifest = '''
[install]
murky.pkg-path = "murky-pkg"
murky.version = "2.0.0"

[options]
systems = ["x86_64-linux", "aarch64-linux", "x86_64-darwin", "aarch64-darwin"]
'''
        result = verify({}, manifest, check_catalog_live=True)
        self.assertEqual(result["violations"], [])
        self.assertEqual(len(result["catalog_unknown"]), 1)
        self.assertEqual(result["catalog_unknown"][0]["install_id"], "murky")
        self.assertEqual(result["catalog_unknown"][0]["pkg_path"], "murky-pkg")
        # A PINNED unknown still names the version the reader has to go
        # and check -- unlike the unpinned case below, where no version
        # row was established as the one that applies.
        self.assertEqual(result["catalog_unknown"][0]["version"], "2.0.0")

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_latest_header_settles_a_row_the_same_way_pinned_or_not(
        self, mock_run, mock_which,
    ):
        # weird-pkg's sole version row IS Latest and carries an
        # unrecognized parenthetical, so the row's own text establishes
        # nothing -- but the header `Systems:` line does, and
        # _parse_flox_show documents it as the ground truth for Latest.
        # Both paths now read that one join (`_catalog_version_rows`), so
        # the same catalog text yields the same verdict whether or not
        # the entry names the version. Adding a `version` used to flip a
        # hard violation into an unestablished reading, which is a
        # strange thing for a pin to do.
        unpinned = '''
[install]
weird.pkg-path = "weird-pkg"

[options]
systems = ["x86_64-linux", "aarch64-linux", "x86_64-darwin", "aarch64-darwin"]
'''
        pinned = unpinned.replace(
            'weird.pkg-path = "weird-pkg"',
            'weird.pkg-path = "weird-pkg"\nweird.version = "2.0.0"',
        )
        for label, manifest in (("unpinned", unpinned), ("pinned", pinned)):
            with self.subTest(label):
                result = verify({}, manifest, check_catalog_live=True)
                self.assertEqual(result["catalog_unknown"], [])
                v = result["violations"]
                self.assertEqual(_rules(v), {"catalog-systems-mismatch"})
                self.assertIn("aarch64-darwin", v[0]["message"])
                self.assertIn("x86_64-darwin", v[0]["message"])

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
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
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
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_prefixed_catalog_scheme_requires_the_full_string(self, mock_run, mock_which):
        # Real posthog golden defect, confirmed against live `flox edit`:
        # python313's catalog version is "python3-3.13.13" — pinning the
        # bare "3.13.13" does NOT resolve.
        manifest = '[install]\npy.pkg-path = "python313"\npy.version = "3.13.13"\n'
        v = verify({}, manifest, check_catalog_live=True)["violations"]
        self.assertEqual(_rules(v), {"catalog-version-missing"})

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
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

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_an_unresolvable_version_spec_is_unknown_not_cleared_or_accused(
        self, mock_run, mock_which,
    ):
        # A spec this module cannot reduce to a literal catalog version
        # is the third case, and neither of the other two can answer for
        # it. The version-row walk ignores `version` entirely, so letting
        # one fall into it clears the entry on whichever row happens to
        # cover the declared systems -- here 17.10, which "^16" cannot
        # select, while 18.4 (the only row the range's own major would
        # reach) has no x86_64-darwin build. Falling the other way is no
        # better: every spec below the first four was confirmed accepted
        # and resolved by a live `flox edit`, so reporting
        # catalog-version-missing on one is a false claim about the
        # catalog. Neither a guess biased toward clean nor a guess biased
        # toward a finding; the answer is that it was not established.
        for spec in ("^16", ">=2.0", "~17.0", "1.2.*", "=18.4", "v18.4",
                     "18.4 || 18.3", "18.2 - 18.5", "18.x"):
            with self.subTest(spec):
                manifest = f'''
[install]
pg.pkg-path = "postgresql"
pg.version = "{spec}"

[options]
systems = ["x86_64-linux", "aarch64-linux", "x86_64-darwin", "aarch64-darwin"]
'''
                result = verify({}, manifest, check_catalog_live=True)
                self.assertEqual(result["violations"], [])
                self.assertEqual(len(result["catalog_unknown"]), 1)
                self.assertEqual(result["catalog_unknown"][0]["install_id"], "pg")
                self.assertEqual(result["catalog_unknown"][0]["version"], spec)

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_scalar_options_systems_is_reported_not_raised(self, mock_run, mock_which):
        # AI-485 F4: TOML allows `systems = 4` at [options] (a bare int is
        # not iterable) just as readily as a proper array -- tomllib parses
        # it without error, but the old `set(options.get("systems") or
        # ALL_SYSTEMS)` crashed with TypeError the moment a non-iterable
        # scalar reached it.
        manifest = '[install]\npg.pkg-path = "postgresql"\n\n[options]\nsystems = 4\n'
        v = verify({}, manifest, check_catalog_live=True)["violations"]
        self.assertIn("malformed-systems", _rules(v))
        note = [x for x in v if x["rule"] == "malformed-systems"][0]
        self.assertIn("options", note["message"])

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_scalar_descriptor_systems_is_reported_not_raised(self, mock_run, mock_which):
        # Same fragility, per-install-entry `systems` field instead of
        # [options].systems -- `set(descriptor.get("systems") or
        # default_systems)` hit the identical TypeError.
        manifest = '[install]\npg.pkg-path = "postgresql"\npg.systems = 4\n'
        v = verify({}, manifest, check_catalog_live=True)["violations"]
        self.assertIn("malformed-systems", _rules(v))
        note = [x for x in v if x["rule"] == "malformed-systems"][0]
        self.assertIn("pg", note["message"])

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_empty_list_systems_still_falls_back_silently(self, mock_run, mock_which):
        # An empty [] must keep behaving like "not declared" (the pre-485
        # `or default_systems` behavior) -- not get flagged as malformed.
        manifest = '[install]\npg.pkg-path = "postgresql"\npg.systems = []\n'
        v = verify({}, manifest, check_catalog_live=True)["violations"]
        self.assertNotIn("malformed-systems", _rules(v))


ALL_FOUR = ('[options]\n'
            'systems = ["aarch64-darwin", "aarch64-linux", '
            '"x86_64-darwin", "x86_64-linux"]\n')


class TestCatalogUnknownIsScopedToWhatIsBlocked(unittest.TestCase):
    """What an unresolved reading may still conclude, and what it may not.

    Every case here is one where `unknown` was previously wider than the
    thing that could not be established -- the checker retreating from a
    conclusion it had, or asserting one it did not.
    """

    def setUp(self):
        verify_mod._SHOW_CACHE.clear()

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_a_range_still_reports_a_platform_no_row_builds(
        self, mock_run, mock_which,
    ):
        # A range narrows the candidate rows; it cannot widen them. So
        # `never_built` -- a union over the WHOLE listing -- is the same
        # set for every subset the range could select, and reporting it
        # needs no operator interpreted. Declining to report it cleared a
        # manifest declaring a platform nothing is ever built for, on
        # nothing but the shape of the version string.
        manifest = ('[install]\n'
                    'p.pkg-path = "linux-only-pkg"\n'
                    'p.version = "^3.0"\n' + ALL_FOUR)
        result = verify({}, manifest, check_catalog_live=True)
        self.assertEqual(_rules(result["violations"]),
                         {"catalog-systems-mismatch"})
        # ...and the message is the one an unpinned entry gets, word for
        # word: the claim is over every version, because that is what was
        # actually walked.
        unpinned = verify({}, manifest.replace('p.version = "^3.0"\n', ''),
                          check_catalog_live=True)
        self.assertEqual(result["violations"][0]["message"],
                         unpinned["violations"][0]["message"])
        self.assertIn("at ANY version (newest is 3.0.0)",
                      result["violations"][0]["message"])
        # The co-resolution half IS blocked, and is still recorded.
        self.assertEqual(len(result["catalog_unknown"]), 1)
        self.assertEqual(result["catalog_unknown"][0]["version"], "^3.0")

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_a_range_over_a_covered_package_reports_nothing_hard(
        self, mock_run, mock_which,
    ):
        # The other direction: when some row builds every declared
        # system, `never_built` is empty and there is nothing the union
        # establishes. Only the unknown record.
        manifest = ('[install]\np.pkg-path = "linux-only-pkg"\n'
                    'p.version = "^3.0"\n'
                    '[options]\nsystems = ["x86_64-linux"]\n')
        result = verify({}, manifest, check_catalog_live=True)
        self.assertEqual(result["violations"], [])
        self.assertEqual(len(result["catalog_unknown"]), 1)

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_a_range_never_gets_the_co_resolution_clause(
        self, mock_run, mock_which,
    ):
        # split-pkg builds every declared system somewhere but never all
        # on one row, so `never_built` is empty and the only clause
        # `_uncovered_msg` could offer is the co-resolution one -- which
        # names "the newest" and would be asserting which row applies.
        manifest = ('[install]\np.pkg-path = "split-pkg"\n'
                    'p.version = "^1.0"\n' + ALL_FOUR)
        result = verify({}, manifest, check_catalog_live=True)
        self.assertEqual(result["violations"], [])
        self.assertEqual(len(result["catalog_unknown"]), 1)

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_a_reading_that_recovered_no_row_never_accuses_a_pin(
        self, mock_run, mock_which,
    ):
        # "No versions" and "no versions I could find" are the same empty
        # list and only the first supports "this version does not exist".
        # `unparsed_rows` cannot tell them apart -- it counts INDENTED
        # rows inside an `Other versions:` block, and this listing never
        # produced that block, so it is zero.
        result = verify({}, '[install]\np.pkg-path = "renamed-pkg"\n'
                            'p.version = "1.2.3"\n', check_catalog_live=True)
        self.assertEqual(result["violations"], [])
        self.assertEqual(len(result["catalog_unknown"]), 1)
        self.assertEqual(result["catalog_unknown"][0]["version"], "1.2.3")
        self.assertEqual(
            verify_mod._parse_flox_show(RENAMED_HEADERS_SHOW)["unparsed_rows"],
            0)

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_an_other_versions_block_that_read_nothing_is_incomplete(
        self, mock_run, mock_which,
    ):
        # The second shape, and the one the empty-list guard misses:
        # `Latest:` still parses, so `rows` is non-empty and looks like a
        # single-version package. The `Other versions:` HEADER is the
        # evidence that it is not -- `flox show` does not print it
        # otherwise -- so the shortfall is recorded at the parser.
        parsed = verify_mod._parse_flox_show(UNINDENTED_BLOCK_SHOW)
        self.assertEqual(parsed["unparsed_rows"], 0)
        self.assertTrue(parsed["version_block_unreadable"])
        result = verify({}, '[install]\np.pkg-path = "flat-pkg"\n'
                            'p.version = "18.4"\n', check_catalog_live=True)
        self.assertEqual(result["violations"], [])
        self.assertEqual(len(result["catalog_unknown"]), 1)

    def test_a_readable_block_is_not_flagged_incomplete(self):
        # The guard must not fire on the ordinary listing, or it would
        # turn every catalog check into a permanent `unknown` -- the same
        # failure direction `unparsed_rows` is careful about for footers.
        for text in (POSTGRESQL_SHOW, SINGLE_ROW_SHOW, TRAILING_LINE_SHOW):
            self.assertFalse(
                verify_mod._parse_flox_show(text)["version_block_unreadable"])

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_a_non_string_version_is_recorded_and_serializable(
        self, mock_run, mock_which,
    ):
        # TOML accepts `version = 2020-01-01` and hands back a
        # `datetime.date`. Storing the raw value made `verify.py --json`
        # raise on a manifest the checker had otherwise handled.
        manifest = ('[install]\np.pkg-path = "linux-only-pkg"\n'
                    'p.version = 2020-01-01\n')
        result = verify({}, manifest, check_catalog_live=True)
        self.assertEqual(result["catalog_unknown"][0]["version"], "2020-01-01")
        self.assertIn("not a string", result["catalog_unknown"][0]["reason"])
        json.dumps(result)

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_a_falsy_non_string_version_is_a_declaration(
        self, mock_run, mock_which,
    ):
        # `version = 0` and `version = false` are declarations, and a
        # truthiness gate routed them to the unpinned walk -- checking a
        # manifest against a constraint it does not contain and clearing
        # it. `version = ""` really is unconstrained and keeps that walk.
        for literal in ("0", "false"):
            result = verify({}, '[install]\np.pkg-path = "linux-only-pkg"\n'
                                f'p.version = {literal}\n',
                            check_catalog_live=True)
            self.assertEqual(len(result["catalog_unknown"]), 1, literal)
            self.assertIn("not a string",
                          result["catalog_unknown"][0]["reason"])
        empty = verify({}, '[install]\np.pkg-path = "linux-only-pkg"\n'
                           'p.version = ""\n', check_catalog_live=True)
        self.assertEqual(empty["catalog_unknown"], [])

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_an_empty_version_never_renders_as_a_constraint(
        self, mock_run, mock_which,
    ):
        # `flox` reads `version = ""` as unconstrained, so the message
        # must be the unpinned one -- `at ANY version matching ""` names
        # a constraint nobody wrote.
        manifest = ('[install]\np.pkg-path = "linux-only-pkg"\n'
                    'p.version = ""\n' + ALL_FOUR)
        v = verify({}, manifest, check_catalog_live=True)["violations"]
        self.assertNotIn('matching ""', v[0]["message"])
        self.assertIn("at ANY version (newest is 3.0.0)", v[0]["message"])

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_a_single_row_package_still_says_at_any_version(
        self, mock_run, mock_which,
    ):
        # `len(candidates) > 1` stood in for "a descent happened", so an
        # unpinned entry against a one-row package fell through to the
        # single-row message and lost the wording that says the platform
        # is unavailable everywhere.
        manifest = '[install]\np.pkg-path = "only-one-pkg"\n'
        v = verify({}, manifest, check_catalog_live=True)["violations"]
        self.assertEqual(_rules(v), {"catalog-systems-mismatch"})
        self.assertIn(
            "has no catalog build for aarch64-darwin, x86_64-darwin at ANY "
            "version (newest is 1.0.0)", v[0]["message"])

    def test_both_spellings_of_the_v_prefix_are_non_literal(self):
        # Whether `flox edit` accepts `V18.4` was never established, and
        # only "a literal the catalog does not hold" licenses a hard
        # `catalog-version-missing`. Excluding both spellings routes the
        # unestablished case to `unknown`.
        self.assertFalse(verify_mod._is_version_literal("v18.4"))
        self.assertFalse(verify_mod._is_version_literal("V18.4"))
        self.assertTrue(verify_mod._is_version_literal("18.4"))
        self.assertTrue(verify_mod._is_version_literal("vips-8.15"))

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_a_discarded_systems_declaration_is_named_as_discarded(
        self, mock_run, mock_which,
    ):
        # "no systems declared anywhere" over a malformed `[options]
        # .systems` sends the reader looking for a line that IS there and
        # is wrong -- the mirror image of blaming a default on
        # `[options]`.
        manifest = ('[install]\nnodejs.pkg-path = "nodejs_24"\n'
                    'nodejs.systems = [1]\n[options]\nsystems = [2]\n')
        v = verify({}, manifest, check_catalog_live=True)["violations"]
        mismatch = [x for x in v if x["rule"] == "catalog-systems-mismatch"][0]
        self.assertIn("malformed and discarded", mismatch["message"])
        self.assertNotIn("no systems declared anywhere", mismatch["message"])

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_the_unknown_note_names_the_pkg_path_and_version(
        self, mock_run, mock_which,
    ):
        # The one surface aimed at a human dropped both -- and for an
        # unresolvable spec the version IS the finding.
        manifest = ('[install]\np.pkg-path = "linux-only-pkg"\n'
                    'p.version = "^3.0"\n')
        result = verify({}, manifest, check_catalog_live=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            verify_mod._print_report(result)
        self.assertIn("p (linux-only-pkg@^3.0):", buf.getvalue())


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
        manifest = f'''
[install]
postgresql.pkg-path = "postgresql"

{POSTGRES_SERVICE}'''
        self.assertEqual(_violations({}, manifest), [])


# ---------------------------------------------------------------------------
# heuristic — compiled-extension runtime split from its native build dep
# (ADVISORY) — AI-464
# ---------------------------------------------------------------------------

# cryptography's search_terms carry no leaf-datastore term (unlike
# psycopg2's "postgresql"), so these fixtures isolate THIS heuristic from
# check_leaf_datastore_services -- same discipline as the existing
# test_non_leaf_client_terms_are_ignored fixture above.
CRYPTOGRAPHY_DETECT = {
    "service_clients": [
        {"package": "cryptography", "search_terms": ["pkg-config", "openssl"],
         "source": "requirements.txt"},
    ],
}

RUBY_VIPS_DETECT = {
    "service_clients": [
        {"package": "ruby-vips", "search_terms": ["vips"], "source": "Gemfile"},
    ],
}


class TestNativeGroupCoherence(unittest.TestCase):
    def test_fires_advisory_when_runtime_and_native_dep_split(self):
        manifest = '''
[install]
python3.pkg-path = "python313"
python3.pkg-group = "python313"
openssl.pkg-path = "openssl"
'''
        v = _violations(CRYPTOGRAPHY_DETECT, manifest)
        fired = [x for x in v if x["rule"] == "native-group-split"]
        self.assertEqual(len(fired), 1, v)
        self.assertEqual(fired[0]["severity"], "advisory")

    def test_never_contributes_to_hard_violations(self):
        manifest = '''
[install]
python3.pkg-path = "python313"
python3.pkg-group = "python313"
openssl.pkg-path = "openssl"
'''
        self.assertEqual(_hard(_violations(CRYPTOGRAPHY_DETECT, manifest)), [])

    def test_fires_advisory_for_ruby_gemfile_shape_when_split(self):
        # The Gemfile/Ruby cousin of test_fires_advisory_when_runtime_and_
        # native_dep_split (Python/cryptography) -- same rule, different
        # ecosystem+source. Inverse of test_does_not_fire_when_runtime_and_
        # native_dep_share_a_group below: same packages, split into two
        # groups instead of one, must fire.
        manifest = '''
[install]
ruby.pkg-path = "ruby_4_0"
ruby.pkg-group = "ruby-runtime"
vips.pkg-path = "vips"
vips.pkg-group = "native-libs"
'''
        v = _violations(RUBY_VIPS_DETECT, manifest)
        fired = [x for x in v if x["rule"] == "native-group-split"]
        self.assertEqual(len(fired), 1, v)
        self.assertEqual(fired[0]["severity"], "advisory")

    def test_does_not_fire_when_runtime_and_native_dep_share_a_group(self):
        # Mastodon golden shape: ruby + vips share "runtime-and-native".
        manifest = '''
[install]
ruby.pkg-path = "ruby_4_0"
ruby.pkg-group = "runtime-and-native"
vips.pkg-path = "vips"
vips.pkg-group = "runtime-and-native"
'''
        self.assertEqual(_violations(RUBY_VIPS_DETECT, manifest), [])

    def test_does_not_fire_when_both_default_to_toplevel(self):
        manifest = '''
[install]
python3.pkg-path = "python313"
openssl.pkg-path = "openssl"
'''
        self.assertEqual(_violations(CRYPTOGRAPHY_DETECT, manifest), [])

    def test_does_not_fire_for_pure_runtime_client_no_native_term(self):
        # pg (ruby) implies only postgresql -- no native-link term, so this
        # heuristic has no evidence to cross-check (leaf-datastore-served
        # is the check that owns "is postgres wired", not this one; the
        # [services.postgres] block here satisfies THAT check so only
        # native-group-split's own behavior is under test).
        detect = {"service_clients": [
            {"package": "pg", "search_terms": ["postgresql"], "source": "Gemfile"},
        ]}
        manifest = f'''
[install]
ruby.pkg-path = "ruby_4_0"
ruby.pkg-group = "runtime-and-native"
postgresql.pkg-path = "postgresql"

{POSTGRES_SERVICE}'''
        self.assertEqual(_violations(detect, manifest), [])

    def test_does_not_fire_when_native_dep_not_installed(self):
        # openssl never installed at all -- a coverage gap, not a split.
        manifest = '[install]\npython3.pkg-path = "python313"\n'
        self.assertEqual(_violations(CRYPTOGRAPHY_DETECT, manifest), [])

    def test_does_not_fire_when_runtime_not_installed(self):
        manifest = '[install]\nopenssl.pkg-path = "openssl"\n'
        self.assertEqual(_violations(CRYPTOGRAPHY_DETECT, manifest), [])

    def test_does_not_fire_without_detect_facts(self):
        manifest = '''
[install]
python3.pkg-path = "python313"
python3.pkg-group = "python313"
openssl.pkg-path = "openssl"
'''
        self.assertEqual(_violations({}, manifest), [])


# ---------------------------------------------------------------------------
# heuristic — manifest fragmentation: too many single-package pkg-groups
# (ADVISORY) — AI-464
# ---------------------------------------------------------------------------

def _single_pkg_group_manifest(n):
    lines = ["[install]"]
    for i in range(n):
        lines.append(f'pkg{i}.pkg-path = "pkg{i}"')
        lines.append(f'pkg{i}.pkg-group = "group{i}"')
    return "\n".join(lines) + "\n"


class TestGroupFragmentation(unittest.TestCase):
    def test_fires_advisory_past_the_threshold(self):
        manifest = _single_pkg_group_manifest(verify_mod.MAX_SINGLE_PKG_GROUPS + 1)
        v = _violations({}, manifest)
        fired = [x for x in v if x["rule"] == "group-fragmentation"]
        self.assertEqual(len(fired), 1, v)
        self.assertEqual(fired[0]["severity"], "advisory")

    def test_never_contributes_to_hard_violations(self):
        manifest = _single_pkg_group_manifest(verify_mod.MAX_SINGLE_PKG_GROUPS + 1)
        self.assertEqual(_hard(_violations({}, manifest)), [])

    def test_does_not_fire_at_the_threshold(self):
        # plausible's current shape: exactly MAX_SINGLE_PKG_GROUPS (2)
        # single-package groups must NOT trip this heuristic.
        manifest = _single_pkg_group_manifest(verify_mod.MAX_SINGLE_PKG_GROUPS)
        self.assertEqual(_violations({}, manifest), [])

    def test_does_not_fire_for_shared_groups(self):
        # sentry golden shape: 5 packages sharing ONE group is economical,
        # not fragmented -- must not fire regardless of package count.
        lines = ["[install]"]
        for i in range(10):
            lines.append(f'pkg{i}.pkg-path = "pkg{i}"')
            lines.append(f'pkg{i}.pkg-group = "shared"')
        manifest = "\n".join(lines) + "\n"
        self.assertEqual(_violations({}, manifest), [])

    def test_does_not_fire_for_toplevel_only_manifest(self):
        manifest = '[install]\na.pkg-path = "a"\nb.pkg-path = "b"\n'
        self.assertEqual(_violations({}, manifest), [])


# ---------------------------------------------------------------------------
# AI-485: malformed-but-syntactically-valid TOML/JSON shapes. An agent-
# generated manifest.toml or a stale/corrupted detect.json can be perfectly
# valid TOML/JSON while carrying the wrong VALUE TYPE for a field every
# check here assumes is a specific shape (a table, a list, a string).
# tomllib/json.loads parse all of these without error -- these fragility
# classes (F1-F5) are what used to reach the actual TypeError/AttributeError
# instead of a reported finding. Same lineage as the AI-463 KeyError fix.
# ---------------------------------------------------------------------------

class TestMalformedManifestSections(unittest.TestCase):
    """F1/F2: a top-level manifest section TOML only allows Flox to treat
    as a table can itself be declared as a scalar or an array."""

    def test_scalar_install_section_is_reported_not_raised(self):
        manifest = 'install = "python"\n'
        result = verify({}, manifest, check_catalog_live=False)
        v = result["violations"]
        self.assertIn("malformed-section", _rules(v))
        match = [x for x in v if x["rule"] == "malformed-section"][0]
        self.assertEqual(match["severity"], "hard")
        self.assertIn("install", match["message"])

    def test_array_vars_section_is_reported_not_raised(self):
        manifest = 'vars = ["FOO", "BAR"]\n\n[install]\npython3.pkg-path = "python312"\n'
        result = verify({}, manifest, check_catalog_live=False)
        v = result["violations"]
        self.assertIn("malformed-section", _rules(v))
        match = [x for x in v if x["rule"] == "malformed-section"][0]
        self.assertIn("vars", match["message"])

    def test_well_formed_sections_never_trip_this_rule(self):
        self.assertNotIn("malformed-section", _rules(_violations({}, AI449_GOOD_MANIFEST)))


class TestNonStrPkgPath(unittest.TestCase):
    """F3: `pkg-path` can be declared as a non-string, non-array scalar
    (an int, a bool, a nested table) -- valid TOML, but `_pkg_path_str`
    used to hand the raw value straight through, which later crashed
    `re.Pattern.match` (needs str/bytes) wherever a pkg-path is pattern-
    matched against a runtime language."""

    def test_non_str_pkg_path_does_not_crash_runtime_check(self):
        manifest = '[install]\nfoo.pkg-path = 123\n'
        detect_facts = {"runtimes": [
            {"language": "python", "version": "3.12", "source": "test"},
        ]}
        v = _violations(detect_facts, manifest)
        # A garbage pkg-path is treated the same as no pkg-path at all --
        # python still reads as undeclared, which is the accurate finding.
        self.assertIn("runtime-not-installed", _rules(v))

    def test_non_str_pkg_path_is_excluded_from_catalog_checks(self):
        self.assertIsNone(verify_mod._pkg_path_str({"pkg-path": 123}))
        self.assertIsNone(verify_mod._pkg_path_str({"pkg-path": True}))
        self.assertIsNone(verify_mod._pkg_path_str({"pkg-path": {"nested": "table"}}))

    def test_malformed_pkg_path_fires_hard(self):
        # PR #66 review I1: F3 must have HARD-finding parity with F4
        # (malformed-systems) -- reviewer's exact repro. Without
        # detect facts or a catalog check, a garbage pkg-path used to
        # read as a fully clean manifest (zero violations), which is
        # the same vacuous-green failure malformed-section/
        # malformed-systems already guard against, one level down.
        manifest = '[install]\nfoo.pkg-path = 123\nbar.pkg-path = true\n'
        v = verify({}, manifest, check_catalog_live=False)["violations"]
        matches = [x for x in v if x["rule"] == "malformed-pkg-path"]
        self.assertEqual(len(matches), 2, v)
        for m in matches:
            self.assertEqual(m["severity"], "hard")

    def test_well_formed_pkg_paths_never_trip_this_rule(self):
        manifest = '[install]\na.pkg-path = "a"\nb.pkg-path = ["python310Packages", "pip"]\n'
        v = verify({}, manifest, check_catalog_live=False)["violations"]
        self.assertNotIn("malformed-pkg-path", _rules(v))

    def test_absent_pkg_path_never_trips_this_rule(self):
        manifest = '[install]\na.priority = 1\n'
        v = verify({}, manifest, check_catalog_live=False)["violations"]
        self.assertNotIn("malformed-pkg-path", _rules(v))


class TestMalformedDetectFacts(unittest.TestCase):
    """F5: detect.json (produced by detect.py, consumed by verify.py) can
    reach verify() with the wrong shape -- not an object at all, or a
    known list-shaped field (runtimes/service_clients/services/
    native_hints) typed as something else. verify()'s own docstring
    already treats `detect=None`/`{}` as "nothing to cross-check, degrade
    to no-ops" -- a malformed detect blob degrades the same way rather
    than crashing, instead of inventing new violation semantics for it."""

    def test_detect_facts_not_a_dict_degrades_like_no_detect_facts(self):
        malformed = verify(["oops"], AI449_BAD_MANIFEST, check_catalog_live=False)
        baseline = verify(None, AI449_BAD_MANIFEST, check_catalog_live=False)
        self.assertEqual(malformed["violations"], baseline["violations"])

    def test_detect_runtimes_field_wrong_type_does_not_raise(self):
        v = _violations({"runtimes": "python"}, AI449_GOOD_MANIFEST)
        self.assertEqual(_hard(v), [])
        self.assertIn("malformed-detect-facts", _rules(v))

    def test_detect_service_clients_field_wrong_type_does_not_raise(self):
        v = _violations({"service_clients": {"not": "a-list"}}, AI449_GOOD_MANIFEST)
        self.assertEqual(_hard(v), [])
        self.assertIn("malformed-detect-facts", _rules(v))

    def test_detect_native_hints_field_wrong_type_does_not_raise(self):
        manifest = '[install]\nvips.pkg-path = "vips"\n'
        v = _violations({"native_hints": "vips"}, manifest)
        self.assertEqual(_hard(v), [])
        self.assertIn("malformed-detect-facts", _rules(v))

    def test_partial_malformation_gets_advisory_not_silent(self):
        # PR #66 review M1: reviewer's exact repro -- runtimes valid,
        # service_clients garbage. A partially-malformed detect blob is
        # NOT the same as detect=None (that whole-blob case is a
        # documented no-op); silently emptying only the bad field could
        # quietly weaken a HARD cross-check (e.g. leaf-datastore-not-
        # served) with an unsurfaced gap, so it gets an ADVISORY instead.
        detect_facts = {
            "runtimes": [{"language": "python", "version": "3.12", "source": "test"}],
            "service_clients": "garbage-not-a-list",
        }
        v = _violations(detect_facts, AI449_GOOD_MANIFEST)
        matches = [x for x in v if x["rule"] == "malformed-detect-facts"]
        self.assertEqual(len(matches), 1, v)
        self.assertEqual(matches[0]["severity"], "advisory")
        self.assertIn("service_clients", matches[0]["message"])
        # A field the checker never asked for is unaffected -- only
        # PRESENT-but-wrong-typed fields are flagged.
        self.assertNotIn("runtimes", matches[0]["message"])

    def test_absent_detect_fields_never_trip_this_rule(self):
        # An absent field is the documented detect=None/{} no-op case,
        # not a malformation -- must not be flagged.
        v = _violations({"runtimes": []}, AI449_GOOD_MANIFEST)
        self.assertNotIn("malformed-detect-facts", _rules(v))

    def test_well_formed_detect_facts_never_trip_this_rule(self):
        v = _violations(AI449_DETECT, AI449_GOOD_MANIFEST)
        self.assertNotIn("malformed-detect-facts", _rules(v))


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


# ---------------------------------------------------------------------------
# CLI layer (main()) — the skill's Phase 3c Step 4 depends on exit-code
# semantics (exit 0 -> proceed, non-zero -> stop) and the stdin '-' /
# --no-catalog fallbacks it documents. Zero coverage here previously meant
# a regression that inverted the exit code or broke a flag would pass every
# test that only calls verify() directly. (Minor review finding.)
# ---------------------------------------------------------------------------

class TestMainCLI(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.detect_path = self.tmp / "detect.json"
        self.manifest_path = self.tmp / "manifest.toml"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _write(self, detect_obj, manifest_text):
        self.detect_path.write_text(json.dumps(detect_obj))
        self.manifest_path.write_text(manifest_text)

    def test_exit_0_on_clean_manifest(self):
        self._write({}, AI449_GOOD_MANIFEST)
        code = verify_mod.main([
            "verify.py", str(self.detect_path), str(self.manifest_path), "--no-catalog",
        ])
        self.assertEqual(code, 0)

    def test_exit_1_on_hard_violation(self):
        self._write(AI449_DETECT, AI449_BAD_MANIFEST)
        code = verify_mod.main([
            "verify.py", str(self.detect_path), str(self.manifest_path), "--no-catalog",
        ])
        self.assertEqual(code, 1)

    def test_exit_2_on_missing_detect_json_file(self):
        self.manifest_path.write_text(AI449_GOOD_MANIFEST)
        code = verify_mod.main([
            "verify.py", str(self.tmp / "does-not-exist.json"),
            str(self.manifest_path), "--no-catalog",
        ])
        self.assertEqual(code, 2)

    def test_exit_2_on_missing_manifest_file(self):
        self.detect_path.write_text("{}")
        code = verify_mod.main([
            "verify.py", str(self.detect_path),
            str(self.tmp / "does-not-exist.toml"), "--no-catalog",
        ])
        self.assertEqual(code, 2)

    def test_exit_2_on_invalid_detect_json(self):
        self.detect_path.write_text("not valid json {{{")
        self.manifest_path.write_text(AI449_GOOD_MANIFEST)
        code = verify_mod.main([
            "verify.py", str(self.detect_path), str(self.manifest_path), "--no-catalog",
        ])
        self.assertEqual(code, 2)

    def test_stdin_dash_reads_detect_json_from_stdin(self):
        self.manifest_path.write_text(AI449_GOOD_MANIFEST)
        with patch("sys.stdin", io.StringIO(json.dumps({}))):
            code = verify_mod.main([
                "verify.py", "-", str(self.manifest_path), "--no-catalog",
            ])
        self.assertEqual(code, 0)

    def test_empty_stdin_treated_as_empty_facts_not_an_error(self):
        self.manifest_path.write_text(AI449_GOOD_MANIFEST)
        with patch("sys.stdin", io.StringIO("")):
            code = verify_mod.main([
                "verify.py", "-", str(self.manifest_path), "--no-catalog",
            ])
        self.assertEqual(code, 0)

    def test_json_flag_emits_parseable_json_with_expected_keys(self):
        self._write(AI449_DETECT, AI449_BAD_MANIFEST)
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            verify_mod.main([
                "verify.py", str(self.detect_path), str(self.manifest_path),
                "--no-catalog", "--json",
            ])
        payload = json.loads(buf.getvalue())
        self.assertIn("violations", payload)
        self.assertIn("catalog_checked", payload)
        self.assertIn("catalog_unknown", payload)
        self.assertIn("_meta", payload)
        self.assertEqual(len(payload["violations"]), 3)

    def test_no_catalog_flag_never_invokes_flox_show(self):
        self._write({}, AI449_GOOD_MANIFEST)
        with patch(f"{_MODULE_KEY}._run_show_command") as mock_run:
            verify_mod.main([
                "verify.py", str(self.detect_path), str(self.manifest_path), "--no-catalog",
            ])
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# AI-467 forensic reproduction: the real_world posthog x5 re-run HARD-fired
# `leaf-datastore-not-served` for pymysql->mariadb and pymongo->mongodb in
# ALL FIVE reps -- including against PostHog's own upstream hand-maintained
# manifest -- while the real_world registry expects neither service for
# posthog. Runs the REAL detect.py against fixtures/posthog-shaped/ (a
# pyproject.toml with the exact dependency list confirmed live against
# PostHog @ 55525a19f353), the same integration boundary
# TestAI466LemmyForensicReproduction proved Hole 1/2/3 at.
# ---------------------------------------------------------------------------

class TestAI467PosthogForensicReproduction(unittest.TestCase):
    def _detect(self):
        return detect.scan(str(SUITE / "fixtures" / "posthog-shaped"))

    def test_posthog_own_manifest_shape_produces_no_leaf_datastore_hard_violation(self):
        # PostHog's actual needs: postgres + redis wired, nothing for
        # mariadb/mongodb anywhere -- matching the real_world registry's
        # expected_services (postgres, redis, clickhouse; no mariadb/
        # mongodb) and what verify.py HARD-fired incorrectly before this
        # fix.
        manifest = f'''
schema-version = "1.13.0"

[install]
python3.pkg-path = "python313"
postgresql.pkg-path = "postgresql_15"
redis.pkg-path = "redis"

{POSTGRES_SERVICE}
[services.redis]
command = "redis-server"
'''
        detected = self._detect()
        v = _hard(_violations(detected, manifest))
        rules = {x["rule"] for x in v}
        self.assertNotIn("leaf-datastore-not-served", rules)

    def test_uncorroborated_mariadb_mongodb_evidence_is_advisory_not_silent(self):
        # The downgrade must still be VISIBLE (as ADVISORY), not silently
        # dropped -- confirm whether mariadb/mongodb is a runtime need is
        # exactly the judgment call this leaves for a human/agent to make.
        manifest = f'''
[install]
postgresql.pkg-path = "postgresql_15"

{POSTGRES_SERVICE}'''
        detected = self._detect()
        v = _violations(detected, manifest)
        advisory_kinds = {
            x["rule"] for x in v
            if x["rule"] == "leaf-datastore-not-served" and x["severity"] == "advisory"
        }
        self.assertIn("leaf-datastore-not-served", advisory_kinds)

    def test_if_posthog_did_wire_mariadb_it_would_still_hard_fire_when_corroborated(self):
        # The invariant isn't neutered -- a genuine mariadb need (a [vars]
        # endpoint) alongside the same pymysql evidence still HARD-fires.
        manifest = (
            '[vars]\nMARIADB_URL = "mysql://u:p@localhost:3306/app"\n'
        )
        detected = self._detect()
        v = _hard(_violations(detected, manifest))
        rules = {x["rule"] for x in v}
        self.assertIn("leaf-datastore-not-served", rules)


if __name__ == "__main__":
    unittest.main()
