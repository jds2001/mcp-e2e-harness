"""Upstream refusal diagnostics, retries, replacement, and recovery."""
import io
import json
import urllib.error
from unittest.mock import Mock

import pytest
from scenarios.upstream_refusals import CASES, experiment

from mcp_e2e_harness import loop_consumer as consumer
from mcp_e2e_harness.reporting import rebuild
from mcp_e2e_harness.runner import replacement_cause


@pytest.mark.parametrize('case', CASES)
def test_refusal_artifacts(tmp_path, case):
    config, result = experiment(tmp_path, **CASES[case])
    first = result.results[0]
    unavailable = first['upstream_unavailable']
    assert unavailable == {'http_status': 503 if case == 'server-error' else 429,
                           'provider': 'FakeProvider', 'attempts': 3, 'retry_schedule_s': [2.0, 5.0],
                           'step': 2 if case in {'part-a', 'budget'} else 1,
                           'tool_calls_before': 1 if case in {'part-a', 'budget'} else 0}
    assert first['loop']['retries'] == 2
    assert first['harness_failure'].startswith('upstream_unavailable: provider=FakeProvider')
    assert not first['consumer_limit'] and not first['answer_chars']
    slot = next(iter(result.slots.values()))
    counts = result.invocation_counts['loop-cell']['A1']
    assert counts['attempts_unmet'] == 0
    assert slot['attempts_unmet'] == 0
    text = (tmp_path / 'progress.txt').read_text()
    assert first['harness_failure'] in text
    if case == 'part-a':
        assert slot['status'] == 'failed' and result.failures == 1
        assert counts['attempts_unavailable'] == 1
        record = json.loads((config.run_dir / 'run-manifest.json').read_text())
        assert record['cell_marks']['loop-cell']['answers']['A1']['invocations'] == 2
        assert record['cell_marks']['loop-cell']['answers']['A1']['answered'] == 1
    elif case == 'exhausted':
        assert slot['status'] == 'exhausted' and slot['cause'] == 'upstream_unavailable'
        assert slot['attempts_unavailable'] == slot['attempts'] == 4
        assert counts['attempts_unavailable'] == 8 and result.failures == 1
        assert result.zero_trace_cells == ['loop-cell']
    elif case == 'budget':
        assert len(result.results) == 1 and slot['status'] == 'budget_stopped'
        assert result.budget_stops
    else:
        assert slot['status'] == 'scored' and slot['attempts'] == 2
        assert slot['attempts_unavailable'] == 1 and counts['attempts_unavailable'] == 1
        assert slot['result'].endswith('/attempt-02') and result.failures == 0
        assert 'REPLACEMENT: upstream_unavailable:' in text
    rebuilt = rebuild(config.run_dir, overwrite=True)
    # A recorded budget event remains authoritative over the unfinished slot.
    assert next(iter(rebuilt['slots'].values()))['status'] == slot['status']
    assert rebuilt['failures'] == result.failures
    assert rebuilt['invocation_counts']['loop-cell']['A1']['attempts_unavailable'] == counts['attempts_unavailable']


@pytest.mark.parametrize('status', [429, 500, 502, 503, 504, 'timeout', 'wrapped-timeout'])
def test_http_retry_record(monkeypatch, status):
    config = consumer.TurnConfig('http://localhost', 'TEST_WO12_KEY', 'fake', {}, {}, None, None,
                                 'loop-scaffold@1', 'turn', '/unused')
    monkeypatch.setenv('TEST_WO12_KEY', 'fake-key')
    sleeps = []
    monkeypatch.setattr(consumer.time, 'sleep', sleeps.append)
    payload = json.dumps({'error': {'metadata': {'provider_name': 'NamedProvider'}}}).encode()

    def refuse(*args, **kwargs):
        if status == 'timeout':
            raise TimeoutError('fixture timed out')
        if status == 'wrapped-timeout':
            raise urllib.error.URLError(TimeoutError('fixture timed out'))
        raise urllib.error.HTTPError('http://localhost', status, 'fixture', {}, io.BytesIO(payload))

    request = Mock(side_effect=refuse)
    monkeypatch.setattr(consumer.urllib.request, 'urlopen', request)
    client = consumer.ChatClient(config)
    with pytest.raises(consumer.UpstreamUnavailable) as exc:
        client.complete({})
    assert request.call_count == 3 and client.retries == 2 and sleeps == [2.0, 5.0]
    assert exc.value.record['http_status'] == (status if isinstance(status, int) else None)
    assert exc.value.record['provider'] == ('NamedProvider' if isinstance(status, int) else None)


@pytest.mark.parametrize('failure', ['spawn failed', 'API recorder forwarding error', 'runner exited 2',
                                     'timeout after 10s', 'wire tools array differs'])
def test_unrelated_failures_never_replaced(failure):
    assert replacement_cause({'harness_failure': failure}) is None
    assert replacement_cause({'harness_failure': failure, 'upstream_unavailable': {'http_status': 429}}) is None


def test_breach_prevents_replacement():
    row = {'harness_failure': 'upstream_unavailable: provider=test', 'upstream_unavailable': {'http_status': 429},
           'breaches': ['wrong provider']}
    assert replacement_cause(row) is None


@pytest.mark.parametrize('breach', [False, True])
def test_preturn_unavailability_and_breach_precedence(tmp_path, monkeypatch, breach):
    from scenarios.crowding import experiment as crowded_experiment
    from scenarios.upstream_refusals import RefusingRouter

    from mcp_e2e_harness.drivers.loop import LoopDriver

    if breach:
        original = LoopDriver.after_turn

        def breached(self, *args, **kwargs):
            extra = original(self, *args, **kwargs)
            extra['breaches'].append('fixture attribution breach')
            return extra

        monkeypatch.setattr(LoopDriver, 'after_turn', breached)
    config, result = crowded_experiment(tmp_path, router_factory=lambda *args: RefusingRouter())
    first = result.results[0]
    assert not first['scored_turn_reached'] and not first['precondition_unmet']
    assert first['loop']['retries'] == 2
    if breach:
        assert len(result.results) == 1 and not first['upstream_unavailable']
        assert first['harness_failure'] == 'fixture attribution breach'
    else:
        assert first['upstream_unavailable']['step'] == 1
        assert first['upstream_unavailable']['tool_calls_before'] == 0
        assert len(result.results) == 3 and result.failures == 0
        assert result.invocation_counts['loop-cell']['A1']['attempts_unmet'] == 0


def test_context_limit_keeps_consumer_classification(monkeypatch):
    config = consumer.TurnConfig('http://localhost', 'TEST_WO12_KEY', 'fake', {}, {}, None, None,
                                 'loop-scaffold@1', 'turn', '/unused')
    monkeypatch.setenv('TEST_WO12_KEY', 'fake-key')
    monkeypatch.setattr(consumer.time, 'sleep', lambda _: None)
    for status, requests in ((400, 1), (429, 3)):
        client = consumer.ChatClient(config)
        client.turn_tool_calls = 1
        limit = {'cause': 'context_length', 'detail': 'fixture context limit'}
        monkeypatch.setattr(client, '_context_limit', lambda *args, limit=limit: limit)

        def refuse(*args, status=status, **kwargs):
            raise urllib.error.HTTPError('http://localhost', status, 'context limit', {}, io.BytesIO(b'{}'))

        request = Mock(side_effect=refuse)
        monkeypatch.setattr(consumer.urllib.request, 'urlopen', request)
        with pytest.raises(consumer.LoopLimit) as exc:
            client.complete({})
        assert exc.value.record == limit
        assert request.call_count == requests
        assert client.retries == requests - 1
