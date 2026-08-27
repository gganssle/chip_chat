"""The retrieval index: what a chunk becomes, and how a rebuild goes live.

Issue #48, RFC-001 §08. Four modules carry the design and the rest are plumbing:

:mod:`chip_chat.search.chunks`
    the chunk metadata schema #35 fixed, restated so the index can be built
    from it, and the two places an index disagrees with a Delta table.
:mod:`chip_chat.search.schema`
    the index definition, as a pure function of the above. Printable and
    reviewable with ``python -m chip_chat.search schema``, before anything is
    created.
:mod:`chip_chat.search.embedding`
    one deployment, read by the build *and* by the index's query-time
    vectorizer, so the two cannot embed into different spaces.
:mod:`chip_chat.search.build`
    the index is rebuilt, never patched: a new index per corpus release, and one
    alias write to make it live.

The application only ever knows the alias, :data:`chip_chat.search.schema.ALIAS`.
"""

from chip_chat.search.errors import SearchError

__all__ = ["SearchError"]
