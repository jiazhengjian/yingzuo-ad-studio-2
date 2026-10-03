"""Persist compiled bytes before a paid POST; test providers and media only."""
import asyncio
from copy import deepcopy
import json
import hashlib

import httpx
import pytest

from app.providers.gateway import Gateway, SubmissionError
from test_failed_video_retry import FailedVideoGateway, prepared, run
from test_pipeline import setup, fake_media
from test_grouped_generation import grouped_segment
from test_providers import settings, source, install_transport, prepared_original, public_dns


class FrozenGateway(FailedVideoGateway):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.compiles = []
        self.received = []

    def prepare_video_request(self, segment):
        self.compiles.append(segment['id'])
        return {"model": "FROZEN_TEST_MODEL", "duration": 10, "prompt": "FROZEN_TEST_" + segment['id'], "prompt_sha256": "TEST_HASH", "input_sha256": input_hash(segment), "skill_trace": [{"version": "TEST_1"}]}

    async def submit_video(self, segment, assets, idempotency_key, *, prepared_request):
        jid = idempotency_key.split(':', 1)[0]
        persisted = self.store.get_step(jid, 'video:' + segment['id'])['snapshot']['video_request']
        assert persisted == prepared_request
        assert prepared_request['input_sha256'] == input_hash(segment)
        self.received.append(deepcopy(prepared_request))
        return await super().submit_video(segment, assets, idempotency_key)


def input_hash(segment):
    return hashlib.sha256(json.dumps(segment, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def test_uncertain_acceptance_retains_exact_request_before_any_receipt(prepared, fake_media):
    store, _, plan, _, job = prepared
    gateway = FrozenGateway(store, plan)
    gateway.uncertain = True
    result = run(prepared, gateway)
    step = store.get_step(job['id'], 'video:s0')
    assert step['state'] == 'submitted_unknown'
    assert step['snapshot']['video_request'] == gateway.received[0]
    assert gateway.compiles == ['s0'] and len(gateway.submissions) == 1
    assert step['snapshot']['video_request']['skill_trace'][0]['version'] == 'TEST_1'
    assert result['status'] == 'submitted_unknown'


def test_explicit_failure_retry_reuses_frozen_prompt_after_skill_changes(prepared, fake_media, monkeypatch):
    store, project, plan, _, job = prepared
    gateway = FrozenGateway(store, plan)
    assert run(prepared, gateway)['status'] == 'failed'
    first = deepcopy(store.get_step(job['id'], 'video:s0')['snapshot']['video_request'])
    child = store.create_failed_video_retry(project['id'], job['id'])
    assert child['payload']['frozen_video_requests'] == {'s0': first}
    def changed_skill(segment):
        pytest.fail('A retry must not recompile with newly changed skills')
    monkeypatch.setattr(gateway, 'prepare_video_request', changed_skill)
    gateway.failing.clear()
    assert run(prepared, gateway, child)['status'] == 'ready'
    assert gateway.received[-1] == first and gateway.compiles == ['s0', 's1']
    snapshot = store.get_step(child['id'], 'video:s0')['snapshot']
    assert snapshot['video_model'] == first['model'] == 'FROZEN_TEST_MODEL'
    assert snapshot['provider_duration_seconds'] == first['duration']


def test_legacy_retry_keeps_original_input_contract_without_new_reference_fields(prepared, fake_media, monkeypatch):
    from app.db import encoded
    store, project, plan, _, job = prepared
    gateway = FrozenGateway(store, plan)
    assert run(prepared, gateway)['status'] == 'failed'
    snapshot = deepcopy(store.get_step(job['id'], 'video:s0')['snapshot'])
    snapshot['segment'].pop('reference_roles')
    snapshot['video_request']['input_sha256'] = input_hash(snapshot['segment'])
    with store._connection(True) as c:
        c.execute('UPDATE steps SET snapshot=? WHERE job_id=? AND step_key=?', (encoded(snapshot), job['id'], 'video:s0'))
    child = store.create_failed_video_retry(project['id'], job['id'])
    monkeypatch.setattr(gateway, 'prepare_video_request', lambda *_: pytest.fail('Legacy retry must not recompile'))
    gateway.failing.clear()
    assert run(prepared, gateway, child)['status'] == 'ready'
    retried = store.get_step(child['id'], 'video:s0')['snapshot']
    assert retried['segment'] == snapshot['segment'] and 'reference_roles' not in retried['segment']
    assert retried['video_request'] == snapshot['video_request']


def test_real_adapter_posts_exact_frozen_prompt_and_excludes_internal_trace(settings, source, install_transport, prepared_original, public_dns, monkeypatch):
    gateway = Gateway(settings)
    segment = grouped_segment(source['id'])
    prepared = gateway.prepare_video_request(segment)
    assert prepared['skill_trace'] and len(prepared['prompt_sha256']) == 64
    monkeypatch.setattr(gateway, 'prepare_video_request', lambda *_: pytest.fail('Frozen request must be reused'))
    scene = {'id': 'scene', 'kind': 'scene', 'metadata': {'provider_url': 'https://cdn.example/scene.png'}}
    calls = []
    def handle(request):
        body = json.loads(request.content); calls.append(body)
        assert body['prompt'] == prepared['prompt']
        assert not {'skill_trace', 'input_sha256', 'prompt_sha256'} & body.keys()
        return httpx.Response(200, json={'data': {'id': 'frozen-task'}})
    install_transport(handle)
    receipt = asyncio.run(gateway.submit_video(segment, [source, scene], 'offline', prepared_request=prepared))
    assert len(calls) == 1 and receipt['skill_trace'] == prepared['skill_trace']
    changed = deepcopy(segment); changed['shots'][0]['description'] += '修改'
    with pytest.raises(SubmissionError, match='冻结的视频请求'):
        asyncio.run(gateway.submit_video(changed, [source, scene], 'invalid', prepared_request=prepared))
    assert len(calls) == 1
