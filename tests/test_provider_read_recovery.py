"""Transient reads of accepted media tasks must never resubmit paid work."""
import asyncio

import httpx
import pytest

from app.providers.gateway import Gateway, ProviderError
from test_providers import settings, install_transport


@pytest.mark.parametrize("failure", ["timeout", "busy", "unavailable"])
def test_accepted_media_task_query_recovers_without_post(settings, install_transport, failure):
    settings.planner_retry_delay_seconds = 0
    calls = []
    def respond(request):
        calls.append((request.method, request.url.path))
        assert request.method == "GET"
        if len(calls) < 3:
            if failure == "timeout":
                raise httpx.ReadTimeout("PRIVATE_NETWORK_DETAIL", request=request)
            return httpx.Response(429 if failure == "busy" else 503, text="PRIVATE_RESPONSE")
        return httpx.Response(200, json={"data": {"id": "accepted_task", "status": "running"}})
    install_transport(respond)
    result = asyncio.run(Gateway(settings)._task("accepted_task"))
    assert result["id"] == "accepted_task"
    assert calls == [("GET", "/v1/tasks/accepted_task")] * 3


@pytest.mark.parametrize("status,attempts", [(503, 3), (401, 1)])
def test_media_query_retry_is_bounded_and_configuration_failure_not_repeated(settings, install_transport, status, attempts):
    settings.planner_retry_delay_seconds = 0
    calls = []
    def respond(request):
        calls.append(request.method)
        return httpx.Response(status, text="PRIVATE_RESPONSE")
    install_transport(respond)
    with pytest.raises(ProviderError) as error:
        asyncio.run(Gateway(settings)._task("accepted_task"))
    assert "PRIVATE" not in str(error.value)
    assert calls == ["GET"] * attempts
