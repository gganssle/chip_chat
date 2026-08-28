"""The trace source: real spans, out of the deployed backend, as live turns.

This is the function ``eval/online/README.md`` promised and #76 never had. Every
other part of this package has existed and been testable since the ticket
landed -- six monitors, a deterministic sampler, a budget line, a drill that
produces each feared condition -- and all of it read a **capture file** that a
human produced by hand. A monitor whose only input arrives when somebody
remembers to produce it is not a monitor; it is a script with good intentions.

So: one function, from a backend's rows to the shape the readers already take.
The README states the rule this obeys and it is worth repeating because it is
the reason this module is ninety lines rather than a subsystem:

    An Arize adapter, a Phoenix adapter and a file are three functions
    producing one shape.

Everything above :class:`~chip_chat.eval.online.signals.LiveTurn` -- the
trajectory reader, the evidence reader, the six monitors, the sampler, the
budget -- is the code the offline evals already use and is untouched by which
backend answered. What is here is HTTP, JSON, and three shape differences
between what a backend stores and what a reader wants.

**This module names a vendor and that is correct.** The rule that forbids it
lives in ``otel/`` and is enforced by
``otel/tests/test_export_configuration.py::test_the_exporter_code_names_no_vendor``:
the *instrumentation* must not know which backend it is talking to, because
decision D6 buys the ability to move backends without touching a call site. An
*adapter* is the opposite thing. It exists precisely to know one backend's row
shape, and the whole design is that the knowing is confined to it.

The three shape differences, since each one is a bug if it is got wrong:

**Attributes arrive nested.** The wire carries ``input.value``; Phoenix's REST
API hands back ``{"input": {"value": ...}}``. Every reader downstream looks up
the dotted OpenInference key, so :func:`_flatten` puts them back. It flattens
defensively rather than assuming, because a span attribute whose *value* is
genuinely a mapping is a thing that exists and must survive.

**Times arrive as ISO-8601 strings.** :class:`TraceSpan` wants an integer, and
the latency monitor divides the difference between two of them by a million to
get milliseconds -- so the integer has to be nanoseconds, not seconds and not
"any monotonic integer", whatever the field's docstring permits in general.

**The end time is not an attribute anywhere.** A span's end is a property of
the span; ``signals.END_TIME`` is the agreed key an adapter puts it under, and
if this module forgot to, every turn would report a duration of zero and the
latency monitor would read every turn as compliant. That is the failure mode
:func:`~chip_chat.eval.online.signals._duration` is written defensively against
and this module is the reason it had to be.

Reading requires no credential because the backend it reads is on an internal
address inside the Container Apps environment; there is no API key to hold and
no vault entry to rotate. If it is ever put behind Phoenix's own authentication,
one ``Authorization`` header belongs in :func:`_get` and nowhere else.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from openinference.semconv.trace import SpanAttributes

from chip_chat.eval.online.signals import END_TIME, LiveTurn, read_turn
from chip_chat.eval.trajectory.trees import TraceSpan
from chip_chat.otel.schema import SpanName

__all__ = ["PhoenixError", "read_live_turns", "read_spans"]

DEFAULT_PROJECT = "default"
"""Where the app's spans land.

The same project the local stack uses, deliberately. A span tree read here and
a span tree read on a laptop are then the same tree in the same place, and
``docs/local-tracing.md``'s instruction to "pick the ``default`` project" does
not have to grow an exception for production.
"""

DEFAULT_PAGE = 1000
"""Spans per request. Not turns -- one turn is on the order of a dozen spans."""

_MAX_PAGES = 50
"""A ceiling on the pagination loop.

Fifty thousand spans is four thousand turns, which is far more than a quarter
of an hour of a demo produces. The ceiling exists so that a backend answering
with a cursor that never advances costs one slow job execution rather than a
job that never ends, and the run says out loud that it stopped early.
"""

_SERVICE_NAME = "service.name"
_TURN = SpanName.CHAT_TURN.value
_RENDER = SpanName.RENDER_RESPONSE.value


class PhoenixError(RuntimeError):
    """The backend could not be read.

    Raised rather than swallowed. A monitoring loop that cannot reach its trace
    source and reports "0 turns, 0 alerts" is worse than one that fails: the
    first looks exactly like quiet traffic, which is the state the monitors
    exist to distinguish from trouble.
    """


def read_live_turns(
    base_url: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    lookback_minutes: float = 20.0,
    project: str = DEFAULT_PROJECT,
    page: int = DEFAULT_PAGE,
    timeout: float = 30.0,
) -> tuple[LiveTurn, ...]:
    """Read a window of production traffic as live turns.

    Args:
        base_url: The backend's base URL, e.g. the value of
            ``OTEL_EXPORTER_OTLP_ENDPOINT``. The REST path is appended.
        since: Inclusive lower bound. ``None`` means ``lookback_minutes`` ago.
        until: Exclusive upper bound. ``None`` means now.
        lookback_minutes: How far back to look when ``since`` is not given.
            The scheduled job passes a window slightly longer than its own
            interval, on purpose: a turn landing between the end of one window
            and the start of the next would otherwise be seen by nobody, and a
            monitor firing twice on one trace is a duplicate alert while a
            monitor firing on none is a missed one.
        project: The Phoenix project. See :data:`DEFAULT_PROJECT`.
        page: Spans per request.
        timeout: Seconds to wait on each request.

    Returns:
        One turn per trace, oldest first. A trace with no ``chat.turn`` span in
        the window is dropped rather than returned as an unreadable turn: it is
        almost always a turn whose root started before the window opened, which
        is a windowing artefact and not a broken trace, and counting it as
        unreadable would put a permanent floor under the unreadable rate.

    Raises:
        PhoenixError: If the backend cannot be reached or answers with
            something that is not the documented shape.
    """
    end = until or datetime.now(tz=UTC)
    start = since or (end - timedelta(minutes=lookback_minutes))
    spans = read_spans(
        base_url, start=start, end=end, project=project, page=page, timeout=timeout
    )
    return tuple(_turns(spans))


def read_spans(
    base_url: str,
    *,
    start: datetime,
    end: datetime,
    project: str = DEFAULT_PROJECT,
    page: int = DEFAULT_PAGE,
    timeout: float = 30.0,
) -> tuple[TraceSpan, ...]:
    """Read the raw spans in a window, in the readers' shape.

    Separate from :func:`read_live_turns` because a span tree is the thing you
    want when you are checking that the backend received what the app sent --
    which is a different question from whether a monitor fires on it, and the
    one worth answering first when a repoint has just happened.

    Args:
        base_url: The backend's base URL.
        start: Inclusive lower bound.
        end: Exclusive upper bound.
        project: The Phoenix project.
        page: Spans per request.
        timeout: Seconds to wait on each request.

    Returns:
        Every span in the window, oldest first.

    Raises:
        PhoenixError: If the backend cannot be reached or answers oddly.
    """
    path = f"/v1/projects/{urllib.parse.quote(project, safe='')}/spans"
    query: dict[str, str] = {
        "start_time": start.astimezone(UTC).isoformat(),
        "end_time": end.astimezone(UTC).isoformat(),
        "limit": str(page),
    }
    collected: list[TraceSpan] = []
    for _ in range(_MAX_PAGES):
        body = _get(base_url, path, query, timeout=timeout)
        rows = body.get("data")
        if not isinstance(rows, list):
            raise PhoenixError(f"{path} answered without a 'data' array")
        collected.extend(_span(row) for row in rows)
        cursor = body.get("next_cursor")
        if not cursor or not rows:
            break
        query["cursor"] = str(cursor)
    return tuple(sorted(collected, key=lambda span: span.started))


def _turns(spans: Sequence[TraceSpan]) -> Iterator[LiveTurn]:
    """Group spans into traces and read each one as a turn."""
    traces: dict[str, list[TraceSpan]] = {}
    for span in spans:
        traces.setdefault(span.trace_id, []).append(span)
    for trace in traces.values():
        roots = [span for span in trace if span.name == _TURN]
        if not roots:
            continue
        yield read_turn(trace, message=_message(trace), reply=_reply(trace))


def _message(spans: Sequence[TraceSpan]) -> str:
    """What the visitor said, off the root span's OpenInference input."""
    for span in spans:
        if span.name == _TURN:
            return _text(span.attributes.get(SpanAttributes.INPUT_VALUE))
    return ""


def _reply(spans: Sequence[TraceSpan]) -> str:
    """What came back.

    The root records it and so does ``render.response``, and they are the same
    string by construction -- but a turn a guard stopped has a root output and
    no render span at all, and a turn whose root was still open when the window
    closed has the render span and an empty root. Preferring the root and
    falling back keeps both readable, and the judged monitors are the ones that
    would otherwise silently score an empty reply.
    """
    for name in (_TURN, _RENDER):
        for span in spans:
            if span.name != name:
                continue
            value = _text(span.attributes.get(SpanAttributes.OUTPUT_VALUE))
            if value:
                return value
    return ""


def _get(
    base_url: str, path: str, query: Mapping[str, str], *, timeout: float
) -> Mapping[str, Any]:
    """One GET against the backend's REST API."""
    url = f"{base_url.rstrip('/')}{path}?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:  # pragma: no cover -- network
        raise PhoenixError(f"{url} answered {error.code}: {error.reason}") from error
    except (urllib.error.URLError, TimeoutError) as error:  # pragma: no cover
        raise PhoenixError(f"{url} could not be reached: {error}") from error
    except json.JSONDecodeError as error:  # pragma: no cover -- network
        raise PhoenixError(f"{url} answered something that is not JSON") from error
    if not isinstance(payload, dict):
        raise PhoenixError(f"{url} answered {type(payload).__name__}, not an object")
    return payload


def _span(row: Any) -> TraceSpan:
    """One backend row, as the span the readers take.

    Raises:
        PhoenixError: If the row is missing the fields every span has. A
            malformed row is a backend contract that moved, and continuing past
            it would report a short trace as a complete one.
    """
    if not isinstance(row, dict):
        raise PhoenixError(f"a span came back as {type(row).__name__}, not an object")
    context = row.get("context")
    if not isinstance(context, dict):
        raise PhoenixError("a span came back without a context")
    attributes = _flatten(row.get("attributes") or {})
    started = _nanos(row.get("start_time"))
    ended = _nanos(row.get("end_time"))
    if ended is not None:
        # The one key no backend carries and every adapter must supply. See the
        # module docstring, and signals.END_TIME for what reads it.
        attributes[END_TIME] = ended
    service = attributes.get(_SERVICE_NAME)
    return TraceSpan(
        name=str(row.get("name", "")),
        span_id=str(context.get("span_id", "")),
        parent_id=str(row["parent_id"]) if row.get("parent_id") else None,
        trace_id=str(context.get("trace_id", "")),
        attributes=attributes,
        service=service if isinstance(service, str) else None,
        started=started or 0,
    )


def _flatten(attributes: Any, prefix: str = "") -> dict[str, Any]:
    """Nested attribute objects back into the dotted keys the readers look up.

    Both forms are kept where a nested value is itself a mapping: the dotted
    leaves *and* the mapping under its own key. A span attribute whose value is
    genuinely a JSON object is a thing that exists -- ``tool.parameters`` is one
    -- and a flattener that consumed it would delete data to tidy a shape.
    """
    flat: dict[str, Any] = {}
    if not isinstance(attributes, dict):
        return flat
    for key, value in attributes.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{dotted}."))
        flat[dotted] = value
    return flat


def _nanos(value: Any) -> int | None:
    """An ISO-8601 timestamp as nanoseconds since the epoch.

    Nanoseconds specifically, because ``signals._duration`` divides the gap
    between two of these by a million and calls the result milliseconds.
    """
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp() * 1_000_000_000)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""
