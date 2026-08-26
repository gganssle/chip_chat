"""Talking to Snowflake through the `snow` CLI, and nothing heavier.

There is a Python driver, and this deliberately does not use it. The connection
is already defined once, in `~/.snowflake/config.toml`, by ``snow connection
add`` -- account identifier, user, key pair, the lot -- and `.env.example` says
so: "only names go here". Reaching for the driver would mean a second place that
knows how to authenticate, a second thing to keep in step with a rotated key,
and a dependency in the lockfile whose only job is to re-derive what a
configuration file on the machine already says.

So this shells out. `snow sql` exits non-zero on the first failing statement and
stops there, which is exactly the behaviour an apply script wants and saves this
module from having to decide what a half-run file means.

Two details the callers depend on:

**Each call is a fresh session.** ``USE ROLE`` in one call does not carry into
the next. `verify` relies on that -- a probe that must be refused runs in its own
process with its own preamble, so nothing a previous probe did can be what let it
through.

**`--format json` returns rows per statement.** One statement gives a flat list
of row objects; two or more give a list of those lists. :func:`query`
normalises to the second shape so a caller can index by statement without
knowing how many it sent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_CONNECTION",
    "SnowError",
    "connection_name",
    "query",
    "require_cli",
    "run_file",
    "run_statements",
]

DEFAULT_CONNECTION = "chipchat"
"""The connection ``snow connection add`` was run with. Overridable by env so a
second account -- a rebuild after this trial expires -- needs no code change."""


def connection_name() -> str:
    """Return the `snow` connection to use.

    Reads ``SNOWFLAKE_CONNECTION_NAME``, falling back to
    :data:`DEFAULT_CONNECTION`.
    """
    return os.environ.get("SNOWFLAKE_CONNECTION_NAME") or DEFAULT_CONNECTION


class SnowError(RuntimeError):
    """A `snow` invocation failed.

    Attributes:
        output: Everything the CLI wrote, both streams. Snowflake's own error
            text is the useful part and it is not always on stderr.
    """

    def __init__(self, message: str, output: str) -> None:
        super().__init__(f"{message}\n{output.strip()}")
        self.output = output


@dataclass(frozen=True, slots=True)
class Completed:
    """The outcome of one `snow` invocation.

    Attributes:
        ok: Whether the CLI exited zero.
        output: stdout and stderr, interleaved as the CLI wrote them.
    """

    ok: bool
    output: str


def require_cli() -> None:
    """Fail early, and with the install command, if `snow` is not on PATH.

    Raises:
        SnowError: If the Snowflake CLI is not installed.
    """
    if shutil.which("snow") is None:
        raise SnowError(
            "the Snowflake CLI is not on PATH",
            "Install it with `brew install snowflake-cli`, then define the "
            "connection:\n"
            "  snow connection add --connection-name chipchat\n"
            "docs/snowflake-account.md section 2 has the values it asks for.",
        )


def _invoke(arguments: list[str]) -> Completed:
    """Run `snow` with ``arguments`` and capture both streams."""
    require_cli()
    completed = subprocess.run(
        ["snow", *arguments, "--connection", connection_name()],
        capture_output=True,
        text=True,
        check=False,
    )
    return Completed(
        ok=completed.returncode == 0,
        output=completed.stdout + completed.stderr,
    )


def run_file(path: Path, variables: dict[str, str] | None = None) -> str:
    """Run every statement in ``path``, stopping at the first failure.

    Args:
        path: A file under `snowflake/sql/`.
        variables: Values for ``<% name %>`` placeholders, if the file has any.

    Returns:
        Everything the CLI printed, for a caller that wants to show it.

    Raises:
        SnowError: If any statement failed. The account is then left in whatever
            state the statements before it reached -- which is safe here because
            every file in `snowflake/sql/` is re-runnable.
    """
    arguments = ["sql", "--filename", str(path)]
    for name, value in (variables or {}).items():
        arguments += ["--variable", f"{name}={value}"]
    result = _invoke(arguments)
    if not result.ok:
        raise SnowError(f"{path.name} failed", result.output)
    return result.output


def run_statements(sql: str) -> Completed:
    """Run ``sql`` in one fresh session and report whether it succeeded.

    Unlike :func:`run_file` this does not raise on failure: its callers are
    checks that expect a refusal, for which a non-zero exit is the result rather
    than the problem.
    """
    return _invoke(["sql", "--query", sql])


def query(sql: str) -> list[list[dict[str, Any]]]:
    """Run ``sql`` and return its rows, one list per statement.

    Args:
        sql: One or more statements, semicolon separated.

    Returns:
        A list with one entry per statement, each a list of row mappings.
        Statements that return nothing contribute an empty list.

    Raises:
        SnowError: If any statement failed, or if the CLI returned something
            that is not the JSON this function knows how to read.
    """
    result = _invoke(["sql", "--query", sql, "--format", "json"])
    if not result.ok:
        raise SnowError("query failed", result.output)
    try:
        parsed = json.loads(result.output)
    except json.JSONDecodeError as error:
        raise SnowError("the CLI did not return JSON", result.output) from error

    if not isinstance(parsed, list):
        raise SnowError("the CLI returned JSON that is not a list", result.output)
    # One statement gives a flat list of rows; several give a list of those
    # lists. Normalise upward so callers can always index by statement.
    if parsed and all(isinstance(row, dict) for row in parsed):
        return [parsed]
    return [statement for statement in parsed if isinstance(statement, list)]
