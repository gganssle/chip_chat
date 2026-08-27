# The golden set

`cases.json` is the set. One entry per question, and each one carries the lane it
should route to, the tool inside that lane, the PRD requirements it covers, and
what has to be observed for it to count as passed.

```bash
python -m chip_chat.eval.golden --check                      # free
python -m chip_chat.eval.golden --check --catalog ./build    # and the menu terms
python -m chip_chat.eval.golden --catalog ./build --out eval/golden/BASELINE.md
```

Read [`BASELINE.md`](BASELINE.md) for what has and has not been measured, and
`eval/src/chip_chat/eval/golden/__init__.py` for the design.

---

## Adding a case

Six fields are the whole of it, and two of them are the ones people skip.

```json
{
  "id": "k4-comparative-calories",
  "message": "which has fewer calories, the chicken bowl or the steak burrito",
  "tool": "search_menu_knowledge",
  "requirements": ["K4", "K2"],
  "checks": ["cites", "grounded"],
  "menu_terms": ["chicken bowl", "steak burrito"],
  "why": "K4's comparative case. Two retrieved chunks and an arithmetic comparison over them."
}
```

**`why` is required, and it is not a comment.** It prints beside the case in the
failure table, which is the moment somebody has to decide whether the agent is
wrong or the case is. A case nobody can explain is a case nobody can fix, and it
will eventually be deleted by whoever is trying to make the number go up.

**`forbidden_tools` is what makes a case worth having.** A question only one tool
could possibly answer measures nothing about lane selection — and lane selection
is the metric the whole five-lane architecture exists to get right. Name the tool
the answer must *not* reach for. `chip_chat.agent.selection` picked six of its
twelve probe cases on the same principle, and the coverage check here requires at
least ten.

The rest:

| Field | What it does |
| --- | --- |
| `lane` | Optional. Carried so the JSON reads as the five-lane table; refused if it disagrees with `tool`. |
| `persona` | A `persona_id` from `data-gen`'s `population.toml`, or `any`. An account answer depends on who is asking. |
| `context` | Prior assistant turns the message presupposes. `{draft_id}` is substituted with a real one. |
| `confirmed` | The visitor has already pressed Confirm on the draft this turn acts on. Action lane only. |
| `dietary` | This is an allergen or dietary question. `eval/grounding` reports these apart and holds them to counts. |
| `menu_terms` | Published terms the case leans on. Checked against a catalogue build. |

**`dietary` is declared, and the set refuses to guess it for you.** Issue #75
scores allergen and dietary questions as their own category, and holds them to
counts that must be zero rather than to a percentage — a rate over a safety
property says how often the promise held. Nothing derives the flag: the
requirement ids do not draw the line (`K3` covers halal *and* cross-contact,
`K4` holds *"what's vegetarian here"*), and neither does a word list — *"are the
black beans cooked in the same pot as the chicken"* is a cross-contact question
containing no allergen word at all.

What the word list does do is catch a forgotten flag, in one direction only: a
case whose message asks about soy, dairy, gluten, halal or vegetarian and is not
marked is **refused at load**. Silence is not absence.

`checks` are the closed list in `chip_chat.eval.golden.cases.Check`. Three of them
— `declines`, `grounded`, `explains` — are judgements about meaning rather than
properties of a payload, and are **unscored** until a judge exists. That is not a
gap to work around with a keyword list; see the module docstring.

---

## Four things about this set

### Every requirement is covered, or delegated with a reason

`--check` will not pass while a PRD requirement has neither a case nor an entry
in `requirements.DELEGATIONS`. Twelve are delegated: the vision lane's accuracy
to the labeled photo set, the launch gates' full phrasing coverage to the
adversarial suite, rate limiting and the spend ceiling to the tests where they
are enforced. Each delegation names its target and its argument, because one
without an argument is a gap somebody labeled to make a report go green.

Entry — `E1`–`E7` — is excluded rather than delegated, and says so:
`requirements.OUT_OF_SCOPE`. There is no visitor message whose answer is *the
opening screen named my persona*.

### The set is held to the menu, the way ground truth is

`--catalog` checks every term in `menu_terms` against a built catalogue. A case
asking about barbacoa, run against a build that does not publish barbacoa, is a
case the deployment cannot pass for a reason that has nothing to do with the
agent.

This matters more than it looks. `cc-z1i` records that RFC-001 §07's generated
vision enums are **not** wired to the live catalogue yet, so the vocabulary in
the tree can drift from what is orderable with nothing to say so. A golden set
that cannot detect its own staleness would keep passing while the menu moved.
Point `--catalog` at a build the deployment actually serves.

### The vision lane is one case, on purpose

`v2-photo-routing` checks that a photo turn reaches `match_meal_from_photo`, and
nothing else. Everything downstream — components, the not-Chipotle case, the
clarify case, several meals in one frame — is measured over real photographs in
[`../photos`](../photos), which is where it belongs.

The division is not arbitrary. `eval/photos` runs the lane **directly**, so lane
selection is invisible to it; this set runs a whole turn, so component accuracy
is invisible here. Between them there is no gap, and neither could close it
alone.

As of 26 August 2026 `../photos/labels.json` is empty — the scorer shipped
without photographs, for licensing reasons ([#56](https://github.com/gganssle/chip_chat/issues/56)).
So the delegated vision requirements are covered by a measurement that has not
been taken yet. That is a fact about this repository rather than about the
arrangement, and it is recorded in `BASELINE.md` rather than hidden by moving
the cases here.

### Nothing here is a test

The manifest is data, not test code, because
[#72](https://github.com/gganssle/chip_chat/issues/72) promotes it into a
versioned Arize dataset in Phase 9 — and a dataset that started life as
parametrised test functions has to be rewritten to get there. The tests in
`eval/tests/test_golden_set.py` read this file; they do not contain it.
