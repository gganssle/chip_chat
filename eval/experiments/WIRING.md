# shipped → shipped

- **Baseline** — `0ec39d67a727`, prompt v1+1c6f84d1f21f, run 2026-08-28T03:33:19+00:00, lanes `none`
- **Candidate** — `0ec39d67a727`, prompt v1+1c6f84d1f21f, run 2026-08-28T03:18:33+00:00, lanes `account+personalization`
- **Dataset** — cilantro-golden-set `9ba196eb786c`, 34 rows

**5 regression(s). Read the breakdowns before the headline.**

> **Read these first.**
> - different lanes wired: none then account+personalization. A tool is offered to the model only when something can answer it, so a lane that came up moved its rows from unscoreable to scored, and part of every delta below is the deployment rather than the change.
> - different sources: 'week-one slice on gpt-5-mini under prompt v1+1c6f84d1f21f, lanes: none' then 'week-one slice on gpt-5-mini under prompt v1+1c6f84d1f21f, lanes: account+personalization'.
> - both sides carry the same configuration fingerprint and different lanes, so this comparison is measuring the wiring rather than the configuration. That is a real measurement and it is the difference between what the model can do and what the deployment lets it do.

## What got worse

- lane personalization: completion -25.0%, tool selection +0.0%
- requirement P1: -33.3%
- requirement T1: -16.7%
- requirement T2: -12.5%
- requirement T5: -33.3%

## What got better

- Tool-selection accuracy: 56.2% → 65.6%
- Groundedness of food and policy claims: 40.0% → 70.0%

## The targets, side by side

| Metric | Target | Baseline | Candidate | Δ |
| --- | ---: | ---: | ---: | ---: |
| Task completion on the golden set | ≥ 85% | 17.6% | 20.6% | +2.9% |
| Tool-selection accuracy | ≥ 95% | 56.2% | 65.6% | +9.4% |
| Groundedness of food and policy claims | ≥ 95% | 40.0% | 70.0% | +30.0% |
| Menu claims made without a citation | 0 | -- | -- | -- |
| Photo → order, component-level F1 | ≥ 80% | -- | -- | -- |

## By lane

| Lane | Completion Δ | Tool selection Δ | Failure shapes that moved |
| --- | ---: | ---: | --- |
| knowledge | +0.0% | +0.0% | — |
| account | +28.6% | +50.0% | correct -1, extra_tools +4, wrong_lane -3 |
| personalization | -25.0% | +0.0% | — |
| action | +0.0% | +0.0% | no_tool +1, wrong_lane -1 |
| vision | +0.0% | +0.0% | — |
| none | +0.0% | +0.0% | — |

## By requirement

| Requirement | Baseline | Candidate | Δ |
| --- | ---: | ---: | ---: |
| K1 | 0.0% | 0.0% | +0.0% |
| K2 | 0.0% | 0.0% | +0.0% |
| K3 | 0.0% | 0.0% | +0.0% |
| K4 | 0.0% | 0.0% | +0.0% |
| K5 | 0.0% | 0.0% | +0.0% |
| A1 | 50.0% | 50.0% | +0.0% |
| A2 | 0.0% | 66.7% | +66.7% |
| A3 | — | — | measured in the adversarial suite, #30 |
| A4 | 0.0% | 0.0% | +0.0% |
| P1 | 33.3% | 0.0% | -33.3% |
| P2 | 0.0% | 0.0% | +0.0% |
| P3 | 0.0% | 0.0% | +0.0% |
| T1 | 50.0% | 33.3% | -16.7% |
| T2 | 62.5% | 50.0% | -12.5% |
| T3 | 100.0% | 100.0% | +0.0% |
| T4 | 50.0% | 50.0% | +0.0% |
| T5 | 100.0% | 66.7% | -33.3% |
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

