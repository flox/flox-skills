# Flox Agentic Tools

This repository provides tools and integrations for AI agents to work with Flox, offering expert guidance and automation for Flox development environments, builds, services, and deployments.

## Overview

This project includes specialized knowledge and tooling for Flox workflows, best practices, and patterns. It provides a comprehensive set of skills covering the entire Flox development lifecycle, from environment setup to production deployment, accessible through multiple AI agent platforms.

## Components

### The Flox Skill

The repository provides a single `flox` skill covering the
entire Flox development lifecycle. The top `SKILL.md` holds the core guidance and
routes to detailed **reference files** for specialized topics (progressive
disclosure). It is the foundational skill that should be used first when creating
any new project. Covers:

- Installing packages and managing dependencies
- Python, Node.js, and Go environment setup
- Environment configuration and secrets management
- Reproducible development workflows
- Sharing, composing, and layering environments — composition via `[include]`, remote environments, FloxHub, team collaboration (see `references/sharing.md`)
- Running services, background processes, and databases (see `references/services.md`)
- Building and packaging applications — manifest/Nix builds, sandbox modes, multi-stage builds (see `references/builds.md`)
- Containerizing environments with Docker/Podman — OCI exports, multi-stage container builds, deployment (see `references/containers.md`)
- Publishing packages/builds to FloxHub — org/personal namespaces, versioning, distribution (see `references/publish.md`)
- CUDA and GPU development (Linux) — NVIDIA CUDA toolkit, cuDNN, deep-learning frameworks, cross-platform GPU/CPU (see `references/cuda.md`)

## Measured Benefits

The skill is measured continuously against a no-skill baseline in
`evals/floxify/` — two arms with an identical tool surface where the
only variable is the skill. Latest batch: five fixture repositories
(Go, Ruby, Rust, Python/uv, Node+Postgres) × 8 reps per arm on
Claude Opus, each rep graded against a verified working environment
(activation, and live services where the repo needs them).

**Portable-by-construction environments.** Without the skill, more
than half of baseline runs (56%) produced a manifest with a hard
portability defect — most often declaring platforms the pinned
package cannot actually serve, an environment that works on the
author's machine and breaks for the next platform. Skill-guided
runs produced zero hard violations across 39 verified reps, passed
100% of fixture hard checks (baseline: 85%), and scored 4.3 vs 2.7
on the 1–5 quality judge.

**Fewer agent turns where wiring judgment lives.** On repos that
need real service wiring, the skill reaches a verified working
environment in materially fewer turns (Node+Postgres: median 13 vs
18.5; Python/uv: 20.5 vs 28.5) — and the only run in the batch that
failed verification was a baseline run. On simple single-toolchain
repos a frontier model is already fluent, and the skill's checking
discipline costs a few extra turns there; the win on those repos is
the portability guarantee above, not speed.

**Cost.** A full skill-guided conversion lands at roughly
$0.50–1.50 per run (Claude Opus, median). Skill runs cost slightly
more than baseline on simple repos; the delta is the price of the
verification discipline that produces the conformance gap.

## Best Practices

The evaluation goldens and the skill enforce the same manifest
discipline, which is equally useful for humans:

- Use the fewest package groups possible — every extra group is a
  full transitive closure (down to libc) to resolve and download.
- Check `flox show` before installing or pinning: confirm the
  version and the platforms the catalog actually serves.
- Declare only systems every package can serve — never ship a
  "works on my machine" manifest.
- Pin only when necessary. Version floors (`>=`) are fine;
  ceilings (`<=`) and exact pins trade Flox's continuous-upgrade
  benefit for a frozen snapshot, so use them deliberately.
- Prefer pinning the top-level toolchain and leaving compatible
  libraries unpinned over splitting packages into extra groups.
- Wire the services a developer needs running locally
  (`[services]`, compose files); leave production-only
  infrastructure out of the development environment.
- Verify before declaring done: activate the environment, exercise
  the runtime and services, and check the manifest against what the
  repository actually requires.

## Installation

### Prerequisites

- Flox CLI installed and configured
- For GPU development: Linux system with NVIDIA GPU (aarch64-linux or x86_64-linux)

### Application-Specific Setup

#### Claude Code

The Flox plugin for Claude Code provides comprehensive Flox integration, including package management, environment composition, service orchestration, build system configuration, containerization, publishing, and CUDA support. The plugin provides the Skills library as native Claude skills.

**Install the Plugin:**

From within Claude Code:

```bash
/plugin marketplace add flox/flox-skills
/plugin install flox@flox-skills
```

Or from the command line:

```bash
claude plugin marketplace add flox/flox-skills
claude plugin install flox@flox-skills
```

**Getting Started:**

Once installed, the plugin automatically activates. Claude Code will use the appropriate skill based on your task:
- Creating a new project, setting up services/databases, building, containerizing, or publishing? The **flox** skill activates first

#### Other Agents (Cursor, Copilot, Windsurf, Gemini, and more)

For agents that support the [skills.sh](https://skills.sh) standard, you
can install the full Flox skills library with a single command (requires
Node.js):

```bash
npx skills add flox/flox-skills
```

This installs the Flox skill into your agent's context, covering environments
plus sharing/composition, services, builds, containers, publishing, and CUDA/GPU
(via its reference files).
Supported agents include Cursor, GitHub Copilot, Windsurf, Gemini, and
[many others](https://skills.sh).

> **Note:** skills.sh is a third-party tool, not maintained by Flox.
> See [skills.sh](https://skills.sh) for supported agents and documentation.

## Documentation

The Flox skill lives in `skills/flox/`:
- `skills/flox/SKILL.md` — core guidance + routing
- `skills/flox/references/` — detailed references: `sharing.md`, `services.md`,
  `builds.md`, `containers.md`, `publish.md`, `cuda.md`

## Contributing

Contributions are welcome! Please feel free to submit issues or pull requests.

## License

See LICENSE file for details.
