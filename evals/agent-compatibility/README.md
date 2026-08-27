# Agent installation compatibility smoke tests

Does the Flox skill actually reach a user, whichever agent application they run
and whichever way they installed it? This suite crosses the three **agent
applications** we support — **Claude Code**, **Codex**, **OpenCode** — with the
three **installation methods** we ship: the native plugin and `npx skills add`,
both documented in the [top-level README](../../README.md), plus the Flox
package (`flox-ai` + `skills-flox`), which is not. Each combination is checked
inside a disposable container. Nothing is installed on your machine and no agent
CLI has to be present on it.

**This suite is manual-only.** It needs live developer credentials and spends
subscription rate limit, so it never runs unattended in CI. CI runs this
directory's unit tests and nothing else — see [What binds
CI](#what-binds-ci).

## What it measures

Two different questions, and the difference matters:

| | Question | What this suite gives you |
|---|---|---|
| **Load compatibility** | Did the skill install where that installation method puts it? | A deterministic answer that needs no credentials, and reads the verification command's output only. **Two of the eight cells go further** and ask the agent — see below. |
| **Trigger reliability** | How *often* does a prompt make the agent reach for the skill? | **Not measured here.** Each cell gets **one** prompt, scored pass/fail. |

Only `claude-native` and `codex-native` query the application itself (`claude
plugin list`, `codex plugin list --json`). The other six assert on the filesystem: the
skills.sh cells list the installer's destination and the Flox-package cells list
`$FLOX_ENV/share/flox/<agent>`, which is the `skills-flox` package's own payload
directory. Those six prove the files landed, not that the agent reads them —
and they match on `floxify`, so what they establish is that the install
delivered this repo's skills, not that `flox` in particular arrived. `flox` is
a substring of `floxify` and of `flox-skills`, so it cannot carry that
distinction; making it carry one needs a stricter match than these checks have.

The two native cells match `flox@flox-skills`, the exact id both CLIs print for
an installed plugin, and the Codex one asks in JSON. `expect="flox"` was a live
false pass there: `codex plugin list` prints a ``Marketplace `<name>` `` header
*and* a row per plugin with a `not installed` status, and this repo's
marketplace is named `flox-skills` with plugin `flox` — so registering the
marketplace alone satisfied it, and so would a `flox@flox-skills` match, since
that is the id the not-installed row prints too. `codex plugin list --json`
without `--available` reports `{"installed": [], "available": []}` in that
state and carries `"pluginId": "flox@flox-skills"` only once the plugin is
really installed. `claude plugin list` prints installed plugins only, so the
bare substring was merely loose there rather than wrong.

The trigger half is a *smoke test*: one attempt per cell. A pass says the
plumbing works end to end — the plugin installed, the agent started, the prompt
reached the model, and the process exited 0. It does **not** say the answer used
the skill; that is what `evidence_class` records separately, below. A failure
says something is broken. Neither is a rate. Reliability needs many repetitions
against a stable prompt set, and that lives in the [`flox` screening
tier](../flox/) (`screen.py`, n≥5), not here. Never quote a green matrix as a
trigger rate.

The trigger half is also weaker evidence than it looks: **none of the three
agent applications enumerates its loaded skills in headless mode**, so no cell
can prove *invocation*. `run_matrix.py` classifies what the transcript actually
supports and records the class next to the verdict:

| `evidence_class` | Means |
|---|---|
| `answer-shaped` | The answer carries at least two of the five fingerprints (`pkg-path`, `python312`, `postgresql_`, `[services]`, `flox activate`). Consistent with the skill — not proof of it, and see the caveat below. |
| `weak` | The agent ran and answered, but fewer than two surfaced. |
| `no-output` | Nothing came back — *or* the agent never launched, because the install step failed first. The `notes` field says which. |

The class is computed from the agent's own output only: the trigger container
re-runs the install first, and everything it prints is discarded at a marker
before the answer is judged. A cell whose trigger passes on exit status but
classifies below `answer-shaped` is flagged in its `notes`, because exit 0 is
not evidence.

**`answer-shaped` is a weaker signal than it looks.** The prompt asks for a Flox
manifest for a project that pins Python 3.12 and needs PostgreSQL, and
`pkg-path`, `[services]` and `python312` are mandatory syntax or supplied by the
prompt — so any answer that produces a valid manifest at all tends to clear the
two-hit threshold. What would make the class discriminating is a control arm
(the same prompt with no skill installed), which this suite does not run.
Treat `answer-shaped` as "did not obviously fail", not as evidence of skill use.

Harness noise is recorded in `notes` and never becomes the verdict. flox-ai
prints `not the flox-patched build` against a Flox-packaged Codex that *is*
patched — its detector byte-scans the Nix wrapper on `PATH` instead of the
binary that wrapper execs, and injection is not gated on the check — so the
warning says nothing about whether the skill was injected.

## The matrix — 8 cells

| | native plugin | skills.sh (`npx skills add`) | Flox package (`flox-ai` + `skills-flox`) |
|---|---|---|---|
| **Claude Code** | `claude-native` | `claude-npx` | `claude-flox-ai` |
| **Codex** | `codex-native` | `codex-npx` | `codex-flox-ai` |
| **OpenCode** | — | `opencode-npx` | `opencode-flox-ai` |

Cell ids are what `--cells` takes. OpenCode has no native cell: it has no
plugin-marketplace concept, and the top-level README routes it through
skills.sh. Claude Code is the control — if a Claude cell fails, suspect the
harness before the skill.

Two images keep the installation methods honest, built by `flox containerize`
from the two environments under `environments/`:

- **`base`** — the three agent CLIs, Node (for `npx`), `git`, `jq`, and **no
  skills**. A skill present in the image would let an agent discover it through
  a method other than the one under test. Serves the native and skills.sh cells.
- **`withpkg`** — `base` plus `flox/flox-ai` and the *published*
  `flox/skills-flox`, so the Flox-package cells exercise the artifact a consumer
  actually installs rather than a working copy.

The load check differs per installation method, because the methods land the
skill in different places and an agent's plugin list is the wrong question for a
skills install (`codex plugin list` correctly says "No marketplace plugins
found" after a *successful* `npx skills add`). That makes the skills.sh load
check the weakest of the three on purpose: it proves the files landed where the
installer put them, not that the agent reads them.

## The runtime: activate once

```bash
flox activate            # once, from anywhere in the repo
```

Every command below is then plain `python3`, on the interpreter this repo
declares in `.flox/env/manifest.toml` and pins in `manifest.lock` — the same one
CI uses (AI-509 Ticket 1). See [`../README.md`](../README.md) for the whole
story.

## Run

```bash
cd evals/agent-compatibility
python3 run_matrix.py --dry-run              # print the plan, invoke nothing
python3 run_matrix.py --load-only            # load half only — no credentials
python3 run_matrix.py                        # full run (needs credentials)
python3 run_matrix.py --cells claude-native  # one cell
python3 run_matrix.py --rebuild              # force the images to rebuild
python3 run_matrix.py --version 20260813     # name the results file and image tag
python3 run_matrix.py --timeout 900          # seconds per container (default 600)
```

A cell runs up to two containers, each on its own `--timeout` budget, so a
full run's worst case is sixteen of them.

Start with `--dry-run`. It costs nothing, starts no container, writes no
results, and prints exactly what each cell would run. `--load-only` is the next
cheapest step: it short-circuits before the model call, so it answers load
compatibility for every cell without a credential or a token.

Results are merged into `results/<version>.jsonl` (gitignored, mode 600,
`--version` defaults to today in UTC), one JSON object per cell — `cell`,
`agent`, `install_method`, `image`, `load`, `trigger`, `evidence_class`,
`load_evidence` and `trigger_evidence` transcript tails, and `notes` — plus a
summary table on stdout. A `--cells` subset run updates only the cells it ran;
the rest of the day's file survives, and an unknown cell id is an error rather
than a silent smaller run.

The merge is by cell id **and then by field**: a run that did not measure the
trigger half cannot overwrite one that did. `--version` defaults to today, so
without that rule the `--load-only` run recommended above would blank the
`trigger`, `evidence_class` and `trigger_evidence` of an authenticated run made
the same morning — exit 0, no warning. A preserved verdict says so in `notes`
and on stderr. Each cell is written as it finishes, so an interrupt during a
full run keeps every cell already paid for in rate limit.

The exit status says what happened, so a release check does not have to parse
the table. **The codes are not disjoint: 3 outranks every other code here**, so
a wrapper testing only `-eq 1` will miss a run that also leaked a credential.
Nothing else can co-occur — a run that never started ran no cells, and a bad
argument started nothing — so 3-over-everything is the whole ranking rule:

| code | meaning |
|---|---|
| 0 | everything the run attempted came out green |
| 1 | a cell did not |
| 2 | a bad `--cells`, `--version` or `--timeout` argument |
| 3 | a credential copy, or a container still holding one, survived cleanup — **outranks every code in this table**, including 5, because it is the only outcome here with a security consequence |
| 4 | every failing cell failed on **credentials**, not on the skill; kept distinct because that is the distinction the runner exists to make |
| 5 | the run never started — an image build failed, or the credentials could not be read. This covers a missing `docker` or `flox` and an unreadable, truncated or non-JSON credential file, not only the well-formed refusals |

## What a full run needs from you

- **Docker**, and the two images (built automatically on first run, ~1.5 GB
  each). **x86_64 Linux only** — both environments pin
  `systems = ["x86_64-linux"]`, so `flox containerize` will not build on an
  aarch64 machine.
- **A logged-in Claude Code and Codex on this machine.** The container cannot do
  an interactive login. Check with
  `jq -r 'keys[]' ~/.claude/.credentials.json` and
  `jq -r .auth_mode ~/.codex/auth.json`.
- **Subscription rate limit, not dollars.** Both authenticate by OAuth, so no
  `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` is involved and nothing is billed per
  token. Eight prompts is negligible against the limits.
- **Network**, for the image build and for the native/skills.sh installs.

If a cell reports `auth-error`, that is a credential problem — a stale copy or a
logged-out CLI — not a skill failure. The runner separates the two on purpose,
and a zero exit is never scored as an auth failure however the answer's prose
reads.

## What leaves your machine

Only your **Claude subscription token** and your **Codex login**, copied into a
temp directory that is deleted when the run ends.

**Credential minimization is enforced in code, not by convention.**
`~/.claude/.credentials.json` holds more than your Claude login — it also
carries OAuth tokens for every MCP server you have authenticated (Linear,
Notion, Slack, Sentry, and friends). None of that is needed here, so
[`lib/creds.py`](lib/creds.py):

- copies **only** the `claudeAiOauth` block, and refuses to proceed unless the
  file it wrote has exactly that one top-level key;
- drops `OPENAI_API_KEY` from the Codex copy and refuses to mount one that
  still carries it, so a per-token-billed key can never reach a container;
- writes both copies mode 600, gives each cell its own copy (OAuth tokens are
  refreshed in place; mounting your live files would let a container race your
  own session), and **never** bakes a credential into an image layer;
- mounts them **only into the trigger container**, and only the store
  belonging to that cell's own agent. The load check is credential-free and
  runs with nothing mounted. Note that every recorded successful load —
  including the 8/8 in `reports/` — was produced *with* both directories
  mounted, so the first `--load-only` run against logged-out CLIs is what
  confirms the two native cells still install without a login. If they do not,
  those two cells go red on the half this README calls deterministic.

**Residual exposure, stated plainly.** The trigger container re-runs the cell's
install step before launching the agent, and it holds one token: the one that
cell's own agent uses. So on the three skills.sh cells, `npx --yes skills add` —
unpinned code fetched from npm at run time, running as root — still executes
with that OAuth file readable, and on `opencode-npx` with neither. The load half
does not do this at all, which is where the installer is exercised for its own
sake; the trigger half needs the skill installed *and* the agent authenticated
in one container. Narrowing it further means splitting the trigger into two
containers, which is not done here — see the follow-ups in the PR description.

Gating on `cell.agent` was deferred once, on the grounds that "nothing prepares
an OpenCode credential at all, yet both OpenCode cells passed authenticated", so
gating would redden two cells. That premise does not hold: the shipped OpenCode
resolves credentials only from `$XDG_DATA_HOME/opencode/auth.json`, else
`~/.local/share/opencode/auth.json`, and a binary scan of 1.18.23 finds
`claudeAiOauth` zero times and no reference to either mounted path outside skill
discovery. Those cells consumed neither directory, so gating leaves their
credential state exactly as it was. **The retained report's two OpenCode rows
were never authenticated passes**: OpenCode serves the prompt from a built-in
provider that needs no login, so both cells pass with nothing mounted. Settled
by rerunning them on 2026-08-27 — `opencode auth list` reports `0 credentials`
inside the container and every step records `"cost":0` — see
[`reports/`](reports/).

The run directory is swept at the end — including files the container wrote as
root — and anything that still holds a credential is reported loudly as a
`WARNING`: a surviving copy, a directory the sweep could not read into, or a
container that could not be reclaimed. Every `docker run` here carries a
`--cidfile`, because `subprocess.run(timeout=)` kills the docker *client* while
the container is a child of the daemon: a timed-out cell used to leave a root
container running with the OAuth directory mounted read-write while the run
reported no leak.

## What binds CI

Only the unit tests, and they run in the `evals` job of
[`.github/workflows/evals.yml`](../../.github/workflows/evals.yml) on every PR:

```bash
cd evals/agent-compatibility
python3 -m unittest discover -s tests -t . -v   # the whole suite
python3 -m unittest tests.test_creds -v         # one module
```

They mock every subprocess — no Docker, no flox, no network, no credentials, no
model spend — and they are the only part of this directory that is automated.
They cover the matrix's structure, the runner's verdict logic (including the
regression cases below), and credential minimization.

The matrix itself is never run by CI and there is no scheduled job for it. It is
run by hand when something it crosses changes: a new agent application, a new
installation method, a new published `skills-flox`, or a release check.

A new published `skills-flox` means **editing the pin** in
`environments/withpkg/.flox/env/manifest.toml` and re-locking — `--rebuild` on
its own re-runs `flox containerize` against the same lock and produces a
byte-identical image, so the three flox-ai cells would keep exercising the
superseded artifact while this README says they exercise the one a consumer
installs. Check with `flox show flox/skills-flox`.

These properties are pinned by tests because a green cell once meant nothing:

- **Every verdict judges one step's output.** `run_matrix.py` emits a marker
  after the install step and discards everything before it — on *both* halves.
  An installer that merely *prints* "flox" and "floxify" (skills.sh renders a
  picker) must not satisfy the load check, and a `git clone` that fails with
  "Authentication failed" must not make the trigger an `auth-error`.
- **flox-ai forwards everything after `--` verbatim**, so a cell's launch
  command omits the binary name. Repeating it runs `claude claude -p …`, which
  exits 0 with the prompt dropped.
- **A subset run merges into the day's results**, rather than rewriting the file
  with only the cells it ran — and a `--dry-run` writes nothing at all.
- **A run that measured nothing overwrites nothing.** `--load-only` records
  `not-attempted`, and that cannot replace a trigger verdict an authenticated
  run of the same day already measured.
- **A measured verdict survives the other half.** A trigger timeout or crash
  leaves an already-passing `load` alone — and a *load* timeout records the
  trigger as `skipped`, because that container never started.
- **Every failure to start exits 5, not 1.** A missing `docker`, a truncated
  credential file, an unreadable one: each leaves its module as the one
  exception type the caller catches.
- **The leak alarm cannot fail open.** A directory the scan could not read and
  a container docker could not be asked about both count as positives.
- **The flox-ai patch warning is a note, not a verdict.** It once suppressed the
  evidence classifier outright, which excluded a healthy cell from the green
  count on every run.

## Layout

| Path | Role |
|---|---|
| `run_matrix.py` | The runner. Cells are data; this builds images, prepares credentials, runs each cell, and writes results |
| `lib/cells.py` | The matrix: one `Cell` per agent application × installation method, with the shell each one runs |
| `lib/creds.py` | Credential minimization — the only code that touches real secrets |
| `lib/images.py` | `flox containerize` wrapper and image tagging |
| `prompts/trigger.txt` | The prompt every trigger attempt uses |
| `environments/{base,withpkg}/` | The two Flox environments the images are built from |
| `tests/` | Unit tests. Mocked throughout; this is what CI runs |
| `reports/` | Retained evidence from real runs — see below |
| `results/` | Generated run output, **gitignored** |

## Retained evidence — lifecycle and owner

`reports/` holds hand-written analyses of real authenticated runs. A report is
kept only if it answers a question someone will ask again; a raw results file
never is (it is gitignored, and nobody without the credentials could reproduce
it anyway).

- **Owner:** the person who ran the matrix, named in the report's header. They
  own it until it is superseded.
- **Contents:** every report states the question it answers, the run conditions
  (image tags, agent CLI versions, package versions, date), and its conclusion.
  A report that cannot state all three should not be committed.
- **Lifecycle:** a report is superseded by the next full authenticated run that
  covers the same question, and the superseded one is **deleted** rather than
  archived — Git retains it, and two reports disagreeing about the current state
  of the matrix is worse than none. Keep at most one current report per
  question.

## Adding a cell

Cells are data, so a new agent application or installation method is an entry in
`CELLS` (`lib/cells.py`) plus its assertions:

1. Add the `Cell` — `id`, `agent`, `install_method`, `image`, the `install`
   shell, a `list_cmd`, the `expect` substring, and a `launch` containing the
   literal `{prompt}`. Prefer a `list_cmd` that asks the agent what it *has*;
   fall back to a filesystem check only when the agent offers no such query, as
   the six non-native cells do, and expect a weaker verdict when you do. Never
   assert on the installer's own output — that is the false pass `LIST_MARKER`
   exists to prevent.
2. If it needs packages the images lack, add them to
   `environments/<image>/.flox/env/manifest.toml` and rebuild with `--rebuild`.
3. Add its structural test to `tests/test_cells.py`. Every cell must prove it
   can be loaded and can be launched; the existing tests enumerate `CELLS`, so
   most of that is free.
4. Verify with `--dry-run`, then `--cells <id> --load-only`, before spending a
   model call on it.
