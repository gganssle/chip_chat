"""Where a *person* can read the page a citation came from, when there is one.

``source_url`` is provenance and it is exactly right at that job: every row the
harvest writes carries the endpoint it was actually read from, which is what
makes a re-harvest diff mean something and what lets an operator answer "where
did this number come from" without guessing. :mod:`chip_chat.harvest` is
explicit that this is per-row and deliberate.

It is wrong as a link, and the difference cost a user-testing session. The
ordering data is read from a JSON API — ``services.chipotle.com`` — so a menu
citation's ``source_url`` is an API endpoint, and a menu citation is the single
most common citation the demo produces. The web renderer took that field and
made an ``<a href>`` out of it, so the visitor tapped a source line under a
sentence about barbacoa and landed on a JSON body or a 404. The report was
blunt and correct: *"It's fine to use the API to look at the data, but if the
user's going to be taken to a null page, it doesn't look good."*

So there are two URLs per citation and they answer two different questions.
``source_url`` says where the fact was read. This module answers where a human
should be sent to read the same fact, and — the part that matters — it returns
``None`` rather than inventing somewhere plausible when no such page exists.

**The rules are host-and-path, never host alone, and that is not fussiness.**
The obvious implementation is "block ``services.chipotle.com``, allow
``www.chipotle.com``", and it is wrong: the FAQ content is harvested from
``https://www.chipotle.com/graphql/execute.json/chipotle/FAQ-Query;region=en-us``,
which is on the ordinary public host, returns HTTP 200, and serves
``application/json``. A visitor tapping that gets the same JSON blob the
services host would have given them. A host-only rule would have shipped this
bug half-fixed and made it harder to see the second time.

**Every mapping below was checked rather than reasoned about**, on 31 August
2026, with a browser user-agent and redirects followed:

===================================================  ======  =========================
URL                                                  Status  Content-Type
===================================================  ======  =========================
``www.chipotle.com/order/build-your-own``            200     ``text/html``
``www.chipotle.com/allergens``                       200     ``text/html``
``www.chipotle.com/rewards``                         200     ``text/html``
``www.chipotle.com/rewards-terms``                   200     ``text/html``
``locations.chipotle.com/``                          200     ``text/html``
``catering.chipotle.com/``                           200     ``text/html``
``www.chipotle.com/graphql/execute.json/...``        200     ``application/json``
``services.chipotle.com/``                           404     —
``www.chipotle.com/menu``                            **404**  —
===================================================  ======  =========================

The last row is the reason this module checks instead of guessing.
``/menu`` is the URL anyone would write down for a menu item from memory, and
it does not exist. Sending visitors there would have been the reported bug
again with a nicer hostname on it, which is a particularly humiliating way to
close a ticket. ``/order/build-your-own`` is the page that does exist, and it
is already what ``databricks/notebooks/lineage_probe.py`` records as the human
page for a menu row, so this agrees with a choice the project had made once
already rather than inventing a second answer to the same question.

**Unlinked is a real answer and it is the default.** Where nothing below
matches, the citation is still shown — label, and the date it was published —
and simply is not a link. A source line a visitor cannot click is a small
disappointment. A source line that opens a 404 tells them the system does not
know what it is talking about, which is the opposite of what a citation is for.
"""

from urllib.parse import urlsplit

__all__ = ["MENU_PUBLIC_URL", "public_url"]

MENU_PUBLIC_URL = "https://www.chipotle.com/order/build-your-own"
"""The published page behind everything read from the ordering API.

Verified 200 ``text/html`` on 31 August 2026. Deliberately *not*
``www.chipotle.com/menu``, which reads like the right answer and returns 404 —
see the module docstring's table.
"""

_API_HOSTS = frozenset({"services.chipotle.com"})
"""Hosts that serve the ordering API and never a page a person reads."""

_PAGE_HOSTS = frozenset(
    {
        "www.chipotle.com",
        "chipotle.com",
        "locations.chipotle.com",
        "catering.chipotle.com",
    }
)
"""Hosts that do serve pages — subject to the path rules below."""

_API_PATH_MARKERS = ("/graphql/", "/execute.json", "/api/")
"""Path fragments that mean "this is an endpoint" on an otherwise human host.

``/graphql/execute.json/...`` is the measured one: HTTP 200, and
``application/json``. The others are here because the harvest reaches for
whatever the site publishes and a future source on the same host would
otherwise be linked by default, which is the failure mode this module exists to
stop rather than one to rediscover later.
"""


def public_url(source_url: str) -> str | None:
    """Return a page a person can read for ``source_url``, or ``None``.

    Args:
        source_url: The provenance URL a passage carries. Never modified; this
            function only ever reads it.

    Returns:
        An ``https`` URL serving HTML that a visitor can usefully be sent to,
        or ``None`` where the fact has no published human page. ``None`` is a
        designed outcome, not a failure: the caller renders the citation
        without a link rather than suppressing it.
    """
    if not source_url:
        return None
    try:
        parts = urlsplit(source_url)
    except ValueError:
        # A URL the harvest wrote that will not even parse is not something to
        # hand a browser. It is worth knowing about, but not here -- the
        # indexer already refuses a chunk whose source_url is unresolvable.
        return None
    if parts.scheme not in ("http", "https"):
        return None

    host = parts.hostname or ""
    if host in _API_HOSTS:
        # Everything read from the ordering API is a menu or ingredient fact,
        # and the published page for all of it is the same one.
        return MENU_PUBLIC_URL
    if host not in _PAGE_HOSTS:
        return None

    path = parts.path.lower()
    if any(marker in path for marker in _API_PATH_MARKERS):
        # A human host serving a machine response. The FAQ endpoint is this,
        # and it is why the host check above is not the whole rule.
        return None

    # A published page on a published host: the provenance URL and the human
    # URL genuinely are the same string, which is the case the old code assumed
    # was the only one.
    return source_url
