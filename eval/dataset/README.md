# The versioned dataset

Issue [#72](https://github.com/gganssle/chip_chat/issues/72). The golden set and
the labeled photo set, promoted into one dataset with a version on it, so that
[#73](https://github.com/gganssle/chip_chat/issues/73) can run a prompt change
as an experiment and the comparison means something.

```bash
make dataset-check      # free; CI runs it
make dataset            # rebuild DATASET.json after adding a case or a frame
make dataset-upload     # create the dataset in Arize, or add a version to it
```

`DATASET.json` is the build, and it is committed. Everything else here is why.

---

## The one command, and the three steps inside it

```bash
python -m chip_chat.eval.dataset --check       # build it, print it, hold the repo to it
python -m chip_chat.eval.dataset --write       # refresh the committed build
export ARIZE_API_KEY=...  ARIZE_SPACE_ID=...
uv run --with arize python -m chip_chat.eval.dataset --upload
```

The dataset is a pure function of two committed files —
[`../golden/cases.json`](../golden/cases.json) and
[`../photos/labels.json`](../photos/labels.json). No network, no model, no clock.
The same clone produces the same version, which is what makes #72's *"reproducible
from the repo, not a one-off manual import"* a property rather than an intention.

`--with arize` rather than a dependency: the SDK constrains protobuf hard enough
to drag the whole workspace's pin backwards, and paying that on every install for
a command CI never runs is the wrong trade. The note in
[`../pyproject.toml`](../pyproject.toml) is there so nobody adds it back without
the argument.

## What a version is

The version is a hash of the entries — twelve hex characters, printed by every
command and stamped onto every uploaded row.

Nobody increments it. That is the whole point. An ordinal reads better on a chart
axis and is wrong exactly when it matters: the day somebody edits a case, ships
it, and forgets. A content hash cannot be forgotten, and two builds of the same
entries agree without anybody arranging it.

`DATASET.json` is committed so that git can answer the question nothing else can:
**which version was current when this score was taken**. `--check` fails when the
committed file is not what the manifests build, because a version that only moves
when somebody remembers to regenerate a file is not a version.

## What the upload will not do

**It will not change an entry that is already published.** If
`k1-bowl-ingredients` asks a different question this week than last week, then
every score recorded against `k1-bowl-ingredients` before this week measured a
question that no longer exists — and neither the dataset, nor the experiment, nor
the chart will say so. So the publish refuses and names the entries.

A changed question is a new question. Give it a new id, let the old one keep the
scores it earned, and delete it later if it is genuinely dead. The seam behind
`publish` has exactly two operations — create a dataset, add a version — and no
operation that replaces a row, so this is a rule the code cannot break rather
than one it is asked to follow.

**It will not upload a set with an uncovered PRD requirement.** #29's first
acceptance criterion, enforced where it now costs something: an uploaded dataset
is what #73, #74 and #75 quote numbers from, and a requirement covered by nothing
and delegated nowhere is a hole those numbers cannot see.

**It will not re-upload rows the dataset already holds.** A publish sends the
difference. Running it twice on the same build is a no-op that says so, because
re-running a command after a network failure is a thing people do.

## What every row carries

#72's third acceptance criterion is *every entry carries its expected lane and
its PRD requirement id*, and both are columns:

| Column | What it holds |
| --- | --- |
| `entry_id` | `golden/<case id>` or `photos/<photo id>`. The join key, stable across versions. |
| `origin` | Which set it came from. |
| `input`, `input_kind` | The visitor's message, or an image path relative to the photo manifest. |
| `expected_lane` | One of the five, or `none`. Never blank. |
| `expected_tool` | The tool the turn should reach for — where routing is scoreable at all. |
| `scores_routing` | Whether it is. See below. |
| `requirements` | The PRD identifiers, as a JSON array. At least one, always. |
| `checks`, `judged_checks` | What has to be observed, and which of those need a judge. |
| `persona`, `context`, `confirmed` | The state the question presupposes. |
| `forbidden_tools` | The confusable half of a boundary case. |
| `menu_terms` | Published terms the entry leans on. |
| `frame_truth` | A photograph's labeled slots, conditions and provenance, as JSON. |
| `entry_digest` | A hash of everything above. How a changed entry is detected. |
| `dataset_version` | The build this row arrived in. |

Composite values are JSON strings rather than nested objects, because the far
side of the seam is a table and a cell holding a list is a cell every consumer
has to guess at. JSON is that guess, made once.

### Why a photograph carries no expected tool

`scores_routing` is `true` for every golden entry — including the ones expecting
no tool at all, because *"call nothing"* is an answer routing can be wrong about
— and `false` for every photograph.

The labeled photo set runs the vision lane **directly**, from a blob reference
through stages 4 and 5. No model ever chose to call `match_meal_from_photo`, so
lane selection is not a thing those rows can be scored on, and an
`expected_tool` on them would invite a tool-selection number computed over turns
where no tool was selected. The section of [`../README.md`](../README.md) headed
*Where the line between them is* draws the same line for the same reason; the
golden set's single vision case is the one that scores routing, and it delegates
`V2`–`V7` to the frames by name.

The frames still carry requirement ids — derived from what each frame *is*, since
the photo set has no requirement field and putting one there would be the same
fact in two files. An orderable frame is evidence for `V2` and `V3`; a
not-Chipotle frame for `V4`; a frame with an unreadable required slot for `V5`; a
multi-meal frame for `V7`.

## Status

`labels.json` holds no frames yet — see [`../photos/README.md`](../photos/README.md)
for what to shoot. So today's dataset is the thirty-four golden cases and nothing
else, and `--check` prints the twelve unmet frame clauses every time it runs.

That is reported rather than fatal. `make photos-check` is the gate that owns the
photo set's completeness; making the dataset's own check fail on it too would
mean the golden half could not be uploaded until somebody had taken thirty
photographs, which is a hostage rather than a gate.
