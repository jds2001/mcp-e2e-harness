# WO-5 — `answer-coverage@1` vectors corrected; retire the xfail

**Issued:** 2026-09-19, spec session. **For:** the implementation session. **Spec authority:** `40-instruments.md` → `answer-coverage@1` reference vectors (corrected 2026-09-19) and pin record; `30-checks.md` → the reading note beneath the method. Files win on conflict. Small and self-contained; land before the next scored C1 run so the fixture table and the spec table agree.

## Background

WO-4's report stated a discrepancy on five `furthest_offset` cells (6,767 versus 6,766 on pinned-r09 at both floors and on floor-crowded; 1,048 versus 1,047 on unpinned-r01 at both floors) and, correctly, did not adjust the vectors. The spec session recomputed the rows with a second reference implementation and found the implementation's values; the table was wrong by one on exactly the spans that end on a collapsed whitespace run. The table is corrected and the method is pinned at the reported hash. The numbered method text is unchanged, so the hash `211931382f74e072f2c2af8c77e678660f45efa4cd2082b8d9e25b2fd4be2d6a` and the embedded text stand.

## Deliverables

1. **Refresh the fixture table** `tests/fixtures/answer-coverage/vectors.json` from the corrected `40-instruments.md` table, verbatim as before. Five cells change; nothing else.
2. **Retire the exception.** The vector test asserts every cell of every row exactly, with no per-cell carve-out; delete the strict-xfail test that held the old values. The suite should have no remaining xfail attributable to this method.
3. **Keep** the test that asserts the embedded method text is a verbatim substring of `30-checks.md`; a prose note was added beneath the numbered list and must not disturb it. If that test fails after pulling the spec, report it — that is a spec defect, not something to fix by re-embedding.
4. `ruff` clean; nothing under `documentation/`. Live spend: $0.

## Acceptance

The vector test's name and its pass over all seven rows with the corrected values; the xfail count of the suite before and after; the substring test still passing.

## Out of scope

Any change to the method text or hash; any second measure.
