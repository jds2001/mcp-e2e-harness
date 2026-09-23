"""Fake-only reporting scenarios shared by regression tests."""


CHECKS = [{'id': 'must-fail', 'applies_to': {'tool': '*'},
           'assert': {'present': ['/__wo11_missing']}}]
MEASURES = [{'name': 'coverage', 'measure': 'answer-coverage@1', 'applies_to': {'tool': '*'},
             'reference': '/response/content/0/text', 'floor': 16}]
CASES = {'flagless-replaced': {'repeats': None}, 'repeated-replaced': {'repeats': 2},
         'flagless-clean': {'repeats': None, 'failures': ()}}
