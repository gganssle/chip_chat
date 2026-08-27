"""The four arms of the ablation, and why they are these four.

#50's fifth scope bullet is the one this module is: *run it as an ablation
across configurations -- keyword only, vector only, hybrid, hybrid + reranker --
so the design choice is defended by data and so the fallback plan is already
measured if the reranker turns out to be unavailable.*

Two sentences, two different jobs, and both are worth stating apart.

**Defending the design.** RFC-001 §08 chose hybrid retrieval on an argument:
*keyword recall matters here more than usual, because item names are proper
nouns that embeddings handle poorly*. An argument is not a measurement. The
first three arms are what turn it into one, and the interesting cell is not the
aggregate -- it is the ingredients and allergen categories, where the labels sit
on menu rows named by proper nouns, against the two policy categories where they
do not. A hybrid that beats both halves everywhere is a nice number; a hybrid
that beats the vector half on menu questions and the keyword half on policy
questions is the argument, confirmed.

**Measuring the fallback before it is needed.** The Free tier gives 1,000
semantic requests a month and past the ceiling the API returns a billing error
rather than a charge, so :class:`~chip_chat.search.allowance.SemanticAllowance`
degrades to :data:`HYBRID` and the product keeps answering. That degradation is
already shipped and has never been scored. The gap between :data:`HYBRID` and
:data:`RERANKED` is what it costs, and knowing it in advance is the difference
between a fallback and a surprise.

**The reranker is only ever varied on top of hybrid**, which is why there are
four arms rather than six. A keyword-only semantic query is not a configuration
anybody would ship and not a fallback anybody would degrade to, so measuring one
would spend the month's allowance to fill in a cell of a table with no reader.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from chip_chat.search.query import Halves

__all__ = [
    "ABLATION",
    "HYBRID",
    "KEYWORD",
    "RERANKED",
    "SERVING",
    "VECTOR",
    "Configuration",
    "semantic_requests",
]


@dataclass(frozen=True, slots=True)
class Configuration:
    """One arm of the sweep.

    Attributes:
        name: How the report's column is headed.
        halves: Which halves of the hybrid query to send.
        rerank: Whether to ask for semantic reranking.
        costs_allowance: Whether running this arm spends the month's semantic
            requests. Derived from :attr:`rerank` and carried anyway, because
            the CLI has to be able to say *this sweep will spend 32 of your
            1,000* before it spends them.
        note: What this arm is in the table for, in one line. Printed under the
            column, because a table of four numbers with no argument attached
            is four numbers somebody averages.
    """

    name: str
    halves: Halves
    rerank: bool
    note: str

    @property
    def costs_allowance(self) -> bool:
        """Whether this arm spends the Free tier's monthly semantic requests."""
        return self.rerank


KEYWORD: Final = Configuration(
    name="keyword only",
    halves=Halves.KEYWORD,
    rerank=False,
    note="BM25 over the five searchable fields. The arm RFC-001 §08 expects to "
    "win on proper nouns and to fail on a question phrased in none of the "
    "corpus's words.",
)

VECTOR: Final = Configuration(
    name="vector only",
    halves=Halves.VECTOR,
    rerank=False,
    note="The index's own vectorizer alone. The arm expected to survive a "
    "paraphrase and to place `barbacoa` by what the embedding thinks Spanish "
    "words mean.",
)

HYBRID: Final = Configuration(
    name="hybrid",
    halves=Halves.HYBRID,
    rerank=False,
    note="Both halves, fused by reciprocal rank. **This is the degrade path** — "
    "what a visitor gets for the rest of the month once the semantic allowance "
    "is spent — so its row is a product number rather than a control.",
)

RERANKED: Final = Configuration(
    name="hybrid + reranker",
    halves=Halves.HYBRID,
    rerank=True,
    note="What production sends while the allowance lasts. The only arm whose "
    "ordering is a relevance score rather than a rank fusion, and therefore the "
    "only one whose confidence comes from the reranker floor.",
)

ABLATION: Final[tuple[Configuration, ...]] = (KEYWORD, VECTOR, HYBRID, RERANKED)
"""The four arms, in the order #50 names them and the report prints them.

Ordered so the two halves come before the thing made of them, and the fallback
comes before the configuration it falls back *from*. A reader going down the
column sees what each addition bought.
"""

SERVING: Final = RERANKED
"""The arm the product actually runs. Named so a report cannot imply otherwise.

:data:`HYBRID` is what it becomes past the ceiling; the other two are controls
and are on no serving path -- :class:`chip_chat.search.query.Halves` is where
that is enforced by argument rather than by type.
"""


def semantic_requests(configurations: Sequence[Configuration], questions: int) -> int:
    """How many of the month's 1,000 semantic requests a sweep would spend.

    Args:
        configurations: The arms to run.
        questions: How many questions each arm runs.

    Returns:
        The count. Printed before a run rather than after it: the allowance is a
        hard stop rather than an overage, so the useful moment to know a sweep
        costs 40 of 1,000 is before the sweep.
    """
    return sum(questions for arm in configurations if arm.costs_allowance)
