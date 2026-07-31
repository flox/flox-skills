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


def _mock_show(pkg_path, flox_bin, timeout):
    if pkg_path == "postgresql":
        return _FakeProc(stdout=POSTGRESQL_SHOW)
    if pkg_path == "nodejs_24":
        return _FakeProc(stdout=NODEJS_24_SHOW)
    if pkg_path == "python313":
        return _FakeProc(stdout=PYTHON313_SHOW)
    if pkg_path == "flaky-pkg":
        return _FakeProc(stdout=MISSING_LATEST_ENTRY_SHOW)
    if pkg_path == "weird-pkg":
        return _FakeProc(stdout=UNRECOGNIZED_ANNOTATION_SHOW)
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
    def test_unpinned_version_checks_against_latest(self, mock_run, mock_which):
        # nodejs_24 with no .version pinned -> latest (24.18.0), which is
        # missing x86_64-darwin; default systems (no [options]) = all four.
        manifest = '[install]\nnodejs.pkg-path = "nodejs_24"\n'
        v = verify({}, manifest, check_catalog_live=True)["violations"]
        self.assertEqual(_rules(v), {"catalog-systems-mismatch"})

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
        # aarch64-linux only) is now the ground truth instead.
        manifest = '''
[install]
flaky.pkg-path = "flaky-pkg"

[options]
systems = ["x86_64-linux", "aarch64-linux", "x86_64-darwin", "aarch64-darwin"]
'''
        result = verify({}, manifest, check_catalog_live=True)
        v = result["violations"]
        self.assertEqual(_rules(v), {"catalog-systems-mismatch"})
        self.assertIn("x86_64-darwin", v[0]["message"])
        self.assertIn("aarch64-darwin", v[0]["message"])
        self.assertEqual(result["catalog_unknown"], [])

    @patch("shutil.which", return_value="/usr/bin/flox")
    @patch(f"{_MODULE_KEY}._run_show_command", side_effect=_mock_show)
    def test_unrecognized_annotation_is_unknown_not_asserted_either_way(
        self, mock_run, mock_which,
    ):
        # weird-pkg@2.0.0's "Other versions" parenthetical isn't the
        # recognized "(... only)" form. Must be excluded from both
        # "confirmed clean" and "violation" -- never guessed.
        manifest = '''
[install]
weird.pkg-path = "weird-pkg"
weird.version = "2.0.0"

[options]
systems = ["x86_64-linux", "aarch64-linux", "x86_64-darwin", "aarch64-darwin"]
'''
        result = verify({}, manifest, check_catalog_live=True)
        self.assertEqual(result["violations"], [])
        self.assertEqual(len(result["catalog_unknown"]), 1)
        self.assertEqual(result["catalog_unknown"][0]["install_id"], "weird")
        self.assertEqual(result["catalog_unknown"][0]["pkg_path"], "weird-pkg")

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
