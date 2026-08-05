# Flox Skills

This directory is the shared skill library for the Flox Codex and Claude Code
plugins. Each subdirectory contains one skill, with its instructions in
`SKILL.md`.

## How Plugins Use These Skills

Both plugin manifests live one directory up, in `flox-plugin/`, and point at
this directory with the same relative path:

```json
{
  "skills": "./skills/"
}
```

- Codex manifest: `../.codex-plugin/plugin.json`
- Claude Code manifest: `../.claude-plugin/plugin.json`

Keep the skill directory layout compatible with both plugin systems unless a
platform-specific difference is intentional.

## Skill Inventory

- `flox`: Create and manage reproducible Flox environments. The top `SKILL.md`
  holds the core guidance and routes to reference files in `references/` for
  sharing/composition, services, builds, containers, publishing, and CUDA/GPU
  workflows.
- `floxify`: Convert an existing repository to a verified working Flox
  environment, with detection and verification scripts in `scripts/`.
- `catalog-resolution-debug`: Diagnose Flox catalog resolution — why a given
  build was selected, why a new publish isn't picked up, and why adding a
  package can make a working `pkg-group` unsatisfiable. A procedure rather
  than reference material: it gathers the environment's packages, reproduces
  the resolve call, and reports a cause.

## Maintenance Notes

- Keep each skill in its own directory with a `SKILL.md` file.
- Preserve the YAML front matter at the top of each `SKILL.md`; plugin loaders
  use it for the skill name and description.
- When adding, renaming, or removing a skill, update this README and the
  top-level repository README together.
- Installation instructions belong in the top-level README. This file is for
  explaining the skill library itself.
