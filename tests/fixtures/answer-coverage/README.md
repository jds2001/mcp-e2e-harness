# `answer-coverage@1` reference vectors

The rows behind the reference-vector table in `documentation/40-instruments.md` (2026-09-18, spec session), copied here so the vectors survive the gitignored run directories. `tests/test_measurements.py` asserts every value in that table against these files.

- `*.answer.txt` — each row's `answer.txt`, byte for byte: model output from our own runs.
- `PLAW-118publ31.window-0-20000.reference.txt` — the `get_public_law` record's `/response/structuredContent/text/content` string for the four loop rows (identical across them: the same 20,000-character window of the same law). Public-law text is public domain.
- `PLAW-118publ31.window-0-100000.reference.txt` — the same pointer on the claude-code row (a 100,000-character window).
- `vectors.json` — the table, verbatim: one entry per table row, keyed to the answer and reference files.

| slug | row |
|---|---|
| `pinned-r09` | `runs/2026-09-18-q8-provider-pin/pinned-r09/loop-isolation-pinned/C/C1`, record 0 |
| `floor-crowded` | `runs/2026-09-18-q8-floor-crowded/loop-floor-pinned/C/C1`, record 0 |
| `pinned-r01` | `runs/2026-09-18-q8-provider-pin/pinned-r01/loop-isolation-pinned/C/C1`, record 0 |
| `unpinned-r01` | `runs/2026-09-18-q8-provider-pin/unpinned-r01/loop-isolation-unpinned/C/C1`, record 0 |
| `claude-code-summary` | `uscode-mcp/documentation/runs/2026-09-16T003754Z/isolation/C/C1` (claude-code), record 0 |

Do not edit a vector to make a test pass: a mismatch is reported as a discrepancy (WO-4 §4), and the spec session owns the table.
