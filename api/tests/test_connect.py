"""The factory, tested without an account, a key or a driver on the network.

``cc-lpy4`` is a credential change, and a credential change is the kind that
gets verified once by hand against a live account and then never again. These
tests hold the four things about it that a live check would not catch a
regression in, because a live check would still pass while any of them silently
changed shape:

**The two driver settings the pool depends on.** ``pool.py`` binds with
``SET DEMO_ID = ?``. The connector's default paramstyle is ``pyformat``, under
which ``?`` is not a placeholder -- so ``paramstyle="qmark"`` in the connect
arguments is not tuning, it is the difference between a bound identity and a
syntax error on the one statement that makes a row access policy true.

**That the settings never carry the key.** A dataclass that ended up holding PEM
would put it in every log line, every ``repr`` and every pytest failure output,
and nothing would notice until one of those was pasted somewhere.

**That an unconfigured deployment gets ``None`` rather than an exception.**
``docs/decisions/shipped-persona-roster.md`` is written on the promise that a
deployment with no Snowflake credential still assigns a populated persona. A
factory that raised would turn that into a container that will not start.

**That the adapter is the protocol the pool takes**, checked by running a real
pool over it with a fake driver underneath -- structurally, and not by asserting
that somebody wrote ``SessionConnection`` in an annotation.

Nothing here opens a socket, reads a vault or wants a key: ``make ci`` is a gate
that must not need a logged-in human.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from chip_chat.api.connect import (
    ACCOUNT_VARIABLE,
    APP_USER,
    CONNECT_SETTINGS,
    DEFAULT_SCHEMA,
    KEY_PATH_VARIABLE,
    KEY_VARIABLE,
    KEY_VAULT_VARIABLE,
    READ_ROLE,
    ConnectorConnection,
    PrivateKey,
    SnowflakeSettings,
    snowflake_connect,
)
from chip_chat.api.pool import SessionConnection, VisitorPool
from chip_chat.api.visitors import VisitorSession, VisitorSessionStore

ACCOUNT = "hq72718.us-east-2.aws"
VISITOR = "demo-0001"
SESSION = "sess-connect"


class FakeCursor:
    """A driver cursor. Holds no state of its own: the session variable is the
    session's, which is the whole reason a connection can be dirty and a cursor
    cannot."""

    def __init__(self, connection: "FakeDriverConnection") -> None:
        self._connection = connection
        self._rows: Sequence[Sequence[object]] = ()
        self.closed = False

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_: object) -> None:
        self.closed = True

    def execute(self, sql: str, parameters: object = None) -> None:
        self._rows = self._connection.run(sql, parameters)

    def fetchall(self) -> Sequence[Sequence[object]]:
        return self._rows


class FakeDriverConnection:
    """The object ``snowflake.connector.connect`` returns, as far as this matters.

    It models the one behaviour the pool's guarantee is built on: ``SET`` puts a
    value on the *session*, ``GETVARIABLE`` reads it back off the session, and
    ``UNSET`` clears it. A fake that always answered ``NULL`` would let a broken
    adapter pass, because the readback is what the checkout is made of.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.cursors: list[FakeCursor] = []
        self.closed = False
        self.variable: str | None = None

    def cursor(self) -> FakeCursor:
        made = FakeCursor(self)
        self.cursors.append(made)
        return made

    def run(self, sql: str, parameters: object) -> Sequence[Sequence[object]]:
        self.calls.append((sql, parameters))
        if sql.startswith("SET DEMO_ID"):
            bound = parameters if isinstance(parameters, tuple) else ()
            self.variable = str(bound[0]) if bound else None
            return (("ok",),)
        if sql.startswith("UNSET DEMO_ID"):
            self.variable = None
            return (("ok",),)
        if "GETVARIABLE" in sql:
            return ((self.variable,),)
        return (("ok",),)

    def close(self) -> None:
        self.closed = True


def test_the_pool_binds_with_a_question_mark_so_the_driver_must_take_one() -> None:
    """``paramstyle="qmark"`` or the bind is a syntax error. See the docstring."""
    assert CONNECT_SETTINGS["paramstyle"] == "qmark"


def test_no_heartbeat_thread_survives_a_pooled_connection() -> None:
    """The pool heals a connection it cannot read; a keep-alive is a thread instead."""
    assert CONNECT_SETTINGS["client_session_keep_alive"] is False


def test_the_settings_default_to_the_read_user_and_the_read_role() -> None:
    """A chat app that came up on the write role would be a silent privilege gain."""
    settings = SnowflakeSettings.from_env({ACCOUNT_VARIABLE: ACCOUNT})

    assert settings is not None
    assert (settings.user, settings.role) == (APP_USER, READ_ROLE)
    assert settings.role == "CHIP_CHAT_READ"
    assert settings.schema == DEFAULT_SCHEMA


def test_the_settings_never_carry_key_material() -> None:
    """Not in a field, not in a repr, not in a pytest failure somebody pastes."""
    settings = SnowflakeSettings.from_env({ACCOUNT_VARIABLE: ACCOUNT})

    assert settings is not None
    assert "PRIVATE KEY" not in repr(settings)
    assert "private_key" not in repr(settings)


def test_the_connect_arguments_carry_the_key_and_the_two_driver_settings() -> None:
    """The one place the material appears, assembled per connection."""
    settings = SnowflakeSettings.from_env({ACCOUNT_VARIABLE: ACCOUNT})

    assert settings is not None
    arguments = settings.as_connect_arguments(b"der")

    assert arguments["private_key"] == b"der"
    assert arguments["paramstyle"] == "qmark"
    assert arguments["role"] == READ_ROLE


def test_a_deployment_with_no_account_gets_no_factory_and_no_exception() -> None:
    """The shipped-roster path. A factory that raised would be a dead container."""
    assert SnowflakeSettings.from_env({}) is None
    assert snowflake_connect({}) is None


def test_an_account_with_no_key_anywhere_is_still_not_an_exception() -> None:
    """Half-configured is the state a misedited deployment is actually in."""
    assert snowflake_connect({ACCOUNT_VARIABLE: ACCOUNT}) is None


@pytest.mark.parametrize(
    "variable", [KEY_VARIABLE, KEY_PATH_VARIABLE, KEY_VAULT_VARIABLE]
)
def test_any_one_of_the_three_key_sources_is_enough_to_be_configured(
    variable: str,
) -> None:
    """`configured` answers from names alone: no file read, no vault, no network."""
    assert PrivateKey({variable: "something"}).configured()


def test_being_configured_is_not_the_same_as_having_read_anything() -> None:
    """§3.11's lesson: a vault read on the start-up path is a probe that times out.

    The key is named here and unreadable; ``configured`` must still answer
    without going and finding out, because the answer decides whether the app
    builds a pool and that decision happens before the liveness probe.
    """
    key = PrivateKey({KEY_PATH_VARIABLE: "/nonexistent/nothing.p8"})

    assert key.configured()
    with pytest.raises(FileNotFoundError):
        key.der()


def test_the_key_error_names_the_sources_and_not_what_they_returned() -> None:
    """A traceback carrying half a private key is a disclosure with a stack trace."""
    with pytest.raises(RuntimeError) as raised:
        PrivateKey({}).der()

    assert KEY_VARIABLE in str(raised.value)
    assert KEY_VAULT_VARIABLE in str(raised.value)


def test_the_adapter_runs_a_statement_and_returns_its_rows() -> None:
    """``cursor.execute(sql, parameters)`` then ``cursor.fetchall()``, and no more."""
    driver = FakeDriverConnection()
    connection = ConnectorConnection(driver)

    rows = connection.execute("SET DEMO_ID = ?", (VISITOR,))

    assert rows == (("ok",),)
    assert driver.calls == [("SET DEMO_ID = ?", (VISITOR,))]
    assert all(cursor.closed for cursor in driver.cursors)


def test_a_statement_with_no_placeholders_binds_nothing() -> None:
    """``()`` and ``None`` are different code paths in the driver; take the second."""
    driver = FakeDriverConnection()

    ConnectorConnection(driver).execute("SELECT GETVARIABLE('DEMO_ID')")

    assert driver.calls == [("SELECT GETVARIABLE('DEMO_ID')", None)]


def test_closing_a_connection_never_raises() -> None:
    """The release path closes connections it is discarding *because* of a fault."""

    class Refuses:
        def cursor(self) -> Any:  # pragma: no cover - never reached
            raise AssertionError("not used")

        def close(self) -> None:
            raise RuntimeError("the socket is already gone")

    ConnectorConnection(Refuses()).close()


def test_the_adapter_is_the_protocol_a_real_pool_checks_out() -> None:
    """Structurally, by running one. mypy holds the other half through the binding."""
    driver = FakeDriverConnection()
    adapter: SessionConnection = ConnectorConnection(driver)
    sessions = VisitorSessionStore()
    moment = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    sessions.bind(
        VisitorSession(
            session_id=SESSION,
            demo_id=VISITOR,
            persona_id="regular",
            label="The Regular",
            display_name=None,
            thread_id=None,
            created_at=moment,
            last_seen=moment,
        )
    )
    pool = VisitorPool(lambda: adapter, sessions=sessions, size=1)

    with pool.for_session(SESSION) as connection:
        connection.execute("SELECT 1")

    statements = [sql for sql, _ in driver.calls]

    # The checkout's four round trips, in the order `pool.py` argues for: read
    # back before binding, bind, read back after binding, clear on release.
    assert statements[0] == "SELECT GETVARIABLE('DEMO_ID')"
    assert statements[1] == "SET DEMO_ID = ?"
    assert ("SET DEMO_ID = ?", (VISITOR,)) in driver.calls
    assert statements[-1] == "UNSET DEMO_ID"


def test_no_signature_in_this_module_accepts_a_visitor_identifier() -> None:
    """The invariant, checked where a credential change would be tempted to break it.

    A factory is exactly the place somebody would add ``demo_id=`` to "make the
    connection know who it is for". The pool binds it; this module opens a
    connection bound to nobody, and there is no argument here to put one in.
    """
    import inspect

    from chip_chat.api import connect

    forbidden = {"demo_id", "visitor", "visitor_id", "customer_id", "persona_id"}
    for name, value in vars(connect).items():
        if name.startswith("_") or not callable(value):
            continue
        try:
            signature = inspect.signature(value)
        except (TypeError, ValueError):  # pragma: no cover - builtins
            continue
        assert not forbidden & set(signature.parameters), name
