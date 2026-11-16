# Agent Stacks: turn an agent setup into a stack

The person asked you to turn their agent setup into an agent stack: a Flox environment, kept in a git repository of its own, that starts the same agent with the same skills and plugins here and on their other computers.
First you set up what onboarding needs, asking before you install anything.
Then the onboarding engine, `flox-agent init`, does the work: you run it, show what it says, and ask its questions.
When the flox-agent the Flox Catalog serves has no `init` yet, you make a simpler stack yourself instead, as [Without the engine](#without-the-engine) says.
Ask only what you have to.

Read this file whole before the first command, then follow it from start to finish.
SKILL.md, where this file names it, is the `flox` skill's own file, which sent you here.

Start by telling the person, in two or three lines, what happens: you check this machine and set up what is missing, asking first; then you read their setup, propose a stack, and build it once they agree; nothing of theirs is uploaded or published unless they choose that, though planning looks up their skills' sources on GitHub and in the Flox Catalog.

If they asked for something under Later, for a stack they already have, do 1 (set up), then go there, or, without the engine, to [Later, without the engine](#later-without-the-engine).

## Rules

- **Asking.** In Claude Code, ask with AskUserQuestion, every question of a step in one call, exactly as given here or by the engine (its `ask` and `shortcut` are in AskUserQuestion's shape), and never ask in prose.
  In other agents, ask in chat with the same words: the question, then each option numbered as `<label>: <description>`, label and description in full and unchanged, with no option added or left out.
  Every question of a step goes in one message.
  Then stop and wait for the answer.
  A reply that is none of the numbers or labels is never a yes: for the Create question it is an answer in their own words (see 3); for any other, ask again.
  Never pick an answer for the person.
- **Showing.** Show the engine's text as it prints it, in a `text` block, and the `summary` of its JSON the same way.
  Add one sentence at most.
  Make no tables of your own.
- **Running.** One command per shell call.
  Write `<bootstrap>` and every other path out in full: a variable set in one call is gone in the next.
  These commands use the network, and write under the home directory and to `/nix/store`.
  If your shell runs in a sandbox, the `flox` and engine commands need to run outside it: say why once, and request each through your agent's own approval prompt.
  Never change your sandbox or approval settings, and never suggest restarting the agent with its checks off.
  Where your shell tool takes a timeout, give 10 minutes: the first `init plan`, getting flox-agent and `init apply` can take minutes.
- **The person's commands.** You cannot answer a password prompt or finish a browser login.
  A command that needs one is the person's to run, in a terminal window of their own: not with Claude Code's `! <command>`, which has no terminal to prompt in either.
  Give them the command, wait until they say it finished, then check again.
- **Consent.** Install nothing, and change no setting, unless the person said yes to it.
  Nothing leaves the machine, and no file outside the stack is changed, unless the person picked an option that says so: `sync --publish --push` only after "Create it, on all my computers", or once they ask to share the stack and say yes to it (see Later); `shortcut` only after "Make … start it", and `shortcut --push` only after "Make … start it, on all my computers"; `promote` only for the packages they picked to share with an organization (see Later); without the engine, `flox push` only after "Create it, on all my computers".
- Never read, print or copy a secret: never run `flox auth token`, and never print `flox config -l` whole.
- Never add `--home` to an engine command.
- Never run `init` without a verb: with none, it asks its questions on a terminal, which your shell does not have.

## 1. Set up

Check each step before you do it, in this order.

### The system

```sh
uname -sm
```

- `Darwin arm64`: macOS on Apple silicon, `aarch64-darwin`.
- `Darwin x86_64`: run `sysctl -n sysctl.proc_translated`.
  `1` is a shell under Rosetta on Apple silicon: `aarch64-darwin`.
  Anything else is an Intel Mac: stop, and say flox-agent, which runs Agent Stacks, is not built for one yet; it runs on Apple silicon Macs and on x86_64 and aarch64 Linux.
- `Linux x86_64` or `Linux amd64`: `x86_64-linux`.
  `Linux aarch64` or `Linux arm64`: `aarch64-linux`.
- Anything else: stop.
  Agent Stacks runs on macOS and Linux, on x86_64 and aarch64.

`<system>` below is the one you found.

### Flox

Agent Stacks needs Flox 1.18 or later.
Run `flox --version`.
When no `flox` is found, try `/usr/local/bin/flox --version`, then `/usr/bin/flox --version`: a fresh install may not be on your `PATH` yet.

- **It runs.** The version is the first `<major>.<minor>` it prints, and 1.18 or later is enough (`1.19.0-rc.1` counts).
  If you cannot tell the version, carry on.
  If only `/usr/local/bin/flox` or `/usr/bin/flox` ran, put `PATH="<its directory>:$PATH" ` before every command from here on, engine commands too: the engine runs `flox` itself.
- **It is older than 1.18.** The person upgrades it with the tool that installed it, then you check again.
  Go by the path `command -v flox` prints (or the one that ran), not where a link leads: the macOS package links `/usr/local/bin/flox` into `/nix/store`.
  - a path under `/nix/`, `~/.nix-profile/` or `~/.local/state/nix/`: `nix profile upgrade '.*flox'`;
  - macOS, when `brew list --cask flox` succeeds: `brew upgrade --cask flox`;
  - other macOS: `curl -fsSL https://get.flox.dev | sh`;
  - Linux with `apt-get`: `sudo apt-get update && sudo apt-get install --only-upgrade flox`; with `dnf`: `sudo dnf upgrade flox`; with `yum`: `sudo yum update flox`;
  - anything else: as SKILL.md's "Installing Flox" says.
- **It fails.** Stop.
  Show what it printed, and say Flox needs repairing or reinstalling, as SKILL.md's "Installing Flox" says.
- **None is found.** Run `ls -ld /etc/flox-version /usr/local/bin/flox /usr/bin/flox`.
  If any of them is there, Flox looks installed but does not run, and you do not install over it: the person opens a new terminal and starts you again there, and if that does not help, repairs it in a terminal with `curl -fsSL https://get.flox.dev | FLOX_FORCE_INSTALL=1 sh`.
  If none is there, say Agent Stacks builds and runs on Flox, and that its installer asks for their password through sudo, so they run it themselves, in a terminal:
  `curl -fsSL https://get.flox.dev | sh`.
  SKILL.md's "Installing Flox" says what the script does, the systems it refuses, and the other ways to install.
  Then check again.

### FloxHub

```sh
flox auth status
```

- It exits 0: they are logged in.
- It could not reach FloxHub: say so, and carry on; the plan says if the login is missing.
- Otherwise they are logged out: the person runs `flox auth login` in a terminal, which opens a browser and waits for them.
  Then check again.

### Beta features and flox-agent

Check both, then ask about every one not set up yet in one call (one message, in other agents), and do what they said yes to, in this order.

**Beta features.**
`flox agent` commands, such as `flox agent launch`, need them.
They are on when `flox config -l | grep '^features'` shows `beta = true`.
If they are off, ask:

- Header `Beta`, question "Turn on Flox's beta features? `flox agent` commands need them."
  - `Turn them on (Recommended)`: "Runs `flox config --set features.beta true`, which changes your Flox configuration."
  - `Not now`: "Onboarding works without them, but `flox agent` commands do not run until you turn them on."

On yes, run `flox config --set features.beta true`.

**flox-agent**, which runs the onboarding, from a Flox environment of its own:

1. `printf '%s\n' "${XDG_CACHE_HOME:-$HOME/.cache}/agent-stacks/bootstrap"` prints `<bootstrap>`, the environment's directory.
2. `curl -fsSL https://downloads.agent-stacks.org/agent-stacks/by-commit/LATEST` prints the commit of the newest Agent Stacks build, 40 hex digits.
   `flox show flox/flox-agent` lists the catalog's versions as `flox/flox-agent@<version>`.
   `<version>` is the one ending `-g<hex>`, seven hex digits or more, that the commit starts with.
   When either command fails, or no version matches, there is no `<version>`: you take the catalog's newest, which may be one it cannot serve yet, and say so.
3. The environment's manifest is this, without the `flox-agent.version` line when there is no `<version>`:

   ```toml-fragment
   <format>

   [install]
   flox-agent.pkg-path = "flox/flox-agent"
   flox-agent.version = "<version>"

   [options]
   systems = ["<system>"]
   ```

   `<format>` is the first line of `<bootstrap>/.flox/env/manifest.toml` that starts `schema-version =` or `version =`, as `flox init` wrote it, else `schema-version = "1.10.0"`.
   flox-agent is set up when that file holds exactly this manifest already.

If it is not set up, ask:

- Header `flox-agent`, question "Get flox-agent `<version>`, which does the onboarding, from the Flox Catalog?", saying "the newest flox-agent" when there is no `<version>`.
  - `Get it (Recommended)`: "Makes the Flox environment `<bootstrap>`, holding only flox-agent, and downloads flox-agent into it. Nothing else on this computer changes."
  - `Not now`: "Onboarding stops here."

On `Not now`, stop once you have done what they said yes to.
On yes:

1. When `<bootstrap>/.flox/env.json` is not there, run `mkdir -p <bootstrap>`, then `flox init -d <bootstrap> -n agent-stacks-bootstrap --no-auto-setup`, and read `<format>` from the manifest it wrote.
2. Lock the manifest, which downloads flox-agent:

   ```sh
   flox edit -d <bootstrap> -f - <<'EOF'
   <the manifest>
   EOF
   ```

   If it fails, show what Flox said, and stop.

### The engine

```sh
env -u FLOX_EXTENSION_NAME flox activate -d <bootstrap> -m run -- flox-agent init --help
```

It exits 0 and prints a usage that starts `Usage: flox-agent init`.
If it prints flox-agent's general usage instead, `Usage: flox-agent [flags] <command>`, with no `init` among its commands, this flox-agent has no `init` yet: go to [Without the engine](#without-the-engine), in place of 2 to 5 and Later.
If it fails any other way, show what it printed, and stop.

From here on, `<engine>` is `env -u FLOX_EXTENSION_NAME flox activate -d <bootstrap> -m run -- flox-agent`, with `<bootstrap>` written out, and the `PATH` prefix in front when Flox needed one.
`<engine> init plan`, `init decide` and `init apply` make the stack; `<engine> sync`, `status`, `shortcut`, `overlay` and `promote` act on it afterwards.
`env -u` drops a `FLOX_EXTENSION_NAME` your session may carry, as one started with `flox agent launch` does: with it, the engine prints commands for later that a new terminal may not run.

## 2. Propose

Say, in one line, that you are reading their setup and matching it to the catalog, which takes about half a minute.
Then run:

```sh
<engine> init plan --json
```

If the person named the stack, add `--name <name>`.
A directory they name for it goes to plan too, as `--stack <dir>`, written out in full.
Never pass `--dir`: flox-agent takes that flag for itself, so the engine never sees it.

It prints JSON: `plan` (the plan file), `summary`, `ask`, `shortcut`, `harness` and `checks.problems`.

- If `checks.problems` is not empty, show each line.
  For any line but the one below, stop: the person does what it says (a command with sudo or a login is theirs to run, in a terminal), then you plan again.
- When the only line says `<dir>` "is already an onboarded stack", an earlier onboarding made that stack.
  `<dir>` is that directory with `~` written out, and `<name>` its last part.
  Ask, with header `Stack` and question "`<dir>` is already a stack from an earlier onboarding. What now?":
  - `Bring it up to date (Recommended)`: "Runs sync on that stack: it takes in your edits and rebuilds what changed. It publishes and pushes nothing."
  - `Make another stack`: "Plans a new stack beside it, named `<name>-2` or a name you give."
  - `Stop`: "Changes nothing."

  For the first, run `<engine> sync --stack <dir>`, then `<engine> status --stack <dir>`, show both, and go to Later.
  For the second, plan again with `--name` and that name.
- Show `summary`, then ask the questions in `ask`.
  Its last question, `Create`, is whether to make the stack, and whether on their other computers too.

## 3. Record the answers

Pass every answer as `--answer "<header>=<label>"`, the label without ` (Recommended)`:

```sh
<engine> init decide <plan> --json --answer "Create=Create it, on all my computers" …
```

It prints JSON: `confirmed`, `share`, `summary` and, once the plan is confirmed, the `shortcut` question.

To **ask again**: decide remakes the plan's questions after a change, and its JSON does not carry them.
Read `<plan>`, the plan file, and ask the questions in its `questions` as they now stand (the last is Create): each with its `header` and `question`, multiSelect false, and only the `label` and `description` of each option.
Never ask from plan's first `ask` again: its descriptions may no longer say what the stack shares.

- **Show every item first**: run `<engine> init decide <plan> --details`, with no answers: it changes nothing and prints every item.
  Show it, then ask again.
- **Not now**: say that nothing was written beyond onboarding's cache and its logs, and stop.
- **An answer in their own words** is a change.
  Turn it into decide's arguments: `<item>=keep` (leave it where it is), `<item>=local` (package their copy as it is), `<item>=catalog` (the catalog's copy), `--name <name>`.
  Run `init decide` with them, show the new `summary`, and ask again.
  A directory for the stack is not one of them: plan again as in 2, with `--stack <dir>`.

## 4. Build, and ask the shortcut meanwhile

Once decide's `confirmed` is true, build the stack, which takes a few minutes the first time:

```sh
<engine> init apply <plan> --json
```

Meanwhile, ask decide's `shortcut` question exactly as given (plan's, if decide gave none): whether the harness's own name, `claude` or `codex`, starts the stack from now on.

- In an agent that can run a command in the background and tells you when it ends, such as Claude Code, start apply that way, and ask the question in the same message.
  Do not wait for apply with a loop or poll it.
- In any other agent, ask the question first and wait for the answer.
  Then say the build can take several minutes the first time, run apply, and wait for it.

When apply has finished, read its JSON.
If it failed, show its error and the log it names, and stop.
Show each entry of its `notes` the way you show the engine's text.

## 5. Finish

Name the stack in every command: `--stack <dir>`, `<dir>` being apply's `dir`.
Keep this order: `shortcut` is refused while a sync of the stack runs.

- **If they chose "Make … start it"**, run the following and show what it prints, with `--push` when they chose "Make … start it, on all my computers" (it pushes the change to their default environment to FloxHub, now or once the sync below adds it):

  ```sh
  <engine> shortcut --stack <dir>
  ```

- **If apply's `share` is true** (they chose all their computers), start the sync and show what it prints:

  ```sh
  <engine> sync --stack <dir> --publish --push --yes --background
  ```

  It returns at once, then uploads their own packages and puts the stack on FloxHub by itself.
  Do not wait for it.
- End with how to try the stack.
  With the shortcut: apply's `harness` (`claude` or `codex`) in a new terminal, as soon as `shortcut` said it starts the stack, or once the upload finishes when it said "once the stack is on FloxHub".
  Without it: apply's `launch`.
  Then, with `<agent-stacks>` being apply's `agent` as it is (it works once this session is over):
  - "After you edit a skill, run `<agent-stacks> sync --stack <dir>`";
  - when sharing: "`<agent-stacks> status --stack <dir>` shows how the upload to your other computers is going".

## Later

`<dir>` is the stack repository; without `--stack`, a command finds the one stack in `~/.local/share/agent-stacks/stacks`.

- `<engine> status --stack <dir>`: what is where, what changed, how the last sync went, and what `claude` starts.
- `<engine> sync --stack <dir>`: take in their edits, with no flags publishing and pushing nothing.
- `<engine> sync --stack <dir> --publish --push --yes --background`, then `<engine> status --stack <dir>`: share the stack with their other computers, or share it again after a sync stopped.
  Run it only when the person asks for it.
  When `<engine> status --stack <dir>` says the stack is "not on FloxHub yet", first ask whether to share it, with header `Share` and question "Put this stack on FloxHub?":
  - `Share it`: "Uploads your own packages to your FloxHub catalog, which only you can see, and puts the stack on FloxHub under your handle. That page is public: its name, package names and versions, and, for packages of your own, your skills' one-line descriptions and the path on this computer they were published from, which includes your user name. The Flox CLI cannot delete it."
  - `Not now`: "Nothing leaves this computer."

  `--yes` makes the stack public on its first push, and their yes is what it stands for.
- `<engine> shortcut --off --stack <dir>`: plain `claude` (or `codex`) again.
- `<engine> overlay <plugin> --stack <dir>`: their overlay on a catalog package.
  `--on <part>` brings a part of the catalog's package back, `--off <part>` leaves one out, `--drop <name>` takes the catalog's copy of one of theirs, and `--drop all` makes it the catalog's package again.
  A part is a kind (`skills`, `commands`, `agents`, `hooks`, `mcp`) or one of them, such as `hooks/SessionStart`.
- A team: only when the person asks to share packages with a team.
  Ask the organization's name.
  Then ask which of the packages of theirs that `status` lists to publish there (multiSelect in Claude Code, numbered in chat), each option described as "Publishes `<plugin>` to `<org>`'s catalog, where every member of `<org>` can install it, and later syncs publish its changes there too."
  `<engine> promote <plugin> --org <org> --stack <dir> --dry-run` shows the steps first, and does nothing.
  Then run `<engine> promote <plugin> --org <org> --stack <dir>` for each package they picked, and no other, one at a time: a promote holds the stack while it runs.

## Without the engine

When flox-agent has no `init` yet, you make a simpler stack: a Flox environment holding flox-agent, the person's agent, and the Flox Catalog's packages of the Claude Code plugins they use, which `flox-agent launch` starts.
Tell the person, in a few lines, first:

- The stack brings their agent and the plugins the catalog has.
- Their own skills, their other plugins, their MCP servers and their settings stay where they are.
  The stack's agent still loads them on this computer, but not on another.
- It does not make `claude` (or `codex`) start the stack.
- Once flox-agent has onboarding, asking for it again, or pasting the same prompt again, makes the full stack.

### Read their setup

Read only what is named here, and change nothing.

- **The agent.** Run `claude --version` and `codex --version`.
  When only one of them runs, `<agent>` is that one: `claude` or `codex`.
  When both run, ask, with header `Agent` and question "Which agent should the stack start?":
  - `Claude Code`: "The stack starts Claude Code, with the plugins below."
  - `Codex`: "The stack starts Codex, with the skills of the plugins below."

  When neither runs, `<agent>` is `claude`, which the stack brings.
  For `claude`, `<id>` is `claude-code`, `<agent-pkg>` is `agent-stacks/claude-code`, and `<title>` is Claude Code.
  For `codex`, they are `codex`, `agent-stacks/codex` and Codex.
- **Plugins**, when `claude` runs.
  `claude plugin list --json` lists Claude Code's plugins: each has an `id`, `<plugin>@<marketplace>`, a `scope` and `enabled`.
  `claude plugin marketplace list --json` lists its marketplaces: each has a `name` and a `source`, and a `github` source has a `repo`, `<owner>/<repo>`.
  A candidate is a plugin whose `scope` is `user`, whose `enabled` is true, and whose marketplace's `source` is `github`.
  Never open Claude Code's settings files for this: they may hold secrets.
- **Skills.** List the folder names in `~/.claude/skills`, and in `~/.codex/skills` and `~/.agents/skills` for Codex, only to say they stay here.

### Match plugins to the catalog

For each candidate, run:

```sh
curl -fsSL https://raw.githubusercontent.com/agent-stacks/agent-pkgs/main/pkgs/agent-plugin-<plugin>/default.json
```

It matches when the command succeeds and the file's `src.owner` and `src.repo` are the marketplace's `<owner>` and `<repo>`, in any case: the catalog's package `agent-stacks/agent-plugin-<plugin>` is made from the same repository.
A plugin of the same name from another repository is another plugin, and does not match.
`<version>` is the file's `version`.
Every plugin that does not match stays here.

### Propose

`<handle>` is the FloxHub handle `flox auth status` printed, in "You are logged in as `<handle>`".
`<name>` is `my-agent`, or the name the person gave: lowercase letters, digits and dashes.
`<dir>` is the folder `printf '%s\n' "${XDG_DATA_HOME:-$HOME/.local/share}/agent-stacks/environments/<name>"` prints.

If `<dir>/.flox` is there, an earlier run made this stack.
Ask, with header `Stack` and question "`<dir>` is already a stack. What now?":

- `Use it as it is (Recommended)`: "Changes nothing, and shows how to start it."
- `Make another stack`: "Makes a new stack beside it, named `<name>-2` or a name you give."
- `Stop`: "Changes nothing."

For the first, go to [Finish, without the engine](#finish-without-the-engine).
For the second, start this step again with that name.

Show the stack you propose, in a `text` block, in this shape:

```text
Stack <handle>/<name> · <title> · started with flox-agent launch
Kept in <dir>

  From the catalog  <plugin> <version>, …
  Stays here        <plugin> (not in the catalog), <skill> (your skill), …
                    and your MCP servers and settings
```

Leave out a line that would list nothing.
Then ask, with header `Create` and question "Create this stack?":

- `Create it, on all my computers (Recommended)`: "Makes the Flox environment `<dir>` with `<title>`, flox-agent and the plugins from the catalog, then pushes it to FloxHub as `<handle>/<name>`. That page is public: its name, package names and versions. Your other computers start it with one command."
- `Create it on this computer only`: "Makes the Flox environment `<dir>` here. Nothing leaves this computer."
- `Not now`: "Changes nothing."

On `Not now`, stop: nothing was written.
An answer in their own words, such as to leave a plugin out, to add a catalog plugin by name, or to give the stack another name, is a change.
Check a plugin they name with `flox show agent-stacks/agent-plugin-<plugin>` first.
Make the change, show the stack again, and ask again.

### Build

1. Run `mkdir -p <dir>`, then `flox init -d <dir> -n <name> --no-auto-setup`, and read `<format>` from the manifest it wrote, as in 1.
2. Write the manifest and lock it, which downloads every package and can take a few minutes:

   ```sh
   flox edit -d <dir> -f - <<'EOF'
   <format>

   [install]
   flox-agent.pkg-path = "flox/flox-agent"
   flox-agent.pkg-group = "flox-agent"
   <id>.pkg-path = "<agent-pkg>"
   <id>.pkg-group = "<id>"
   agent-plugin-<plugin>.pkg-path = "agent-stacks/agent-plugin-<plugin>"
   agent-plugin-<plugin>.pkg-group = "agent-plugin-<plugin>"

   [options]
   systems = ["aarch64-darwin", "aarch64-linux", "x86_64-linux"]
   EOF
   ```

   Write the two `agent-plugin-<plugin>` lines once for each plugin from the catalog, and not at all when there is none.
   Each package has a group of its own: in one group, packages must all resolve together, and these do not.
   If it fails naming a plugin's package, which the catalog may not serve, or not build for one of these systems, take that plugin out, say so, and run it again.
   If it fails any other way, show what Flox said, and stop.
3. Show what the stack's agent gets, as it prints it: `flox activate -d <dir> -- flox-agent list`.
4. **If they chose "Create it, on all my computers"**, run `flox push -d <dir>`, and show what it prints.
   If FloxHub already has an environment named `<handle>/<name>`, say so: the stack works here, and to share it, the person deletes the old one at hub.flox.dev, or makes another stack under another name.

### Finish, without the engine

End with:

- How to start the stack here: `flox activate -d <dir> -- flox-agent launch <agent>`.
- After a push, on any computer logged in to FloxHub as them, this computer included: `flox activate -r <handle>/<name> -- flox-agent launch <agent>`.
- On this computer, Claude Code also loads its own copy of a plugin the stack brings too.
  The person can turn theirs off with `/plugin` in Claude Code; never change their settings yourself.
- Once flox-agent has onboarding, asking for it again, or pasting the same prompt again, makes the full stack, which packages their own skills and can make `<agent>` start it.

### Later, without the engine

- `flox activate -d <dir> -- flox-agent list`: what the stack's agent gets.
- To add or take out a catalog plugin: `flox edit -d <dir>` opens the manifest, where each plugin has its two lines, as in Build.
  After a change to a stack on FloxHub, `flox push -d <dir>` shares it.
- On another computer, after a push: `flox activate -r <handle>/<name> -- flox-agent launch <agent>`.

## When something fails

Show the command and its last lines, and the log it names: every command but `status` logs what it ran in `~/.local/state/agent-stacks/onboard/logs/`.
Do not retry an outward step (a publish, a push, a shortcut) on your own.

- **Your shell tool stopped a command at its time limit**: run it again with the longest limit it allows.
  An apply stopped partway picks up where it left off; never add `--force` for this.
- **"Resolving packages requires logging in to FloxHub"**, or a sync's "… could not be checked" naming `flox auth login`: the login expired.
  The person runs `flox auth login` in a terminal, then you run that command again.
- **git asks to accept the Xcode licence**: onboarding does not need Apple's git; it uses Flox's when Apple's does not run.
- **A plan matched less than expected**: matching compares content, never names alone.
  An edited copy is an `overlay`, which is right; a version the catalog lacks is a `pin`, and its `alternatives` name the nearest catalog version.
- **"nobody has confirmed this plan"**: a change cleared the person's yes; ask again (see 3).
- **A package does not build**, with "attribute '…' missing": an inferred library or tool has no package by that name.
  Offer to leave that item out: `init decide` with `<item>=keep`, ask again (see 3), then run `init apply` again with `--force`.
- **The stack does not lock**, with a package missing for a system: the catalog changed since the plan; plan again.
- **"a sync of … is already running"** (or a promote, or a shortcut): one at a time per stack.
  `status` says what it is doing; run the command again once it ends.
- **status says the last sync stopped**: show its error and its log.
  To share it again, the person asks, as Later says; what was published is not published again.
- **"… was published twice, and the catalog still cannot serve it"**: FloxHub took the package but has not finished storing it.
  Once the person says so, a few minutes later, run that sync again.
- **"An environment named … already exists"** on push: an earlier stack of theirs has that name on FloxHub.
  They pick another name, or delete the old one at hub.flox.dev.
- **"… was first published from another repository"**: publishing would replace a teammate's package's source, which sync never does on its own.
  Show the message; the person decides, and runs the command it names in a terminal themselves; never run it.
- **"note: the stack's FloxHub page now also shows the path …"**: show it as it is: the page is public.
- **`claude` still runs plain Claude Code** in a terminal open before `shortcut`: open a new terminal, or run `exec $SHELL` in it.
- **`claude` runs plain Claude Code in one folder**: that folder's own Flox environment installs Claude Code, which comes first there.
  The person decides whether to take it out, never you.
- **"… once the stack is on FloxHub"**: their default environment is on FloxHub, and can include only a stack that is there too.
  The background sync adds it; `status` says when.
- **"… was left as it was (…), so the shortcut is a script on PATH instead"**: show the note; the script starts the stack meanwhile.
- **Another computer**: after "Make … start it, on all my computers", `flox pull -D` there, then a new terminal.
  After "Make … start it on this computer only", the person runs `flox push -D` here (it sends any other change of theirs too), then `flox pull -D` there.
  Never run `flox push` for them.
  Otherwise, once the stack is on FloxHub, they start it there with the `flox activate -r …` command in the stack repository's `README.md`.
- **"Not sent to FloxHub: …"**: show the line; it says what the person runs.
- **"could not bring … up to date from FloxHub first"**: this computer's copy and FloxHub's both changed.
  Pulling or pushing with `--force` drops one side's changes: the person chooses which, never you.
- **A line starting "But"**: something in their shell, such as an alias, runs before the shortcut.
  Show the line; never edit their alias or startup file yourself.
- **"… is a link into /nix/store"**: home-manager manages that startup file.
  Show the lines `shortcut` printed; the person adds them to their home-manager configuration.
- **"… is not a shortcut onboard wrote"**: a file of theirs is at that path; ask before moving it.
- **"… is out, but your changes do not carry onto it"**: sync keeps the overlay on its release.
  The person chooses: drop the change that clashes (`overlay <plugin> --drop <name>`), or make it again on the new release's copy.
