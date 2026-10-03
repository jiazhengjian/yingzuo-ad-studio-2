"""Failure attribution through actual transport/worker boundaries; offline only."""
import asyncio
import json
import logging
from copy import deepcopy

import httpx
import pytest

from app.planner_runtime import safe_planner_transport_failure
from app.planning_session import PlanningSession, PlanningSessionError, allow_manual_planning_retry
from app.providers.gateway import Gateway, PlannerRequestError, PlannerConfigurationError
from test_planning_recovery import run_case, offline, env, isolated_skill_loader


def test_transport_diagnostics_are_allowlisted_without_error_text_or_provider_body():
    assert safe_planner_transport_failure('plan_review', 1.25, status_code=504) == {
        'stage': 'plan_review', 'elapsed_ms': 1250, 'status_code': 504}
    assert safe_planner_transport_failure('PRIVATE_STAGE', float('nan'),
        status_code=True, error_type='PRIVATE_ERROR_URL_TOKEN') == {
        'stage': 'unknown', 'elapsed_ms': None, 'error_type': 'TransportError'}


@pytest.mark.parametrize('failure,expected,exception', [
    ('http504', {'status_code': 504}, PlannerRequestError),
    ('http401', {'status_code': 401}, PlannerConfigurationError),
    ('read_timeout', {'error_type': 'ReadTimeout'}, PlannerRequestError),
    ('read_error', {'error_type': 'ReadError'}, PlannerRequestError),
])
def test_actual_transport_records_status_or_type_without_sensitive_details(env, monkeypatch, caplog, failure, expected, exception):
    def respond(request):
        if failure.startswith('http'):
            return httpx.Response(int(failure[4:]), text='PRIVATE_PROVIDER_BODY', headers={'x-private': 'PRIVATE_HEADER'})
        error = httpx.ReadTimeout if failure == 'read_timeout' else httpx.ReadError
        raise error('PRIVATE_EXCEPTION_WITH_URL_AND_TOKEN', request=request)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    with caplog.at_level(logging.WARNING, logger='app.providers.gateway'):
        with pytest.raises(exception) as failure_info:
            asyncio.run(Gateway(env[1].state.settings)._planner_json('PRIVATE_PROMPT', {'stage': 'plan_review'}, []))
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith('Planner transport failure: ')]
    assert len(lines) == 1
    diagnostic = json.loads(lines[0].split(': ', 1)[1])
    assert diagnostic.pop('stage') == 'plan_review'
    assert diagnostic.pop('elapsed_ms') >= 0
    assert diagnostic == expected
    assert 'PRIVATE' not in caplog.text and 'PRIVATE' not in str(failure_info.value)


def test_incomplete_review_is_not_reported_as_content_rejection_or_replayed(run_case, monkeypatch):
    client, store, pid, job, calls, _, control, execute = run_case
    original = Gateway._planner_json
    async def malformed(self, system, context, sources, max_tokens=7000):
        result = await original(self, system, context, sources, max_tokens=max_tokens)
        if context['stage'] == 'plan_review':
            result.pop('checks')
        return result
    monkeypatch.setattr(Gateway, '_planner_json', malformed)
    before = store.project_detail(pid)
    result = execute()
    assert result['status'] == 'failed'
    assert result['progress']['error_code'] == 'planning_review_incomplete'
    assert result['progress']['retry_action'] == 'continue_conversation'
    assert result['progress']['retryable'] is False
    assert calls == ['ad_script', 'storyboard', 'plan_review', 'plan_review']
    assert 'plan_refinement' not in calls
    frozen = deepcopy(result['payload']['planning_checkpoint'])
    response = client.post(f'/api/projects/{pid}/retry', json={'job_id': job['id']})
    assert response.status_code == 409
    assert '修改要求' not in response.text
    assert store.get_job(job['id'])['payload']['planning_checkpoint'] == frozen
    after = store.project_detail(pid)
    assert after['project']['plan'] == before['project']['plan']
    assert after['assets'] == before['assets'] and after['renders'] == before['renders']
    assert result['submission_count'] == 0


def test_total_request_limit_is_durable_and_does_not_imply_a_content_failure():
    saved = []
    session = PlanningSession(None, saved.append, {'input': 'same'}, max_requests=1, retry_delay=0)
    async def work():
        return {'complete': True}
    assert asyncio.run(session.call('ad_script', {'a': 1}, work)) == {'complete': True}
    with pytest.raises(PlanningSessionError) as info:
        asyncio.run(session.call('storyboard', {'b': 2}, work))
    assert info.value.error_code == 'planning_request_limit'
    assert not info.value.retryable and info.value.retry_action == 'continue_conversation'
    assert session.state['request_count'] == 1
    restored = PlanningSession(saved[-1], saved.append, {'input': 'same'}, max_requests=1, retry_delay=0)
    assert asyncio.run(restored.call('ad_script', {'a': 1}, work)) == {'complete': True}
    with pytest.raises(PlanningSessionError) as repeated:
        asyncio.run(restored.call('storyboard', {'b': 2}, work))
    assert repeated.value.error_code == 'planning_request_limit'
    assert restored.state['request_count'] == 1
    with pytest.raises(ValueError):
        allow_manual_planning_retry(saved[-1])
