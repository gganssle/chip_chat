"""Experiments: a configuration in, a defensible number out.

Issue [#73](https://github.com/gganssle/chip_chat/issues/73). *"I tweaked the
system prompt and it feels better"* is the sentence this package exists to
replace, and replacing it takes four things that are easy to get almost right.

**A configuration, and nothing hardcoded behind it.** The four axes #73 names --
system prompt version, model deployment, retrieval settings, matcher thresholds
-- live in ``eval/experiments/CONFIGURATIONS.json`` and reach the runner as one
value. :mod:`~chip_chat.eval.experiment.configurations` is that value, and it
carries a fingerprint for the same reason the dataset carries a version: a
configuration somebody edited and re-ran under the old name is the failure mode a
name cannot catch and a content hash cannot miss.

**A fixed thing to score against.** The rows come from the versioned dataset
(#72), never from a manifest, because two scores are comparable only if the thing
they scored is the same thing and can be shown to be.

**Three readings of one turn.** Running the golden set, the trajectory eval and
the grounding eval in sequence would spend three model calls to observe one turn
three times. :mod:`~chip_chat.eval.experiment.turns` runs each row once inside a
span recorder and hands the recording to the readers the three evals already use.
Thirty-four rows is thirty-four turns, which is the difference between a harness
somebody runs after every prompt edit and one they run before a demo.

**Two breakdowns, because one aggregate lies.** Per lane, because a 92% that
gained four points in knowledge while losing the account lane is a regression the
aggregate calls an improvement. Per requirement, because a lane is where the
architecture is and a requirement is where the product is, and they partition the
same rows differently.

```
chip_chat.eval.experiment
├── configurations  the four axes, as data with a fingerprint
├── turns           one pass over the rows, read three ways
├── run             a configuration and a factory in, an experiment out
├── results         the flattened form two runs can be compared in
├── compare         what moved, per metric, per lane, per requirement
├── report          both documents, caveats above the numbers
└── testing         a deployment that is wrong in one nameable way
```

```bash
python -m chip_chat.eval.experiment --check                 # free
python -m chip_chat.eval.experiment --ceiling --run shipped # free
python -m chip_chat.eval.experiment --compare shipped lean-lanes
```

[`eval/experiments/README.md`](../../../../experiments/README.md) is the
write-up: what an arm is, why the prompt enters the fingerprint as a digest, and
what a comparison under the routing oracle is and is not evidence of.
"""

from chip_chat.eval.experiment.compare import Comparison, compare
from chip_chat.eval.experiment.configurations import (
    ConfigurationError,
    ExperimentConfiguration,
    MatcherThresholds,
    RetrievalSettings,
    configurations,
    named,
)
from chip_chat.eval.experiment.report import render_comparison, render_result
from chip_chat.eval.experiment.results import (
    TARGETS,
    ExperimentResult,
    LaneResult,
    Metric,
    RequirementResult,
    ResultError,
    Target,
    load_result,
    write_result,
)
from chip_chat.eval.experiment.run import Experiment, run_experiment
from chip_chat.eval.experiment.turns import Recorded, record_rows

__all__ = [
    "TARGETS",
    "Comparison",
    "ConfigurationError",
    "Experiment",
    "ExperimentConfiguration",
    "ExperimentResult",
    "LaneResult",
    "MatcherThresholds",
    "Metric",
    "Recorded",
    "RequirementResult",
    "ResultError",
    "RetrievalSettings",
    "Target",
    "compare",
    "configurations",
    "load_result",
    "named",
    "record_rows",
    "render_comparison",
    "render_result",
    "run_experiment",
    "write_result",
]
