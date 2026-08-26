"""The loyalty ledger: points earned on real totals, redeemed at a real cost.

Issue #27 reconciles this ledger against Chipotle's published rewards terms,
which the policy harvest already carries. Two decisions here exist to make
that reconciliation a join rather than an argument.

**Every arithmetic constant is config.** Points per dollar and the cost of a
redemption live in ``population.toml`` under ``[loyalty]``, stated as the
generator's parameters and asserted nowhere as facts about the real programme.
When #27 reads the published terms, the difference between them and these is a
diff on one file.

**Every entry names the order it came from.** ``loyalty_ledger.order_id`` is
not in RFC-001 section 04, and without it reconciling the ledger against the
orders would mean regenerating the ledger — which checks that the code agrees
with itself and nothing else.

Only settled orders earn. A refunded order is real history, stays in
``orders``, and moves no points.
"""

from collections.abc import Iterator, Sequence
from datetime import datetime
from random import Random

from chip_chat.data_gen.config import LoyaltyConfig, OrderConfig
from chip_chat.data_gen.records import ENTRY_ID_FORMAT, LoyaltyEntry, Order


def ledger_for(
    rng: Random,
    demo_id: str,
    seed_points: int,
    created_at: datetime,
    orders: Sequence[Order],
    loyalty: LoyaltyConfig,
    statuses: OrderConfig,
    numbers: Iterator[int],
) -> tuple[LoyaltyEntry, ...]:
    """Return one customer's whole ledger, in time order.

    Args:
        rng: This customer's loyalty stream.
        demo_id: Whose ledger.
        seed_points: The archetype's opening balance.
        created_at: When that balance was granted.
        orders: Their orders, oldest first.
        loyalty: The rewards arithmetic.
        statuses: Which order statuses settle.
        numbers: The running entry numbering, shared across the population.

    Returns:
        The entries. An opening balance if the archetype has one, an earn for
        every settled order, and a redemption whenever the balance clears the
        threshold and the customer decides to spend it.
    """
    entries: list[LoyaltyEntry] = []
    balance = 0
    if seed_points > 0:
        entries.append(
            LoyaltyEntry(
                entry_id=ENTRY_ID_FORMAT.format(index=next(numbers)),
                demo_id=demo_id,
                delta=seed_points,
                reason=loyalty.seed_reason,
                order_id=None,
                created_at=created_at,
            )
        )
        balance += seed_points

    for order in orders:
        if order.status not in statuses.settled_statuses:
            continue
        earned = int(order.total * loyalty.points_per_dollar)
        if earned > 0:
            entries.append(
                LoyaltyEntry(
                    entry_id=ENTRY_ID_FORMAT.format(index=next(numbers)),
                    demo_id=demo_id,
                    delta=earned,
                    reason=loyalty.earn_reason,
                    order_id=order.order_id,
                    created_at=order.placed_at,
                )
            )
            balance += earned
        if balance < loyalty.redemption_threshold:
            continue
        if rng.random() >= loyalty.redemption_probability:
            continue
        entries.append(
            LoyaltyEntry(
                entry_id=ENTRY_ID_FORMAT.format(index=next(numbers)),
                demo_id=demo_id,
                delta=-loyalty.redemption_threshold,
                reason=loyalty.redeem_reason,
                order_id=order.order_id,
                created_at=order.placed_at,
            )
        )
        balance -= loyalty.redemption_threshold

    return tuple(entries)
