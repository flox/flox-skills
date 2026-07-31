#!/usr/bin/env python3
"""Harness plumbing for the stretch tier (AI-431).

The stretch tier adds known-hard and conversion-mode fixtures to the
/floxify eval.
It is REPORT-ONLY: tracked and reported, but it never gates the build. It
reuses the synthetic runner (`run_floxify.py`) verbatim — same fixtures/ and
expected/ layout, same CHECKS — driven by a separate registry, `stretch.jsonl`,
so the default/weekly `run_floxify.py --gate` run stays exactly the six
should-tier fixtures it was and is not slowed or destabilised by these
exploratory cases:

    python3 run_floxify.py --tasks stretch.jsonl          # report-only run
    python3 run_floxify.py --tasks stretch.jsonl --only ruby-native-gems

These are deterministic, no-network unit tests (no `claude`, no `flox`) —
they check the registry/fixtures/goldens are internally consistent and,
crucially, that the "never gates" guarantee is structural:
`run_floxify.py`'s gate binds ONLY `should`-tier tasks, so every stretch
entry being `stretch` is what makes the tier report-only. The live golden
lint (catalog + lock resolution) lives in test_stretch_golden_lint.py; the
agentic outcome run is exercised by an actual `run_floxify.py
--tasks stretch.jsonl` invocation, same as synthetic/real-world's own skill runs.

Run:
    python3 -m unittest tests.test_stretch -v
"""
import json
import unittest
from pathlib import Path

import run_floxify

HERE = Path(__file__).resolve().parent
SUITE = HERE.parent          # evals/floxify
REPO_ROOT = SUITE.parent.parent
STRETCH_FILE = SUITE / "stretch.jsonl"
FIXTURES_DIR = SUITE / "fixtures"
EXPECTED_DIR = SUITE / "expected"

# TOML parser, same graceful fallback the runner uses.
try:
    import tomllib as _toml  # Python 3.11+
except ImportError:  # pragma: no cover - exercised only on <3.11
    _toml = None


def _load_stretch():
    entries = [
        json.loads(line)
        for line in STRETCH_FILE.read_text().splitlines()
        if line.strip()
    ]
    return entries


_STRETCH = _load_stretch()
# A path typo or an emptied registry must fail loudly at collection time,
# not silently report "0 tests, all passed" (the same discipline
# test_real_world_golden_lint.py's `assert _GOLD_IDS` uses).
assert _STRETCH, f"no stretch entries found in {STRETCH_FILE} — check the path"

# The synthetic registry, so we can assert the two namespaces don't collide —
# both draw fixtures/ and expected/ from the same directories.
_SYNTHETIC_IDS = {
    json.loads(line)["id"]
    for line in (SUITE / "synthetic.jsonl").read_text().splitlines()
    if line.strip()
}


class TestStretchRegistry(unittest.TestCase):
    """The registry is well-formed and the report-only guarantee holds."""

    def test_registry_non_empty(self):
        self.assertTrue(_STRETCH)

    def test_ids_unique(self):
        ids = [e["id"] for e in _STRETCH]
        self.assertEqual(len(ids), len(set(ids)), "duplicate id in stretch.jsonl")

    def test_ids_do_not_collide_with_synthetic(self):
        """fixtures/ and expected/ are shared with synthetic — a colliding id
        would make `--tasks stretch.jsonl` and the default run fight over
        the same fixture/gold pair and confuse the by-id regression diff."""
        clash = sorted({e["id"] for e in _STRETCH} & _SYNTHETIC_IDS)
        self.assertFalse(clash, f"stretch ids collide with synthetic.jsonl: {clash}")

    def test_every_entry_is_stretch_tier(self):
        """The heart of 'report-only'. run_floxify.py's gate binds only
        should-tier tasks (`binding = [r for r in scored if r['tier'] ==
        'should']`), so a stretch entry that slipped in as 'should' would
        silently become gate-binding. Every entry MUST be 'stretch'."""
        offenders = [e["id"] for e in _STRETCH if e.get("tier") != "stretch"]
        self.assertFalse(
            offenders,
            f"the stretch tier is report-only; these are not tier 'stretch': {offenders}",
        )

    def test_no_entry_would_bind_the_gate(self):
        """Restates the guarantee against the runner's ACTUAL gate rule
        rather than the string 'stretch', so a future change to how the
        gate selects binding tasks is caught here too."""
        binding = [e for e in _STRETCH if e.get("tier") == "should"]
        self.assertEqual(
            binding, [], "no stretch-tier entry may be should-tier (would gate)"
        )

    def test_required_fields_present(self):
        for e in _STRETCH:
            with self.subTest(id=e.get("id")):
                for field in ("id", "tier", "ecosystem", "checks", "rubric"):
                    self.assertIn(field, e, f"{e.get('id')} missing '{field}'")
                self.assertIsInstance(e["checks"], list)
                self.assertTrue(e["checks"], f"{e['id']} has empty checks")
                self.assertTrue(e["rubric"].strip(), f"{e['id']} has empty rubric")

    def test_checks_are_real_check_names(self):
        """Every declared check must be a key in run_floxify.CHECKS —
        a typo'd check name is silently skipped by the runner otherwise."""
        valid = set(run_floxify.CHECKS)
        for e in _STRETCH:
            with self.subTest(id=e["id"]):
                unknown = [c for c in e["checks"] if c not in valid]
                self.assertFalse(
                    unknown,
                    f"{e['id']} references unknown check(s) {unknown}; "
                    f"valid checks: {sorted(valid)}",
                )

    def test_baseline_checks_present_everywhere(self):
        """Every fixture, hard or conversion, must at least assert a
        manifest was produced, parses, has an [install] section, and
        carries no absolute paths / hallucinated install URL — the floor
        every synthetic fixture also holds."""
        floor = {
            "manifest_created", "valid_toml", "has_install_section",
            "no_abs_paths", "no_fake_install_url",
        }
        for e in _STRETCH:
            with self.subTest(id=e["id"]):
                missing = floor - set(e["checks"])
                self.assertFalse(missing, f"{e['id']} missing floor checks {missing}")


class TestStretchFixturesAndExpected(unittest.TestCase):
    """Each registered id has a fixture repo and a gold manifest on disk."""

    def test_fixture_dir_exists_and_nonempty(self):
        for e in _STRETCH:
            with self.subTest(id=e["id"]):
                fx = FIXTURES_DIR / e["id"]
                self.assertTrue(fx.is_dir(), f"missing fixtures/{e['id']}/")
                files = [p for p in fx.rglob("*") if p.is_file()]
                self.assertTrue(files, f"fixtures/{e['id']}/ has no files")

    def test_fixture_has_no_flox_dir(self):
        """synthetic discipline (README 'Adding a fixture'): fixtures ship no
        .flox/ — the skill creates it. A vendored .flox/ would let a rep
        score a pre-existing manifest instead of the skill's own output."""
        for e in _STRETCH:
            with self.subTest(id=e["id"]):
                self.assertFalse(
                    (FIXTURES_DIR / e["id"] / ".flox").exists(),
                    f"fixtures/{e['id']}/.flox exists — remove it; the "
                    f"skill must create the manifest",
                )

    def test_gold_manifest_exists_and_parses(self):
        for e in _STRETCH:
            with self.subTest(id=e["id"]):
                gold = EXPECTED_DIR / f"{e['id']}.toml"
                self.assertTrue(gold.is_file(), f"missing expected/{e['id']}.toml")
                text = gold.read_text()
                self.assertTrue(
                    run_floxify._is_valid_toml(text),
                    f"expected/{e['id']}.toml does not parse as TOML",
                )
                if _toml is not None:
                    parsed = _toml.loads(text)
                    self.assertIn(
                        "install", parsed,
                        f"expected/{e['id']}.toml has no [install] section",
                    )


if __name__ == "__main__":
    unittest.main()
