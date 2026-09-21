# WO-7 implementation and acceptance

Implemented [WO-7](../documentation/work-orders/WO-7-null-final-content.md) using the engineering debug workflow: reproduce the reported bytes, isolate the serialization, fix recording, then exercise the full runner through a local fake upstream. Live spend was **$0**. Nothing under `documentation/` or in the original consumer's historical runs was changed.

## Reproduction and fix

The pre-change fake run reproduced the exact signature: repetition 2 wrote `null`, `answer_chars: 4`, digest `74234e98afe7498f`, no consumer outcome, and no transcript on the fresh row. Its per-prompt counts incorrectly read `invocations: 3, answered: 3, distinct: 2`.

The cause was `run_turn` serializing non-string final content with `json.dumps`. The loop now accepts only non-empty strings as answers. Null, empty, or other non-string final content with no tool calls records `consumer_limit.cause = "null_final_content"`; there is no retry or re-prompt. The runner writes an empty answer, records zero characters and a null digest, and excludes that row from answered counts and digest frequencies. Empty answers have a null digest for every driver, including existing context/step limits and instrument failures. An actual non-empty string such as `"null"` is preserved verbatim; it is not confused with a JSON null.

The existing `consumer_limit` key is retained as the single place for S13/S16 answerless consumer outcomes, even though this cause is not a limit. It is represented in `run-manifest.json → consumer_limits` beside existing causes, and live progress names `CONSUMER OUTCOME (null_final_content)`. An observed call-free null final is exempt from the zero-trace cell liveness failure. Independent instrument failures remain failures: missing assistant messages, missing product answer files, process failures, and wire-surface breaches are not relabeled as valid consumer outcomes.

The loop computes `reasoning_tail_json` and `reasoning_tail_matches_tools` from the final response. Text chunks from `reasoning_details` take precedence, with plain `reasoning` as a fallback. The suffix parser handles nested objects, quoted braces, escapes, malformed JSON, and absent reasoning. Matches list offered wire tool names whose top-level parameter properties cover every key of the parsed object; this does not claim that a tool was called or that argument values satisfy its schema. These marks carry no reasoning text or argument values.

## Transcripts and product-driver audit

Fresh loop rows now write `loop-session-single.json`. Crowded rows retain `loop-session-<id>.json`. Both use the same `{messages, offered_tools, scaffold}` shape. Conversation snapshots are saved before I/O, after responses/tool results, and on exit, so failed requests also leave the conversation reached so far. Plain reasoning is preserved in transcripts without adding it to the scaffold's existing message-echo policy. The runner secret-scans the loop transcript artifacts in both contexts. Tests place a configured secret only in reasoning and verify that the run is rejected before its run manifest is written.

Neither product-driver path in the harness can reach the offending JSON serialization: Claude Code supplies plain stdout; Codex supplies the text file named by `-o`. Neither harness path parses and serializes a JSON final message. Successful empty output on either channel now records the same answerless outcome with null reasoning marks, because those channels do not expose reasoning. Tests cover both channel mechanisms without launching either paid product. Behavior inside the vendor CLIs was not measured.

## Observable artifacts

Artifacts are under [runs/2026-09-21-wo7](../runs/2026-09-21-wo7/) and remain ignored by Git under repository convention. [acceptance-summary.json](../runs/2026-09-21-wo7/acceptance-summary.json) compares the pre-change run with both post-change runs.

| Experiment | Answer bytes at r02 | Digest at r02 | Invocations / answered / distinct | Recorded null-final outcomes |
| --- | --- | --- | --- | --- |
| [Before](../runs/2026-09-21-wo7/before/run/run-manifest.json) | 4 (`null`) | `74234e98afe7498f` | 3 / 3 / 2 | 0 |
| [Fresh after](../runs/2026-09-21-wo7/fresh/run/run-manifest.json) | 0 | null | 3 / 2 / 1 | 1 |
| [Crowded after](../runs/2026-09-21-wo7/crowded/run/run-manifest.json) | 0 | null | 3 / 2 / 1 | 1 |

Both post-change runs have zero harness failures and no voided cells. Each contains three transcripts. In each run only the r02 transcript contains the fixture's reasoning marker; it appears in no wire line, metadata, answer, or other artifact. The recorder implementation and content-free wire contract were unchanged. The affected row still records `finish_reason: stop`, two scored requests, and zero retries.

The [fresh r02 metadata](../runs/2026-09-21-wo7/fresh/run/loop-cell/A/A1/r02/meta.json) records:

```json
{
  "answer_chars": 0,
  "answer_sha256_16": null,
  "harness_failure": null,
  "consumer_limit": {
    "cause": "null_final_content",
    "detail": "final message has no non-empty string content and no tool calls",
    "reasoning_tail_json": true,
    "reasoning_tail_matches_tools": ["mcp__notes_sut__file_note"]
  }
}
```

The corresponding [transcript](../runs/2026-09-21-wo7/fresh/run/loop-cell/A/A1/r02/loop-session-single.json) preserves the actual final null content and reasoning. The crowded row also matches the distractor's `mcp__shared_notes__file_note`, because both offered tools cover the object's parameter names. Each run manifest has exactly one entry in `consumer_limits`: `loop-cell/A/A1/r02: null_final_content`. [Fresh progress](../runs/2026-09-21-wo7/fresh/progress.txt) and [crowded progress](../runs/2026-09-21-wo7/crowded/progress.txt) name the outcome.

`loop-scaffold@1` was not edited. Its content hash before and after is identical:

```text
before: b92ad9f83c8f23bee833fd1cb4c746ff23c3b0cb9a2b4637edb1b8c2ce17e2f6
after:  b92ad9f83c8f23bee833fd1cb4c746ff23c3b0cb9a2b4637edb1b8c2ce17e2f6
```

## Validation and reproduction

The baseline suite passed **324 tests**; the completed suite passes **368 tests**, including 44 new cases. `ruff check .` and `git diff --check` pass. The new regressions in [test_wo7.py](test_wo7.py) cover reasoning suffixes, null/empty/non-string finals, real tool calls with null content, unchanged reasoning echo behavior, failed-request transcripts, fresh/crowded acceptance artifacts, both product output channels, call-free finals, secret hygiene, and independent instrument failures. The test fixes the expected scaffold hash and checks that the null outcome spends no additional request.

Recreate the post-change acceptance runs in unused directories:

```sh
.venv/bin/python tests/wo7_acceptance.py runs/wo7-recheck/fresh
.venv/bin/python tests/wo7_acceptance.py runs/wo7-recheck/crowded --crowded
.venv/bin/python -m pytest tests/ -q
```
