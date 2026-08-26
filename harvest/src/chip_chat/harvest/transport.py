"""The HTTP boundary.

Every network call in this package goes through :class:`Transport`. Tests
inject a fake, which is what lets the whole framework — robots handling,
rate limiting, retries, caching — be tested without a socket, and what makes
the warm-cache assertion in ``test_harvester.py`` meaningful: a fake that is
never called is proof, where a mocked-out real client would only be a claim.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from chip_chat.harvest.version import __version__

DEFAULT_CONTACT = "https://github.com/gganssle/chip_chat"
"""Where a site owner should complain. Override it if you deploy this."""

DEFAULT_TIMEOUT_SECONDS = 30.0


def build_user_agent(contact: str = DEFAULT_CONTACT) -> str:
    """Return an honest User-Agent naming the client and how to reach it.

    Args:
        contact: A URL or ``mailto:`` address a site owner can use to reach
            whoever is running the harvest.

    Returns:
        A User-Agent string of the form ``chip-chat-harvest/0.0.0 (+contact)``.

    Raises:
        ValueError: If ``contact`` is empty. A crawler that does not say who
            it is has no business on someone else's site.
    """
    if not contact.strip():
        raise ValueError("a harvest User-Agent must carry a contact address")
    return f"chip-chat-harvest/{__version__} (+{contact.strip()})"


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """One HTTP response, reduced to what the framework stores and decides on.

    Attributes:
        url: The final URL, after any redirects.
        status_code: The HTTP status.
        content: The raw response body, undecoded and unparsed.
        headers: Response headers, with lowercase keys.
    """

    url: str
    status_code: int
    content: bytes
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def content_type(self) -> str:
        """Return the ``Content-Type`` header, or the empty string."""
        return self.headers.get("content-type", "")

    @property
    def text(self) -> str:
        """Return the body decoded as UTF-8, replacing undecodable bytes."""
        return self.content.decode("utf-8", errors="replace")


class Transport(Protocol):
    """Issues one HTTP GET and returns the response.

    An implementation raises :class:`OSError` (or an ``httpx`` error, which is
    not an ``OSError``) for a failure at the socket level; the harvester turns
    those into :class:`~chip_chat.harvest.errors.TransientFetchError`. It does
    not raise for HTTP error statuses — those come back as responses, because
    the harvester distinguishes retryable ones from permanent ones itself.
    """

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        """Fetch ``url`` and return the response."""
        ...

    def close(self) -> None:
        """Release any underlying connections."""
        ...


class HttpxTransport:
    """The real transport, backed by a pooled :class:`httpx.Client`."""

    def __init__(self, *, follow_redirects: bool = True) -> None:
        import httpx

        self._client = httpx.Client(follow_redirects=follow_redirects)

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> HttpResponse:
        import httpx

        try:
            response = self._client.get(url, headers=dict(headers), timeout=timeout)
        except httpx.HTTPError as error:
            raise OSError(f"{type(error).__name__}: {error}") from error
        return HttpResponse(
            url=str(response.url),
            status_code=response.status_code,
            content=response.content,
            headers={key.lower(): value for key, value in response.headers.items()},
        )

    def close(self) -> None:
        self._client.close()
