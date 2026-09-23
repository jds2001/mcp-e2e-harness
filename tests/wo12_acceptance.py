"""WO-12 exhausted upstream refusals; all requests go to a local fake."""
import os
import sys
from pathlib import Path

from fake_openrouter import FakeOpenRouter
from test_driver_loop import loop_config, loop_manifest

from mcp_e2e_harness.runner import run


class RefusingRouter(FakeOpenRouter):
    def __init__(self, refusals=3, second_step=False, status=429):
        super().__init__()
        self.refusals = refusals
        self.second_step = second_step
        self.status = status

    def answer(self, body):
        status, payload = super().answer(body)
        if (not self._is_probe(body) and self.refusals
                and (not self.second_step or body['messages'][-1]['role'] == 'tool')):
            self.refusals -= 1
            return self.status, {'error': {'message': 'temporarily unavailable', 'metadata': {
                'provider_name': 'FakeProvider', 'raw': 'WO-12 refusal fixture'}}}
        return status, payload


def experiment(root, *, refusals=3, second_step=False, status=429, retries=3, budget=None, repeats=2):
    root.mkdir(parents=True, exist_ok=True)
    fake = RefusingRouter(refusals, second_step, status)
    old = {key: os.environ.get(key) for key in ('MCP_E2E_OPENROUTER_UPSTREAM', 'OPENROUTER_API_KEY')}
    os.environ['MCP_E2E_OPENROUTER_UPSTREAM'] = fake.start()
    os.environ['OPENROUTER_API_KEY'] = 'sk-or-wo12-fake-0123456789'
    logs = []
    try:
        data = loop_manifest()
        data['checks'] = [{'id': 'observed-tools', 'applies_to': {'tool': '*'},
                           'assert': {'present': ['/tool']}}]
        config = loop_config(root, data, repeats=repeats, precondition_retries=retries,
                             budget_usd=budget, log=logs.append)
        result = run(config)
        (root / 'progress.txt').write_text('\n'.join(logs) + '\n')
        return config, result
    finally:
        fake.stop()
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


CASES = {'part-a': {'second_step': True, 'retries': 0}, 'replaced': {},
         'exhausted': {'refusals': 24}, 'budget': {'second_step': True, 'budget': .002},
         'server-error': {'status': 503}}

if __name__ == '__main__':
    for name, kwargs in CASES.items():
        experiment(Path(sys.argv[1]) / name, **kwargs)
        print(name, flush=True)
