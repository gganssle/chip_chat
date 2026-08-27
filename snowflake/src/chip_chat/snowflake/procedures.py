"""The write path, as data. Issue #46 on one side, `snowflake/sql/` on the other.

`chip_chat.snowflake.account` does this for roles and `chip_chat.snowflake.schema`
does it for tables, and this module keeps their bargain: `snowflake/sql/` is the
only thing that creates anything, and nothing here creates a procedure. What it
adds is a third party to an argument that would otherwise have two --
docs/action-surface.md §7 fixes what each write action validates,
`sql/12_procedures.sql` and `sql/13_cancel_order.sql` spell it as Snowflake
Scripting, and `tests/test_procedure_layout.py` holds the second to the first
through this.

Four things are declared here that the SQL cannot check about itself:

**That every procedure runs as its caller.** :attr:`Procedure.rights` is
``CALLER`` on all four and there is no other legal value. Snowflake's default is
owner's rights, and an owner's-rights procedure reads ``GETVARIABLE('DEMO_ID')``
from the owner's session and is filtered by #43's row access policies as the
owner. A write path that ran as its owner would undo RFC-001 §05 from the inside
while every single-session test kept passing. The test fails on the word.

**That no argument names a visitor.** :data:`IDENTITY_VOCABULARY` is the same
absence `chip_chat.agent.surface` is built on, carried one tier down: the tool
surface has no field through which a model could name somebody, and neither does
the procedure surface underneath it. Identity arrives as a session variable and
an argument list cannot contradict it.

**Which rejection codes each procedure can return.** :attr:`Procedure.rejections`
is docs/action-surface.md §7's list per tool, and the test asserts that the set
in the SQL and the set here are the same one. A code that appears in a procedure
and not here is a rejection the ops API has never been told to render; a code
here and not in the SQL is a promise nothing keeps.

**What is invented, and what removing it would cost.**
:attr:`Procedure.invention` is null for three of the four. It is not null for
``cancel_order``, which models an affordance the published record explicitly
refuses -- see :data:`CANCELLATION_WINDOW_MINUTES` and the header of
`sql/13_cancel_order.sql`. That procedure is alone in its own file so that the
exit docs/action-surface.md §10 records stays a deletion.

The gap this tier does not close is stated once, in :data:`ENFORCED_ELSEWHERE`,
rather than in four docstrings.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Final

__all__ = [
    "CANCELLATION_WINDOW_MINUTES",
    "DISPLAY_NAME_MAX_LENGTH",
    "ENFORCED_ELSEWHERE",
    "IDENTITY_VARIABLE",
    "IDENTITY_VOCABULARY",
    "LIVE_ID_BAND",
    "MAX_STATED_PREFERENCES",
    "PROCEDURES",
    "RECEIPT_TABLE",
    "SEQUENCES",
    "SHARED_REJECTIONS",
    "STANCES",
    "Argument",
    "Procedure",
    "procedure",
    "separable",
]

IDENTITY_VARIABLE: Final = "DEMO_ID"
"""The session variable every procedure reads its visitor from.

Set on the connection by the pool (#44), compared against by #43's row access
policies, and absent from every argument list below. RFC-001 §05: the enforcement
mechanism is the absence, not a rule about how to use the presence.
"""

IDENTITY_VOCABULARY: Final = (
    "demo_id",
    "visitor",
    "visitor_id",
    "customer",
    "customer_id",
    "user",
    "user_id",
    "account",
    "account_id",
    "session_id",
    "persona_id",
)
"""Words no procedure argument may be spelled with, in any case.

The same list `agent/tests/test_surface.py` holds the tool schemas to. A
procedure that grew a ``demo_id`` argument would be a field for a compromised
ops API to fill in with somebody else's identifier, and the row access policies
would not save it: a policy filters what a session may SEE, and an INSERT that
names another visitor is a write, not a read.
"""

RECEIPT_TABLE: Final = "action_receipts"
"""Where a spent retry key lives. ``CHIP_CHAT.ACCOUNTS.action_receipts``.

Every procedure claims its key here with a ``MERGE``, inside its own
transaction, and the test asserts the word MERGE rather than merely asserting
the table: a ``SELECT`` then ``INSERT`` reads identically in a review and does
not serialise, so two retries of one order would both find nothing and both
write. See the table's own header in `sql/07_accounts.sql`.
"""

SEQUENCES: Final = ("live_order_seq", "live_ledger_seq")
"""The two sequences the procedures mint identifiers from, in ACCOUNTS."""

LIVE_ID_BAND: Final = 9_000_001
"""Where a live identifier starts, and generated history cannot reach.

``ord-9000001`` and ``loy-9000001`` upward. data-gen produces tens of thousands
of rows numbered from one, so an identifier at or above this is one a visitor
placed live rather than one the demo was seeded with -- which is a distinction a
reviewer reading the table wants without a join. Sequences are monotonic and not
gapless: Snowflake hands a session a range at a time, so the second live order
of a fresh session is not ``ord-9000002`` and was never promised to be.
"""

CANCELLATION_WINDOW_MINUTES: Final = 10
"""How long a live order stays cancellable. **INVENTED, and the only invented
number in the write path.**

docs/action-surface.md §10 row 1. The published record does not merely omit
self-service cancellation, it refuses it: *"When you submit an order, it's sent
directly to our restaurant crew, so we're unable to cancel"* (`faq_entries`,
Ordering / General), and the delivery answer routes the customer to a human and
warns of a cancelation fee. PRD T1 requires the action anyway and PRD T5 says
every action is simulated, so the window is ours and every receipt says so.

`sql/13_cancel_order.sql` declares the same number and
`tests/test_procedure_layout.py` fails if the two drift -- an invented constant
that has quietly become two invented constants is one nobody can remove.
"""

DISPLAY_NAME_MAX_LENGTH: Final = 40
"""Characters, after trimming. **INVENTED**: docs/action-surface.md §10 row 5,
an ordinary product limit nobody publishes. Stated so a reviewer can argue with
the number rather than discover it."""

MAX_STATED_PREFERENCES: Final = 20
"""Entries. **INVENTED**, same row, same reason."""

STANCES: Final = ("always", "never", "light", "extra", "side")
"""The five stances a stated preference may take.

Not a design: four are the published portion vocabulary of
docs/action-surface.md §1.3 and ``always``/``never`` are the presence axis the
slot grammar already carries in *No Rice* and *No Beans*. ``Half`` is
deliberately absent -- it exists on rice and beans only and is a choice made per
order, not a standing one.
"""

ENFORCED_ELSEWHERE: Final = (
    (
        "required modifier slots (§7.1 rule 6)",
        "CHIP_CHAT.CATALOGUE.modifiers carries no group name, minimum or "
        "maximum, so there is nothing here to check a bowl's missing rice "
        "choice against. api/drafts.py checks it at proposal time against "
        "chip_chat.catalog, which does carry the slots",
    ),
    (
        "per-pair portion permissions (§7.1 rule 8, §7.4 rule 4)",
        "there is no portion_options table in the serving projection at all. "
        "What is checked here is that a stance is one of the five published "
        "words; that light guacamole is unorderable because no item offers it "
        "is the ops API's check",
    ),
    (
        "the six per-item caps (§7.1 rule 9)",
        "max_customizations, max_contents, max_extras, max_halfs, "
        "max_extras_plus_halfs and max_on_the_side are columns on the "
        "lakehouse's menu_items and not on the serving layer's nine",
    ),
    (
        "the draft is confirmed and unexpired (§7.1 rule 11)",
        "deliberately the ops API's and deliberately not reachable from here. "
        "A confirmation flag the model can reach is not a confirmation, and "
        "the flag lives in the app tier for exactly that reason (#62)",
    ),
    (
        "the session's spend cap has room (§7.1 rule 12)",
        "counted per session in the app tier by chip_chat.api.guard, which is "
        "where a turn's tokens are already being counted",
    ),
)
"""What the database does not validate, and where it is validated instead.

Written down rather than left as an absence, because the absence is the kind
that reads as an oversight. Issue #46 asks the database to make one thing
structural -- *no SKU in any response that does not exist in the catalogue* --
and that is enforced here, at the row that would have to exist. The rules above
are about a grammar the serving projection of the catalogue does not carry the
columns for, and adding those columns is #24's and #42's question rather than
this ticket's.
"""

SHARED_REJECTIONS: Final = (
    "SESSION_NOT_BOUND",
    "RETRY_KEY_REQUIRED",
    "VISITOR_NOT_FOUND",
    "RETRY_KEY_SPENT_ON_ANOTHER_ACTION",
    "WRITE_FAILED",
)
"""The five every procedure can return, and none of them is in §7's lists.

§7's codes are about the action a visitor asked for. These are about the call:
a session with no identity bound, a write with no retry key, a key already spent
on something else, and the failure nobody predicted. They are listed once here
rather than repeated four times below.
"""


@dataclass(frozen=True, slots=True)
class Argument:
    """One procedure argument, and why the procedure needs it.

    Attributes:
        name: As the SQL declares it, upper case.
        sql_type: The declared type.
        why: What the procedure cannot do without it. Every one of these is a
            thing the ops API knows and the database does not, which is the
            test a proposed fifth argument has to pass -- an argument the
            database could look up for itself is one a caller can get wrong.
    """

    name: str
    sql_type: str
    why: str


@dataclass(frozen=True, slots=True)
class Procedure:
    """One write action, and what it promises.

    Attributes:
        name: The procedure name, unqualified.
        file: Which file in `snowflake/sql/` creates it.
        surface: The section of docs/action-surface.md that fixes its rules.
        arguments: In declaration order. None of them names a visitor.
        writes: The tables it writes, other than :data:`RECEIPT_TABLE`, which
            all four write.
        rejections: The codes it can return, beyond
            :data:`SHARED_REJECTIONS`. The test holds the SQL to this set in
            both directions.
        invention: What about this procedure the published record does not
            support, or None. Not null for exactly one of the four.
        rights: ``CALLER``. There is no other permitted value and the field
            exists so that a test can say so.
    """

    name: str
    file: str
    surface: str
    arguments: tuple[Argument, ...]
    writes: tuple[str, ...]
    rejections: tuple[str, ...]
    invention: str | None = None
    rights: str = field(default="CALLER")

    def qualified(self) -> str:
        """Return ``CHIP_CHAT.ACCOUNTS.place_order`` and the like."""
        return f"CHIP_CHAT.ACCOUNTS.{self.name}"

    def all_rejections(self) -> tuple[str, ...]:
        """Return every code this procedure can return, shared ones included."""
        return tuple(sorted({*SHARED_REJECTIONS, *self.rejections}))


_RETRY_KEY = Argument(
    "RETRY_KEY",
    "VARCHAR",
    "the idempotency key for one confirmed action. The database cannot mint it "
    "-- two calls are the same attempt only if the caller says so, and a "
    "network timeout is exactly the case where the database sees one call and "
    "the world had two",
)

_PLACE_ORDER = Procedure(
    name="place_order",
    file="12_procedures.sql",
    surface="7.1",
    arguments=(
        _RETRY_KEY,
        Argument(
            "STORE_ID",
            "NUMBER",
            "which restaurant. Money is a column on a restaurant "
            "(docs/decisions/menu-pricing.md), so an order cannot be priced "
            "until it has one, and a live order is priced at the store it is "
            "placed at or it is rejected -- never at a store the visitor is "
            "not standing in",
        ),
        Argument(
            "CHANNEL",
            "VARCHAR",
            "IN_STORE or DELIVERY. The catalogue publishes two prices per item "
            "and they differ by about thirty percent, so a total is "
            "unexplainable until the row says which list priced it",
        ),
        Argument(
            "ORDER_LINES",
            "VARIANT",
            "the composition: item ids, quantities and modifier item ids. The "
            "draft itself lives in the app tier where the confirmation flag is "
            "out of the model's reach, so what arrives here is what the "
            "visitor confirmed rather than a pointer to it",
        ),
    ),
    writes=("orders", "order_items", "loyalty_ledger"),
    rejections=(
        "CHANNEL_NOT_RECOGNISED",
        "DRAFT_EMPTY",
        "EARN_RATE_NOT_PUBLISHED",
        "ITEM_NOT_ORDERABLE",
        "ITEM_UNAVAILABLE_AT_STORE",
        "MODIFIER_NOT_OFFERED",
        "MODIFIER_UNAVAILABLE_AT_STORE",
        "QUANTITY_EXCEEDS_MAX",
        "STORE_NOT_FOUND",
    ),
)

_REDEEM_POINTS = Procedure(
    name="redeem_points",
    file="12_procedures.sql",
    surface="7.3",
    arguments=(
        _RETRY_KEY,
        Argument(
            "REWARD_ID",
            "VARCHAR",
            "which published reward. The slug is ours -- the published "
            "catalogue carries no identifier at all -- and that is invention "
            "#2 in docs/action-surface.md §10 rather than anything this "
            "procedure decides",
        ),
        Argument(
            "QUOTED_POINT_COST",
            "NUMBER",
            "what the visitor was SHOWN, or null to skip the check. The terms "
            "let Chipotle change a reward's cost at any time, so a cost read "
            "when the card was rendered is a quote; a mismatch is re-proposed "
            "rather than silently charged. The database cannot know what was "
            "on the card, which is the whole reason this is an argument",
        ),
    ),
    writes=("loyalty_ledger",),
    rejections=(
        "INSUFFICIENT_POINTS",
        "REWARD_COST_CHANGED",
        "REWARD_UNAVAILABLE",
    ),
)

_UPDATE_PREFERENCES = Procedure(
    name="update_preferences",
    file="12_procedures.sql",
    surface="7.4",
    arguments=(
        _RETRY_KEY,
        Argument(
            "PREFS",
            "VARIANT",
            "a partial object over the three editable fields. A VARIANT rather "
            "than three columns because absent and null are different calls -- "
            "leave it alone versus clear it -- and three nullable arguments "
            "cannot tell them apart",
        ),
    ),
    writes=("demo_visitors",),
    rejections=(
        "MODIFIER_NOT_RECOGNISED",
        "NAME_TOO_LONG",
        "NOTHING_TO_UPDATE",
        "STANCE_NOT_AVAILABLE_FOR_MODIFIER",
        "STORE_NOT_FOUND",
        "TOO_MANY_PREFERENCES",
    ),
)

_CANCEL_ORDER = Procedure(
    name="cancel_order",
    file="13_cancel_order.sql",
    surface="7.2",
    arguments=(
        _RETRY_KEY,
        Argument(
            "ORDER_ID",
            "VARCHAR",
            "which order. Validated against the bound visitor, so a "
            "well-formed id belonging to somebody else is a not-found rather "
            "than a forbidden -- 'somebody else has this' is a fact a stranger "
            "is not owed",
        ),
    ),
    writes=("orders", "loyalty_ledger"),
    rejections=(
        "CANCELLATION_WINDOW_CLOSED",
        "ORDER_NOT_CANCELLABLE",
        "ORDER_NOT_FOUND",
    ),
    invention=(
        "THE ACTION ITSELF. Chipotle's published FAQ does not omit "
        "self-service cancellation, it refuses it: a submitted order goes "
        "straight to the restaurant crew, so 'we're unable to cancel', and a "
        "delivery order can only be cancelled by contacting Customer Service, "
        "possibly for a fee. PRD T1 requires the action and PRD T5 says every "
        "action is simulated, so the tool exists and every receipt carries "
        "both sentences. The window is CANCELLATION_WINDOW_MINUTES and is ours. "
        "docs/action-surface.md §3 and §10 row 1; the exit §10 records is a PRD "
        "change dropping T1's cancellation clause, and this procedure is alone "
        "in its own file so that the exit stays a deletion rather than a "
        "migration."
    ),
)

PROCEDURES: Final[tuple[Procedure, ...]] = (
    _PLACE_ORDER,
    _REDEEM_POINTS,
    _UPDATE_PREFERENCES,
    _CANCEL_ORDER,
)
"""The four write actions RFC-001 §06 fixes, and no fifth.

The list is closed by the RFC rather than by this file. Group ordering and
Points Requests are the two candidates the real product would have justified,
and docs/action-surface.md §5 says why each is out of reach -- a second identity
in one draft, and a receipt-image evidence path into a ledger that is read-only
to visitors.
"""


def procedure(name: str) -> Procedure:
    """Return one procedure by name.

    Args:
        name: An unqualified procedure name, in any case.

    Returns:
        The declaration.

    Raises:
        KeyError: If no procedure is called that.
    """
    for candidate in PROCEDURES:
        if candidate.name.upper() == name.upper():
            return candidate
    raise KeyError(f"no write procedure {name!r} in CHIP_CHAT.ACCOUNTS")


def separable() -> Iterator[Procedure]:
    """Yield the procedures that model something the published record does not.

    One, today. The point of the iterator is what it makes checkable: every
    invented procedure has a file to itself, so removing it is deleting a file
    rather than editing three procedures that should survive it.
    """
    for candidate in PROCEDURES:
        if candidate.invention is not None:
            yield candidate
