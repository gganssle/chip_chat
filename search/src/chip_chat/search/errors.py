"""One base class, so a caller can tell this package's failures from the SDK's.

``chip_chat.harvest.errors`` does the same thing for the same reason: a build
that dies wants to say *which* of the three things it talks to refused — the
corpus, the search service, or the embedding deployment — and a shared base is
what lets ``__main__`` report that without catching ``Exception``.
"""

__all__ = ["SearchError"]


class SearchError(RuntimeError):
    """Something in the retrieval index lane refused."""
