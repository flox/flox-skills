"""The 8-cell host x install-method matrix.

Cells are data. `install`, `list_cmd` and `launch` are shell fragments that
land in a script file mounted into the container — NOT in a `bash -lc`
argument, which the flox activation entrypoint re-quotes and breaks on any
`$( )` (see PROBE.md). `{prompt}` in `launch` is replaced with the container
path of the Tier B prompt file.

Every command here traces to PROBE.md:
  claude   -p "<prompt>" --output-format json   /  claude plugin list
  codex    exec "<prompt>" --json               /  codex plugin list
  opencode run "<message>" --format json        /  no list subcommand exists,
                                                   so Tier A is a file check

OpenCode has no native-plugin cell: it has no plugin-marketplace concept, and
the README routes it through skills.sh.
"""
from __future__ import annotations

from dataclasses import dataclass, field

REPO = "flox/flox-skills"

# Headless invocation, pinned by PROBE.md. `cat` runs inside a mounted
# script, where command substitution is safe.
CLAUDE_LAUNCH = 'claude -p "$(cat {prompt})" --output-format json'
CODEX_LAUNCH = 'codex exec "$(cat {prompt})" --json --skip-git-repo-check'
OPENCODE_LAUNCH = 'opencode run "$(cat {prompt})" --format json'

# OpenCode ships no list subcommand, so its Tier A is a filesystem search.
# The exact skills path is still unobserved — Task 6 pins it — so the check
# searches both candidate roots and prints whatever it finds.
OPENCODE_LIST = (
    'find "$HOME/.config/opencode" "$HOME/.local/share/opencode" '
    '-maxdepth 4 -iname "*flox*" 2>/dev/null'
)

CLAUDE_DIRS = ("$HOME/.claude",)
CODEX_DIRS = ("$HOME/.codex",)
OPENCODE_DIRS = ("$HOME/.config/opencode", "$HOME/.local/share/opencode")


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
        list_cmd=OPENCODE_LIST, expect="flox",
        launch=OPENCODE_LAUNCH, snapshot_dirs=OPENCODE_DIRS,
    ),
    Cell(
        id="opencode-flox-ai", host="opencode", method="flox-ai", image="withpkg",
        install="",
        list_cmd="flox-ai search flox", expect="flox",
        launch="flox-ai launch opencode -- " + OPENCODE_LAUNCH, snapshot_dirs=OPENCODE_DIRS,
    ),
)
