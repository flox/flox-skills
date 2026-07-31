#!/usr/bin/env python3
"""Shared helper for loading skill scripts (detect.py, verify.py) as fresh
module instances from an arbitrary skill directory.

Both test_verify.py and test_real_world_golden_lint.py used to load
flox-plugin/skills/floxify/scripts/verify.py via their own copy of this
same ~6-line loader, and both registered the result under the SAME global
key, `sys.modules["verify"]`. Whichever file's module import ran second
silently overwrote the first's entry; `unittest.mock.patch("verify.attr")`
re-resolves `sys.modules["verify"]` at every test call (not once at
decoration time), so it ended up patching whichever instance happened to
be registered last — not the instance the test actually exercised. In CI
(no flox on PATH), this meant TestCatalog's "mocked" tests fell through
to a real `subprocess.run(["flox", ...])`, hit `FileNotFoundError`, and
every catalog check returned `catalog-unresolved` instead of exercising
the mock. Where flox *was* present, the tests silently made live network
calls instead of using the fixture data they appeared to be testing
against.

`load_module` fixes this by making sys.modules registration opt-in and
keyed by a name the CALLER chooses — pass a name unique to your call site
(e.g. "verify_under_test_verify") when you need `@patch("<name>.attr")`
to resolve correctly; omit it when you don't. The harness's per-task
loader (run_floxify.py) never uses @patch and reloads on every task
(including concurrently, via ThreadPoolExecutor), so it omits the key
entirely — each call gets a private module instance untouched by any
other loader in the process.
"""
import importlib.util
import sys


def load_module(path, sys_modules_key=None):
    """Load `path` as a fresh module instance.

    `sys_modules_key`, when given, MUST be unique among every other
    loaded instance of the same file in this process — reusing a key
    (e.g. always "verify") is exactly the collision this helper exists
    to prevent. Leave it None for loaders that never need
    `unittest.mock.patch` to resolve the module by string name.
    """
    name = sys_modules_key or f"_skill_module_{id(path)}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    if sys_modules_key:
        sys.modules[sys_modules_key] = mod
    spec.loader.exec_module(mod)
    return mod
