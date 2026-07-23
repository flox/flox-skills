#!/usr/bin/env python3
"""Parse-check every TOML snippet in the `flox` skill with `flox edit -f` (AI-494).

The floxify evals verify manifests the model *generates*; nothing verified the
manifests the skill *ships*. A 2026-07-22 validation pass (1a8119c) found four
classes of hand-written snippet in `SKILL.md` / `references/*.md` that fail
`flox edit` outright — a user copying them hits a parse error (`is-daemon` with no
shutdown command, an invented `[include]` version field, bare `systems` under
`[install]`, a `[nodejs]` table). That commit fixed them by hand and left the
regression guard to AI-494. This is that guard.

Running it over the whole skill immediately found five more of the same kind that
the manual pass had missed — three CUDA `[hook]` blocks holding bare shell lines
instead of `on-activate = '''...'''`, a `[profile.common]` shell table, and a
multi-line inline `labels` table — all fixed alongside this file. That is the
argument for the guard in one sentence: reading for parse errors does not scale,
and `flox edit` never misses one.

Method (the one that surfaced the bugs, on flox 1.13.2): `flox init` a throwaway
env once, then `flox edit -f <snippet>` for every fenced ```toml block, prepending
`version = 1` when the snippet omits it.

Two tiers, because `flox edit` does two things:

  structural (default, BINDING)
      Only `Failed to parse manifest` counts as a failure. flox parses the whole
      manifest before it resolves anything, so this catches every bug above --
      including the `[install]` ones -- without needing the catalog. Runs offline:
      pass --offline to point the proxy vars at a closed port so a networked
      machine behaves exactly like an air-gapped one. Deterministic and fast
      (~25ms per snippet); this is what CI gates on.

  catalog (opt-in, ADVISORY)
      Additionally requires every snippet to resolve against the live catalog, so
      `flox edit` must exit 0. Needs network, takes seconds per `[install]` block,
      and can fail for reasons that have nothing to do with the skill (catalog
      outage, a package legitimately renamed). Report-only by design; run it to
      find snippets that parse but no longer resolve.

Snippets that are deliberately partial -- or that aren't flox manifests at all --
opt out with an explicit marker, either of:

    ```toml
    # eval: skip <reason>          <- a standalone comment line in the block
    ```

    ```toml-fragment               <- the fence's info string
    ```

Prefer the comment form: it keeps ```toml syntax highlighting and forces you to
write down *why*. Never add a marker to silence a real parse error -- fix the
snippet, or allowlist it in KNOWN_PARSE_FAILURES below. See evals/README.md
("Skill TOML snippet guard").

Usage:
    python3 skill_toml_lint.py                          # structural tier
    python3 skill_toml_lint.py --offline                # ... and prove it needs no network
    python3 skill_toml_lint.py --tier catalog           # + live catalog resolution
    python3 skill_toml_lint.py --only references/services.md
    python3 skill_toml_lint.py --list                   # extract only, no flox
    python3 skill_toml_lint.py --json results/skill-toml-lint.json

Exit 0 if every checked snippet passed its tier, 1 otherwise. Pure stdlib.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_SKILL_DIR = HERE.parent / "flox-plugin" / "skills" / "flox"

# ```toml / ```toml-fragment ... ```  (indented fences included: the skill nests
# blocks under list items). The closing fence must match the opening indent+run.
_FENCE_OPEN = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>```+)(?P<info>[^\s`]*)[ \t]*$")

# A standalone comment line opting the block out, with a mandatory reason:
#   # eval: skip not a flox manifest (pyproject.toml)
_SKIP_MARKER = re.compile(r"^[ \t]*#[ \t]*eval:[ \t]*skip\b[ \t]*(?P<reason>.*)$", re.M)

# Info strings that mean "this is a deliberate fragment, don't parse it".
_FRAGMENT_INFO = {"toml-fragment"}

# Any snippet flox will *parse*, whether or not it's a complete manifest.
_MANIFEST_INFO = {"toml"} | _FRAGMENT_INFO

# `version = 1` at the top level, i.e. before the first table header.
_VERSION_KEY = re.compile(r"^[ \t]*version[ \t]*=", re.M)
_TABLE_HEADER = re.compile(r"^[ \t]*\[", re.M)

# [install], [install.foo], or a top-level `install.foo = ...` dotted key. Only
# used to label results -- both tiers run every snippet.
_INSTALL_SECTION = re.compile(r"^[ \t]*(\[install[.\]]|install[ \t]*\.)", re.M)

# flox's own wording. Everything flox rejects at parse time is prefixed with
# this; catalog failures ("catalog error: ...", "could not be resolved") are not.
_PARSE_ERROR = re.compile(r"Failed to parse manifest", re.I)

# Statuses, most-severe first.
PARSE_ERROR = "parse-error"
CATALOG_ERROR = "catalog-error"
OTHER_ERROR = "other-error"
KNOWN_FAILURE = "known-failure"
OK = "ok"
SKIPPED = "skipped"


# --- known failures ---------------------------------------------------------
#
# Escape hatch for snippets that are NOT fragments -- they are meant to be real
# manifests and they genuinely do not parse -- when the fix is too large to land
# alongside the change that caught them. Same discipline as
# evals/floxify/test_golden_lint.py's KNOWN_VIOLATIONS.
#
# It is EMPTY, and that is the intended steady state: the five snippets this
# guard found on its first run (three `[hook]`-as-bare-shell CUDA examples, a
# `[profile.common]` shell table, and a multi-line inline `labels` table) were
# fixed in the same PR rather than allowlisted, so the guard ships with real
# teeth instead of a pre-loaded excuse list.
#
# Keyed by (document, fingerprint) -- a content hash, not a line number, so an
# entry survives edits elsewhere in the file and DIES the moment the snippet is
# fixed. Stale entries are themselves a failure (see stale_allowlist_entries),
# so a fix that forgets to remove its entry can't leave a dead slot that
# silently absorbs a future regression.
#
# Prefer fixing the snippet. Never add an entry just to make a red build green.
KNOWN_PARSE_FAILURES = {}


def fingerprint(body):
    """Stable content id for a block -- allowlist key, immune to line drift."""
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()[:10]


class Block:
    """One fenced TOML block lifted out of a skill document."""

    def __init__(self, path, line, info, body, skip_reason=None):
        self.path = path          # display path, e.g. "references/services.md"
        self.line = line          # 1-based line of the opening fence
        self.info = info          # fence info string ("toml", "toml-fragment")
        self.body = body          # block contents, verbatim
        self.skip_reason = skip_reason

    @property
    def id(self):
        return "%s:%d" % (self.path, self.line)

    @property
    def has_install(self):
        return bool(_INSTALL_SECTION.search(self.body))

    @property
    def fingerprint(self):
        return fingerprint(self.body)

    @property
    def allowlist_key(self):
        return (self.path, self.fingerprint)

    def manifest_text(self):
        """The snippet as flox will see it: `version = 1` prepended if absent.

        Only a *top-level* version counts -- `version` inside a table (e.g.
        [build.foo] version = "1.0") is a different key entirely.
        """
        head = self.body
        first_table = _TABLE_HEADER.search(head)
        if first_table:
            head = head[: first_table.start()]
        if _VERSION_KEY.search(head):
            return self.body
        return "version = 1\n" + self.body


def extract_blocks(text, display_path):
    """Every ```toml / ```toml-fragment block in `text`, in document order."""
    blocks = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _FENCE_OPEN.match(lines[i])
        if not m or m.group("info") not in _MANIFEST_INFO:
            i += 1
            continue
        indent, fence, info = m.group("indent"), m.group("fence"), m.group("info")
        start = i
        body_lines = []
        i += 1
        closed = False
        while i < len(lines):
            close = _FENCE_OPEN.match(lines[i])
            if close and close.group("info") == "" and len(close.group("fence")) >= len(fence):
                closed = True
                break
            body_lines.append(lines[i])
            i += 1
        if not closed:
            # An unterminated fence is a documentation bug of its own; surface it
            # rather than silently swallowing the rest of the file.
            raise ValueError("%s:%d: unterminated ```%s fence" % (display_path, start + 1, info))
        body = _dedent(body_lines, indent)
        skip_reason = None
        if info in _FRAGMENT_INFO:
            skip_reason = "```%s fence" % info
        else:
            marker = _SKIP_MARKER.search(body)
            if marker:
                skip_reason = marker.group("reason").strip() or "(no reason given)"
        blocks.append(Block(display_path, start + 1, info, body, skip_reason))
        i += 1
    return blocks


def _dedent(body_lines, indent):
    out = []
    for line in body_lines:
        out.append(line[len(indent):] if indent and line.startswith(indent) else line)
    return "\n".join(out) + ("\n" if out else "")


def skill_documents(skill_dir):
    """SKILL.md plus every references/*.md, in a stable order."""
    skill_dir = Path(skill_dir)
    docs = []
    skill_md = skill_dir / "SKILL.md"
    if skill_md.is_file():
        docs.append(skill_md)
    docs.extend(sorted((skill_dir / "references").glob("*.md")))
    return docs


def collect_blocks(skill_dir, only=None):
    skill_dir = Path(skill_dir)
    blocks = []
    for doc in skill_documents(skill_dir):
        display = doc.relative_to(skill_dir).as_posix()
        if only and only not in display:
            continue
        blocks.extend(extract_blocks(doc.read_text(encoding="utf-8"), display))
    return blocks


def classify(returncode, output):
    """Map a `flox edit -f` result onto a status.

    The tier split lives here: flox parses the manifest *before* it resolves
    anything, so a parse error is reported even with no network, while catalog
    failures never carry the parse prefix.
    """
    if returncode == 0:
        return OK
    if _PARSE_ERROR.search(output or ""):
        return PARSE_ERROR
    if re.search(r"catalog error|could not be resolved|resolution fail", output or "", re.I):
        return CATALOG_ERROR
    return OTHER_ERROR


def _offline_env(env):
    """Force catalog resolution to fail immediately, without touching the network.

    A closed local port, so the structural tier behaves identically on a
    networked runner and an air-gapped one -- and can't be slowed or flaked by
    the catalog it deliberately does not depend on.
    """
    dead = "http://127.0.0.1:1"
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env[var] = dead
    env["NO_PROXY"] = ""
    env["no_proxy"] = ""
    return env


def check_blocks(blocks, tier="structural", offline=False, flox="flox", env_dir=None):
    """Run every block through `flox edit -f` in one throwaway env."""
    env = dict(os.environ)
    env["FLOX_DISABLE_METRICS"] = "true"
    if offline:
        _offline_env(env)

    tmp = None
    if env_dir is None:
        tmp = tempfile.mkdtemp(prefix="skill-toml-lint-")
        env_dir = tmp
    try:
        init = subprocess.run(
            [flox, "init", "-n", "skill-toml-lint"],
            cwd=env_dir, env=env, capture_output=True, text=True,
        )
        if init.returncode != 0:
            raise RuntimeError("flox init failed:\n%s%s" % (init.stdout, init.stderr))

        snippet = Path(env_dir) / "snippet.toml"
        results = []
        for block in blocks:
            if block.skip_reason:
                results.append(_result(block, SKIPPED, "", tier))
                continue
            snippet.write_text(block.manifest_text(), encoding="utf-8")
            proc = subprocess.run(
                [flox, "edit", "-f", str(snippet)],
                cwd=env_dir, env=env, capture_output=True, text=True,
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            status = classify(proc.returncode, output)
            if status == PARSE_ERROR and block.allowlist_key in KNOWN_PARSE_FAILURES:
                status = KNOWN_FAILURE
            results.append(_result(block, status, output.strip(), tier))
        return results
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def stale_allowlist_entries(results):
    """Allowlist keys that no longer match a failing block.

    A snippet that got fixed (or deleted) must take its entry with it, or the
    dead slot sits there ready to absorb an unrelated future regression.

    Only entries for documents actually in `results` are judged -- a `--only`
    run sees a subset of the skill and must not call the rest stale.
    """
    live = {(r["file"], r["fingerprint"]) for r in results if r["status"] == KNOWN_FAILURE}
    seen_files = {r["file"] for r in results}
    return sorted(
        key for key in set(KNOWN_PARSE_FAILURES) - live if key[0] in seen_files
    )


def _result(block, status, output, tier):
    return {
        "id": block.id,
        "file": block.path,
        "line": block.line,
        "info": block.info,
        "fingerprint": block.fingerprint,
        "status": status,
        "has_install": block.has_install,
        "skip_reason": block.skip_reason,
        "known_reason": KNOWN_PARSE_FAILURES.get(block.allowlist_key),
        "output": output,
        "failed": is_failure(status, tier),
    }


def is_failure(status, tier):
    """Which statuses bind, per tier.

    structural: only a parse error. A catalog error means "flox parsed this and
                then went looking for packages" -- the snippet passed this tier.
    catalog:    anything that isn't a clean exit 0 (except explicit skips).

    KNOWN_FAILURE never binds in either tier: it is an allowlisted parse error,
    reported loudly and burned down separately.
    """
    if status in (OK, SKIPPED, KNOWN_FAILURE):
        return False
    if tier == "catalog":
        return True
    return status == PARSE_ERROR


def summarize(results, tier):
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    stale = stale_allowlist_entries(results)
    return {
        "tier": tier,
        "blocks": len(results),
        "checked": sum(1 for r in results if r["status"] != SKIPPED),
        "skipped": counts.get(SKIPPED, 0),
        "known_failures": counts.get(KNOWN_FAILURE, 0),
        "failed": sum(1 for r in results if r["failed"]),
        "stale_allowlist_entries": ["%s %s" % (f, h) for f, h in stale],
        "by_status": counts,
    }


def _print_report(results, summary, verbose):
    for r in results:
        if r["failed"]:
            mark = "FAIL"
        elif r["status"] == KNOWN_FAILURE:
            mark = "KNOWN"
        elif r["status"] == SKIPPED:
            mark = "skip"
        else:
            mark = "ok"
        if r["failed"] or r["status"] == KNOWN_FAILURE or verbose:
            note = r["known_reason"] or r["skip_reason"] or r["status"]
            print("%-5s %s  [%s]  (%s)" % (mark, r["id"], r["fingerprint"], note))
        if r["failed"]:
            for line in (r["output"] or "").splitlines():
                print("        | %s" % line)
    print(
        "\n%s tier: %d block(s), %d checked, %d skipped, %d known-failure, %d failed"
        % (summary["tier"], summary["blocks"], summary["checked"], summary["skipped"],
           summary["known_failures"], summary["failed"])
    )
    for entry in summary["stale_allowlist_entries"]:
        print("STALE allowlist entry no longer matches any failing block: %s" % entry)
    if summary["stale_allowlist_entries"]:
        print("Remove it from KNOWN_PARSE_FAILURES in evals/skill_toml_lint.py.")
    if summary["failed"]:
        print(
            "\nA snippet in the flox skill does not parse. Fix the snippet -- or, if it is\n"
            "deliberately partial, mark it: add `# eval: skip <reason>` inside the block\n"
            "(see evals/README.md, 'Skill TOML snippet guard')."
        )


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--skill-dir", default=str(DEFAULT_SKILL_DIR),
                    help="skill directory holding SKILL.md + references/ (default: the flox skill)")
    ap.add_argument("--tier", choices=("structural", "catalog"), default="structural",
                    help="structural = parse only (default, offline-safe); catalog = also resolve")
    ap.add_argument("--offline", action="store_true",
                    help="force catalog resolution to fail fast; structural tier only")
    ap.add_argument("--only", help="substring filter on the document path, e.g. services.md")
    ap.add_argument("--list", action="store_true", help="list the extracted blocks and exit (no flox)")
    ap.add_argument("--json", help="write full results to this JSON file")
    ap.add_argument("--flox", default="flox", help="flox executable (default: flox)")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every block, not just failures")
    args = ap.parse_args(argv)

    blocks = collect_blocks(args.skill_dir, only=args.only)
    if not blocks:
        print("No ```toml blocks found under %s" % args.skill_dir, file=sys.stderr)
        return 1

    if args.list:
        for b in blocks:
            print("%s  [%s]  info=%s install=%s skip=%s"
                  % (b.id, b.fingerprint, b.info, b.has_install, b.skip_reason or "-"))
        print("\n%d block(s)" % len(blocks))
        return 0

    if args.offline and args.tier == "catalog":
        ap.error("--offline is incompatible with --tier catalog (which needs the catalog)")

    if shutil.which(args.flox) is None:
        print("flox not found (%s); skipping the live check." % args.flox, file=sys.stderr)
        return 0

    results = check_blocks(blocks, tier=args.tier, offline=args.offline, flox=args.flox)
    summary = summarize(results, args.tier)
    _print_report(results, summary, args.verbose)

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"summary": summary, "results": results}, indent=2), encoding="utf-8")
        print("wrote %s" % out)

    return 1 if (summary["failed"] or summary["stale_allowlist_entries"]) else 0


if __name__ == "__main__":
    sys.exit(main())
