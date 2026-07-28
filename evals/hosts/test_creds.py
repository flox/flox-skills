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


if __name__ == "__main__":
    unittest.main()
