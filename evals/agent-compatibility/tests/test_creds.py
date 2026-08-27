"""Credential minimization — the only code that touches real secrets.

No network, no container, no live credential files: every test builds its
own fixture in a temp dir.
"""
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from lib import creds

FULL_CLAUDE = {
    "claudeAiOauth": {"accessToken": "sk-test", "refreshToken": "rt-test"},
    "mcpOAuth": {
        "sentry|abc": {"accessToken": "MUST-NOT-LEAK"},
        "linear-server|def": {"accessToken": "MUST-NOT-LEAK"},
    },
}
CODEX = {"auth_mode": "chatgpt", "OPENAI_API_KEY": "sk-MUST-NOT-LEAK",
         "tokens": {"access_token": "at-test"}, "last_refresh": "2026-07-28"}


class TestMinimizeClaude(unittest.TestCase):
    def test_keeps_only_the_subscription_block(self):
        out = creds.minimize_claude(FULL_CLAUDE)
        self.assertEqual(list(out), ["claudeAiOauth"])

    def test_drops_every_mcp_token(self):
        out = creds.minimize_claude(FULL_CLAUDE)
        self.assertNotIn("MUST-NOT-LEAK", json.dumps(out))

    def test_raises_when_subscription_block_missing(self):
        with self.assertRaises(creds.CredentialError):
            creds.minimize_claude({"mcpOAuth": {}})


class TestPrepare(unittest.TestCase):
    def _srcs(self, tmp):
        c = Path(tmp) / "src-claude.json"
        x = Path(tmp) / "src-codex.json"
        c.write_text(json.dumps(FULL_CLAUDE))
        x.write_text(json.dumps(CODEX))
        return c, x

    def test_written_claude_file_is_minimized(self):
        with TemporaryDirectory() as tmp:
            c, x = self._srcs(tmp)
            dest = Path(tmp) / "run"
            creds.prepare(dest, c, x)
            written = json.loads((dest / "claude" / ".credentials.json").read_text())
            self.assertEqual(list(written), ["claudeAiOauth"])

    def test_files_are_mode_600(self):
        with TemporaryDirectory() as tmp:
            c, x = self._srcs(tmp)
            dest = Path(tmp) / "run"
            creds.prepare(dest, c, x)
            for rel in ("claude/.credentials.json", "codex/auth.json"):
                mode = os.stat(dest / rel).st_mode & 0o777
                self.assertEqual(mode, 0o600, rel)

    def test_codex_copy_drops_the_api_key(self):
        """OAuth only: a per-token-billed key must never reach a container."""
        with TemporaryDirectory() as tmp:
            c, x = self._srcs(tmp)
            dest = Path(tmp) / "run"
            creds.prepare(dest, c, x)
            written = (dest / "codex" / "auth.json").read_text()
            self.assertNotIn("MUST-NOT-LEAK", written)
            self.assertNotIn("OPENAI_API_KEY", written)
            self.assertIn("chatgpt", written)

    def test_codex_copy_keeps_the_oauth_tokens(self):
        with TemporaryDirectory() as tmp:
            c, x = self._srcs(tmp)
            dest = Path(tmp) / "run"
            creds.prepare(dest, c, x)
            written = json.loads((dest / "codex" / "auth.json").read_text())
            self.assertEqual(written["tokens"]["access_token"], "at-test")
            self.assertEqual(written["last_refresh"], "2026-07-28")

    def test_assert_minimized_rejects_a_fat_file(self):
        with TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps(FULL_CLAUDE))
            with self.assertRaises(creds.CredentialError):
                creds.assert_minimized(bad)

    def test_missing_source_file_raises(self):
        with TemporaryDirectory() as tmp:
            c, x = self._srcs(tmp)
            with self.assertRaises(creds.CredentialError):
                creds.prepare(Path(tmp) / "run", c, Path(tmp) / "nope.json")


class TestOpenRouterStore(unittest.TestCase):
    """The third store: optional, dotenv-sourced, and off unless asked for.

    It is the only credential here minted for the suite rather than copied
    from an agent application's own session, and the only one whose SOURCE has
    no schema — a shell env file may hold anything at all.
    """

    def _env(self, tmp, text):
        f = Path(tmp) / ".env-open-router"
        f.write_text(text)
        return f

    def test_dotenv_parses_key_and_value(self):
        with TemporaryDirectory() as tmp:
            f = self._env(tmp, "OPENROUTER_API_KEY=sk-or-v1-test\n")
            self.assertEqual(creds.read_dotenv(f),
                             {"OPENROUTER_API_KEY": "sk-or-v1-test"})

    def test_dotenv_ignores_comments_blanks_and_quotes(self):
        with TemporaryDirectory() as tmp:
            f = self._env(tmp, '# a comment\n\nOPENROUTER_API_KEY="sk-quoted"\n')
            self.assertEqual(creds.read_dotenv(f),
                             {"OPENROUTER_API_KEY": "sk-quoted"})

    def test_dotenv_on_a_missing_file_is_a_credential_error(self):
        """Not an OSError: exit 5 ("the run never started") is reachable only
        for exceptions this module owns."""
        with TemporaryDirectory() as tmp:
            with self.assertRaises(creds.CredentialError):
                creds.read_dotenv(Path(tmp) / "nope")

    def test_minimize_is_an_allowlist_not_a_denylist(self):
        """The Codex path is a denylist and is documented as the weaker
        shape. A shell env file can carry anything, so nothing but the one
        named key may travel."""
        out = creds.minimize_openrouter(
            {"OPENROUTER_API_KEY": "sk-keep", "ANTHROPIC_API_KEY": "MUST-NOT-LEAK",
             "AWS_SECRET_ACCESS_KEY": "MUST-NOT-LEAK"})
        self.assertNotIn("MUST-NOT-LEAK", json.dumps(out))
        self.assertEqual(out, {"openrouter": {"type": "api", "key": "sk-keep"}})

    def test_minimize_rejects_a_missing_or_empty_key(self):
        for raw in ({}, {"OPENROUTER_API_KEY": ""}, {"OTHER": "x"}):
            with self.assertRaises(creds.CredentialError):
                creds.minimize_openrouter(raw)

    def test_the_store_is_off_by_default(self):
        """Preparing it whenever the key file happens to exist would redefine
        what the two OpenCode cells measure with nothing in the run saying so."""
        self.assertNotIn("opencode", [s.agent for s in creds.active_stores()])
        self.assertIn("opencode", [s.agent for s in creds.active_stores(True)])

    def test_prepare_skips_it_unless_asked(self):
        """A missing key file must not fail a default run."""
        with TemporaryDirectory() as tmp:
            c = Path(tmp) / "c.json"; c.write_text(json.dumps(FULL_CLAUDE))
            x = Path(tmp) / "x.json"; x.write_text(json.dumps(CODEX))
            dest = Path(tmp) / "run"
            creds.prepare(dest, c, x, Path(tmp) / "absent")
            self.assertFalse((dest / "opencode").exists())

    def test_prepare_writes_the_shape_opencode_reads(self):
        """`{"openrouter": {"type": "api", "key": ...}}` at
        `.local/share/opencode/auth.json` — the layout `opencode auth list`
        reports as "OpenRouter api, 1 credentials" with no env var set."""
        with TemporaryDirectory() as tmp:
            c = Path(tmp) / "c.json"; c.write_text(json.dumps(FULL_CLAUDE))
            x = Path(tmp) / "x.json"; x.write_text(json.dumps(CODEX))
            e = self._env(tmp, "OPENROUTER_API_KEY=sk-or-v1-test\n")
            dest = Path(tmp) / "run"
            creds.prepare(dest, c, x, e, stores=creds.active_stores(True))
            written = json.loads((dest / "opencode" / "auth.json").read_text())
            self.assertEqual(written,
                             {"openrouter": {"type": "api", "key": "sk-or-v1-test"}})
            self.assertEqual(os.stat(dest / "opencode" / "auth.json").st_mode & 0o777,
                             0o600)

    def test_the_mount_point_is_where_opencode_looks(self):
        store, = [s for s in creds.STORES if s.agent == "opencode"]
        self.assertEqual(store.container_dir, ".local/share/opencode")
        self.assertEqual(store.filename, "auth.json")

    def test_a_fat_written_file_is_refused(self):
        with TemporaryDirectory() as tmp:
            bad = Path(tmp) / "auth.json"
            bad.write_text(json.dumps({"openrouter": {}, "anthropic": {}}))
            with self.assertRaises(creds.CredentialError):
                creds.assert_only_openrouter(bad)


if __name__ == "__main__":
    unittest.main()
