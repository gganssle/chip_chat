"""The loyalty ledger: Chipotle's published arithmetic, run over real orders.

Issue #25 left the arithmetic here as declared-provisional parameters and said
issue #27 would reconcile them against the published rewards terms. It has:
nothing in this module is a number this project chose. The earn rate, the
expiry window, the daily cap and every redemption price arrive as
:class:`~chip_chat.data_gen.rewards.RewardsTerms`, read off the policy harvest
by :func:`~chip_chat.data_gen.rewards.load_rewards_terms`, which refuses to
produce a programme it could not find published. ``population.toml`` keeps
only *behaviour* — how eager an archetype is to spend, and what the ``reason``
column calls each kind of movement.

**The ledger is append-only and the balance is derived.** There is no balance
column anywhere in this package. A customer's balance is the sum of their
entries and nothing else, which is what makes "the ledger sum equals what
``get_points_balance`` returns" a property rather than a reconciliation job.

Four published rules are honoured, and each one is why some entry exists or
does not:

**Ten points per dollar, on qualifying purchases only.** A refunded or
cancelled order is real history, stays in ``orders``, and moves no points.
Points are floored to whole units, because a ledger of fractional points is a
ledger no register prints.

**Three qualifying purchases per day.** The fourth order on the same day is a
real order that earns nothing. The day is the order's own UTC calendar day —
the only day boundary ``orders`` publishes — so a reviewer re-derives the cap
from ``orders.placed_at`` alone, without joining stores and assuming a zone.

**Points expire after 365 days of account inactivity.** A gap that long
between qualifying purchases writes a single entry taking the balance to zero,
dated at the moment the published window closed. It is an entry rather than a
silent reset for the same reason the opening balance is: a balance that
changes without a row is a balance nothing can audit.

**A redemption costs what the Rewards Exchange charges.** The customer spends
on a real published reward they can afford, and the entry names it. There is
no threshold constant: the cheapest published reward *is* the threshold. And
there is no cap on how many a visit may hold, because none is published —
:attr:`~chip_chat.data_gen.config.PersonaSpec.redemption_probability` is asked
again after each one, so the customer stops when they stop wanting to or when
the balance no longer covers anything.
"""

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from random import Random

from chip_chat.data_gen.config import LoyaltyConfig, OrderConfig
from chip_chat.data_gen.records import ENTRY_ID_FORMAT, LoyaltyEntry, Order
from chip_chat.data_gen.rewards import Reward, RewardsTerms


def ledger_for(
    rng: Random,
    demo_id: str,
    seed_points: int,
    created_at: datetime,
    orders: Sequence[Order],
    terms: RewardsTerms,
    loyalty: LoyaltyConfig,
    statuses: OrderConfig,
    redemption_probability: float,
    numbers: Iterator[int],
) -> tuple[LoyaltyEntry, ...]:
    """Return one customer's whole ledger, in time order.

    Args:
        rng: This customer's loyalty stream.
        demo_id: Whose ledger.
        seed_points: The archetype's opening balance.
        created_at: When that balance was granted.
        orders: Their orders, oldest first.
        terms: Chipotle's published rewards programme. Every number the
            arithmetic uses comes from here.
        loyalty: What the ``reason`` column calls each movement, and how a
            customer chooses between the rewards they can afford.
        statuses: Which order statuses qualify. A refunded order earns
            nothing.
        redemption_probability: How likely this archetype is to spend at a
            register where it could. Per archetype rather than per population
            because the Lapsed Regular's unredeemed balance is the point of
            them.
        numbers: The running entry numbering, shared across the population.

    Returns:
        The entries, oldest first. An opening balance if the archetype has one,
        an earn for every qualifying order, an expiry whenever the published
        inactivity window closes on a live balance, and a redemption whenever
        the customer decides to spend one they can afford.
    """
    entries: list[LoyaltyEntry] = []
    balance = 0
    qualifying_on: dict[date, int] = {}
    last_qualified: datetime | None = None

    def write(
        delta: int, reason: str, when: datetime, order_id: str | None, reward: str | None
    ) -> None:
        """Append one entry and move the running balance by it."""
        nonlocal balance
        entries.append(
            LoyaltyEntry(
                entry_id=ENTRY_ID_FORMAT.format(index=next(numbers)),
                demo_id=demo_id,
                delta=delta,
                reason=reason,
                order_id=order_id,
                reward_name=reward,
                created_at=when,
            )
        )
        balance += delta

    if seed_points > 0:
        # Enrollment starts the inactivity clock: the published window runs
        # from the last qualifying purchase, and before there has been one it
        # runs from the day the account — and its opening balance — existed.
        write(seed_points, loyalty.seed_reason, created_at, None, None)
        last_qualified = created_at

    for order in orders:
        if order.status not in statuses.settled_statuses:
            continue

        expired_at = _expiry(last_qualified, order.placed_at, terms)
        if expired_at is not None and balance > 0:
            write(-balance, loyalty.expiry_reason, expired_at, None, None)

        day = order.placed_at.astimezone(UTC).date()
        counted = qualifying_on.get(day, 0)
        if counted < terms.daily_qualifying_purchases:
            qualifying_on[day] = counted + 1
            last_qualified = order.placed_at
            earned = int(order.total * terms.points_per_dollar)
            if earned > 0:
                write(earned, loyalty.earn_reason, order.placed_at, order.order_id, None)

        # Not `if`. Nothing Chipotle publishes limits a visit to one
        # redemption, and the cap that used to be here is what let a
        # high-spend balance grow without bound: a customer earning more in a
        # visit than the costliest reward costs could never drain it however
        # much they wanted to. See ``docs/decisions/persona-fixtures.md``.
        while (affordable := terms.affordable(balance)) and (
            rng.random() < redemption_probability
        ):
            reward = _chosen(rng, affordable, loyalty.splurge_share)
            write(
                -reward.point_cost,
                loyalty.redeem_reason,
                order.placed_at,
                order.order_id,
                reward.name,
            )

    return tuple(entries)


def _expiry(
    last_qualified: datetime | None, now: datetime, terms: RewardsTerms
) -> datetime | None:
    """Return when a balance expired before ``now``, or ``None`` if it did not.

    Args:
        last_qualified: The last qualifying purchase, or ``None`` if there has
            not been one.
        now: The order about to be earned on.
        terms: The published programme, for its inactivity window.

    Returns:
        The instant the published window closed, which is strictly before
        ``now`` and at or after ``last_qualified`` — so an expiry entry never
        lands out of order. ``None`` when the account was never inactive that
        long, which is the ordinary case and, in the tuned population, the only
        one.
    """
    if last_qualified is None:
        return None
    window = timedelta(days=terms.inactivity_expiry_days)
    if now - last_qualified < window:
        return None
    return last_qualified + window


def _chosen(rng: Random, affordable: Sequence[Reward], splurge_share: float) -> Reward:
    """Return the reward this customer takes, from the ones they can afford.

    A population where everybody always takes the most expensive thing their
    balance covers has one redemption story in it, and so does one where the
    choice is uniform: the first never buys a side tortilla, the second never
    saves for an entrée. ``splurge_share`` is the share of redemptions that
    take the best available, and the rest are drawn from the whole affordable
    line-up.

    Args:
        rng: This customer's loyalty stream.
        affordable: The published rewards their balance covers, non-empty.
        splurge_share: Probability they take the most expensive one.

    Returns:
        One published reward.
    """
    if rng.random() < splurge_share:
        return max(affordable, key=lambda reward: (reward.point_cost, -reward.position))
    return affordable[rng.randrange(len(affordable))]
