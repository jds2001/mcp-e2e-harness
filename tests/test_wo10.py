import json

import pytest
from wo9_acceptance import experiment
from wo10_acceptance import CASES

from mcp_e2e_harness.crowding import PROCEDURES


@pytest.mark.parametrize('filed,passed', [
    (['n01', 'n02', 'n03', 'n04'], True), ([], False), (['n01', 'n02'], False),
    (['n01', 'n02', 'n03', 'n04', 'n05'], False), (['n01', 'n02', 'n03', 'wrong'], False),
])
def test_predicates(filed, passed):
    for procedure in PROCEDURES.values():
        check = procedure.check_state({'filed': dict.fromkeys(filed, 'any-folder')})
        assert check['passed'] is passed
        assert check['observed'] == sorted(filed)


@pytest.mark.parametrize('case', list(CASES))
def test_state_acceptance(tmp_path, case):
    config, result = experiment(tmp_path, **CASES[case])
    manifest = json.loads((config.run_dir / 'run-manifest.json').read_text())
    cell = config.cells[0]
    counts = manifest['invocation_counts'][cell]['A1']
    assert manifest['cell_marks'][cell]['answers']['A1']['invocations'] == counts['reached']
    if case in {'two', 'five'}:
        row = result.results[0]
        assert row['precondition_unmet']['cause'] == 'state_mismatch'
        assert row['precondition_unmet']['expected'] == ['n01', 'n02', 'n03', 'n04']
        assert len(row['precondition_unmet']['observed']) == (2 if case == 'two' else 5)
        assert row['crowding']['preturn_ok'] is False
        assert result.results[1]['crowding']['state_check']['passed'] is True
        assert counts == {'asked': 2, 'reached': 2, 'attempts': 3}
    elif case == 'product-empty':
        assert counts == {'asked': 2, 'reached': 0, 'attempts': 8}
        assert len(result.preconditions_unmet) == 8
    elif case in {'missing', 'unreadable'}:
        assert counts == {'asked': 2, 'reached': 0, 'attempts': 2}
        assert not result.preconditions_unmet
        assert result.failures == 3
        assert all('state unavailable' in row['harness_failure'] for row in result.results)
    elif case in {'product-pass', 'clean'}:
        assert counts == {'asked': 2, 'reached': 2, 'attempts': 2}
        assert not list(config.run_dir.rglob('attempt-*'))
    elif case == 'disabled':
        assert list(result.slots.values())[0]['status'] == 'replacement_disabled'
    elif case == 'budget':
        text = (tmp_path / 'progress.txt').read_text()
        assert '$0.004' in text and '$0.00 ' not in text
        assert 'not started' not in text
