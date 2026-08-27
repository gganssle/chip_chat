"""The nightly demo-data reset, as data -- and the manual trigger, as a command.

Issue #47 on one side, `sql/14_demo_reset.sql` on the other, and this module is
the third party to the argument, the way `chip_chat.snowflake.procedures` is for
the write path. Nothing here creates anything: `snowflake/sql/` is still the only
thing that does. What is here is the part of the job the SQL cannot check about
itself, plus the one thing #47 asks for that is not SQL at all -- *"a manual
trigger exists"*, because the first time a demo goes badly is when a reset is
most needed and a cron entry is not a thing an operator can reach for.

Four things are declared here:

**How long a session lives, and that the number is DERIVED.**
:data:`SESSION_TTL_DAYS` is not a taste. `api.app` sets the session cookie with
``max_age=86_400``, so a visitor can return to their ``demo_id`` for one day and
then never again; a TTL shorter than that would age out visitors who could still
come back, which is #9's persistence undone by the job meant to respect it.
:func:`ttl_is_sound` is that relation as an assertion rather than as a paragraph.

**What a reset may delete, and what it must not.** :data:`BANDED_DELETES` names
the three tables it may only delete from ABOVE :data:`LIVE_ID_BAND`, and
:data:`PRESERVED_BELOW_BAND` says what is underneath: eighteen months of
generated history, which is the thing the demo is for.
`tests/test_demo_reset.py` fails if a ``DELETE`` in the SQL touches one of those
tables without the band predicate on it, which is the single most expensive
mistake this file could contain and the one that would look right in review.

**What it restores, and from where.** :data:`RESTORED_COLUMNS` and
:data:`CLEARED_COLUMNS` are every column the reset writes on ``demo_visitors``.
Their union has to be exactly the columns a live demo can move -- the three
`schema.EDITABLE_COLUMNS`, plus the two the app keeps its own session
bookkeeping in -- and a test asserts that in both directions, so a column added
to ``demo_visitors`` that a visitor can change is one this module fails until
somebody decides whether a reset puts it back.

**That the rejection codes in the SQL are the ones anybody was told about.**
:data:`REJECTIONS`, held to the SQL the way `procedures.Procedure.rejections` is.

The gap this tier does not close is :data:`ENFORCED_ELSEWHERE`, and it is stated
once rather than in four docstrings.
"""

from __future__ import annotations

import argparse
import json
from typing import Final

from chip_chat.snowflake import account, procedures, snow

__all__ = [
    "BANDED_DELETES",
    "BASELINE_TABLE",
    "CLEARED_COLUMNS",
    "COOKIE_MAX_AGE_SECONDS",
    "ENFORCED_ELSEWHERE",
    "LIVE_ID_BAND",
    "PRESERVED_BELOW_BAND",
    "PROCEDURE",
    "REJECTIONS",
    "RESTORED_COLUMNS",
    "SESSION_TTL_DAYS",
    "TASK",
    "TASK_SCHEDULE",
    "TASK_WAREHOUSE",
    "WHOLESALE_DELETES",
    "call",
    "main",
    "ttl_is_sound",
]

PROCEDURE: Final = "reset_demo_sessions"
"""The procedure `sql/14_demo_reset.sql` creates, in ``CHIP_CHAT.ACCOUNTS``.

One procedure, called by two things: the task below, and this module's
``main``. The nightly run and the manual run are therefore the same code with
the same arguments, which is the only arrangement in which the manual one is
worth having -- a hand-written variant is a thing nobody has run this month.
"""

TASK = "reset_demo_sessions_nightly"
"""The scheduled task, in ``CHIP_CHAT.ACCOUNTS``. Created by the same file."""

TASK_SCHEDULE: Final = "USING CRON 0 9 * * * UTC"
"""09:00 UTC daily, two hours after #39's publish starts at 07:00.

Not an ordering requirement -- this job restores ``demo_visitors``, which the
publisher cannot see -- but both write ``ACCOUNTS.orders``, and a ``DELETE``
landing inside an ``INSERT OVERWRITE`` is a lock wait at best.
"""

TASK_WAREHOUSE: Final = account.PUBLISH_WAREHOUSE
"""The batch lane. A reset is a batch, and the split exists so that a batch
cannot queue in front of a conversation -- and the serving warehouse cancels
anything still running after sixty seconds, which a loop over the roster could
plausibly exceed."""

BASELINE_TABLE: Final = "demo_visitor_baseline"
"""Where the generated state of ``demo_visitors`` is kept. `sql/07_accounts.sql`.

Loaded from the generator's own ``demo_visitors.jsonl``, in the same run as
``demo_visitors`` itself -- `schema.Table.source` is where that is declared.
That is what makes #47's first acceptance criterion, *"restores generated state
exactly, verified against the generator's output"*, a thing a reviewer can check
rather than a thing they have to believe.
"""

LIVE_ID_BAND: Final = procedures.LIVE_ID_BAND
"""Re-exported rather than restated. The reset and the write path have to agree
about where generated history stops, and two constants would eventually not."""

COOKIE_MAX_AGE_SECONDS: Final = 86_400
"""What `api.app` sets on the session cookie, transcribed.

A copy, and the risk of a copy is accepted here for the same reason
`semantic.SETTLED_STATUS` accepts it: the alternative is this package importing
the API's app module to read one integer. :func:`ttl_is_sound` is what makes the
copy load-bearing rather than decorative -- if somebody shortens the cookie, the
TTL is still sound; if somebody lengthens it past the TTL and updates this
constant, the test fails and the TTL has to be argued again.
"""

SESSION_TTL_DAYS: Final = 2
"""How long a visitor's session survives inactivity, in days.

DERIVED, not invented, which is why it has no ``[INVENTED]`` tag beside it. The
session cookie lives :data:`COOKIE_MAX_AGE_SECONDS`, so a visitor can return to
their ``demo_id`` for one day and then cannot reach it again by any means. The
TTL is that day plus one: everybody who could possibly come back still has their
state, and nobody is kept for a visit that cannot happen.

The second day is the slack, and it is doing real work rather than being
round-number caution -- a visit that starts in the last minute of a cookie's
life is a conversation that may run past the cookie's own expiry, and a reset
that fired at exactly 24 hours would land in the middle of it.
"""

RESTORED_COLUMNS: Final = (
    "display_name",
    "home_store_override",
    "stated_preferences",
    "last_seen",
)
"""The columns the reset copies back out of :data:`BASELINE_TABLE`.

The first three are `schema.EDITABLE_COLUMNS` and two of them are generated
non-null for some customers, so "restore" is emphatically not "set to NULL".
``last_seen`` is the fourth because putting it back is what makes an aged-out
visitor indistinguishable from one nobody was ever assigned, rather than one
that is due to be aged out again tomorrow morning.
"""

CLEARED_COLUMNS: Final = ("thread_id",)
"""The columns the reset nulls rather than restores.

One, and it is null in the baseline anyway -- data-gen's population has never
spoken to anybody. It is listed separately because the reason is different: the
pointer is cleared so that the next visitor handed this fixture cannot resume a
stranger's conversation. The Foundry thread on the other end of it is not
deleted, because nothing in Snowflake can call Azure; the ids come back on the
receipt instead.
"""

BANDED_DELETES: Final[dict[str, str]] = {
    "orders": "order_id",
    "order_items": "order_id",
    "loyalty_ledger": "entry_id",
}
"""Tables the reset deletes from, and the identifier that says which half.

Only rows whose identifier is at or above :data:`LIVE_ID_BAND` may go. Every one
of these tables holds eighteen months of generated history underneath that band,
and ``order_items`` is banded on its ORDER's identifier rather than on one of
its own because a line has no identifier of its own.
"""

WHOLESALE_DELETES: Final = ("action_receipts",)
"""Tables the reset empties for an aged visitor, with no band.

One. A receipt exists only because a live write made it, so there is no
generated half to protect -- and a spent retry key that outlived the order it
made is worse than useless: it replays a receipt for an order nobody can find.
"""

PRESERVED_BELOW_BAND: Final = (
    "eighteen months of generated orders, their lines and their loyalty ledger. "
    "This is what a returning visitor's assistant is personal about, what every "
    "gold mart is computed from, and what #9's decision to persist sessions was "
    "made to protect. A DELETE on one of these tables without the band "
    "predicate on it would empty a persona and pass every test that counts rows "
    "afterwards, because the count it would produce is a plausible one"
)
"""Why :data:`BANDED_DELETES` is banded, in the file a test can read it from."""

REJECTIONS: Final = frozenset(
    {
        "TTL_TOO_SHORT",
        "MAINTENANCE_ESCAPE_UNAVAILABLE",
        "BASELINE_NOT_LOADED",
        "RESET_FAILED",
    }
)
"""Every way the procedure can decline, and the set is closed by a test.

``MAINTENANCE_ESCAPE_UNAVAILABLE`` is the one worth reading. #43's row access
policies filter ``DELETE`` and ``UPDATE`` as well as ``SELECT``, so a reset run
without the maintenance escape in effect changes nothing at all and reports a
clean run -- personas drifting for a month with a green job beside them. The
procedure therefore checks that the escape actually took, and fails rather than
succeeding emptily.
"""

ENFORCED_ELSEWHERE: Final[dict[str, str]] = {
    "last_seen on a read-only turn": (
        "a visitor who only talks writes nothing anywhere, so nothing dates "
        "them. The reset refuses to guess -- it holds such a visitor and "
        "reports the count as held_no_clock -- but the fix is in the app tier: "
        "whatever writes demo_visitors.thread_id when a session binds is the "
        "thing that should write last_seen beside it, because both are the "
        "same row's session bookkeeping. cc-9xod"
    ),
    "the Foundry thread behind a retired pointer": (
        "Snowflake cannot call Azure. The reset clears the pointer and returns "
        "the ids under threads_retired; deleting the thread itself needs the "
        "project credential, which is agent/'s. cc-mdmf"
    ),
    "live rows the nightly publish overwrites": (
        "#39 replaces orders, order_items and loyalty_ledger wholesale every "
        "night, which erases live-band rows for every visitor rather than for "
        "the aged-out ones. docs/nightly-publish.md section 7 routes that "
        "decision to #47 and docs/demo-reset.md section 6 makes it: live rows "
        "survive until their visitor ages out, so the publish is what has to "
        "change, and that is cc-fxf4. The reset is correct either way -- it "
        "deletes what is there"
    ),
    "unconfirmed order drafts": (
        "they never reach Snowflake. api.drafts holds them in the ops API's "
        "own memory with a 900-second TTL and sweeps them itself, so a nightly "
        "job has nothing to collect -- the ones that would outlive a restart "
        "do not outlive the restart"
    ),
}
"""What #47 asks about that this file does not do, and where each is done.

Written down rather than left as an absence, for the same reason
`procedures.ENFORCED_ELSEWHERE` is: a gap nobody named is a gap nobody owns.
"""


def ttl_is_sound() -> bool:
    """Return whether :data:`SESSION_TTL_DAYS` outlives the session cookie.

    The whole argument for the number, as one comparison. A TTL at or below the
    cookie's life would age out visitors who can still return, which is the
    persistence decision of #9 being undone by the job written to respect it.
    """
    return SESSION_TTL_DAYS * 86_400 > COOKIE_MAX_AGE_SECONDS


def call(ttl_days: int = SESSION_TTL_DAYS, *, dry_run: bool = False) -> dict:
    """Run the reset now, and return the receipt it produced.

    The same procedure the nightly task calls, with the same arguments. It runs
    as :data:`~chip_chat.snowflake.account.ADMIN_ROLE` on the publish warehouse:
    the role because #43's maintenance escape is honoured for no other, and the
    warehouse because a reset is a batch and the serving warehouse cancels
    anything still running after a minute.

    The procedure sets and unsets the escape itself, so there is no preamble
    here to forget -- and no session left cross-visitor if this process dies
    halfway, because the variable never lived in this session at all.

    Args:
        ttl_days: How long a session survives inactivity.
        dry_run: Select and report, delete and restore nothing.

    Returns:
        The receipt, parsed. ``ok`` is False for every rejection in
        :data:`REJECTIONS`, and the reason is under ``rejection``.

    Raises:
        snow.SnowError: If the call itself failed, which is a different thing
            from the procedure declining -- a decline is a receipt.
    """
    rows = snow.query(
        f"USE ROLE {account.ADMIN_ROLE};\n"
        f"USE WAREHOUSE {account.PUBLISH_WAREHOUSE};\n"
        f"CALL {account.schema('ACCOUNTS')}.{PROCEDURE}"
        f"({int(ttl_days)}, {'TRUE' if dry_run else 'FALSE'});"
    )[-1]
    if not rows:
        raise snow.SnowError(
            f"{PROCEDURE} returned no row",
            "a CALL that produces nothing is a procedure that is not there, or "
            "a connection that is not the account this package describes.",
        )
    return json.loads(next(iter(rows[0].values())))


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit status."""
    parser = argparse.ArgumentParser(
        prog="python -m chip_chat.snowflake.reset",
        description=(
            "Age demo sessions out and restore those visitors to the state "
            "data-gen produced. Issue #47's manual trigger; the nightly task "
            "calls the same procedure with the same arguments."
        ),
    )
    parser.add_argument(
        "--ttl-days",
        type=int,
        default=SESSION_TTL_DAYS,
        help=(
            f"how long a session survives inactivity (default {SESSION_TTL_DAYS}, "
            "which is the session cookie's day plus one)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what a run would age out, and change nothing",
    )
    arguments = parser.parse_args(argv)

    snow.require_cli()
    receipt = call(arguments.ttl_days, dry_run=arguments.dry_run)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    if not receipt.get("ok"):
        print(f"\nrefused: {receipt.get('rejection')}")
        return 1
    if arguments.dry_run:
        print(f"\ndry run -- {receipt.get('visitors_aged')} visitors would be reset")
    else:
        print(f"\n{receipt.get('visitors_aged')} visitors reset")
    if receipt.get("held_no_clock"):
        print(
            f"{receipt['held_no_clock']} visitors held: they have a conversation "
            "and no dated activity, so nothing here can tell whether they left. "
            "The app tier owes demo_visitors.last_seen a write when a session "
            "binds -- see reset.ENFORCED_ELSEWHERE."
        )
    if receipt.get("held_no_baseline"):
        print(
            f"{receipt['held_no_baseline']} visitors held: "
            f"{BASELINE_TABLE} has no row for them, so they cannot be restored "
            "and were therefore not emptied either. Re-run the load."
        )
    threads = receipt.get("threads_retired") or []
    if threads:
        print(
            f"{len(threads)} Foundry threads were detached and NOT deleted -- "
            "nothing here can call Azure. The ids are on the receipt."
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a command
    raise SystemExit(main())
