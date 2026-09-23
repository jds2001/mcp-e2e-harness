"""Fake-only crowding state scenarios shared by regression tests."""
import json
import sys

from mcp_e2e_harness.drivers.base import TurnSpec
from scenarios.crowding import PreturnRouter, ProductPreturnDriver


class StateRouter(PreturnRouter):
    filed_count = 2

    def answer(self, body):
        status, payload = super().answer(body)
        if self._is_probe(body):
            return status, payload
        messages = body['messages']
        user = max(i for i, m in enumerate(messages) if m['role'] == 'user')
        if not messages[user]['content'].startswith("You're helping tidy") or self.preturn_attempt != 1:
            return status, payload
        calls = sum(m['role'] == 'tool' for m in messages[user + 1:])
        message = {'role': 'assistant', 'content': 'Done for now.'}
        if calls < self.filed_count:
            message.update(content=None, tool_calls=[{
                'id': f'file_{calls}', 'type': 'function', 'function': {
                    'name': 'mcp__shared_notes__file_note',
                    'arguments': json.dumps({'note_id': f'n{calls + 1:02d}', 'folder': 'logistics'})}}])
        payload['choices'][0]['message'] = message
        payload['choices'][0]['finish_reason'] = 'tool_calls' if message.get('tool_calls') else 'stop'
        return status, payload


class FiveRouter(StateRouter):
    filed_count = 5


class StateProductDriver(ProductPreturnDriver):
    def __init__(self, count=0, missing=False, unreadable=False):
        super().__init__(False)
        self.count, self.missing, self.unreadable = count, missing, unreadable

    def build_turn(self, ctx):
        if ctx.session[0] != 'open':
            return super().build_turn(ctx)
        path = str(ctx.dest / 'distractor-state.json')
        content = '{broken' if self.unreadable else json.dumps({
            'filed': {f'n{i:02d}': 'arbitrary' for i in range(1, self.count + 1)}})
        code = f'from pathlib import Path; p=Path({path!r}); p.write_text({content!r})'
        if self.missing:
            code += '; p.unlink()'
        return TurnSpec([sys.executable, '-c', code], stdin_text=ctx.prompt)


CASES = {
    'two': {'router_factory': StateRouter, 'failures': ()},
    'five': {'router_factory': FiveRouter, 'failures': ()},
    'product-empty': {'product': True, 'product_driver': StateProductDriver()},
    'product-pass': {'product': True, 'product_driver': StateProductDriver(4)},
    'missing': {'product': True, 'product_driver': StateProductDriver(4, missing=True)},
    'unreadable': {'product': True, 'product_driver': StateProductDriver(4, unreadable=True)},
    'clean': {'failures': ()}, 'replaced': {}, 'disabled': {'retries': 0},
    'budget': {'failures': (1, 2, 3, 4), 'budget': .004},
}
