"""What goes wrong generating a population, and why each of them raises.

The generator's whole value is that its output is reproducible and composed
only of real catalogue rows. Both properties fail silently if the generator is
allowed to improvise, so every case below stops the run instead of producing a
smaller or slightly different population that nothing downstream would notice.
"""


class GeneratorError(Exception):
    """Base class for every failure in this package."""


class ConfigError(GeneratorError):
    """The generation parameters do not describe a population that can exist.

    Raised rather than clamped. Shares that do not sum to one, a negative
    cadence, an hour distribution with no mass in it: each of these produces a
    population, and each produces a *different* population than the one whose
    numbers were written down. Issue #25 asks that the population be retunable
    from a config file, and a file whose numbers are silently corrected is not
    the thing that was tuned.
    """


class ThinCatalogError(GeneratorError):
    """The catalogue does not contain enough to compose an order from.

    The rule this package exists to keep is that an order references only real
    catalogue rows. When the catalogue has no orderable entree — because it was
    built from a trimmed fixture, or because a parser change emptied a column —
    the only way to honour that rule is to refuse. Inventing an item here is
    exactly the hallucinated menu item the whole pipeline is arranged to make
    impossible.
    """


class RewardsTermsError(GeneratorError):
    """The published rewards terms do not state the arithmetic the ledger needs.

    Issue #27 asks that accrual rates and redemption costs be "taken from the
    real published rewards terms, not invented", and the only way to keep that
    promise when the terms stop saying something is to stop. A default earn
    rate here would be a number this project made up, sitting in a column
    labelled as Chipotle's — which is the precise failure the whole harvest is
    arranged to prevent.

    Raised when a published rule cannot be found, when two published documents
    state it differently, or when the Rewards Exchange lands with no rewards
    in it to redeem.
    """
