"""Unit tests for the `floxify` outcome eval suite.

A real package (not a namespace one) so `discover` finds the modules: unittest
dropped namespace-package discovery in 3.11, which is the interpreter this repo
pins, and `discover -s tests -t .` fails with `ImportError: Start directory is
not importable` without this file. Discovery is the only thing it buys —
addressing a module as `tests.test_verify` works either way, since that path
goes through ordinary namespace-package *import*.

Both suites own a package named `tests`, so a single process cannot span them:
run each from its own suite root.

Run them from the suite root (`evals/floxify/`), which puts the suite's own
modules — `run_floxify`, `real_world`, `_skill_module_loader` — on `sys.path`:

    python3 -m unittest discover -s tests -t . -v
"""
