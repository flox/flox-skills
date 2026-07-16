#!/usr/bin/env python3
"""Unit tests for the Tier 2 /floxify eval harness (tier2.py).

Covers the deterministic, unit-testable pieces: structural-conformance
checks (runtime pin / service-block regexes), registry loading, and the
clone-at-SHA fallback chain. The agentic skill run and LLM judge call are
integration-only (same as Tier 1's run_floxify.py has no unit tests around
`_run_claude_agent`/`_judge`) and are exercised by an actual `--only
mastodon` run, not here.

Run: python3 -m unittest test_tier2 -v
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tier2


class TestRunGit(unittest.TestCase):
    """`_run_git` wraps subprocess.run into a (ok, error) tuple."""

    @patch("tier2.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        ok, err = tier2._run_git(["git", "init"], timeout=10)
        self.assertTrue(ok)
        self.assertEqual(err, "")

    @patch("tier2.subprocess.run")
    def test_failure_captures_stderr(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=128, stdout="", stderr="fatal: repository not found"
        )
        ok, err = tier2._run_git(["git", "clone", "bad-url"], timeout=10)
        self.assertFalse(ok)
        self.assertIn("fatal: repository not found", err)

    @patch("tier2.subprocess.run")
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=10)
        ok, err = tier2._run_git(["git", "fetch"], timeout=10)
        self.assertFalse(ok)
        self.assertIn("timed out", err)


class TestCloneAtSha(unittest.TestCase):
    """`_clone_at_sha` falls back direct-fetch -> partial-clone -> full-clone."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="tier2-clone-test-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("tier2._try_full_clone")
    @patch("tier2._try_partial_clone")
    @patch("tier2._try_direct_fetch")
    def test_direct_fetch_succeeds_short_circuits(
        self, mock_direct, mock_partial, mock_full
    ):
        mock_direct.return_value = None
        result = tier2._clone_at_sha("https://example.com/r", "abc123", self.tmpdir)
        self.assertIsNone(result)
        mock_direct.assert_called_once()
        mock_partial.assert_not_called()
        mock_full.assert_not_called()

    @patch("tier2._try_full_clone")
    @patch("tier2._try_partial_clone")
    @patch("tier2._try_direct_fetch")
    def test_falls_back_to_partial_clone(self, mock_direct, mock_partial, mock_full):
        mock_direct.return_value = "couldn't find remote ref abc123"
        mock_partial.return_value = None
        result = tier2._clone_at_sha("https://example.com/r", "abc123", self.tmpdir)
        self.assertIsNone(result)
        mock_direct.assert_called_once()
        mock_partial.assert_called_once()
        mock_full.assert_not_called()

    @patch("tier2._try_full_clone")
    @patch("tier2._try_partial_clone")
    @patch("tier2._try_direct_fetch")
    def test_all_strategies_fail_reports_combined_error(
        self, mock_direct, mock_partial, mock_full
    ):
        mock_direct.return_value = "direct failed"
        mock_partial.return_value = "partial failed"
        mock_full.return_value = "full failed"
        result = tier2._clone_at_sha("https://example.com/r", "abc123", self.tmpdir)
        self.assertIsNotNone(result)
        self.assertIn("direct failed", result)
        self.assertIn("partial failed", result)
        self.assertIn("full failed", result)


class TestRuntimePinned(unittest.TestCase):
    def test_matches_generic_pkg_path(self):
        manifest = 'ruby.pkg-path = "ruby"\n'
        self.assertTrue(tier2._runtime_pinned(manifest, r"ruby(_[0-9_]+)?"))

    def test_matches_versioned_pkg_path(self):
        manifest = 'ruby.pkg-path = "ruby_3_3"\n'
        self.assertTrue(tier2._runtime_pinned(manifest, r"ruby(_[0-9_]+)?"))

    def test_rejects_near_miss_package(self):
        # rubyPackages.foo must not satisfy a "ruby" runtime pin.
        manifest = 'lint.pkg-path = "rubyPackages.foo"\n'
        self.assertFalse(tier2._runtime_pinned(manifest, r"ruby(_[0-9_]+)?"))

    def test_matches_nodejs_24_exactly(self):
        manifest = 'nodejs.pkg-path = "nodejs_24"\n'
        self.assertTrue(tier2._runtime_pinned(manifest, "nodejs_24"))

    def test_rejects_wrong_node_version(self):
        manifest = 'nodejs.pkg-path = "nodejs_20"\n'
        self.assertFalse(tier2._runtime_pinned(manifest, "nodejs_24"))

    def test_none_manifest_returns_false(self):
        self.assertFalse(tier2._runtime_pinned(None, "nodejs_24"))


class TestServicePresent(unittest.TestCase):
    def test_matches_postgres_section(self):
        manifest = "[services.postgres]\ncommand = \"postgres\"\n"
        self.assertTrue(tier2._service_present(manifest, "postgres"))

    def test_matches_postgresql_variant(self):
        manifest = "[services.postgresql]\ncommand = \"postgres\"\n"
        self.assertTrue(tier2._service_present(manifest, "postgres"))

    def test_no_match_when_service_absent(self):
        manifest = "[services.redis]\ncommand = \"redis-server\"\n"
        self.assertFalse(tier2._service_present(manifest, "postgres"))

    def test_none_manifest_returns_false(self):
        self.assertFalse(tier2._service_present(None, "postgres"))


class TestStructuralChecks(unittest.TestCase):
    def test_full_conformant_manifest(self):
        entry = {
            "id": "mastodon",
            "expected_runtimes": [
                {"name": "ruby", "pattern": r"ruby(_[0-9_]+)?"},
                {"name": "nodejs_24", "pattern": "nodejs_24"},
            ],
            "expected_services": ["postgres", "redis"],
        }
        manifest = (
            'schema-version = "1.13.0"\n'
            "[install]\n"
            'ruby.pkg-path = "ruby"\n'
            'nodejs.pkg-path = "nodejs_24"\n'
            "[services.postgres]\n"
            'command = "postgres"\n'
            "[services.redis]\n"
            'command = "redis-server"\n'
        )
        checks = tier2._structural_checks(entry, manifest)
        self.assertTrue(checks["manifest_created"])
        self.assertTrue(checks["valid_toml"])
        self.assertTrue(checks["no_abs_paths"])
        self.assertTrue(checks["pins_ruby"])
        self.assertTrue(checks["pins_nodejs_24"])
        self.assertTrue(checks["has_service_postgres"])
        self.assertTrue(checks["has_service_redis"])
        self.assertTrue(all(checks.values()))

    def test_missing_service_fails_that_check_only(self):
        entry = {
            "id": "mastodon",
            "expected_runtimes": [{"name": "ruby", "pattern": r"ruby(_[0-9_]+)?"}],
            "expected_services": ["postgres", "redis"],
        }
        manifest = (
            'schema-version = "1.13.0"\n'
            "[install]\n"
            'ruby.pkg-path = "ruby"\n'
            "[services.postgres]\n"
            'command = "postgres"\n'
        )
        checks = tier2._structural_checks(entry, manifest)
        self.assertTrue(checks["pins_ruby"])
        self.assertTrue(checks["has_service_postgres"])
        self.assertFalse(checks["has_service_redis"])
        self.assertFalse(all(checks.values()))

    def test_no_manifest_fails_everything(self):
        entry = {
            "id": "mastodon",
            "expected_runtimes": [{"name": "ruby", "pattern": r"ruby(_[0-9_]+)?"}],
            "expected_services": ["postgres"],
        }
        checks = tier2._structural_checks(entry, None)
        self.assertFalse(checks["manifest_created"])
        self.assertFalse(checks["valid_toml"])
        self.assertFalse(checks["no_abs_paths"])
        self.assertFalse(checks["pins_ruby"])
        self.assertFalse(checks["has_service_postgres"])

    def test_absolute_path_fails_check(self):
        entry = {"id": "x", "expected_runtimes": [], "expected_services": []}
        manifest = (
            "[vars]\n"
            'cache_dir = "/home/user/.cache"\n'
        )
        checks = tier2._structural_checks(entry, manifest)
        self.assertFalse(checks["no_abs_paths"])


class TestLoadRegistry(unittest.TestCase):
    def test_parses_jsonl_lines(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            f.write(json.dumps({"id": "a"}) + "\n")
            f.write("\n")  # blank lines are skipped
            f.write(json.dumps({"id": "b"}) + "\n")
            path = f.name
        try:
            entries = tier2._load_registry(path)
            self.assertEqual([e["id"] for e in entries], ["a", "b"])
        finally:
            Path(path).unlink()


def _run(hard_pass=True, score=5, correct=True, error=None):
    """Build a per-run result the shape `process_entry` returns."""
    if error is not None:
        return {"id": "x", "error": error}
    return {
        "id": "x",
        "hard_pass": hard_pass,
        "hard_checks": {"manifest_created": hard_pass},
        "judge": {"score": score, "correct": correct},
        "activation": {"ok": None, "skipped": True},
    }


class TestSummarize(unittest.TestCase):
    """Guards the reps>1 reporting bug: an aggregate entry has no top-level
    `judge` key, so an unflattened summary silently reported all-zeros even
    when every run passed. `_summarize` must flatten first."""

    def test_reps_1_plain_entries(self):
        results = [_run(hard_pass=True, score=5)]
        summary = tier2._summarize(results, "skill@branch")
        self.assertEqual(summary["n_repos"], 1)
        self.assertEqual(summary["n"], 1)
        self.assertEqual(summary["hard_pass_rate"], 1.0)
        self.assertEqual(summary["avg_judge_score"], 5.0)
        self.assertEqual(summary["n_errors"], 0)

    def test_reps_gt_1_aggregate_is_flattened_not_zeroed(self):
        # Aggregate shape from process_task when reps>1: no top-level judge.
        results = [
            {
                "id": "mastodon",
                "reps": 2,
                "runs": [_run(hard_pass=True, score=5), _run(hard_pass=True, score=4)],
                "hard_pass_rate_across_reps": 1.0,
            }
        ]
        summary = tier2._summarize(results, "skill@branch")
        self.assertEqual(summary["n_repos"], 1)     # one repo
        self.assertEqual(summary["n"], 2)           # two scored runs, NOT zero
        self.assertEqual(summary["hard_pass_rate"], 1.0)
        self.assertEqual(summary["avg_judge_score"], 4.5)
        self.assertEqual(summary["n_errors"], 0)

    def test_reps_gt_1_counts_error_runs(self):
        results = [
            {
                "id": "x",
                "reps": 2,
                "runs": [_run(error="clone failed"), _run(hard_pass=True, score=3)],
            }
        ]
        summary = tier2._summarize(results, "s")
        self.assertEqual(summary["n_errors"], 1)
        self.assertEqual(summary["n"], 1)  # one scored run among the two


class TestRegistryPatternDriftGuard(unittest.TestCase):
    """Runs the real mastodon registry patterns against a committed capture
    of the skill's actual manifest output. Fails if a future registry-pattern
    edit silently stops matching real output (regex drift)."""

    def test_mastodon_patterns_match_real_manifest(self):
        here = Path(tier2.__file__).resolve().parent
        entry = next(
            e for e in tier2._load_registry(here / "tier2.jsonl")
            if e["id"] == "mastodon"
        )
        manifest = (here / "testdata" / "mastodon-manifest.toml").read_text()
        checks = tier2._structural_checks(entry, manifest)
        self.assertTrue(checks["pins_ruby_4_0"], checks)
        self.assertTrue(checks["pins_nodejs_24"], checks)
        self.assertTrue(checks["has_service_postgres"], checks)
        self.assertTrue(checks["has_service_redis"], checks)
        self.assertTrue(checks["no_abs_paths"], checks)
        self.assertTrue(all(checks.values()), checks)


class TestProbeCommandFor(unittest.TestCase):
    """AI-447: per-kind connectivity probes.

    The postgres probe deliberately passes NO host/port. `pg_isready` reads
    PGHOST/PGPORT from the environment, and the environment is what the
    manifest's own [vars] set — so a bare `pg_isready` asserts the service is
    reachable *at the address the manifest advertises*. That is the check that
    catches a manifest whose [vars] point at a datastore nothing serves.
    """

    def test_postgres_probe_is_bare_pg_isready(self):
        cmd = tier2._probe_command_for("postgres")
        self.assertIn("pg_isready", cmd)
        self.assertNotIn("-h ", cmd)
        self.assertNotIn("-p ", cmd)

    def test_postgresql_alias_resolves(self):
        self.assertIn("pg_isready", tier2._probe_command_for("postgresql"))

    def test_redis_probe_expects_pong(self):
        cmd = tier2._probe_command_for("redis")
        self.assertIn("redis-cli", cmd)
        self.assertIn("ping", cmd.lower())

    def test_mariadb_probe(self):
        self.assertIn("admin", tier2._probe_command_for("mariadb"))

    def test_unknown_kind_has_no_probe(self):
        self.assertIsNone(tier2._probe_command_for("clickhouse"))


class TestProbeServices(unittest.TestCase):
    """AI-447: prove services actually serve, not just that a section exists.

    Services can only be started from *inside* an activation — `flox services
    start` on an unactivated env errors with "Cannot start services for an
    environment that is not activated". So the probe is a single
    `flox activate --start-services -c <script>`, where the script polls the
    connectivity probe and prints a sentinel.

    The sentinels matter: they separate "the service did not serve" (a real
    verdict about the manifest) from "flox/the environment errored" (a harness
    problem that must never be reported as a service failure).
    """

    @patch("tier2.shutil.which", return_value=None)
    def test_flox_absent_skips_rather_than_fails(self, _which):
        res = tier2._probe_services("/tmp/x", ["postgres"])
        self.assertTrue(res["postgres"]["skipped"])
        self.assertIsNone(res["postgres"]["ok"])

    @patch("tier2.shutil.which", return_value="/usr/bin/flox")
    @patch("tier2._run_flox")
    def test_uses_activate_start_services_not_bare_services_start(
        self, mock_flox, _which
    ):
        mock_flox.return_value = (True, "__SERVICE_OK__")
        tier2._probe_services("/tmp/x", ["postgres"])
        args = mock_flox.call_args_list[0].args[0]
        self.assertIn("activate", args)
        self.assertIn("--start-services", args)

    @patch("tier2.shutil.which", return_value="/usr/bin/flox")
    @patch("tier2._run_flox")
    def test_service_serving_is_ok(self, mock_flox, _which):
        mock_flox.return_value = (True, "__SERVICE_OK__")
        res = tier2._probe_services("/tmp/x", ["postgres"])
        self.assertTrue(res["postgres"]["ok"], res)
        self.assertFalse(res["postgres"]["skipped"])

    @patch("tier2.shutil.which", return_value="/usr/bin/flox")
    @patch("tier2._run_flox")
    def test_service_never_comes_up_is_a_real_failure(self, mock_flox, _which):
        # THE case this ticket exists for: [services.*] present, activation ok,
        # but nothing ever answers on the advertised address.
        mock_flox.return_value = (False, "__SERVICE_DEAD__")
        res = tier2._probe_services("/tmp/x", ["postgres"])
        self.assertFalse(res["postgres"]["ok"], res)
        self.assertFalse(res["postgres"]["skipped"], res)

    @patch("tier2.shutil.which", return_value="/usr/bin/flox")
    @patch("tier2._run_flox")
    def test_harness_error_is_skipped_not_a_service_failure(self, mock_flox, _which):
        # No sentinel in the output => flox itself errored (bad flag, env
        # broken, timeout). Reporting that as "your postgres is broken" would
        # be a lie — exactly the confusion AI-454 flags for activation.
        mock_flox.return_value = (False, "ERROR: unknown flag --start-services")
        res = tier2._probe_services("/tmp/x", ["postgres"])
        self.assertTrue(res["postgres"]["skipped"], res)
        self.assertIsNone(res["postgres"]["ok"], res)
        self.assertIn("could not be probed", res["postgres"]["notes"])

    @patch("tier2.shutil.which", return_value="/usr/bin/flox")
    @patch("tier2._run_flox")
    def test_unprobeable_service_is_skipped_not_failed(self, mock_flox, _which):
        # clickhouse has no probe; absence of a probe must never read as failure.
        mock_flox.return_value = (True, "__SERVICE_OK__")
        res = tier2._probe_services("/tmp/x", ["clickhouse"])
        self.assertTrue(res["clickhouse"]["skipped"])
        self.assertIsNone(res["clickhouse"]["ok"])

    @patch("tier2.shutil.which", return_value="/usr/bin/flox")
    @patch("tier2._run_flox")
    def test_undeclared_service_is_not_probed(self, mock_flox, _which):
        """A service the manifest never declared must not be probed at all.

        Regression: lemmy produced a manifest with NO [services.*] section
        whose [hook] nevertheless started postgres to bootstrap the database.
        A bare `pg_isready` then answered, and the probe reported OK — for an
        environment with no service. A hook-spawned postgres is not a
        Flox-managed service: `flox services` can't start/stop/status it and it
        dies with the activation. Crediting it is a false positive.

        `has_service_*` owns "did you wire it"; the probe owns "does the wired
        service work". Probing an undeclared service answers neither.
        """
        manifest = '[install]\npg.pkg-path = "postgresql_16"\n[hook]\non-activate = "pg_ctl start"\n'
        res = tier2._probe_services("/tmp/x", ["postgres"], manifest_text=manifest)
        self.assertTrue(res["postgres"]["skipped"], res)
        self.assertIsNone(res["postgres"]["ok"], res)
        self.assertIn("not declared", res["postgres"]["notes"])
        mock_flox.assert_not_called()

    @patch("tier2.shutil.which", return_value="/usr/bin/flox")
    @patch("tier2._run_flox")
    def test_declared_service_is_probed(self, mock_flox, _which):
        mock_flox.return_value = (True, "__SERVICE_OK__")
        manifest = '[services.postgres]\ncommand = "postgres"\n'
        res = tier2._probe_services("/tmp/x", ["postgres"], manifest_text=manifest)
        self.assertTrue(res["postgres"]["ok"], res)
        mock_flox.assert_called()

    @patch("tier2.shutil.which", return_value="/usr/bin/flox")
    @patch("tier2._run_flox")
    def test_probe_script_polls_for_readiness(self, mock_flox, _which):
        # Services start asynchronously — a single immediate probe would race.
        mock_flox.return_value = (True, "__SERVICE_OK__")
        tier2._probe_services("/tmp/x", ["postgres"])
        script = mock_flox.call_args_list[0].args[0][-1]
        self.assertIn("pg_isready", script)
        self.assertIn("sleep", script)


if __name__ == "__main__":
    unittest.main()
