"""Reading Chipotle's own words about the limits of its allergen chart.

The chart at ``/allergens`` is drawn in the browser from the allergen and diet
endpoint, so the interesting part of that page — for this project — is not the
chart. It is the prose around it, which is served in the HTML and published
nowhere else:

    Individual foods may come into contact with one another during
    preparation, which is not reflected on this chart. Although we do not use
    eggs, mustard, peanuts, tree nuts, sesame, shellfish, or fish as
    ingredients in our food, Chipotle cannot guarantee the complete absence of
    these allergens in its restaurants.

That paragraph is the reason
:class:`~...nutrition_records.ItemAllergen` has three statuses rather than a
boolean. Chipotle says in as many words that an item not marked with an
allergen is not thereby free of it, and PRD K3 requires Cilantro to pass that
on rather than round it off. So the wording is carried into the dataset
verbatim, hedges included, for an answer to quote.

Extraction is deliberately blunt: every published text block inside the page's
``<main>`` element, in document order, with the markup stripped and nothing
else changed. Blunt because a selector tuned to today's paragraph is a
selector that silently returns nothing after a content edit, and a dataset
that silently loses its safety caveat is worse than one that fails to build.
:func:`parse_caveats` raises when it finds nothing.
"""

import re
from dataclasses import dataclass
from html.parser import HTMLParser

from chip_chat.harvest.sources.chipotle.errors import ChipotleSourceError

MAIN_TAG = "main"
"""The element the page's own content lives in. Header and footer are outside
it, and their text blocks are navigation rather than anything published about
food."""

TEXT_BLOCK_CLASS = "cmp-text"
"""The class the site's authoring tool puts on every block of edited prose."""

_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_BREAKS = frozenset({"p", "br", "div", "li", "tr"}) | _HEADINGS
_SKIP = frozenset({"script", "style"})


@dataclass(frozen=True, slots=True)
class CaveatBlock:
    """One published block of prose, as it appeared on the page.

    Attributes:
        heading: The block's own heading, where it has one.
        text: The visible text, with paragraph breaks preserved as newlines
            and nothing else altered.
    """

    heading: str | None
    text: str


class _MainTextCollector(HTMLParser):
    """Collects the text of every ``cmp-text`` block inside ``<main>``."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[CaveatBlock] = []
        self._in_main = 0
        self._depth = 0
        self._block_depth: int | None = None
        self._skip_depth: int | None = None
        self._parts: list[str] = []
        self._heading: str | None = None
        self._heading_depth: int | None = None
        self._heading_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            if self._skip_depth is None:
                self._skip_depth = self._depth
            self._depth += 1
            return
        if tag == MAIN_TAG:
            self._in_main += 1
        classes = ""
        for name, value in attrs:
            if name == "class":
                classes = value or ""
        if (
            self._in_main
            and self._block_depth is None
            and TEXT_BLOCK_CLASS in classes.split()
        ):
            self._block_depth = self._depth
            self._parts = []
            self._heading = None
        if self._block_depth is not None:
            if tag in _HEADINGS and self._heading is None and self._heading_depth is None:
                self._heading_depth = self._depth
                self._heading_parts = []
            if tag in _BREAKS:
                self._parts.append("\n")
        self._depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._block_depth is not None and tag in _BREAKS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        self._depth = max(self._depth - 1, 0)
        if self._skip_depth is not None:
            if self._depth == self._skip_depth:
                self._skip_depth = None
            return
        if self._heading_depth is not None and self._depth == self._heading_depth:
            self._heading = _tidy("".join(self._heading_parts)) or None
            self._heading_depth = None
        if self._block_depth is not None and self._depth == self._block_depth:
            text = _tidy("".join(self._parts))
            if text:
                self.blocks.append(CaveatBlock(heading=self._heading, text=text))
            self._block_depth = None
            self._parts = []
            self._heading = None
        if tag == MAIN_TAG and self._in_main:
            self._in_main -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth is not None:
            return
        if self._heading_depth is not None:
            self._heading_parts.append(data)
        if self._block_depth is not None:
            self._parts.append(data)


def parse_caveats(html: str, source_url: str) -> tuple[CaveatBlock, ...]:
    """Return the published prose blocks inside a page's ``<main>`` element.

    Args:
        html: The page source.
        source_url: Where it came from. Used only in the error message.

    Returns:
        The blocks, in document order.

    Raises:
        ChipotleSourceError: If the page carries no text blocks inside a
            ``<main>`` element. An allergen dataset that quietly shipped
            without the caveat that qualifies it would be more dangerous than
            one that refused to build, so this is an error and not an empty
            tuple.
    """
    collector = _MainTextCollector()
    collector.feed(html)
    if not collector.blocks:
        raise ChipotleSourceError(
            f"{source_url} published no text inside <main>; the allergen "
            f"caveats this dataset has to quote are not there to read"
        )
    return tuple(collector.blocks)


def _tidy(text: str) -> str:
    """Collapse whitespace within lines, keeping the line breaks between them."""
    lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)
