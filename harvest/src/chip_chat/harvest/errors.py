"""Failures the harvest framework raises.

The split that matters is :class:`TransientFetchError` versus
:class:`PermanentFetchError`: the first is worth retrying with backoff, the
second never is. Retrying a 404 is not politeness, it is noise on someone
else's server.
"""


class HarvestError(Exception):
    """Base class for every failure raised by the harvest framework."""


class RobotsDisallowedError(HarvestError):
    """The site's ``robots.txt`` forbids fetching this URL.

    This is a refusal, not a warning. Nothing in the framework catches it and
    fetches anyway.
    """

    def __init__(self, url: str, user_agent: str) -> None:
        super().__init__(f"robots.txt disallows {url} for user-agent {user_agent!r}")
        self.url = url
        self.user_agent = user_agent


class FetchError(HarvestError):
    """A request could not be completed."""

    def __init__(self, url: str, reason: str, status_code: int | None = None) -> None:
        super().__init__(f"{url}: {reason}")
        self.url = url
        self.reason = reason
        self.status_code = status_code


class TransientFetchError(FetchError):
    """A failure worth retrying: a timeout, a connection reset, 429, or 5xx."""


class PermanentFetchError(FetchError):
    """A failure not worth retrying: 4xx other than 429."""


class CacheCorruptError(HarvestError):
    """The cache holds a pointer whose content blob is missing or altered."""


class DocumentAnalysisError(HarvestError):
    """A document could not be analysed by Document Intelligence.

    Distinct from :class:`FetchError` because nothing about it is the source
    site's doing: the bytes are already in hand, and what failed was our own
    call to Azure. Retrying it costs Azure money rather than costing a stranger
    bandwidth, so the two are never handled by the same code.
    """

    def __init__(self, subject: str, reason: str) -> None:
        super().__init__(f"{subject}: {reason}")
        self.subject = subject
        self.reason = reason
