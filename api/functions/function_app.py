"""The Azure Functions host: four routes, one credential, and no SQL.

RFC-001 section 03 gives the write path its own service, and the sentence that
matters is *the only path that writes*. This file is that service's edge: it
resolves who is asking, rejoins the turn's trace, hands the call to
:class:`chip_chat.api.ops.OpsService`, and turns what comes back into a
response. Every rule the write is held to lives one layer in, in
``chip_chat.api.ops``, where it can be tested without a host.

**What is here that is nowhere else.** :class:`SnowflakeWriteBackend` holds the
only credentials in the system with the Snowflake write role. Nothing in
``api/src`` opens a connection; ``chip_chat.api.ops.WriteBackend`` is a protocol
precisely so that the credential lives at the deployment boundary and a unit
test never needs one.

**Why this file writes no SQL either.** The statement it sends is
``CALL <procedure>(...)``, and which procedure, in what argument order, with
which arguments needing ``PARSE_JSON``, all come from
:data:`chip_chat.snowflake.procedures.PROCEDURES` -- issue #46's declaration of
the write path. Issue #63's scope says *each function calls its corresponding
stored procedure -- no ad-hoc SQL*, and reading the shape off the declaration is
how that stays true when the declaration changes.

**Three things a caller must present**, and the order they are checked in:

1. The ops key. This service is the only path that writes, so an unauthenticated
   caller who finds its hostname would be a caller who can write as anybody.
   Compared with :func:`hmac.compare_digest`, and its absence from the
   environment refuses every request rather than allowing them all.
2. W3C trace context. Gate 2 is auditable *because* every write emits
   ``ops.<action>`` with its confirmation state, and a write with no parent span
   emits that into a trace nobody will find. It is also a second authenticity
   signal: the app always sends these headers, having opened ``tool.<name>``
   around the call.
3. The visitor. :data:`~chip_chat.api.ops.SESSION_HEADER` carries the ``demo_id``
   the app resolved from the session cookie. It is server-to-server, never seen
   by a browser and never by a model, and it is the last place in the write path
   an identifier appears at all.

**A rejection is a 200.** ``sql/12_procedures.sql`` says it in its own header --
*reject, never repair; a rejection is a returned object, ok false, with a code* --
and the ops API keeps that contract at the edge. An unconfirmed draft is not a
malformed request or a server fault; it is the answer. The status codes that are
not 200 are for calls this service will not answer at all (401, 400) and for the
one state RFC-001 section 10 gives copy to (503).

**The seam this file does not close.** :func:`build_ops_service` needs a
catalogue, because the draft store prices against one, and the production
catalogue loader is #66's -- exactly as :func:`chip_chat.api.app.build_service`
records for its photo lane. Until it exists, this host answers 503 with
:data:`~chip_chat.api.ops.OPS_UNAVAILABLE_MESSAGE`, which is the behaviour
RFC-001 section 10 specifies for an ops API that is not there and is therefore
the honest state rather than a hole.

The second half of that seam is topology, and is worth stating plainly: a draft
minted in the chat app's process is held in that process's memory (#62), so an
ops service in a *different* process cannot see it. V0 runs the ops service
behind the app for that reason, and this host is the shape it moves into when
the two ledgers move behind a shared store -- the same honest limitation
:class:`~chip_chat.api.ledger.BudgetLedger` carries, and the same one obvious
place for a shared implementation to land.
"""

import hmac
import json
import logging
import os
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

import azure.functions as func

from chip_chat.api.confirmations import ConfirmationLedger
from chip_chat.api.drafts import DraftStore
from chip_chat.api.ops import (
    OPS_UNAVAILABLE_MESSAGE,
    SESSION_HEADER,
    OpsRejectedError,
    OpsService,
    OpsSession,
    OpsUnavailableError,
    Receipt,
)
from chip_chat.catalog import MenuCatalog
from chip_chat.otel import SpanName, TurnContextError, continue_turn
from chip_chat.snowflake.procedures import IDENTITY_VARIABLE, procedure

_LOG: Final = logging.getLogger(__name__)

OPS_KEY_HEADER: Final = "x-cilantro-ops-key"
"""The shared secret the chat app presents. See rule 1 in the module docstring."""

OPS_KEY_VARIABLE: Final = "CHIP_CHAT_OPS_KEY"
"""Where the secret is read from. Key Vault reference on the Functions app."""

OPS_USER: Final = "CHIP_CHAT_OPS"
"""The Snowflake user this host authenticates as. `sql/04_users.sql` creates it.

Three users exist and they are not interchangeable: ``CHIP_CHAT_APP`` reads,
``CHIP_CHAT_PUBLISHER`` runs the nightly publish, and this one -- and only this
one -- defaults to a role that may write to ``ACCOUNTS``.
"""

WRITE_ROLE: Final = "CHIP_CHAT_WRITE"
"""The role that may change ``CHIP_CHAT.ACCOUNTS``. `sql/00_roles.sql` creates it.

Named rather than left to the user's default role, so that what this host runs as
is a fact in this file rather than a property of an account somebody may edit.
"""

SERVING_WAREHOUSE: Final = "CHIP_CHAT_SERVING_WH"
"""X-Small, suspends after sixty seconds. ``CHIP_CHAT_PUBLISH_WH`` is the other
one, and only the nightly publish may name it."""

_DEMO_ID = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
"""What a ``demo_id`` may be spelled with.

Snowflake's ``SET`` does not take a bound parameter, so the identifier is
interpolated into the statement that binds it -- and an allowlist rather than an
escape is what makes that safe. Anything outside it is refused before a
connection is opened. data-gen's identifiers are of the form ``dm-000123``.
"""

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
"""The Functions app. ``FUNCTION`` auth on top of the key check, not instead."""

_service: OpsService | None = None


# ---------------------------------------------------------------------------
# The write role's connection. The only one in the system.
# ---------------------------------------------------------------------------


class SnowflakeWriteBackend:
    """Sessions on the write role, one per call, with ``DEMO_ID`` bound.

    A session per call rather than a pool. The three things a pooled connection
    saves -- handshake, warehouse resume, authentication -- are worth having on a
    read path serving every turn; this path serves the handful of writes a
    conversation makes, and a pooled connection carrying a *previous* visitor's
    session variable is the one bug this whole design exists to make impossible.

    ``chip_chat.snowflake.snow`` shells out to the ``snow`` CLI and says why: the
    connection is already defined once, in a configuration file on the machine. A
    Function has no such machine. So this reaches for the driver, which is the
    deviation and the reason it lives in ``api/functions/`` rather than in
    ``snowflake/src`` beside its sibling.
    """

    __slots__ = ("_settings",)

    def __init__(self, settings: Mapping[str, str]) -> None:
        """Hold the connection settings the driver takes.

        Args:
            settings: Keyword arguments for ``snowflake.connector.connect``.
        """
        self._settings = dict(settings)

    @classmethod
    def from_env(cls) -> "SnowflakeWriteBackend":
        """Read the connection out of the Functions app's settings.

        The private key arrives as a Key Vault reference, so what is in the
        environment of a running Function is the material itself and never a
        path to it -- there is no file system here worth writing a key to.

        Returns:
            The backend.

        Raises:
            RuntimeError: If a required setting is missing. A write path that
                silently came up without a role is worse than one that refuses
                to come up at all.
        """
        required = ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_PRIVATE_KEY")
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise RuntimeError(
                f"the ops API has no write credentials: {', '.join(missing)} unset"
            )
        return cls(
            {
                "account": os.environ["SNOWFLAKE_ACCOUNT"],
                "user": os.environ.get("SNOWFLAKE_OPS_USER", OPS_USER),
                "private_key": os.environ["SNOWFLAKE_PRIVATE_KEY"],
                "role": os.environ.get("SNOWFLAKE_WRITE_ROLE", WRITE_ROLE),
                "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", SERVING_WAREHOUSE),
                "database": os.environ.get("SNOWFLAKE_DATABASE", "CHIP_CHAT"),
                "schema": os.environ.get("SNOWFLAKE_SCHEMA", "ACCOUNTS"),
            }
        )

    def session(self, demo_id: str) -> "SnowflakeWriteBackend._Session":
        """Open a connection and bind ``DEMO_ID`` on it.

        Args:
            demo_id: The visitor the request resolved to.

        Returns:
            The bound session.

        Raises:
            OpsUnavailableError: If the connection could not be opened or the
                variable could not be set. Either way nothing was written.
            ValueError: If ``demo_id`` is not something :data:`_DEMO_ID` allows.
        """
        if not _DEMO_ID.match(demo_id):
            raise ValueError(f"{demo_id!r} is not a well-formed visitor identifier")
        try:
            # Imported here rather than at module scope: this is the only
            # place in the repository that reaches for the driver.
            import snowflake.connector

            connection = snowflake.connector.connect(**self._settings)
            with connection.cursor() as cursor:
                cursor.execute(f"SET {IDENTITY_VARIABLE} = '{demo_id}'")
        # Any driver error is an outage: what a caller needs to know is that
        # nothing was written, not which exception class said so.
        except Exception as failure:
            raise OpsUnavailableError(f"no write session: {failure}") from failure
        return SnowflakeWriteBackend._Session(connection)

    def available(self) -> bool:
        """Whether a card composed now should say ordering is available."""
        try:
            # Imported here rather than at module scope: this is the only
            # place in the repository that reaches for the driver.
            import snowflake.connector

            connection = snowflake.connector.connect(**self._settings)
        except Exception:
            return False
        connection.close()
        return True

    class _Session:
        """One bound connection. Takes no identifier: the visitor is bound on it."""

        __slots__ = ("_connection",)

        def __init__(self, connection: Any) -> None:
            self._connection = connection

        def call(
            self, procedure_name: str, arguments: Sequence[object]
        ) -> Mapping[str, Any]:
            """Call one procedure and decode the ``VARIANT`` it returned.

            The statement is assembled from the declaration rather than written
            out: ``PARSE_JSON`` wraps exactly the arguments #46 declared as
            ``VARIANT``, and everything else is bound.

            Args:
                procedure_name: Fully qualified.
                arguments: Positional, in declaration order.

            Returns:
                The decoded object.

            Raises:
                OpsUnavailableError: If the call did not complete. Whether it was
                    attempted is deliberately not claimed -- the retry key is
                    what makes that survivable.
            """
            declaration = procedure(procedure_name.rsplit(".", 1)[-1])
            slots: list[str] = []
            bindings: list[object] = []
            for declared, value in zip(declaration.arguments, arguments, strict=True):
                if declared.sql_type == "VARIANT":
                    slots.append("PARSE_JSON(%s)")
                    bindings.append(json.dumps(value, default=str))
                else:
                    slots.append("%s")
                    bindings.append(value)
            statement = f"CALL {procedure_name}({', '.join(slots)})"
            try:
                with self._connection.cursor() as cursor:
                    cursor.execute(statement, bindings)
                    row = cursor.fetchone()
            except Exception as failure:
                raise OpsUnavailableError(
                    f"the write did not complete: {failure}"
                ) from failure
            finally:
                self._connection.close()
            decoded = json.loads(row[0]) if row and row[0] else {}
            return dict(decoded)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_ops_service(catalog: MenuCatalog | None = None) -> OpsService:
    """Assemble the ops service from the environment.

    Args:
        catalog: The built catalogue the draft store prices against. **Nothing
            supplies one yet** -- the production catalogue loader is #66's, and
            :func:`chip_chat.api.app.build_service` records the same seam for the
            same reason. The parameter exists so the gap is named rather than
            discovered.

    Returns:
        The service.

    Raises:
        RuntimeError: If ``catalog`` is ``None`` or the write credentials are
            missing. Both are refusals to come up, and both surface to a caller
            as :data:`~chip_chat.api.ops.OPS_UNAVAILABLE_MESSAGE`.
    """
    if catalog is None:
        raise RuntimeError(
            "the ops API has no catalogue to price drafts against; the "
            "production loader is #66's"
        )
    return OpsService(
        SnowflakeWriteBackend.from_env(), DraftStore(catalog), ConfirmationLedger()
    )


def configure(service: OpsService) -> None:
    """Install the service this host serves.

    Called by a deployment that assembles the service itself, and by the tests
    that drive these routes without a Snowflake account.

    Args:
        service: The assembled service.
    """
    global _service
    _service = service


def _resolved() -> OpsService:
    """Return the installed service, assembling one from the environment if not.

    Raises:
        OpsUnavailableError: If none can be assembled. That is the state
            RFC-001 section 10 gives copy to, so it is raised as the failure
            that already has an answer rather than as a new one.
    """
    global _service
    if _service is None:
        try:
            _service = build_ops_service()
        except RuntimeError as failure:
            raise OpsUnavailableError(str(failure)) from failure
    return _service


# ---------------------------------------------------------------------------
# The edge
# ---------------------------------------------------------------------------


@app.route(route="place_order", methods=("POST",))
def place_order(request: func.HttpRequest) -> func.HttpResponse:
    """Place a confirmed draft. Body: ``{"draft_id": ...}``."""
    return _handle(
        request, "draft_id", lambda session, reference: session.place_order(reference)
    )


@app.route(route="cancel_order", methods=("POST",))
def cancel_order(request: func.HttpRequest) -> func.HttpResponse:
    """Cancel a confirmed order. Body: ``{"order_id": ...}``."""
    return _handle(
        request, "order_id", lambda session, reference: session.cancel_order(reference)
    )


@app.route(route="redeem_points", methods=("POST",))
def redeem_points(request: func.HttpRequest) -> func.HttpResponse:
    """Redeem a confirmed reward. Body: ``{"reward_id": ...}``."""
    return _handle(
        request, "reward_id", lambda session, reference: session.redeem_points(reference)
    )


@app.route(route="update_preferences", methods=("POST",))
def update_preferences(request: func.HttpRequest) -> func.HttpResponse:
    """Store a confirmed preference edit. Body: ``{"prefs": {...}}``.

    The only route whose body is not an identifier, because
    ``update_preferences`` names no row. What identifies it is its own content,
    and :func:`chip_chat.api.confirmations.preferences_reference` is where that
    is turned back into one.
    """
    return _handle(
        request,
        "prefs",
        lambda session, reference: session.update_preferences(reference),
        shape=dict,
    )


def _handle(
    request: func.HttpRequest,
    field: str,
    write: Callable[[OpsSession, Any], Receipt],
    *,
    shape: type = str,
) -> func.HttpResponse:
    """Authenticate, rejoin the turn, write, and answer.

    Args:
        request: The inbound request.
        field: Which body field carries what the visitor was shown.
        write: The write to perform, given a bound session and that field.
        shape: What ``field`` must be. A string for the three identifiers, a
            mapping for ``prefs``.

    Returns:
        The response. See the module docstring for why a rejection is a 200.
    """
    if not _authentic(request):
        return _refusal(401, "OPS_KEY_INVALID", "this caller may not write")

    body = _body(request)
    if body is None:
        return _refusal(400, "BODY_NOT_JSON", "the request body is not a JSON object")
    reference = body.get(field)
    if not isinstance(reference, shape) or not reference:
        return _refusal(400, "REFERENCE_REQUIRED", f"{field} is required")

    demo_id = request.headers.get(SESSION_HEADER, "")
    if not demo_id:
        return _refusal(401, "SESSION_REQUIRED", "no visitor is bound to this request")

    try:
        with continue_turn(dict(request.headers), parent=SpanName.TOOL):
            service = _resolved()
            receipt = write(service.session(demo_id), reference)
    except TurnContextError as split:
        # Before the write, not after: gate 2 is auditable because every write
        # emits ops.<action>, and a write nobody can find in a trace is a write
        # this service declines to make.
        return _refusal(400, "TRACE_CONTEXT_REQUIRED", str(split))
    except OpsRejectedError as rejected:
        return _json(200, rejected.as_result())
    except OpsUnavailableError as unavailable:
        _LOG.warning("ops API unavailable: %s", unavailable.detail)
        return _json(
            503,
            {
                "ok": False,
                "error": "OPS_UNAVAILABLE",
                "message": unavailable.message,
                "ordering_available": False,
            },
        )
    except ValueError as malformed:
        return _refusal(400, "REQUEST_MALFORMED", str(malformed))
    return _json(200, {"ok": True, "receipt": dict(receipt.as_dict())})


def _authentic(request: func.HttpRequest) -> bool:
    """Whether the caller presented the ops key.

    An unset :data:`OPS_KEY_VARIABLE` refuses everything. The other way round --
    no key configured, so no check -- is how a write path ends up open, and it
    fails in exactly the environment nobody tests.
    """
    expected = os.environ.get(OPS_KEY_VARIABLE, "")
    presented = request.headers.get(OPS_KEY_HEADER, "")
    return bool(expected) and hmac.compare_digest(expected, presented)


def _body(request: func.HttpRequest) -> Mapping[str, Any] | None:
    """Return the request body as a mapping, or ``None`` if it is not one."""
    try:
        parsed = request.get_json()
    except ValueError:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _refusal(status: int, code: str, detail: str) -> func.HttpResponse:
    """Answer a call this service will not make at all."""
    return _json(status, {"ok": False, "error": code, "detail": detail})


def _json(status: int, body: Mapping[str, Any]) -> func.HttpResponse:
    """Render one response."""
    return func.HttpResponse(
        json.dumps(body, default=str),
        status_code=status,
        mimetype="application/json",
    )


UNAVAILABLE_BODY: Final = {
    "ok": False,
    "error": "OPS_UNAVAILABLE",
    "message": OPS_UNAVAILABLE_MESSAGE,
    "ordering_available": False,
}
"""The 503 body, named so a test can assert on it rather than on a string."""
