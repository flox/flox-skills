"""Runner tests over mocked subprocesses — no docker, no flox, no API spend."""
import subprocess
import unittest
from unittest.mock import patch

from lib import images


class TestImages(unittest.TestCase):
    def test_tag_is_env_name_and_version(self):
        # `flox containerize` names the repo after the ENVIRONMENT (hosts-base),
        # and -t is the tag alone.
        self.assertEqual(images.image_tag("base", "20260727"), "hosts-base:20260727")
        self.assertEqual(images.image_tag("withpkg", "20260727"), "hosts-withpkg:20260727")

    @patch("lib.images.subprocess.run")
    def test_image_exists_is_true_when_docker_prints_an_id(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="abc123\n", stderr="")
        self.assertTrue(images.image_exists("hosts-base:x"))

    @patch("lib.images.subprocess.run")
    def test_image_exists_is_false_when_docker_prints_nothing(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="\n", stderr="")
        self.assertFalse(images.image_exists("hosts-base:x"))

    @patch("lib.images.image_exists", return_value=True)
    @patch("lib.images.subprocess.run")
    def test_build_skips_when_image_present(self, run, _exists):
        images.build("base", "20260727")
        run.assert_not_called()

    @patch("lib.images.image_exists", return_value=True)
    @patch("lib.images.subprocess.run")
    def test_build_rebuilds_when_forced(self, run, _exists):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        images.build("base", "20260727", rebuild=True)
        self.assertEqual(run.call_count, 1)
        argv = run.call_args[0][0]
        self.assertIn("containerize", argv)
        # -t carries the bare version; a "name:version" here is the bug that
        # produced `hosts-base:base:20260727` and "invalid reference format".
        self.assertEqual(argv[argv.index("-t") + 1], "20260727")

    @patch("lib.images.image_exists", return_value=False)
    @patch("lib.images.subprocess.run")
    def test_build_raises_on_failure(self, run, _exists):
        run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
        with self.assertRaises(images.BuildError):
            images.build("base", "20260727")


if __name__ == "__main__":
    unittest.main()
