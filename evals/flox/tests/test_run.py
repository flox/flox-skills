#!/usr/bin/env python3
"""Unit tests for run.py's deterministic pieces.

The agent + judge calls are integration-only. Everything here is pure logic
over mocked subprocesses — no claude, no network, no API spend.

    python3 -m unittest tests.test_run -v
"""
import json
import subprocess
import unittest
from unittest.mock import MagicMock, patch

import run


# A realistic `claude -p --output-format json` envelope. The harness has always
# read exactly one field of this (`result`) and dropped the rest — including the
# cost it is handed on every single call (AI-459).
CLAUDE_JSON = {
    "result": "here is your manifest",
    "total_cost_usd": 1.2717,
    "duration_ms": 406123,
    "num_turns": 14,
    "usage": {
        "input_tokens": 2,
        "cache_creation_input_tokens": 32775,
        "cache_read_input_tokens": 957709,
        "output_tokens": 18594,
    },
}


class TestParseMeta(unittest.TestCase):
    """Cost/usage extraction from the claude envelope."""

    def test_extracts_cost_usage_duration(self):
        meta = run._parse_meta(CLAUDE_JSON)
        self.assertAlmostEqual(meta["cost_usd"], 1.2717)
        self.assertEqual(meta["duration_ms"], 406123)
        self.assertEqual(meta["usage"]["output_tokens"], 18594)
        self.assertEqual(meta["usage"]["cache_read_input_tokens"], 957709)

    def test_missing_fields_do_not_raise(self):
        # Never let a cost-accounting detail break a run.
        meta = run._parse_meta({"result": "x"})
        self.assertEqual(meta["cost_usd"], 0.0)
        self.assertEqual(meta["usage"], {})

    def test_non_numeric_cost_is_zero_not_crash(self):
        meta = run._parse_meta({"result": "x", "total_cost_usd": None})
        self.assertEqual(meta["cost_usd"], 0.0)


class TestRunClaudeReturnsMeta(unittest.TestCase):
    @patch("run.subprocess.run")
    def test_success_returns_meta(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(CLAUDE_JSON), stderr=""
        )
        result, err, meta = run.run_claude("p", "skills", None)
        self.assertIsNone(err)
        self.assertEqual(result, "here is your manifest")
        self.assertAlmostEqual(meta["cost_usd"], 1.2717)

    @patch("run.subprocess.run")
    def test_error_returns_zero_cost_meta_not_none(self, mock_run):
        # A failed call may still have burned tokens; and callers must be able
        # to sum unconditionally without a None check.
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=1)
        result, err, meta = run.run_claude("p", "skills", None, retries=1)
        self.assertIsNone(result)
        self.assertEqual(err, "TIMEOUT")
        self.assertEqual(meta["cost_usd"], 0.0)


class TestProcessTaskRecordsCost(unittest.TestCase):
    """A task's cost is agent + judge — the judge is half of every run's calls
    and has never been separately visible."""

    TASK = {"id": "t1", "area": "env", "tier": "should",
            "prompt": "p", "rubric": "r", "checks": []}

    @patch("run.subprocess.run")
    def test_task_records_agent_and_judge_cost_split(self, mock_run):
        judge_json = dict(CLAUDE_JSON)
        judge_json["result"] = '{"score": 5, "correct": true, "issues": []}'
        judge_json["total_cost_usd"] = 0.2
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps(CLAUDE_JSON), stderr=""),
            MagicMock(returncode=0, stdout=json.dumps(judge_json), stderr=""),
        ]
        r = run.process_task(self.TASK, "skills", None)
        self.assertAlmostEqual(r["cost"]["agent_usd"], 1.2717)
        self.assertAlmostEqual(r["cost"]["judge_usd"], 0.2)
        self.assertAlmostEqual(r["cost"]["total_usd"], 1.4717)


class TestAutoStartChecks(unittest.TestCase):
    """`services.auto-start` hard-checks (AI-503).

    Both failure modes below produce a manifest flox refuses to load, so a
    check that merely greps for the string would pass a broken answer.
    """

    def _answer(self, toml):
        return f"Add this to your manifest:\n\n```toml\n{toml}```\n"

    def test_accepts_key_on_the_services_table(self):
        self.assertTrue(run._sets_auto_start(self._answer(
            'schema-version = "1.12.0"\n\n[services]\nauto-start = true\n'
            'web.command = "python3 -m http.server"\n'
        )))

    def test_accepts_top_level_dotted_form(self):
        self.assertTrue(run._sets_auto_start(self._answer(
            'schema-version = "1.12.0"\nservices.auto-start = true\n\n'
            '[services.web]\ncommand = "python3 -m http.server"\n'
        )))

    def test_rejects_key_inside_a_service(self):
        # flox: unknown field `auto-start`, expected one of `command`, `vars`, ...
        self.assertFalse(run._sets_auto_start(self._answer(
            'schema-version = "1.12.0"\n\n[services.web]\n'
            'command = "python3 -m http.server"\nauto-start = true\n'
        )))

    def test_rejects_prose_only_mention(self):
        self.assertFalse(run._sets_auto_start(
            "You can set auto-start = true somewhere in there."
        ))

    def test_does_not_borrow_a_services_header_from_another_block(self):
        answer = ('```toml\n[services]\nweb.command = "x"\n```\n'
                  '```toml\n[services.web]\nauto-start = true\n```\n')
        self.assertFalse(run._sets_auto_start(answer))

    def test_accepts_manifest_preceded_by_a_bash_block(self):
        # ANSWER_SUFFIX asks for the manifest *and* the commands, so a mixed
        # answer is the expected shape and block order varies run to run. The
        # old fence regex matched an empty info string, so the closing ```
        # of the bash block read as an opening one and the manifest was lost —
        # a red gate on a correct answer.
        answer = ('```bash\nflox edit\n```\n\n'
                  '```toml\nschema-version = "1.12.0"\n\n[services]\n'
                  'auto-start = true\nweb.command = "x"\n```\n')
        self.assertTrue(run._sets_auto_start(answer))
        self.assertTrue(run.CHECKS["auto_start_schema_version"](answer))

    def test_rejects_auto_start_inside_a_multiline_command_body(self):
        # tomllib: `services.auto-start` does not exist here — the text is
        # part of `web.command`. The old line scanner had no '''/""" state.
        self.assertFalse(run._sets_auto_start(self._answer(
            'schema-version = "1.12.0"\n\n[services]\nweb.command = \'\'\'\n'
            'auto-start = true\nsleep 100\n\'\'\'\n'
        )))

    def test_accepts_key_after_a_bracket_leading_shell_line(self):
        # `[ -d node_modules ] || npm ci` inside a command body set the
        # scanner's current table to `-d node_modules`, hiding the real key
        # that followed — a manifest tomllib confirms is correct.
        self.assertTrue(run._sets_auto_start(self._answer(
            'schema-version = "1.12.0"\n\n[services]\nweb.command = \'\'\'\n'
            '[ -d node_modules ] || npm ci\nnpm start\n\'\'\'\n'
            'auto-start = true\n'
        )))


class TestAutoStartSchemaVersion(unittest.TestCase):
    """The schema half of the `services.auto-start` gate (AI-503).

    All three facts — the key is set, the schema is new enough, no `version = 1`
    survives — must hold in the SAME fenced manifest. Asserting them across the
    whole answer certified manifests the check never inspected.
    """

    ok = staticmethod(lambda a: run.CHECKS["auto_start_schema_version"](a))

    def _manifest(self, version_line):
        return (f'```toml\n{version_line}\n\n[services]\nauto-start = true\n'
                'web.command = "python3 -m http.server"\n```\n')

    def test_accepts_1_12_and_newer(self):
        for v in ('"1.12.0"', '"1.13.0"', '"1.20.0"', '"1.100.0"', '"2.0.0"'):
            with self.subTest(v=v):
                self.assertTrue(self.ok(self._manifest(f"schema-version = {v}")))

    def test_accepts_toml_literal_string_form(self):
        # `'1.12.0'` is an ordinary TOML string; the old substring probe only
        # matched the double-quoted spelling.
        self.assertTrue(self.ok(self._manifest("schema-version = '1.12.0'")))

    def test_rejects_older_schema(self):
        for v in ('"1.11.0"', '"1.10.0"'):
            with self.subTest(v=v):
                self.assertFalse(self.ok(self._manifest(f"schema-version = {v}")))

    def test_rejects_legacy_version_line(self):
        self.assertFalse(self.ok(self._manifest("version = 1")))

    def test_rejects_malformed_versions(self):
        # A substring probe accepted all of these.
        for v in ('"1.12garbage"', '"1.29-nonsense"', '"1.12"', '"garbage"'):
            with self.subTest(v=v):
                self.assertFalse(self.ok(self._manifest(f"schema-version = {v}")))

    def test_rejects_prose_schema_over_a_version_1_manifest(self):
        # The RED this task exists to catch: "knows the key exists and even
        # places it correctly, but keeps `version = 1`". flox rejects it with
        # `invalid type: boolean true, expected struct ServiceDescriptor`.
        answer = ('You need schema-version = "1.12.0" for this.\n\n'
                  '```toml\nversion = 1\n\n[services]\nauto-start = true\n'
                  'web.command = "x"\n```\n')
        self.assertTrue(run._sets_auto_start(answer))  # placement is right
        self.assertFalse(self.ok(answer))              # ... but it cannot load

    def test_rejects_schema_declared_in_a_different_block(self):
        answer = ('```toml\nschema-version = "1.12.0"\n```\n\n'
                  '```toml\nversion = 1\n\n[services]\nauto-start = true\n```\n')
        self.assertFalse(self.ok(answer))

    def test_rejects_manifest_carrying_both_version_keys(self):
        # flox rejects a manifest with both spellings.
        answer = ('```toml\nversion = 1\nschema-version = "1.12.0"\n\n'
                  '[services]\nauto-start = true\n```\n')
        self.assertFalse(self.ok(answer))


class TestBuildSandboxChecks(unittest.TestCase):
    """`sandbox = "warn"|"enforce"` / `sandbox-allow` hard-checks (AI-503).

    Both fields arrived with schema 1.13.0; under `version = 1` flox rejects
    the manifest with ``unknown variant `warn`, expected `off` or `pure` ``.
    """

    def _answer(self, toml):
        return f"```toml\n{toml}```\n"

    ENFORCE = ('[build.app]\ncommand = "make"\nsandbox = "enforce"\n'
               'sandbox-allow = [ "~/.npm/**" ]\n')

    def test_accepts_enforce_with_schema_1_13(self):
        a = self._answer(f'schema-version = "1.13.0"\n\n{self.ENFORCE}')
        self.assertTrue(run.CHECKS["sets_build_sandbox_mode"](a))
        self.assertTrue(run.CHECKS["build_sandbox_schema_version"](a))

    def test_rejects_gated_field_under_version_1(self):
        a = self._answer(f"version = 1\n\n{self.ENFORCE}")
        self.assertTrue(run.CHECKS["sets_build_sandbox_mode"](a))
        self.assertFalse(run.CHECKS["build_sandbox_schema_version"](a))

    def test_rejects_schema_below_1_13(self):
        a = self._answer(f'schema-version = "1.12.0"\n\n{self.ENFORCE}')
        self.assertFalse(run.CHECKS["build_sandbox_schema_version"](a))

    def test_ungated_sandbox_values_do_not_count(self):
        for mode in ('"off"', '"pure"'):
            with self.subTest(mode=mode):
                a = self._answer(f'version = 1\n\n[build.app]\ncommand = "make"\n'
                                 f"sandbox = {mode}\n")
                self.assertFalse(run.CHECKS["sets_build_sandbox_mode"](a))

    def test_boolean_sandbox_does_not_count(self):
        # `sandbox = true` is the habit the skill exists to break.
        a = self._answer('schema-version = "1.13.0"\n\n[build.app]\n'
                         'command = "make"\nsandbox = true\n')
        self.assertFalse(run.CHECKS["sets_build_sandbox_mode"](a))

    def test_prose_only_mention_does_not_count(self):
        self.assertFalse(run.CHECKS["sets_build_sandbox_mode"](
            'Set sandbox = "enforce" in your build section.'
        ))


class TestFencedManifestExtraction(unittest.TestCase):
    """Fence handling is delegated to `skill_toml_lint.extract_blocks`."""

    def test_skips_non_toml_fences(self):
        text = '```bash\nflox install hello\n```\n\n```toml\nversion = 1\n```\n'
        self.assertEqual([b.body for b in run._fenced_manifests(text)], ["version = 1\n"])

    def test_unterminated_fence_does_not_lose_earlier_blocks(self):
        text = '```toml\nversion = 1\n```\n\n```toml\n[services]\nauto-start = true\n'
        self.assertEqual(len(run._fenced_manifests(text)), 2)

    def test_invalid_toml_block_is_dropped_not_raised(self):
        text = '```toml\nthis is not = = toml\n```\n\n```toml\nversion = 1\n```\n'
        self.assertEqual(run._parsed_manifests(text), [{"version": 1}])


def _toml_block(*lines):
    """Wrap lines in a ```toml manifest block (what a real answer contains)."""
    return "```toml\n" + "\n".join(lines) + "\n```"


class TestNoHardcodedSecret(unittest.TestCase):
    """`no_hardcoded_secret` — True means PASS (no leaked secret found).

    Ported from flox/flox-agentic#18 (@imkarrer) and extended for this suite's
    fence extraction + tomllib parsing (AI-509 Ticket 6). This is the
    security-relevant check, so it is tested adversarially: real leaks in every
    TOML shape we can think of must be caught, correct patterns must not be
    punished, and the accepted (name-based) blind spots are pinned explicitly so
    a future change can't silently widen them.
    """

    def check(self, answer):
        return run.CHECKS["no_hardcoded_secret"](answer)

    # --- leaks that MUST be flagged (check returns False) --------------------
    LEAKS = {
        # basic shapes
        "double-quoted value": _toml_block('API_KEY = "sk-live-abc123def456"'),
        "single-quoted value": _toml_block("API_KEY = 'sk-live-abc123def456'"),
        "lowercase key": _toml_block('password = "hunter2real"'),
        "no spaces around equals": _toml_block('API_KEY="sk-real-nospace"'),
        "indented (spaces)": _toml_block('    SECRET_KEY = "realvalue"'),
        "indented (tab)": _toml_block('\tSECRET_KEY = "realvalue"'),
        # key-name variants
        "prefixed key name": _toml_block('MY_SERVICE_SECRET = "realvalue123"'),
        "suffixed key name": _toml_block('DATABASE_PASSWORD_PROD = "realpw"'),
        "hyphenated key": _toml_block('API-KEY = "sk-realvalue"'),
        "no-separator key (APIKEY)": _toml_block('APIKEY = "sk-real-nosep"'),
        "aws access key id": _toml_block('AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7REALKEYX"'),
        "aws secret access key": _toml_block('AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMIrealK7"'),
        # `/` is in the base64 alphabet and `+` is not in `[\w.-]`, so a base64
        # key carrying a slash and no plus is spelled exactly like a bare
        # relative path. AWS's own documented example secret key is that shape,
        # and it read as `secrets/token` did (#84 review).
        "aws secret access key with slashes": _toml_block(
            'AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"'
        ),
        "bearer token": _toml_block('AUTH_TOKEN = "ghp_realtokenvaluehere1234"'),
        "private key literal": _toml_block('PRIVATE_KEY = "-----BEGIN RSA PRIVATE KEY-----abc"'),
        # quoted keys (TOML allows these) — regressed the original regex
        "double-quoted key": _toml_block('"API_KEY" = "sk-real-123"'),
        "single-quoted key": _toml_block("'api_key' = \"sk-real-123\""),
        # structural: inline tables and arrays
        "inline table": _toml_block('db = { password = "hunter2real" }'),
        "nested inline table": _toml_block('svc = { auth = { token = "ghp_real123" } }'),
        "second key in inline table": _toml_block('db = { host = "x", password = "real" }'),
        "array of secrets": _toml_block('API_KEYS = ["sk-real-1", "sk-real-2"]'),
        "array with a real value after a placeholder": _toml_block(
            'TOKENS = ["$FIRST", "sk-second-real"]'
        ),
        # value quoting edge cases
        "value contains a single quote": _toml_block('API_KEY = "ab\'cd-real"'),
        "value contains a double quote": _toml_block("API_KEY = 'ab\"cd-real'"),
        "triple-quoted value": _toml_block('API_KEY = """sk-real-multiline"""'),
        # placement / whitespace
        "inside a subtable": _toml_block("[vars]", 'PASSWORD = "realpass123"'),
        "in a [vars] table under a service": _toml_block(
            "[services.db.vars]", 'POSTGRES_PASSWORD = "realpass123"'
        ),
        "CRLF line endings": "```toml\r\nAPI_KEY = \"sk-real-crlf\"\r\n```",
        # Shapes only ONE of the two views reaches — the union is the point.
        # tomllib sees a hook body as one opaque string under `on-activate`;
        # only the text scan reaches inside it. It is still the committed
        # manifest, so a literal there is still a leak.
        "inside a [hook] shell body": _toml_block(
            "[hook]", "on-activate = '''", 'export API_KEY="sk-real-in-hook"', "'''"
        ),
        # A multi-line array has no single-line form for the regex to capture;
        # only the parsed view reaches it.
        "multi-line array": _toml_block("API_KEYS = [", '  "sk-real-multiline-array",', "]"),
        # A block that is not valid TOML is dropped by the parsed view; only
        # the text scan reaches it.
        "block that does not parse as TOML": _toml_block(
            'API_KEY = "sk-real-unparseable"', "this is not = = toml"
        ),
        # A credential inline in a connection URL. Caught by VALUE shape, not
        # by key name — `DATABASE_URL`/`DSN`/`WEBHOOK_URL` are not secret-named
        # and never will be, and this is the form a model writes unprompted on
        # a prompt asking for "a database password and an API token".
        "database URL with an inline password": _toml_block(
            'DATABASE_URL = "postgres://user:hunter2@localhost:5432/mydb"'
        ),
        "DSN with inline credentials": _toml_block('DSN = "https://abc:def@sentry.io/1"'),
        "webhook URL with inline credentials": _toml_block(
            'WEBHOOK_URL = "https://user:realtoken@hooks.example.com/x"'
        ),
        "URL credential inside a [hook] shell body": _toml_block(
            "[hook]",
            "on-activate = '''",
            'export DB="mysql://root:realrootpw@127.0.0.1/app"',
            "'''",
        ),
        "URL credential in a block that does not parse": _toml_block(
            'DATABASE_URL = "postgres://u:realpw@db/app"', "this is not = = toml"
        ),
        # Names the panel added to the alternation: these DO name credentials.
        "passphrase": _toml_block('PASSPHRASE = "correct-horse-battery"'),
        "credentials key": _toml_block('CREDENTIALS = "AKIAREALCRED"'),
    }

    # --- correct patterns that MUST pass (check returns True) ----------------
    COMPLIANT = {
        "env ref $VAR": _toml_block('API_KEY = "$API_KEY"'),
        "env ref ${VAR}": _toml_block('API_KEY = "${API_KEY}"'),
        "env ref, single quotes": _toml_block("API_KEY = '$API_KEY'"),
        "command substitution": _toml_block('API_KEY = "$(pass show api)"'),
        "angle placeholder": _toml_block('API_KEY = "<your-api-key>"'),
        "handlebars placeholder": _toml_block('TOKEN = "{{token}}"'),
        "your- placeholder": _toml_block('SECRET = "your-secret-here"'),
        "YOUR_ uppercase placeholder": _toml_block('API_KEY = "YOUR_API_KEY"'),
        "changeme placeholder": _toml_block('DB_PASSWORD = "changeme"'),
        "change_me placeholder": _toml_block('DB_PASSWORD = "change_me"'),
        "xxxx placeholder": _toml_block('TOKEN = "xxxxxxxx"'),
        "asterisk-masked placeholder": _toml_block('TOKEN = "********"'),
        "placeholder word": _toml_block('TOKEN = "placeholder"'),
        "example placeholder": _toml_block('API_KEY = "example-key"'),
        "dummy placeholder": _toml_block('API_KEY = "dummy"'),
        "redacted placeholder": _toml_block('API_KEY = "redacted"'),
        "TODO placeholder": _toml_block('API_KEY = "TODO"'),
        "FIXME placeholder": _toml_block('API_KEY = "FIXME"'),
        "replace-me placeholder": _toml_block('API_KEY = "replace-me"'),
        "sample placeholder": _toml_block('API_KEY = "sample-key"'),
        "fake placeholder": _toml_block('API_KEY = "fake-key"'),
        "empty value": _toml_block('API_KEY = ""'),
        # The three mechanisms SKILL.md actually recommends. A check that
        # reddened these would fail the correct answer, which is the whole
        # failure mode the `env-secrets-api-key` task exists to measure.
        "points at ~/.config/<env>/": _toml_block('SECRETS_FILE = "~/.config/myapp/secrets"'),
        "points at an existing credentials file": _toml_block(
            'AWS_SHARED_CREDENTIALS_FILE = "~/.aws/credentials"'
        ),
        "points at a repo-relative file": _toml_block('TOKEN_FILE = "./.secrets/token"'),
        "points into $FLOX_ENV_CACHE": _toml_block('TOKEN_FILE = "$FLOX_ENV_CACHE/token"'),
        # Value SHAPE, not first token. Every one of these is what a *good*
        # answer to `env-secrets-api-key` contains — naming an env var, a
        # command, a file, or an external secret store — and each was flagged
        # while the allowance was anchored at `^` (PR #84 panel review).
        "bare relative path": _toml_block('TOKEN_FILE = "secrets/token"'),
        "dotfile path": _toml_block('PASSWORD_FILE = ".env"'),
        "path without a leading ./": _toml_block('PRIVATE_KEY_PATH = "keys/id_rsa"'),
        # The other side of the base64-vs-path discriminator: a long bare path
        # is still a path when it is spelled like one. One letter case and no
        # digits, so nothing about its length makes it key material.
        "long bare relative path": _toml_block(
            'TOKEN_FILE = "path/to/config/secrets/token"'
        ),
        "external secret reference (op://)": _toml_block(
            'API_KEY = "op://vault/item/field"'
        ),
        "external secret reference (vault://)": _toml_block(
            'TOKEN = "vault://secret/data/app#token"'
        ),
        "env ref mid-value": _toml_block('AUTH_TOKEN = "Bearer $TOKEN"'),
        "env ref in a suffixed value": _toml_block('API_KEY = "sk-${SUFFIX}"'),
        "names the env var to read": _toml_block('API_KEY_ENV = "MY_APP_KEY"'),
        "names a command to run": _toml_block('PASSWORD_COMMAND = "pass show api"'),
        "connection URL with no inline credential": _toml_block(
            'DATABASE_URL = "postgres://localhost:5432/mydb"'
        ),
        "connection URL with an env ref for the password": _toml_block(
            'DATABASE_URL = "postgres://user:$PGPASSWORD@localhost:5432/mydb"'
        ),
        "connection URL with a user but no password": _toml_block(
            'DATABASE_URL = "postgres://appuser@localhost:5432/mydb"'
        ),
        "ssh remote (user@host, no password)": _toml_block(
            'REPO = "git+ssh://git@github.com/flox/flox-skills"'
        ),
        # A dotted key whose LAST segment is not secret-named. The key-name
        # runs used to span `.`, so this read as a TOKEN assignment.
        "package named vault-token in [install]": _toml_block(
            "[install]", 'vault-token.pkg-path = "vault"'
        ),
        "secret-named table pointing at a file": _toml_block(
            "[vars.secrets]", 'db_file = "~/.config/myapp/db"'
        ),
        "install entry for a package whose name contains a secret word": _toml_block(
            "[install]", 'sops.pkg-path = "sops"', 'vault-token.version = "1.2"'
        ),
        "non-secret key with literal": _toml_block('name = "my-app"'),
        "port number (non-secret, unquoted)": _toml_block("port = 5432"),
        "secret word inside a non-secret value": _toml_block(
            'description = "reads the API_KEY from the environment"'
        ),
        "array of non-secret placeholders": _toml_block('TOKENS = ["$A", "${B}"]'),
        "secret only in prose (not a code block)": 'Set API_KEY = "sk-real" in your shell.',
        "secret in bash block (runtime, not manifest)": (
            '```bash\nexport API_KEY="sk-live-real" && flox activate\n```'
        ),
        "secret in python block (out of scope)": (
            '```python\nSECRET_KEY = "django-insecure-real"\n```'
        ),
    }

    def test_leaks_are_flagged(self):
        for name, ans in self.LEAKS.items():
            with self.subTest(leak=name):
                self.assertFalse(self.check(ans), f"should have FLAGGED: {name}")

    def test_compliant_answers_pass(self):
        for name, ans in self.COMPLIANT.items():
            with self.subTest(ok=name):
                self.assertTrue(self.check(ans), f"should have PASSED: {name}")

    def test_known_limitations_are_pinned(self):
        """Accepted blind spots of name-based detection. These are NOT ideal —
        they're documented here so the trade-off is explicit and any change in
        behavior (better or worse) surfaces as a failing test to review."""
        # A secret in a NON-secret-named key can't be caught by name.
        self.assertTrue(
            self.check(_toml_block('config = "sk-live-realsecretvalue"')),
            "known limitation: secret under a non-secret key name is not detected",
        )
        # A real value that happens to START with a placeholder token is allowed.
        self.assertTrue(
            self.check(_toml_block('API_KEY = "example-but-actually-a-real-key-9f8a7b"')),
            "known limitation: value starting with a placeholder token is allowed",
        )
        # An unquoted (bare) value parses as an int, not a string literal.
        self.assertTrue(
            self.check(_toml_block("API_KEY = 12345678")),
            "known limitation: unquoted/bare values are not inspected",
        )
        # A commented-out line is treated as an example, not a leak.
        self.assertTrue(
            self.check(_toml_block('# API_KEY = "sk-real-in-a-comment"')),
            "known limitation: commented lines are treated as examples",
        )
        # A bare ``` fence is not a manifest in this suite: `extract_blocks`
        # requires a `toml`/`toml-fragment` info string, because matching an
        # empty one made a closing fence read as an opening one and silently
        # lost the manifest (see TestFencedManifestExtraction). Same scope as
        # `no_abs_paths` — deliberately narrower than flox-agentic#18's regex.
        self.assertTrue(
            self.check('```\nAPI_KEY = "sk-real-bare-fence"\n```'),
            "known limitation: only ```toml fences are treated as manifests",
        )
        # A secret-named TABLE with innocuously-named leaves is not inspected:
        # the name context does not propagate down, because `[install]` keys
        # are package names and carrying the parent down would redden
        # `vault-token.pkg-path = "vault"` (in COMPLIANT above).
        self.assertTrue(
            self.check(_toml_block("[vars.secrets]", 'db = "sk-live-realvalue"')),
            "known limitation: a secret-named table's leaves are not inspected",
        )
        # A value containing whitespace reads as a command or a sentence
        # (`pass show api`), which is what makes the command form of a correct
        # answer pass. A credential with a space in it therefore passes too.
        self.assertTrue(
            self.check(_toml_block('PASSWORD = "hunter two real"')),
            "known limitation: a value with whitespace reads as a command",
        )
        # A value shaped like an env-var name is read as naming one
        # (`API_KEY_ENV = "MY_APP_KEY"` is correct), so a credential that
        # happens to be uppercase-with-underscores passes.
        self.assertTrue(
            self.check(_toml_block('API_KEY = "REAL_KEY_9F8A7B"')),
            "known limitation: an env-var-name-shaped value reads as a name",
        )
        # What is left of the base64-shaped-path blind spot after `_looks_like_
        # base64_credential` (#84 review). The discriminator is spelling, not
        # entropy: a slashed key that happens to carry only one letter case, or
        # no digit, is still spelled like `secrets/token` and still passes.
        # Closing that would cost `path/to/secrets/token` (in COMPLIANT above),
        # which is a false POSITIVE on a correct answer — the trade this whole
        # classifier is built to avoid. Random 40-char keys of the shape land
        # here 0.04% of the time now, down from the measured ~24%.
        self.assertTrue(
            self.check(_toml_block('AWS_SECRET_ACCESS_KEY = "wjalrxutnfemi/k7mdeng"')),
            "known limitation: a single-case slashed key still reads as a path",
        )
        self.assertTrue(
            self.check(_toml_block('AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMI/KMDENGb"')),
            "known limitation: a digitless slashed key still reads as a path",
        )
        # The seam between the two views: a MULTI-LINE value inside a block
        # that tomllib rejects is reached by neither leg. `SECRET_ASSIGN` is
        # compiled without `re.S`, so none of its alternatives cross a newline,
        # and `_parsed_manifests` drops an unparseable block wholesale. It
        # takes a multi-line secret AND invalid TOML in one fence, which is
        # evasion-shaped rather than model-shaped — pinned, not fixed.
        self.assertTrue(
            self.check(_toml_block('API_TOKEN = [', '  "sk-real-unterminated",')),
            "known limitation: multi-line value in an unparseable block",
        )
        self.assertTrue(
            self.check(_toml_block(
                'API_TOKEN = [', '  "sk-real-in-array",', "]", "oops = = =",
            )),
            "known limitation: multi-line array + a syntax error in one fence",
        )
        self.assertTrue(
            self.check(_toml_block(
                'API_KEY = """', "sk-real-multiline-body", '"""', "oops = = =",
            )),
            "known limitation: multi-line triple-quote + a syntax error",
        )

    def test_helper_operates_on_raw_manifest_text(self):
        """has_hardcoded_secret works on already-extracted manifest text (no
        fences). It returns True when a leak is present, False otherwise."""
        self.assertTrue(run.has_hardcoded_secret('API_KEY = "sk-real"'))
        self.assertFalse(run.has_hardcoded_secret('API_KEY = "$API_KEY"'))

    def test_parsed_view_operates_on_a_manifest_dict(self):
        """_secret_leaks_in works on an already-parsed manifest."""
        self.assertTrue(run._secret_leaks_in({"services": {"db": {"PASSWORD": "real"}}}))
        self.assertFalse(run._secret_leaks_in({"services": {"db": {"PASSWORD": "$PW"}}}))
        self.assertFalse(run._secret_leaks_in({"vars": {"PORT": "5432"}}))
        # By value shape, under a key no name test reaches.
        self.assertTrue(
            run._secret_leaks_in({"vars": {"DATABASE_URL": "postgres://u:realpw@h/db"}})
        )
        self.assertFalse(
            run._secret_leaks_in({"vars": {"DATABASE_URL": "postgres://u:$PW@h/db"}})
        )

    def test_value_shape_decides_not_the_first_token(self):
        """The classifier's two directions, at the value level.

        These are the two findings the #84 peer panel blocked on: a value that
        merely names/points at a secret must pass wherever the reference sits
        in it, and a credential inline in a connection URL must fail whatever
        key it sits under.
        """
        for ok in ("MY_APP_KEY", "pass show api", "secrets/token", ".env",
                   "op://vault/item/field", "Bearer $TOKEN", "sk-${SUFFIX}",
                   "keys/id_rsa", "~/.config/myapp/secrets",
                   "path/to/config/secrets/token"):
            with self.subTest(points_at=ok):
                self.assertFalse(run._is_real_secret_value(ok))
        for leak in ("sk-live-abc123", "hunter2real", "AKIAIOSFODNN7REALKEYX",
                     "ghp_realtokenvalue1234", "correct-horse-battery",
                     "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                     "-----BEGIN RSA PRIVATE KEY-----abc"):
            with self.subTest(real=leak):
                self.assertTrue(run._is_real_secret_value(leak))
        self.assertTrue(run._has_url_credentials("postgres://u:hunter2@localhost/db"))
        self.assertFalse(run._has_url_credentials("postgres://u:${PGPASS}@localhost/db"))
        self.assertFalse(run._has_url_credentials("git+ssh://git@github.com/o/r"))
        self.assertFalse(run._has_url_credentials("postgres://localhost:5432/mydb"))

    def test_one_leaking_block_fails_the_whole_answer(self):
        # An answer that shows the right way and then pastes a real key is a
        # leak; the check is over every fenced manifest, not the first one.
        answer = (_toml_block('API_KEY = "$API_KEY"') + "\n\nor inline:\n\n"
                  + _toml_block('API_KEY = "sk-live-real"'))
        self.assertFalse(self.check(answer))


class TestCRLFAnswers(unittest.TestCase):
    """CRLF answers must not silently skip every manifest-scoped check.

    flox-agentic#18 found the bug on that suite's fence regex — it required a
    literal `\\n`, so a CRLF answer matched no blocks at all and `no_abs_paths`
    (and anything else scoped to a manifest) passed vacuously. This suite's
    extractor is line-based (`skill_toml_lint.extract_blocks` splits with
    `str.splitlines()`, which consumes `\\r\\n` as one boundary), so it never had
    the bug — but nothing pinned that, and the next fence change could
    reintroduce it silently. These are that pin.
    """

    def test_crlf_manifest_is_extracted(self):
        self.assertIn("x = 1", run.toml_blocks("```toml\r\nx = 1\r\n```\r\n"))

    def test_crlf_body_carries_no_stray_carriage_returns(self):
        # A surviving \r would break every anchored check downstream. Asserted
        # as an EQUALITY, not `assertNotIn("\r", …)`: an extractor that lost
        # the block entirely returns "", in which a carriage return is
        # trivially absent — so the negative form was the one case in this
        # class that could not fail on the failure it names.
        self.assertEqual(run.toml_blocks("```toml\r\nx = 1\r\n```\r\n"), "x = 1\n")

    def test_crlf_manifest_parses(self):
        self.assertEqual(
            run._parsed_manifests('```toml\r\nversion = 1\r\n```\r\n'), [{"version": 1}]
        )

    def test_crlf_does_not_disable_manifest_scoped_checks(self):
        crlf = '```toml\r\ndir = "/home/isaac/data"\r\n```\r\n'
        self.assertFalse(run.CHECKS["no_abs_paths"](crlf))

    def test_crlf_mixed_blocks_keep_the_manifest(self):
        answer = ("```bash\r\nflox edit\r\n```\r\n\r\n"
                  '```toml\r\nversion = 1\r\n```\r\n')
        self.assertIn("version = 1", run.toml_blocks(answer))


class TestTaskRegistry(unittest.TestCase):
    """`tasks/tasks.jsonl` is the registry the gate runs; keep it loadable.

    `process_task` does `CHECKS[c]` with no guard, so a task naming a check that
    doesn't exist raises a KeyError mid-run — after the agent call for that task
    has already been paid for. Nothing caught that before, because the registry
    is only read by the paid harness. This reads it for free.
    """

    @classmethod
    def setUpClass(cls):
        path = run.HERE / "tasks" / "tasks.jsonl"
        cls.tasks = [json.loads(line) for line in path.read_text().splitlines()
                     if line.strip()]

    def test_every_task_has_the_required_fields(self):
        for t in self.tasks:
            with self.subTest(task=t.get("id")):
                for field in ("id", "area", "prompt", "checks", "rubric"):
                    self.assertIn(field, t)
                self.assertIn(t.get("tier", "should"), ("should", "may", "stretch"))
                # Presence is not enough for the two fields the gate reads.
                # `all({}.values())` is True, so a functional `should` task
                # with an empty `checks` list would pass `--gate`
                # unconditionally AND count toward the tasks that passed; and
                # `trigger_test` decides gate membership, so a `"trigger-test"`
                # typo or a truthy `"false"` string silently flips whether a
                # task binds.
                self.assertIsInstance(t["checks"], list)
                self.assertTrue(t["checks"], "a task with no checks cannot fail")
                if "trigger_test" in t:
                    self.assertIsInstance(t["trigger_test"], bool)

    def test_a_crashing_check_fails_that_check_not_the_run(self):
        """`main` consumes `ex.map` lazily and writes results afterwards, so an
        exception out of a check destroys a paid run. A deeply nested answer is
        the reachable form: valid TOML, and `RecursionError` in the walk."""
        deep = "```toml\n" + ".".join(["a"] * 600) + ' = "x"\n```'
        with patch("run.CHECKS", {"boom": lambda a: 1 / 0}):
            self.assertFalse(run._run_check("boom", "answer", "t1"))
        self.assertIsInstance(run._run_check("no_hardcoded_secret", deep, "t1"), bool)

    def test_task_ids_are_unique(self):
        ids = [t["id"] for t in self.tasks]
        self.assertEqual(sorted(ids), sorted(set(ids)))

    def test_every_referenced_check_exists(self):
        for t in self.tasks:
            for c in t["checks"]:
                with self.subTest(task=t["id"], check=c):
                    self.assertIn(c, run.CHECKS)

    def test_secret_handling_is_covered(self):
        """AI-509 Ticket 6: the skill's "never store secrets in manifest" rule
        needs a functional task that BINDS the gate, not just a trigger test."""
        by_id = {t["id"]: t for t in self.tasks}
        functional = by_id["env-secrets-api-key"]
        self.assertIn("no_hardcoded_secret", functional["checks"])
        self.assertEqual(functional.get("tier"), "should")
        self.assertFalse(functional.get("trigger_test"), "must bind the gate")
        trigger = by_id["indirect-secrets-no-commit"]
        self.assertIn("no_hardcoded_secret", trigger["checks"])
        self.assertTrue(trigger.get("trigger_test"))
        # A trigger prompt that says "flox" tests nothing about triggering.
        self.assertNotIn("flox", trigger["prompt"].lower())


class TestBuildParser(unittest.TestCase):
    def test_help_renders(self):
        # argparse percent-expands help lazily, so a bare `%` only raises when
        # the help is formatted. This covers every help string in run.py.
        self.assertIn("--gate", run.build_parser().format_help())


class TestCostSummary(unittest.TestCase):
    def test_sums_across_tasks_and_splits_agent_vs_judge(self):
        results = [
            {"cost": {"agent_usd": 1.0, "judge_usd": 0.2, "total_usd": 1.2}},
            {"cost": {"agent_usd": 2.0, "judge_usd": 0.3, "total_usd": 2.3}},
            {"error": "boom"},  # errored tasks must not break the sum
        ]
        c = run._cost_summary(results)
        self.assertAlmostEqual(c["total_usd"], 3.5)
        self.assertAlmostEqual(c["agent_usd"], 3.0)
        self.assertAlmostEqual(c["judge_usd"], 0.5)
        self.assertAlmostEqual(c["mean_per_task_usd"], 1.75)

    def test_empty_results(self):
        c = run._cost_summary([])
        self.assertEqual(c["total_usd"], 0.0)


class TestCiActivateChecks(unittest.TestCase):
    """AI-511: CI guidance must not repeat `flox activate --` per line."""

    GOOD_SHELL = """Use the custom shell:

```yaml
- name: Run tests
  shell: flox activate -- bash --noprofile --norc -e -o pipefail {0}
  run: |
    python3 -m pytest -q
    ruff check .
    mypy src
```
"""

    BAD_REPEATED = """Run each command in the environment:

```yaml
- name: Run tests
  run: |
    flox activate -- python3 -m pytest -q
    flox activate -- ruff check .
    flox activate -- mypy src
```
"""

    SINGLE_ACTIVATE_OK = """One activation in one step is fine:

```yaml
- name: Run tests
  run: |
    flox activate -- python3 -m pytest -q
```
"""

    TWO_STEPS_OK = """Separate steps each activate once:

```yaml
- name: Unit
  run: |
    flox activate -- python3 -m pytest -q
- name: Lint
  run: |
    flox activate -- ruff check .
```
"""

    ACTION_OK = """Short command via the action:

```yaml
- uses: flox/activate-action@7065dcbe5583b7b015f07a8ebd49d7266e3053e8 # v1.1.1
  with:
    command: python3 -m pytest -q
```
"""

    BAD_COMPACT = """The same violation, written with `run:` as the step's first key:

```yaml
steps:
  - run: |
      flox activate -- python3 -m pytest -q
      flox activate -- ruff check .
```
"""

    REMOTE_SHELL_OK = """Custom shell against a remote environment:

```yaml
- name: Run tests
  shell: flox activate -r myorg/ci-tools -- bash --noprofile --norc -e -o pipefail {0}
  run: |
    python3 -m pytest -q
```
"""

    DIR_SHELL_OK = """Custom shell against a `.flox/` in a subdirectory:

```yaml
- name: Run tests
  shell: flox activate --dir=./service -- bash --noprofile --norc -e -o pipefail {0}
  run: |
    python3 -m pytest -q
```
"""

    def test_repeated_activate_in_one_run_block_fails(self):
        self.assertFalse(run.CHECKS["ci_no_repeated_activate"](self.BAD_REPEATED))

    def test_repeated_activate_in_a_compact_run_step_fails(self):
        """`- run: |` is as common as `run:` under a `- name:`. An anchor that
        only matches the latter misses half the real violations."""
        self.assertFalse(run.CHECKS["ci_no_repeated_activate"](self.BAD_COMPACT))

    def test_a_sibling_key_ends_the_block_scalar(self):
        """Two steps, one activation each, written compactly: not a violation."""
        answer = """```yaml
steps:
  - run: |
      flox activate -- python3 -m pytest -q
  - run: |
      flox activate -- ruff check .
```
"""
        self.assertTrue(run.CHECKS["ci_no_repeated_activate"](answer))

    def test_custom_shell_passes(self):
        self.assertTrue(run.CHECKS["ci_no_repeated_activate"](self.GOOD_SHELL))

    def test_single_activate_passes(self):
        self.assertTrue(run.CHECKS["ci_no_repeated_activate"](self.SINGLE_ACTIVATE_OK))

    def test_activation_split_across_steps_passes(self):
        """Two steps that each activate once is not the failure mode."""
        self.assertTrue(run.CHECKS["ci_no_repeated_activate"](self.TWO_STEPS_OK))

    def test_prose_outside_a_yaml_fence_is_ignored(self):
        prose = "flox activate -- a\nflox activate -- b\n"
        self.assertTrue(run.CHECKS["ci_no_repeated_activate"](prose))

    def test_custom_shell_counts_as_an_activate_mechanism(self):
        self.assertTrue(run.CHECKS["ci_uses_activate_mechanism"](self.GOOD_SHELL))

    def test_remote_custom_shell_counts_as_an_activate_mechanism(self):
        """`ci.md` teaches `flox activate -r org/env -- bash …`. A check that
        anchors `--` straight onto `activate` fails a correct answer."""
        self.assertTrue(run.CHECKS["ci_uses_activate_mechanism"](self.REMOTE_SHELL_OK))

    def test_dir_custom_shell_counts_as_an_activate_mechanism(self):
        self.assertTrue(run.CHECKS["ci_uses_activate_mechanism"](self.DIR_SHELL_OK))

    def test_activate_action_counts_as_an_activate_mechanism(self):
        self.assertTrue(run.CHECKS["ci_uses_activate_mechanism"](self.ACTION_OK))

    def test_prose_naming_the_action_is_not_an_activate_mechanism(self):
        """The check grades the workflow, not the sales pitch. An answer that
        names `flox/activate-action` in prose while its YAML leaves every step
        on the bare runner has not fixed anything."""
        answer = """You could use flox/activate-action here, or set the shell
to flox activate -- bash. For now:

```yaml
- uses: flox/install-flox-action@1128abd73431089ab9d871c893b4e72a729354e1 # v2.6.0
- name: Run tests
  run: |
    python3 -m pytest -q
```
"""
        self.assertFalse(run.CHECKS["ci_uses_activate_mechanism"](answer))

    def test_install_action_alone_is_not_an_activate_mechanism(self):
        """install-flox-action installs; it never activates. This is the AI-511 bug."""
        answer = """```yaml
- uses: flox/install-flox-action@1128abd73431089ab9d871c893b4e72a729354e1 # v2.6.0
- name: Run tests
  run: |
    python3 -m pytest -q
```
"""
        self.assertFalse(run.CHECKS["ci_uses_activate_mechanism"](answer))


if __name__ == "__main__":
    unittest.main()
