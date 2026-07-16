#!/usr/bin/env python3
"""Golden-manifest lint: run verify.py's checker over every hand-curated
reference in testdata/gold/*.toml (AI-456 item 2).

These manifests are the reference every produced manifest is judged
against, and until now nothing had ever linted them — two hand reviews
(AI-455) found real defects. This is a cheap, unit-test-tier check: no
`claude`, no agent, no cloned repo. There is no detect.json for these
repos (they aren't vendored — cloning all eight just to lint golden TOML
would defeat the point of a cheap tier), so only the manifest-only checks
run: [vars] literalness, hook-mutation, and catalog resolution/version/
per-system availability. The detect-cross-check invariants (runtime
installed, leaf-datastore served) need facts this tier doesn't have and
are exercised instead by test_verify.py and the harness (run_floxify.py).

Catalog checks need `flox` + network — same ADVISORY-skip as the harness's
own activation check. When unavailable, this test still runs (skipped
catalog checks yield zero catalog violations) but proves nothing about
catalog drift; it only fails on genuinely-checked violations.

KNOWN_VIOLATIONS is an explicit allowlist, one entry per current golden
defect, each tagged AI-457 (the follow-up that fixes the goldens — do NOT
fix golden content in this change, per the AI-461 ticket). Entries are
keyed by (fixture id, rule, a pkg-path substring), not the full violation
message, so a routine catalog version bump (e.g. nodejs_24's latest moving
from 24.18.0 to 24.19.0) doesn't false-fail this test — the point is to
catch a NEW class of violation, not to pin today's exact version numbers.

Run:
    python3 test_golden_lint.py
    pytest test_golden_lint.py
"""
import importlib.util
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
GOLD_DIR = HERE / "testdata" / "gold"
VERIFY = HERE.parent.parent / "flox-plugin" / "skills" / "floxify" / "scripts" / "verify.py"


def _load_verify():
    spec = importlib.util.spec_from_file_location("verify", VERIFY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["verify"] = mod
    spec.loader.exec_module(mod)
    return mod


verify_mod = _load_verify()
verify = verify_mod.verify

# (fixture id, rule, pkg-path substring) -> tracking ticket.
# Recorded from a live `flox show` run against nixpkgs on 2026-07-16 — see
# the AI-461 PR description for the exact violation list. AI-457 burns this
# down by fixing the golden content; new entries here should be rare and
# always tagged with the ticket that will resolve them.
KNOWN_VIOLATIONS = {
    ("mastodon", "catalog-systems-mismatch", "nodejs_24"): "AI-457",
    ("mastodon", "catalog-systems-mismatch", "ffmpeg"): "AI-457",
    ("mastodon", "catalog-systems-mismatch", "libidn"): "AI-457",
    ("gitea", "catalog-systems-mismatch", "nodejs_26"): "AI-457",
    ("gitea", "catalog-systems-mismatch", "pnpm"): "AI-457",
    ("gitea", "catalog-systems-mismatch", "sqlite"): "AI-457",
    ("posthog", "catalog-version-missing", "python313"): "AI-457",
    ("posthog", "catalog-systems-mismatch", "uv"): "AI-457",
    ("posthog", "catalog-systems-mismatch", "docker-compose"): "AI-457",
    ("plausible", "catalog-systems-mismatch", "postgresql"): "AI-457",
    ("sentry", "catalog-systems-mismatch", "python313"): "AI-457",
    ("sentry", "catalog-systems-mismatch", "nodejs_24"): "AI-457",
    ("sentry", "catalog-systems-mismatch", "uv"): "AI-457",
    ("sentry", "catalog-systems-mismatch", "pnpm_10"): "AI-457",
    ("sentry", "catalog-systems-mismatch", "openssl"): "AI-457",
    ("supabase", "catalog-systems-mismatch", "deno"): "AI-457",
}


def _is_allowlisted(fixture_id, v):
    return any(
        fid == fixture_id and rule == v["rule"] and needle in v["message"]
        for (fid, rule, needle) in KNOWN_VIOLATIONS
    )


def _gold_ids():
    return sorted(p.stem for p in GOLD_DIR.glob("*.toml"))


class TestGoldenLint(unittest.TestCase):
    """One test per golden so a failure names the exact fixture."""

    @classmethod
    def setUpClass(cls):
        cls.catalog_checked = None  # filled in by the first golden run

    def _lint(self, fixture_id):
        manifest_text = (GOLD_DIR / f"{fixture_id}.toml").read_text(encoding="utf-8")
        # No detect facts for these repos (not vendored) -- manifest-only
        # checks only; see module docstring.
        result = verify({}, manifest_text, check_catalog_live=True)
        TestGoldenLint.catalog_checked = result["catalog_checked"]
        hard = [v for v in result["violations"] if v["severity"] == "hard"]
        unlisted = [v for v in hard if not _is_allowlisted(fixture_id, v)]
        if unlisted:
            detail = "\n".join(f"  [{v['rule']}] {v['message']}" for v in unlisted)
            self.fail(
                f"{fixture_id}.toml has {len(unlisted)} violation(s) not in "
                f"KNOWN_VIOLATIONS (new regression, or the allowlist needs "
                f"an AI-457-tagged entry):\n{detail}"
            )


def _make_test(fixture_id):
    def test(self):
        self._lint(fixture_id)
    test.__name__ = f"test_{fixture_id.replace('-', '_')}_has_no_unlisted_violations"
    return test


for _fixture_id in _gold_ids():
    setattr(TestGoldenLint, _make_test(_fixture_id).__name__, _make_test(_fixture_id))


if __name__ == "__main__":
    unittest.main()
