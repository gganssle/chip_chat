"""Rewrite the committed catalogue fixture from the fixture site.

    uv run python catalog/tests/regenerate_fixture.py

Issue #24 asks that row counts and a sample be committed so that downstream
work can start without a live harvest. That sample is generated rather than
hand-written, and ``test_catalog_fixture.py`` regenerates it on every run and
compares — so a change to the catalogue's shape fails the suite with an
instruction rather than leaving a fixture that quietly describes last week's
schema.

Run this after any deliberate change to the tables, read the diff, and commit
it with the change that caused it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from catalog_fixtures import FIXTURES, catalog

from chip_chat.catalog import render_module
from chip_chat.harvest.blobs import LocalBlobStore

CATALOG_PREFIX = "catalog"
VOCABULARY_FILE = FIXTURES / "vision-vocabulary.py.txt"


def main() -> int:
    """Rebuild the fixture catalogue and the vocabulary module beside it.

    Returns:
        A process exit status.
    """
    built = catalog()
    written = built.write(LocalBlobStore(FIXTURES), prefix=CATALOG_PREFIX)
    VOCABULARY_FILE.write_text(
        render_module(built.vocabulary, built.content_version()), encoding="utf-8"
    )

    for name, rows in built.tables():
        print(f"{name:16} {len(rows):5} rows")
    print(f"catalog_version  {built.version()}")
    print(f"content_version  {built.content_version()}")
    print(
        f"wrote {len(written)} files to {FIXTURES / CATALOG_PREFIX} and {VOCABULARY_FILE}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
