# Quality report

Generated 2026-09-06T11:48:16Z · Bancada 0.1.0 · annotation schema v7

## Scope

1 annotation(s) in delivered state (`avaliada`), of which **1 human** and **0 synthetic**.

Synthetic annotations are machine-generated on purpose and carry a hidden target rating. They are **excluded from the data exports** and kept here because measuring the reviewer is what they are for. The signal is a single column: `anotacoes.gabarito_avaliacao_json IS NOT NULL`.

## How the work was checked

Every annotation goes through two independent passes:

1. **Triage** — approve or return. This is the *only* pass that sends work back to the annotator; returned work is re-submitted as a new version.
2. **Rate and Review** — rate the work as it arrived, fix it in place with a stated reason for every single change, rate the result, and justify the rating. `borderline_admin` escalates to an administrator who decides and closes the item; `incorrigivel` is terminal.

| measure | value |
|---|---|
| annotations rated | 1 |
| corrected in place by the reviewer | 0 |
| individual field edits, each with a reason | 0 |
| self-reviewed (solo mode) | 0 |
| median active time per annotation | 240000 ms |

**Rating on arrival**

| value | n | share |
|---|---|---|
| adequado | 1 | 100.0% |

**Rating after correction**

| value | n | share |
|---|---|---|
| adequado | 1 | 100.0% |

## Did triage let anything through?

This is the metric that measures **the triage reviewer**, not the annotator: an item approved in pass 1 and found unusable in pass 2 went through a gate that should have stopped it.

| measure | value |
|---|---|
| approved by triage | 1 |
| of those, reached pass 2 | 1 |
| arrived `inutilizavel` | 0 |
| ended `incorrigivel` | 0 |
| leak rate | 0.0% |

## Reviewer calibration

No synthetic annotation has been rated yet, so reviewer calibration cannot be measured. It is not zero — it is **not measured**, which is a different statement and the honest one.

## Composition

| task type | n |
|---|---|
| comparar_ab | 1 |

**By annotator**

| value | n | share |
|---|---|---|
| Ana Ribeiro | 1 | 100.0% |

**By prompt language**

| value | n | share |
|---|---|---|
| pt | 1 | 100.0% |

**By prompt license**

| value | n | share |
|---|---|---|
| fixture | 1 | 100.0% |

