"""WO-11 checks on every acceptance arm, including injected evaluator failure."""
import sys
from pathlib import Path
from unittest.mock import patch

from wo9_acceptance import experiment

CHECKS = [{'id': 'must-fail', 'applies_to': {'tool': '*'},
           'assert': {'present': ['/__wo11_missing']}}]
MEASURES = [{'name': 'coverage', 'measure': 'answer-coverage@1', 'applies_to': {'tool': '*'},
             'reference': '/response/content/0/text', 'floor': 16}]
CASES = {'flagless-replaced': {'repeats': None}, 'repeated-replaced': {'repeats': 2},
         'flagless-clean': {'repeats': None, 'failures': ()}}


def injected(*args, **kwargs):
    raise RuntimeError('WO-11 injected evaluator exception')


if __name__ == '__main__':
    root = Path(sys.argv[1])
    for name, kwargs in CASES.items():
        experiment(root / name, checks=CHECKS, measurements=MEASURES, **kwargs)
        print(name, flush=True)
    with patch('mcp_e2e_harness.checks.evaluate_checks', injected):
        experiment(root / 'checks-exception', checks=CHECKS, measurements=MEASURES)
    with patch('mcp_e2e_harness.measurements.compute_measurements', injected):
        experiment(root / 'measurements-exception', checks=CHECKS, measurements=MEASURES)
