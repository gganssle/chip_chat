# The labeled photo set

The vision lane's ground truth. Issue
[#56](https://github.com/gganssle/chip_chat/issues/56) opens with the sentence
this directory exists to make true: *without it, "the photo matcher works well"
is an opinion.*

**Status: the scorer is here and the photographs are not.** `labels.json` holds
no frames. Everything below is what to shoot, how to label it, and how to run
the scoring once there is something to score. See `BASELINE.md` for what that
means for the PRD's F1 target and for issue #54's confidence floors, both of
which are waiting on a number this set has not produced yet.

```bash
python -m chip_chat.eval.photos --check          # free; run it after every frame
```

That command exits non-zero today, and should: an empty set is not vacuously
complete.

---

## What to photograph

Thirty frames is the floor, and thirty *clean overhead bowls* is not the set —
that set scores beautifully and proves nothing, which is why the scope lives in
`chip_chat.eval.photos.coverage` as checks rather than here as advice.
`--check` prints exactly which of these are unmet:

| How many | What | Why |
| --- | --- | --- |
| ≥ 10 | Clean, well-lit, one meal, contents visible | Without a floor of easy frames, a poor score cannot be told from a set nobody could read either. |
| ≥ 2 | Poor lighting | Restaurant light is orange and a phone camera is not good at it. |
| ≥ 2 | Partially eaten | What a photograph taken at a table actually looks like. |
| ≥ 2 | Contents not visible — a wrapped burrito, a lid | The case with no ground truth for the inside. See "unreadable" below. |
| ≥ 2 | Food this restaurant does not serve | PRD V4. Poke, a curry, a sandwich. |
| ≥ 3 | Several meals in one frame | `docs/decisions/multi-meal-photos.md`, and #58's own acceptance criterion. |
| ≥ 1 | One meal beside a side — a bowl next to a bag of chips | The likeliest false positive in the lane, and it fires on the most ordinary photo anyone will send. |
| ≥ 2 | A required slot the photograph does not answer | The clarify path. PRD V5: ask, do not guess. |
| ≥ 4 each | Bowls and burritos | Vessel is half of every SKU; a set of one vessel proves nothing about the slot. |
| ≥ 3 each | Chicken and steak | Protein carries the highest floor because a wrong one is a different meal at a different price. |

Shoot them the way a visitor would: a phone, held over the tray, in whatever
light is there. A studio photograph measures a pipeline nobody uses.

### Licensing — not optional

This is a public repository. **Only photographs you took yourself, or ones
unambiguously licensed for this purpose.** Every entry records who took it and
under what terms, and the loader refuses an entry without both. A labeled
dataset of someone else's photographs is an avoidable problem.

---

## How to label one

Open the photograph. Write down what you can see, in the vocabulary the
catalogue publishes — `python -c "from chip_chat.vision import Vocabulary;
print(Vocabulary.from_env().values('protein'))"` will list a slot's terms, and
a term the catalogue does not publish is refused at load.

```json
{
  "id": "clean-chicken-bowl-01",
  "image": "images/clean-chicken-bowl-01.jpg",
  "capture": {
    "photographer": "gganssle",
    "license": "CC0-1.0",
    "taken": "2026-08-24"
  },
  "conditions": ["clean"],
  "is_chipotle_style": true,
  "meals_visible": 1,
  "slots": {
    "vessel": "bowl",
    "protein": "chicken",
    "rice": "white_rice",
    "beans": "black_beans",
    "salsas": ["fresh_tomato_salsa"],
    "toppings": ["cheese", "guacamole"]
  },
  "unreadable": [],
  "notes": "Overhead, daylight, half a lime on the tray."
}
```

**`conditions`** is a closed list — `clean`, `low_light`, `partially_eaten`,
`contents_hidden`, `angled`, `cluttered`, `multi_meal`, `meal_with_side`,
`not_chipotle`. A frame may carry several. The coverage checks are written
against these, so a condition nobody spells consistently is a check that
silently stops counting.

**`meals_visible`** counts *orderable meal-sized compositions*. A bowl and a bag
of chips is **one**. Four bowls on a table is four. This one integer carries the
whole multi-meal behaviour, so it is scored in both directions: a false positive
costs a working order and a false negative costs a fabricated one.

**`unreadable`** is the field to get right, and the one most likely to be
skipped. It names the slots **the photograph does not answer** — the rice inside
a foil-wrapped burrito, the beans under everything else. Those slots are scored
in *neither* direction. The alternatives are both wrong: labeling the rice you
happen to remember ordering scores the model on clairvoyance, and leaving the
slot out silently scores it wrong for naming rice that is really there. If a
*required* slot (vessel, protein, rice, beans) is unreadable, the correct
behaviour for that frame is a clarifying question rather than a draft, and the
scorer expects one.

Three rules the loader enforces rather than trusts:

- A frame that is **not one Chipotle-style meal** carries no component labels at
  all. On a table of four bowls the stage-4 slots describe *the picture* rather
  than any one meal — that is the whole argument of
  `docs/decisions/multi-meal-photos.md` — so there is nothing for a per-meal
  label to be true about.
- Every **required** slot is either given a term or named in `unreadable`.
  Silence is not the same as absence.
- Every term is one the **catalogue publishes**. This is D3 applied to the
  ground truth: the model cannot name a food the menu does not sell, so a label
  that does would score the model wrong for being right.

---

## Running the experiment

```bash
export CHIP_CHAT_VISION_VOCABULARY=chip_chat.vision_vocabulary
export CHIP_CHAT_FOUNDRY_ENDPOINT=... CHIP_CHAT_FOUNDRY_API_KEY=...
export CHIP_CHAT_FOUNDRY_VISION_DEPLOYMENT=...

python -m chip_chat.eval.photos --catalog ./catalog-build --out eval/photos/BASELINE.md
```

One vision call per frame. Each frame goes through stages 1 and 2 exactly as an
upload does — validated, re-encoded, downscaled to `UploadLimits.max_edge` —
because issue #63 measured this deployment's accuracy collapsing below about
512 pixels and a run that skipped the downscale would flatter a pipeline nobody
deploys. Stage 3 (Content Safety) is the one production stage the runner skips:
these are our own photographs and moderation is not what is being measured.

The floors come from `CHIP_CHAT_MATCHER_*` and are printed in the report, so
tuning is: change one, run again, diff the two documents.

### What the report says, and why it says it in that order

**Coverage first**, above every score. A set missing its hard cases produces a
good F1 and a false conclusion, and the reader has to meet that fact before they
meet the number.

**Components twice** — once as the model described them, once as stage 5
believed them after the floors. The first measures the model; the second is the
PRD's *photo → order* metric, because a slot below its floor never reaches an
order. The gap between them is what the floors cost, per slot, which is the
number issue #54 shipped its floors waiting for.

**Detection in both directions**, with the failing frames named. **Which path
each frame took**, so #55's three branches are measured rather than assumed, and
a frame that produced a draft where it should have declined gets a sentence of
its own — that is the expensive direction. **The confidence distribution**,
which is issue #53's fourth acceptance criterion meeting real photographs for
the first time. **Frames the lane could not answer for**, counted apart from
everything else, because a deployment that is down is not a model that is wrong.
