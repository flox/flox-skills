"""Unit tests for the agent installation compatibility suite.

A real package (not a namespace one) so `discover` finds the modules: unittest
dropped namespace-package discovery in 3.11, which is the interpreter this repo
pins, and `discover -s tests -t .` fails with `ImportError: Start directory is
not importable` without this file. Discovery is the only thing it buys —
addressing a module as `tests.test_cells` works either way, since that path
goes through ordinary namespace-package *import*.

Every suite here owns a package named `tests`, so a single process cannot span
two of them: run each from its own suite root.

Run them from the suite root (`evals/agent-compatibility/`), which puts the
suite's own modules — `run_matrix` and the `lib` package — on `sys.path`:

    python3 -m unittest discover -s tests -t . -v

Nothing here starts a container, reads a real credential file, or calls a
model: every subprocess is mocked and every fixture is built in a temp dir.
The matrix itself is manual-only — see README.md.
"""
