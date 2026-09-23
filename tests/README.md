# Regression tests

Tests are organized by behavior. Add coverage to the relevant module rather than creating a file per work order. Git history preserves the work-order lineage.

| Module | Coverage |
| --- | --- |
| `test_cell_identity.py` | Cell identity, canonicalization, and secret-key identity |
| `test_checks.py` | Check predicates, per-cell outcomes, roll-ups, and failure references |
| `test_run_repetitions.py` | Grid repetition, paths, answer counts, budgets, and cell environments |
| `test_null_final_content.py` | Empty answers, reasoning tails, transcripts, and consumer classification |
| `test_zero_trace_liveness.py` | Required evidence for zero-trace null-final exemptions |
| `test_invocation_replacements.py` | Replacement eligibility, retained attempts, exhaustion, and budgets |
| `test_crowding_state.py` | Recorded crowding state and precondition checks across drivers |
| `test_reporting.py` | Reporting durability, row identity, recovery, and immutable evidence |
| `test_upstream_unavailable.py` | Upstream refusals, retry schedules, replacement, and recovery |

The existing driver, runner, manifest, proxy, measurement, and other component modules continue to cover their respective components.

## Shared fixtures

`conftest.py` owns shared pytest fixtures and basic manifest/driver helpers. `scenarios/` owns reusable fake-only integration setups, response scripts, and scenario matrices. These are durable test support, not disposable acceptance drivers. Tests and scenarios do not import other test modules or anything under `runs/`.

`fixtures/` contains checked-in reference data and expected measurement vectors. Those files are part of the regression suite and must remain available.

## Disposable acceptance work

One-off acceptance drivers, reports, and generated artifacts belong under ignored `runs/`. The historical work-order drivers and reports were relocated to `runs/acceptance/` in the maintainer's existing workspace. They are not required in a fresh checkout and can be deleted along with other run artifacts. Their former versions remain in git history.

When a one-off acceptance experiment reveals a regression, put the reusable fixture in `scenarios/` and the assertion in a behavior-named test module. Keep the CLI used to produce a particular run under `runs/`.

Run the regression suite with `.venv/bin/python -m pytest tests/ -q`; it requires no installed vendor CLI or live model credentials. Fake-upstream tests bind local loopback sockets, and refusal tests exercise real retry waits.
