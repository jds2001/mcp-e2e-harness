# WO-7 — a final message with null content is a recorded outcome, never the answer `null`

**Issued:** 2026-09-21, spec session. **For:** the implementation session. **Spec authority:** `10-harness.md` → Layer 2, ruling S16; `00-INDEX.md` → S16 and its corollary. Files win on conflict. The maintainer may challenge any item.

## Background

In `uscode-mcp/runs/2026-09-21T01:13:27-04:00-loop-complete` 7 of 26 loop rows have an `answer.txt` containing the four characters `null`, `answer_chars: 4`, digest `74234e98afe7498f` (SHA-256 of `null`), and no failure or limit recorded. Where a session file exists (`loop-floor` C3, C4, D1, D3) the final assistant message is `content: null` with no `tool_calls`, its `reasoning_details` text ends in a JSON object shaped like arguments to an offered tool, and the final wire line reads `finish_reason: stop`. A JSON null was serialized as text, hashed, and counted as an answer. Since WO-6 these rows would also count in `answered` and their shared digest would read as replays of one draw.

## Deliverables

1. **Only a non-empty string is an answer.** When the final message's content is null or empty: `answer.txt` is empty, `answer_chars` is 0, `answer_sha256_16` is null, and the row is not counted in `answered` nor in `digests`. The text `null` (or `None`) is never written. This holds for every driver; report whether any product-driver path can reach the same serialization.
2. **A distinct recorded outcome** with `harness_failure: null`: the S13 block with cause `null_final_content` (if you judge a separate key cleaner than reusing `consumer_limit`, say so and why — the requirement is one place a scorer looks for answerless consumer outcomes). Counted beside the limits in `run-manifest.json`, never under broken, never voided. S9 progress output names it when it happens.
3. **Two derived marks on that outcome,** computed by the loop consumer from the response it already reads: `reasoning_tail_json` — true when the final message's reasoning text, stripped, ends in a balanced JSON object that parses, false when it does not, null when the message has no reasoning text; and `reasoning_tail_matches_tools` — the offered tools whose parameter names include every key of that object (empty list when none; null when the first mark is not true). The marks carry no text and no part of the object.
4. **`loop-scaffold@1` is untouched:** no retry, no re-prompt, no change to the loop policy or its hash. Show the hash before and after.
5. **Every loop row records its consumer transcript.** `10-harness.md` → "Recorded per cell, per run" requires it; crowded rows have the message list as `loop-session-*.json`, and fresh rows have none (`loop-isolation` rows in the run above), which is why three of the seven rows could not be fully read. Same content and same file shape for both contexts; the secret-hygiene scan covers it as it does any transcript. The wire recorder's contract is unaffected — `api-surface.jsonl` still carries no content.
6. Unit tests for 1–3 and 5, including a final message with empty-string content, one with null content and no reasoning, and one with a tool call present (which is not this outcome); `ruff` clean; nothing under `documentation/`. Live spend: $0.

## Acceptance — observable artifacts

A fake-upstream run, `--repeats 3` on one cell, where the fake's final response on repetition 2 is `content: null`, no tool calls, `finish_reason: stop`, with reasoning text ending in a JSON object whose keys are parameters of one offered tool: that row's empty `answer.txt`, its `meta.json` showing the outcome with both marks and `harness_failure: null`, null digest, `answer_chars: 0`; `answers` reading `invocations: 3, answered: 2`; the run manifest's count of the outcome; the scaffold hash unchanged. The same run on a fresh cell showing the transcript file in every row, and the reasoning text present there and in no other artifact.

## Out of scope

Any retry or re-prompt — that is `loop-scaffold@2` if it is ever wanted (S16 corollary), and it is not ordered. Re-reading the uscode-mcp run: its rows stand as recorded, and its spec session identifies the affected rows by digest.
