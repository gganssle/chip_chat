"""Reading `snowflake/sql/` as text, for the two tests that hold it to Python.

Both tests here parse SQL rather than execute it, and both want the same two
things first: the statements, and the statements with their whitespace
collapsed so a column-aligned file can be searched with a substring.

The splitting is quote-aware, and that is not fastidiousness. Issue #42's DDL
carries a comment on every table and every column -- they are what #45's
semantic view retrieves against -- and English contains semicolons. Split
naively, ``COMMENT 'lift above one means the pair turns up together; one means
it does not'`` becomes two statements, the second of which begins with the word
``one``. Nothing fails today, and what eventually fails is a test asserting
something about a fragment of a sentence, which is a bad hour for whoever hits
it.

Apostrophes inside a SQL string are doubled, and a doubled quote is two
consecutive quote characters rather than an escape, so a state machine that
simply flips on every quote gets it right without a special case.
"""

import re
from dataclasses import dataclass

__all__ = ["Declared", "declared_tables", "flat", "privileges", "statements"]


def statements(source: str) -> list[str]:
    """Return ``source``'s statements, comments stripped, whitespace collapsed.

    Comments are most of these files by volume and all of the words "GRANT"
    that are not grants, so removing them first is what keeps the grant parser
    from reading the prose above it. String literals are kept whole, including
    any semicolon inside one.
    """
    uncommented = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("--")
    )
    found: list[str] = []
    current: list[str] = []
    in_string = False
    for character in uncommented:
        if character == "'":
            in_string = not in_string
        if character == ";" and not in_string:
            found.append("".join(current))
            current = []
            continue
        current.append(character)
    found.append("".join(current))
    return [
        re.sub(r"\s+", " ", statement).strip() for statement in found if statement.strip()
    ]


def flat(source: str) -> str:
    """Return ``source``'s statements as one string, whitespace collapsed.

    The SQL is column-aligned so that the grant table reads as a table, which
    means ``GRANT ROLE CHIP_CHAT_WRITE   TO USER`` has three spaces in it and a
    naive substring check misses. Every existence assertion runs against this
    rather than against the file.
    """
    return " ; ".join(statements(source))


def privileges(clause: str) -> set[str]:
    """Split the privilege list of a GRANT into its parts."""
    return {part.strip().upper() for part in clause.split(",") if part.strip()}


@dataclass(frozen=True, slots=True)
class Declared:
    """One ``CREATE OR ALTER TABLE`` as the DDL wrote it.

    Attributes:
        name: The table name, as written.
        columns: ``(name, type, not_null)`` per column, in declaration order.
        comments: Column name to the text of its ``COMMENT``, for the columns
            that carry one.
        table_comment: The table's own ``COMMENT =``, or the empty string.
        key: The columns of the declared ``PRIMARY KEY``.
        body: The raw text between the parentheses, for anything else a test
            wants to ask.
    """

    name: str
    columns: tuple[tuple[str, str, bool], ...]
    comments: dict[str, str]
    table_comment: str
    key: tuple[str, ...]
    body: str


_TABLE = re.compile(
    r"CREATE OR ALTER TABLE (?P<name>\w+) \((?P<body>.*?)\n\)\n"
    r"COMMENT = '(?P<comment>.*?)';",
    re.DOTALL,
)
_COLUMN = re.compile(
    r"^    (?P<name>\w+) (?P<type>[A-Z_]+(?:\(\d+,\d+\))?)(?P<required> NOT NULL)?$"
)


def declared_tables(source: str) -> list[Declared]:
    """Return every table one DDL file declares, in the order it declares them."""
    found: list[Declared] = []
    for match in _TABLE.finditer(source):
        body = match.group("body")
        columns: list[tuple[str, str, bool]] = []
        comments: dict[str, str] = {}
        lines = body.splitlines()
        for index, line in enumerate(lines):
            column = _COLUMN.match(line)
            if column is None:
                continue
            columns.append(
                (
                    column.group("name"),
                    column.group("type"),
                    bool(column.group("required")),
                )
            )
            following = lines[index + 1] if index + 1 < len(lines) else ""
            comment = re.match(r"^        COMMENT '(.*)',?$", following)
            if comment:
                comments[column.group("name")] = comment.group(1)
        primary = re.search(r"PRIMARY KEY \((?P<key>[^)]*)\)", body)
        found.append(
            Declared(
                name=match.group("name"),
                columns=tuple(columns),
                comments=comments,
                table_comment=match.group("comment"),
                key=tuple(part.strip() for part in primary.group("key").split(","))
                if primary
                else (),
                body=body,
            )
        )
    return found
