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


# ---------------------------------------------------------------------------
# The semantic view -- #45's one statement, which is bigger than some files
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SemanticTable:
    """One logical table of a ``CREATE SEMANTIC VIEW``.

    Attributes:
        alias: What the view calls it.
        table: The physical table it stands for, as written.
        key: The declared ``PRIMARY KEY``.
        synonyms: The ``WITH SYNONYMS`` list.
        comment: The ``COMMENT``.
    """

    alias: str
    table: str
    key: tuple[str, ...]
    synonyms: tuple[str, ...]
    comment: str


@dataclass(frozen=True, slots=True)
class SemanticElement:
    """One fact, dimension or metric.

    Attributes:
        kind: ``FACT``, ``DIMENSION`` or ``METRIC``.
        table: The logical table alias it hangs off.
        name: The element name.
        using: The relationships named in a metric's ``USING`` clause.
        expression: The SQL after ``AS``, whitespace collapsed.
        synonyms: The ``WITH SYNONYMS`` list.
        comment: The ``COMMENT``.
    """

    kind: str
    table: str
    name: str
    using: tuple[str, ...]
    expression: str
    synonyms: tuple[str, ...]
    comment: str


@dataclass(frozen=True, slots=True)
class SemanticVerifiedQuery:
    """One entry of ``AI_VERIFIED_QUERIES``.

    Attributes:
        name: The verified query name.
        question: Its ``QUESTION``.
        verified_at: Its ``VERIFIED_AT``, or zero.
        onboarding: Whether ``ONBOARDING_QUESTION TRUE``.
        sql: Its ``SQL``, whitespace collapsed and doubled quotes undoubled.
    """

    name: str
    question: str
    verified_at: int
    onboarding: bool
    sql: str


@dataclass(frozen=True, slots=True)
class SemanticView:
    """A whole ``CREATE SEMANTIC VIEW``, parsed.

    Attributes:
        name: The object name, as written after ``CREATE OR REPLACE``.
        tables: The ``TABLES`` clause.
        relationships: ``(name, table, columns, references)`` per relationship.
        elements: Every fact, dimension and metric, in declaration order.
        verified: The ``AI_VERIFIED_QUERIES`` clause.
        comment: The view's own ``COMMENT``.
        sql_generation: ``AI_SQL_GENERATION``.
        question_categorization: ``AI_QUESTION_CATEGORIZATION``.
        copy_grants: Whether the statement ends ``COPY GRANTS``.
    """

    name: str
    tables: tuple[SemanticTable, ...]
    relationships: tuple[tuple[str, str, tuple[str, ...], str], ...]
    elements: tuple[SemanticElement, ...]
    verified: tuple[SemanticVerifiedQuery, ...]
    comment: str
    sql_generation: str
    question_categorization: str
    copy_grants: bool


def _split_top_level(body: str) -> list[str]:
    """Split on the commas that are outside every paren and every string."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    in_string = False
    for character in body:
        if character == "'":
            in_string = not in_string
        elif not in_string and character in "([":
            depth += 1
        elif not in_string and character in ")]":
            depth -= 1
        if character == "," and depth == 0 and not in_string:
            parts.append("".join(current))
            current = []
            continue
        current.append(character)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _clause(source: str, keyword: str) -> str:
    """Return the balanced contents of ``keyword ( ... )``, or the empty string."""
    match = re.search(rf"\b{keyword}\s*\(", source)
    if match is None:
        return ""
    depth = 0
    in_string = False
    start = match.end()
    for index in range(match.end() - 1, len(source)):
        character = source[index]
        if character == "'":
            in_string = not in_string
            continue
        if in_string:
            continue
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return source[start:index]
    return ""


def _quoted_list(text: str) -> tuple[str, ...]:
    """Return the strings of a ``('a', 'b')`` list, undoubling quotes."""
    return tuple(
        value.replace("''", "'") for value in re.findall(r"'((?:[^']|'')*)'", text or "")
    )


def _string_after(source: str, keyword: str) -> str:
    """Return the single-quoted literal following ``keyword``, undoubled."""
    match = re.search(rf"\b{keyword}\s+'((?:[^']|'')*)'", source)
    return match.group(1).replace("''", "'") if match else ""


_SEMANTIC_TABLE = re.compile(
    r"^(?P<alias>\w+)\s+AS\s+(?P<table>[\w.]+)\s+"
    r"PRIMARY KEY\s*\((?P<key>[^)]*)\)"
    r"(?:\s+WITH SYNONYMS\s*=\s*\((?P<synonyms>.*?)\))?"
    r"(?:\s+COMMENT\s*=\s*'(?P<comment>(?:[^']|'')*)')?\s*$",
    re.DOTALL,
)
_SEMANTIC_ELEMENT = re.compile(
    r"^(?P<table>\w+)\.(?P<name>\w+)"
    r"(?:\s+USING\s*\((?P<using>[^)]*)\))?"
    r"\s+AS\s+(?P<expression>.*?)"
    r"(?:\s+WITH SYNONYMS\s*=\s*\((?P<synonyms>[^)]*)\))?"
    r"(?:\s+COMMENT\s*=\s*'(?P<comment>(?:[^']|'')*)')?\s*$",
    re.DOTALL,
)
_RELATIONSHIP = re.compile(
    r"^(?P<name>\w+)\s+AS\s+(?P<table>\w+)\s*\((?P<columns>[^)]*)\)\s*"
    r"REFERENCES\s+(?P<references>\w+)\s*$",
    re.DOTALL,
)
_VERIFIED = re.compile(r"^(?P<name>\w+)\s+AS\s*\((?P<body>.*)\)\s*$", re.DOTALL)


def _first_statement(source: str) -> str:
    """Return ``source`` up to its first semicolon that is outside a string.

    `statements` splits the same way and the module docstring says why: a
    ``COMMENT`` in this file reads "Present so that a single order can be
    quoted; do not sum it", and a naive split ends the semantic view there --
    after one fact, with every clause below it invisible and every assertion
    over them vacuously true.
    """
    in_string = False
    for index, character in enumerate(source):
        if character == "'":
            in_string = not in_string
        elif character == ";" and not in_string:
            return source[:index]
    return source


_CLAUSES = (
    "TABLES",
    "RELATIONSHIPS",
    "FACTS",
    "DIMENSIONS",
    "METRICS",
    "AI_VERIFIED_QUERIES",
)


def _outer(statement: str) -> str:
    """Return the statement with every clause body blanked out.

    What is left is the view's own ``COMMENT`` and the two ``AI_`` strings,
    which would otherwise be found by the first logical table's ``COMMENT``.
    """
    remaining = statement
    for keyword in _CLAUSES:
        body = _clause(remaining, keyword)
        if body:
            remaining = remaining.replace(body, " ", 1)
    return remaining


def semantic_view(source: str) -> SemanticView:
    """Parse the one ``CREATE SEMANTIC VIEW`` in ``source``.

    Comments are stripped first, for the same reason `statements` strips them:
    the prose above this statement is longer than the statement and contains
    every word the regexes below look for.

    Raises:
        ValueError: If the file declares no semantic view, or if an entry in
            one of its clauses does not parse. Both are failures rather than
            skips -- a parser that quietly matches nothing turns every
            assertion built on it into a pass.
    """
    body = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("--")
    )
    header = re.search(r"CREATE OR REPLACE SEMANTIC VIEW\s+(\w+)", body)
    if header is None:
        raise ValueError("no CREATE OR REPLACE SEMANTIC VIEW in this file")

    statement = _first_statement(body[header.start() :])

    tables = []
    for entry in _split_top_level(_clause(statement, "TABLES")):
        match = _SEMANTIC_TABLE.match(entry)
        if match is None:
            raise ValueError(f"unparsed logical table: {entry[:80]!r}")
        tables.append(
            SemanticTable(
                alias=match.group("alias"),
                table=match.group("table"),
                key=tuple(part.strip() for part in match.group("key").split(",")),
                synonyms=_quoted_list(match.group("synonyms")),
                comment=(match.group("comment") or "").replace("''", "'"),
            )
        )

    relationships = []
    for entry in _split_top_level(_clause(statement, "RELATIONSHIPS")):
        match = _RELATIONSHIP.match(re.sub(r"\s+", " ", entry).strip())
        if match is None:
            raise ValueError(f"unparsed relationship: {entry[:80]!r}")
        relationships.append(
            (
                match.group("name"),
                match.group("table"),
                tuple(part.strip() for part in match.group("columns").split(",")),
                match.group("references"),
            )
        )

    elements = []
    for kind in ("FACTS", "DIMENSIONS", "METRICS"):
        for entry in _split_top_level(_clause(statement, kind)):
            match = _SEMANTIC_ELEMENT.match(entry)
            if match is None:
                raise ValueError(f"unparsed {kind[:-1].lower()}: {entry[:80]!r}")
            using = match.group("using") or ""
            elements.append(
                SemanticElement(
                    kind=kind[:-1],
                    table=match.group("table"),
                    name=match.group("name"),
                    using=tuple(
                        part.strip() for part in using.split(",") if part.strip()
                    ),
                    expression=re.sub(r"\s+", " ", match.group("expression")).strip(),
                    synonyms=_quoted_list(match.group("synonyms")),
                    comment=(match.group("comment") or "").replace("''", "'"),
                )
            )

    verified = []
    for entry in _split_top_level(_clause(statement, "AI_VERIFIED_QUERIES")):
        match = _VERIFIED.match(entry)
        if match is None:
            raise ValueError(f"unparsed verified query: {entry[:80]!r}")
        inner = match.group("body")
        at = re.search(r"\bVERIFIED_AT\s+(\d+)", inner)
        verified.append(
            SemanticVerifiedQuery(
                name=match.group("name"),
                question=_string_after(inner, "QUESTION"),
                verified_at=int(at.group(1)) if at else 0,
                onboarding=bool(re.search(r"\bONBOARDING_QUESTION\s+TRUE\b", inner)),
                sql=re.sub(r"\s+", " ", _string_after(inner, "SQL")).strip(),
            )
        )

    outer = _outer(statement)
    return SemanticView(
        name=header.group(1),
        tables=tuple(tables),
        relationships=tuple(relationships),
        elements=tuple(elements),
        verified=tuple(verified),
        comment=_string_after(outer, "COMMENT ="),
        sql_generation=_string_after(outer, "AI_SQL_GENERATION"),
        question_categorization=_string_after(outer, "AI_QUESTION_CATEGORIZATION"),
        copy_grants="COPY GRANTS" in outer,
    )
