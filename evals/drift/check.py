#!/usr/bin/env python3
"""Drift checker: are the skills' CLI claims still true of the installed flox?

`evals/flox/skill_toml_lint.py` checks that TOML snippets in the skills
parse. Nothing checked the prose claims about flox's own surface, which is
where the skills actually go stale: a flag renamed upstream leaves a skill
confidently instructing an agent to run something that no longer exists.

This reads a curated registry of load-bearing claims and verifies each one
against `flox <cmd> --help`. Deterministic, offline, free, and therefore
suitable for a per-PR gate and a pre-commit hook (AI-512 layers L1 and L2).

Two things are checked per registry entry, in both directions:

  1. the claim holds against the installed flox
  2. the entry's `quote` still exists in the skill file it points at

The second is what stops the registry rotting silently. An entry whose
quote has left the skill is reported as a finding rather than passing on a
claim nobody reads any more.

WHAT THIS DOES NOT CHECK. Only `command_exists` and `flag_exists`, the two
kinds that provably work offline. Manifest semantics are out: the
`x86_64-darwin` default-systems change of Flox 1.15 broke a skill claim and
would NOT have been caught here, because it is neither a command nor a
flag. `schema_version_current` has no working probe at all -- a fresh
`flox init` writes a commented template with no readable version line. Both
are later layers, and the coverage of this one should not be read as wider
than it is.

Usage:
    python3 check.py                  # verify the shipped registry
    python3 check.py --suggest        # also print the current surface
    python3 check.py --claims other.jsonl
    python3 check.py --flox /path/to/flox

Exit 0 when every claim holds, 1 on any drift, 2 on a setup error.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
DEFAULT_CLAIMS = HERE / "tasks" / "claims.jsonl"
DEFAULT_SKILL_ROOT = REPO_ROOT / "flox-plugin"

KINDS = ("command_exists", "flag_exists")
REQUIRED = ("id", "kind", "command", "skill_ref")
HELP_TIMEOUT = 30

# A subcommand line in `flox --help`: four spaces, the name (or an
# alias pair like "install, i"), then two-plus spaces and prose. Anchored
# so a word inside a description can never match.
_COMMAND_LINE = re.compile(r"^ {4}(?P<names>[a-z][a-z0-9-]*(?:, [a-z])?)\s{2,}\S")

# A flag in a subcommand's `--help`. Either "-x, --long" or a bare
# "    --long". The token ends at =, comma, or whitespace, so
# "--mode=ARG" registers as --mode and prose mentioning a word does not
# register at all.
_FLAG_TOKEN = re.compile(r"(?:^|[\s,])(--[a-z][a-z0-9-]*)(?=[=,\s]|$)")


@dataclass
class Result:
    ok: bool
    detail: str = ""
    line: int = 0


def load_claims(path):
    """Parse and validate the registry.

    Raises ValueError on a malformed entry: a registry typo should fail
    loudly at load rather than as a confusing mid-run KeyError, and a
    duplicate id would make one of the two silently unreportable.
    """
    claims, seen = [], set()
    for i, raw in enumerate(Path(path).read_text().splitlines(), 1):
        if not raw.strip():
            continue
        try:
            c = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}:{i}: not valid JSON: {e}") from e
        missing = [f for f in REQUIRED if f not in c]
        if missing:
            raise ValueError(f"{path}:{i}: missing fields {missing}")
        if c["kind"] not in KINDS:
            raise ValueError(f"{path}:{i}: unknown kind {c['kind']!r}, expected one of {KINDS}")
        if c["kind"] == "flag_exists" and "flag" not in c:
            raise ValueError(f"{path}:{i}: flag_exists needs a 'flag'")
        for f in ("file", "quote"):
            if f not in c["skill_ref"]:
                raise ValueError(f"{path}:{i}: skill_ref needs a {f!r}")
        if c["id"] in seen:
            raise ValueError(f"{path}:{i}: duplicate id {c['id']!r}")
        seen.add(c["id"])
        claims.append(c)
    return claims


def make_probe(flox_bin):
    """Return help_for(argv) -> text, caching per command path.

    The whole flox surface this module touches, so a test replaces exactly
    this one function and never spawns a process.
    """
    cache = {}

    def help_for(argv):
        key = tuple(argv)
        if key not in cache:
            proc = subprocess.run(
                [flox_bin, *argv, "--help"],
                capture_output=True, text=True, timeout=HELP_TIMEOUT,
            )
            # flox prints help to stdout; keep stderr as a fallback so a
            # future change of stream does not silently empty every check.
            cache[key] = proc.stdout or proc.stderr or ""
        return cache[key]

    return help_for


def _commands(help_text):
    """Subcommand names in a `--help` listing, aliases expanded."""
    names = set()
    for line in help_text.splitlines():
        m = _COMMAND_LINE.match(line)
        if m:
            names.update(p.strip() for p in m.group("names").split(","))
    return names


def _flags(help_text):
    return set(_FLAG_TOKEN.findall(help_text))


def check_claim(claim, help_for):
    """Verify one claim against the flox surface."""
    if claim["kind"] == "command_exists":
        name = claim["command"][-1]
        parent = claim["command"][:-1]
        found = _commands(help_for(parent))
        if name in found:
            return Result(True, f"`flox {' '.join(claim['command'])}` exists")
        return Result(False, f"`{name}` is not a subcommand of `flox {' '.join(parent)}`".rstrip())

    flag = claim["flag"]
    found = _flags(help_for(claim["command"]))
    if flag in found:
        return Result(True, f"`{flag}` exists on `flox {' '.join(claim['command'])}`")
    return Result(False, f"`{flag}` is not a flag of `flox {' '.join(claim['command'])}`")


def check_quote(claim, skill_root):
    """Verify the entry still points at text that exists.

    A claim can be true of flox and still be dead weight: if the skill no
    longer says the thing, the entry guards nothing and its id will never
    lead anyone to a real line.
    """
    ref = claim["skill_ref"]
    path = Path(skill_root) / ref["file"]
    if not path.is_file():
        return Result(False, f"skill file not found: {ref['file']}")
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if ref["quote"] in line:
            return Result(True, f"{ref['file']}:{n}", n)
    return Result(False, f"quote not found in {ref['file']}: {ref['quote']!r}")


def suggest(claim, help_for):
    """The current surface, for printing beside a stale line.

    Deliberately not a rewrite. Confident for "this is gone" and "this
    exists now"; a rename substitution is a guess, and guessing in an
    auto-suggestion is worse than leaving the human to look.
    """
    if claim["kind"] == "command_exists":
        parent = claim["command"][:-1]
        current = sorted(_commands(help_for(parent)))
        scope = f"flox {' '.join(parent)}".rstrip()
        return f"subcommands of `{scope}` today: {', '.join(current)}"
    current = sorted(_flags(help_for(claim["command"])))
    return f"flags of `flox {' '.join(claim['command'])}` today: {', '.join(current)}"


def run(claims_path, skill_root, flox_bin, show_suggestions):
    try:
        claims = load_claims(claims_path)
    except (OSError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if not shutil.which(flox_bin) and not Path(flox_bin).is_file():
        print(f"ERROR: flox not found: {flox_bin}", file=sys.stderr)
        return 2

    help_for = make_probe(flox_bin)
    drifted = []
    for c in claims:
        quote = check_quote(c, skill_root)
        try:
            claim_r = check_claim(c, help_for)
        except (subprocess.TimeoutExpired, OSError) as e:
            print(f"ERROR: probing `flox {' '.join(c['command'])}`: {e}", file=sys.stderr)
            return 2
        if claim_r.ok and quote.ok:
            print(f"  PASS  {c['id']:32} {quote.detail}")
            continue
        drifted.append(c)
        print(f"  DRIFT {c['id']:32} {quote.detail if quote.ok else ''}")
        if not claim_r.ok:
            print(f"          {claim_r.detail}")
        if not quote.ok:
            print(f"          {quote.detail}")
        if show_suggestions and not claim_r.ok:
            print(f"          {suggest(c, help_for)}")

    print(f"\n{len(claims) - len(drifted)}/{len(claims)} claims hold.")
    if drifted:
        print("Drift is not necessarily a skill bug: the claim may be right and "
              "the registry stale. Check both ends before editing.")
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--claims", type=Path, default=DEFAULT_CLAIMS)
    ap.add_argument("--skill-root", type=Path, default=DEFAULT_SKILL_ROOT)
    ap.add_argument("--flox", default="flox")
    ap.add_argument("--suggest", action="store_true",
                    help="print the current surface beside each drifted claim")
    a = ap.parse_args()
    return run(a.claims, a.skill_root, a.flox, a.suggest)


if __name__ == "__main__":
    sys.exit(main())
