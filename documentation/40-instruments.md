# Pinned instruments

The registry of harness-owned instrument content the spec has pinned. A crowded run is scoreable only against a procedure listed here with status **pinned**, and only when the per-run meta's recorded hash matches the pin (`10-harness.md` → "Crowding is part of the instrument"). A mismatch is an instrument defect: fix or re-pin (with rationale committed here) before any disposition.

## Crowding procedures

| procedure | content hash (SHA-256) | pinned | status |
|---|---|---|---|
| `neutral-file-triage@2` | `3197f43ed3cc8b794ef97bb62945f63cb6a5287e12859cbcf65e946dde52ac3e` | 2026-08-31 | **pinned** |
| `neutral-file-triage@1` | `d7cf153febdc9497dab62109dd666158162e11fc4596b69dfa57457e0fd400d2` | 2026-08-30 | superseded — S6 defect, see below |

## Loop scaffolds

| scaffold | content hash (SHA-256) | pinned | status |
|---|---|---|---|
| `loop-scaffold@1` | `b92ad9f83c8f23bee833fd1cb4c746ff23c3b0cb9a2b4637edb1b8c2ce17e2f6` | 2026-09-18 | **pinned** |

## `loop-scaffold@1` — pin record (2026-09-18, spec session)

**Hash measured** by executing `LOOP_SCAFFOLD_V1.content_hash()`; the value matches the implementation's report (WO-1 §1) and the `scaffold.content_hash` recorded in every loop cell's `meta.json`, every `cell_gates` tuple, and every probe-cache entry across the 2026-09-18 runs (`runs/2026-09-18-q8-*`, `runs/2026-09-18-loop-smoke`). Identity pin, same caveat as the crowding pins: obtained by running the instrument, so it detects drift rather than certifying what the code hashes over; the report states the hash covers name, version, system prompt, step cap, tool-loop policy text, probe tool definition, and probe prompt.

**Content review against requirement 1 and no-disclosure (`50-drivers.md` → loop; `10-harness.md`).** System prompt, verbatim from the report: "You are a helpful assistant with access to tools. When a tool can supply information you need, call it instead of relying on memory. Base your answer on what the tools return, cite or quote retrieved material where that matters, and say plainly when something could not be determined with the tools available." Verdict: generic, domain-neutral, no mention of testing, harnesses, or evaluation; it does instruct tool-first behavior and honest non-determination, which is a scaffold choice the suite's Layer-2 criteria must be read against (a consumer told to say plainly what could not be determined is being nudged toward the floor behaviors the criteria score — the loop measures server × model *under this nudge*, and that is what S11 means by "under a pinned harness scaffold"). Step cap 24 requests per turn, fresh per pre-turn and scored turn. Tool-loop policy: one request per step, tool results appended as text joined by newlines (JSON serialization when no text), no `tool_choice` sent, turn ends on a call-free response or at the cap. Probe tool `get_probe_value(key)`, probe prompt asks for key `alpha`. Not in the hash but sent on every request: `usage.include` and the strict-routing provider block; tools offered under the `mcp__<server>__<tool>` idiom so wire surfaces compare across drivers. Recorded as adequate for v1. Two things a v2 would want, noted so they are not forgotten: the join-with-newlines flattening discards MCP result structure (image or resource items never reach the model), and the cap applies per turn rather than per conversation. Third, observed 2026-09-18 across the C1 provider-pin runs: the prompt carries no audience guidance (nothing says the person cannot see tool parameters or results) and "cite or quote retrieved material" nudges toward pasting a window verbatim; `openai/gpt-oss-120b` at low effort relayed the server's continuation instruction (`start_char=20000`) to the user word for word under this scaffold, while `openai/gpt-5.4-nano` under the same scaffold rephrased it in user terms — so the effect is model-dominated, the scaffold contributes, and any v2 that adds audience guidance is a new pinned version whose cells never pool with v1 (S11). The behavior itself is a consumer-behavior finding handed to the suite's spec session, not an instrument defect.

## `neutral-file-triage@2` — pin record (2026-08-31, spec session)

**Hash measured** by executing `NEUTRAL_FILE_TRIAGE_V2.content_hash()`; the value matches both the implementation's report and the `crowding.content_hash` recorded in the live run's meta (`runs/2026-08-31-driver-proof/smoke-crowded/S/S1/meta.json`) — the pin, the code, and a real run agree. Same identity-pin caveat as v1: obtained by running the instrument, so it detects drift rather than certifying what the code hashes over.

**Content review against S6:** identical notes, folders, and distractor server to v1; the only delta is the opening prompt, which now bounds the pre-turn to the first four notes and leaves the rest explicitly pending ("we'll keep going in a bit, and the rest MUST wait until you get the go-ahead"). Verdict: satisfies "genuinely mid-way through" — the scored prompt lands inside an owned, unfinished task. Judgment note: an explicit hold is the implementable form of interruption in a turn-based driver (a pre-turn cannot be cut off mid-generation), and pending-and-owned work is the competing goal S6 wants present; recorded as adequate, not merely tolerated.

**Mid-task property verified from artifacts, not the report:** the live run's `distractor-state.json` shows exactly `n01`–`n04` filed at scored-turn time — 8 of 12 notes remained, and the scored turn's answer came via a traced SUT call while the crowding state persisted beside it.

## `neutral-file-triage@1` — superseded (S6 defect, found 2026-08-31)

The v1 pre-turn ran the triage to completion ("Inbox is now empty" before the scored prompt), so the scored turn landed **after** the competing goal was gone — the cell measured a post-task consumer, not a mid-task one, violating S6's "genuinely mid-way through". Disposition: **any crowded result produced against v1 is instrument-defective for the crowded condition and is not scored** (`10-harness.md`, instrument defects block all other dispositions). No such result was ever scored — the defect was caught in the driver-proof run before any suite used the cell. The v1 identity pin is retained above so drift in the still-registered v1 content remains detectable (the implementation keeps a test asserting the v1 hash).

This finding is also the first live datum on the Q5(b) question's territory: completing-vs-pending is evidently a real axis of crowding strength, which sharpens what the padding-vs-task experiment should control for if it ever runs.
