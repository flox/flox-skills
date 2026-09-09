#!/usr/bin/env python3
"""Guard: no shipped skill document may leak an XML-like tag out of code
context into the text an agent reads.

claudelint's skill-xml-tags-anywhere strips fenced blocks and inline code
from a document, then errors on XML-like tags in whatever survives, on the
grounds that stray tags can steer Claude's prompt parsing. Its stripper
works one line at a time (`/`[^`]*`/g` per line, dist/utils/formats/
markdown.js), so backticks are paired within a line and never across one.

That makes several innocuous-looking shapes leak. Six errors shipped this
way, and none is visible on inspection -- the markdown renders correctly,
the prose reads normally, and the placeholders are plainly inside backticks
when you look at the source:

  * A span that WRAPS a line break is never recognised as code, exposing
    what it contains.
  * Worse, the unpaired backtick cascades: the rest of that line re-pairs
    wrongly, exposing tags in spans that looked perfectly closed. This is
    how `flox show <pkg>` leaked while sitting on one line.
  * A ``double-backtick`` span leaks always, since the stripper matches the
    two pairs and leaves the middle behind.

So this does not pattern-match the shapes. It replays the stripper and
applies the rule, which is exact by construction: whatever claudelint would
report on a SKILL.md, this reports first, and it cannot drift into a
heuristic that approximates the rule instead of stating it.

Scope is every *.md under the skills tree, not only SKILL.md. claudelint
itself returns early on any other filename, so a reference file cannot fail
its rule -- but references are loaded into the agent's context by the same
skill, a leaked tag reaches the model identically, and reference content
gets promoted into SKILL.md often enough that a latent leak becomes a real
one without anyone editing the line. The claudelint rule is the mechanism
that makes this detectable, not the whole reason to care.

Three concerns, in the order they can silently lose teeth:

  1. The replay -- fences (both ``` and ~~~), per-line pairing, the
     allowed-HTML list. Exercised against defects that really shipped.
  2. Scope -- what must NOT be flagged, so the guard does not demand
     unrelated reflows and get switched off.
  3. The shipped documents, including a check that the scan found them at
     all: an empty glob makes an empty offender list, which is a green that
     means "looked at nothing".

    python3 -m unittest tests.test_skill_span_lint -v
"""
import pathlib
import re
import unittest

# Ported from claudelint 0.5.0:
#   dist/rules/skills/skill-xml-tags-anywhere.js
#   dist/utils/formats/markdown.js  (stripCodeBlocks)
ALLOWED_TAGS = set(
    "a b i em strong code pre p br hr img ul ol li h1 h2 h3 h4 h5 h6 table tr "
    "td th thead tbody details summary blockquote div span sub sup del s dd dl "
    "dt kbd var samp picture source video audio".split()
)
XML_TAG = re.compile(r"<\/?([a-zA-Z][a-zA-Z0-9_-]*)\b[^>]{0,200}\/?>")
AUTOLINK = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]{1,31}:\S+$")
INLINE_CODE = re.compile(r"`[^`]*`")

SKILLS_ROOT = pathlib.Path(__file__).resolve().parents[3] / "flox-plugin" / "skills"


def strip_code(text):
    """Replay stripCodeBlocks: drop fenced blocks, then strip inline code one
    line at a time. Returns a list of lines, blanked where content was
    removed, so line numbers survive."""
    out, fence = [], None
    for line in text.split("\n"):
        head = line.lstrip()
        if fence is None:
            if head.startswith("```") or head.startswith("~~~"):
                fence = head[0]
                out.append("")
                continue
        else:
            # Only the same fence character closes the block, which is why a
            # ``` inside a ~~~ block must not reopen anything.
            if head.startswith(fence * 3):
                fence = None
            out.append("")
            continue
        out.append(INLINE_CODE.sub("", line))
    return out


def leaked_tags(text):
    """Return (line, tag) for every XML-like tag claudelint's rule would see."""
    hits = []
    for lineno, line in enumerate(strip_code(text), 1):
        for m in XML_TAG.finditer(line):
            if m.group(1).lower() in ALLOWED_TAGS:
                continue
            if AUTOLINK.match(m.group(0)[1:-1]):
                continue
            hits.append((lineno, m.group(0)))
    return hits


class TestReplayHasTeeth(unittest.TestCase):
    """Concern 1: prove it reports the defects that really shipped."""

    def test_span_wrapping_a_line_break(self):
        text = (
            "- **Never invent a package name.** Verify names with `flox search\n"
            "  <term>` and versions with `flox show <pkg>`; pin only to a version\n"
        )
        # <term> leaks from the wrapped span; <pkg> leaks from the cascade,
        # in a span that looks correctly closed. Both really shipped.
        self.assertEqual([(2, "<term>"), (2, "<pkg>")], leaked_tags(text))

    def test_multiline_quoted_message(self):
        text = "Note: `First activate installs <N> pip packages.\nSubsequent activates skip.`\n"
        self.assertEqual([(1, "<N>")], leaked_tags(text))

    def test_double_backtick_span_leaks_on_one_line(self):
        self.assertEqual([(1, "<foo>")], leaked_tags("Use ``<foo>`` as the placeholder.\n"))

    def test_a_stray_backtick_does_not_blind_the_rest_of_the_file(self):
        text = (
            "A lone backtick ` in prose.\n"
            "\n"
            "Verify names with `flox search\n"
            "<term>` before installing.\n"
        )
        self.assertIn((4, "<term>"), leaked_tags(text))


class TestReplayScope(unittest.TestCase):
    """Concern 2: a guard that over-reaches gets switched off."""

    def test_single_line_span_is_fine(self):
        self.assertEqual([], leaked_tags("Run `flox show <pkg>` first.\n"))

    def test_wrapped_span_without_a_tag_is_fine(self):
        self.assertEqual([], leaked_tags("Pull changes in with `flox\ninclude upgrade` now.\n"))

    def test_fenced_block_is_fine(self):
        self.assertEqual([], leaked_tags("```\nflox show <pkg>\nmore <tags>\n```\n"))

    def test_tilde_fence_is_fine(self):
        self.assertEqual([], leaked_tags("~~~\nflox show <pkg>\n~~~\n"))

    def test_allowed_html_and_autolinks_are_fine(self):
        self.assertEqual([], leaked_tags("See <details> and <https://flox.dev> below.\n"))


class TestShippedSkills(unittest.TestCase):
    """Concern 3: the documents we actually ship."""

    def test_scan_reaches_the_shipped_documents(self):
        # An empty glob yields an empty offender list, which passes the test
        # below while having looked at nothing.
        docs = list(SKILLS_ROOT.rglob("*.md"))
        self.assertIn("SKILL.md", {p.name for p in docs}, f"no SKILL.md under {SKILLS_ROOT}")
        self.assertGreater(len(docs), 3, "references/*.md should be picked up too")

    def test_no_shipped_document_leaks_a_tag(self):
        offenders = []
        for path in sorted(SKILLS_ROOT.rglob("*.md")):
            rel = path.relative_to(SKILLS_ROOT.parent.parent)
            for lineno, tag in leaked_tags(path.read_text()):
                offenders.append(f"{rel}:{lineno} {tag}")
        self.assertEqual(
            [],
            offenders,
            "an XML-like tag reaches the text an agent reads, because the "
            "backticks around it do not pair within one line. Rejoin the span "
            "onto a single line, or fence it:\n  " + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
