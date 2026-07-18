#!/usr/bin/env python3
"""Unit tests for screen.py's deterministic pieces.

Two concerns, both pure logic — no claude, no network, no API spend:

  1. hard_check() against REAL recorded answer excerpts (from earlier
     screen.py runs, saved in evals/results/screen*.json on the
     bill/ai-435-discriminating-evals branch before this harness landed on
     main). These are genuine model output, not contrived strings — using
     them proves a check works against how models actually phrase correct
     (and incorrect) answers, including the "shows the anti-pattern as a
     labeled counter-example" phrasing that sank several checks.
  2. _score_arm()'s handling of run_claude/judge's current return shapes
     (mocked at the subprocess boundary, same pattern as test_run.py).

    python3 -m unittest test_screen -v
"""
import json
import subprocess
import unittest
from unittest.mock import MagicMock, patch

import screen

# ---- real recorded answer excerpts -----------------------------------------
# Pulled verbatim from evals/results/screen*.json (screen-pass2-haiku.json,
# screen-sonnet.json, screen.json, screen-regression-n5-old.json) as recorded
# during the AI-435 investigation, before candidate ids were renamed/fixed.
# Truncated to 1200 chars, matching _score_arm's own excerpt length.

VARS_GOOD_HOOK = (
    'The `[vars]` section alone isn\'t quite right—it\'s for static values. To prepend to an *existing* environment variable like PATH, you need to reference its current value, which requires `hook.on-activate`:\n\n**flox.toml:**\n\n```toml\n[package]\nname = "your-project"\n\n[hook]\non-activate = """\nexport PATH="./node_modules/.bin:$PATH"\n"""\n```\n\n**Setup:**\n\n```bash\n# Initialize the environment (if you haven\'t already)\nflox init\n\n# Then just activate—the hook runs automatically\nflox activate\n```\n\n**Why this approach:**\n\n- `[vars]` sets static, literal values—it can\'t reference existing env vars like `$PAT'
    'H`.\n- `hook.on-activate` runs a shell snippet *after* the environment is set up, so you can reference the current PATH and prepend to it.\n- When you run `flox activate`, the hook exports the modified PATH for that shell session.\n- Locally-installed CLI tools (e.g., `npm`-installed binaries) are now found before system versions.\n\nThe hook approach is the standard way to modify existing environment variables in Flox. If you have other activation logic, you can add more commands to the same `on-activate` script.'
)

VARS_GOOD_PROFILE = (
    '`[vars]` is the wrong section for this — it sets variables to literal string values with no shell expansion, so `$PATH` would be treated as the literal text `$PATH`, not the current PATH value. You\'d clobber PATH rather than prepend to it.\n\nThe right section is `[profile]`, which is sourced directly into the interactive shell after activation, so variable references expand correctly.\n\n**Manifest** (`.flox/env/manifest.toml`):\n\n```toml\nversion = 1\n\n[profile]\ncommon = """\n  export PATH="$FLOX_ENV_PROJECT/node_modules/.bin:$PATH"\n"""\n```\n\n`$FLOX_ENV_PROJECT` is set by Flox to the absolute path of'
    " the directory containing `.flox/`, so this works regardless of what directory you're in when you run `flox activate`.\n\n**Commands:**\n\n```bash\n# Open the manifest in your editor\nflox edit\n\n# After saving, re-activate to pick up the change\n# (if already inside a flox activate shell, exit first)\nflox activate\n```\n\n**Why not `[hook]`?** The `[hook.on-activate]` script runs in a subshell — environment mutations there don't propagate back to your shell. `[profile]` is sourced, so they do.\n\n**Why `$FLOX_ENV_PROJECT` over `./`?** A bare `./node_modules/.bin` in profile would be evaluated at source ti"
)

VARS_BAD_ENV_SECTION = (
    'You\'re on the right track but **[vars] isn\'t quite right** — it would *replace* PATH entirely, not prepend to it. You need the **[env]** section, which allows shell variable expansion.\n\n## Solution\n\n**Manifest (flox.toml or .flox/env/manifest.toml):**\n\n```toml\n[options]\nsystems = ["x86_64-linux", "aarch64-darwin"]\n\n[package]\nname = "myproject"\n\n[vars]\n# Your other environment variables here\n\n[env]\nPATH = "./node_modules/.bin:${PATH}"\n```\n\n**Flox commands:**\n\n```bash\n# If starting fresh:\nflox init\n\n# Edit manifest to add the [env] section above, then:\nflox activate\n\n# Or update an existing envi'
    'ronment:\n# Edit .flox/env/manifest.toml, add [env] section, then:\nflox activate\n```\n\n## Why [env], not [vars]?\n\n- **[vars]** → simple key=value assignments; PATH would be overwritten, losing system paths\n- **[env]** → shell-aware setup; `${PATH}` expands to the current PATH, so `:${PATH}` appends to it\n\nThe syntax `"./node_modules/.bin:${PATH}"` prepends the local `.bin` directory while keeping all inherited PATH entries intact. When you activate the environment, locally-installed CLI tools will be found first.'
)

HOOK_RETURN_GOOD_DONT_TABLE = (
    '# Exiting Flox Activation Hooks Early\n\n**Use `return` instead of `exit`.** The activation hook is sourced into your shell, so `return` exits the script and returns control to your shell without killing it. `exit` would terminate your entire shell.\n\n## Complete Solution\n\n**manifest.toml:**\n```toml\n[env]\nactivation.on-enter = """\n  # Check prerequisites\n  if ! command -v required-tool &> /dev/null; then\n    echo "Error: required-tool not found in PATH"\n    return 1\n  fi\n  \n  if [ ! -f "$HOME/.config/myapp" ]; then\n    echo "Error: ~/.config/myapp not found"\n    return 1\n  fi\n  \n  # Proceed with '
    'normal setup if prerequisites pass\n  export MY_ENV_VAR="value"\n  echo "Environment activated successfully"\n"""\n```\n\n**Commands:**\n```bash\n# Create the environment\nflox init\n\n# Edit and add your manifest\nflox edit\n\n# Enter the environment (activation hook runs; early returns work correctly)\nflox activate\n```\n\n## Key Points\n\n| Do                                   | Don\'t                                    |\n|--------------------------------------|------------------------------------------|\n| Use `return 0` for success           | Use `exit 0` (kills the shell)           |\n| Use `return 1` for fa'
)

HOOK_RETURN_GOOD_PROSE = (
    '## Bailing out early from a Flox activation hook\n\n### The core rule\n\nThe `[hook] on-activate` script is **sourced** into the activating shell — it is not run in a subshell. That means:\n\n- **Use `return`** to leave early. It exits the sourced script and returns control to the shell.\n- **Never use `exit`** in a hook. Because the script is sourced, `exit` terminates the *parent shell session* — the user\'s terminal disappears.\n\n---\n\n### Complete example\n\n```toml\nversion = 1\n\n[hook]\non-activate = """\n  # Bail out early if Docker is not running\n  if ! docker info &>/dev/null 2>&1; then\n    echo "war'
    'ning: Docker is not available — skipping Docker setup" >&2\n    return 1\n  fi\n\n  # Prerequisite met — proceed with the rest of the hook\n  export DOCKER_BUILDKIT=1\n  echo "Docker is ready."\n"""\n```\n\n---\n\n### Flox commands\n\n```bash\n# Create a new environment (or work with an existing one)\nflox init\n\n# Open the manifest in your editor to add/edit the hook\nflox edit\n\n# Activate to test the hook\nflox activate\n```\n\n---\n\n### Why `return`, not `exit`\n\n| | `return` | `exit` |\n|---|---|---|\n| In a **sourced** script | Exits the script, shell continues | **Kills the parent shell** |\n| In a subshell (`bash'
)

LAYER_GOOD_BASELINE = (
    "## The answer: layer them, don't compose them\n\nFlox gives you two ways to get more than one environment's packages in play, and they're different mechanisms:\n\n- **Composition** (`[include]` in a manifest) merges the environments into a single new definition at build time. That's explicitly what you said you *don't* want.\n- **Layering** is runtime stacking: you activate one environment, then activate the next *inside* it. Both stay separate environments with their own hooks, vars, and services — they just stack. **The last (innermost) environment activated takes precedence**, because its `bin` "
    'is prepended to `PATH` on top of the outer one.\n\nSo layering is your answer, and precedence is simply your activation order.\n\n### The commands\n\n```bash\n# base is the outer layer; tools wins conflicts and drops you in an interactive shell\nflox activate -r myorg/base -- flox activate -r myorg/tools\n```\n\nTo flip precedence, flip the order:\n\n```bash\nflox activate -r myorg/tools -- flox activate -r myorg/base\n```\n\nIf you also want your local project environment on top of both:\n\n```bash\nflox activate -r myorg/base -- flox activate -r myorg/tools -- flox activate\n```\n\nVerify the stack and who won:\n\n`'
)

LAYER_GOOD_SKILLS = (
    '## The short answer\n\nWhat you\'re describing is **layering**, not composition — and layering has **no manifest**. It\'s a purely runtime construct, expressed entirely in the `flox activate` command. The only thing in Flox that takes two environments and puts them in one manifest is `[include]`, and that\'s exactly the build-time merge you said you don\'t want.\n\nSo the honest version of "the complete solution" is: the commands are the solution, and the manifest is optional — it only matters for making the two envs *layer cleanly* against each other.\n\n## The commands\n\nNest one activation inside anot'
    "her. **Activation order is the precedence order: the innermost (last) environment wins** — it's prepended to `PATH` last, and its `[vars]` overwrite the outer env's.\n\n```bash\n# myorg/base is the base layer; myorg/tools is layered on top and takes precedence\nflox activate -r myorg/base -- flox activate -r myorg/tools\n```\n\nThat drops you into an interactive shell with both active. Variations:\n\n```bash\n# Run a command under both layers instead of getting a subshell\nflox activate -r myorg/base -- flox activate -r myorg/tools -- ./run.sh\n\n# Layer your local project env on top of both remotes (local"
)

CONTAINERIZE_GOOD_BASELINE = (
    "`flox containerize --runtime docker` does exactly this — it builds an OCI image from your environment and hands it straight to the local Docker daemon. No Dockerfile, no registry, no `docker load` step. The one thing worth adding to your manifest is a `[containerize.config]` block, which is where the stuff you'd normally put in a Dockerfile (`CMD`, `EXPOSE`, `WORKDIR`, `USER`, `VOLUME`) lives instead.\n\n## Manifest\n\n`.flox/env/manifest.toml` — adapt the `[install]` section to whatever your environment actually needs; the container-relevant part is the last block.\n\n```toml\nversion = 1\n\n[install]"
    '\npython.pkg-path = "python311"\nflask.pkg-path = "python311Packages.flask"\n\n[vars]\nFLASK_APP = "app.py"\n\n[hook]\non-activate = \'\'\'\n  # runs on every activation, including container startup\n\'\'\'\n\n[options]\nsystems = ["x86_64-linux", "aarch64-linux", "aarch64-darwin", "x86_64-darwin"]\n\n# This is your Dockerfile, expressed as config.\n[containerize.config]\ncmd = ["python", "-m", "flask", "run", "--host=0.0.0.0"]\nexposed-ports = ["5000/tcp"]\nworking-dir = "/app"\nuser = "1000:1000"\nvolumes = ["/data"]\nstop-signal = "SIGTERM"\n```\n\nEvery field there is optional. If you omit `cmd`, `docker run -it <image>'
)

CONTAINERIZE_GOOD_SKILLS = (
    '`flox containerize --runtime docker` does exactly this — it builds an OCI image from your environment and hands it straight to the local Docker daemon. No Dockerfile, no registry, no intermediate tarball.\n\n## Manifest\n\nAdd a `[containerize.config]` section to `.flox/env/manifest.toml` to control what the image does when it runs:\n\n```toml\nversion = 1\n\n[install]\npython.pkg-path = "python311"\nflask.pkg-path = "python311Packages.flask"\n\n[vars]\nFLASK_APP = "app.py"\n\n[containerize.config]\nexposed-ports = ["8000/tcp"]\ncmd = ["python", "-m", "flask", "run", "--host=0.0.0.0", "--port=8000"]\nworking-dir'
    ' = "/app"\nuser = "1000:1000"\nstop-signal = "SIGTERM"\n\n[options]\nsystems = ["x86_64-linux", "aarch64-linux", "x86_64-darwin", "aarch64-darwin"]\n```\n\nSwap the `[install]` packages and `cmd` for whatever your environment actually runs. Every key in `[containerize.config]` is optional — with no `cmd`, `docker run -it <image>` just drops you into a shell with the environment activated.\n\n## Commands\n\n```bash\n# Build the image and load it into the local Docker daemon\nflox containerize --runtime docker --tag myapp:v1.0\n\n# Confirm it\'s there\ndocker images myapp\n\n# Run it\ndocker run -p 8000:8000 -v "$(p'
)

UV_GOOD_ORIGINAL_ORDER = (
    '# Complete Solution\n\n## Manifest (`flox.toml`)\n\n```toml\n[build]\nscript = \'\'\'\n  python -m venv "$FLOX_ENV_CACHE/venv"\n  uv pip install --python "$FLOX_ENV_CACHE/venv/bin/python" -r requirements.txt\n\'\'\'\n\n[profile.default.hook.on-activate]\nbash = \'\'\'\n  export PATH="$FLOX_ENV_CACHE/venv/bin:$PATH"\n  export VIRTUAL_ENV="$FLOX_ENV_CACHE/venv"\n\'\'\'\n```\n\n## Exact Command\n\n**In the activation hook or one-off:**\n```bash\nuv pip install --python "$FLOX_ENV_CACHE/venv/bin/python" -r requirements.txt\n```\n\n**To activate and use:**\n```bash\nflox activate\n```\n\n## Explanation\n\n- The `[build]` script creates the v'
    "env and installs requirements once during `flox init` or `flox update`\n- The `--python` flag tells uv to use the venv's Python interpreter explicitly (uv's default behavior)\n- The `on-activate` hook adds the venv's bin directory to PATH so subsequent commands use the venv automatically\n- `FLOX_ENV_CACHE` is Flox's standard location for build artifacts that persist across activations—the venv is cached there, not recreated each time\n\nIf you want to reinstall requirements after changing `requirements.txt`, use:\n```bash\nflox rerun-build\n```"
)

UV_GOOD_REORDERED_HAIKU = (
    'Here\'s the complete solution:\n\n**floxfile.toml:**\n```toml\n[options]\npython.version = "3.11"  # adjust as needed\n\n[packages]\nuv = "*"\n```\n\n**Activation hook command:**\n```bash\nuv pip install -r requirements.txt --python $FLOX_ENV_CACHE/venv/bin/python\n```\n\n**Explanation:**\n- `uv pip install` is the uv subcommand for installing packages\n- `--python` flag explicitly targets the venv\'s Python interpreter at `$FLOX_ENV_CACHE/venv/bin/python`\n- `$FLOX_ENV_CACHE` is automatically set by Flox to a persistent cache directory that survives `flox gc`\n- This installs all dependencies from `requirements.tx'
    't` into that specific venv, avoiding pollution of the system Python\n\n**To use in a hook**, add to your floxfile.toml:\n```toml\n[hook.activate]\ntext = """\nuv pip install -r requirements.txt --python $FLOX_ENV_CACHE/venv/bin/python\n"""\n```'
)

UV_GOOD_REORDERED_SONNET = (
    '```bash\nuv pip install -r requirements.txt --python $FLOX_ENV_CACHE/venv/bin/python\n```\n\nThat\'s the exact command. `uv pip install` accepts `--python` to target a specific interpreter (and by extension its venv), so you don\'t need to activate the venv first.\n\nIn context, your activation hook in `manifest.toml` would look like:\n\n```toml\n[profile]\ncommon = """\n  if [ ! -d "$FLOX_ENV_CACHE/venv" ]; then\n    uv venv "$FLOX_ENV_CACHE/venv"\n  fi\n  uv pip install -r "$FLOX_ENV/requirements.txt" --python "$FLOX_ENV_CACHE/venv/bin/python"\n  export PATH="$FLOX_ENV_CACHE/venv/bin:$PATH"\n  export VIRTUAL_'
    'ENV="$FLOX_ENV_CACHE/venv"\n"""\n```\n\nA few notes:\n- `$FLOX_ENV_CACHE` persists across activations but is per-environment, making it the right place for a venv.\n- `$FLOX_ENV` points to the environment\'s store path where you can co-locate `requirements.txt`, or use a repo-relative path like `"$PWD/requirements.txt"` if you prefer.\n- Setting `VIRTUAL_ENV` (without activating) is enough for most tools to discover the venv\'s packages.\n- The existence check on the venv dir keeps activation fast after the first run; if you want `uv pip install` to also be skipped when nothing changed, you\'d need a sen'
)

# Synthetic bad answers for checks with no recorded genuinely-wrong sample
# (every recorded run either used the correct construction, or was scored
# False purely by the check bug under audit). Each mirrors the failure mode
# named in the candidate's own "target" field.
HOOK_RETURN_BAD_EXIT_ONLY = (
    "Use `exit 1` to bail out of the hook early when a prerequisite is "
    "missing:\n\n```toml\n[hook]\non-activate = '''\n  if [ ! -f \"$HOME/.env\" ]; then\n"
    "    echo \"missing .env\" >&2\n    exit 1\n  fi\n'''\n```"
)
LAYER_BAD_USES_INCLUDE = (
    "Use `[include]` to bring both environments into one definition:\n\n"
    '```toml\n[include]\nenvironments = [\n  { remote = "myorg/base" },\n'
    '  { remote = "myorg/tools" },\n]\n```\n\nThen `flox activate` once.'
)
CONTAINERIZE_BAD_DOCKERFILE = (
    "Write a Dockerfile and build it:\n\n```dockerfile\nFROM ubuntu:22.04\n"
    "RUN apt-get update && apt-get install -y python3\nCOPY . /app\n"
    'CMD ["python3", "/app/main.py"]\n```\n\n'
    "```bash\ndocker build -t myapp .\n```"
)
UV_BAD_M_UV_INVOCATION = (
    'Run:\n```bash\n"$FLOX_ENV_CACHE/venv/bin/python" -m uv pip install '
    "-r requirements.txt\n```"
)


def _load_candidate(candidates_file: str, candidate_id: str) -> dict:
    """Load one candidate record by id from a jsonl file next to screen.py.

    Reads the file screen.py itself would load, so a future edit to a
    check's must_match/must_not_match is exercised by these tests without
    also needing to change here.
    """
    path = screen.HERE / candidates_file
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        if record["id"] == candidate_id:
            return record
    raise KeyError(f"{candidate_id!r} not found in {candidates_file}")


class TestHardCheckVarsNoInterpolation(unittest.TestCase):
    """trap-vars-no-interpolation: a correct answer illustrates the [vars]
    anti-pattern as a labeled counter-example, which false-fired the old
    proximity must_not_match \\[vars\\][\\s\\S]{0,200}PATH."""

    def setUp(self):
        self.candidate = _load_candidate(
            "candidates-all.jsonl", "trap-vars-no-interpolation"
        )

    def test_good_answer_using_hook_passes(self):
        self.assertTrue(screen.hard_check(
            VARS_GOOD_HOOK, self.candidate["must_match"],
            self.candidate["must_not_match"]))

    def test_good_answer_using_profile_passes(self):
        self.assertTrue(screen.hard_check(
            VARS_GOOD_PROFILE, self.candidate["must_match"],
            self.candidate["must_not_match"]))

    def test_bad_answer_using_nonexistent_env_section_fails(self):
        # Real recorded Haiku answer: recommends a nonexistent [env] section
        # instead of [profile]/[hook] -- genuinely wrong, must still fail.
        self.assertFalse(screen.hard_check(
            VARS_BAD_ENV_SECTION, self.candidate["must_match"],
            self.candidate["must_not_match"]))


class TestHardCheckHookReturnNotExit(unittest.TestCase):
    """trap-hook-return-not-exit: a correct answer documents `exit 0`/`exit 1`
    as the labeled Don't case (e.g. a Do/Don't table), which false-fired the
    old must_not_match \\bexit\\s+[0-9]\\b."""

    def setUp(self):
        self.candidate = _load_candidate(
            "candidates-all.jsonl", "trap-hook-return-not-exit"
        )

    def test_good_answer_with_dont_table_passes(self):
        self.assertTrue(screen.hard_check(
            HOOK_RETURN_GOOD_DONT_TABLE, self.candidate["must_match"],
            self.candidate["must_not_match"]))

    def test_good_answer_with_prose_warning_passes(self):
        self.assertTrue(screen.hard_check(
            HOOK_RETURN_GOOD_PROSE, self.candidate["must_match"],
            self.candidate["must_not_match"]))

    def test_bad_answer_recommending_exit_only_fails(self):
        self.assertFalse(screen.hard_check(
            HOOK_RETURN_BAD_EXIT_ONLY, self.candidate["must_match"],
            self.candidate["must_not_match"]))


class TestHardCheckLayerVsCompose(unittest.TestCase):
    """trap-layer-vs-compose-fixed: a correct answer explains that [include]
    is NOT what's wanted here, using it as a counter-example -- false-fired
    the must_not_match \\[include\\] that the superseded stretch-layer-vs-
    compose id still carries in the retired candidates.jsonl."""

    def setUp(self):
        self.candidate = _load_candidate(
            "candidates-all.jsonl", "trap-layer-vs-compose-fixed"
        )

    def test_good_answer_baseline_style_passes(self):
        self.assertTrue(screen.hard_check(
            LAYER_GOOD_BASELINE, self.candidate["must_match"],
            self.candidate["must_not_match"]))

    def test_good_answer_skills_style_passes(self):
        self.assertTrue(screen.hard_check(
            LAYER_GOOD_SKILLS, self.candidate["must_match"],
            self.candidate["must_not_match"]))

    def test_bad_answer_using_include_fails(self):
        self.assertFalse(screen.hard_check(
            LAYER_BAD_USES_INCLUDE, self.candidate["must_match"],
            self.candidate["must_not_match"]))


class TestHardCheckContainerizeNopush(unittest.TestCase):
    """trap-containerize-nopush-fixed: a correct answer's prose contains the
    common English word "from" (e.g. "builds an OCI image from your
    environment"), which the case-insensitive must_not_match FROM\\s+\\w
    false-fired on -- it was meant to catch a Dockerfile FROM line, not
    ordinary prose."""

    def setUp(self):
        self.candidate = _load_candidate(
            "candidates-all.jsonl", "trap-containerize-nopush-fixed"
        )

    def test_good_answer_baseline_style_passes(self):
        self.assertTrue(screen.hard_check(
            CONTAINERIZE_GOOD_BASELINE, self.candidate["must_match"],
            self.candidate["must_not_match"]))

    def test_good_answer_skills_style_passes(self):
        self.assertTrue(screen.hard_check(
            CONTAINERIZE_GOOD_SKILLS, self.candidate["must_match"],
            self.candidate["must_not_match"]))

    def test_bad_answer_authoring_dockerfile_fails(self):
        self.assertFalse(screen.hard_check(
            CONTAINERIZE_BAD_DOCKERFILE, self.candidate["must_match"],
            self.candidate["must_not_match"]))


class TestHardCheckUvVenvInvocation(unittest.TestCase):
    """trap-uv-venv-invocation: the literal substring must_match
    "uv pip install --python" required --python to immediately follow the
    subcommand, but correct answers commonly write the -r flag first
    (`uv pip install -r requirements.txt --python ...`) -- a prefix-only
    false negative."""

    def setUp(self):
        self.candidate = _load_candidate(
            "candidates-all.jsonl", "trap-uv-venv-invocation"
        )

    def test_good_answer_original_flag_order_passes(self):
        self.assertTrue(screen.hard_check(
            UV_GOOD_ORIGINAL_ORDER, self.candidate["must_match"],
            self.candidate["must_not_match"]))

    def test_good_answer_reordered_flags_passes_haiku(self):
        self.assertTrue(screen.hard_check(
            UV_GOOD_REORDERED_HAIKU, self.candidate["must_match"],
            self.candidate["must_not_match"]))

    def test_good_answer_reordered_flags_passes_sonnet(self):
        self.assertTrue(screen.hard_check(
            UV_GOOD_REORDERED_SONNET, self.candidate["must_match"],
            self.candidate["must_not_match"]))

    def test_bad_answer_using_m_uv_invocation_fails(self):
        self.assertFalse(screen.hard_check(
            UV_BAD_M_UV_INVOCATION, self.candidate["must_match"],
            self.candidate["must_not_match"]))


class TestDefaultCandidatesFileExcludesStaleEntries(unittest.TestCase):
    """The retired candidates.jsonl carried stretch-layer-vs-compose and
    stretch-containerize-nopush with the same false-firing must_not_match
    patterns audited above, under different ids, and screen.py's --candidates
    default pointed at it. candidates-all.jsonl is a superset that replaces
    both with fixed ids and adds the rest of the pass2/regression batches;
    it is now the default and the only candidates file this harness ships."""

    def test_default_candidates_path_is_the_consolidated_file(self):
        self.assertEqual(
            (screen.HERE / "candidates-all.jsonl").resolve(),
            screen.DEFAULT_CANDIDATES.resolve(),
        )

    def test_stale_duplicate_ids_are_absent_from_default_file(self):
        ids = {
            json.loads(line)["id"]
            for line in (screen.HERE / "candidates-all.jsonl").read_text().splitlines()
            if line.strip()
        }
        self.assertNotIn("stretch-layer-vs-compose", ids)
        self.assertNotIn("stretch-containerize-nopush", ids)


# A realistic `claude -p --output-format json` envelope, matching test_run.py's
# CLAUDE_JSON fixture (same harness, same envelope shape).
AGENT_JSON = {
    "result": "the manifest goes in [hook] with return, not exit",
    "total_cost_usd": 0.5,
    "duration_ms": 12000,
    "usage": {"output_tokens": 100},
}
JUDGE_JSON = {
    "result": '{"score": 5, "correct": true, "issues": []}',
    "total_cost_usd": 0.1,
    "duration_ms": 3000,
    "usage": {"output_tokens": 20},
}


class TestScoreArmMatchesRunClaudeShape(unittest.TestCase):
    """run.py's run_claude/judge return (result, err, meta) / (verdict, meta)
    -- cost/usage accounting (AI-459) -- not the (answer, err) / verdict
    shapes screen.py originally assumed when it was written against an
    older run.py. _score_arm must unpack the current shapes without
    raising, for both a single rep and reps>1 (the required multi-rep path)."""

    CANDIDATE = {
        "id": "t1", "area": "environments", "prompt": "p",
        "rubric": "r", "must_match": [], "must_not_match": [],
    }

    @patch("run.subprocess.run")
    def test_single_rep_does_not_raise_and_scores(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps(AGENT_JSON), stderr=""),
            MagicMock(returncode=0, stdout=json.dumps(JUDGE_JSON), stderr=""),
        ]
        result = screen._score_arm(self.CANDIDATE, "baseline", None, reps=1)
        self.assertTrue(result["hard_pass"])
        self.assertEqual(result["judge_score"], 5)
        self.assertTrue(result["judge_correct"])
        self.assertEqual(result["ok_reps"], 1)

    @patch("run.subprocess.run")
    def test_multi_rep_aggregates_across_reps(self, mock_run):
        # 2 reps x (agent call + judge call) = 4 subprocess invocations.
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps(AGENT_JSON), stderr=""),
            MagicMock(returncode=0, stdout=json.dumps(JUDGE_JSON), stderr=""),
            MagicMock(returncode=0, stdout=json.dumps(AGENT_JSON), stderr=""),
            MagicMock(returncode=0, stdout=json.dumps(JUDGE_JSON), stderr=""),
        ]
        result = screen._score_arm(self.CANDIDATE, "skills", None, reps=2)
        self.assertEqual(result["ok_reps"], 2)
        self.assertEqual(result["hard_pass_count"], 2)
        self.assertAlmostEqual(result["judge_score"], 5.0)

    @patch("run.subprocess.run")
    def test_cost_is_summed_from_agent_and_judge_meta(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps(AGENT_JSON), stderr=""),
            MagicMock(returncode=0, stdout=json.dumps(JUDGE_JSON), stderr=""),
        ]
        result = screen._score_arm(self.CANDIDATE, "baseline", None, reps=1)
        # AGENT_JSON's total_cost_usd (0.5) + JUDGE_JSON's (0.1).
        self.assertAlmostEqual(result["cost_usd"], 0.6)

    @patch("run.subprocess.run")
    def test_agent_error_does_not_raise_and_records_zero_cost_meta(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=1)
        result = screen._score_arm(self.CANDIDATE, "baseline", None, reps=1)
        self.assertFalse(result["hard_pass"])
        self.assertIn("error", result)
        self.assertEqual(result["cost_usd"], 0.0)


if __name__ == "__main__":
    unittest.main()
