"""Reading a published policy page as sections rather than as one long string.

Issue #21 asks that the policy corpus land "with section structure intact",
and it says why: a terms document is chunked for retrieval *by section*, not
by a fixed window. Deciding the window belongs to the retrieval ticket, but
the boundaries have to survive the harvest or there is nothing left to chunk
on — a page flattened to one paragraph cannot be un-flattened later.

Chipotle's rewards terms are one authored text block containing about twenty
sections, and the page marks each section's heading the plainest way there
is::

    <p><b>ELIGIBILITY</b></p>
    <p>Chipotle Rewards is open to legal residents of ...</p>

So the rule here is: a paragraph whose *entire* text came from inside a ``<b>``
or ``<strong>`` is a heading and starts a section. That distinguishes the
headings above from the many bold *runs* inside a section, which are lead-ins
rather than boundaries::

    <p><b>Mandatory Pre-Arbitration Informal Dispute Resolution: </b>Should a
    Dispute arise between you and the Chipotle Entities, ...</p>

A bullet in front of the bold run does not stop it being a heading — the terms
decorate one section that way — but any other text does. An ``<h1>`` to ``<h6>``
inside a block is a heading too, and each authored block starts a section of its
own, so a page with no bold headings at all still lands as its published blocks
rather than as one wall of text.

**What this deliberately does not do is chase CSS classes.** The rewards
landing page marks its own FAQ questions bold with a class rather than a
``<b>``, and those questions therefore land inside a section instead of
starting one. Tuning a selector to today's class name is how a parser comes to
return nothing after a content edit and say nothing about it — and that page's
FAQ is published properly, with real structure, in the FAQ endpoint this same
dataset reads. Losing a boundary on a page that has a better copy elsewhere is
the cheaper mistake.
"""

import re
from dataclasses import dataclass
from html.parser import HTMLParser

from chip_chat.harvest.sources.chipotle.caveats import MAIN_TAG, TEXT_BLOCK_CLASS
from chip_chat.harvest.sources.chipotle.errors import ChipotleSourceError

TITLE_CLASS = "cmp-title"
"""The class the site's authoring tool puts on a page's own title component.

The rewards terms carry their ``<h1>`` in one of these, outside the text block
holding the document, which is why the title comes back beside the sections
rather than as the first of them.
"""

_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_BOLD = frozenset({"b", "strong"})
_UNITS = frozenset({"p", "li", "tr"}) | _HEADINGS
_BREAKS = frozenset({"br", "div"}) | _UNITS
_SKIP = frozenset({"script", "style"})
_MARKERS = " \t\n\r\x0b\x0c\xa0\u00b7\u2022\u25aa\u25e6\u2023*\u2013\u2014-"
"""Whitespace, plus the bullets a heading may be decorated with.

The terms put ``·&nbsp;&nbsp;&nbsp;`` in front of ``CLASS ACTION WAIVER AND
JURY TRIAL WAIVER``, outside the ``<b>``. Requiring the *whole* paragraph to be
bold would fold that section into the seventeen-thousand-character one above it
over a bullet character, which is a boundary lost to punctuation.
"""


@dataclass(frozen=True, slots=True)
class Section:
    """One section of a published document, as the page laid it out.

    Attributes:
        heading: The heading that opened the section, or ``None`` for the
            prose that precedes the first one.
        text: The section's visible text, with the breaks between paragraphs
            preserved as newlines and nothing else altered.
    """

    heading: str | None
    text: str


@dataclass(frozen=True, slots=True)
class Document:
    """A whole published page, split up.

    Attributes:
        title: The page's own title component, where it publishes one.
        sections: Its authored prose, in the order the page published it.
    """

    title: str | None
    sections: tuple[Section, ...]


class _SectionCollector(HTMLParser):
    """Splits the authored text blocks inside ``<main>`` into sections.

    Three buffers are in play at once, which is the whole of the complexity:
    ``_unit`` holds the paragraph being read, ``_plain`` holds only the part
    of it that was *not* inside a ``<b>`` — so an empty ``_plain`` is what
    "this paragraph is a heading" looks like — and ``_body`` holds the
    paragraphs collected since the last heading.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sections: list[Section] = []
        self.title: str | None = None
        self._depth = 0
        self._in_main = 0
        self._block_depth: int | None = None
        self._skip_depth: int | None = None
        self._title_depth: int | None = None
        self._unit_depth: int | None = None
        self._bold_depth = 0
        self._heading: str | None = None
        self._body: list[str] = []
        self._unit: list[str] = []
        self._plain: list[str] = []
        self._title_parts: list[str] = []
        self._block_start = 0
        self._previous_block: list[Section] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            if self._skip_depth is None:
                self._skip_depth = self._depth
            self._depth += 1
            return
        if tag == MAIN_TAG:
            self._in_main += 1
        classes = _classes(attrs)
        if self.title is None and self._title_depth is None and TITLE_CLASS in classes:
            self._title_depth = self._depth
            self._title_parts = []
        if self._in_main and self._block_depth is None and TEXT_BLOCK_CLASS in classes:
            self.finish()
            self._block_depth = self._depth
            self._block_start = len(self.sections)
        if self._block_depth is not None:
            if tag in _UNITS and self._unit_depth is None:
                self._start_unit()
            elif self._unit_depth is not None and tag in _BOLD:
                self._bold_depth += 1
            elif tag in _BREAKS:
                self._unit.append("\n")
        self._depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._block_depth is not None and tag in _BREAKS:
            self._unit.append("\n")

    def handle_endtag(self, tag: str) -> None:
        self._depth = max(self._depth - 1, 0)
        if self._skip_depth is not None:
            if self._depth == self._skip_depth:
                self._skip_depth = None
            return
        if self._title_depth is not None and self._depth == self._title_depth:
            self.title = _tidy("".join(self._title_parts)) or None
            self._title_depth = None
        if self._unit_depth is not None and tag in _BOLD and self._bold_depth:
            self._bold_depth -= 1
        if self._unit_depth is not None and self._depth == self._unit_depth:
            self._close_unit(tag)
        if self._block_depth is not None and self._depth == self._block_depth:
            self.finish()
            self._close_block()
        if tag == MAIN_TAG and self._in_main:
            self._in_main -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth is not None:
            return
        if self._title_depth is not None:
            self._title_parts.append(data)
        if self._block_depth is None:
            return
        if self._unit_depth is None:
            # Text a block carries outside any paragraph of its own. Rare, and
            # kept rather than dropped: prose that vanishes because the markup
            # around it was unusual is exactly the silent loss this module is
            # written to avoid.
            self._start_unit()
        self._unit.append(data)
        if not self._bold_depth:
            self._plain.append(data)

    def finish(self) -> None:
        """Emit whatever has accumulated. Idempotent, and safe to over-call."""
        if self._unit_depth is not None:
            self._close_unit("p")
        body = "\n".join(self._body)
        if self._heading is not None or body:
            self.sections.append(Section(heading=self._heading, text=body))
        self._heading = None
        self._body = []

    def _close_block(self) -> None:
        """End an authored block, discarding it if it repeats the last one.

        The site renders several of its blocks twice, once per breakpoint, and
        the copies are not adjacent section by section — two blocks of two
        sections each arrive interleaved as A, B, A, B. Comparing whole blocks
        rather than neighbouring sections is what tells that apart from a page
        that really does say the same thing twice.
        """
        block = self.sections[self._block_start :]
        if block and block == self._previous_block:
            del self.sections[self._block_start :]
        elif block:
            self._previous_block = block
        self._block_depth = None

    def _start_unit(self) -> None:
        """Begin reading one paragraph."""
        self._unit_depth = self._depth
        self._unit = []
        self._plain = []
        self._bold_depth = 0

    def _close_unit(self, tag: str) -> None:
        """End the current paragraph, starting a section if it was a heading."""
        text = _tidy("".join(self._unit))
        plain = "".join(self._plain)
        self._unit_depth = None
        self._unit = []
        self._plain = []
        self._bold_depth = 0
        if not text:
            return
        if tag in _HEADINGS or not plain.strip(_MARKERS):
            self.finish()
            self._heading = text
            return
        self._body.append(text)


def parse_document(html: str, source_url: str) -> Document:
    """Return a page's title and its authored prose, split into sections.

    An authored block that merely repeats the block before it is dropped. The
    site renders several of them twice, once for each breakpoint, and two
    copies of the same words are a fact about the markup rather than about
    what was published.

    Args:
        html: The page source.
        source_url: Where it came from. Used only in the error message.

    Returns:
        The parsed document.

    Raises:
        ChipotleSourceError: If the page carries no authored text inside a
            ``<main>`` element. A policy corpus that quietly shipped without
            the document it is named after would be worse than one that
            refused to build.
    """
    collector = _SectionCollector()
    collector.feed(html)
    collector.finish()
    if not collector.sections:
        raise ChipotleSourceError(
            f"{source_url} published no text inside <main>; the policy "
            f"document this dataset has to carry is not there to read"
        )
    return Document(title=collector.title, sections=tuple(collector.sections))


def _classes(attrs: list[tuple[str, str | None]]) -> frozenset[str]:
    """Return an element's classes as a set."""
    for name, value in attrs:
        if name == "class":
            return frozenset((value or "").split())
    return frozenset()


def _tidy(text: str) -> str:
    """Collapse whitespace within lines, keeping the line breaks between them."""
    lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)
