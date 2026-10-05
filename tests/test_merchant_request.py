"""The frozen current turn reaches every planner stage alongside original intent."""
import asyncio
from copy import deepcopy

import httpx
import pytest

from app.providers.gateway import Gateway, ProviderError
from test_direction_selection import candidates_for, approved_direction_review, concept_pool_for
from test_progressive_planning import (progressive_gateway, chosen_strategy, director_output,
                                       accepted_review, reject_first_shot)
from test_providers import settings, source, install_transport, chat_output, request_context


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    async def denied(*args, **kwargs):
        pytest.fail("Current-turn tests must not contact a real supplier")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied)


@pytest.mark.parametrize("latest", ["调整开头并保留商品外观。", "选择创意方向：桌面一角"])
def test_selected_continuation_and_repairs_keep_current_turn_and_prior_requirements(progressive_gateway, latest):
    state = progressive_gateway
    state.brief = "你好"
    state.conversation = [{"role": "user", "content": "用原图中的瓶子做办公室广告，保持二十秒。"},
        {"role": "assistant", "content": "先前方案说明"}, {"role": "user", "content": latest},
        {"role": "assistant", "content": "已收到"}, {"role": "user", "content": " \n "}]
    before = deepcopy(state.conversation)
    state.first_review_mutation = reject_first_shot
    state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement", "plan_review"]
    for context in state.contexts:
        assert context["merchant_request"] == latest
        assert context["initial_brief"] == "你好"
        assert context["conversation"] == [message for message in before if message["role"] == "user"]
    assert state.conversation == before


@pytest.mark.parametrize("conversation", [[], [{"role": "assistant", "content": "已收到"}],
    [{"role": "user", "content": " \n "}]])
def test_no_nonempty_user_message_falls_back_to_initial_brief(progressive_gateway, conversation):
    state = progressive_gateway
    state.brief = "原图商品的办公室广告"
    state.conversation = conversation
    state.run()
    assert all(context["merchant_request"] == state.brief == context["initial_brief"] for context in state.contexts)


@pytest.mark.parametrize("unlock", [False, True])
def test_selection_preserves_initial_fixed_copy_and_later_explicit_unlock(progressive_gateway, unlock):
    state = progressive_gateway
    state.brief = "固定台词“看轮廓。”"
    state.conversation = ([{"role": "user", "content": "台词可以改。"}] if unlock else []) + [
        {"role": "user", "content": "选择创意方向：桌面一角"}]
    state.first_director_mutation = lambda value: value["ad_script"]["beats"][0].update(narration="看外形。")
    if unlock:
        state.run()
    else:
        with pytest.raises(ProviderError):
            state.run()
    assert state.contexts[1]["fixed_speech"] == ([] if unlock else ["看轮廓。"])
    assert state.contexts[1]["merchant_request"] == "选择创意方向：桌面一角"
    assert state.contexts[1]["initial_brief"] == state.brief


@pytest.mark.parametrize("new_directions", [False, True])
def test_routed_edit_and_new_direction_review_receive_actual_latest_request(settings, source, install_transport, tmp_path, new_directions):
    settings.data_root = tmp_path / "data"
    settings.direction_selection_enabled = settings.progressive_planning_enabled = True
    settings.planning_generation_strategy = "continuity_v1"
    strategy = chosen_strategy(source)
    selected = next(d for d in strategy.directions if d.id == strategy.selected_direction_id)
    latest = "重新给我三个创意方向" if new_directions else "  沿用选中的方向，开场直接呈现商品。\n保持二十秒。  "
    conversation = [{"id": "first", "role": "user", "content": "商品用原图，场景是办公室"},
                    {"id": "last", "role": "user", "content": latest}]
    calls = []

    def handler(request):
        _, context = request_context(request)
        calls.append(context["stage"])
        assert context["merchant_request"] == latest  # Preserve exact text, including newlines.
        assert context["initial_brief"] == "你好"
        assert context["conversation"] == [{k: message[k] for k in ("role", "content")} for message in conversation]
        if context["stage"] == "strategy":
            assert context["brief"] == "你好"
            if new_directions:
                candidates = candidates_for((None, None, None, source))
                return chat_output({"kind": "directions", "message": "三个方向", "brief": candidates.brief.model_dump(),
                                    "directions": [d.model_dump() for d in candidates.directions],
                                    "concept_pool": concept_pool_for(candidates.directions)})
            return chat_output({"kind": "plan", "message": "修改方向", "brief": strategy.brief.model_dump(),
                "directions": [selected.model_dump()], "selected_direction_id": selected.id})
        if context["stage"] == "direction_review":
            return chat_output(approved_direction_review(context["directions"]))
        if context["stage"] == "ad_script":
            return chat_output(director_output(strategy)["ad_script"])
        if context["stage"] == "storyboard":
            return chat_output(director_output(strategy))
        assert context["stage"] == "plan_review"
        return chat_output(accepted_review(context["final_plan"]))

    install_transport(handler)
    result = asyncio.run(Gateway(settings).decide({"brief": "你好", "duration_seconds": 20}, [source],
        conversation, [], None if new_directions else {"strategy": strategy.model_dump()}))
    assert result["kind"] == ("directions" if new_directions else "plan")
    assert calls == (["strategy", "direction_review"] if new_directions else
                     ["strategy", "ad_script", "storyboard", "plan_review"])
