"""Does a Foundry thread survive a gap between visits?

``docs/decisions/foundry-agent-shape.md`` settled where conversation state lives
— Microsoft-managed thread storage on a basic-setup project — and left exactly
one question open, routed to issue #8:

    *How long Microsoft-managed thread storage retains a thread, and whether a
    thread can be retrieved by id after an arbitrary gap between visits.*

It matters because #9 made visitor state durable: a returning visitor is
supposed to resume the conversation they left. The app stores the ``thread_id``;
if the thread behind it has expired, the pointer is a dangling reference and
message history has to move into the app's own store.

**This module is an instrument, not an answer.** The honest shape of the problem
is that "does it survive an arbitrary gap" cannot be established in an
afternoon, and a script that claimed to establish it would be lying. So it does
the two things that *can* be done now:

``create``
    Mints a thread, writes a message into it, and prints the id and a verbatim
    dump of the thread object. Run it and keep the id.

``fetch <thread_id>``
    Reads that thread and its messages back and reports the age of the thread in
    days. Run it after a week, a month, a quarter. The first run that fails is
    the retention answer, and the last run that succeeds is the lower bound.

What the baseline run already establishes is recorded in
``docs/phase-0-verification.md``. Nothing here needs an agent to exist: threads
are created against the project, not against an agent.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Any

from chip_chat.agent.foundry import credential

__all__ = ["main"]

_AI_SCOPE = "https://ai.azure.com/.default"

_DEFAULT_API_VERSION = "v1"
"""The Agents data-plane version. ``2025-05-01`` and ``2025-11-15-preview`` both
answer identically for the calls here, checked 2026-08-26."""


class ThreadProbeError(RuntimeError):
    """The probe could not reach the project, or the project refused it."""


def _project_endpoint() -> str:
    endpoint = os.environ.get("CHIP_CHAT_FOUNDRY_PROJECT_ENDPOINT", "").strip()
    if not endpoint:
        raise ThreadProbeError(
            "CHIP_CHAT_FOUNDRY_PROJECT_ENDPOINT is not set. Read it with:\n"
            "  terraform -chdir=infra/terraform output -json foundry_project_endpoints"
            '\nand take the "AI Foundry API" value.'
        )
    return endpoint.rstrip("/")


def _call(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    api_version: str = _DEFAULT_API_VERSION,
) -> dict[str, Any]:
    """Make one Agents data-plane call.

    Raw ``urllib`` rather than an SDK on purpose. The question this module asks
    is what the *service* returns — specifically whether a thread object carries
    any expiry field at all — and an SDK that deserialises into a typed model
    answers by discarding exactly the fields that would settle it.
    """
    token = credential().get_token(_AI_SCOPE).token
    url = f"{_project_endpoint()}/{path.lstrip('/')}?api-version={api_version}"
    request = urllib.request.Request(
        url,
        method=method,
        data=json.dumps(body or {}).encode() if method == "POST" else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            decoded: dict[str, Any] = json.loads(response.read())
            return decoded
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise ThreadProbeError(
            f"{method} {path} returned HTTP {error.code}\n{detail}"
        ) from error


def create() -> dict[str, Any]:
    """Mint a thread, write one message into it, and return the thread object."""
    thread = _call("POST", "threads")
    _call(
        "POST",
        f"threads/{thread['id']}/messages",
        {
            "role": "user",
            "content": (
                "Retention probe for issue #11. If this message is still "
                "readable, Microsoft-managed thread storage retained it."
            ),
        },
    )
    return thread


def fetch(thread_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read a thread and its messages back by id.

    Raises:
        ThreadProbeError: If the thread is gone or unreadable, which is the
            outcome this probe exists to catch.
    """
    thread = _call("GET", f"threads/{thread_id}")
    messages = _call("GET", f"threads/{thread_id}/messages")
    return thread, list(messages.get("data", []))


def _report_expiry_fields(thread: dict[str, Any]) -> str:
    """Say whether the service expresses an expiry on the thread object at all.

    A retention period the API declines to express in the object is a retention
    period you cannot code against — the app cannot pre-emptively migrate a
    thread that is about to lapse if nothing tells it one is about to lapse.
    """
    candidates = sorted(
        key
        for key in thread
        if any(word in key.lower() for word in ("expire", "expiry", "ttl", "retain"))
    )
    if candidates:
        return "expiry-ish fields present: " + ", ".join(
            f"{key}={thread[key]!r}" for key in candidates
        )
    return (
        "no expiry, ttl or retention field on the thread object "
        f"(fields: {', '.join(sorted(thread))})"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run one probe action. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m chip_chat.agent.threads",
        description="Foundry thread retention probe for issue #11.",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("create", help="mint a probe thread and print its id")
    fetch_parser = sub.add_parser("fetch", help="read a probe thread back by id")
    fetch_parser.add_argument("thread_id")
    args = parser.parse_args(argv)

    if args.action == "create":
        thread = create()
        print(f"thread_id  {thread['id']}")
        print(f"created_at {thread['created_at']}")
        print(f"           {_report_expiry_fields(thread)}")
        print(json.dumps(thread, indent=2, sort_keys=True))
        print(
            "\nKeep that id. Re-run with `fetch <id>` after a week, a month, a "
            "quarter.\nThe first run that fails is the retention answer; the last "
            "that succeeds is the lower bound."
        )
        return 0

    try:
        thread, messages = fetch(args.thread_id)
    except ThreadProbeError as error:
        print(f"GONE — {args.thread_id} could not be read.\n{error}", file=sys.stderr)
        print(
            "\nIf this thread was readable before, that is the retention limit and "
            "issue #11's fallback applies: message history moves into the app's "
            "own durable store.",
            file=sys.stderr,
        )
        return 1

    age_days = (time.time() - thread["created_at"]) / 86400
    print(f"ALIVE — {thread['id']} readable {age_days:.1f} days after creation.")
    print(f"        {len(messages)} message(s) retained.")
    print(f"        {_report_expiry_fields(thread)}")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
