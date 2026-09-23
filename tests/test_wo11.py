import hashlib
import json

import pytest
from wo9_acceptance import experiment
from wo11_acceptance import CASES, CHECKS, MEASURES

from mcp_e2e_harness import checks, measurements
from mcp_e2e_harness.cli import main
from mcp_e2e_harness.reporting import rebuild


def row_digests(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file()
            and path.name not in {'run-manifest.json', 'checks-report.json'}}


@pytest.mark.parametrize('case', CASES)
def test_replacement_check_matrix(tmp_path, case):
    config, result = experiment(tmp_path, checks=CHECKS, measurements=MEASURES, **CASES[case])
    assert not result.failures
    report = json.loads((config.run_dir / 'checks-report.json').read_text())
    failures = report['checks'][0]['failures']
    assert failures and report['checks'][0]['outcome'] == 'fail'
    for failure in failures:
        meta = json.loads((config.run_dir / failure['invocation'] / 'meta.json').read_text())
        for key in ('cell', 'prompt_id', 'repetition', 'attempt'):
            assert failure[key] == meta[key]
    assert failures[0]['attempt'] == (1 if case == 'flagless-clean' else 2)
    assert failures[0]['repetition'] == (1 if case == 'repeated-replaced' else None)
    before = row_digests(config.run_dir)
    with pytest.raises(ValueError, match='overwrite'):
        rebuild(config.run_dir)
    recovered = rebuild(config.run_dir, overwrite=True)
    assert row_digests(config.run_dir) == before
    assert recovered['checks'] == {'must-fail': 'fail'}
    assert recovered['rebuilt']['harness_version']
    assert recovered['cell_marks']['loop-cell']['served_providers']['FakeProvider'] > 0
    assert recovered['cell_gates']['loop-cell']['verdict'] == 'pass'
    assert len(recovered['results']) == len(result.results)


def test_identity_is_not_the_path():
    label = 'wrong-cell/wrong-group/wrong-prompt/attempt-nonsense'
    metadata = {label: {'cell': 'real', 'prompt_id': 'P', 'repetition': None, 'attempt': 4}}
    report = checks.evaluate_checks(CHECKS, {label: [{'tool': 'test'}]}, metadata=metadata)
    assert set(report[0]['cells']) == {'real'}
    failure = report[0]['failures'][0]
    assert failure == {'invocation': label, **metadata[label], 'index': None,
                       'details': ['/__wo11_missing missing']}


@pytest.mark.parametrize('stage', ['checks', 'measurements'])
def test_stage_exception_retains_run(tmp_path, monkeypatch, stage):
    def fail(*args, **kwargs):
        checkpoint = json.loads((tmp_path / 'run/run-manifest.json').read_text())
        assert checkpoint['reporting'][stage]['status'] == 'running'
        assert len(checkpoint['results']) == 2
        assert list((tmp_path / 'run').rglob('meta.json'))
        raise RuntimeError('injected stage exception')

    monkeypatch.setattr(checks if stage == 'checks' else measurements,
                        'evaluate_checks' if stage == 'checks' else 'compute_measurements', fail)
    config, result = experiment(tmp_path, failures=(), checks=CHECKS, measurements=MEASURES)
    assert result.failures > 0
    record = json.loads((config.run_dir / 'run-manifest.json').read_text())
    assert record['reporting'][stage] == {'status': 'error', 'exception': 'RuntimeError: injected stage exception'}
    report = json.loads((config.run_dir / 'checks-report.json').read_text())
    if stage == 'checks':
        assert all(entry['outcome'] == 'error' and 'injected' in entry['error'] for entry in report['checks'])
    else:
        assert report['checks'][0]['outcome'] == 'fail'
        assert record['measurements'] is None
    text = (tmp_path / 'progress.txt').read_text()
    assert 'REPORTING ERROR' in text and 'missing completed' in text
    from mcp_e2e_harness import cli
    monkeypatch.setattr(cli, 'run', lambda _: result)
    assert main(['run', '--manifest', str(config.manifest.path)]) == 1


def test_killed_run_recovery_and_unknown_facts(tmp_path):
    config, result = experiment(tmp_path, checks=CHECKS, repeats=None)
    # Mimic a pre-WO-11 run: rows survive, no checkpoint or startup snapshot.
    for name in ('run-manifest.json', 'checks-report.json', 'manifest-source.json', 'run-input.json'):
        (config.run_dir / name).unlink()
    before = row_digests(config.run_dir)
    assert main(['report', '--run-dir', str(config.run_dir)]) == 0
    assert row_digests(config.run_dir) == before
    rebuilt = json.loads((config.run_dir / 'run-manifest.json').read_text())
    assert rebuilt['pre_run'] is None and rebuilt['budget']['stops'] is None
    assert rebuilt['budget']['spent_usd'] is None
    assert 'pre_run' in rebuilt['unrecoverable']
    assert len(rebuilt['preconditions_unmet']) == 1
    assert len(rebuilt['replacement_events']) == 1
    assert rebuilt['replacement_events'][0]['cause'] == 'null_final_content'
    slot = next(iter(rebuilt['slots'].values()))
    assert slot['status'] == 'scored' and slot['result'].endswith('attempt-02')
    assert len(rebuilt['results']) == len(result.results)
    assert main(['report', '--run-dir', str(config.run_dir)]) == 2
    again = rebuild(config.run_dir, overwrite=True)
    assert again['invocation_counts']['loop-cell']['A1']['asked'] is None
    assert 'budget' in again['unrecoverable']
    assert row_digests(config.run_dir) == before


def test_rebuild_refuses_wrong_manifest(tmp_path):
    config, _ = experiment(tmp_path, failures=(), checks=CHECKS, repeats=None)
    original = (config.run_dir / 'run-manifest.json').read_bytes()
    wrong = tmp_path / 'wrong.json'
    wrong.write_text(config.manifest.raw_bytes.decode() + ' ')
    with pytest.raises(ValueError, match='matching'):
        rebuild(config.run_dir, overwrite=True, manifest_path=wrong)
    assert (config.run_dir / 'run-manifest.json').read_bytes() == original


def test_killed_run_with_snapshot_recovers_requested_counts(tmp_path):
    config, _ = experiment(tmp_path, checks=CHECKS, repeats=2)
    (config.run_dir / 'run-manifest.json').unlink()
    (config.run_dir / 'checks-report.json').unlink()
    before = row_digests(config.run_dir)
    record = rebuild(config.run_dir)
    assert record['invocation_counts']['loop-cell']['A1'] == {
        'asked': 2, 'observed_slots': 2, 'reached': 2, 'attempts': 3}
    assert record['selection']['precondition_retries'] == 3
    assert row_digests(config.run_dir) == before


def test_missing_recorded_trace_is_reporting_error(tmp_path):
    config, _ = experiment(tmp_path, failures=(), checks=CHECKS, repeats=None)
    next(config.run_dir.rglob('trace.jsonl')).unlink()
    record = rebuild(config.run_dir, overwrite=True)
    assert record['reporting']['checks']['status'] == 'error'
    assert 'FileNotFoundError' in record['reporting']['checks']['exception']
    assert record['checks'] == {'must-fail': 'error'}
