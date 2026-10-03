"""Initial authored copy and directing receive the same user-owned speech locks."""
import asyncio
from copy import deepcopy

import pytest

from app.progressive_planning import compile_director
from app.providers.gateway import Gateway, ProviderError
from app.strategy import StrategyRecord
from ad_script_fixtures import stage_outputs
from test_progressive_planning import (director_output, forbid_real_network,
                                       install_transport, progressive_gateway,
                                       settings, source)
from test_providers import chat_output, plan, request_context, strategy_stage


def test_preserving_current_copy_is_explicit_before_initial_authoring(progressive_gateway, source):
    state = progressive_gateway
    current = compile_director(director_output(state.strategy), state.strategy, state.duration,
                               "continuity_v1", [source["id"]])[0]
    state.current_plan = deepcopy(current)
    state.conversation = [{"role": "user", "content": "台词保持不变，继续安排画面。"}]
    delivered = state.run()
    expected = [beat["narration"] for beat in current["ad_script"]["beats"] if beat["narration"]]
    assert expected
    assert state.contexts[0]["fixed_speech"] == state.contexts[1]["fixed_speech"] == expected
    assert delivered["script"] == current["script"]
    assert state.current_plan == current
    assert state.calls == ["ad_script", "storyboard", "plan_review"]


def test_author_summary_and_assistant_proposals_never_become_fixed_copy(progressive_gateway):
    state = progressive_gateway
    strategy = state.strategy.model_dump()
    selected = next(direction for direction in strategy["directions"]
                    if direction["id"] == strategy["selected_direction_id"])
    author_text = "固定台词“这是作者的说明。”"
    selected["film_concept"]["sound_role"] = author_text
    selected["story_summary"] += author_text
    state.strategy = StrategyRecord.model_validate(strategy)
    state.conversation = [{"role": "assistant", "content": author_text},
                          {"role": "user", "content": "选择这个方向，开始安排。"}]
    delivered = state.run()
    assert state.contexts[0]["selected_direction"]["film_concept"]["sound_role"] == author_text
    assert state.contexts[0]["fixed_speech"] == state.contexts[1]["fixed_speech"] == []
    assert "这是作者的说明" not in delivered["script"]
    assert state.calls == ["ad_script", "storyboard", "plan_review"]


def test_early_lock_context_does_not_relax_final_fixed_speech_guard(progressive_gateway):
    state = progressive_gateway
    state.brief = "保留台词“看轮廓。”，继续安排。"
    state.first_director_mutation = lambda value: value["ad_script"]["beats"][0].update(narration="看外形。")
    with pytest.raises(ProviderError):
        state.run()
    assert state.contexts[0]["fixed_speech"] == state.contexts[1]["fixed_speech"] == ["看轮廓。"]
    assert state.calls == ["ad_script", "storyboard"]


def test_nonchoice_legacy_authoring_keeps_prior_input_contract(settings, source, plan,
                                                             strategy_stage, install_transport):
    outputs = stage_outputs(strategy_stage, plan)
    observed = []

    def handle(request):
        _, context = request_context(request)
        observed.append(context)
        return chat_output(outputs[context["stage"]])

    install_transport(handle)
    delivered = asyncio.run(Gateway(settings).plan({"brief": "蓝瓶商品"}, [source], []))
    content = next(context for context in observed if context["stage"] == "ad_script")
    assert "fixed_speech" not in content
    assert "stage_skills" not in content
    assert delivered["script"] == plan["script"]
