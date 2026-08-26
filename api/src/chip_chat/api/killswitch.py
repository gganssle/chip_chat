"""The circuit breaker: one manual flip that puts the whole app in the stop state.

RFC-001 section 11 asks for a kill switch "reachable without a deploy", and that
last clause is the requirement rather than a nicety. The scenario it is written
for is noticing an invoice-shaped problem from a phone: whatever the switch is,
flipping it has to take a minute, not a build.

So every implementation here re-reads its source on *every* check. None of them
caches a value at import time, none needs a restart, and
:class:`CachedKillSwitch` exists to make "cheap enough to check on every request"
and "responds within seconds" compatible, rather than trading one away.

Which source a deployment uses is its own business -- an app setting, a file on
a mounted share, a Key Vault reference, an ops endpoint that calls
:meth:`ManualKillSwitch.throw`. :func:`any_of` combines them, and any one of
them stops the app.
"""

import os
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from chip_chat.api.clock import Clock, SystemClock

__all__ = [
    "CachedKillSwitch",
    "EnvironmentKillSwitch",
    "FileKillSwitch",
    "KillSwitch",
    "ManualKillSwitch",
    "any_of",
]

KILL_SWITCH_VARIABLE = "CHIP_CHAT_KILL_SWITCH"
"""The application setting :class:`EnvironmentKillSwitch` reads."""

_FALSEY = frozenset({"", "0", "false", "no", "off", "run"})


class KillSwitch(Protocol):
    """Answers one question, and is asked it before every turn."""

    def is_thrown(self) -> bool:
        """True when the app must serve the stop state and call no model."""
        ...


def _reads_as_thrown(raw: str) -> bool:
    """Interpret a switch's textual value.

    Anything that is not recognisably "off" counts as thrown. A value nobody
    can parse should stop the spending, not continue it -- that asymmetry is
    the whole point of an emergency stop.
    """
    return raw.strip().lower() not in _FALSEY


class ManualKillSwitch:
    """An in-process switch, flipped by an ops endpoint or by a test.

    The definitive one: no filesystem, no environment, nothing that can fail to
    be read. What it lacks is durability -- a restart clears it -- so a
    deployment pairs it with a switch whose source outlives the process.
    """

    __slots__ = ("_thrown",)

    def __init__(self, *, thrown: bool = False) -> None:
        """Initialise the switch.

        Args:
            thrown: Whether the app starts in the stop state.
        """
        # A plain bool assignment is atomic under the GIL; the flip is a single
        # store and readers see one value or the other, never a torn one.
        self._thrown = thrown

    def is_thrown(self) -> bool:
        return self._thrown

    def throw(self) -> None:
        """Stop the app. Takes effect on the next check, with no restart."""
        self._thrown = True

    def reset(self) -> None:
        """Let the app serve again."""
        self._thrown = False


class EnvironmentKillSwitch:
    """Reads ``CHIP_CHAT_KILL_SWITCH`` from the environment on every check.

    On App Service or Container Apps this is an application setting, editable
    from the portal on a phone. Note that changing one restarts the container,
    which is fine -- the app comes back already stopped, because the value is
    read at the moment of the check and not at import.
    """

    __slots__ = ("_env", "_variable")

    def __init__(
        self,
        variable: str = KILL_SWITCH_VARIABLE,
        env: Mapping[str, str] | None = None,
    ) -> None:
        """Initialise the switch.

        Args:
            variable: The variable to read.
            env: Environment mapping; defaults to live :data:`os.environ`.
        """
        self._variable = variable
        self._env = env

    def is_thrown(self) -> bool:
        source = os.environ if self._env is None else self._env
        raw = source.get(self._variable)
        return raw is not None and _reads_as_thrown(raw)


class FileKillSwitch:
    """Reads a file on every check; the file appearing is the flip.

    Written for a mounted share or a projected secret, where creating a
    one-byte file is something a phone can do and a deploy is not.

    An unreadable path is treated as *not* thrown. That is the uncomfortable
    choice and it is deliberate: a typo in the path would otherwise take the
    demo down permanently and silently, and the money is still bounded by the
    daily ceiling either way. Pair this with :class:`ManualKillSwitch` when a
    stop must be certain.
    """

    __slots__ = ("_path",)

    def __init__(self, path: Path | str) -> None:
        """Initialise the switch.

        Args:
            path: The file whose presence stops the app. Its contents may say
                ``off`` to disarm it without deleting it.
        """
        self._path = Path(path)

    def is_thrown(self) -> bool:
        try:
            raw = self._path.read_text()
        except FileNotFoundError:
            return False
        except OSError:
            return False
        return _reads_as_thrown(raw)


class CachedKillSwitch:
    """Memoises another switch for a few seconds.

    A file read or an environment lookup per request is not free, and a switch
    that is expensive to check is a switch somebody eventually checks less
    often. Bounding the staleness instead keeps both properties: the underlying
    source is still re-read, just not more than once per ``ttl_seconds``, and
    the requirement is a minute rather than a millisecond.
    """

    __slots__ = ("_clock", "_expires_at", "_inner", "_lock", "_thrown", "_ttl")

    def __init__(
        self,
        inner: KillSwitch,
        ttl_seconds: float = 5.0,
        clock: Clock | None = None,
    ) -> None:
        """Initialise the cache.

        Args:
            inner: The switch to consult.
            ttl_seconds: How long an answer may be reused. Must be positive.
            clock: Source of monotonic time. Defaults to the system clock.

        Raises:
            ValueError: If ``ttl_seconds`` is not positive.
        """
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")
        self._inner = inner
        self._ttl = ttl_seconds
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._lock = threading.Lock()
        self._thrown = False
        self._expires_at = float("-inf")

    def is_thrown(self) -> bool:
        with self._lock:
            now = self._clock.monotonic()
            if now >= self._expires_at:
                self._thrown = self._inner.is_thrown()
                self._expires_at = now + self._ttl
            return self._thrown

    def invalidate(self) -> None:
        """Drop the cached answer, so the next check re-reads the source."""
        with self._lock:
            self._expires_at = float("-inf")


class _AnyKillSwitch:
    """Thrown when any of several switches is."""

    __slots__ = ("_switches",)

    def __init__(self, switches: Sequence[KillSwitch]) -> None:
        self._switches = tuple(switches)

    def is_thrown(self) -> bool:
        return any(switch.is_thrown() for switch in self._switches)


def any_of(*switches: KillSwitch) -> KillSwitch:
    """Combine switches so that any one of them stops the app.

    Args:
        *switches: The switches to consult, in the order they are checked.

    Returns:
        A switch that is thrown when any argument is. With no arguments, one
        that is never thrown.
    """
    return _AnyKillSwitch(switches)
