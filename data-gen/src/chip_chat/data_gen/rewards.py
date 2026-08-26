"""The rewards programme, read off what Chipotle publishes about it.

Issue #27's instruction is "reconcile, do not invent": the points a settled
order earns and what a redemption costs must be Chipotle's published numbers,
not the generator's. Issue #25 left them in ``population.toml`` as declared
provisional parameters and said the reconciliation should be a join. This
module is that join, and it moves them out of the config file entirely — the
arithmetic of the ledger is now *read*, and the only place it can be retuned
is Chipotle's own website.

Four published rules come out of the policy harvest of issue #21:

======================  ======================================================
``points_per_dollar``   "you'll earn 10 points per $1 spent at Chipotle" — the
                        FAQ, and the rewards page's "every dollar spent ...
                        gets you 10 points closer to your next reward".
``rewards``             The Rewards Exchange line-up and its point costs, the
                        harvest's ``rewards`` table, carried through whole.
``expiry_days``         "Points expire after 365 days of account inactivity".
``daily_purchases``     "Each Chipotle Rewards participant account is limited
                        to three qualifying purchases per day."
======================  ======================================================

The first, third and fourth are published as prose, because that is how a
contract publishes a rule; :func:`load_rewards_terms` reads them out of the
harvested section and FAQ text with the narrow patterns below. Three things
about that are deliberate.

**Every rule is read from every document that states it, and a disagreement
stops the run.** The earn rate is published twice and the expiry window twice,
on different pages, and a harvest where those two now disagree is a harvest
that has caught Chipotle mid-change. Picking one would be picking which of two
true-looking numbers to put in a column labelled as theirs.

**A rule that is no longer published stops the run too.** There is no default
earn rate here and no fallback in the config file, because a fallback is an
invented number wearing a citation. :class:`RewardsTermsError` is the whole
error-handling strategy.

**Every number keeps the URL it was read from.** :attr:`RewardsTerms.citations`
is the audit trail, and it is why "the ledger agrees with the published terms"
is a claim a reviewer can check by opening two pages rather than by reading
this file.
"""

import hashlib
import json
import re
import types
import typing
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any, get_args, get_origin

from chip_chat.data_gen.errors import RewardsTermsError
from chip_chat.harvest.blobs import BlobStore
from chip_chat.harvest.sources.chipotle import (
    DEFAULT_POLICY_PREFIX,
    FaqEntry,
    PolicySection,
    Reward,
)

POINTS_PER_DOLLAR = "points_per_dollar"
"""The rule naming how many points a dollar of a settled order earns."""

EXPIRY_DAYS = "inactivity_expiry_days"
"""The rule naming how long a balance survives without a qualifying purchase."""

DAILY_PURCHASES = "daily_qualifying_purchases"
"""The rule naming how many purchases a day may earn points."""

EARN_RATE_PATTERNS = (
    re.compile(r"(?P<value>\d+)\s+points?\s+per\s+\$\s?1\b", re.IGNORECASE),
    re.compile(
        r"every\s+dollar\s+spent\b[^.]*?\b(?P<value>\d+)\s+points?\b", re.IGNORECASE
    ),
)
"""How the earn rate is published: once in the FAQ, once on the rewards page.

Both are matched everywhere rather than each against the page it is expected
on, so that a rate moving from one document to the other is a diff of zero
lines and a rate that changes on one page only is an error.
"""

EXPIRY_PATTERNS = (
    re.compile(
        r"points\s+expire\s+after\s+(?P<value>\d+)\s+days\s+of\s+account\s+inactivity",
        re.IGNORECASE,
    ),
)
"""How the expiry window is published, in the terms and again in the FAQ."""

DAILY_PURCHASE_PATTERNS = (
    re.compile(
        r"limited\s+to\s+(?P<value>[a-z]+|\d+)\s+qualifying\s+purchases\s+per\s+day",
        re.IGNORECASE,
    ),
)
"""How the daily cap is published. The count is spelled as a word, which is
why :data:`NUMBER_WORDS` exists."""

NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
"""Small numbers as a contract spells them. Deliberately short: a terms page
that says "twenty-seven qualifying purchases per day" has changed enough to be
worth a person reading it."""

TABLES = ("rewards", "policy_sections", "faq_entries")
"""The three policy tables the ledger's arithmetic is read out of."""

ROW_TYPES: dict[str, type] = {
    "rewards": Reward,
    "policy_sections": PolicySection,
    "faq_entries": FaqEntry,
}
"""Which harvest record class each of those tables holds.

The classes are the harvest's own rather than copies, which is what makes
"the set of redeemable rewards matches the harvested rewards catalogue
exactly" an identity instead of a comparison between two schemas that have to
be kept in step.
"""


@dataclass(frozen=True, slots=True)
class Citation:
    """One published statement of one rule, and where it was read.

    Attributes:
        rule: Which rule — :data:`POINTS_PER_DOLLAR`, :data:`EXPIRY_DAYS` or
            :data:`DAILY_PURCHASES`.
        value: The number that document states.
        source_url: The document. The harvested row's ``source_url``, which is
            the page a visitor can be shown.
    """

    rule: str
    value: int
    source_url: str


@dataclass(frozen=True, slots=True)
class RewardsTerms:
    """Chipotle's published rewards programme, as the generator needs it.

    Attributes:
        points_per_dollar: Points a dollar of a settled order earns.
        inactivity_expiry_days: Days a balance survives with no qualifying
            purchase before it expires.
        daily_qualifying_purchases: How many purchases in one day may earn.
        rewards: The published Rewards Exchange, in published order. These are
            the harvest's own rows: the set a ledger may redeem from is this
            table and nothing else.
        citations: Every published statement of every rule, in rule order.
    """

    points_per_dollar: int
    inactivity_expiry_days: int
    daily_qualifying_purchases: int
    rewards: tuple[Reward, ...]
    citations: tuple[Citation, ...]

    @property
    def cheapest(self) -> Reward:
        """The least expensive published reward.

        Returns:
            The reward with the lowest point cost, and the earliest published
            position among ties.
        """
        return min(self.rewards, key=lambda reward: (reward.point_cost, reward.position))

    @property
    def costliest(self) -> Reward:
        """The most expensive published reward.

        This is the number "a balance worth surfacing unprompted" is measured
        against: a customer who cannot afford this cannot be shown the whole
        Rewards Exchange.

        Returns:
            The reward with the highest point cost, and the earliest published
            position among ties.
        """
        return max(self.rewards, key=lambda reward: (reward.point_cost, -reward.position))

    def affordable(self, balance: int) -> tuple[Reward, ...]:
        """Return every published reward a balance covers, in published order.

        Args:
            balance: The points on hand.

        Returns:
            The rewards costing no more than ``balance``. Empty when the
            balance does not reach the cheapest one, which is the only reason
            the ledger ever declines to redeem.
        """
        return tuple(reward for reward in self.rewards if reward.point_cost <= balance)

    def content_version(self) -> str:
        """Return the digest of the programme this ledger was generated against.

        Provenance is deliberately excluded, exactly as
        :meth:`~chip_chat.catalog.MenuCatalog.content_version` excludes it: two
        harvests of an unchanged rewards page state the same arithmetic, and a
        population should not be invalidated by having been read on a Tuesday.

        Returns:
            A SHA-256 hex digest over the rules and the reward line-up.
        """
        running = hashlib.sha256()
        running.update(
            json.dumps(
                {
                    POINTS_PER_DOLLAR: self.points_per_dollar,
                    EXPIRY_DAYS: self.inactivity_expiry_days,
                    DAILY_PURCHASES: self.daily_qualifying_purchases,
                    "rewards": [
                        {
                            "position": reward.position,
                            "name": reward.name,
                            "point_cost": reward.point_cost,
                        }
                        for reward in self.rewards
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        return running.hexdigest()


def load_rewards_terms(
    blobs: BlobStore, prefix: str = DEFAULT_POLICY_PREFIX
) -> RewardsTerms:
    """Read the published rewards programme out of the parsed policy tables.

    Args:
        blobs: Where the policy harvest of issue #21 was written.
        prefix: The key prefix it was written under.

    Returns:
        The programme, with a citation for every rule it states.

    Raises:
        RewardsTermsError: If a table is missing or malformed, if the Rewards
            Exchange is empty, if a rule is published nowhere, or if two
            published documents state one differently. Every one of those is a
            reason to stop rather than to fall back on a number this project
            chose.
    """
    root = prefix.strip("/")
    tables = {
        name: _rows(blobs, f"{root}/{name}.jsonl", ROW_TYPES[name]) for name in TABLES
    }
    rewards: tuple[Reward, ...] = tuple(tables["rewards"])
    if not rewards:
        raise RewardsTermsError(
            f"{root}/rewards.jsonl publishes no rewards; a ledger cannot redeem "
            f"against an empty Rewards Exchange"
        )

    published = tuple(_published(tables["policy_sections"], tables["faq_entries"]))
    rate = _rule(published, POINTS_PER_DOLLAR, EARN_RATE_PATTERNS)
    expiry = _rule(published, EXPIRY_DAYS, EXPIRY_PATTERNS)
    daily = _rule(published, DAILY_PURCHASES, DAILY_PURCHASE_PATTERNS)

    return RewardsTerms(
        points_per_dollar=rate[0].value,
        inactivity_expiry_days=expiry[0].value,
        daily_qualifying_purchases=daily[0].value,
        rewards=rewards,
        citations=rate + expiry + daily,
    )


def _published(
    sections: Sequence[PolicySection], entries: Sequence[FaqEntry]
) -> Iterator[tuple[str, str]]:
    """Yield every published passage as ``(text, source_url)``.

    A section's heading and an entry's question are included with their bodies
    because a rule may be stated in either — "Do points expire?" is half of
    the answer it introduces.
    """
    for section in sections:
        yield f"{section.heading or ''}\n{section.text}", section.source_url
    for entry in entries:
        yield f"{entry.question}\n{entry.answer}", entry.source_url


def _rule(
    published: Sequence[tuple[str, str]],
    rule: str,
    patterns: Sequence[re.Pattern[str]],
) -> tuple[Citation, ...]:
    """Read one rule from every document that states it.

    Args:
        published: Every published passage, with the page it came from.
        rule: Which rule is being read, for the citations and the message.
        patterns: The ways it is published. Every one is tried against every
            passage.

    Returns:
        A citation per document that states it, first in document order.

    Raises:
        RewardsTermsError: If nothing states it, or if two documents state it
            differently.
    """
    found: list[Citation] = []
    seen: set[tuple[int, str]] = set()
    for text, source_url in published:
        for pattern in patterns:
            for match in pattern.finditer(text):
                value = _number(match.group("value"))
                if value is None or (value, source_url) in seen:
                    continue
                seen.add((value, source_url))
                found.append(Citation(rule=rule, value=value, source_url=source_url))
    if not found:
        raise RewardsTermsError(
            f"no harvested policy document publishes {rule}; the ledger's "
            f"arithmetic is read from the published terms and there is no "
            f"default for it"
        )
    values = {citation.value for citation in found}
    if len(values) > 1:
        stated = "; ".join(
            f"{citation.source_url} says {citation.value}" for citation in found
        )
        raise RewardsTermsError(
            f"the harvested policy documents disagree about {rule}: {stated}"
        )
    return tuple(found)


def _number(token: str) -> int | None:
    """Return the integer a published token means, or ``None`` if it means none.

    Args:
        token: Digits, or a small number spelled as a contract spells it.

    Returns:
        The value, or ``None`` for a word :data:`NUMBER_WORDS` does not carry —
        which reaches :func:`_rule` as "this passage does not state the rule"
        rather than as a match on a number nobody published.
    """
    if token.isdigit():
        return int(token)
    return NUMBER_WORDS.get(token.lower())


def _rows(blobs: BlobStore, key: str, row_type: type) -> list[Any]:
    """Decode one policy table's JSON Lines into its harvest record class.

    Args:
        blobs: Where the table was written.
        key: Its key.
        row_type: The record class its rows are.

    Returns:
        The rows, in the order they were written.

    Raises:
        RewardsTermsError: If the table is not there, or if a row does not fit
            the class. Nothing is coerced: a column that arrived as a string
            where an integer belongs is a parser that changed shape, and
            reading it anyway would put a guess into the ledger.
    """
    body = blobs.read(key)
    if body is None:
        raise RewardsTermsError(
            f"no harvested policy table at {key}; the ledger's arithmetic comes "
            f"from the policy harvest of issue #21, which has not been run here"
        )
    rows: list[Any] = []
    for number, line in enumerate(body.decode("utf-8").splitlines(), start=1):
        try:
            rows.append(_decode(json.loads(line), row_type))
        except (RewardsTermsError, ValueError, TypeError) as error:
            raise RewardsTermsError(f"{key} line {number}: {error}") from error
    return rows


def _decode(payload: Any, wanted: Any) -> Any:
    """Return ``payload`` as ``wanted``, following the annotation exactly."""
    origin = get_origin(wanted)
    if origin in (typing.Union, types.UnionType):
        return _decode_union(payload, get_args(wanted))
    if origin is tuple:
        return _decode_tuple(payload, get_args(wanted))
    if is_row(wanted):
        return _decode_row(payload, wanted)
    return _decode_scalar(payload, wanted)


def is_row(wanted: Any) -> bool:
    """Whether an annotation names one of the harvest's record classes."""
    return isinstance(wanted, type) and wanted in ROW_TYPES.values()


def _decode_union(payload: Any, options: tuple[Any, ...]) -> Any:
    """Decode an ``X | None`` annotation, which is every optional column here."""
    if payload is None:
        if type(None) in options:
            return None
        raise RewardsTermsError(f"null is not one of {options}")
    for option in options:
        if option is type(None):
            continue
        return _decode(payload, option)
    raise RewardsTermsError(f"{payload!r} is not one of {options}")


def _decode_tuple(payload: Any, args: tuple[Any, ...]) -> tuple[Any, ...]:
    """Decode a homogeneous ``tuple[X, ...]`` column — an FAQ answer's links."""
    if not isinstance(payload, list):
        raise RewardsTermsError(f"{payload!r} is not a list")
    return tuple(_decode(item, args[0]) for item in payload)


def _decode_row(payload: Any, wanted: type) -> Any:
    """Decode one row into its record class, with every column accounted for."""
    if not isinstance(payload, dict):
        raise RewardsTermsError(f"{payload!r} is not an object")
    hints = typing.get_type_hints(wanted)
    names = {field.name for field in fields(wanted)}
    for label, missing in (
        ("has no column", set(payload) - names),
        ("is missing", names - set(payload)),
    ):
        if missing:
            raise RewardsTermsError(
                f"{wanted.__name__} {label} {', '.join(sorted(missing))}"
            )
    return wanted(**{name: _decode(payload[name], hints[name]) for name in names})


def _decode_scalar(payload: Any, wanted: Any) -> Any:
    """Decode the scalar kinds a policy column holds."""
    if wanted is datetime:
        if not isinstance(payload, str):
            raise RewardsTermsError(f"{payload!r} is not a timestamp")
        return datetime.fromisoformat(payload)
    if wanted is bool:
        if not isinstance(payload, bool):
            raise RewardsTermsError(f"{payload!r} is not a boolean")
        return payload
    if wanted is int:
        if isinstance(payload, bool) or not isinstance(payload, int):
            raise RewardsTermsError(f"{payload!r} is not an integer")
        return payload
    if wanted is str:
        if not isinstance(payload, str):
            raise RewardsTermsError(f"{payload!r} is not a string")
        return payload
    raise RewardsTermsError(f"{wanted!r} is not a column type this reads")


def rewards_by_name(rewards: Iterable[Reward]) -> dict[str, Reward]:
    """Index published rewards by name, for reconciling a ledger against them.

    Args:
        rewards: The published line-up.

    Returns:
        Name to reward. ``loyalty_ledger.reward_name`` holds the published
        name verbatim, so this is the join that turns a redemption entry back
        into the thing that was redeemed.
    """
    return {reward.name: reward for reward in rewards}
