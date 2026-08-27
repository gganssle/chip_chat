# shipped → lean-lanes

- **Baseline** — `0ec39d67a727`, prompt v1+1c6f84d1f21f, run 2026-08-27T20:08:48+00:00
- **Candidate** — `7f1ab05ad991`, prompt v2-lean+a3c60ea9af5f, run 2026-08-27T20:35:01+00:00
- **Dataset** — cilantro-golden-set `9ba196eb786c`, 34 rows

**1 regression(s). Read the breakdowns before the headline.**

> **Read these first.**
> - different sources: 'week-one slice on gpt-5-mini under prompt v1+1c6f84d1f21f' then 'week-one slice on gpt-5-mini under prompt v2-lean+a3c60ea9af5f'.

## What got worse

- Tool-selection accuracy: 50.0% → 0.0%

## The targets, side by side

| Metric | Target | Baseline | Candidate | Δ |
| --- | ---: | ---: | ---: | ---: |
| Task completion on the golden set | ≥ 85% | 0.0% | 0.0% | +0.0% |
| Tool-selection accuracy | ≥ 95% | 50.0% | 0.0% | -50.0% |
| Groundedness of food and policy claims | ≥ 95% | -- | -- | -- |
| Menu claims made without a citation | 0 | -- | -- | -- |
| Photo → order, component-level F1 | ≥ 80% | -- | -- | -- |

## By lane

| Lane | Completion Δ | Tool selection Δ | Failure shapes that moved |
| --- | ---: | ---: | --- |
| knowledge | +0.0% | -- | extra_tools -1, unscored +1 |
| account | +0.0% | -- | — |
| personalization | +0.0% | -- | — |
| action | +0.0% | -- | no_tool -1, unscored +1 |
| vision | +0.0% | -- | unscored -1, wrong_lane +1 |
| none | +0.0% | -- | unscored -2, wrong_lane +2 |

## By requirement

| Requirement | Baseline | Candidate | Δ |
| --- | ---: | ---: | ---: |
| K1 | 0.0% | 0.0% | +0.0% |
| K2 | 0.0% | 0.0% | +0.0% |
| K3 | 0.0% | 0.0% | +0.0% |
| K4 | 0.0% | 0.0% | +0.0% |
| K5 | 0.0% | 0.0% | +0.0% |
| A1 | 0.0% | 0.0% | +0.0% |
| A2 | 0.0% | 0.0% | +0.0% |
| A3 | — | — | measured in the adversarial suite, #30 |
| A4 | 0.0% | 0.0% | +0.0% |
| P1 | 0.0% | 0.0% | +0.0% |
| P2 | 0.0% | 0.0% | +0.0% |
| P3 | 0.0% | 0.0% | +0.0% |
| T1 | 0.0% | 0.0% | +0.0% |
| T2 | 0.0% | 0.0% | +0.0% |
| T3 | 0.0% | 0.0% | +0.0% |
| T4 | 0.0% | 0.0% | +0.0% |
| T5 | 0.0% | 0.0% | +0.0% |
| V1 | — | — | measured in api/tests/test_upload_limits.py and vision/tests/test_intake.py |
| V2 | — | — | measured in the labeled photo set, #56 |
| V3 | — | — | measured in the labeled photo set, #56 |
| V4 | — | — | measured in the labeled photo set, #56 |
| V5 | — | — | measured in the labeled photo set, #56 |
| V6 | — | — | measured in chip_chat.vision.matcher, and RFC-001 D3 |
| V7 | — | — | measured in the labeled photo set, #56, and docs/decisions/multi-meal-photos.md |
| S1 | — | — | measured in api/tests/test_guard.py and vision/tests/test_moderation.py |
| S2 | — | — | measured in the adversarial suite, #30 |
| S3 | — | — | measured in api/tests/test_limits.py and api/tests/test_source_ratelimit.py |
| S4 | — | — | measured in api/tests/test_killswitch.py and api/tests/test_spend_gate.py |

## How this document decides

A rate has to move by at least 3% to be called a regression, because the dataset is 34 rows and one row is about 2.9% of it — a threshold below one row would report a regression every time a single case flipped. A **count** has no threshold: PRD §05 makes the gates zero, and one more than zero is one more than zero. A requirement has no threshold either, because it is usually covered by one or two cases and a rate over two cases has no resolution for a threshold to use.

