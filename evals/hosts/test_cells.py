"""The matrix definition is data, so the tests are structural."""
import unittest

from lib.cells import CELLS, Cell


class TestMatrix(unittest.TestCase):
    def test_has_eight_cells(self):
        self.assertEqual(len(CELLS), 8)

    def test_cell_ids_are_unique(self):
        ids = [c.id for c in CELLS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_cell_targets_a_known_image(self):
        for c in CELLS:
            self.assertIn(c.image, ("base", "withpkg"), c.id)

    def test_opencode_has_no_native_plugin_cell(self):
        methods = {c.method for c in CELLS if c.host == "opencode"}
        self.assertEqual(methods, {"npx", "flox-ai"})

    def test_flox_ai_cells_use_the_withpkg_image(self):
        for c in CELLS:
            if c.method == "flox-ai":
                self.assertEqual(c.image, "withpkg", c.id)

    def test_non_flox_ai_cells_use_the_base_image(self):
        for c in CELLS:
            if c.method != "flox-ai":
                self.assertEqual(c.image, "base", c.id)

    def test_every_cell_can_prove_load(self):
        for c in CELLS:
            self.assertTrue(c.list_cmd, c.id)
            self.assertTrue(c.expect, c.id)

    def test_every_cell_has_a_launch_command_with_a_prompt_slot(self):
        for c in CELLS:
            self.assertIn("{prompt}", c.launch, c.id)

    def test_no_unresolved_probe_markers(self):
        """Guards the <<<from PROBE.md>>> placeholders out of a real run."""
        for c in CELLS:
            for field in (c.install, c.list_cmd, c.launch):
                self.assertNotIn("<<<", field, c.id)

    def test_cells_are_frozen(self):
        with self.assertRaises(Exception):
            CELLS[0].id = "mutated"


if __name__ == "__main__":
    unittest.main()
