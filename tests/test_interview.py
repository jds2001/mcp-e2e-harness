"""Sequential unscored interviews, replay fidelity, and scored-artifact isolation."""
import copy
import hashlib
import json

import pytest
from fake_openrouter import FakeOpenRouter
from scenarios.loop_config import TEST_KEY, loop_config, loop_manifest
from scenarios.reporting import CHECKS, MEASURES

from mcp_e2e_harness.cli import main
from mcp_e2e_harness.interview import interview
from mcp_e2e_harness.reporting import rebuild
from mcp_e2e_harness.runner import run


def read(path):
    return json.loads(path.read_text())


def digests(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file() and 'interview' not in p.relative_to(root).parts}


class InterviewRouter(FakeOpenRouter):
    null_scored = True

    def answer(self, body):
        status, payload = super().answer(body)
        last = next((m['content'] for m in reversed(body['messages']) if m['role'] == 'user'), '')
        if (self.null_scored and status == 200 and last == 'Please list the unfiled notes.'
                and body['messages'][-1]['role'] == 'tool'):
            payload['choices'][0]['message'] = {
                'role': 'assistant', 'content': None, 'reasoning': 'plain recorded reasoning',
                'reasoning_details': [{'type': 'reasoning.text', 'text': 'recorded reasoning'}]}
        return status, payload


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    fake = InterviewRouter()
    monkeypatch.setenv('MCP_E2E_OPENROUTER_UPSTREAM', fake.start())
    monkeypatch.setenv('OPENROUTER_API_KEY', TEST_KEY)
    data = loop_manifest()
    data['cells']['crowded'] = copy.deepcopy(data['cells']['loop-cell'])
    data['cells']['crowded'].update(context='crowded', crowding={
        'procedure': 'neutral-file-triage@2', 'collision_review': 'disjoint fixture names'})
    data.update(checks=CHECKS, measurements=MEASURES)
    config = loop_config(tmp_path, data, repeats=2)
    try:
        result = run(config)
        assert not result.failures
        yield fake, config, config.run_dir / 'loop-cell/A/A1/r01'
    finally:
        fake.stop()


def test_verbatim_sequential_tools_and_isolation(recorded, tmp_path, capsys, monkeypatch):
    fake, config, row = recorded
    before = digests(config.run_dir)
    prefix = read(row / 'loop-session-single.json')
    questions = ['  Why did you stop?\nDo explain. 🦉', 'What did that tool return?']
    ask_file = tmp_path / 'questions.json'
    ask_file.write_text(json.dumps(questions))
    start = len(fake.requests)
    assert main(['interview', '--row', str(row), '--ask-file', str(ask_file)]) == 0
    dest = row / 'interview/01'
    meta = read(dest / 'meta.json')
    assert meta['interview'] is True and meta['aggregate'] is False
    assert meta['reasoning_replayed'] is True
    assert meta['session_sha256'] == hashlib.sha256((row / 'loop-session-single.json').read_bytes()).hexdigest()
    assert meta['repetition'] == 1 and meta['provider_pin'] == read(row / 'meta.json')['provider_pin']
    assert meta['knobs'] == read(row / 'meta.json')['knobs']
    assert meta['api_surface']['wire_surface_mismatches'] == []
    assert meta['spend_usd'] == .004
    assert [q['question'] for q in read(dest / 'questions.json')] == questions
    assert [q['finish_reason'] for q in meta['questions']] == ['stop', 'stop']
    assert [q['tool_calls'] for q in meta['questions']] == [1, 1]
    transcript = read(dest / 'interview-session.json')
    assert transcript['messages'][:len(prefix['messages'])] == prefix['messages']
    requests = fake.requests[start:]
    assert len(requests) == 4
    assert requests[0]['messages'] == prefix['messages'] + [{'role': 'user', 'content': questions[0]}]
    assert requests[0]['tools'] == prefix['tool_definitions']
    assert requests[0]['provider'] == read(row / 'loop-turn.json')['provider']
    assert requests[2]['messages'][-2]['content'] == fake.answer_text
    assert requests[2]['messages'][-1]['content'] == questions[1]
    wire = [json.loads(line) for line in (dest / 'api-surface.jsonl').read_text().splitlines()]
    assert wire[0]['seq'] == 1 and wire[0]['usage_prompt_tokens']
    assert len((dest / 'trace.jsonl').read_text().splitlines()) == 2
    assert digests(config.run_dir) == before
    first = {p.name: p.read_bytes() for p in dest.iterdir() if p.is_file()}
    interview(row, ['again'], log=lambda _: None)
    assert (row / 'interview/02/meta.json').exists()
    assert first == {p.name: p.read_bytes() for p in dest.iterdir() if p.is_file()}
    assert main(['report', '--run-dir', str(config.run_dir)]) == 2
    assert digests(config.run_dir) == before
    # An explicit rebuild enumerates rows, and must skip interview trees even if
    # an interview contains something shaped like a scored row.
    monkeypatch.setattr('mcp_e2e_harness.reporting.now', lambda: 'fixed-time')
    rebuild(config.run_dir, overwrite=True, log=lambda _: None)
    rebuilt = (config.run_dir / 'run-manifest.json').read_bytes()
    (dest / 'nested').mkdir()
    (dest / 'nested/meta.json').write_bytes((row / 'meta.json').read_bytes())
    rebuild(config.run_dir, overwrite=True, log=lambda _: None)
    assert (config.run_dir / 'run-manifest.json').read_bytes() == rebuilt
    output = capsys.readouterr().out
    assert output.index('interview  :') < output.index('ESTIMATE') < output.index('question 1:')
    assert 'answer 2:' in output


def test_crowded_does_not_repeat_preturn(recorded):
    fake, config, _ = recorded
    row = config.run_dir / 'crowded/A/A1/r01'
    prefix = read(next(row.glob('loop-session-*.json')))['messages']
    before = digests(config.run_dir)
    start = len(fake.requests)
    dest = interview(row, ['explain'], log=lambda _: None)
    assert len(fake.requests[start:]) == 2
    assert fake.requests[start]['messages'][:-1] == prefix
    assert any(m.get('content') == 'Filed four notes.' for m in prefix)
    assert not (dest / 'crowding.json').exists()
    assert digests(config.run_dir) == before


@pytest.mark.parametrize('budget,asked', [(0, [False, False]), (.002, [True, False]), (.001, [True, False])])
def test_budget(recorded, budget, asked):
    fake, config, row = recorded
    before = digests(config.run_dir)
    start = len(fake.requests)
    dest = interview(row, ['one', 'two'], budget, log=lambda _: None)
    meta = read(dest / 'meta.json')
    assert [q['asked'] for q in meta['questions']] == asked
    assert meta['budget_stop']
    assert meta['spend_usd'] == budget
    assert len(fake.requests[start:]) == round(budget / .001)
    assert meta['questions'][1]['not_asked_reason'] == 'budget'
    assert digests(config.run_dir) == before


@pytest.mark.parametrize('field,value', [('driver_family', 'product'), ('harness_failure', 'broken'),
                                       ('precondition_unmet', {'cause': 'state'}),
                                       ('upstream_unavailable', {'http_status': 503}),
                                       ('scored_turn_reached', False), ('session', None)])
def test_refusal_writes_nothing(recorded, field, value, capsys):
    _, config, row = recorded
    if field == 'session':
        (row / 'loop-session-single.json').unlink()
    else:
        meta = read(row / 'meta.json')
        meta[field] = value
        (row / 'meta.json').write_text(json.dumps(meta))
    before = digests(config.run_dir)
    assert main(['interview', '--row', str(row), '--ask', 'why']) == 2
    assert 'REFUSED:' in capsys.readouterr().out
    assert not (row / 'interview').exists()
    assert digests(config.run_dir) == before


def test_endpoint_refusal_continues(recorded):
    fake, _, row = recorded
    fake.status_sequence = [400, 200, 200]
    dest = interview(row, ['refused', 'answer'], log=lambda _: None)
    meta = read(dest / 'meta.json')
    assert 'HTTP 400' in meta['questions'][0]['error']
    assert meta['questions'][1]['answer'] == fake.answer_text
    assert fake.requests[-2]['messages'][-2:] == [
        {'role': 'user', 'content': 'refused'}, {'role': 'user', 'content': 'answer'}]


@pytest.mark.parametrize('args', [[], ['--budget-usd', 'nan'], ['--budget-usd', '-1']])
def test_bad_input_no_write(recorded, args):
    _, _, row = recorded
    argv = ['interview', '--row', str(row)] + (['--ask', 'why'] if args else []) + args
    assert main(argv) == 2
    assert not (row / 'interview').exists()


def test_repeated_asks_then_file_and_invalid_file(recorded, tmp_path):
    fake, _, row = recorded
    path = tmp_path / 'ask.json'
    path.write_text('{"bad": "shape"}')
    assert main(['interview', '--row', str(row), '--ask-file', str(path)]) == 2
    assert not (row / 'interview').exists()
    path.write_text('["third"]')
    assert main(['interview', '--row', str(row), '--ask', 'first', '--ask', 'second',
                 '--ask-file', str(path)]) == 0
    assert [q['question'] for q in read(row / 'interview/01/questions.json')] == ['first', 'second', 'third']
    assert fake.requests[-2]['messages'][-1]['content'] == 'third'


def test_recorded_tool_schema_replayed_and_legacy_disclosed(recorded):
    fake, _, row = recorded
    path = row / 'loop-session-single.json'
    session = read(path)
    session['tool_definitions'][0]['function']['description'] = 'Recorded schema, since changed upstream.'
    path.write_text(json.dumps(session))
    start = len(fake.requests)
    dest = interview(row, ['why'], log=lambda _: None)
    assert fake.requests[start]['tools'] == session['tool_definitions']
    assert read(dest / 'meta.json')['tool_definitions_source'] == 'recorded session'
    del session['tool_definitions']
    path.write_text(json.dumps(session))
    dest = interview(row, ['why'], log=lambda _: None)
    assert 'unverifiable' in read(dest / 'meta.json')['tool_definitions_source']


def test_surface_drift_and_pin_breach_recorded(recorded):
    fake, _, row = recorded
    path = row / 'loop-session-single.json'
    session = read(path)
    original = path.read_bytes()
    session['offered_tools'].append('nonexistent_tool')
    path.write_text(json.dumps(session))
    start = len(fake.requests)
    dest = interview(row, ['why'], log=lambda _: None)
    assert len(fake.requests) == start
    assert read(dest / 'meta.json')['questions'][0]['breach']['kind'] == 'surface_drift'
    path.write_bytes(original)
    fake.provider_sequence = ['WrongProvider']
    dest = interview(row, ['why'], log=lambda _: None)
    meta = read(dest / 'meta.json')
    assert meta['questions'][0]['breach']['kind'] == 'provider_mismatch'
    assert meta['questions'][0]['served_providers'][0]['provider'] == 'WrongProvider'
    assert meta['spend_usd'] == .001


@pytest.mark.parametrize('outcome', ['step_cap', 'context_length', 'null_final_content'])
def test_consumer_outcomes_are_eligible_and_continue(recorded, outcome):
    fake, _, row = recorded
    path = row / 'meta.json'
    meta = read(path)
    meta['consumer_limit'] = {'cause': outcome}
    path.write_text(json.dumps(meta))
    questions = ['Please list the unfiled notes.', 'Please list the unfiled notes.']
    if outcome == 'step_cap':
        fake.null_scored = False
        fake.runaway = True
    elif outcome == 'context_length':
        fake.status_sequence = [400, 400]
    dest = interview(row, questions, log=lambda _: None)
    meta = read(dest / 'meta.json')
    assert [q['asked'] for q in meta['questions']] == [True, True]
    if outcome == 'context_length':
        assert all('HTTP 400' in q['error'] for q in meta['questions'])
    else:
        assert [q['consumer_limit']['cause'] for q in meta['questions']] == [outcome, outcome]


def test_budget_is_checked_before_retries(recorded, monkeypatch):
    from mcp_e2e_harness.interview import InterviewClient

    fake, _, row = recorded
    fake.status_sequence = [429, 200, 200]
    original = InterviewClient.before_request
    checks = []

    def check(client):
        checks.append(client.retries)
        original(client)

    monkeypatch.setattr(InterviewClient, 'before_request', check)
    start = len(fake.requests)
    dest = interview(row, ['one', 'two'], .002, log=lambda _: None)
    meta = read(dest / 'meta.json')
    assert len(fake.requests) - start == 3
    assert checks == [0, 1, 1]
    assert meta['budget_stop']['cause'] == 'budget'
    assert meta['questions'][1]['asked'] is False


def test_server_start_failure_is_durable(recorded, monkeypatch):
    from mcp_e2e_harness.loop_consumer import LoopError

    _, config, row = recorded
    before = digests(config.run_dir)

    def fail(*args, **kwargs):
        raise LoopError('server cannot start')

    monkeypatch.setattr('mcp_e2e_harness.interview.open_servers', fail)
    dest = interview(row, ['one', 'two'], log=lambda _: None)
    meta = read(dest / 'meta.json')
    assert meta['harness_failure'] == 'server cannot start'
    assert all(q['not_asked_reason'] == 'server cannot start' for q in meta['questions'])
    assert meta['api_surface']['verified'] is False
    assert meta['reasoning_replayed'] is None
    assert digests(config.run_dir) == before
