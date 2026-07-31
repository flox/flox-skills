"""Unit tests for the `floxify` outcome eval suite.

A real package (not a namespace one) so `python3 -m unittest` can address the
modules as `tests.test_verify` and so `discover` finds them: unittest dropped
namespace-package discovery in 3.11, which is the interpreter this repo pins.

Run them from the suite root (`evals/floxify/`), which puts the suite's own
modules — `run_floxify`, `tier2`, `_skill_module_loader` — on `sys.path`:

    python3 -m unittest discover -s tests -t . -v
"""
