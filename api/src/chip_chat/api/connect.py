"""The connection factory, and the one argument in this repository it reverses.

``pool.py`` describes the object it wants and then says why nothing in the tree
provides one:

    A protocol rather than the Snowflake driver, for the reason
    :mod:`chip_chat.snowflake.snow` gives for shelling out to the CLI: the driver
    is not in this lockfile, the connection is already described once in
    ``~/.snowflake/config.toml``, and a second place that knows how to
    authenticate is a second thing to rotate a key in.

    [...] An adapter over ``snowflake.connector`` is a handful of lines:
    ``cursor.execute(sql, parameters)`` then ``cursor.fetchall()``.

This module is that handful of lines, and the paragraph above it is now half
wrong on purpose. **The container has no ``~/.snowflake/config.toml`` and no
``snow`` on its PATH.** A file on a developer's laptop is a fine place to
describe a connection for a developer's laptop; it is not a place a Container
App can read, and the argument for not putting the driver in the lockfile was an
argument about a workspace in which *nothing imported it*. `api/pyproject.toml`
carries the reversal in as many words. The rest of it stands: this is the only
module in ``api/`` that knows how to authenticate, and the key it presents is
described in exactly one place per tier.

WHAT WAS ACTUALLY BROKEN, AND WHY THIS FILE IS THE FIX FOR IT. On 27 August 2026
the deployed app opened every conversation with a sentence read off the persona
it had assigned -- *"397 points on the card"* -- and then answered *"how many
points do I have?"* with ``1,340``, off
:data:`chip_chat.agent.hardcoded.ACCOUNT`. Both halves were correct for a
deployment with no account lane and the pair of them was indefensible.
``docs/public-demo.md`` §9 has the transcript. There was one cause and it was
this file's absence: ``build_service(lanes=NO_LANES, connect=None)`` is a roster
read from a shipped JSON export and four tools reading a fixture, and no amount
of care in either half makes them agree. A connection factory closes both at
once, which is why it was the highest-leverage change left.

THE FOUR THINGS THAT HAVE TO BE TRUE HERE AND ARE NOT OBVIOUS:

**1. The driver wants DER, not PEM.** ``snowflake.connector.connect`` documents a
``private_key`` argument and rejects a PEM string with *"Please provide a valid
RSA or ECDSA private key in DER format as bytes object"* -- it does not sniff the
encoding. Key Vault holds PKCS#8 PEM, because that is what an operator can paste
and what ``openssl`` prints, so :func:`_der` does the one conversion. Getting
this wrong is a ``ProgrammingError`` at the first checkout and nowhere earlier.

**2. The driver's default paramstyle is not the pool's.** ``pool.py`` spells its
bind as ``SET DEMO_ID = ?``, and the connector defaults to ``pyformat``, under
which ``?`` is not a placeholder at all -- it is a syntax error, on the one
statement in the system that makes a row access policy true. So
:data:`CONNECT_SETTINGS` passes ``paramstyle="qmark"`` per connection rather than
setting the module-level global, and ``SET`` really does take a bound parameter:
verified against the live account on 27 August 2026, which is worth saying
because ``api/functions/function_app.py`` states the opposite and interpolates.

**3. The role is the read role, and it is read off the declaration.**
:data:`APP_USER` is spelled here once; :data:`READ_ROLE` and
:data:`SERVING_WAREHOUSE` are taken from
:data:`chip_chat.snowflake.account.USERS`, so a deployment cannot come up on the
chat app's user holding the ops API's role because somebody edited one string.
``CHIP_CHAT_READ`` is refused an ``INSERT`` by the account itself; this is the
tier that makes sure nothing even asks.

**4. Nothing here touches the network at start-up.** ``docs/deployment.md``
§3.11 is a write-up of a deployment that spent thirty-five seconds looking
healthy and then stopped answering ``/healthz``, because assembling the photo
intake constructed two Azure SDK clients on the start-up path. The same trap is
one line away here: a Key Vault read to fetch a private key is exactly that kind
of client. So :func:`snowflake_connect` decides whether this deployment *has* a
credential by reading environment variables and nothing else, and the key
material is fetched on the first connection and memoised. A deployment with a
misconfigured vault fails on its first Snowflake checkout, which is a lane that
declines -- not a liveness probe that times out.

WHERE THE KEY COMES FROM, IN PRECEDENCE ORDER. Three sources, and the first is
the one production takes:

``SNOWFLAKE_PRIVATE_KEY``
    The PEM itself, in the environment. On the Container App this is a Container
    Apps *secret* whose value is a Key Vault reference resolved by the platform
    using the app's managed identity (``infra/terraform/compute.tf``), so the key
    does reach the process from Key Vault -- the platform makes the call, before
    the process exists, and the app pays nothing for it at start-up.

``SNOWFLAKE_PRIVATE_KEY_PATH``
    A file. For a developer running the app against the real account, and for
    the unencrypted PKCS#8 copies ``snow`` wants for ``--private-key-file``.
    Never a value that exists in a deployment: the container has no filesystem
    worth writing a key to.

``AZURE_KEY_VAULT_URI`` + ``SNOWFLAKE_PRIVATE_KEY_SECRET``
    The vault, read directly with :class:`~azure.identity.DefaultAzureCredential`
    -- ``az login`` locally, the managed identity in Azure. This is the path
    ``.env.example`` describes for every other secret in the system (*"secrets
    live in Key Vault and are read at runtime over the credential ``az login``
    writes"*), and it is third rather than first only because the two above are
    cheaper, not because it is less correct.

**None of the three is a failure.** :func:`snowflake_connect` returns ``None``,
``build_visitors`` takes the ``connect is None`` path, and the deployment runs on
the shipped roster with the hardcoded account exactly as
``docs/decisions/shipped-persona-roster.md`` describes. That degradation is the
whole reason this function returns an optional rather than raising: a demo
without Snowflake credentials should be a demo with a fixture in it, not a
container that will not start.

AND THE KEY IS NEVER LOGGED, PRINTED OR REPR'D. :class:`PrivateKey` holds bytes
behind a method and defines no ``__repr__`` that could reach them, the settings
dataclass does not carry the material at all, and every failure message here
names the *source* that was tried rather than what it returned.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from chip_chat.api.pool import SessionConnection
from chip_chat.snowflake.account import DATABASE, USERS

__all__ = [
    "ACCOUNT_VARIABLE",
    "APP_USER",
    "CONNECT_LOGGER",
    "CONNECT_SETTINGS",
    "DEFAULT_KEY_SECRET",
    "DEFAULT_SCHEMA",
    "KEY_PATH_VARIABLE",
    "KEY_SECRET_VARIABLE",
    "KEY_VARIABLE",
    "KEY_VAULT_VARIABLE",
    "READ_ROLE",
    "SERVING_WAREHOUSE",
    "ConnectorConnection",
    "KeyPairJwt",
    "PrivateKey",
    "SnowflakeSettings",
    "snowflake_connect",
]

CONNECT_LOGGER: Final = logging.getLogger("chip_chat.api.connect")
"""Where a deployment says which credential path it took, or that it took none.

At ``INFO`` on the way up and ``WARNING`` where a source was configured and did
not work. An operator reading the container's first ten lines should be able to
tell a deployment running on the live account from one running on the shipped
roster without asking, which is the same promise ``shipped_roster()`` makes.
"""

_APP: Final = next(user for user in USERS if user.name == "CHIP_CHAT_APP")
"""The chat app's service identity, as ``snowflake/sql/04_users.sql`` declares it.

Looked up rather than transcribed. The name is spelled once, above, and the two
things that decide what a connection may *do* -- the role and the warehouse --
come off :data:`chip_chat.snowflake.account.USERS`. A rename there is a
``StopIteration`` at import time, which is a loud failure at the only moment one
is cheap; three transcribed strings that quietly drifted would be a chat app
running on the write role and nothing saying so.
"""

APP_USER: Final = _APP.name
"""``CHIP_CHAT_APP``. Reads, and cannot write: see :data:`READ_ROLE`."""

READ_ROLE: Final = _APP.role
"""``CHIP_CHAT_READ``. Named explicitly rather than left to the user's
``DEFAULT_ROLE``, for the reason ``api/functions/function_app.py`` names the
write role explicitly: what a tier runs as should be a fact in the tier's own
source rather than a property of an account somebody may edit."""

SERVING_WAREHOUSE: Final = _APP.warehouse
"""``CHIP_CHAT_SERVING_WH``. X-Small, suspends after sixty seconds."""

ACCOUNT_VARIABLE: Final = "SNOWFLAKE_ACCOUNT"
"""The account locator, e.g. ``hq72718.us-east-2.aws``.

The one setting with no defensible default. Everything else here defaults to
what ``snowflake/sql/`` creates; an account identifier guessed in code and wrong
is a lane that fails on every turn with a DNS error, which is the argument
:func:`chip_chat.snowflake.cortex.host_from_env` already makes for its host.
"""

KEY_VARIABLE: Final = "SNOWFLAKE_PRIVATE_KEY"
"""The PEM itself. A Container Apps secret sourced from Key Vault in a
deployment; the same name the Functions host reads, carrying that tier's own
key. One process, one key, and never the other tier's."""

KEY_PATH_VARIABLE: Final = "SNOWFLAKE_PRIVATE_KEY_PATH"
"""A path to the PEM. Development only -- see the module docstring."""

KEY_SECRET_VARIABLE: Final = "SNOWFLAKE_PRIVATE_KEY_SECRET"
"""Which secret in the vault holds it. Defaults to :data:`DEFAULT_KEY_SECRET`."""

KEY_VAULT_VARIABLE: Final = "AZURE_KEY_VAULT_URI"
"""The vault. Already set on the Container App and on the Functions app by
``infra/terraform/compute.tf``, and already in ``.env.example``."""

DEFAULT_KEY_SECRET: Final = "snowflake-app-private-key"
"""The app user's key. ``snowflake-ops-private-key`` is the other one and belongs
to the write tier; a chat app that read it would be a chat app holding a
credential it has no route to use and every reason not to hold."""

DEFAULT_SCHEMA: Final = "ACCOUNTS"
"""The schema a connection lands in.

Load-bearing for exactly one statement. Every read in ``snowflake/reads.py`` is
fully qualified; ``visitors.py``'s roster query says ``FROM persona_fixtures``,
because it is the one read that happens before there is a visitor and it is
written against #43's ``entry_roster`` policy rather than against a mart.
"""

# The five overrides, for the four names that have a default and the user that
# has one too. Spelled the way the ops host's `SNOWFLAKE_OPS_USER` /
# `SNOWFLAKE_WRITE_ROLE` pair already is, so a reader who has seen one tier's
# settings can read the other's without a translation table.
USER_VARIABLE: Final = "SNOWFLAKE_APP_USER"
ROLE_VARIABLE: Final = "SNOWFLAKE_READ_ROLE"
WAREHOUSE_VARIABLE: Final = "SNOWFLAKE_WAREHOUSE"
DATABASE_VARIABLE: Final = "SNOWFLAKE_DATABASE"
SCHEMA_VARIABLE: Final = "SNOWFLAKE_SCHEMA"

CONNECT_SETTINGS: Final[Mapping[str, Any]] = {
    # `?`, because that is what `pool.py` spells and the connector's default
    # `pyformat` would make a syntax error of it. Per connection rather than
    # `snowflake.connector.paramstyle = "qmark"`, which is a module global and
    # would reach into any other consumer of the driver in the same process.
    "paramstyle": "qmark",
    # Reads only. An implicit transaction left open on a pooled connection is a
    # lock held across visitors, which is a different way to make one visitor's
    # request depend on another's.
    "autocommit": True,
    # A connection that cannot be established has to fail inside a turn's
    # budget rather than inside the ingress timeout. Fifteen seconds is the same
    # ceiling `chip_chat.snowflake.analyst.DEFAULT_TIMEOUT_SECONDS` puts on the
    # Analyst hop, and both are comfortably under the serving warehouse's
    # sixty-second statement timeout.
    "login_timeout": 15,
    "network_timeout": 30,
    # No heartbeat thread. The pool already destroys and replaces a connection
    # that cannot answer `SELECT GETVARIABLE('DEMO_ID')`, so a keep-alive buys a
    # saved reconnect and costs a background thread per pooled connection in a
    # container that scales to zero. Healing is cheaper than remembering here,
    # which is the same trade `VisitorPool._acquire` makes for staleness.
    "client_session_keep_alive": False,
}
"""Everything handed to ``snowflake.connector.connect`` that is not an identity.

Spelled as data rather than as keyword arguments so that
``api/tests/test_connect.py`` can assert on the two that are load-bearing --
``paramstyle`` and the absence of a keep-alive -- without opening a connection.
"""


@dataclass(frozen=True, slots=True)
class SnowflakeSettings:
    """Who this process connects as, and against what. No key material.

    Deliberately not the mapping handed to the driver: the key is fetched
    lazily and lives in :class:`PrivateKey`, so this object can be logged,
    compared in a test and put in a repr without anybody having to check first.

    Attributes:
        account: The account locator, from :data:`ACCOUNT_VARIABLE`.
        user: The service user. :data:`APP_USER` unless overridden.
        role: The role, which is a read role. :data:`READ_ROLE` unless
            overridden -- and an override is how an operator would point a
            deployment at a *narrower* role, never a wider one.
        warehouse: The serving warehouse.
        database: ``CHIP_CHAT``.
        schema: ``ACCOUNTS``, which is what makes ``visitors.py``'s unqualified
            ``FROM persona_fixtures`` resolve. Every other statement in the
            system is fully qualified; the roster query is the exception, and it
            is the one read that happens before a visitor exists.
    """

    account: str
    user: str = APP_USER
    role: str = READ_ROLE
    warehouse: str = SERVING_WAREHOUSE
    database: str = DATABASE
    schema: str = DEFAULT_SCHEMA

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SnowflakeSettings | None:
        """Read the settings, or return ``None`` if this deployment has none.

        Reads environment variables and nothing else. No file, no vault, no
        network -- see the module docstring's fourth point.

        Args:
            env: The environment. Defaults to :data:`os.environ`.

        Returns:
            The settings, or ``None`` where :data:`ACCOUNT_VARIABLE` is unset.
            ``None`` is the shipped-roster deployment and is not an error.
        """
        source = os.environ if env is None else env
        locator = source.get(ACCOUNT_VARIABLE, "").strip()
        if not locator:
            return None
        return cls(
            account=locator,
            user=source.get(USER_VARIABLE, "").strip() or APP_USER,
            role=source.get(ROLE_VARIABLE, "").strip() or READ_ROLE,
            warehouse=source.get(WAREHOUSE_VARIABLE, "").strip() or SERVING_WAREHOUSE,
            database=source.get(DATABASE_VARIABLE, "").strip() or DATABASE,
            schema=source.get(SCHEMA_VARIABLE, "").strip() or DEFAULT_SCHEMA,
        )

    def as_connect_arguments(self, key: bytes) -> dict[str, Any]:
        """Assemble the keyword arguments ``snowflake.connector.connect`` takes.

        Args:
            key: The private key, DER-encoded. See :meth:`PrivateKey.der`.

        Returns:
            The full argument mapping, key material included. Not logged, not
            stored, and built per connection.
        """
        return {
            "account": self.account,
            "user": self.user,
            "role": self.role,
            "warehouse": self.warehouse,
            "database": self.database,
            "schema": self.schema,
            "private_key": key,
            **CONNECT_SETTINGS,
        }


class PrivateKey:
    """The app's private key, fetched once and held as DER.

    Three sources in precedence order, resolved on first use rather than at
    construction, because construction happens on the start-up path and a Key
    Vault read is the exact shape of client ``docs/deployment.md`` §3.11 is a
    write-up of. Thread-safe: the pool opens connections from whichever request
    thread first needs one, and two threads racing to read a vault is two vault
    reads and, worse, two different answers if a rotation lands between them.
    """

    __slots__ = ("_der", "_env", "_lock")

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        """Initialise the source.

        Args:
            env: The environment. Defaults to :data:`os.environ`, read at every
                resolution rather than captured, so a restart is not required to
                notice a setting that was added.
        """
        self._env = env
        self._lock = threading.Lock()
        self._der: bytes | None = None

    @property
    def _environment(self) -> Mapping[str, str]:
        return os.environ if self._env is None else self._env

    def configured(self) -> bool:
        """Whether any of the three sources is *named*, without reading any of them.

        The question :func:`snowflake_connect` asks at start-up. It is
        deliberately not "does the key work" -- answering that here would put a
        vault read, or a file read, on the path a liveness probe waits behind.

        Returns:
            ``True`` if a key could plausibly be fetched later.
        """
        env = self._environment
        if env.get(KEY_VARIABLE, "").strip():
            return True
        if env.get(KEY_PATH_VARIABLE, "").strip():
            return True
        return bool(env.get(KEY_VAULT_VARIABLE, "").strip())

    def der(self) -> bytes:
        """Return the key, DER-encoded PKCS#8, fetching it the first time.

        Returns:
            The key material the driver takes.

        Raises:
            RuntimeError: If no source produced a key, naming the sources that
                were tried and never what any of them returned.
        """
        with self._lock:
            if self._der is None:
                self._der = _der(self._pem())
            return self._der

    def _pem(self) -> str:
        """Fetch the PEM from the first source that has one."""
        env = self._environment
        inline = env.get(KEY_VARIABLE, "").strip()
        if inline:
            CONNECT_LOGGER.info("Snowflake private key read from %s", KEY_VARIABLE)
            return inline
        path = env.get(KEY_PATH_VARIABLE, "").strip()
        if path:
            CONNECT_LOGGER.info("Snowflake private key read from %s", path)
            with open(path, encoding="utf-8") as handle:
                return handle.read()
        vault = env.get(KEY_VAULT_VARIABLE, "").strip()
        if vault:
            name = env.get(KEY_SECRET_VARIABLE, "").strip() or DEFAULT_KEY_SECRET
            CONNECT_LOGGER.info(
                "Snowflake private key read from %s, secret %s", vault, name
            )
            return _from_key_vault(vault, name)
        raise RuntimeError(
            "no Snowflake private key: none of "
            f"{KEY_VARIABLE}, {KEY_PATH_VARIABLE} or {KEY_VAULT_VARIABLE} is set"
        )


class ConnectorConnection:
    """A :class:`~chip_chat.api.pool.SessionConnection` over ``snowflake.connector``.

    The handful of lines ``pool.py`` said it would be, and nothing more. It does
    not know what a ``demo_id`` is, cannot be asked for one, and holds no state
    beyond the driver's connection object -- everything about identity happens
    one layer up, in the checkout, which is where the guarantee is written down.

    A cursor per statement rather than one held open: the pool's own round trips
    interleave with a lane's queries on the same connection, and a cursor that
    outlived a statement would be shared mutable state between them for no gain.
    """

    __slots__ = ("_connection",)

    def __init__(self, connection: Any) -> None:
        """Wrap an open ``snowflake.connector`` connection.

        Args:
            connection: The driver's connection object.
        """
        self._connection = connection

    def execute(
        self, sql: str, parameters: Sequence[object] = ()
    ) -> Sequence[Sequence[object]]:
        """Run one statement and return its rows.

        Args:
            sql: The statement, with ``?`` placeholders where it binds.
            parameters: The bound values, in order.

        Returns:
            The rows, each a sequence of columns. A statement that returns no
            result set still returns the driver's one-row acknowledgement, which
            callers ignore.
        """
        bound = tuple(parameters)
        with self._connection.cursor() as cursor:
            # `None` rather than `()`: the driver treats an empty sequence as
            # "bind these", and a `SELECT` with no placeholders and an empty
            # binding list is a different code path with nothing to gain from.
            cursor.execute(sql, bound if bound else None)
            rows: Sequence[Sequence[object]] = cursor.fetchall()
        return rows

    def close(self) -> None:
        """Close the underlying session, and never raise doing it.

        Called by :func:`chip_chat.api.pool._quietly_close` on every discarded
        connection, including the ones being discarded *because* something about
        them was wrong. A close that raised there would replace a handled
        finding with an unhandled exception on the release path.
        """
        try:
            self._connection.close()
        except Exception:  # pragma: no cover - the driver is written not to
            CONNECT_LOGGER.debug("closing a Snowflake connection failed", exc_info=True)


class KeyPairJwt:
    """A :class:`~chip_chat.snowflake.cortex.TokenSource` minted from the same key.

    ``cortex.py``'s :class:`~chip_chat.snowflake.cortex.CliJwt` shells out to
    ``snow connection generate-jwt`` and says why: the account, the user and the
    private key are described once in ``~/.snowflake/config.toml``. The container
    has neither that file nor the CLI, and ``cortex.py`` anticipated exactly
    this -- *"a deployment that cannot ship the CLI supplies a different
    ``TokenSource``; that is what the protocol is for"*.

    It lives here rather than beside :class:`CliJwt` on purpose. ``snowflake/``
    has no driver in its dependencies and holds no credentials; ``api/`` is the
    tier that has both. Putting the key-pair signer next to the key keeps that
    line where it is, and satisfies the protocol structurally -- there is no
    import in either direction.

    Tokens are cached for ``lifetime_seconds``, for the reason :class:`CliJwt`
    caches: a signature per turn on a lane already spending three seconds of
    inference is a cost with nothing to show for it.
    """

    __slots__ = ("_key", "_lifetime", "_settings")

    def __init__(
        self,
        settings: SnowflakeSettings,
        key: PrivateKey,
        *,
        lifetime_seconds: int = 240,
    ) -> None:
        """Initialise the token source.

        Args:
            settings: Who the token is minted for.
            key: The signing key, resolved lazily.
            lifetime_seconds: How long a minted token is valid for. Well under
                Snowflake's own hour, because a token this process is still
                holding when it expires is a 401 in the middle of a turn.
        """
        self._settings = settings
        self._key = key
        self._lifetime = lifetime_seconds

    @property
    def token_type(self) -> str:
        """``KEYPAIR_JWT``, which is what this signs."""
        return "KEYPAIR_JWT"

    def token(self) -> str:
        """Return a bearer token for the Snowflake REST API.

        Returns:
            The JWT.

        Raises:
            AnalystError: If the key could not be fetched or the token could not
                be signed. Raised as the lane's own exception type so that
                ``ask_account_question`` declines rather than the turn failing --
                the same contract :meth:`CliJwt.token` keeps.
        """
        from chip_chat.snowflake.cortex import AnalystError

        try:
            # Imported here rather than at module scope so that importing this
            # module -- which `make ci` does -- costs nothing.
            from snowflake.connector.auth import AuthByKeyPair

            signer = AuthByKeyPair(
                private_key=self._key.der(), lifetime_in_seconds=self._lifetime
            )
            minted: str = signer.prepare(
                account=self._settings.account, user=self._settings.user
            )
            return minted
        except Exception as error:
            raise AnalystError(
                f"could not sign a Snowflake JWT for {self._settings.user}: "
                f"{type(error).__name__}"
            ) from error


def snowflake_connect(
    env: Mapping[str, str] | None = None,
) -> Callable[[], SessionConnection] | None:
    """Return the factory :func:`chip_chat.api.app.build_visitors` takes, or ``None``.

    The seam ``build_service`` has documented and nothing has filled since the
    pool was written. It reads the environment and nothing else: whether this
    deployment *has* a Snowflake credential is a question about configuration,
    and answering it must not cost a network round trip on the start-up path.

    Args:
        env: The environment. Defaults to :data:`os.environ`.

    Returns:
        A callable opening one bound-to-nobody connection per call, or ``None``
        where the account or every key source is unconfigured. ``None`` is the
        shipped-roster deployment ``docs/decisions/shipped-persona-roster.md``
        describes and is not an error -- which is why this returns an optional
        rather than raising.
    """
    settings = SnowflakeSettings.from_env(env)
    if settings is None:
        CONNECT_LOGGER.info(
            "%s is unset, so this deployment has no Snowflake connection: the "
            "shipped persona roster and the hardcoded account fixture are what "
            "a visitor will see",
            ACCOUNT_VARIABLE,
        )
        return None
    key = PrivateKey(env)
    if not key.configured():
        CONNECT_LOGGER.warning(
            "%s names an account but no private key is configured (%s, %s and %s "
            "are all unset), so the shipped persona roster and the hardcoded "
            "account fixture are what a visitor will see",
            ACCOUNT_VARIABLE,
            KEY_VARIABLE,
            KEY_PATH_VARIABLE,
            KEY_VAULT_VARIABLE,
        )
        return None
    CONNECT_LOGGER.info(
        "Snowflake connections will be opened as %s on %s against %s.%s",
        settings.user,
        settings.role,
        settings.database,
        settings.schema,
    )

    def connect() -> SessionConnection:
        # Imported inside the factory rather than at module scope, so that
        # importing `chip_chat.api.app` -- which every test does -- neither
        # imports the driver nor pays for it.
        import snowflake.connector

        return ConnectorConnection(
            snowflake.connector.connect(**settings.as_connect_arguments(key.der()))
        )

    return connect


def _der(pem: str) -> bytes:
    """Convert a PKCS#8 PEM private key to the DER bytes the driver takes.

    Args:
        pem: The key, as Key Vault and ``openssl`` hold it.

    Returns:
        The same key, DER-encoded PKCS#8 and unencrypted.

    Raises:
        RuntimeError: If it could not be parsed. The message says so and nothing
            about the contents -- a traceback carrying half a private key is a
            disclosure with a stack trace attached.
    """
    from cryptography.hazmat.primitives import serialization

    try:
        loaded = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
        return loaded.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    except Exception as error:
        raise RuntimeError(
            "the Snowflake private key could not be read as an unencrypted "
            f"PKCS#8 PEM ({type(error).__name__}); the account's service users "
            "are key-pair authenticated and cannot use a password"
        ) from error


def _from_key_vault(vault_uri: str, secret_name: str) -> str:
    """Read one secret out of Key Vault over the ambient Azure credential.

    ``az login`` on a laptop, the user-assigned managed identity in Azure --
    which already holds **Key Vault Secrets User** on ``kv-chip-chat-c8b63a``,
    granted for the photo lane and reused rather than duplicated here.

    Args:
        vault_uri: ``https://<vault>.vault.azure.net/``.
        secret_name: The secret holding the PEM.

    Returns:
        The secret's value.

    Raises:
        RuntimeError: If the vault could not be read, naming the vault and the
            secret and not what the failure returned.
    """
    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient

        client = SecretClient(vault_url=vault_uri, credential=DefaultAzureCredential())
        value = client.get_secret(secret_name).value
    except Exception as error:
        raise RuntimeError(
            f"could not read {secret_name} from {vault_uri}: {type(error).__name__}"
        ) from error
    if not value:
        raise RuntimeError(f"{secret_name} in {vault_uri} is empty")
    return value
