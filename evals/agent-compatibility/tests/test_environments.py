"""The two container environments, and the relation the docs claim between them.

Reads the manifests as text — no `flox` call, no build, nothing to mock.
"""
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


if __name__ == "__main__":
    unittest.main()
