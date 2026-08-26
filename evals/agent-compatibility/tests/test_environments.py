"""The two container environments, and the relation the docs claim between them.

Reads the manifests as text — no `flox` call, no build, nothing to mock.
"""
import json
import re
import unittest
from pathlib import Path


class TestEnvironments(unittest.TestCase):
    """Both READMEs describe `withpkg` as "`base` plus …", and nothing enforced it."""

    ENVIRONMENTS = Path(__file__).resolve().parent.parent / "environments"

    @staticmethod
    def _installs(manifest: Path):
        """The `[install]` block's entries, as a set of `name.attr = value`."""
        lines, inside = set(), False
        for raw in manifest.read_text().splitlines():
            line = raw.strip()
            if line.startswith("["):
                inside = line == "[install]"
                continue
            if inside and line and not line.startswith("#"):
                lines.add(line)
        return lines

    def test_withpkg_is_base_plus(self):
        base = self._installs(self.ENVIRONMENTS / "base/.flox/env/manifest.toml")
        withpkg = self._installs(self.ENVIRONMENTS / "withpkg/.flox/env/manifest.toml")
        self.assertTrue(base, "no [install] block parsed from base")
        missing = base - withpkg
        self.assertFalse(
            missing,
            f"withpkg is documented as base plus flox-ai and skills-flox, but "
            f"these base entries are missing from it: {sorted(missing)}")

    def test_withpkg_adds_only_the_flox_package_path(self):
        base = self._installs(self.ENVIRONMENTS / "base/.flox/env/manifest.toml")
        withpkg = self._installs(self.ENVIRONMENTS / "withpkg/.flox/env/manifest.toml")
        added = {line.split(".")[0] for line in withpkg - base}
        self.assertEqual(added, {"flox-ai", "skills-flox"},
                         "the two images must differ in exactly one dimension")


class TestPinnedPackage(unittest.TestCase):
    """`withpkg` exists to exercise the PUBLISHED artifact, so its pin has to be
    the version the image will really contain.

    Editing the manifest without re-locking is silent: `--rebuild` re-runs
    `flox containerize` against the unchanged lock and produces a byte-identical
    image, so the cells would keep exercising the old package while the README
    says they exercise the pinned one.
    """

    ENV = Path(__file__).resolve().parent.parent / "environments" / "withpkg"

    def test_the_pin_and_the_lock_agree(self):
        toml = (self.ENV / ".flox/env/manifest.toml").read_text()
        declared = re.search(r'^skills-flox\.version = "([^"]+)"', toml, re.M)
        self.assertIsNotNone(declared, "no skills-flox pin in the manifest")
        lock = json.loads((self.ENV / ".flox/env/manifest.lock").read_text())
        locked = {p["version"] for p in lock["packages"]
                  if p.get("install_id") == "skills-flox"}
        self.assertEqual(locked, {declared.group(1)},
                         "manifest.toml was edited without re-locking")


if __name__ == "__main__":
    unittest.main()
