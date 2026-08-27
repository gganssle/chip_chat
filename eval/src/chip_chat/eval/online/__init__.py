"""Online evals: the ones that only matter because you went public.

Issue [#76](https://github.com/gganssle/chip_chat/issues/76). Every other set in
``eval/`` scores questions somebody wrote down. This one scores questions nobody
wrote down, and the difference is not a matter of degree: there is no expected
lane, no labelled refusal direction and no ground truth of any kind on a
stranger's turn, so a monitor here may only fire on something wrong **on its
face**.

Five of those, and #76 names them rather than leaving them generic — an
ungrounded menu claim; a photo match with no confident SKU; a refusal where the
corpus plainly had the answer; a cross-visitor disclosure signal; latency and
cost breaching their targets. Three need no model and therefore run on **every**
turn, which is what makes launch gate one mean anything in production: a
disclosure monitor sampling a fifth of traffic misses four disclosures in five.

```
chip_chat.eval.online
├── signals   one live turn, as the thing a monitor may look at
├── sampling  which turns get a judge, and the arithmetic behind the rate
├── monitors  the five fears, each as a condition that can fire
├── budget    judge tokens, inside the daily cap rather than beside it
├── run       sample, judge, monitor, alert -- and count what it cost
└── testing   each condition produced deliberately, so each monitor is demonstrated
```

```bash
python -m chip_chat.eval.online --check   # free: policy, monitors, budget
python -m chip_chat.eval.online --drill   # free: every condition, produced
```

Two things this package deliberately does not do. It does not **deliver** an
alert: severity is a routing decision and the route is somebody's action group,
so the run hands back alerts and the caller routes them. And it does not
**enforce** the spend cap: that is inline in the request path, in
``chip_chat.api.limits``, because a ceiling can only refuse a call before it is
made. What :mod:`~chip_chat.eval.online.budget` does is the arithmetic that says
whether the judges fit inside it, which is the thing #76 asks for and the thing
that is otherwise a paragraph nobody can check.

[`eval/online/README.md`](../../../../online/README.md) is the write-up: why the
rate is 20%, which three classes ignore it, and what a drill is and is not
evidence of.
"""

from chip_chat.eval.online.budget import JudgeBudget, budget_from_env
from chip_chat.eval.online.monitors import MONITORS, Alert, Monitor, Severity, evaluate
from chip_chat.eval.online.run import OnlineRun, Scored, run_online
from chip_chat.eval.online.sampling import (
    DEFAULT_RATE,
    Reason,
    SamplingDecision,
    SamplingPolicy,
)
from chip_chat.eval.online.signals import LiveTurn, read_turn

__all__ = [
    "DEFAULT_RATE",
    "MONITORS",
    "Alert",
    "JudgeBudget",
    "LiveTurn",
    "Monitor",
    "OnlineRun",
    "Reason",
    "SamplingDecision",
    "SamplingPolicy",
    "Scored",
    "Severity",
    "budget_from_env",
    "evaluate",
    "read_turn",
    "run_online",
]
