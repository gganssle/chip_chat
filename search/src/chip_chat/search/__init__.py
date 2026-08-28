"""The knowledge lane: what a chunk becomes, and what a question gets back.

Issues #48 and #49, RFC-001 §08. Two halves, and the alias is the seam between
them — the build is the only thing that ever knows an index name.

**The corpus** (#48). The index is rebuilt, never patched.

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
    a new index per corpus release, and one alias write to make it live.

**The query** (#49). Hybrid, reranked when there is allowance for it, cited
either way.

:mod:`chip_chat.search.query`
    one visitor sentence becomes one hybrid query — and three things it refuses
    to guess at.
:mod:`chip_chat.search.retrieve`
    the single interface the tool layer calls: passages, every score that
    ranked them, and how much the corpus actually had to say.
:mod:`chip_chat.search.fusion`
    whether the vector half of a hybrid query actually answered, read off the
    fused scores — because on the Free tier it sometimes does not, and the
    service returns HTTP 200 either way.
:mod:`chip_chat.search.allowance`
    the Free tier's 1,000 semantic requests a month, counted — because past
    them there is no bill, only a refusal.
:mod:`chip_chat.search.lane`
    the ``retriever.search`` span, and the lane that declines by itself when
    the service is down.

The application only ever knows the alias, :data:`chip_chat.search.schema.ALIAS`.
"""

from chip_chat.search.errors import SearchError

__all__ = ["SearchError"]
