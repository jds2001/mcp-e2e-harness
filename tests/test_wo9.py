"""S17/S18 classification, retained attempts, budgets, and operator evidence."""
import json

import pytest
from test_driver_loop import loop_config, loop_manifest
from wo9_acceptance import experiment

from mcp_e2e_harness.drivers.base import TurnSpec
from mcp_e2e_harness.drivers.loop import LoopDriver
from mcp_e2e_harness.runner import HarnessError, preflight


@pytest.mark.parametrize('case,kwargs,counts,unmet', [
    ('disabled', {'retries': 0}, (2, 1, 2), 1),
    ('replaced', {}, (2, 2, 3), 1),
    ('exhausted', {'failures': (1, 2, 3, 4)}, (2, 1, 5), 4),
    ('all', {'failures': (1, 2), 'retries': 0}, (2, 0, 2), 2),
    ('crash', {'crash': True}, (2, 1, 2), 0),
    ('null', {'failures': (), 'scored_null': True}, (2, 2, 2), 0),
    ('budget', {'failures': (1, 2, 3, 4), 'budget': 0.004}, (2, 0, 2), 2),
    ('cell-budget', {'failures': (1, 2, 3, 4), 'cell_budget': 0.004}, (2, 0, 2), 2),
    ('clean', {'failures': ()}, (2, 2, 2), 0),
])
def test_attempts(tmp_path, case, kwargs, counts, unmet):
    config, result = experiment(tmp_path, **kwargs)
    manifest = json.loads((config.run_dir / 'run-manifest.json').read_text())
    assert manifest['invocation_counts']['loop-cell']['A1'] == dict(
        zip(('asked', 'reached', 'attempts'), counts, strict=True))
    assert len(manifest['preconditions_unmet']) == unmet
    assert len(result.results) == counts[2]
    assert len({row["cwd"] for row in result.results}) == counts[2]
    slots = list(manifest['slots'].values())
    log = (tmp_path / 'progress.txt').read_text()
    for path, item in manifest['preconditions_unmet'].items():
        row = json.loads((config.run_dir / path / 'meta.json').read_text())
        reported = json.loads((config.run_dir / path / 'loop-result-crowding.json').read_text())['consumer_limit']
        assert row['precondition_unmet'] == {**reported, 'stage': 'crowding_preturn',
                                             'expected': ['n01', 'n02', 'n03', 'n04'],
                                             'observed': ['n01'], 'passed': False}
        assert row['harness_failure'].startswith('null_final_content:')
        assert row['harness_failure'] in log
        assert not row['consumer_limit'] and not row['answer_chars']
        assert not row['scored_turn_reached'] and not row['crowding']['preturn_ok']
        assert row['api_surface']['requests_recorded'] == 2
        assert item['attempt'] == row['attempt']
        assert not (config.run_dir / path / 'loop-result-turn.json').exists()
    if case in {'all', 'budget', 'cell-budget'}:
        assert result.zero_trace_cells == ['loop-cell']
        assert (config.run_dir / 'loop-cell' / 'CELL-VOID.json').exists()
        assert result.failures == 1
    elif case == 'crash':
        assert result.failures == 1
    else:
        assert result.failures == 0
    if case == 'replaced':
        assert slots[0]['result'].endswith('/r01/attempt-02')
        assert 'REPLACEMENT: null_final_content:' in log
        assert (config.run_dir / slots[0]['result'] / 'answer.txt').read_text()
    if case in {'exhausted', 'disabled', 'all'}:
        assert slots[0]['status'] == ('replacement_disabled' if kwargs.get('retries') == 0 else 'exhausted')
    if 'budget' in case:
        assert manifest['budget']['stops'][0]['scope'] == ('cell' if case == 'cell-budget' else 'run')
        assert slots[0]['status'] == 'budget_stopped'
    if case == 'clean':
        assert not list(config.run_dir.rglob('attempt-*'))
    if case == 'null':
        assert len(manifest['consumer_limits']) == 2
    estimate = manifest['pre_run']['loop']['precondition_replacements']
    assert estimate['worst_case_usd'] == pytest.approx(estimate['expected_usd'] * (1 + config.precondition_retries))


@pytest.mark.parametrize('cause', ['null_final_content', 'step_cap', 'context_length'])
def test_reported_preturn_causes(tmp_path, cause):
    result = tmp_path / 'result.json'
    config = tmp_path / 'turn.json'
    config.write_text(json.dumps({'result_path': str(result)}))
    turn = TurnSpec(['python', '-m', 'loop', '--config', str(config)], stdin_text=None)
    outcome = {'cause': cause, 'detail': 'full detail', 'reasoning_tail_json': None,
               'reasoning_tail_matches_tools': None}
    result.write_text(json.dumps({'consumer_limit': outcome}))
    driver = LoopDriver()
    assert driver.preturn_outcome(turn, 4) == outcome
    assert driver.preturn_outcome(turn, 1) is None
    result.write_text(json.dumps({'consumer_limit': outcome, 'breach': {'kind': 'wrong provider'}}))
    assert driver.preturn_outcome(turn, 4) is None


@pytest.mark.parametrize('value', [-1, 1.5, True])
def test_invalid_retries(tmp_path, value):
    config = loop_config(tmp_path, loop_manifest(), precondition_retries=value)
    with pytest.raises(HarnessError, match='precondition-retries'):
        preflight(config)


@pytest.mark.parametrize('crash', [False, True])
def test_product_preturn_observed(tmp_path, crash):
    config, result = experiment(tmp_path, product=True, crash=crash)
    assert len(result.results) == 2
    for row in result.results:
        assert row['precondition_unmet'] is None
        assert row['scored_turn_reached'] is False
    assert result.failures == 3


@pytest.mark.parametrize('cause', ['null_final_content', 'step_cap', 'context_length'])
def test_scored_outcomes_never_replaced(tmp_path, monkeypatch, cause):
    from mcp_e2e_harness import runner

    original = runner.run_one

    def scored(*args, **kwargs):
        row = original(*args, **kwargs)
        row.update(consumer_limit={'cause': cause}, scored_turn_reached=True,
                   answer_chars=0, answer_sha256_16=None)
        return row

    monkeypatch.setattr(runner, 'run_one', scored)
    config = loop_config(tmp_path, loop_manifest(), dry_run=True, repeats=2, precondition_retries=3)
    result = runner.run(config)
    assert len(result.results) == 2
    assert list(result.consumer_limits.values()) == [cause, cause]
    assert all(row['attempt'] == 1 for row in result.results)


def test_breach_overrides_precondition(tmp_path, monkeypatch):
    original = LoopDriver.after_turn

    def breached(self, *args, **kwargs):
        extra = original(self, *args, **kwargs)
        extra['breaches'].append('fixture provider breach')
        return extra

    monkeypatch.setattr(LoopDriver, 'after_turn', breached)
    config, result = experiment(tmp_path)
    assert len(result.results) == 1
    assert result.results[0]['precondition_unmet'] is None
    assert result.results[0]['harness_failure'] == 'fixture provider breach'
    assert result.failures > 0
    assert not json.loads((config.run_dir / 'run-manifest.json').read_text())['preconditions_unmet']


@pytest.mark.parametrize("value", ["-1", "text", "1.5"])
def test_cli_rejects_invalid_retries(value):
    from mcp_e2e_harness.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["run", "--precondition-retries", value])
    assert exc.value.code == 2
