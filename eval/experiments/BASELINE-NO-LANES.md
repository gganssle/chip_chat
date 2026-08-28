# Experiment — shipped

- **Configuration** — `0ec39d67a727`, prompt v1+1c6f84d1f21f
- **Dataset** — cilantro-golden-set `9ba196eb786c`, 34 rows
- **Answered by** — week-one slice on gpt-5-mini under prompt v1+1c6f84d1f21f, lanes: none
- **Lanes wired** — `none` — the hardcoded three-item menu and the account fixture answered, and `ask_account_question`, `get_recommendations` and `match_meal_from_photo` were not offered to the model at all
- **Judged by** — gpt-5-mini as judge
- **Run at** — 2026-08-28T03:33:19+00:00
- **Judge spend** — 12889 tokens

> **No lane was wired, so the slice registers six of the eleven tools.** A tool that is not registered cannot be routed to, so its rows come back `no_tool` however good the model is, and a span tree cannot tell that apart from a model that chose not to call. `ask_account_question`, `get_recommendations` and `match_meal_from_photo` are the three, and this run scored their rows at zero for a reason that is not about the model. Read `eval/trajectory/BASELINE.md` beside this document.

> **Recorded and not applied: retrieval, matcher.** This run wires no lane behind those axes, so a flat line on either is a fact about the deployment rather than evidence that the setting does not matter.

## The targets

| Metric | Target | This run | Met | Over |
| --- | ---: | ---: | :---: | --- |
| Task completion on the golden set | ≥ 85% | 17.6% | **no** | 20 of 34 row(s) |
| Tool-selection accuracy | ≥ 95% | 56.2% | **no** | 32 of 34 row(s) |
| Groundedness of food and policy claims | ≥ 95% | 40.0% | **no** | 10 of 10 row(s) |
| Menu claims made without a citation | 0 | -- | — | 0 of 34 row(s) |
| Photo → order, component-level F1 | ≥ 80% | -- | — | — |

**Unverified, which is not the same as unmet:** Menu claims made without a citation, Photo → order, component-level F1. A target nobody measured has not passed.

- `uncited_claims` — no source reported citations (cc-bap)
- `photo_f1` — delegated to eval/photos (#56); one whole-turn case cannot say whether the salsa was right

## By lane

The aggregate above is one number over five lanes with different amounts of the product behind them. This is where a regression in one of them stops hiding behind a gain in another.

| Lane | Rows | Completion | Tool selection | wrong lane | no tool | extra tools | wrong query | ungrounded | over-refusals |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| knowledge | 12 | 0.0% | 91.7% | 1 | 0 | 11 | 0 | 6 | 2 |
| account | 7 | 14.3% | 16.7% | 5 | 0 | 0 | 0 | 0 | 0 |
| personalization | 4 | 25.0% | 66.7% | 1 | 0 | 2 | 0 | 0 | 0 |
| action | 7 | 57.1% | 57.1% | 3 | 0 | 4 | 0 | 0 | 0 |
| vision | 1 | 0.0% | 0.0% | 1 | 0 | 0 | 0 | 0 | 0 |
| none | 3 | 0.0% | 0.0% | 3 | 0 | 0 | 0 | 0 | 0 |

## By requirement

A lane is where the architecture is; a requirement is where the product is. One case can cover two requirements and one requirement can be covered by six cases, so this is a different partition of the same rows and not a re-presentation of the table above.

| Requirement | Lane | Cases | Passed | Failed | Unscored | Rate |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| K1 | knowledge | 6 | 0 | 1 | 5 | 0.0% |
| K2 | knowledge | 10 | 0 | 1 | 9 | 0.0% |
| K3 | knowledge | 4 | 0 | 0 | 4 | 0.0% |
| K4 | knowledge | 3 | 0 | 0 | 3 | 0.0% |
| K5 | knowledge | 2 | 0 | 0 | 2 | 0.0% |
| A1 | account | 2 | 1 | 1 | 0 | 50.0% |
| A2 | account | 3 | 0 | 3 | 0 | 0.0% |
| A3 | account | — | — | — | — | measured in the adversarial suite, #30 |
| A4 | account | 1 | 0 | 1 | 0 | 0.0% |
| P1 | personalization | 3 | 1 | 0 | 2 | 33.3% |
| P2 | personalization | 1 | 0 | 1 | 0 | 0.0% |
| P3 | personalization | 1 | 0 | 0 | 1 | 0.0% |
| T1 | action | 6 | 3 | 3 | 0 | 50.0% |
| T2 | action | 8 | 5 | 3 | 0 | 62.5% |
| T3 | action | 1 | 1 | 0 | 0 | 100.0% |
| T4 | action | 2 | 1 | 1 | 0 | 50.0% |
| T5 | action | 3 | 3 | 0 | 0 | 100.0% |
| V1 | vision | — | — | — | — | measured in api/tests/test_upload_limits.py and vision/tests/test_intake.py |
| V2 | vision | — | — | — | — | measured in the labeled photo set, #56 |
| V3 | vision | — | — | — | — | measured in the labeled photo set, #56 |
| V4 | vision | — | — | — | — | measured in the labeled photo set, #56 |
| V5 | vision | — | — | — | — | measured in the labeled photo set, #56 |
| V6 | vision | — | — | — | — | measured in chip_chat.vision.matcher, and RFC-001 D3 |
| V7 | vision | — | — | — | — | measured in the labeled photo set, #56, and docs/decisions/multi-meal-photos.md |
| S1 | none | — | — | — | — | measured in api/tests/test_guard.py and vision/tests/test_moderation.py |
| S2 | none | — | — | — | — | measured in the adversarial suite, #30 |
| S3 | none | — | — | — | — | measured in api/tests/test_limits.py and api/tests/test_source_ratelimit.py |
| S4 | none | — | — | — | — | measured in api/tests/test_killswitch.py and api/tests/test_spend_gate.py |

