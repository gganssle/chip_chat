"""``robots.txt``: parsed, cached, and enforced.

The rules here are the ones in RFC 9309, and the one that matters is what
happens when ``robots.txt`` cannot be read. A 4xx means the site published no
rules and everything is permitted. A 5xx or a connection failure means we do
not know what the rules are, and the framework refuses everything until it
does. Guessing in the permissive direction is how a crawler ends up somewhere
it was told not to go.
"""

from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from chip_chat.harvest.transport import HttpResponse

ROBOTS_PATH = "/robots.txt"


def robots_url_for(url: str) -> str:
    """Return the ``robots.txt`` URL governing ``url``.

    Args:
        url: Any absolute URL.

    Returns:
        The ``robots.txt`` URL for that URL's scheme, host and port.

    Raises:
        ValueError: If ``url`` has no scheme or no host.
    """
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"expected an absolute URL, got {url!r}")
    return urlunsplit((parts.scheme, parts.netloc, ROBOTS_PATH, "", ""))


def origin_of(url: str) -> str:
    """Return the ``scheme://host:port`` origin of ``url``.

    Args:
        url: Any absolute URL.

    Returns:
        The origin, with no trailing slash.

    Raises:
        ValueError: If ``url`` has no scheme or no host.
    """
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"expected an absolute URL, got {url!r}")
    return f"{parts.scheme}://{parts.netloc}"


class RobotsPolicy:
    """The fetch rules for one origin.

    Build one with :meth:`from_text` when ``robots.txt`` was read,
    :meth:`allow_all` when the site published none, and :meth:`deny_all` when
    it could not be read at all.
    """

    def __init__(self, parser: RobotFileParser | None, *, default: bool) -> None:
        """Initialise the policy.

        Args:
            parser: A parser holding real rules, or ``None`` for a blanket
                policy.
            default: The answer to every question when ``parser`` is ``None``.
        """
        self._parser = parser
        self._default = default

    @classmethod
    def from_text(cls, text: str) -> "RobotsPolicy":
        """Parse the body of a ``robots.txt``.

        Args:
            text: The decoded contents of the file.

        Returns:
            A policy enforcing the rules it declares.
        """
        parser = RobotFileParser()
        parser.parse(text.splitlines())
        return cls(parser, default=True)

    @classmethod
    def allow_all(cls) -> "RobotsPolicy":
        """Return the policy for a site that publishes no ``robots.txt``."""
        return cls(None, default=True)

    @classmethod
    def deny_all(cls) -> "RobotsPolicy":
        """Return the policy for a site whose ``robots.txt`` could not be read."""
        return cls(None, default=False)

    def can_fetch(self, user_agent: str, url: str) -> bool:
        """Return whether ``user_agent`` is permitted to fetch ``url``.

        Args:
            user_agent: The full User-Agent string the client will send.
            url: The absolute URL under consideration.

        Returns:
            ``True`` if the rules permit the fetch.
        """
        if self._parser is None:
            return self._default
        return self._parser.can_fetch(user_agent, url)

    def crawl_delay(self, user_agent: str) -> float | None:
        """Return the ``Crawl-delay`` this site asks of ``user_agent``.

        Args:
            user_agent: The full User-Agent string the client will send.

        Returns:
            The delay in seconds, or ``None`` if the site asks for none.
        """
        if self._parser is None:
            return None
        declared = self._parser.crawl_delay(user_agent)
        return None if declared is None else float(declared)


def policy_from_response(response: HttpResponse | None) -> RobotsPolicy:
    """Turn the result of fetching ``robots.txt`` into a policy.

    Args:
        response: The response, or ``None`` if the fetch failed outright.

    Returns:
        The parsed rules for a 2xx, an allow-all for a 4xx, and a deny-all for
        anything else — including a failed fetch, a redirect we did not follow,
        and every 5xx.
    """
    if response is None:
        return RobotsPolicy.deny_all()
    if 200 <= response.status_code < 300:
        return RobotsPolicy.from_text(response.text)
    if 400 <= response.status_code < 500:
        return RobotsPolicy.allow_all()
    return RobotsPolicy.deny_all()
