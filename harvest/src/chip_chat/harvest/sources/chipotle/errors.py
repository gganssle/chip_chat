"""Failures specific to reading Chipotle's published menu.

These are separate from the framework's fetch errors because they mean
something different. A :class:`~chip_chat.harvest.errors.FetchError` says the
network or the server misbehaved and may well succeed on a retry. The errors
here say the bytes arrived intact and do not mean what this source expects —
a page stopped publishing the API configuration, or a restaurant answered with
a menu that has no prices in it. Retrying those is pointless; someone has to
look.
"""

from chip_chat.harvest.errors import HarvestError


class ChipotleSourceError(HarvestError):
    """Chipotle published something this source cannot make sense of."""


class MissingDocumentError(ChipotleSourceError):
    """A document the parser needs is not in the cache.

    Raised only by the offline path, where finding nothing means the harvest
    has not been run rather than that the site is down.
    """

    def __init__(self, url: str) -> None:
        super().__init__(f"{url} is not in the cache; run the harvest first")
        self.url = url
