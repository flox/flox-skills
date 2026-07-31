"""Unit tests for the `flox` guidance eval suite.

A real package (not a namespace one) so `python3 -m unittest` can address the
modules as `tests.test_run` and so `discover` finds them: unittest dropped
namespace-package discovery in 3.11, which is the interpreter this repo pins.

Run them from the suite root (`evals/flox/`), which puts the suite's own
modules — `run`, `screen`, `skill_toml_lint` — on `sys.path`:

    python3 -m unittest discover -s tests -t . -v
"""
