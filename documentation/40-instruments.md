# Pinned instruments

The registry of harness-owned instrument content the spec has pinned. A crowded run is scoreable only against a procedure listed here, and only when the per-run meta's recorded hash matches the pin (`10-harness.md` → "Crowding is part of the instrument"). A mismatch is an instrument defect: fix or re-pin (with rationale committed here) before any disposition.

## Crowding procedures

| procedure | content hash (SHA-256) | pinned | status |
|---|---|---|---|
| `neutral-file-triage@1` | `d7cf153febdc9497dab62109dd666158162e11fc4596b69dfa57457e0fd400d2` | 2026-08-30 | pinned |

**How the hash was measured (2026-08-30, spec session):** executed `NEUTRAL_FILE_TRIAGE_V1.content_hash()` twice in separate interpreter invocations via `uv run python -c ...`; both returned the value above. This is an identity pin obtained by running the instrument itself, so it shares the instrument's failure class — it detects future drift; it does not independently certify what the code hashes over. Disagreement between this pin and a run's meta is the signal it exists to produce.

**Content review (2026-08-30, spec session, against S6):** the procedure's full content was dumped and reviewed — twelve mundane office notes (printer toner, fire drill, parking permits, …), four folders, and an opening prompt that sets a genuine competing goal ("keep filing until they're done"), served by a distractor MCP server named `shared_notes`. Verdict: task-shaped and coherent per S6, not padding. Generic neutrality only — a suite whose server touches note-keeping, office facilities, or file-filing domains collides with this procedure and must attest accordingly and select a different one (`20-manifest.md` → `crowding.collision_review`).
