"""The chat app's end of the write path: four POSTs, and nothing that writes.

``api/functions/function_app.py`` is the ops API's edge and
:mod:`chip_chat.api.ops` is the rule it enforces. This module is the *caller* --
the half that had never been written, and whose absence is what
``docs/ops-api.md`` recorded as *"the write path is deployed, credentialled and
refusing correctly, and the chat app does not yet call it."*

It is deliberately small, and everything it does not do is the point.

**It holds no write credential.** RFC-001 §03: the ops API is the only path that
writes, and the only holder of the Snowflake write role. What this class holds is
two bearer secrets that let it *ask* -- the platform's function key and the
application's own ops key -- and a derived key it signs confirmations with. A
compromise here yields the ability to call four stored procedures under a
confirmation the app itself minted; it does not yield SQL.

**It composes no procedure arguments.** What travels on the wire is the reference
the model named and a signed grant carrying the arguments the *app* built from
the record the visitor confirmed. The ops API reads the second and ignores the
first except as a binding to check the second against. See
:mod:`chip_chat.api.grants`.

**It never retries a write.** :class:`~chip_chat.api.ops.OpsService` already
retries once inside the ops API, with the retry key, which is the only place a
retry is safe -- the key is spent inside the procedure's own transaction. A retry
*here* would be a second HTTP request carrying the same grant, which the retry
key would also make safe, and would still be wrong: this side cannot distinguish
"the request never arrived" from "the response was lost", and a visitor waiting
on a doubled timeout is worse served than one told that ordering is temporarily
unavailable. RFC-001 §10 gives that state its copy and this module raises it.

**Trace context is not optional.** The ops API refuses a write it cannot find in
a trace, which is a rule about auditability rather than about transport (see
``docs/ops-api.md``). So every call injects the current span with
:func:`chip_chat.otel.turn_context_headers`, and it is called from inside
``tool.<name>`` -- which is what makes ``ops.<action>`` a child of the tool span
in the deployed system, across a process boundary, exactly as the span schema
requires.

**Availability is asked, cached, and never guessed.** RFC-001 §10 wants a
confirmation card that *renders* and says ordering is unavailable, which is only
possible if somebody asked before the card was composed.
:meth:`OpsClient.available` is that question, memoised for
:data:`AVAILABILITY_TTL_SECONDS` so that composing a card costs nothing on the
common path and so that a dead ops API is noticed within a few seconds rather
than a few minutes.

The probe is the one call in this module that carries **no** trace context, and
that is deliberate rather than an omission. A probe is not a turn: it is asked
from ``GET /healthz/lanes`` where no conversation is open, and injecting a
context there would either fail -- there is no span -- or, worse, invent a trace
for something no visitor did. So it sends a request the ops API is guaranteed to
refuse and reads *which* refusal came back. ``400 TRACE_CONTEXT_REQUIRED`` is
the answer it expects, and it is a stronger signal than it looks: the ops API
checks the key *before* the trace context, so that refusal establishes the route
is registered, this code is loaded on the worker, and both keys were accepted --
which is every question ``make ops-check`` asks, answered in one request that
touches no warehouse. The alternative, a fifth route on the only service in the
system that holds the write role, is a worse trade.
"""

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any, Final

from chip_chat.api.grants import GRANT_HEADER
from chip_chat.api.ops import (
    PRECONDITION_REJECTIONS,
    SESSION_HEADER,
    OpsRejectedError,
    OpsUnavailableError,
)
from chip_chat.otel import OpsAction, TurnContextError, turn_context_headers

__all__ = [
    "AVAILABILITY_TTL_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "FUNCTION_KEY_HEADER",
    "OPS_KEY_HEADER",
    "OpsClient",
]

_log = logging.getLogger("chip_chat.api.opsclient")

OPS_KEY_HEADER: Final = "x-cilantro-ops-key"
"""The application's shared secret. ``function_app._authentic`` compares it."""

FUNCTION_KEY_HEADER: Final = "x-functions-key"
"""The platform's own key, because the host runs at ``AuthLevel.FUNCTION``.

Two keys and not one, and they are not redundant. The function key is Azure's,
rotatable from the portal, and stops an anonymous caller reaching the worker at
all; the ops key is the application's, compared in code, and is what makes an
*unset* secret refuse every request rather than allow them all. Either alone
would be a defensible design. Both is what is deployed, so both are sent.
"""

DEFAULT_TIMEOUT_SECONDS: Final = 25.0
"""How long a write waits before it is called down.

Generous, and the number is the warehouse's rather than the network's: the ops
API opens a Snowflake session per call and ``CHIP_CHAT_SERVING_WH`` suspends
after sixty seconds of idle, so the first write of a conversation pays for a
resume. Short enough to stay inside the sixty seconds Container Apps ingress
allows a silent response -- the trap chip-901 fixed for the chat route -- and
long enough that a cold warehouse is a slow order rather than a failed one.
"""

AVAILABILITY_TTL_SECONDS: Final = 15.0
"""How long an availability answer is reused.

Fifteen seconds. Long enough that a conversation proposing three cards asks
once; short enough that an operator who has just restarted the ops API does not
have to explain to anybody why the banner is still up.
"""

_PROBE_REFERENCE: Final = "draft-availability-probe"
"""What :meth:`OpsClient.available` names. Deliberately not a plausible id.

The probe is refused before a Snowflake session is acquired, so it costs the ops
API one HTTP request and no warehouse time -- and a probe that ever *succeeded*
would be a probe that had placed an order, which is why this is a string no
draft store could have minted rather than a random one that might one day
collide.
"""

_PROBE_REFUSALS: Final = frozenset({"TRACE_CONTEXT_REQUIRED", *PRECONDITION_REJECTIONS})
"""The answers that mean the ops API is up and refusing correctly.

``TRACE_CONTEXT_REQUIRED`` is the one the probe actually expects, for the reason
the module docstring gives. The precondition rejections are here too because a
future edge that checked the record before the trace would answer one of those
instead, and a health surface that went red on a *reordering of two refusals*
would be a health surface nobody trusts. What is deliberately absent is
``OPS_KEY_INVALID`` -- a deployment whose secret is unresolved is answering, and
is not available for anything.
"""

_BODY_FIELDS: Final[Mapping[OpsAction, str]] = {
    OpsAction.PLACE_ORDER: "draft_id",
    OpsAction.CANCEL_ORDER: "order_id",
    OpsAction.REDEEM_POINTS: "reward_id",
    OpsAction.UPDATE_PREFERENCES: "prefs",
}
"""Which body field each route reads, as ``function_app`` declares them.

A mapping rather than four call sites, so that the four routes and the four
actions cannot drift apart silently -- the same argument
:data:`chip_chat.snowflake.procedures.PROCEDURES` makes one tier down about
argument order.
"""


class OpsClient:
    """One deployed ops API, as the four calls the app makes to it.

    Thread-safe. A Container App serves concurrent requests and the only mutable
    state here is the memoised availability answer, which is guarded.
    """

    __slots__ = (
        "_base",
        "_checked_at",
        "_function_key",
        "_lock",
        "_ops_key",
        "_timeout",
        "_up",
    )

    def __init__(
        self,
        base_url: str,
        ops_key: str,
        function_key: str = "",
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """Point the app at a deployed ops API.

        Args:
            base_url: The Functions app's origin, with or without a trailing
                slash. ``/api/<route>`` is appended.
            ops_key: The shared secret, from :data:`CHIP_CHAT_OPS_KEY
                <chip_chat.api.grants.OPS_KEY_VARIABLE>`.
            function_key: The platform key, where the host runs at
                ``AuthLevel.FUNCTION``. Empty is allowed, because a host at
                ``ANONYMOUS`` needs none and because sending an empty header is
                worse than sending none.
            timeout: See :data:`DEFAULT_TIMEOUT_SECONDS`.

        Raises:
            ValueError: If ``base_url`` or ``ops_key`` is empty. A client that
                could be built without a secret would be a client whose every
                call is refused with ``OPS_KEY_INVALID``, one layer away from
                where anybody would look.
        """
        if not base_url:
            raise ValueError("an ops client needs somewhere to post")
        if not ops_key:
            raise ValueError("an ops client with no key can only be refused")
        self._base = base_url.rstrip("/")
        self._ops_key = ops_key
        self._function_key = function_key
        self._timeout = timeout
        self._lock = threading.Lock()
        self._up: bool | None = None
        self._checked_at = 0.0

    @property
    def base_url(self) -> str:
        """Where this client posts. For a health surface and a log line."""
        return self._base

    def available(self) -> bool:
        """Whether a card composed now should say ordering is available.

        Asked while a card is being *composed*, never when Confirm is pressed;
        :class:`~chip_chat.api.ops.WriteBackend` says why at length and the
        answer is the same here. Memoised -- see
        :data:`AVAILABILITY_TTL_SECONDS`.

        Returns:
            Whether the deployed ops API answered this app at all. It does not
            establish that Snowflake is reachable *from* the ops API: that is a
            fact about a third process and the only honest way to learn it is to
            attempt a write, which this must not do.
        """
        now = time.monotonic()
        with self._lock:
            if self._up is not None and now - self._checked_at < AVAILABILITY_TTL_SECONDS:
                return self._up
        up = self._probe()
        with self._lock:
            self._up = up
            self._checked_at = time.monotonic()
        return up

    def write(
        self,
        action: OpsAction,
        *,
        demo_id: str,
        reference: Any,
        confirmation: str,
    ) -> Mapping[str, Any]:
        """Make one write, and return the receipt the procedure composed.

        Args:
            action: Which of the four.
            demo_id: The visitor the app resolved from the session cookie. It
                travels on :data:`~chip_chat.api.ops.SESSION_HEADER`,
                server-to-server, and is never seen by a browser or a model.
            reference: What the visitor was shown -- an id for three of the
                routes, the preferences object for the fourth.
            confirmation: The signed grant, from
                :meth:`chip_chat.api.grants.GrantSigner.mint`. Required and
                without a default: a client that could make a write with no
                confirmation would be a client with the gate as an option.

        Returns:
            The receipt, as ``sql/12_procedures.sql`` composed it.

        Raises:
            OpsRejectedError: The write was refused -- by the gate, or by the
                procedure. A refusal is a normal answer and carries a code.
            OpsUnavailableError: The ops API could not be reached, or answered
                something this client cannot read. Nothing was written that this
                side knows of, and the retry key is what makes the uncertainty
                survivable.
        """
        body = {_BODY_FIELDS[action]: reference}
        status, answer = self._post(action.value, body, demo_id, confirmation)
        if status == 503:
            raise OpsUnavailableError(
                str(answer.get("message") or answer.get("detail") or "ops API down")
            )
        if status != 200:
            # 401 and 400 are the calls the ops API will not answer at all: a
            # bad key, no visitor, no trace context. Every one of them is a
            # deployment fault rather than a visitor's, so it surfaces as the
            # outage it is rather than as a rejection the model is invited to
            # explain to somebody.
            detail = str(answer.get("error") or status)
            raise OpsUnavailableError(f"the ops API refused the call: {detail}")
        if answer.get("ok"):
            receipt = answer.get("receipt")
            if isinstance(receipt, Mapping):
                return dict(receipt)
            raise OpsUnavailableError("the ops API answered ok with no receipt")
        code = str(answer.get("error", "WRITE_FAILED"))
        raise OpsRejectedError(
            action,
            code,
            str(answer.get("detail", "the write was refused")),
            _subject(answer),
        )

    # --- transport ---------------------------------------------------------

    def _probe(self) -> bool:
        """Ask the ops API something it must refuse, and read whether it did.

        One of :data:`_PROBE_REFUSALS` is the strongest evidence available
        short of writing: it means the route is registered, this code is loaded
        on the worker, and both keys were accepted. A 404, an
        ``OPS_KEY_INVALID`` or a connection error is not availability, and
        neither is a 503 -- which is the ops API saying it cannot reach
        Snowflake, and is precisely the state RFC-001 §10 wants a card to
        report rather than hide.
        """
        try:
            _, answer = self._post(
                OpsAction.PLACE_ORDER.value,
                {"draft_id": _PROBE_REFERENCE},
                demo_id="dm-availability-probe",
                confirmation="",
                traced=False,
            )
        except OpsUnavailableError as unreachable:
            _log.info("the ops API is not answering: %s", unreachable.detail)
            return False
        refusal = str(answer.get("error", ""))
        if refusal not in _PROBE_REFUSALS:
            _log.info("the ops API answered the availability probe with %r", refusal)
            return False
        return True

    def _post(
        self,
        route: str,
        body: Mapping[str, Any],
        demo_id: str,
        confirmation: str,
        *,
        traced: bool = True,
    ) -> tuple[int, Mapping[str, Any]]:
        """Send one request and decode one answer.

        Raises:
            OpsUnavailableError: For anything that is not an HTTP answer with a
                JSON object in it -- a DNS failure, a timeout, a proxy's HTML
                error page. The one case that is *not* an outage is an HTTP
                error status carrying a JSON body, which is how the ops API says
                401, 400 and 503, so those are read and returned.
        """
        payload = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base}/api/{route}",
            data=payload,
            headers=self._headers(demo_id, confirmation, traced=traced),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return response.status, _decoded(response.read())
        except urllib.error.HTTPError as answered:
            return answered.code, _decoded(answered.read())
        except OSError as unreachable:
            raise OpsUnavailableError(
                f"the ops API could not be reached: {unreachable}"
            ) from unreachable

    def _headers(
        self, demo_id: str, confirmation: str, *, traced: bool = True
    ) -> dict[str, str]:
        """The headers one call carries.

        Args:
            demo_id: The visitor, on :data:`~chip_chat.api.ops.SESSION_HEADER`.
            confirmation: The signed grant, or empty for the probe.
            traced: Whether to inject the current span. True for every write and
                False for the availability probe alone -- see the module
                docstring on why a probe is not a turn.

        Raises:
            OpsUnavailableError: If no span is open. The ops API refuses a write
                it cannot find in a trace, so a call composed outside one would
                be refused there with ``TRACE_CONTEXT_REQUIRED`` -- and finding
                that out here, by name, is cheaper than finding it out over
                HTTPS. It is an outage rather than a rejection because it is a
                fault in this repository and never a visitor's doing.
        """
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            OPS_KEY_HEADER: self._ops_key,
            SESSION_HEADER: demo_id,
        }
        if self._function_key:
            headers[FUNCTION_KEY_HEADER] = self._function_key
        if confirmation:
            headers[GRANT_HEADER] = confirmation
        if not traced:
            return headers
        try:
            headers.update(turn_context_headers())
        except TurnContextError as unparented:
            raise OpsUnavailableError(
                f"a write was composed outside a span: {unparented}"
            ) from unparented
        return headers


def _decoded(raw: bytes) -> Mapping[str, Any]:
    """Read one response body, or say that it was not one.

    Raises:
        OpsUnavailableError: If the body is not a JSON object. A proxy's HTML,
            an empty body from a closed connection and a JSON array are all the
            same fact -- the thing that answered was not the ops API answering.
    """
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as unreadable:
        raise OpsUnavailableError(
            f"the ops API answered something that is not JSON: {unreadable}"
        ) from unreadable
    if not isinstance(decoded, dict):
        raise OpsUnavailableError("the ops API answered JSON that is not an object")
    return decoded


def _subject(answer: Mapping[str, Any]) -> str | None:
    """The row a procedure named in its refusal, where it named one."""
    subject = answer.get("subject")
    return None if subject is None else str(subject)
