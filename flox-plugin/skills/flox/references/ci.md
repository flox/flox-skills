# Flox in CI

Guidance for running CI steps inside a Flox environment, with GitHub Actions as
the worked example. The distinction that matters most: **installing Flox and
activating an environment are two separate things**, and only one action does
each.

## Install is not activation

| Action | What it does | What it does not do |
|---|---|---|
| [`flox/install-flox-action`](https://github.com/flox/install-flox-action) | Installs the Flox CLI on Linux and macOS runners | Does not activate a project environment. It has no input for running a command inside one. |
| [`flox/activate-action`](https://github.com/flox/activate-action) | Runs one command inside a local or remote Flox environment | Does not install Flox. Run the install action first. |

After `install-flox-action`, `flox` is on `PATH` — but the subsequent steps are
still running on the bare runner. Anything the environment provides
(interpreters, linters, services) is not there until something activates.

**Never install Flox in CI with a `curl … | bash` one-liner.** Use the action.

## Short commands: `activate-action`

```yaml
- uses: flox/install-flox-action@1128abd73431089ab9d871c893b4e72a729354e1 # v2.6.0

- name: Run tests
  uses: flox/activate-action@7065dcbe5583b7b015f07a8ebd49d7266e3053e8 # v1.1.1
  with:
    command: python3 -m pytest -q
```

Inputs:

| Input | Required | Meaning |
|---|---|---|
| `command` | yes | The command to run inside the environment |
| `environment` | no | A remote environment (`owner/name`), passed as `-r=`. Omit to use the local `.flox/` |
| `dir` | no | Path containing the `.flox/` directory, passed as `--dir=` |

**Caveat — do not pass an arbitrary multiline script as `command`.** The
composite action interpolates the input into a single-quoted
`flox activate … -c '<command>'`. Any single quote in your script terminates
that wrapper: `awk '{print $1}'`, `bash -c 'x'`, and even an apostrophe in an
echoed message will break or silently mis-execute the step. Keep `command` to a
short, quote-free invocation; for anything longer, use the custom shell below.

## Multiline scripts: Flox as the step's shell

For a substantial multiline script, make Flox the shell so GitHub's generated
script runs entirely inside the environment:

```yaml
- name: Run checks
  shell: flox activate -- bash --noprofile --norc -e -o pipefail {0}
  run: |
    python3 -m pytest -q
    ruff check .
    mypy src
```

GitHub substitutes the path of the script it generated for `{0}`. One
activation covers the whole block.

Each flag earns its place:

- `--noprofile --norc` — do not source the runner's shell startup files, which
  would re-order `PATH` and can undo the activation.
- `-e` — fail the step on the first failing command. Without it a custom shell
  reports success as long as the last line succeeded.
- `-o pipefail` — fail on a failing command anywhere in a pipeline, not just the
  last one.

**Do not repeat activation per line:**

```yaml
# WRONG — re-enters the environment three times, and each line loses any
# state the previous one set.
- name: Run checks
  run: |
    flox activate -- python3 -m pytest -q
    flox activate -- ruff check .
    flox activate -- mypy src
```

## Complete workflow

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6

      - name: Install Flox
        uses: flox/install-flox-action@1128abd73431089ab9d871c893b4e72a729354e1 # v2.6.0

      - name: Run checks
        shell: flox activate -- bash --noprofile --norc -e -o pipefail {0}
        run: |
          python3 -m pytest -q
          ruff check .
          mypy src
```

`shell:` is a step-level key, so mixed workflows are fine: steps that need the
environment carry it, steps that do not (uploading an artifact, posting a
comment) run on the default shell.

## Setting the shell once per job

If most steps in a job need the environment, set it as the job default and
override on the exceptions:

```yaml
jobs:
  test:
    runs-on: ubuntu-latest
    defaults:
      run:
        shell: flox activate -- bash --noprofile --norc -e -o pipefail {0}
    steps:
      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6
      - uses: flox/install-flox-action@1128abd73431089ab9d871c893b4e72a729354e1 # v2.6.0
      - run: python3 -m pytest -q
```

Note the ordering constraint: `defaults.run.shell` applies to every `run:` step
in the job, including any that precede the install step. Keep pre-install work
in `uses:` steps, or set the shell per step instead.

## Remote environments

To run against a FloxHub environment rather than the repo's `.flox/`:

```yaml
- name: Run tests
  uses: flox/activate-action@7065dcbe5583b7b015f07a8ebd49d7266e3053e8 # v1.1.1
  with:
    environment: myorg/ci-tools
    command: python3 -m pytest -q
```

The custom-shell equivalent is `shell: flox activate -r myorg/ci-tools -- bash --noprofile --norc -e -o pipefail {0}`.
A remote environment that is not already trusted must be listed in
`install-flox-action`'s `trusted-environments` input.

## Pinning

Where repository policy requires it — and it does in `flox/flox-skills` — pin
every third-party action to a full commit SHA with the version in a trailing
comment:

```yaml
- uses: flox/install-flox-action@1128abd73431089ab9d871c893b4e72a729354e1 # v2.6.0
```

A moving tag (`@v2`, `@main`) is a supply-chain hole: the tag can be repointed
at any commit. The SHA cannot.

## Common mistakes

| Mistake | Why it fails | Fix |
|---|---|---|
| Treating `install-flox-action` as activation | It installs the CLI only; later steps run on the bare runner | Add `activate-action` or the custom `shell:` |
| `flox activate --` on every line of one `run:` block | Re-enters the environment per command; per-line state is lost | One `shell:` on the step |
| A multiline script in `activate-action`'s `command:` | Interpolated into `-c '…'`; an embedded `'` breaks the wrapper | Use the custom `shell:` |
| Custom shell without `-e -o pipefail` | Step passes as long as the last line passes | Keep both flags |
| Custom shell without `--noprofile --norc` | Runner startup files re-order `PATH` | Keep both flags |
| `curl … \| bash` to install Flox | Not a supported install path | `flox/install-flox-action` |
| `uses: flox/install-flox-action@v2` | Moving tag; supply-chain risk | Pin the full SHA |

## Other CI systems

The same split applies. Install Flox with the platform's package manager or the
documented installer, then enter the environment once per script rather than per
line — `flox activate -- <interpreter> <script>`, or by making the script's
first action an activation.
