"""Stage 0. Bound the *read*, before there is a payload to validate at all.

:mod:`chip_chat.vision.validate` takes ``bytes`` and its first gate compares
``len(data)`` against the ceiling. That gate is correct and it is also too late
on its own: something has to have read the body into memory for ``len`` to have
an answer, and a handler that does ``await request.body()`` has already bought
whatever the sender chose to send before the ceiling gets a vote. This module is
the gate in front of that one.

Two attacks live here, and neither is a size the byte ceiling can see:

**A small declared size with a large body.** ``Content-Length`` is a number the
client typed, exactly like ``Content-Type`` -- see
:mod:`chip_chat.vision.validate` for why that matters. It is used here in one
direction only: a declared length *over* the ceiling refuses before a single
byte is read, because a sender who admits to being too big can be taken at their
word. A declared length *under* the ceiling buys nothing and is not believed --
nothing preallocates from it, and the running total decides.

**A body that never ends.** Eight mebibytes at one byte a second is under every
size ceiling for ninety-seven days. A few hundred of those connections is the
whole worker pool, and nothing about any of them is oversized. So the read
carries a deadline as well as a ceiling: see
:attr:`~chip_chat.vision.limits.UploadLimits.max_seconds`.

The refusal for both is an ordinary
:class:`~chip_chat.vision.validate.UploadRejectedError`, so the handler has one
``except`` and not two.

.. code-block:: python

    @app.post("/upload")
    async def upload(file: UploadFile, request: Request) -> UploadResponse:
        try:
            payload = await read_upload_async(
                file, declared_length=content_length(request)
            )
            photo = intake.accept(payload, declared_media_type=file.content_type)
        except UploadRejectedError as refusal:
            return UploadResponse(error=refusal.message)

What this module can and cannot do is worth being exact about. It bounds a
*trickle*: the deadline is checked between reads, so a sender delivering one
byte at a time is cut off on schedule. It does not bound a socket that accepts
the connection and then goes silent forever inside a single ``read`` -- no
in-process loop can, and the answer to that one is the server's own read
timeout, which belongs to the deployment rather than here.
"""

import time
from collections.abc import Callable
from typing import Final, Protocol

from chip_chat.vision.limits import UploadLimits
from chip_chat.vision.validate import RejectionReason, rejection

__all__ = [
    "AsyncByteStream",
    "ByteStream",
    "content_length",
    "read_upload",
    "read_upload_async",
]

_CHUNK_BYTES: Final = 64 * 1024
"""How much is asked for per read.

Small enough that the ceiling is crossed by at most this much rather than by
whatever the sender chose, and large enough that an 8 MB upload is 128 reads
rather than eight million.
"""


class ByteStream(Protocol):
    """Anything with a blocking ``read(size)``, which is every file-like object."""

    def read(self, size: int, /) -> bytes:
        """Return at most ``size`` bytes, or empty at end of stream."""
        ...


class AsyncByteStream(Protocol):
    """Anything with an awaitable ``read(size)`` -- Starlette's ``UploadFile``, say."""

    async def read(self, size: int, /) -> bytes:
        """Return at most ``size`` bytes, or empty at end of stream."""
        ...


def content_length(raw: str | int | None) -> int | None:
    """Read a ``Content-Length`` header into something :func:`read_upload` can use.

    Anything unparseable, negative, or absent becomes ``None``, which means "no
    claim was made". That is the safe reading: the header is a hint that can
    only ever make the refusal *earlier*, so a hint we cannot parse costs one
    early exit and nothing else.

    Args:
        raw: The header value as it arrived, or ``None``.

    Returns:
        The declared length, or ``None`` if there is no usable claim.
    """
    if raw is None:
        return None
    try:
        declared = int(raw)
    except (TypeError, ValueError):
        return None
    return declared if declared >= 0 else None


def read_upload(
    stream: ByteStream,
    *,
    declared_length: int | None = None,
    limits: UploadLimits | None = None,
    monotonic: Callable[[], float] | None = None,
) -> bytes:
    """Read one upload under a byte ceiling and a deadline.

    Args:
        stream: The request body. Read in :data:`_CHUNK_BYTES` pieces.
        declared_length: What the request claimed the body was, if it claimed
            anything. Consulted only to refuse early -- see the module
            docstring. Pass it through :func:`content_length` first.
        limits: The ceilings to enforce. Defaults to :class:`UploadLimits`.
        monotonic: Source of monotonic seconds, for the deadline. Defaults to
            :func:`time.monotonic`; a test passes its own so that "this upload
            took too long" is a property it can assert rather than wait for.

    Returns:
        The body. Never more than
        :attr:`~chip_chat.vision.limits.UploadLimits.max_bytes` bytes, and
        possibly empty -- an empty upload is
        :func:`~chip_chat.vision.validate.validate`'s refusal to make, so that
        every verdict about the *content* is reached in one place.

    Raises:
        UploadRejectedError: With
            :attr:`~chip_chat.vision.validate.RejectionReason.TOO_LARGE` once
            the ceiling is crossed, or
            :attr:`~chip_chat.vision.validate.RejectionReason.TOO_SLOW` once the
            deadline is. Either way the stream is abandoned where it stands,
            unread.
    """
    ceilings = limits if limits is not None else UploadLimits()
    read_clock = monotonic if monotonic is not None else time.monotonic
    budget = _ReadBudget(ceilings, read_clock, declared_length)
    while True:
        budget.before_read()
        chunk = stream.read(_CHUNK_BYTES)
        if not chunk:
            return budget.finish()
        budget.take(chunk)


async def read_upload_async(
    stream: AsyncByteStream,
    *,
    declared_length: int | None = None,
    limits: UploadLimits | None = None,
    monotonic: Callable[[], float] | None = None,
) -> bytes:
    """Read one upload from an awaitable stream, under the same two ceilings.

    The async half of :func:`read_upload`, which is the one an ASGI route will
    actually call. The two share :class:`_ReadBudget` so there is one set of
    rules rather than two that drift.

    Args:
        stream: The request body, with an awaitable ``read``.
        declared_length: What the request claimed, if anything. See
            :func:`read_upload`.
        limits: The ceilings to enforce. Defaults to :class:`UploadLimits`.
        monotonic: Source of monotonic seconds. Defaults to
            :func:`time.monotonic`.

    Returns:
        The body, bounded exactly as :func:`read_upload` bounds it.

    Raises:
        UploadRejectedError: On the byte ceiling or the deadline.
    """
    ceilings = limits if limits is not None else UploadLimits()
    read_clock = monotonic if monotonic is not None else time.monotonic
    budget = _ReadBudget(ceilings, read_clock, declared_length)
    while True:
        budget.before_read()
        chunk = await stream.read(_CHUNK_BYTES)
        if not chunk:
            return budget.finish()
        budget.take(chunk)


class _ReadBudget:
    """The ceiling and the deadline, and the running totals that meet them.

    Shared by the sync and async readers. Everything an attacker can influence
    -- how big, how slow -- is decided in this one object, so the two loops
    above are only the plumbing that differs between them.
    """

    __slots__ = ("_chunks", "_deadline", "_limits", "_monotonic", "_total")

    def __init__(
        self,
        limits: UploadLimits,
        monotonic: Callable[[], float],
        declared_length: int | None,
    ) -> None:
        """Open a budget, refusing immediately on a declared length over the ceiling.

        Args:
            limits: The ceilings in force.
            monotonic: Source of monotonic seconds.
            declared_length: The claim, or ``None``.

        Raises:
            UploadRejectedError: If the sender declared more than the ceiling
                allows, in which case nothing is read at all.
        """
        self._limits = limits
        self._monotonic = monotonic
        self._deadline = monotonic() + limits.max_seconds
        self._chunks: list[bytes] = []
        self._total = 0
        # The one thing the declared length is good for: a sender who admits to
        # being oversized is believed, and costs us a comparison rather than a
        # ceiling's worth of reads.
        if declared_length is not None and declared_length > limits.max_bytes:
            raise rejection(RejectionReason.TOO_LARGE, limits)

    def before_read(self) -> None:
        """Check the deadline before asking for more.

        Raises:
            UploadRejectedError: If the upload has run out of time.
        """
        if self._monotonic() > self._deadline:
            raise rejection(RejectionReason.TOO_SLOW, self._limits)

    def take(self, chunk: bytes) -> None:
        """Accept one chunk, or refuse the whole upload.

        The chunk is counted before it is kept, so the ceiling is crossed by at
        most one chunk and the buffer never holds more than
        ``max_bytes + _CHUNK_BYTES``.

        Args:
            chunk: What the last read returned.

        Raises:
            UploadRejectedError: If the ceiling is crossed.
        """
        self._total += len(chunk)
        if self._total > self._limits.max_bytes:
            raise rejection(RejectionReason.TOO_LARGE, self._limits)
        self._chunks.append(chunk)

    def finish(self) -> bytes:
        """Join what was read.

        Returns:
            The body.

        Raises:
            UploadRejectedError: If the stream ended after the deadline. A body
                that arrived one byte at a time and happened to finish is still
                a body that held a worker for too long.
        """
        self.before_read()
        return b"".join(self._chunks)
