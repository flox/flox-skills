"""The matrix definition is data, so the tests are structural."""
import dataclasses
import unittest

from lib.cells import CELLS, Cell

# The two dimensions the matrix crosses, in the vocabulary the README uses:
# three agent applications against three installation methods.
AGENTS = ("claude", "codex", "opencode")
INSTALL_METHODS = ("native", "npx", "flox-ai")


class TestMatrix(unittest.TestCase):
    def test_has_eight_cells(self):
        self.assertEqual(len(CELLS), 8)

    def test_every_cell_crosses_a_known_agent_and_installation_method(self):
        for c in CELLS:
            self.assertIn(c.agent, AGENTS, c.id)
            self.assertIn(c.install_method, INSTALL_METHODS, c.id)

    def test_cell_ids_are_unique(self):
        ids = [c.id for c in CELLS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_cell_targets_a_known_image(self):
        for c in CELLS:
            self.assertIn(c.image, ("base", "withpkg"), c.id)

    def test_opencode_has_no_native_plugin_cell(self):
        methods = {c.install_method for c in CELLS if c.agent == "opencode"}
        self.assertEqual(methods, {"npx", "flox-ai"})

    def test_flox_ai_cells_use_the_withpkg_image(self):
        for c in CELLS:
            if c.install_method == "flox-ai":
                self.assertEqual(c.image, "withpkg", c.id)

    def test_non_flox_ai_cells_use_the_base_image(self):
        for c in CELLS:
            if c.install_method != "flox-ai":
                self.assertEqual(c.image, "base", c.id)

    def test_every_cell_can_prove_load(self):
        for c in CELLS:
            self.assertTrue(c.list_cmd, c.id)
            self.assertTrue(c.expect, c.id)

    def test_every_cell_has_a_launch_command_with_a_prompt_slot(self):
        for c in CELLS:
            self.assertIn("{prompt}", c.launch, c.id)

    def test_no_unresolved_placeholders(self):
        """No `<<<to be observed>>>` stub ever reaches a real run."""
        for c in CELLS:
            for field in (c.install, c.list_cmd, c.launch):
                self.assertNotIn("<<<", field, c.id)

    def test_npx_cells_use_skills_sh_agent_ids(self):
        """`-a claude` is rejected — skills.sh calls it `claude-code`."""
        for c in CELLS:
            if c.install_method == "npx":
                self.assertIn("-a ", c.install, c.id)
                self.assertNotIn("-a claude ", c.install, c.id)

    def test_npx_cells_are_non_interactive(self):
        """Without -s/-y the installer renders a picker and blocks.

        On whitespace-split tokens, not substrings: `npx --yes` CONTAINS `-y`,
        so deleting the real ` -y ` flag left this assertion green and only the
        `-s` half was enforced.
        """
        for c in CELLS:
            if c.install_method == "npx":
                tokens = c.install.split()
                self.assertIn("-y", tokens, c.id)
                self.assertIn("-s", tokens, c.id)

    def test_npx_cells_assert_the_shared_skills_tree(self):
        """A skill is not a plugin: `codex plugin list` is empty after a
        successful skills.sh install, which writes to ~/.agents/skills."""
        for c in CELLS:
            if c.install_method == "npx":
                self.assertIn("skills", c.list_cmd, c.id)
                self.assertNotIn("plugin list", c.list_cmd, c.id)

    def test_flox_ai_launches_omit_the_binary_name(self):
        """The README lists this among the properties pinned by tests, and
        nothing asserted it until now.

        flox-ai forwards everything after `--` VERBATIM. Repeating the binary
        name runs `claude claude -p ...`, which exits 0 while silently dropping
        the prompt — the model answered "I'm here and ready. What would you
        like to work on?" and the cell was scored a pass.
        """
        for c in CELLS:
            if c.install_method == "flox-ai":
                _, _, forwarded = c.launch.partition(" -- ")
                self.assertTrue(forwarded, c.id)
                self.assertFalse(forwarded.startswith(c.agent),
                                 f"{c.id}: launch repeats the binary name after `--`")

    def test_every_launch_survives_prompt_substitution(self):
        """`run_matrix` does `cell.launch.format(prompt=...)`, so a literal
        brace — a jq filter, a shell brace expansion — raises at run time.
        `jq` is in both images, so this is a realistic way to add a cell."""
        for c in CELLS:
            try:
                c.launch.format(prompt="/prompt.txt")
            except (KeyError, IndexError, ValueError) as exc:
                self.fail(f"{c.id}: launch breaks .format() — {exc!r}")

    def test_cells_are_frozen(self):
        """`assertRaises(Exception)` was satisfied by the `IndexError` of an
        empty `CELLS` and by the `AttributeError` of a renamed field, so it
        could pass without a frozen dataclass anywhere in the file."""
        with self.assertRaises(dataclasses.FrozenInstanceError):
            CELLS[0].id = "mutated"



if __name__ == "__main__":
    unittest.main()
