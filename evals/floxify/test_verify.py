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
VERIFY = HERE.parent.parent / "flox-plugin" / "skills" / "floxify" / "scripts" / "verify.py"
DETECT = HERE.parent.parent / "flox-plugin" / "skills" / "floxify" / "scripts" / "detect.py"

# Unique sys.modules key so @patch("...") resolves THIS file's instance —
# test_golden_lint.py loads the same verify.py under its OWN unique key.
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
        return detect.scan(str(HERE / "fixtures" / "lemmy-shaped"))

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
        self.assertEqual(_rules(v), {"hook-mutates-tree"})

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
        self.assertEqual(_rules(v), {"hook-mutates-tree"})

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


if __name__ == "__main__":
    unittest.main()
