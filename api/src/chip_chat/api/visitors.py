"""Who a visitor is, decided here and nowhere else.

RFC-001 §05 is the section to read before this module, and its sentence is the
specification:

    Identity originates in the app's server-side session store, is applied to
    the Snowflake connection as a session variable, and is enforced by row
    access policies on every visitor-scoped table.

:mod:`chip_chat.api.pool` built the second clause and left the first as a
:class:`~chip_chat.api.pool.VisitorSessions` protocol with a placeholder behind
it, saying in as many words that *"the store itself belongs to [#66]"*. This is
that store, plus the thing that puts a visitor in it: persona assignment.

**Assignment is the product risk, not a nicety.** Issue #66 says it plainly --
*the cold start is the product risk, and an empty account is how this demo
dies.* A visitor who lands on an account with no orders, no home store and no
points has nothing to ask, so :class:`SnowflakeRoster` does not merely prefer
populated fixtures, it **refuses to offer an unpopulated one**. A roster with no
qualifying rows is empty, and an empty roster is a state the app is told about
(:meth:`VisitorDesk.admit` returns ``None``) rather than one it discovers by
serving somebody a blank account.

**Where the identity comes from, and the three places it does not.** The
``demo_id`` is read off a roster row that Snowflake itself returned, bound to a
session id the app minted, and handed to the pool by session id. It is not a
request field, not a tool argument and not a model output -- and the reason it
is none of those is that no signature in this module accepts one.
:meth:`VisitorDesk.admit` takes a session id and an optional display name;
that is the entire input surface. ``api/tests/test_visitors.py`` holds every
public callable here to
:data:`~chip_chat.snowflake.procedures.IDENTITY_VOCABULARY`, which is the same
absence :mod:`chip_chat.api.ops` is held to one tier down.

**The roster read is the one deliberately unbound query.** #43's ``entry_roster``
policy exists precisely for it: ``persona_fixtures`` is readable while nothing
is bound and narrows to a single row once something is, so the roster is chosen
from before there is a visitor to choose it *for*. :meth:`VisitorPool.unbound`
is the checkout that does it and the only one this module makes.

Restart behaviour is issue #66's fourth acceptance criterion, and it offers a
choice: *survives a container restart, or degrades in a way that is decided
rather than discovered.* Both are here. :class:`FileJournal` is the surviving
half -- an append-only record on a mounted share, replayed at start-up -- and
:class:`NoJournal` is the decided degradation, which announces itself with a
``WARNING`` at assembly rather than being found out when a visitor comes back
to a stranger's account. A journal path that cannot be written falls back to
:class:`NoJournal` loudly, because a demo that will not boot when a file share
is unmounted is worse than one that forgets.

.. code-block:: python

    desk = VisitorDesk(SnowflakeRoster(pool), store=VisitorSessionStore(journal))

    visitor = desk.admit(session_id, display_name="Sam")   # entry
    if visitor is None:
        ...                                                # roster not loaded

    with pool.for_session(session_id) as connection:       # the binding
        rows = connection.execute("SELECT order_id FROM orders")

The store is what ``pool.for_session`` resolves against, so those two statements
are the whole of RFC-001 §05's trusted path as running code.
"""

import json
import logging
import os
import random
import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Protocol

from chip_chat.api.clock import Clock, SystemClock
from chip_chat.api.pool import UnboundConnection, VisitorPool
from chip_chat.snowflake.procedures import DISPLAY_NAME_MAX_LENGTH

__all__ = [
    "DEFAULT_ROSTER_TTL_SECONDS",
    "DEFAULT_SESSION_MAX_AGE_DAYS",
    "JOURNAL_VARIABLE",
    "MAX_DISPLAY_NAME_CHARS",
    "ROSTER_COLUMNS",
    "SHIPPED_ROSTER_PATH",
    "VISITORS_LOGGER",
    "FileJournal",
    "NoJournal",
    "PersonaFixture",
    "PersonaRoster",
    "SessionJournal",
    "SnowflakeRoster",
    "StaticRoster",
    "VisitorDesk",
    "VisitorSession",
    "VisitorSessionStore",
    "clean_display_name",
    "journal_from_env",
    "shipped_roster",
]

VISITORS_LOGGER: Final = logging.getLogger("chip_chat.api.visitors")
"""Where an empty roster and an unusable journal are reported.

Both are conditions a visitor cannot see and an operator has to. An empty roster
means every visitor is being served unbound -- correct, and correct in the way
that shows nothing -- and a journal that fell back to :class:`NoJournal` means
the next restart forgets everybody. Neither raises, so a log line is the only
place they exist.
"""

JOURNAL_VARIABLE: Final = "CHIP_CHAT_SESSION_JOURNAL"
"""Path the session journal is kept at. Unset means :class:`NoJournal`.

Named rather than defaulted to a path on the mounted share, because a default
would make "sessions survive a restart" true on a machine with that directory
and false everywhere else, which is exactly the discovered degradation the
acceptance criterion rules out.
"""

DEFAULT_ROSTER_TTL_SECONDS: Final = 300.0
"""How long a read of ``persona_fixtures`` is reused before it is taken again.

The roster changes when the nightly load runs (#47) and at no other time, so
this is short enough to pick a reload up within one visit and long enough that
a busy minute is one query rather than a hundred.
"""

DEFAULT_SESSION_MAX_AGE_DAYS: Final = 30
"""How long a journalled session is replayed after its last activity.

Matched to the shape of #47's ageing policy rather than to its number: a visitor
Snowflake has aged out should not be restored by this file into a binding whose
rows have been reset underneath it. Erring long is the safe direction -- a
restored session that finds a reset account is a visitor starting fresh, which
is the same thing an unrestored one gets.
"""

MAX_DISPLAY_NAME_CHARS: Final = DISPLAY_NAME_MAX_LENGTH
"""Longest display name accepted from the entry form.

A first name, invented, and the only free text a visitor supplies that the app
keeps. There is no PII in this system by construction --
``sql/07_accounts.sql`` says so of the column this mirrors.

The number is #46's rather than a second one that happens to agree: the entry
form and ``update_preferences`` write the same column, and an app that accepted
a name the procedure would refuse would produce a visitor whose rename silently
failed one tier down.
"""

_TOUCH_INTERVAL_SECONDS: Final = 60.0
"""How stale ``last_seen`` may get in the journal before a turn rewrites it.

Every turn touching the session would make the journal one append per message
on a mounted file share. A minute of drift costs nothing: the value is read by a
restart and by #47's ageing, and neither can tell a minute.
"""

ROSTER_COLUMNS: Final = (
    "demo_id",
    "persona_id",
    "label",
    "rank",
    "home_store",
    "home_store_name",
    "points_balance",
    "usual_item_id",
    "order_count",
    "lifetime_spend",
    "narrative",
)
"""The columns of ``ACCOUNTS.persona_fixtures`` the entry flow reads.

Every one of them is either the identity, the evidence that the account is
populated, or something the opening message says out loud. ``rank`` is here
because rank one is the archetype's strongest exemplar and a demo should hand
those out first. ``api/tests/test_visitors.py`` holds this tuple against
:func:`chip_chat.snowflake.schema.columns_of`, so a column renamed in the DDL
fails a test here rather than an empty roster in production.
"""

_ROSTER_QUERY: Final = (
    f"SELECT {', '.join(ROSTER_COLUMNS)} FROM persona_fixtures ORDER BY persona_id, rank"
)

_CONTROL_CHARACTERS: Final = re.compile(r"[\x00-\x1f\x7f]")


class SessionJournal(Protocol):
    """Where sessions are written so a restart can read them back.

    Two methods, and deliberately no delete: a session is forgotten by ageing
    out of :meth:`restore`, which is one rule in one place rather than a
    lifetime the store and the file have to agree about.
    """

    def record(self, session: "VisitorSession") -> None:
        """Persist ``session``, replacing any earlier record of it."""
        ...

    def restore(self) -> Sequence["VisitorSession"]:
        """Return every session still worth remembering, oldest first."""
        ...

    @property
    def durable(self) -> bool:
        """Whether a restart will actually find anything here."""
        ...


class NoJournal:
    """The decided degradation: sessions live for as long as the process does.

    Honest for a local run and for the first deployment, and it is a class
    rather than a ``None`` check so that "this deployment forgets on restart" is
    something the assembly states and logs rather than something a visitor finds
    out. See :func:`journal_from_env`.
    """

    __slots__ = ()

    def record(self, session: "VisitorSession") -> None:
        """Do nothing, on purpose."""

    def restore(self) -> Sequence["VisitorSession"]:
        """Return nothing, on purpose."""
        return ()

    @property
    def durable(self) -> bool:
        return False


class FileJournal:
    """An append-only record of every binding, replayed at start-up.

    One JSON object per line, appended under a lock and flushed, so a container
    killed between two turns loses at most the turn in flight. Later lines win
    on replay, which makes an append an update and keeps the write path a single
    ``write`` with no read underneath it.

    The file is compacted during :meth:`restore` -- one line per surviving
    session, written to a temporary file and moved into place -- so its size is
    bounded by the number of live visitors rather than by how long the container
    has been up.
    """

    __slots__ = ("_clock", "_lock", "_max_age", "_path")

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        clock: Clock | None = None,
        max_age_days: int = DEFAULT_SESSION_MAX_AGE_DAYS,
    ) -> None:
        """Open the journal.

        Args:
            path: The file. Its parent directory must exist and be writable;
                :func:`journal_from_env` is what checks that and falls back.
            clock: Time source, for ageing. Defaults to the system clock.
            max_age_days: How long after its last activity a session is still
                replayed. See :data:`DEFAULT_SESSION_MAX_AGE_DAYS`.
        """
        self._path = Path(path)
        self._clock = clock if clock is not None else SystemClock()
        self._max_age = timedelta(days=max_age_days)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """The file being written."""
        return self._path

    @property
    def durable(self) -> bool:
        return True

    def record(self, session: "VisitorSession") -> None:
        """Append ``session``. Later lines win, so this is also an update.

        Args:
            session: The binding to persist.

        Raises:
            OSError: If the file cannot be written. The caller is the session
                store, which treats a failed append as a lost restart rather
                than a failed request -- see :class:`VisitorSessionStore`.
        """
        line = json.dumps(session.as_record(), separators=(",", ":"))
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")

    def restore(self) -> Sequence["VisitorSession"]:
        """Replay the file, drop what has aged out, and compact what is left.

        A malformed line is skipped rather than fatal. The file is written by
        this class alone, so a broken line means a truncated write during a
        kill -- and refusing to start over one would turn a lost turn into a
        lost deployment.

        Returns:
            The surviving sessions, least recently seen first.
        """
        with self._lock:
            surviving = self._replay()
            self._compact(surviving)
        return surviving

    def _replay(self) -> list["VisitorSession"]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError:
            VISITORS_LOGGER.exception(
                "session journal at %s could not be read; starting empty",
                self._path,
            )
            return []
        cutoff = self._clock.now() - self._max_age
        latest: dict[str, VisitorSession] = {}
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                session = VisitorSession.from_record(json.loads(line))
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
            latest[session.session_id] = session
        return sorted(
            (item for item in latest.values() if item.last_seen >= cutoff),
            key=lambda item: item.last_seen,
        )

    def _compact(self, sessions: Sequence["VisitorSession"]) -> None:
        temporary = self._path.with_suffix(f"{self._path.suffix}.compact")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                for session in sessions:
                    handle.write(
                        f"{json.dumps(session.as_record(), separators=(',', ':'))}\n"
                    )
            temporary.replace(self._path)
        except OSError:
            # A journal that will not compact is still a journal that reads.
            # Losing the compaction costs disk; refusing to start costs the demo.
            VISITORS_LOGGER.exception(
                "session journal at %s could not be compacted", self._path
            )
            temporary.unlink(missing_ok=True)


def journal_from_env(clock: Clock | None = None) -> SessionJournal:
    """Return the journal this deployment is configured for, and say which.

    The decision and its announcement are the same statement, which is the
    point: issue #66's criterion is not that sessions survive but that the
    behaviour is *decided*. An unset variable is a decision, and it is logged as
    one; a path that cannot be written is a misconfiguration, and it is logged
    as one rather than raised, because a file share that failed to mount should
    not also take the demo down.

    Args:
        clock: Time source for ageing. Defaults to the system clock.

    Returns:
        A :class:`FileJournal` when :data:`JOURNAL_VARIABLE` names a writable
        path, and :class:`NoJournal` otherwise.
    """
    configured = os.environ.get(JOURNAL_VARIABLE, "").strip()
    if not configured:
        VISITORS_LOGGER.warning(
            "%s is unset: visitor sessions live in memory and a restart assigns "
            "returning visitors a new persona. Set it to a path on a mounted "
            "share to make them durable.",
            JOURNAL_VARIABLE,
        )
        return NoJournal()
    journal = FileJournal(configured, clock=clock)
    try:
        journal.path.parent.mkdir(parents=True, exist_ok=True)
        with journal.path.open("a", encoding="utf-8"):
            pass
    except OSError:
        VISITORS_LOGGER.exception(
            "%s is set to %s, which cannot be written; falling back to an "
            "in-memory session store that forgets on restart",
            JOURNAL_VARIABLE,
            configured,
        )
        return NoJournal()
    return journal


@dataclass(frozen=True, slots=True)
class PersonaFixture:
    """One row of ``ACCOUNTS.persona_fixtures``: a customer, not an archetype.

    ``docs/decisions/persona-fixtures.md``'s standard is that a reviewer who
    doubts the narrative re-derives it from the row, so the measurements travel
    with the sentence rather than behind it.

    Attributes:
        demo_id: The synthetic customer. The only identity in this module, and
            it arrives from Snowflake rather than from any caller.
        persona_id: Which of the seven archetypes they are an exemplar of.
        label: What the demo calls that archetype out loud.
        rank: Position among the archetype's fixtures, from one. Rank one is the
            strongest exemplar by the archetype's own measure.
        home_store: Where they actually order, as a ``stores.store_id``.
        home_store_name: That restaurant's published name, where there is one.
        points_balance: Loyalty points. Part of what makes the account non-empty.
        usual_item_id: The item they order most, where they have one.
        order_count: How many orders they have placed. The cold-start test.
        lifetime_spend: What those orders totalled.
        narrative: The sentence the opening message is written from.
    """

    demo_id: str
    persona_id: str
    label: str
    rank: int = 1
    home_store: int | None = None
    home_store_name: str | None = None
    points_balance: int | None = None
    usual_item_id: str | None = None
    order_count: int = 0
    lifetime_spend: float | None = None
    narrative: str | None = None

    @property
    def populated(self) -> bool:
        """Whether this account has something in it to talk about.

        Issue #66's own three: order history, a home store and a points balance.
        A fixture that fails this is not handed to a visitor -- see
        :class:`SnowflakeRoster`.
        """
        return (
            self.order_count > 0
            and self.home_store is not None
            and self.points_balance is not None
        )

    @classmethod
    def from_row(cls, row: Sequence[object]) -> "PersonaFixture":
        """Build a fixture from one row of :data:`ROSTER_COLUMNS`.

        Args:
            row: The columns, in the order :data:`ROSTER_COLUMNS` names them.

        Returns:
            The fixture.

        Raises:
            ValueError: If the row is the wrong width, or carries no
                ``demo_id``. Both mean the query and this class have drifted
                apart, which is a wiring bug and not a row to skip quietly.
        """
        if len(row) != len(ROSTER_COLUMNS):
            raise ValueError(
                f"persona_fixtures row has {len(row)} columns; "
                f"{len(ROSTER_COLUMNS)} were selected"
            )
        values = dict(zip(ROSTER_COLUMNS, row, strict=True))
        demo_id = _text(values["demo_id"])
        if not demo_id:
            raise ValueError("a persona fixture with no demo_id is not a visitor")
        return cls(
            demo_id=demo_id,
            persona_id=_text(values["persona_id"]) or "",
            label=_text(values["label"]) or "",
            rank=_whole(values["rank"]) or 1,
            home_store=_whole(values["home_store"]),
            home_store_name=_text(values["home_store_name"]),
            points_balance=_whole(values["points_balance"]),
            usual_item_id=_text(values["usual_item_id"]),
            order_count=_whole(values["order_count"]) or 0,
            lifetime_spend=_number(values["lifetime_spend"]),
            narrative=_text(values["narrative"]),
        )


@dataclass(frozen=True, slots=True)
class VisitorSession:
    """One browser, one synthetic customer, and what the app remembers of both.

    Frozen because a binding that can be edited in place is a binding two
    threads can disagree about. :class:`VisitorSessionStore` replaces the record
    rather than mutating it.

    Attributes:
        session_id: The value in the session cookie. Minted by the app; a
            client-supplied one that does not resolve here is simply unbound.
        demo_id: The synthetic customer. Read off a roster row Snowflake
            returned, and applied to a connection by
            :meth:`~chip_chat.api.pool.VisitorPool.for_session`.
        persona_id: The archetype, for the span and for the opening message.
        label: The archetype's spoken name.
        display_name: The invented first name the visitor typed, or ``None``.
        thread_id: The Foundry thread this conversation lives in.
            ``docs/decisions/foundry-agent-shape.md`` puts message history in
            Microsoft-managed storage and the *pointer* in the app, because the
            pointer has to outlive the visit.
        created_at: When the binding was made, UTC.
        last_seen: The visitor's most recent activity, UTC. What the journal
            ages on.
        fixture: The roster row this visitor was assigned, where it is still
            known. Absent after a restart, because the journal keeps the
            binding and Snowflake keeps the account.
    """

    session_id: str
    demo_id: str
    persona_id: str
    label: str
    display_name: str | None
    thread_id: str | None
    created_at: datetime
    last_seen: datetime
    fixture: PersonaFixture | None = None

    def as_record(self) -> dict[str, Any]:
        """Return the JSON object :class:`FileJournal` writes.

        The fixture is deliberately not in it. The roster row is Snowflake's to
        state and re-reading it is one query; journalling a copy would let a
        restart serve an account summary the database has since reset.
        """
        return {
            "session_id": self.session_id,
            "demo_id": self.demo_id,
            "persona_id": self.persona_id,
            "label": self.label,
            "display_name": self.display_name,
            "thread_id": self.thread_id,
            "created_at": self.created_at.isoformat(),
            "last_seen": self.last_seen.isoformat(),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "VisitorSession":
        """Rebuild a session from a journal line.

        Args:
            record: One decoded line of :meth:`as_record`.

        Returns:
            The session.

        Raises:
            ValueError: If the line carries no session or no visitor, or if a
                timestamp will not parse. The caller skips the line.
        """
        session_id = _text(record.get("session_id"))
        demo_id = _text(record.get("demo_id"))
        if not session_id or not demo_id:
            raise ValueError("a journal line needs both a session and a visitor")
        return cls(
            session_id=session_id,
            demo_id=demo_id,
            persona_id=_text(record.get("persona_id")) or "",
            label=_text(record.get("label")) or "",
            display_name=_text(record.get("display_name")),
            thread_id=_text(record.get("thread_id")),
            created_at=_instant(record["created_at"]),
            last_seen=_instant(record["last_seen"]),
        )


class PersonaRoster(Protocol):
    """The synthetic customers a visitor may be assigned one of.

    Read-only, and it returns whole rows rather than identifiers, because the
    entry flow renders the account it just assigned and a second query for what
    it already has in hand is a second chance to get the identity wrong.
    """

    def fixtures(self) -> Sequence[PersonaFixture]:
        """Return every assignable customer. Empty when none is loaded."""
        ...


class StaticRoster:
    """A roster held in memory, for tests and for a fixed local run.

    Unpopulated fixtures are dropped at construction rather than at assignment,
    so a roster that would hand somebody an empty account is empty instead --
    the same refusal :class:`SnowflakeRoster` makes, made at the earliest moment
    this class can make it.
    """

    __slots__ = ("_fixtures",)

    def __init__(self, fixtures: Iterable[PersonaFixture] = ()) -> None:
        """Initialise the roster.

        Args:
            fixtures: The customers. Unpopulated ones are discarded.
        """
        self._fixtures = tuple(fixture for fixture in fixtures if fixture.populated)

    def fixtures(self) -> Sequence[PersonaFixture]:
        return self._fixtures


class SnowflakeRoster:
    """``persona_fixtures``, read on a connection that has bound nobody.

    The read #43's ``entry_roster`` policy was written for, and the only query
    this package makes outside a bound checkout. It is safe for the reason
    :meth:`~chip_chat.api.pool.VisitorPool.unbound` gives: every other
    visitor-scoped table is default deny, so an unbound connection reads zero
    rows from all seven of them.

    The result is cached for :data:`DEFAULT_ROSTER_TTL_SECONDS`, and a refresh
    that fails keeps the roster it already had. A Snowflake blip should cost a
    stale roster, never an empty one -- an empty roster means every new visitor
    is served unbound, which is the failure this class exists to avoid.
    """

    __slots__ = ("_clock", "_fixtures", "_lock", "_pool", "_read_at", "_ttl")

    def __init__(
        self,
        pool: VisitorPool,
        *,
        clock: Clock | None = None,
        ttl_seconds: float = DEFAULT_ROSTER_TTL_SECONDS,
    ) -> None:
        """Initialise the roster.

        Args:
            pool: The connection pool. Only :meth:`VisitorPool.unbound` is used.
            clock: Time source for the cache. Defaults to the system clock.
            ttl_seconds: How long a read is reused.
        """
        self._pool = pool
        self._clock = clock if clock is not None else SystemClock()
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._fixtures: tuple[PersonaFixture, ...] = ()
        self._read_at: float | None = None

    def fixtures(self) -> Sequence[PersonaFixture]:
        """Return the assignable customers, reading them again if the cache is old."""
        with self._lock:
            if self._read_at is not None and (
                self._clock.monotonic() - self._read_at < self._ttl
            ):
                return self._fixtures
            try:
                fixtures = self._read()
            except Exception:
                VISITORS_LOGGER.exception(
                    "persona_fixtures could not be read; keeping the %d fixture(s) "
                    "already loaded",
                    len(self._fixtures),
                )
                # Do not stamp `_read_at`: the next caller tries again rather
                # than waiting out a TTL that a failure never earned.
                return self._fixtures
            self._fixtures = fixtures
            self._read_at = self._clock.monotonic()
            if not fixtures:
                VISITORS_LOGGER.warning(
                    "persona_fixtures returned no populated rows: every visitor "
                    "will be served without a synthetic account until #47's load "
                    "has run"
                )
            return self._fixtures

    def _read(self) -> tuple[PersonaFixture, ...]:
        with self._pool.unbound() as connection:
            rows = _roster_rows(connection)
        fixtures: list[PersonaFixture] = []
        skipped = 0
        for row in rows:
            fixture = PersonaFixture.from_row(row)
            if not fixture.populated:
                skipped += 1
                continue
            fixtures.append(fixture)
        if skipped:
            VISITORS_LOGGER.warning(
                "%d persona fixture(s) were not offered: no order history, no "
                "home store or no points balance",
                skipped,
            )
        return tuple(fixtures)


def _roster_rows(connection: UnboundConnection) -> Sequence[Sequence[object]]:
    """Run the roster query. Separate so a test can point at the statement."""
    return connection.execute(_ROSTER_QUERY)


SHIPPED_ROSTER_PATH: Final = Path(__file__).with_name("fixtures") / (
    "persona_fixtures.json"
)
"""An export of ``ACCOUNTS.persona_fixtures``, committed and shipped in the image.

This file exists because of a gap between two issues that is real today and will
close on its own. :class:`SnowflakeRoster` is the right roster and reads the
authoritative rows, but it needs a connection factory, and there is no Snowflake
driver in this lockfile: ``build_service`` is called with ``connect=None`` on
every deployment, so the roster is empty and every visitor is served unbound.

An unbound visitor is precisely the failure PRD §06 and issue #67 are written
about -- *a visitor types their name, arrives at an empty account, asks the only
question that occurs to them, and is told they have zero points and no order
history.* Between "no personas at all" and "the same twenty-eight rows Snowflake
holds, read from a file", the second is better, and it is better in a way that
does not compromise anything: these are not invented accounts. They are the
rows ``data-gen`` generated and ``#26`` curated, exported verbatim, ``demo_id``
included, so a session bound from this file is bound to the *same* synthetic
customer a Snowflake-backed deployment would have bound it to.

``docs/decisions/shipped-persona-roster.md`` is the write-up, including the one
rule that keeps this honest: the shipped roster is consulted **only** when there
is no connection factory. The moment ``cc-lpy4`` lands a driver, this file stops
being read, and the fallback becomes dead weight rather than a second source of
truth competing with the first.
"""


def shipped_roster() -> StaticRoster:
    """Return the roster held in :data:`SHIPPED_ROSTER_PATH`.

    Returns:
        A :class:`StaticRoster` over the committed export, or an empty one when
        the file is missing or unreadable. Missing is not fatal: an empty roster
        is a state the app already handles, and a demo that refuses to boot
        because a data file did not make it into the image is worse than one
        that boots and says so.
    """
    try:
        payload = json.loads(SHIPPED_ROSTER_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        VISITORS_LOGGER.exception(
            "the shipped persona roster at %s could not be read; every visitor "
            "will be served without a synthetic account",
            SHIPPED_ROSTER_PATH,
        )
        return StaticRoster()
    fixtures = [
        PersonaFixture.from_row(tuple(record.get(column) for column in ROSTER_COLUMNS))
        for record in payload
    ]
    roster = StaticRoster(fixtures)
    VISITORS_LOGGER.warning(
        "no Snowflake connection factory was supplied, so persona assignment is "
        "reading the %d fixture(s) committed at %s rather than "
        "ACCOUNTS.persona_fixtures. See docs/decisions/shipped-persona-roster.md.",
        len(roster.fixtures()),
        SHIPPED_ROSTER_PATH.name,
    )
    return roster


class VisitorSessionStore:
    """The server-side session store RFC-001 §05's first clause names.

    Satisfies :class:`~chip_chat.api.pool.VisitorSessions`, so
    :meth:`VisitorPool.for_session` resolves against it and the identity reaches
    Snowflake without passing through a single caller's argument.

    Bindings are made by :class:`VisitorDesk` and by nothing else in the running
    system: :meth:`bind` is public because a test and a restart both need it,
    and neither of those is a request.
    """

    __slots__ = ("_journal", "_lock", "_max", "_sessions")

    def __init__(
        self,
        journal: SessionJournal | None = None,
        *,
        max_sessions: int = 8_192,
    ) -> None:
        """Initialise the store and replay whatever the journal remembers.

        Args:
            journal: Where bindings are persisted. Defaults to
                :class:`NoJournal`, which is the decided degradation.
            max_sessions: Bindings held before the least recently seen are
                dropped. A bound on memory, not a policy: the journal's ageing
                is what decides how long a visitor is remembered.
        """
        self._journal: SessionJournal = journal if journal is not None else NoJournal()
        self._max = max_sessions
        self._lock = threading.Lock()
        self._sessions: dict[str, VisitorSession] = {}
        for session in self._journal.restore()[-max_sessions:]:
            self._sessions[session.session_id] = session

    @property
    def durable(self) -> bool:
        """Whether a restart will find these bindings again."""
        return self._journal.durable

    def demo_id_for(self, session_id: str) -> str | None:
        """Return the visitor bound to ``session_id``, or ``None``.

        The one method :class:`~chip_chat.api.pool.VisitorPool` calls, and the
        whole of the seam between this store and the database session.
        """
        with self._lock:
            session = self._sessions.get(session_id)
        return session.demo_id if session is not None else None

    def session(self, session_id: str) -> VisitorSession | None:
        """Return the whole binding for ``session_id``, or ``None``."""
        with self._lock:
            return self._sessions.get(session_id)

    def sessions(self) -> Sequence[VisitorSession]:
        """Return every live binding, least recently seen first."""
        with self._lock:
            return sorted(self._sessions.values(), key=lambda item: item.last_seen)

    def bind(self, session: VisitorSession) -> VisitorSession:
        """Store ``session``, replacing any binding it already had.

        Args:
            session: The binding.

        Returns:
            The stored binding.
        """
        with self._lock:
            self._store(session)
        self._journal_record(session)
        return session

    def touch(self, session_id: str, *, now: datetime) -> VisitorSession | None:
        """Move a session's ``last_seen`` forward.

        Journalled only when the value has drifted by more than
        :data:`_TOUCH_INTERVAL_SECONDS`, so an active conversation is not one
        file append per message.

        Args:
            session_id: The conversation.
            now: The instant to record.

        Returns:
            The updated binding, or ``None`` if there was none.
        """
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is None:
                return None
            updated = replace(existing, last_seen=now)
            self._store(updated)
            drifted = (now - existing.last_seen).total_seconds()
        if drifted >= _TOUCH_INTERVAL_SECONDS:
            self._journal_record(updated)
        return updated

    def set_thread_id(self, session_id: str, thread_id: str) -> VisitorSession | None:
        """Record the Foundry thread this conversation lives in.

        ``docs/decisions/foundry-agent-shape.md`` puts the pointer in the app
        because it has to outlive the visit. Its eventual home is
        ``demo_visitors.thread_id``; until a write path to that column exists,
        the journal is what makes "outlives the visit" true.

        Args:
            session_id: The conversation.
            thread_id: The thread.

        Returns:
            The updated binding, or ``None`` if there was none.
        """
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is None:
                return None
            if existing.thread_id == thread_id:
                return existing
            updated = replace(existing, thread_id=thread_id)
            self._store(updated)
        self._journal_record(updated)
        return updated

    def release(self, session_id: str) -> None:
        """Forget ``session_id``. A later checkout for it fails closed."""
        with self._lock:
            self._sessions.pop(session_id, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def _store(self, session: VisitorSession) -> None:
        """Insert or replace a binding. Called with the lock held."""
        self._sessions.pop(session.session_id, None)
        self._sessions[session.session_id] = session
        while len(self._sessions) > self._max:
            # Insertion-ordered, and every write re-inserts, so this is the
            # least recently touched binding.
            self._sessions.pop(next(iter(self._sessions)))

    def _journal_record(self, session: VisitorSession) -> None:
        """Persist a binding, and never fail a request because it could not be.

        A journal that will not write costs the next restart its memory. Raising
        here would cost the visitor in front of us their turn, which is a worse
        trade in both directions.
        """
        try:
            self._journal.record(session)
        except OSError:
            VISITORS_LOGGER.exception(
                "session journal write failed; this binding will not survive a restart"
            )


class VisitorDesk:
    """Where a browser becomes a synthetic customer.

    The entry point of RFC-001 §05's trusted path, and the reason it is trusted
    is visible in the signature: :meth:`admit` takes a session id and a name,
    and there is no parameter through which a request body, a tool result or a
    model could name the visitor it wants to be.

    Assignment prefers an archetype nobody is currently browsing as, then a
    customer nobody is currently browsing as, then rank. That ordering is issue
    #66's second acceptance criterion made structural: two concurrent sessions
    get different personas because the desk looks at what is already held before
    it chooses, not because the roster is large enough to make a collision
    unlikely.
    """

    __slots__ = ("_clock", "_lock", "_random", "_roster", "_store")

    def __init__(
        self,
        roster: PersonaRoster,
        *,
        store: VisitorSessionStore | None = None,
        clock: Clock | None = None,
        entropy: random.Random | None = None,
    ) -> None:
        """Assemble the desk.

        Args:
            roster: The customers a visitor may be assigned. Positional and
                required: a desk that could assign nobody would have to invent
                somebody, and an invented account is the empty account #66 is
                written to prevent.
            store: The session store. Defaults to a fresh one with no journal.
            clock: Time source. Defaults to the system clock.
            entropy: Where the tie-break between equally good fixtures comes
                from. Injected so a test gets the same roster twice.
        """
        self._roster = roster
        self._store = store if store is not None else VisitorSessionStore()
        self._clock = clock if clock is not None else SystemClock()
        self._random = entropy if entropy is not None else random.Random()
        self._lock = threading.Lock()

    @property
    def store(self) -> VisitorSessionStore:
        """The session store, which is what the pool resolves against."""
        return self._store

    @property
    def roster(self) -> PersonaRoster:
        """The roster this desk assigns from."""
        return self._roster

    def visitor(self, session_id: str) -> VisitorSession | None:
        """Return the visitor bound to ``session_id``, without assigning one."""
        return self._store.session(session_id)

    def admit(
        self, session_id: str, *, display_name: str | None = None
    ) -> VisitorSession | None:
        """Return this session's visitor, assigning one if it has none.

        Idempotent for a session that already has a binding, which is what makes
        a returning visitor resume their own account rather than collect a
        second one: #9 decided visitor state persists between visits, and the
        cookie is how it is recognised.

        Args:
            session_id: The conversation, resolved from the session cookie by
                the app and never read from a request body.
            display_name: The invented first name from the entry form, if the
                visitor supplied one. Cleaned by :func:`clean_display_name`. A
                name given for an already-bound session replaces the one it had,
                because renaming is the one edit the entry screen offers.

        Returns:
            The binding, or ``None`` when the roster is empty -- a deployment
            whose synthetic population has not been loaded. The caller serves
            the demo unbound in that case, which is a decided state and one
            :data:`VISITORS_LOGGER` has already reported.
        """
        cleaned = clean_display_name(display_name)
        now = self._clock.now()
        existing = self._store.session(session_id)
        if existing is not None:
            if cleaned is not None and cleaned != existing.display_name:
                return self._store.bind(
                    replace(existing, display_name=cleaned, last_seen=now)
                )
            return self._store.touch(session_id, now=now) or existing

        fixtures = self._roster.fixtures()
        if not fixtures:
            return None
        with self._lock:
            # Re-read inside the lock: two requests for one new session arriving
            # together must not walk away with two different customers.
            settled = self._store.session(session_id)
            if settled is not None:
                return settled
            fixture = self._choose(fixtures)
            return self._store.bind(
                VisitorSession(
                    session_id=session_id,
                    demo_id=fixture.demo_id,
                    persona_id=fixture.persona_id,
                    label=fixture.label,
                    display_name=cleaned,
                    thread_id=None,
                    created_at=now,
                    last_seen=now,
                    fixture=fixture,
                )
            )

    def switch(
        self,
        old_session_id: str,
        new_session_id: str,
        *,
        display_name: str | None = None,
    ) -> VisitorSession | None:
        """Release one session's binding and assign the next one a *different* archetype.

        Issue #69's last scope bullet is the whole of this method: *a switch is a
        new* ``demo_id`` *on a clean connection, not a mutation of the existing
        one.* So the old binding is dropped from the store before the new one is
        made, which is what makes the release real -- the pool resolves
        identities by asking the store, and a session the store has forgotten
        checks out nothing at all.

        Note what is **not** a parameter. The caller does not say who the visitor
        should become next, and it does not say who they were: the archetype to
        move away from is read out of the store, under the same lock that makes
        the choice. Two session ids in, one binding out, and no way for a
        request body or a tool result to steer either.

        Args:
            old_session_id: The conversation being left. Its binding is
                released whether or not a new one can be made.
            new_session_id: The conversation being started. Minted by the app,
                on the same terms as any other session id.
            display_name: What the visitor would like to be called. ``None``
                carries the leaving session's name across, which is what a
                switcher on the chat surface wants: the visitor is changing who
                they are shopping as, not who they are.

        Returns:
            The new binding, or ``None`` when the roster is empty -- the same
            decided state :meth:`admit` returns it for.
        """
        now = self._clock.now()
        with self._lock:
            leaving = self._store.session(old_session_id)
            cleaned = clean_display_name(display_name) or (
                leaving.display_name if leaving is not None else None
            )
            self._store.release(old_session_id)
            fixtures = self._roster.fixtures()
            if not fixtures:
                return None
            avoid = frozenset() if leaving is None else frozenset({leaving.persona_id})
            fixture = self._choose(fixtures, avoid_personas=avoid)
            return self._store.bind(
                VisitorSession(
                    session_id=new_session_id,
                    demo_id=fixture.demo_id,
                    persona_id=fixture.persona_id,
                    label=fixture.label,
                    display_name=cleaned,
                    thread_id=None,
                    created_at=now,
                    last_seen=now,
                    fixture=fixture,
                )
            )

    def _choose(
        self,
        fixtures: Sequence[PersonaFixture],
        *,
        avoid_personas: frozenset[str] = frozenset(),
    ) -> PersonaFixture:
        """Pick the fixture that collides with the fewest live sessions.

        Called with :attr:`_lock` held. Three tiers, best first: an archetype
        nobody holds, a customer nobody holds, and -- once the roster is
        genuinely exhausted -- the customer whose holder has been idle longest,
        because reusing an account somebody is mid-conversation with is worse
        than reusing one nobody has touched for an hour.

        Args:
            fixtures: The assignable customers.
            avoid_personas: Archetypes to treat as already held even when no
                live session holds them. :meth:`switch` passes the one the
                visitor is leaving, because *"the point of switching is to see
                the Lapsed Customer after the Regular"* and a switch that
                re-rolled the same archetype would be a reshuffle. It is a
                preference and not a rule: if avoiding it would leave nothing to
                assign, the tiers below fall through to the ordinary choice
                rather than returning nobody.
        """
        live = self._store.sessions()
        held_visitors = {session.demo_id for session in live}
        held_personas = {session.persona_id for session in live} | avoid_personas
        fresh_persona = [
            fixture
            for fixture in fixtures
            if fixture.persona_id not in held_personas
            and fixture.demo_id not in held_visitors
        ]
        if fresh_persona:
            return self._best_of(fresh_persona)
        fresh_visitor = [
            fixture for fixture in fixtures if fixture.demo_id not in held_visitors
        ]
        if fresh_visitor:
            return self._best_of(fresh_visitor)
        idle_first = {session.demo_id: index for index, session in enumerate(live)}
        return min(
            fixtures,
            key=lambda fixture: (idle_first.get(fixture.demo_id, -1), fixture.rank),
        )

    def _best_of(self, candidates: Sequence[PersonaFixture]) -> PersonaFixture:
        """Return a strongest-ranked candidate, breaking ties at random.

        Rank one is the archetype's strongest exemplar, so a demo should hand
        those out first. The random tie-break is why two containers behind one
        roster do not both start at the top of the same list.
        """
        best = min(fixture.rank for fixture in candidates)
        return self._random.choice(
            [fixture for fixture in candidates if fixture.rank == best]
        )


def clean_display_name(value: str | None) -> str | None:
    """Return a display name fit to keep, or ``None``.

    Control characters go, whitespace collapses, and the result is bounded by
    :data:`MAX_DISPLAY_NAME_CHARS`. This is not an escaping function -- the page
    escapes what it renders -- it is the bound on a piece of visitor text the
    app writes to a file and carries for the life of a session.

    Args:
        value: What the entry form supplied.

    Returns:
        The cleaned name, or ``None`` if nothing survived.
    """
    if value is None:
        return None
    collapsed = " ".join(_CONTROL_CHARACTERS.sub(" ", value).split())
    if not collapsed:
        return None
    return collapsed[:MAX_DISPLAY_NAME_CHARS]


def _text(value: object) -> str | None:
    """Return a non-empty string, or ``None``. Snowflake nulls arrive as ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _whole(value: object) -> int | None:
    """Return an integer, or ``None`` for a null or an unparseable number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None


def _number(value: object) -> float | None:
    """Return a float, or ``None`` for a null or an unparseable number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _instant(value: object) -> datetime:
    """Parse a journalled timestamp into an aware UTC datetime.

    Raises:
        ValueError: If it will not parse. The caller skips the line.
    """
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
