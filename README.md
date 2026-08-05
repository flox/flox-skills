# Flox Skills

This repository provides skills that teach AI coding agents how to use 
[Flox](https://flox.dev) properly. Install this plugin and your agent gains a 
Flox specialist: it builds reproducible, portable development environments, 
onboards existing repos to Flox, and wires up services, builds, containers, 
and package publishing by applying Flox best practices for you.

Works with **Claude Code**, **Codex**, and any agent that supports the
[skills.sh](https://skills.sh) standard.

## What's inside

Three skills covering the Flox lifecycle, from a blank directory to a published
build — and diagnosing it when the catalog doesn't give you the build you expected:

- **`flox`** — Create and manage reproducible Flox environments. Installs
  packages and pins toolchains (Python, Node, Go, Rust, and more), sets up
  services and databases, builds and containerizes applications, publishes to
  FloxHub, and composes/layers environments across teams. Reach for it first when
  starting any new project.
- **`floxify`** — Onboard an *existing* repo to Flox. It detects your runtimes,
  services, and build tools, then writes `.flox/env/manifest.toml` so that
  `flox activate` becomes the only setup command a new developer needs. It
  handles a broad range of project shapes:
  - **Languages & runtimes** — Python, Node, Ruby, Go, Rust, PHP, Elixir, .NET,
    Deno, Bun, and more, pinned from the files already in your repo
    (`.python-version`, `.nvmrc`, `go.mod`, `rust-toolchain.toml`, …).
  - **Existing tool configs** — converts DevBox (`devbox.json`), Dev Containers
    (`.devcontainer/`), Homebrew `Brewfile`s, and `asdf`/`mise` pins, and
    coexists with `flake.nix`/`shell.nix`.
  - **Package managers** — wires up uv, Poetry, npm, pnpm, Yarn, Bundler, Cargo,
    Composer, and Mix to install dependencies automatically on activation.
  - **Services** — stands up PostgreSQL, Redis, MySQL/MariaDB, and MongoDB as
    managed Flox services, and hands `docker-compose`, Tilt, or Sentry
    devservices topologies back to their own orchestrator.
  - **Re-runs safely** — on a repo that already uses Flox, it audits for gaps
    instead of overwriting your manifest.
- **`catalog-resolution-debug`** — Work out why the catalog gave you the build it
  did. Reach for it when a package won't resolve, when `flox install` keeps
  picking an old build after you published a new one, or when adding one package
  makes a working environment fail with "constraints too tight". It explains what
  the resolver is actually choosing between and walks the diagnosis to a specific
  cause and a fix.

Each skill keeps a lean core and loads detailed reference material only when your
task calls for it — so the agent stays fast and focused.

## Why use it

- **Portable by construction.** The agent declares only the platforms your
  packages can actually serve and verifies resolution against the live catalog —
  no more "works on my machine" manifests.
- **One command to onboard.** `floxify` turns a fresh clone into an activatable
  environment, replacing a page of setup instructions with a single `flox activate`.
- **Verified, not guessed.** The skills activate the environment and exercise its
  services before calling a setup done.
- **Measured, not asserted.** The skills are evaluated continuously against a
  no-skill baseline. In the latest batch, skill-guided runs produced **zero** hard
  portability defects, versus a majority of baseline runs shipping one. See
  [`evals/`](evals/README.md) for the methodology and full numbers.

## Install

You'll need the [Flox CLI](https://flox.dev) installed. For GPU/CUDA work you'll
also need a Linux machine with an NVIDIA GPU.

### Claude Code

From within Claude Code:

```
/plugin marketplace add flox/flox-skills
/plugin install flox@flox-skills
```

Or from the command line:

```bash
claude plugin marketplace add flox/flox-skills
claude plugin install flox@flox-skills
```

### Codex

Run from a clone of this repository:

```bash
codex plugin marketplace add .   # in the repo's top-level directory
codex plugin add flox@flox-skills
codex plugin list                # verify
```

### Other agents (Cursor, Copilot, Windsurf, Gemini, and more)

For any agent that supports the [skills.sh](https://skills.sh) standard, install
with a single command (requires Node.js):

```bash
npx skills add flox/flox-skills
```

> skills.sh is a third-party tool, not maintained by Flox. See
> [skills.sh](https://skills.sh) for the list of supported agents.

## Using it

Once installed, your agent loads the right skill automatically based on what you
ask — there's nothing to invoke by hand. For example:

- *"Set up a new Python project with Postgres"* → the **flox** skill scaffolds the
  environment and wires the service.
- *"Get this repo running with Flox"* → the **floxify** skill inspects the repo and
  writes a manifest you can `flox activate`.
- *"I published a new version but flox install still gives me the old build"* → the
  **catalog-resolution-debug** skill works out which build the resolver picked and
  why.

## Learn more

- [`flox-plugin/skills/`](flox-plugin/skills/README.md) — the skill library: what
  each skill covers and how its reference material is organized.
- [`evals/`](evals/README.md) — how the skills are measured and the results behind
  the claims above, including the [floxify conversion evals](evals/floxify/README.md).

## Contributing

Issues and pull requests are welcome. Every change to a skill ships with an eval
that verifies the new guidance is actually followed — see [`evals/`](evals/README.md).

## License

See [LICENSE](LICENSE).
