"""What goes wrong building a catalogue, and why each of them raises.

Every error here is a refusal rather than a fallback. The catalogue is the one
table three other subsystems resolve against — the generator of issue #25, the
vision matcher of issue #54, the retrieval chunker of issue #35 — so a
catalogue that is quietly half-built is worse than no catalogue at all. A
missing document, a name that slugifies onto another name, an allergen the
menu knows about and the nutrition data does not: each stops the build.
"""


class CatalogError(Exception):
    """Base class for every failure in this package."""


class MissingSourceError(CatalogError):
    """A dataset the catalogue is built from was not supplied.

    Raised rather than defaulted. Building the catalogue without the nutrition
    dataset would produce a table whose ``calories`` and ``allergens`` columns
    are empty for every row, and empty allergen columns read as "safe".
    """


class VocabularyCollisionError(CatalogError):
    """Two published names slugify onto the same vocabulary value.

    The vision model's enum members are derived from published names. If two
    of them collide, one term would silently stand for two foods and the
    matcher would resolve a photograph to whichever the dictionary happened to
    keep. That is the fabricated-SKU failure D3 exists to prevent, arriving by
    a different route.
    """

    def __init__(self, slot: str, value: str, first: str, second: str) -> None:
        super().__init__(
            f"{slot!r} vocabulary value {value!r} is claimed by both {first!r} "
            f"and {second!r}; two foods cannot share one enum member"
        )
        self.slot = slot
        self.value = value
        self.first = first
        self.second = second


class CatalogLoadError(CatalogError):
    """A written catalogue could not be read back into its records."""
