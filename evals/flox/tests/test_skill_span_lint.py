#!/usr/bin/env python3
"""Guard: no inline code span may wrap across a line break while holding a
placeholder that reads as an XML tag.

claudelint's skill-xml-tags-anywhere strips fenced blocks and inline code,
then flags XML-like tags in whatever survives. Its stripper works one line
at a time, so a span broken across a newline is never recognised as code and
any `<placeholder>` inside it is reported as a rogue tag -- an error, on the
grounds that stray tags can steer Claude's prompt parsing.

Six such errors shipped for months across the flox and floxify skills. The
defect is invisible on inspection: the markdown renders correctly, the prose
reads normally, and the tags are plainly inside backticks when you look at
the source. Only the line break gives it away, and it reappears the moment
anyone reflows a paragraph.

Three concerns, in the order they can silently lose teeth:

  1. The detector -- fences, double-backtick spans, and the allowed-HTML
     list. A detector that quietly matches nothing reports "0 failed"
     forever, so it is exercised against a synthetic defect first.
  2. Scope -- a wrapped span WITHOUT a tag is fine and must not be flagged,
     or the guard demands 30-odd unrelated reflows and gets switched off.
  3. The shipped skills themselves. Free, so it gates per-PR.

    python3 -m unittest tests.test_skill_span_lint -v
"""
import pathlib
import re
import unittest

# Mirrors claudelint 0.5.0's rule: dist/rules/skills/skill-xml-tags-anywhere.js
ALLOWED_TAGS = set(
    "a b i em strong code pre p br hr img ul ol li h1 h2 h3 h4 h5 h6 table tr "
    "td th thead tbody details summary blockquote div span sub sup del s dd dl "
    "dt kbd var samp picture source video audio".split()
)
XML_TAG = re.compile(r"<\/?([a-zA-Z][a-zA-Z0-9_-]*)\b[^>]{0,200}\/?>")
AUTOLINK = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]{1,31}:\S+$")

SKILLS_ROOT = pathlib.Path(__file__).resolve().parents[3] / "flox-plugin" / "skills"


def wrapped_spans_with_tags(text):
    """Return (line, tag) for every inline span that crosses a newline while
    containing a tag claudelint would treat as rogue."""
    hits, fenced, buf, start = [], False, None, None
    for lineno, line in enumerate(text.split("\n"), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        # ``literal backticks`` are balanced on their own line; drop them
        # before counting so they cannot fake an unclosed span.
        odd = re.sub(r"``[^`]*``", "", line).count("`") % 2
        if buf is not None:
            buf.append(line)
            if odd:
                span = "\n".join(buf)
                for m in XML_TAG.finditer(span):
                    inner = m.group(0)[1:-1]
                    if m.group(1).lower() not in ALLOWED_TAGS and not AUTOLINK.match(inner):
                        hits.append((start, m.group(0)))
                        break
                buf, start = None, None
        elif odd:
            buf, start = [line], lineno
    return hits


class TestDetectorHasTeeth(unittest.TestCase):
    """Concern 1: prove it fails on the real defect before trusting a pass."""

    def test_flags_the_defect_that_shipped(self):
        # Verbatim shape of flox/SKILL.md before the fix.
        text = (
            "- **Never invent a package name.** Verify names with `flox search\n"
            "  <term>` and versions with `flox show <pkg>`; pin only to a version\n"
        )
        self.assertEqual([(1, "<term>")], wrapped_spans_with_tags(text))

    def test_flags_a_multiline_quoted_message(self):
        text = "Note: `First activate installs <N> pip packages.\nSubsequent activates skip.`\n"
        self.assertEqual([(1, "<N>")], wrapped_spans_with_tags(text))


class TestDetectorScope(unittest.TestCase):
    """Concern 2: a guard that over-reaches gets switched off."""

    def test_single_line_span_is_fine(self):
        self.assertEqual([], wrapped_spans_with_tags("Run `flox show <pkg>` first.\n"))

    def test_wrapped_span_without_a_tag_is_fine(self):
        text = "Pull changes in with `flox\ninclude upgrade` when ready.\n"
        self.assertEqual([], wrapped_spans_with_tags(text))

    def test_fenced_block_is_fine(self):
        text = "```\nflox show <pkg>\nmore <tags>\n```\n"
        self.assertEqual([], wrapped_spans_with_tags(text))

    def test_allowed_html_is_fine(self):
        text = "See the note `in\n<details>` below.\n"
        self.assertEqual([], wrapped_spans_with_tags(text))


class TestShippedSkills(unittest.TestCase):
    """Concern 3: the documents we actually ship."""

    def test_no_shipped_document_wraps_a_span_around_a_tag(self):
        offenders = []
        for path in sorted(SKILLS_ROOT.rglob("*.md")):
            for lineno, tag in wrapped_spans_with_tags(path.read_text()):
                rel = path.relative_to(SKILLS_ROOT.parent.parent)
                offenders.append(f"{rel}:{lineno} {tag}")
        self.assertEqual(
            [],
            offenders,
            "inline code span wraps across a line break around an XML-like "
            "placeholder; claudelint will report the placeholder as a rogue "
            "tag. Rejoin the span onto one line, or fence it:\n  "
            + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
