"""The 8-cell agent-application x installation-method matrix.

Cells are data. `install`, `list_cmd` and `launch` are shell fragments that
land in a script file mounted into the container — NOT in a `bash -lc`
argument, which the flox activation entrypoint re-quotes and breaks on any
`$( )`. `{prompt}` in `launch` is replaced with the container path of the
trigger prompt file.

Every command here was observed against a built image, not assumed:

    claude   -p "<prompt>" --output-format json   /  claude plugin list
    codex    exec "<prompt>" --json               /  codex plugin list
    opencode run "<message>" --format json        /  no list subcommand exists,
                                                     so the load check is a
                                                     filesystem check

OpenCode has no native-plugin cell: it has no plugin-marketplace concept, and
the top-level README routes it through skills.sh.
"""
from __future__ import annotations

from dataclasses import dataclass

REPO = "flox/flox-skills"

# Headless invocation, one form per agent application. `cat` runs inside a
# mounted script, where command substitution is safe.
CLAUDE_LAUNCH = 'claude -p "$(cat {prompt})" --output-format json'
CODEX_LAUNCH = 'codex exec "$(cat {prompt})" --json --skip-git-repo-check'
OPENCODE_LAUNCH = 'opencode run "$(cat {prompt})" --format json'

# flox-ai forwards args after `--` VERBATIM to the agent, so these omit the
# binary name. Repeating it runs `claude claude -p ...`, which exits 0 while
# silently dropping the prompt — a false pass.
CLAUDE_ARGS = '-p "$(cat {prompt})" --output-format json'
CODEX_ARGS = 'exec "$(cat {prompt})" --json --skip-git-repo-check'
OPENCODE_ARGS = 'run "$(cat {prompt})" --format json'

# skills.sh's own agent ids — NOT this matrix's agent names. `-a claude` is
# rejected with "Invalid agents: claude"; the id is `claude-code`.
NPX_AGENT = {"claude": "claude-code", "codex": "codex", "opencode": "opencode"}


# `npx skills add` is INTERACTIVE by default — it renders a picker and waits.
# `-a <agent> -s '*' -y` is the non-interactive form; `-g` installs at user
# level, since a container has no project to install into.
def npx_install(agent: str) -> str:
    return f"npx --yes skills add {REPO} -a {NPX_AGENT[agent]} -s '*' -y -g"


# Where skills.sh actually copies, observed per agent application: it knows
# Claude Code's native directory, but falls back to the generic skills.sh tree
# for the others.
#     claude-code -> ~/.claude/skills/{flox,floxify}
#     codex       -> ~/.agents/skills/{flox,floxify}
#     opencode    -> ~/.agents/skills/{flox,floxify}
#
# An agent's plugin list is the WRONG assertion for an npx cell — `codex plugin
# list` correctly says "No marketplace plugins found" after a successful
# skills install, because a skill is not a plugin.
#
# This makes the npx load check weaker than the other methods on purpose: it
# proves the files landed where the installer put them, not that the agent
# reads them. Pickup is only evidenced by the trigger half.
#
# `expect` is `floxify` on every filesystem cell, and `flox` would be useless
# there: it is a substring of `floxify`, of `flox-skills` and of the repo name,
# so it cannot distinguish "the flox skill landed" from "something with flox in
# the name is on the path". `floxify` is the unambiguous token, and the cost is
# that these six cells prove the INSTALL ran (`-s '*'` fetches both skills)
# rather than proving the `flox` skill specifically arrived. Distinguishing them
# needs a whole-path-component match against `ls -R` output nobody has captured;
# see the follow-ups in the PR description.
NPX_DEST = {
    "claude": "$HOME/.claude/skills",
    "codex": "$HOME/.agents/skills",
    "opencode": "$HOME/.agents/skills",
}


def npx_list(agent: str) -> str:
    return f'ls -R "{NPX_DEST[agent]}"'


# The images carry no findutils (`find: command not found`), so every
# filesystem assertion uses `ls -R`, which is coreutils.


def floxai_list(agent: str) -> str:
    """Load check for a flox-ai cell: does the packaged tree carry this agent's skills?

    NOT `flox-ai search flox` — that reports "no skills matched" even when
    `flox-ai doctor` sees four installed fragment dirs, so it answers a
    different question than "is the skill here". The per-agent layouts differ
    (claude and opencode nest under `skills/`, codex and pi are bare roots),
    so the check lists recursively rather than assuming a shape. `ls -R`
    rather than `find`: the images have no findutils.
    """
    return f'ls -R "$FLOX_ENV/share/flox/{agent}"'


@dataclass(frozen=True)
class Cell:
    id: str
    agent: str           # claude | codex | opencode — the agent application
    install_method: str  # native | npx | flox-ai
    image: str           # base | withpkg
    install: str         # shell; empty for flox-ai cells (package ships in the image)
    list_cmd: str        # shell; the load check passes when it exits 0 AND prints `expect`
    expect: str          # substring looked for in list_cmd stdout
    launch: str          # shell; must contain the literal {prompt}


CELLS: tuple[Cell, ...] = (
    Cell(
        id="claude-native", agent="claude", install_method="native", image="base",
        install=f"claude plugin marketplace add {REPO} && claude plugin install flox@flox-skills",
        list_cmd="claude plugin list", expect="flox",
        launch=CLAUDE_LAUNCH,
    ),
    Cell(
        id="claude-npx", agent="claude", install_method="npx", image="base",
        install=npx_install("claude"),
        list_cmd=npx_list("claude"), expect="floxify",
        launch=CLAUDE_LAUNCH,
    ),
    Cell(
        id="claude-flox-ai", agent="claude", install_method="flox-ai", image="withpkg",
        install="",
        list_cmd=floxai_list("claude"), expect="floxify",
        launch="flox-ai launch claude -- " + CLAUDE_ARGS,
    ),
    Cell(
        id="codex-native", agent="codex", install_method="native", image="base",
        install=(f"git clone --depth 1 https://github.com/{REPO}.git /work/flox-skills "
                 "&& cd /work/flox-skills && codex plugin marketplace add . "
                 "&& codex plugin add flox@flox-skills"),
        list_cmd="codex plugin list", expect="flox",
        launch=CODEX_LAUNCH,
    ),
    Cell(
        id="codex-npx", agent="codex", install_method="npx", image="base",
        install=npx_install("codex"),
        list_cmd=npx_list("codex"), expect="floxify",
        launch=CODEX_LAUNCH,
    ),
    Cell(
        id="codex-flox-ai", agent="codex", install_method="flox-ai", image="withpkg",
        install="",
        list_cmd=floxai_list("codex"), expect="floxify",
        launch="flox-ai launch codex -- " + CODEX_ARGS,
    ),
    Cell(
        id="opencode-npx", agent="opencode", install_method="npx", image="base",
        install=npx_install("opencode"),
        list_cmd=npx_list("opencode"), expect="floxify",
        launch=OPENCODE_LAUNCH,
    ),
    Cell(
        id="opencode-flox-ai", agent="opencode", install_method="flox-ai", image="withpkg",
        install="",
        list_cmd=floxai_list("opencode"), expect="floxify",
        launch="flox-ai launch opencode -- " + OPENCODE_ARGS,
    ),
)
