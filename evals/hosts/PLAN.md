# Host-Matrix Smoke Test Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the `flox` and `floxify` skills load and trigger in Codex, OpenCode, and Claude Code across our documented install methods, inside a disposable Flox-built container, without mutating the developer's machine.

**Architecture:** Two container images built by `flox containerize` from dedicated Flox environments — `base` (host CLIs, no skills) and `withpkg` (base + `flox-ai` + published `flox/skills-flox@1.0.0`). A Python runner iterates 8 cells, each a fresh `docker run --rm` with a per-cell copy of minimized credentials, records Tier A (load, auth-free) and Tier B (trigger, authenticated) results to JSONL, and prints a summary. Unknowns — each host's headless-invocation flags and skill-discovery paths — are resolved by a probe task **first**, so no later task guesses.

**Tech Stack:** Flox (`containerize`), Docker, Python 3 stdlib only (`subprocess`, `json`, `dataclasses`, `unittest`) matching `evals/run.py` and `evals/test_run.py`.

**Design:** `evals/hosts/DESIGN.md`

## Global Constraints

Every task's requirements implicitly include these. Values copied verbatim from the design.

- **Smoke test only.** One attempt per cell, pass/fail. Not a rate. A pass proves the plumbing works; a fail proves it's broken.
- **Credential minimization is required, not optional.** Copy `.claudeAiOauth` only. `~/.claude/.credentials.json` also holds `mcpOAuth` tokens for Fellow, Linear, Notion, Slack, and Sentry. Assert the written file has exactly one top-level key before any cell starts.
- **No API keys.** `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` are never set. Both hosts authenticate by OAuth; Codex reports `auth_mode: "chatgpt"`.
- **Credentials are never baked into an image layer** — mount only.
- **Cells are independent.** A failing cell records and continues; it never aborts the run.
- **Not wired into CI.** Manual harness; CI runs only its unit tests.
- **`x86_64-linux` only.**
- **Claude Code is the control.** A Claude cell failing means the harness is wrong, not the skill.
- **flox-ai cells are additional signal, not ground truth.** Native cells are ground truth.
- **Where invocation cannot be proven**, record `answer-shaped evidence only` — never as a verification.
- **Nothing is pushed.** Commits are local; `git push` is not in this plan.

## File Structure

| Path | Responsibility |
|---|---|
| `evals/hosts/base/.flox/env/manifest.toml` | `base` image environment: three host CLIs + node + git + jq |
| `evals/hosts/withpkg/.flox/env/manifest.toml` | `withpkg` image environment: base + `flox-ai` + `flox/skills-flox` |
| `evals/hosts/PROBE.md` | Captured facts from Task 1: container `$HOME`, per-host headless flags, config-dir layout |
| `evals/hosts/lib/creds.py` | Credential minimization and throwaway-dir preparation |
| `evals/hosts/lib/cells.py` | The 8-cell matrix definition |
| `evals/hosts/lib/images.py` | Image build / existence check |
| `evals/hosts/run_matrix.py` | Runner: build, iterate cells, write results, summarize |
| `evals/hosts/prompts/trigger.txt` | The single Tier B prompt |
| `evals/hosts/test_creds.py` | Tests for credential minimization |
| `evals/hosts/test_cells.py` | Tests for the matrix definition |
| `evals/hosts/test_run_matrix.py` | Tests for the runner over mocked subprocesses |
| `evals/hosts/results/` | Run output (JSONL + per-cell logs) |
| `evals/hosts/README.md` | How to run it |

---

### Task 1: Probe the base image (resolves every unknown)

Builds the `base` image and captures the facts later tasks depend on: the container's `$HOME`, each host's headless-invocation flags, and each host's config-dir layout. No credentials, no tokens.

**Files:**
- Create: `evals/hosts/base/.flox/env/manifest.toml`
- Create: `evals/hosts/PROBE.md`

- [ ] **Step 1: Scaffold the environment, then write the manifest**

A hand-written `.flox/env/manifest.toml` is **not** a valid environment —
`flox containerize` fails with "Found a '.flox' directory but unable to locate
an 'env.json' in it." Scaffold first:

```bash
flox init -d evals/hosts/base -n hosts-base
```

Then write the manifest below to a temp file and apply it with
`flox edit -d evals/hosts/base -f <file>`, which validates and locks it.
Editing `manifest.toml` in place skips both.

Manifest contents (note `schema-version`, matching the repo's own manifest —
not `version = 1`):

```toml
schema-version = "1.13.0"

# Image `base` for the host-matrix smoke test: the three agent CLIs plus the
# Node toolchain the skills.sh install path needs. Deliberately contains NO
# skills — the native-plugin and npx cells install them at run time, and a
# skill present in the image would let a host discover it through a path
# other than the one under test.

[install]
claude-code.pkg-path = "flox/claude-code"
codex.pkg-path = "flox/codex"
opencode.pkg-path = "flox/opencode"
nodejs.pkg-path = "nodejs"
git.pkg-path = "git"
jq.pkg-path = "jq"

[options]
systems = ["x86_64-linux"]
```

- [ ] **Step 2: Build the image**

Run: `flox containerize -d evals/hosts/base --runtime docker -t flox-skills-hosts-base:probe`
Expected: ends with the image loaded into Docker; `docker images flox-skills-hosts-base` lists tag `probe`.

If this fails with `constraints for group 'toplevel' are too tight`, the four
agent packages cannot share one resolution group. Fix by giving each its own
group, then re-run this step:

```toml
claude-code.pkg-path = "flox/claude-code"
claude-code.pkg-group = "claude"
codex.pkg-path = "flox/codex"
codex.pkg-group = "codex"
opencode.pkg-path = "flox/opencode"
opencode.pkg-group = "opencode"
```

- [ ] **Step 3: Capture the container's HOME and host versions**

Run:
```bash
docker run --rm flox-skills-hosts-base:probe bash -lc \
  'echo "HOME=$HOME"; echo "USER=$(id -un)"; claude --version; codex --version; opencode --version; node --version; npx --version'
```
Expected: a `HOME=` line and five version strings. Record all of it in `PROBE.md`.

- [ ] **Step 4: Capture each host's headless-invocation flags**

Run:
```bash
docker run --rm flox-skills-hosts-base:probe bash -lc \
  'claude --help; echo "=== CODEX ==="; codex --help; echo "=== CODEX EXEC ==="; codex exec --help; echo "=== OPENCODE ==="; opencode --help'
```
Expected: usage text for each. From it, record in `PROBE.md` the exact non-interactive form for each host — for Claude it is `claude -p <prompt> --output-format json` (already used by `evals/run.py:137`); for Codex and OpenCode, write down the equivalent verbatim, plus whichever flag emits machine-readable output or a transcript.

- [ ] **Step 5: Capture each host's config-dir layout**

Run:
```bash
docker run --rm flox-skills-hosts-base:probe bash -lc \
  'for d in "$HOME/.claude" "$HOME/.codex" "$HOME/.config/opencode" "$HOME/.local/share/opencode"; do echo "=== $d"; ls -la "$d" 2>&1 | head -20; done'
```
Expected: mostly "No such file or directory" on a fresh image — that is the useful baseline. Record it; Task 5 diffs against it after an install to learn each host's real skill-discovery path.

- [ ] **Step 6: Write PROBE.md**

Create `evals/hosts/PROBE.md` with four sections — `## Container identity` (HOME, user, versions), `## Headless invocation` (one exact command line per host), `## Config dirs, fresh image` (the Step 5 output), `## Open` (anything the probe failed to answer). Every constant later tasks use comes from this file.

- [ ] **Step 7: Commit**

```bash
git add evals/hosts/base/.flox/env/manifest.toml evals/hosts/PROBE.md
git commit -m "test(hosts): base image env + probe of host CLIs"
```

---

### Task 2: Credential minimization

The security-critical unit. Isolated and fully testable offline.

**Files:**
- Create: `evals/hosts/lib/creds.py`
- Test: `evals/hosts/test_creds.py`

**Interfaces:**
- Produces: `minimize_claude(raw: dict) -> dict`, `prepare(dest: Path, claude_src: Path, codex_src: Path) -> None`, `assert_minimized(path: Path) -> None`, exception `CredentialError`.

- [ ] **Step 1: Write the failing tests**

Create `evals/hosts/test_creds.py`:

```python
"""Credential minimization — the only code that touches real secrets.

No network, no container, no live credential files: every test builds its
own fixture in a temp dir.
"""
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from lib import creds

FULL_CLAUDE = {
    "claudeAiOauth": {"accessToken": "sk-test", "refreshToken": "rt-test"},
    "mcpOAuth": {
        "sentry|abc": {"accessToken": "MUST-NOT-LEAK"},
        "linear-server|def": {"accessToken": "MUST-NOT-LEAK"},
    },
}
CODEX = {"auth_mode": "chatgpt", "tokens": {"access_token": "at-test"}}


class TestMinimizeClaude(unittest.TestCase):
    def test_keeps_only_the_subscription_block(self):
        out = creds.minimize_claude(FULL_CLAUDE)
        self.assertEqual(list(out), ["claudeAiOauth"])

    def test_drops_every_mcp_token(self):
        out = creds.minimize_claude(FULL_CLAUDE)
        self.assertNotIn("MUST-NOT-LEAK", json.dumps(out))

    def test_raises_when_subscription_block_missing(self):
        with self.assertRaises(creds.CredentialError):
            creds.minimize_claude({"mcpOAuth": {}})


class TestPrepare(unittest.TestCase):
    def _srcs(self, tmp):
        c = Path(tmp) / "src-claude.json"
        x = Path(tmp) / "src-codex.json"
        c.write_text(json.dumps(FULL_CLAUDE))
        x.write_text(json.dumps(CODEX))
        return c, x

    def test_written_claude_file_is_minimized(self):
        with TemporaryDirectory() as tmp:
            c, x = self._srcs(tmp)
            dest = Path(tmp) / "run"
            creds.prepare(dest, c, x)
            written = json.loads((dest / "claude" / ".credentials.json").read_text())
            self.assertEqual(list(written), ["claudeAiOauth"])

    def test_files_are_mode_600(self):
        with TemporaryDirectory() as tmp:
            c, x = self._srcs(tmp)
            dest = Path(tmp) / "run"
            creds.prepare(dest, c, x)
            for rel in ("claude/.credentials.json", "codex/auth.json"):
                mode = os.stat(dest / rel).st_mode & 0o777
                self.assertEqual(mode, 0o600, rel)

    def test_assert_minimized_rejects_a_fat_file(self):
        with TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps(FULL_CLAUDE))
            with self.assertRaises(creds.CredentialError):
                creds.assert_minimized(bad)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd evals/hosts && python3 -m unittest test_creds -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lib'`.

- [ ] **Step 3: Write the implementation**

Create `evals/hosts/lib/__init__.py` (empty file), then `evals/hosts/lib/creds.py`:

```python
"""Prepare a throwaway credential directory for one matrix run.

Only the Claude subscription block travels into a container. The live
~/.claude/.credentials.json also holds mcpOAuth tokens (Fellow, Linear,
Notion, Slack, Sentry); nothing in this matrix needs MCP, so none of them
leave the host. Codex's auth.json is OAuth too (auth_mode "chatgpt") and is
copied whole — it holds nothing but the Codex login.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

CLAUDE_SRC = Path.home() / ".claude" / ".credentials.json"
CODEX_SRC = Path.home() / ".codex" / "auth.json"


class CredentialError(RuntimeError):
    """Raised when credentials are missing, malformed, or under-minimized."""


def minimize_claude(raw: dict) -> dict:
    """Return only the subscription block. Everything else is dropped."""
    if "claudeAiOauth" not in raw:
        raise CredentialError(
            "no 'claudeAiOauth' block in Claude credentials — is the host logged in?"
        )
    return {"claudeAiOauth": raw["claudeAiOauth"]}


def _write_600(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh)
    os.chmod(path, 0o600)


def assert_minimized(path: Path) -> None:
    """Fail loudly unless the written Claude file carries exactly one key."""
    keys = list(json.loads(path.read_text()))
    if keys != ["claudeAiOauth"]:
        raise CredentialError(
            f"refusing to mount {path}: expected exactly ['claudeAiOauth'], got {keys}"
        )


def prepare(dest: Path, claude_src: Path = CLAUDE_SRC, codex_src: Path = CODEX_SRC) -> None:
    """Populate `dest` with per-run credential copies, mode 600."""
    for src in (claude_src, codex_src):
        if not src.exists():
            raise CredentialError(f"missing credential file: {src}")
    _write_600(dest / "claude" / ".credentials.json",
               minimize_claude(json.loads(claude_src.read_text())))
    _write_600(dest / "codex" / "auth.json", json.loads(codex_src.read_text()))
    assert_minimized(dest / "claude" / ".credentials.json")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd evals/hosts && python3 -m unittest test_creds -v`
Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add evals/hosts/lib/__init__.py evals/hosts/lib/creds.py evals/hosts/test_creds.py
git commit -m "test(hosts): credential minimization — claudeAiOauth only, never mcpOAuth"
```

---

### Task 3: The cell matrix

**Files:**
- Create: `evals/hosts/lib/cells.py`
- Create: `evals/hosts/prompts/trigger.txt`
- Test: `evals/hosts/test_cells.py`

**Interfaces:**
- Consumes: the headless-invocation lines recorded in `PROBE.md` (Task 1).
- Produces: frozen dataclass `Cell(id, host, method, image, install, list_cmd, expect, launch, snapshot_dirs)` and `CELLS: tuple[Cell, ...]`.

- [ ] **Step 1: Write the Tier B prompt**

Create `evals/hosts/prompts/trigger.txt`:

```
I have a Python project that pins Python 3.12 in .python-version and needs
PostgreSQL for local development. Write me the Flox manifest for it.
```

This is chosen so a loaded skill answers differently from a bare model: the skill's guidance produces versioned `pkg-path` entries (`python312`, `postgresql_<major>`) and wires postgres as a Flox service, where an unguided model typically emits a bare `python`/`postgres`.

- [ ] **Step 2: Write the failing tests**

Create `evals/hosts/test_cells.py`:

```python
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

    def test_cells_are_frozen(self):
        with self.assertRaises(Exception):
            CELLS[0].id = "mutated"


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd evals/hosts && python3 -m unittest test_cells -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lib.cells'`.

- [ ] **Step 4: Write the implementation**

Create `evals/hosts/lib/cells.py`. Replace each `<<<from PROBE.md>>>` marker with the exact line recorded in Task 1 — the file will not import cleanly until you do, which is deliberate:

```python
"""The 8-cell host x install-method matrix.

Cells are data. `install`, `list_cmd` and `launch` are shell fragments run
inside the container via `bash -lc`. `{prompt}` in `launch` is replaced with
the container path of the Tier B prompt file.

OpenCode has no native-plugin cell: it has no plugin-marketplace concept, and
the README routes it through skills.sh.
"""
from __future__ import annotations

from dataclasses import dataclass, field

REPO = "flox/flox-skills"


@dataclass(frozen=True)
class Cell:
    id: str
    host: str          # claude | codex | opencode
    method: str        # native | npx | flox-ai
    image: str         # base | withpkg
    install: str       # shell; empty for flox-ai cells (package ships in the image)
    list_cmd: str      # shell; Tier A passes when it exits 0 AND prints `expect`
    expect: str        # substring looked for in list_cmd stdout
    launch: str        # shell; must contain the literal {prompt}
    snapshot_dirs: tuple[str, ...] = field(default=())


CLAUDE_LAUNCH = 'claude -p "$(cat {prompt})" --output-format json'
CODEX_LAUNCH = "<<<from PROBE.md: headless Codex invocation, with {prompt}>>>"
OPENCODE_LAUNCH = "<<<from PROBE.md: headless OpenCode invocation, with {prompt}>>>"

CLAUDE_DIRS = ("$HOME/.claude",)
CODEX_DIRS = ("$HOME/.codex",)
OPENCODE_DIRS = ("$HOME/.config/opencode", "$HOME/.local/share/opencode")

CELLS: tuple[Cell, ...] = (
    Cell(
        id="claude-native", host="claude", method="native", image="base",
        install=f"claude plugin marketplace add {REPO} && claude plugin install flox@flox-skills",
        list_cmd="claude plugin list", expect="flox",
        launch=CLAUDE_LAUNCH, snapshot_dirs=CLAUDE_DIRS,
    ),
    Cell(
        id="claude-npx", host="claude", method="npx", image="base",
        install=f"npx --yes skills add {REPO}",
        list_cmd="claude plugin list", expect="flox",
        launch=CLAUDE_LAUNCH, snapshot_dirs=CLAUDE_DIRS,
    ),
    Cell(
        id="claude-flox-ai", host="claude", method="flox-ai", image="withpkg",
        install="",
        list_cmd="flox-ai search flox", expect="flox",
        launch="flox-ai launch claude -- " + CLAUDE_LAUNCH, snapshot_dirs=CLAUDE_DIRS,
    ),
    Cell(
        id="codex-native", host="codex", method="native", image="base",
        install=(f"git clone --depth 1 https://github.com/{REPO}.git /work/flox-skills "
                 "&& cd /work/flox-skills && codex plugin marketplace add . "
                 "&& codex plugin add flox@flox-skills"),
        list_cmd="codex plugin list", expect="flox",
        launch=CODEX_LAUNCH, snapshot_dirs=CODEX_DIRS,
    ),
    Cell(
        id="codex-npx", host="codex", method="npx", image="base",
        install=f"npx --yes skills add {REPO}",
        list_cmd="codex plugin list", expect="flox",
        launch=CODEX_LAUNCH, snapshot_dirs=CODEX_DIRS,
    ),
    Cell(
        id="codex-flox-ai", host="codex", method="flox-ai", image="withpkg",
        install="",
        list_cmd="flox-ai search flox", expect="flox",
        launch="flox-ai launch codex -- " + CODEX_LAUNCH, snapshot_dirs=CODEX_DIRS,
    ),
    Cell(
        id="opencode-npx", host="opencode", method="npx", image="base",
        install=f"npx --yes skills add {REPO}",
        list_cmd="ls $HOME/.config/opencode/skills $HOME/.local/share/opencode/skills 2>/dev/null",
        expect="flox",
        launch=OPENCODE_LAUNCH, snapshot_dirs=OPENCODE_DIRS,
    ),
    Cell(
        id="opencode-flox-ai", host="opencode", method="flox-ai", image="withpkg",
        install="",
        list_cmd="flox-ai search flox", expect="flox",
        launch="flox-ai launch opencode -- " + OPENCODE_LAUNCH, snapshot_dirs=OPENCODE_DIRS,
    ),
)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd evals/hosts && python3 -m unittest test_cells -v`
Expected: PASS, 9 tests. If `test_every_cell_has_a_launch_command_with_a_prompt_slot` fails, a `<<<from PROBE.md>>>` marker is still unreplaced.

- [ ] **Step 6: Commit**

```bash
git add evals/hosts/lib/cells.py evals/hosts/prompts/trigger.txt evals/hosts/test_cells.py
git commit -m "test(hosts): define the 8-cell host x install-method matrix"
```

---

### Task 4: Image build helper

**Files:**
- Create: `evals/hosts/lib/images.py`
- Test: `evals/hosts/test_run_matrix.py` (created here, extended in Task 5)

**Interfaces:**
- Produces: `image_tag(name: str, version: str) -> str`, `image_exists(tag: str) -> bool`, `build(name: str, version: str, rebuild: bool = False) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `evals/hosts/test_run_matrix.py`:

```python
"""Runner tests over mocked subprocesses — no docker, no flox, no API spend."""
import subprocess
import unittest
from unittest.mock import patch

from lib import images


class TestImages(unittest.TestCase):
    def test_tag_is_name_and_version(self):
        self.assertEqual(images.image_tag("base", "20260727"),
                         "flox-skills-hosts-base:20260727")

    @patch("lib.images.subprocess.run")
    def test_image_exists_is_true_when_docker_prints_an_id(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="abc123\n", stderr="")
        self.assertTrue(images.image_exists("flox-skills-hosts-base:x"))

    @patch("lib.images.subprocess.run")
    def test_image_exists_is_false_when_docker_prints_nothing(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="\n", stderr="")
        self.assertFalse(images.image_exists("flox-skills-hosts-base:x"))

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
        self.assertIn("containerize", run.call_args[0][0])

    @patch("lib.images.image_exists", return_value=False)
    @patch("lib.images.subprocess.run")
    def test_build_raises_on_failure(self, run, _exists):
        run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
        with self.assertRaises(images.BuildError):
            images.build("base", "20260727")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd evals/hosts && python3 -m unittest test_run_matrix -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lib.images'`.

- [ ] **Step 3: Write the implementation**

Create `evals/hosts/lib/images.py`:

```python
"""Build the two matrix images with `flox containerize`."""
from __future__ import annotations

import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


class BuildError(RuntimeError):
    """Raised when `flox containerize` fails."""


def image_tag(name: str, version: str) -> str:
    return f"flox-skills-hosts-{name}:{version}"


def image_exists(tag: str) -> bool:
    proc = subprocess.run(["docker", "images", "-q", tag],
                          capture_output=True, text=True)
    return bool(proc.stdout.strip())


def build(name: str, version: str, rebuild: bool = False) -> str:
    """Build image `name` unless it already exists. Returns the tag."""
    tag = image_tag(name, version)
    if image_exists(tag) and not rebuild:
        return tag
    cmd = ["flox", "containerize", "-d", str(HERE / name),
           "--runtime", "docker", "-t", f"{name}:{version}"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise BuildError(f"containerize {name} failed: {proc.stderr.strip()}")
    return tag
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd evals/hosts && python3 -m unittest test_run_matrix -v`
Expected: PASS, 6 tests.

- [ ] **Step 5: Verify the real tag matches what the helper predicts**

Run: `docker images --format '{{.Repository}}:{{.Tag}}' | grep flox-skills-hosts`
Expected: the tag printed matches `image_tag("base", ...)`. If `flox containerize -t base:X` produces a repository name other than `flox-skills-hosts-base`, correct `image_tag` and the `-t` argument together so they agree, and re-run Step 4.

- [ ] **Step 6: Commit**

```bash
git add evals/hosts/lib/images.py evals/hosts/test_run_matrix.py
git commit -m "test(hosts): image build helper over flox containerize"
```

---

### Task 5: The runner

**Files:**
- Create: `evals/hosts/run_matrix.py`
- Create: `evals/hosts/README.md`
- Modify: `evals/hosts/test_run_matrix.py` (append the runner tests)

**Interfaces:**
- Consumes: `lib.creds.prepare`, `lib.cells.CELLS`, `lib.images.build`.
- Produces: `docker_cmd(cell, tag, creds_dir, prompt_path) -> list[str]`, `run_cell(cell, tag, creds_dir, dry_run=False) -> dict`, `main(argv) -> int`.

- [ ] **Step 1: Write the failing tests**

Append to `evals/hosts/test_run_matrix.py`:

```python
import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import run_matrix
from lib.cells import CELLS


class TestDockerCmd(unittest.TestCase):
    def test_mounts_creds_read_write_and_prompt_read_only(self):
        cmd = run_matrix.docker_cmd(CELLS[0], "img:1", Path("/tmp/run"), Path("/tmp/p.txt"))
        joined = " ".join(cmd)
        self.assertIn("--rm", joined)
        self.assertIn("/tmp/run/claude:", joined)
        self.assertIn(":ro", joined)

    def test_never_passes_an_api_key(self):
        cmd = run_matrix.docker_cmd(CELLS[0], "img:1", Path("/tmp/run"), Path("/tmp/p.txt"))
        joined = " ".join(cmd)
        self.assertNotIn("ANTHROPIC_API_KEY", joined)
        self.assertNotIn("OPENAI_API_KEY", joined)


class TestRunCell(unittest.TestCase):
    def test_dry_run_invokes_nothing(self):
        with patch("run_matrix.subprocess.run") as run:
            out = run_matrix.run_cell(CELLS[0], "img:1", Path("/tmp/run"), dry_run=True)
            run.assert_not_called()
        self.assertEqual(out["tier_a"], "dry-run")

    @patch("run_matrix.subprocess.run")
    def test_tier_a_passes_when_list_cmd_prints_expect(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="flox@flox-skills", stderr="")
        out = run_matrix.run_cell(CELLS[0], "img:1", Path("/tmp/run"))
        self.assertEqual(out["tier_a"], "pass")

    @patch("run_matrix.subprocess.run")
    def test_tier_a_fails_when_expect_absent(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="no plugins", stderr="")
        out = run_matrix.run_cell(CELLS[0], "img:1", Path("/tmp/run"))
        self.assertEqual(out["tier_a"], "fail")

    @patch("run_matrix.subprocess.run")
    def test_auth_failure_is_reported_distinctly(self, run):
        run.return_value = subprocess.CompletedProcess(
            [], 1, stdout="", stderr="Invalid API key · Please run /login")
        out = run_matrix.run_cell(CELLS[0], "img:1", Path("/tmp/run"))
        self.assertEqual(out["tier_b"], "auth-error")

    @patch("run_matrix.subprocess.run", side_effect=RuntimeError("boom"))
    def test_a_crashing_cell_records_and_does_not_raise(self, _run):
        out = run_matrix.run_cell(CELLS[0], "img:1", Path("/tmp/run"))
        self.assertEqual(out["tier_a"], "error")


class TestResults(unittest.TestCase):
    def test_results_are_one_json_object_per_line(self):
        rows = [{"cell": "a", "tier_a": "pass"}, {"cell": "b", "tier_a": "fail"}]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.jsonl"
            run_matrix.write_results(path, rows)
            lines = path.read_text().strip().splitlines()
        self.assertEqual([json.loads(x)["cell"] for x in lines], ["a", "b"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd evals/hosts && python3 -m unittest test_run_matrix -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'run_matrix'`.

- [ ] **Step 3: Write the implementation**

Create `evals/hosts/run_matrix.py`:

```python
#!/usr/bin/env python3
"""Host-matrix smoke test runner.

One attempt per cell, pass/fail — not a rate. See DESIGN.md.

    python3 run_matrix.py --dry-run          # print the plan, invoke nothing
    python3 run_matrix.py                    # full run (needs credentials)
    python3 run_matrix.py --cells claude-native,codex-npx
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from lib import creds, images
from lib.cells import CELLS, Cell

HERE = Path(__file__).resolve().parent
PROMPT = HERE / "prompts" / "trigger.txt"
RESULTS = HERE / "results"
CONTAINER_PROMPT = "/prompt.txt"
AUTH_MARKERS = ("/login", "invalid api key", "unauthorized", "not logged in",
                "authentication", "expired")


def docker_cmd(cell: Cell, tag: str, creds_dir: Path, prompt: Path) -> list[str]:
    """Build the `docker run` line for one cell. Credentials mount rw (OAuth
    refresh writes in place); the prompt mounts ro. No API keys, ever."""
    return [
        "docker", "run", "--rm",
        "-v", f"{creds_dir}/claude:/root/.claude:rw",
        "-v", f"{creds_dir}/codex:/root/.codex:rw",
        "-v", f"{prompt}:{CONTAINER_PROMPT}:ro",
        tag, "bash", "-lc",
    ]


def _sh(cmd: list[str], script: str, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd + [script], capture_output=True, text=True, timeout=timeout)


def _looks_like_auth_failure(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in AUTH_MARKERS)


def run_cell(cell: Cell, tag: str, creds_dir: Path, dry_run: bool = False,
             timeout: int = 600) -> dict:
    """Run one cell. Never raises: every failure becomes a recorded verdict."""
    row = {"cell": cell.id, "host": cell.host, "method": cell.method,
           "image": cell.image, "tier_a": "dry-run", "tier_b": "dry-run",
           "evidence": "", "notes": ""}
    if dry_run:
        row["notes"] = " && ".join(x for x in (cell.install, cell.list_cmd) if x)
        return row

    base = docker_cmd(cell, tag, creds_dir, PROMPT)
    try:
        # Tier A — install, then prove the host can see the skill. No credentials
        # are needed for this half, so it still answers when Tier B is blocked.
        script = " && ".join(x for x in (cell.install, cell.list_cmd) if x)
        a = _sh(base, script, timeout)
        row["evidence"] = (a.stdout or a.stderr)[-2000:]
        row["tier_a"] = "pass" if (a.returncode == 0 and cell.expect in a.stdout) else "fail"

        # Tier B — one prompt. Only attempted when the skill actually loaded.
        if row["tier_a"] != "pass":
            row["tier_b"] = "skipped"
            row["notes"] = "tier A did not pass; trigger not attempted"
            return row
        launch = cell.launch.format(prompt=CONTAINER_PROMPT)
        b = _sh(base, " && ".join(x for x in (cell.install, launch) if x), timeout)
        combined = b.stdout + b.stderr
        if _looks_like_auth_failure(combined):
            row["tier_b"] = "auth-error"
            row["notes"] = "credential problem, not a skill problem"
        elif b.returncode != 0:
            row["tier_b"] = "fail"
        else:
            row["tier_b"] = "pass"
        row["evidence"] = combined[-4000:]
    except subprocess.TimeoutExpired:
        row["tier_a"], row["tier_b"] = "timeout", "timeout"
    except Exception as exc:  # a broken cell must not take the run down
        row["tier_a"], row["tier_b"] = "error", "error"
        row["notes"] = f"{type(exc).__name__}: {exc}"
    return row


def write_results(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def summarize(rows: list[dict]) -> str:
    lines = ["", f"{'cell':<20} {'tier A (load)':<14} {'tier B (trigger)':<16}", "-" * 52]
    for r in rows:
        lines.append(f"{r['cell']:<20} {r['tier_a']:<14} {r['tier_b']:<16}")
    passed = sum(1 for r in rows if r["tier_a"] == "pass" and r["tier_b"] == "pass")
    lines += ["-" * 52, f"{passed}/{len(rows)} cells fully green", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print the plan, invoke nothing")
    ap.add_argument("--cells", help="comma-separated cell ids (default: all)")
    ap.add_argument("--rebuild", action="store_true", help="force image rebuild")
    ap.add_argument("--version", default=datetime.now(timezone.utc).strftime("%Y%m%d"))
    args = ap.parse_args(argv)

    selected = [c for c in CELLS if not args.cells or c.id in args.cells.split(",")]
    if not selected:
        print("no cells selected", file=sys.stderr)
        return 2

    tags = {}
    if not args.dry_run:
        for name in sorted({c.image for c in selected}):
            tags[name] = images.build(name, args.version, rebuild=args.rebuild)

    run_dir = Path(tempfile.mkdtemp(prefix="hostmatrix-"))
    try:
        if not args.dry_run:
            creds.prepare(run_dir)
        rows = []
        for cell in selected:
            cell_creds = run_dir / cell.id
            if not args.dry_run:
                shutil.copytree(run_dir / "claude", cell_creds / "claude")
                shutil.copytree(run_dir / "codex", cell_creds / "codex")
            rows.append(run_cell(cell, tags.get(cell.image, "dry-run"),
                                 cell_creds, dry_run=args.dry_run))
            print(f"{cell.id}: {rows[-1]['tier_a']} / {rows[-1]['tier_b']}")
        out = RESULTS / f"{args.version}.jsonl"
        write_results(out, rows)
        print(summarize(rows))
        print(f"results: {out}")
        return 0
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd evals/hosts && python3 -m unittest test_run_matrix -v`
Expected: PASS, 13 tests.

- [ ] **Step 5: Verify the dry run spends nothing**

Run: `cd evals/hosts && python3 run_matrix.py --dry-run`
Expected: 8 lines, each `<cell>: dry-run / dry-run`, then the summary and a results path. No Docker containers started (`docker ps -a` unchanged), no credential directory left behind (`ls /tmp/hostmatrix-*` → no such file).

- [ ] **Step 6: Write the README**

Create `evals/hosts/README.md` covering: what the matrix proves and what it does not (smoke test, not a rate); the two images; `--dry-run` first; that a full run needs a logged-in Claude and Codex on the host and spends subscription rate limit, not dollars; that only `.claudeAiOauth` ever leaves the machine; and that this is deliberately not in CI.

- [ ] **Step 7: Commit**

```bash
git add evals/hosts/run_matrix.py evals/hosts/README.md evals/hosts/test_run_matrix.py
git commit -m "test(hosts): matrix runner with dry-run, per-cell isolation, auth-vs-skill verdicts"
```

---

### Task 6: The withpkg image and a real Tier A run

First contact with live hosts. No credentials yet — Tier A only.

**Files:**
- Create: `evals/hosts/withpkg/.flox/env/manifest.toml`
- Modify: `evals/hosts/PROBE.md` (append findings)
- Modify: `evals/hosts/lib/cells.py` (pin discovery paths learned here)

- [ ] **Step 1: Write the withpkg manifest**

Create `evals/hosts/withpkg/.flox/env/manifest.toml`:

```toml
version = 1

# Image `withpkg`: everything in `base`, plus the flox-ai launcher and the
# PUBLISHED skills-flox package — so the flox-ai cells exercise the exact
# artifact a consumer installs, not a working copy.

[install]
claude-code.pkg-path = "flox/claude-code"
codex.pkg-path = "flox/codex"
opencode.pkg-path = "flox/opencode"
nodejs.pkg-path = "nodejs"
git.pkg-path = "git"
jq.pkg-path = "jq"
flox-ai.pkg-path = "flox/flox-ai"
skills-flox.pkg-path = "flox/skills-flox"
skills-flox.version = "1.0.0"

[options]
systems = ["x86_64-linux"]
```

- [ ] **Step 2: Build it**

Run: `flox containerize -d evals/hosts/withpkg --runtime docker -t withpkg:$(date -u +%Y%m%d)`
Expected: image loaded. If resolution fails, apply the same per-package `pkg-group` split shown in Task 1 Step 2.

- [ ] **Step 3: Confirm the skills tree is where flox-ai expects it**

Run:
```bash
docker run --rm flox-skills-hosts-withpkg:$(date -u +%Y%m%d) bash -lc \
  'ls $FLOX_ENV/share/flox/*/flox; flox-ai doctor; flox-ai search flox'
```
Expected: `claude`, `codex`, `opencode`, `pi` subtrees each containing the plugin; `doctor` reports launch-ready; `search flox` lists both skills. Record the output in `PROBE.md`.

- [ ] **Step 4: Run Tier A for every cell**

Run: `cd evals/hosts && python3 run_matrix.py --cells claude-native,claude-npx,codex-native,codex-npx,opencode-npx,claude-flox-ai,codex-flox-ai,opencode-flox-ai`

Expected: each cell prints a Tier A verdict. Tier B will report `auth-error` or `fail` — credentials are not mounted yet in this task, which is fine; only Tier A is being read here.

- [ ] **Step 5: Pin what the run revealed**

For every cell where Tier A failed on a wrong `list_cmd` or a wrong expected path, correct `lib/cells.py` from the captured `evidence` field in `results/<date>.jsonl` — particularly OpenCode's real skills directory, which is inferred, not observed. Re-run Step 4 until every Tier A verdict is `pass` or is a genuine product failure worth reporting.

- [ ] **Step 6: Run the unit tests again**

Run: `cd evals/hosts && python3 -m unittest discover -v`
Expected: PASS. If `test_cells` now fails, the structural expectations need updating alongside the pinned values.

- [ ] **Step 7: Commit**

```bash
git add evals/hosts/withpkg/.flox/env/manifest.toml evals/hosts/PROBE.md evals/hosts/lib/cells.py
git commit -m "test(hosts): withpkg image + Tier A verdicts pinned against live hosts"
```

---

### Task 7: The authenticated run (gated — do not start without Bill's go-ahead)

Spends subscription rate limit and mounts real OAuth tokens. Everything before this point is free and offline-testable.

**Files:**
- Create: `evals/hosts/results/<date>.jsonl` and per-cell logs
- Modify: AI-497 in Linear

- [ ] **Step 1: Confirm the host is logged in**

Run: `jq -r 'keys[]' ~/.claude/.credentials.json && jq -r .auth_mode ~/.codex/auth.json`
Expected: `claudeAiOauth` (plus `mcpOAuth`) and `chatgpt`. If either is missing, log in on the host first — the container cannot do an interactive login.

- [ ] **Step 2: Prove minimization before anything is mounted**

Run:
```bash
cd evals/hosts && python3 -c "
from pathlib import Path; from tempfile import mkdtemp; from lib import creds
d = Path(mkdtemp()); creds.prepare(d)
import json; print(list(json.loads((d/'claude'/'.credentials.json').read_text())))"
```
Expected: exactly `['claudeAiOauth']`. If anything else prints, stop — that is the leak this whole design exists to prevent.

- [ ] **Step 3: Run the full matrix**

Run: `cd evals/hosts && python3 run_matrix.py`
Expected: 8 cells, each with a Tier A and Tier B verdict, plus the summary table and a results path.

- [ ] **Step 4: Classify every non-pass**

For each cell that is not `pass/pass`, read its `evidence` and write one line in the run's notes assigning it to exactly one of: skill defect, host incompatibility, harness bug, or credential problem. A cell whose Tier B passed but could not prove skill invocation is recorded as `answer-shaped evidence only` — reported as such, never as a verification.

- [ ] **Step 5: Update AI-497**

Tick the checklist items the run supports, quoting the cell verdicts. Items with no green cell stay unticked. If Codex or OpenCode cannot prove invocation, say so plainly on the issue rather than ticking on the strength of a plausible answer.

- [ ] **Step 6: Commit the evidence**

```bash
git add evals/hosts/results
git commit -m "test(hosts): first authenticated host-matrix run"
```

---

## Self-Review

**Spec coverage.** Design → task: two images (T1, T6); throwaway creds volume with minimization (T2, runner T5, gate T7 Step 2); 8-cell matrix incl. OpenCode having no native cell (T3); Tier A auth-free (T5 `run_cell`, exercised T6); Tier B invocation evidence (T5, classified T7 Step 4); runner with `--dry-run`, per-cell independence, JSONL, summary (T5); auth-failure distinguished from skill failure (T5 `_looks_like_auth_failure`, tested); fixture-tested parsing logic (T2/T4/T5 unit tests); not in CI (README, T5 Step 6); `x86_64-linux` pinned in both manifests. Design risks 1–3 are what T1 and T6 exist to resolve; risk 4 (image size) is observed at T1 Step 2; risks 5–6 are handled by T7 Step 1 and the `auth-error` verdict.

**Placeholder scan.** The `<<<from PROBE.md>>>` markers in Task 3 are intentional and load-bearing: Task 1 produces those exact strings, and `test_cells` fails while a marker remains. Everything else carries real code and real commands.

**Type consistency.** `Cell` fields are used identically in `cells.py`, `run_matrix.py`, and both test modules. `images.image_tag`/`build` signatures match their call sites in `run_matrix.main`. `creds.prepare(dest, claude_src, codex_src)` matches its call in `main` and in `test_creds`. One thing Task 4 Step 5 exists to catch: `flox containerize -t <name>:<version>` may not produce a repository literally named `flox-skills-hosts-<name>`, so the tag helper and the `-t` argument get reconciled against real output before the runner depends on them.
