"""Promotion: the loop that makes going public pay for itself.

Issue [#77](https://github.com/gganssle/chip_chat/issues/77). The golden set was
written from imagination, because there was nothing else to write it from.
Strangers will ask things it never imagined, the online evals in #76 will score
some of those badly, and the ones scored badly are the best cases nobody has
written yet. This package is the path from one to the other.

**Two minutes is the design constraint.** #77 says so twice, and it rules out the
obvious design. So the split is by who can supply what: the trace supplies the
message, the persona, the lane it actually took and the tool it actually called;
a person supplies the three things a trace cannot -- which PRD requirements this
covers, what has to be observed for it to count as passed, and why the case is
worth having. Two commands and one edit between them.

**What the agent did is never the label.** The interesting traces are the ones
where what it did was wrong; that is why a monitor flagged them. A draft that
wrote the observed tool into ``tool`` would promote the bug as the expected
behaviour, so the observation is carried under a different name and the promotion
refuses a draft that still says `TODO`.

**Provenance lives beside the dataset, never inside it.** The dataset's version is
a hash of its entries and an entry's digest is a hash of its columns, so a
``provenance`` column would rebase every existing digest the day the first trace
was promoted. `eval/dataset/PROVENANCE.json` is the ledger; the only thing that
moves when a case is added is the version, because there is one more row.

```
chip_chat.eval.promote
├── candidates  a flagged turn, and the draft it becomes
├── ledger      where every entry came from, and which sources are permanent
└── apply       validation, the append, and the provenance row -- in that order
```

```bash
python -m chip_chat.eval.promote --check                       # free, and in CI
python -m chip_chat.eval.promote --drafts capture.json > cases.json
python -m chip_chat.eval.promote --apply cases.json && make dataset
```

[`eval/dataset/README.md`](../../../../dataset/README.md) is the write-up for the
dataset; the promotion path's own argument is in the module docstrings above.
"""

from chip_chat.eval.promote.apply import PromotionError, apply_draft, traffic_entries
from chip_chat.eval.promote.candidates import (
    NEEDS_A_HUMAN,
    Candidate,
    draft,
    from_alerts,
)
from chip_chat.eval.promote.ledger import (
    DEFAULT_LEDGER,
    LedgerError,
    PermanentSource,
    Promotion,
    Provenance,
    check,
    load,
    write,
)

__all__ = [
    "DEFAULT_LEDGER",
    "NEEDS_A_HUMAN",
    "Candidate",
    "LedgerError",
    "PermanentSource",
    "Promotion",
    "PromotionError",
    "Provenance",
    "apply_draft",
    "check",
    "draft",
    "from_alerts",
    "load",
    "traffic_entries",
    "write",
]
