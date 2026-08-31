# Pinned instruments

The registry of harness-owned instrument content the spec has pinned. A crowded run is scoreable only against a procedure listed here with status **pinned**, and only when the per-run meta's recorded hash matches the pin (`10-harness.md` → "Crowding is part of the instrument"). A mismatch is an instrument defect: fix or re-pin (with rationale committed here) before any disposition.

## Crowding procedures

| procedure | content hash (SHA-256) | pinned | status |
|---|---|---|---|
| `neutral-file-triage@2` | `3197f43ed3cc8b794ef97bb62945f63cb6a5287e12859cbcf65e946dde52ac3e` | 2026-08-31 | **pinned** |
| `neutral-file-triage@1` | `d7cf153febdc9497dab62109dd666158162e11fc4596b69dfa57457e0fd400d2` | 2026-08-30 | superseded — S6 defect, see below |

## `neutral-file-triage@2` — pin record (2026-08-31, spec session)

**Hash measured** by executing `NEUTRAL_FILE_TRIAGE_V2.content_hash()`; the value matches both the implementation's report and the `crowding.content_hash` recorded in the live run's meta (`runs/2026-08-31-driver-proof/smoke-crowded/S/S1/meta.json`) — the pin, the code, and a real run agree. Same identity-pin caveat as v1: obtained by running the instrument, so it detects drift rather than certifying what the code hashes over.

**Content review against S6:** identical notes, folders, and distractor server to v1; the only delta is the opening prompt, which now bounds the pre-turn to the first four notes and leaves the rest explicitly pending ("we'll keep going in a bit, and the rest MUST wait until you get the go-ahead"). Verdict: satisfies "genuinely mid-way through" — the scored prompt lands inside an owned, unfinished task. Judgment note: an explicit hold is the implementable form of interruption in a turn-based driver (a pre-turn cannot be cut off mid-generation), and pending-and-owned work is the competing goal S6 wants present; recorded as adequate, not merely tolerated.

**Mid-task property verified from artifacts, not the report:** the live run's `distractor-state.json` shows exactly `n01`–`n04` filed at scored-turn time — 8 of 12 notes remained, and the scored turn's answer came via a traced SUT call while the crowding state persisted beside it.

## `neutral-file-triage@1` — superseded (S6 defect, found 2026-08-31)

The v1 pre-turn ran the triage to completion ("Inbox is now empty" before the scored prompt), so the scored turn landed **after** the competing goal was gone — the cell measured a post-task consumer, not a mid-task one, violating S6's "genuinely mid-way through". Disposition: **any crowded result produced against v1 is instrument-defective for the crowded condition and is not scored** (`10-harness.md`, instrument defects block all other dispositions). No such result was ever scored — the defect was caught in the driver-proof run before any suite used the cell. The v1 identity pin is retained above so drift in the still-registered v1 content remains detectable (the implementation keeps a test asserting the v1 hash).

This finding is also the first live datum on the Q5(b) question's territory: completing-vs-pending is evidently a real axis of crowding strength, which sharpens what the padding-vs-task experiment should control for if it ever runs.
