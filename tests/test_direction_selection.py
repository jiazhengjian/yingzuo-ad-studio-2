"""Three-direction choice with real persistence and planner transport, offline only."""
import asyncio
from copy import deepcopy
import json
import logging

import httpx
import pytest
from pydantic import ValidationError

from app import runtime_skills
from app import strategy as strategy_module
from app.db import Store
from app.pipeline import Pipeline
from app.planning import StrategyDecision, evidence_sources, keeps_selected_direction, requests_new_directions, strategy_decision_schema
from app.providers.gateway import Gateway, ProviderError
from app.schemas import AgentDecision
from app.strategy import CreativeDirection, DirectionCandidates, ProductBrief, StrategyRecord, build_direction_candidates, select_rules
from test_conversation_api import env, post_turn
from test_continuity_planning import compiled_from_reservations as legacy_compiled_from_reservations
from test_continuity_planning import directed_review as legacy_directed_review
from test_providers import chat_output, request_context
from ad_script_fixtures import CRITERIA, with_action_timing, execution_check


@pytest.fixture(autouse=True)
def forbid_unmocked_network(monkeypatch):
    async def denied(*args, **kwargs):
        pytest.fail("Direction tests must never call a real supplier")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied)


@pytest.fixture
def isolated_skill_loader(monkeypatch):
    # Explicit transport fixture, not a runtime fallback. Runtime loader bytes
    # and registry bindings are independently verified in test_runtime_skills.
    def load(stage, mode=None, **kwargs):
        skill = {"id": "ads-video-" + stage.replace("_", "-"), "display_name": "Ads Video " + stage,
            "version": "test-1", "mode": "planner", "content": "EXPLICIT OFFLINE SKILL",
            "resources": [{"path": "SKILL.md", "sha256": "b" * 64}], "content_sha256": "a" * 64}
        return {"schema_version": 1, "stage": stage, "skills": [skill], "content_sha256": "c" * 64}
    monkeypatch.setattr(runtime_skills, "stage_skill_context", load)


def candidates_for(env):
    asset = env[3]
    brief = ProductBrief(product_name="瓶子", category="general", audience="关注外观的顾客", scenario="办公室",
        ad_form=env[1].state.store.get_project(env[2]).get("ad_form") if env[1] is not None else None,
        creative_intent={"use_context": "办公桌用品的选择", "setting_requirement": "",
            "reference_intent": "", "structure_preference": "auto"},
        facts=[{"id": "appearance", "text": "瓶身轮廓", "source_kind": "image", "source_id": asset["id"]}])
    directions = [CreativeDirection(id=f"direction_{i}", rule_id=rule, label=label,
        audience=brief.audience, scenario=brief.scenario, angle=angle, hook=hook,
        story_summary=[
            "一道窗光落在瓶身轮廓上，光线移开后逐渐看清整只瓶子，最后留下完整外观。",
            "忙碌的人整理办公桌，在桌角留出一处位置，摆上自己喜欢的瓶子，再回到工作中。",
            "同一只瓶子留在窗边，早晚不同角度的光依次经过它，最后回到自然光下的完整商品。",
        ][i],
        attraction_mechanism=[
            "用局部轮廓引出它是什么的好奇，再以完整外观揭示答案。",
            "从熟悉的整理桌面动作建立代入，让观众期待桌面最后的变化。",
            "让观众等待光线带来的视觉变化，记住同一商品的轮廓。",
        ][i],
        audience_value=["选择桌面用品时，希望先看清瓶子的实际轮廓。",
            "布置办公桌时，希望判断这只瓶子的外观是否适合自己的桌面。",
            "在意装饰品外观的顾客，希望从不同光线中认清同一瓶子的形态。",
        ][i],
        product_expression=["从局部轮廓过渡到全貌，揭示完整瓶形，不演示未知功能。",
            "通过桌面搭配前后的构图变化，让瓶形成为办公桌布置选择的一部分。",
            "保持瓶子不变，依次展示早晚光线下的外形与自然光全貌。",
        ][i],
        film_concept={
            "structure": ("scene_action" if i == 1 else "world_intercut") if brief.ad_form == "scene_story"
            else ("world_intercut" if i == 1 else "product_visual") if brief.ad_form == "brand_tvc"
            else ("lifestyle" if i == 1 else "product_visual"),
            "visual_skeleton": ("world_intercut" if i == 2 else "lifestyle") if brief.ad_form == "scene_story"
            else ("world_intercut" if i == 1 else "product_visual") if brief.ad_form == "brand_tvc"
            else ("lifestyle" if i == 1 else "product_visual"),
            "visual_motif": ["窗光沿瓶身", "空出的桌角", "早晚同一束光"][i],
            "side_proof": ["光从一条轮廓走到整只瓶子。", "整理出来的空位被这只瓶子占住。", "光线换过，还是同一只瓶子。"][i],
            "key_visual": ["窗光勾勒同一瓶身，从线条逐渐看清轮廓。", "整理后的桌角由瓶身轮廓占据。", "同一瓶身上的光线由早到晚流动。"][i],
            "memory_anchor": ["完整瓶形接住开场的轮廓线。", "桌角留下这一只瓶子。", "光线改变，瓶形始终一致。"][i],
            "sound_role": "画外音补充桌面用品的选择语境，不复述画面动作。"},
        development=development, payoff=payoff, evidence_ids=["appearance"],
        reason=reason, production_notes=["只使用原图可见外观，不承诺功能"],
        production={"product_constraints": [{"text": "保持同一瓶身轮廓，不替换商品", "evidence_ids": ["appearance"]}],
            "characters": ([{"id": "char_worker", "name": "办公者", "description": "创作设定：短发成年办公者，穿浅色长袖衬衫"}] if i == 1 else []),
            "speech_mode": "voiceover", "speaker_id": "", "voice_description": "自然普通话画外音，向观众介绍桌面搭配，平缓不播音腔"})
        for i, (rule, label, angle, hook, development, payoff, reason) in enumerate([
            ("appearance_reveal", "轮廓之美", "先从轮廓认出这只瓶子", "一道轮廓先出现", "看到瓶身线条", "整体落定", "外观可见"),
            ("everyday_context", "桌面一角", "桌角还空着，这只瓶子刚好放下", "手整理办公桌", "将商品摆在留出的桌面空间", "桌面搭配完成", "场景符合用户要求"),
            ("appearance_reveal", "光线里的变化", "同一只瓶子，早晚光线不一样", "斜射光照到商品", "主体不动，光线经过瓶身", "保留自然光中的商品", "不依赖未知功能"),
        ])]
    hooks = {"scene_story": ("awkward_moment", "situational_conflict", "arresting_line"),
             "presenter_pitch": ("direct_question", "counterintuitive", "result_first")}.get(
        brief.ad_form, ("macro_detail", "scale_shift", "silhouette"))
    insights = (
        "还没看清整只瓶子，就想知道它长什么样。",
        "桌面已经很满，还想给这只瓶子留一个位置。",
        "光线一变，就怕认不出还是不是同一只瓶子。",
    )
    ideas = ("先看见一条轮廓", "桌角终于有了它", "光线换了瓶子还在")
    worlds = ("functional", "identity", "sensory")
    for index, direction in enumerate(directions):
        direction.consumer_insight = insights[index]
        direction.big_idea = ideas[index]
        direction.hook_type = hooks[index]
        direction.brand_world = worlds[index]
    if brief.ad_form == "presenter_pitch":
        # These type-switch tests use a synthetic presenter response rather
        # than relabeling the voiceover fixture as a successful presenter ad.
        for direction in directions:
            direction.production = type(direction.production).model_validate({**direction.production.model_dump(),
                "characters": [{"id": "char_presenter", "name": "讲解者",
                    "description": "创作设定：成年桌面用品讲解者"}],
                "speech_mode": "character_dialogue", "speaker_id": "char_presenter"})
            direction.film_concept = direction.film_concept.model_copy(update={
                "structure": "presenter_explanation", "visual_skeleton": "lifestyle"})
            direction.product_expression = "讲解者面向观众介绍瓶形，并拿起实物说明桌面选择。"
            direction.story_summary = "讲解者向观众提出桌面布置的选择，拿起瓶子介绍外形，再回到完整商品。"
    return build_direction_candidates(brief, directions, "direction_0", select_rules(None))


def approved_direction_review(directions):
    return {"directions": deepcopy(directions), "ready": True, "distinct_directions_passed": True,
        "summary": "离线审核夹具：三种观看机制不同，动作后状态和商品作用可执行。",
        "checks": [{"direction_id": direction["id"], "physical_plausibility_passed": True,
            "product_role_passed": True, "attraction_passed": True,
            "claim_support_passed": True, "expression_form_passed": True, "film_concept_passed": True,
            "creative_tension": "medium", "hook_strength": "medium",
            "note": "离线审核夹具：保留同一商品，完成展示或摆放后状态一致，开场后面有展开。"}
            for direction in directions]}


def concept_pool_for(directions):
    """Six short concepts whose greedy pick is the three supplied directions."""
    items = []
    for direction in directions:
        data = direction if isinstance(direction, dict) else direction.model_dump()
        items.append({"id": data["id"], "rule_id": data["rule_id"], "hook_type": data["hook_type"],
            "brand_world": data["brand_world"], "evidence_ids": list(data["evidence_ids"])})
    for index in range(3):
        items.append({**items[0], "id": f"spare_{index}"})
    return items


def compiled_from_reservations(context):
    """Match only new request fields, leaving historical fixtures untouched."""
    result = legacy_compiled_from_reservations(context)
    required = context["schema"]["$defs"]["Shot"]["required"]
    if "beat_ids" in required:
        people = [person["id"] for person in (context.get("production") or {}).get("characters", [])]
        for segment in result["segments"]:
            for shot in segment["shots"]:
                shot["beat_ids"] = [shot["beat_id"]]
                shot["character_ids"] = people[:]
    return result


def directed_review(compiled):
    result = legacy_directed_review(compiled)
    by_id = {(segment["id"], shot["id"]): shot for segment in compiled["segments"] for shot in segment["shots"]}
    for check in result["shots"]:
        shot = by_id[(check["segment_id"], check["shot_id"])]
        if "beat_ids" in shot:
            check["pacing"] = {"action_seconds_required": min(1, shot["end"] - shot["start"]),
                "action_timing_passed": True, "cut_necessity_passed": True,
                "note": "离线审核夹具：静置商品有辨认时间，连续观察不计为实际切镜。"}
    return result


def progressive_review_alignment(compiled):
    """Fresh plan-review output for these voiceover / historical fixtures only."""
    assert (compiled.get("production") or {}).get("speech_mode") in (None, "voiceover")
    result = directed_review(compiled)
    for span in result["spans"]:
        span["speaker_visibility"] = "not_applicable"
    actual_shots = {(segment["id"], shot["id"]): shot
                    for segment in compiled["segments"] for shot in segment["shots"]}
    for shot in result["shots"]:
        shot["temporal_omission"] = "no_omission"
        actual = actual_shots[(shot["segment_id"], shot["shot_id"])]
        shot["visual_execution"] = {"verdict": "clear", "source": "description",
            "quote": actual["description"][:240],
            "explanation": "离线审核夹具：当前镜头的实际画面与摄影变化相容。"}
    return result


def offer(env):
    response = post_turn(env, content="帮我策划三个方向")
    assert response.status_code == 200
    store = env[1].state.store
    job = response.json()["job"]
    assert store.claim_job(job["id"])
    candidates = candidates_for(env)
    decision = {"kind": "directions", "message": "请选择一个方向", "direction_candidates": candidates.model_dump()}
    store.finish_decision(job["id"], decision)
    return store.pending_gate(env[2], "direction"), job


def select_body(gate, direction_id="direction_1", request_id="select-1"):
    return {"client_turn_id": request_id, "gate_id": gate["id"], "direction_id": direction_id,
        "expected_context_version": gate["context_version"], "accepted_revision": gate["revision"]}


def select(env, gate, **kwargs):
    return env[0].post(f"/api/projects/{env[2]}/direction-select", json=select_body(gate, **kwargs))


def test_offer_is_durable_has_no_plan_or_media_and_refreshes(env):
    gate, job = offer(env)
    snapshot = env[0].get(f"/api/projects/{env[2]}").json()
    assert snapshot["project"]["status"] == "awaiting_direction"
    assert snapshot["project"]["revision"] == 0 and snapshot["project"]["plan"] is None
    assert gate["kind"] == "direction" and len(gate["options"]) == 3
    assert gate["questions"] == [] and gate["answers"] == [] and gate["selected_option_id"] is None
    assert [o["id"] for o in gate["options"] if o["recommended"]] == ["direction_0"]
    assert all("segment" not in option for option in gate["options"])
    assert [asset["kind"] for asset in snapshot["assets"]] == ["product"]
    restored = Store(env[1].state.store.db_path, env[1].state.store.media_root)
    assert restored.pending_gate(env[2]) == gate and restored.recover_jobs() == []
    assert restored.get_job(job["id"])["phase"] == "awaiting_direction"


def test_select_is_atomic_idempotent_and_freezes_only_one_choice(env):
    gate, source = offer(env)
    first = select(env, gate)
    assert first.status_code == 200
    job = first.json()["job"]
    assert first.json()["snapshot"]["project"]["context_version"] == gate["context_version"] + 1
    assert select(env, gate).json()["job"]["id"] == job["id"]
    assert select(env, gate, request_id="another-click").json()["job"]["id"] == job["id"]
    assert select(env, gate, direction_id="direction_2").status_code == 409
    assert select(env, gate, direction_id="direction_2", request_id="other-choice").status_code == 409
    store = env[1].state.store
    payload = store.get_job(job["id"])["payload"]
    assert payload["selected_strategy"]["selected_direction_id"] == "direction_1"
    assert payload["direction_gate_id"] == gate["id"] and "plan" not in payload
    assert store.get_job(source["id"])["phase"] == "direction_selected"
    assert len(store.project_detail(env[2])["jobs"]) == 2
    sources = evidence_sources(payload["frozen"]["project"], payload["frozen"]["messages"], payload["frozen"]["gates"])
    assert all("选择创意方向：" not in value for value in sources.values())


@pytest.mark.parametrize("field,value", [("accepted_revision", 99), ("expected_context_version", 99), ("gate_id", "unknown"), ("direction_id", "unknown")])
def test_invalid_or_stale_choice_does_not_mutate(env, field, value):
    gate, _ = offer(env)
    before = env[1].state.store.project_detail(env[2])
    body = {**select_body(gate), field: value}
    response = env[0].post(f"/api/projects/{env[2]}/direction-select", json=body)
    assert response.status_code in (409, 422)
    assert env[1].state.store.project_detail(env[2]) == before


def test_product_image_and_info_keep_the_pending_direction(env):
    """A later product photo or product note stays on the offer the merchant is already choosing."""
    import io
    from PIL import Image
    gate, job = offer(env)
    client, app, pid, _ = env
    store = app.state.store
    option_ids = [option["id"] for option in gate["options"]]
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buffer, "PNG")
    uploaded = client.post(f"/api/projects/{pid}/assets", files={"file": ("草莓蛋糕.png", buffer.getvalue(), "image/png")})
    assert uploaded.status_code == 201
    kept = store.pending_gate(pid, "direction")
    assert kept["id"] == gate["id"] and kept["status"] == "pending"
    assert [option["id"] for option in kept["options"]] == option_ids
    assert kept["context_version"] == store.get_project(pid)["context_version"]
    assert store.get_job(job["id"])["status"] == "awaiting_input"
    assert store.pending_gate(pid, "clarification") is None
    noted = post_turn(env, client_turn_id="product-note", content="这是草莓蛋糕，表面有奶油和水果。")
    assert noted.status_code == 200
    assert noted.json()["job"]["id"] == job["id"]
    assert noted.json()["job"]["status"] == "awaiting_input"
    kept = store.pending_gate(pid, "direction")
    assert kept["id"] == gate["id"] and [option["id"] for option in kept["options"]] == option_ids
    assert kept["context_version"] == store.get_project(pid)["context_version"]
    assert store.pending_gate(pid, "clarification") is None
    assert not any(item["status"] == "queued" for item in store.project_detail(pid)["jobs"])
    assert any(message["role"] == "user" and message["content"] == "这是草莓蛋糕，表面有奶油和水果。" for message in store.project_detail(pid)["messages"])
    assert select(env, kept).status_code == 200


def test_free_input_supersedes_gate_and_question_reply_preserves_offer(env):
    gate, _ = offer(env)
    response = post_turn(env, client_turn_id="question", content="第一个方向是什么意思？")
    assert response.status_code == 200 and select(env, gate).status_code == 409
    store = env[1].state.store
    job = response.json()["job"]
    store.claim_job(job["id"])
    store.finish_decision(job["id"], {"kind": "reply", "message": "第一个主要关注商品的轮廓。"})
    replacement = store.pending_gate(env[2], "direction")
    assert replacement["id"] != gate["id"] and replacement["options"] == gate["options"]
    assert replacement["context_version"] == store.get_project(env[2])["context_version"]


def test_cannot_mix_direction_gate_with_image_confirmation(env):
    gate, _ = offer(env)
    response = env[0].post(f"/api/projects/{env[2]}/creative-confirm", json={"client_turn_id": "wrong-kind",
        "gate_id": gate["id"], "accepted_revision": 1, "option_id": "direction_0"})
    assert response.status_code == 409
    assert env[1].state.store.pending_gate(env[2], "direction")["id"] == gate["id"]


def test_direction_contract_rejects_auto_selection_and_cosmetic_duplicates(env):
    candidate = candidates_for(env)
    base = {"kind": "directions", "message": "选方向", "brief": candidate.brief.model_dump(),
        "directions": [d.model_dump() for d in candidate.directions],
        "concept_pool": concept_pool_for(candidate.directions), "selected_direction_id": None}
    StrategyDecision.model_validate(base)
    for bad in ({**base, "selected_direction_id": "direction_0"}, {**base, "directions": base["directions"][:2]}):
        with pytest.raises(ValidationError):
            StrategyDecision.model_validate(bad)
    duplicate = deepcopy(candidate.model_dump())
    duplicate["directions"][2]["development"] = duplicate["directions"][0]["development"]
    duplicate["directions"][2]["payoff"] = duplicate["directions"][0]["payoff"]
    with pytest.raises(ValidationError):
        AgentDecision.model_validate({"kind": "directions", "message": "选方向", "direction_candidates": duplicate})


@pytest.mark.parametrize("field", ["story_summary", "attraction_mechanism"])
@pytest.mark.parametrize("value", [None, "", " \n\t "])
def test_new_direction_response_accepts_a_pitch_without_the_old_essays(env, field, value):
    candidate = candidates_for(env)
    payload = {"kind": "directions", "message": "请选择方向", "brief": candidate.brief.model_dump(),
        "directions": [d.model_dump() for d in candidate.directions],
        "concept_pool": concept_pool_for(candidate.directions)}
    if value is None:
        payload["directions"][1].pop(field, None)
    else:
        payload["directions"][1][field] = value
    decision = StrategyDecision.model_validate(payload)
    assert not getattr(decision.directions[1], field)


@pytest.mark.parametrize("omit_brief", [False, True])
def test_new_direction_response_cannot_inherit_or_omit_brief(env, omit_brief):
    candidate = candidates_for(env)
    response = {"kind": "directions", "message": "三个方向", "brief": None,
        "directions": [d.model_dump() for d in candidate.directions],
        "concept_pool": concept_pool_for(candidate.directions)}
    if omit_brief:
        response.pop("brief")
    with pytest.raises(ValidationError):
        StrategyDecision.model_validate(response)
    assert StrategyDecision.model_validate({"kind": "reply", "message": "普通答复", "brief": None}).brief is None


def test_new_request_schema_drops_essays_and_revisions_keep_saved_stories():
    fresh = strategy_decision_schema(new_directions=True)["$defs"]["CreativeDirection"]
    updating = strategy_decision_schema(retain_direction_story=True)["$defs"]["CreativeDirection"]
    historical = strategy_decision_schema()["$defs"]["CreativeDirection"]
    for field in ("story_summary", "attraction_mechanism"):
        assert field not in fresh["properties"]
        assert field in updating["required"]
        assert updating["properties"][field]["minLength"] == 1
        assert field not in historical["required"]
        assert historical["properties"][field]["default"] == ""
    assert "audience_value" not in fresh["properties"]
    assert "big_idea" not in fresh["properties"]
    assert "product_expression" in fresh["required"]
    assert fresh["properties"]["product_expression"]["maxLength"] == 60
    assert "audience_value" not in updating["required"]
    assert "product_expression" not in updating["required"]
    assert fresh["properties"]["hook"]["maxLength"] == 40
    assert fresh["properties"]["reason"]["maxLength"] == 40


def historical_candidates(env):
    payload = candidates_for(env).model_dump()
    payload["brief"].pop("creative_intent")
    for direction in payload["directions"]:
        direction.pop("film_concept")
        direction.pop("story_summary")
        direction.pop("attraction_mechanism")
        direction.pop("audience_value")
        direction.pop("product_expression")
        direction.pop("production")
    return payload


def test_historical_pending_direction_survives_question_and_can_be_selected(env, monkeypatch):
    old_catalog = deepcopy(strategy_module._catalog())
    for rule in old_catalog["rules"]:
        rule["version"] = "1.0.0"
    store = env[1].state.store
    # Persist under the old catalog, then restore under the real newer catalog.
    # Removing story fields alone would not exercise provenance compatibility.
    with monkeypatch.context() as previous_release:
        previous_release.setattr(strategy_module, "_catalog", lambda: deepcopy(old_catalog))
        payload = historical_candidates(env)
        candidate = DirectionCandidates.model_validate(payload)
        assert candidate.model_dump() == payload
        StrategyRecord.model_validate(candidate.select("direction_1").model_dump())
        initial = post_turn(env, content="历史任务").json()["job"]
        assert store.claim_job(initial["id"])
        store.finish_decision(initial["id"], {"kind": "directions", "message": "历史方向", "direction_candidates": payload})
    assert {r.version for r in candidate.rules_used} == {"1.0.0"}
    assert {r.version for r in candidates_for(env).rules_used} == {"1.2.0"}
    original = store.pending_gate(env[2], "direction")
    question = post_turn(env, client_turn_id="legacy-question", content="第二个是什么意思？").json()["job"]
    assert store.claim_job(question["id"])
    store.finish_decision(question["id"], {"kind": "reply", "message": "解释现有方向，不重新选择。"})
    restored = store.pending_gate(env[2], "direction")
    assert restored["options"] == original["options"]
    response = select(env, restored)
    assert response.status_code == 200
    strategy = store.get_job(response.json()["job"]["id"])["payload"]["selected_strategy"]
    assert strategy["selected_direction_id"] == "direction_1"
    assert {r["version"] for r in strategy["rules_used"]} == {"1.0.0"}
    assert "story_summary" not in strategy["directions"][1]


def test_new_offer_still_rejects_stale_rule_provenance_even_with_frozen_pending_offer(env):
    offer(env)
    job = post_turn(env, client_turn_id="new-offer", content="重新给我方向").json()["job"]
    store = env[1].state.store
    assert store.claim_job(job["id"])
    assert store.get_job(job["id"])["payload"]["pending_direction_candidates"]
    stale = candidates_for(env).model_dump()
    for rule in stale["rules_used"]:
        rule["version"] = "1.0.0"
    with pytest.raises(ValueError, match="方向候选的规则来源不正确"):
        store.finish_decision(job["id"], {"kind": "directions", "message": "新的候选",
            "direction_candidates": stale})
    assert store.get_job(job["id"])["status"] == "running"


@pytest.mark.parametrize("message,expected", [("换个思路，再给我三个方向", True), ("我想重新选创意方向", True),
    ("把台词改短一点", False), ("改成30秒", False), ("场景换成客厅", False),
    ("不要换方向，只改台词", False), ("按新版流程重新策划", False)])
def test_direction_reselection_requires_explicit_request(message, expected):
    assert requests_new_directions([{"role": "user", "content": message}]) is expected


@pytest.mark.parametrize("real_skills,historical_choice", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("progressive", [False, True])
def test_real_gateway_pauses_then_uses_only_selected_direction(env, monkeypatch, request, real_skills, historical_choice, progressive):
    if not real_skills:
        request.getfixturevalue("isolated_skill_loader")
    _, app, pid, asset = env
    settings = app.state.settings
    settings.direction_selection_enabled = True
    settings.progressive_planning_enabled = progressive
    settings.planning_generation_strategy = "continuity_v1"
    candidates = candidates_for(env)
    if historical_choice:
        candidates = DirectionCandidates.model_validate(historical_candidates(env))
    selected_direction = candidates.directions[1].model_dump()
    selected_story = ({key: value for key, value in selected_direction.items()
                       if key not in ("hook", "development", "payoff", "production_notes", "reason")}
                      if selected_direction.get("story_summary") and selected_direction.get("attraction_mechanism")
                      else selected_direction)
    strategy_response = {"kind": "directions", "message": "选一个方向", "brief": candidates.brief.model_dump(),
        "directions": [d.model_dump() for d in candidates.directions],
        "concept_pool": concept_pool_for(candidates.directions), "selected_direction_id": None,
        "recommended_direction_id": candidates.recommended_direction_id}
    calls, compiled = [], None
    real_client = httpx.AsyncClient

    def handle(request):
        nonlocal compiled
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        payload, context = request_context(request)
        stage = context["stage"]
        calls.append(stage)
        assert context["stage_skills"]["skills"]
        if stage == "strategy":
            assert context["orchestrator_skills"]["stage"] == "orchestrator"
            assert context["allowed_decisions"] == ["reply", "clarify", "directions"]
            required = context["schema"]["$defs"]["CreativeDirection"]["required"]
            assert {"product_expression", "consumer_insight", "hook_type", "brand_world", "film_concept", "hook", "payoff", "reason"} <= set(required)
            for dropped in ("story_summary", "attraction_mechanism", "audience_value", "big_idea", "angle", "development"):
                assert dropped not in required
                assert dropped not in context["schema"]["$defs"]["CreativeDirection"]["properties"]
            assert context["schema"]["$defs"]["CreativeDirection"]["properties"]["product_expression"]["maxLength"] == 60
            return chat_output(strategy_response)
        if stage == "direction_review":
            assert context["immutable_brief"] == candidates.brief.model_dump()
            assert context["reference_capacity"]["product_reference_count"] == 1
            return chat_output(approved_direction_review(context["directions"]))
        if stage == "ad_script":
            assert "evidence_ids" not in context["schema"]["properties"]
            assert context["selected_direction"] == selected_story
            assert "selected_direction" not in context["creative_strategy"]
            assert "directions" not in context["creative_strategy"]
            assert "content" not in context["runtime_skill"]
            script = {"core_message": "办公桌上留一个好看的位置", "audience_takeaway": "关注桌面与商品的外观搭配",
                "product_role": "主体是商品", "skill_version": context["runtime_skill"]["version"],
                "beats": [{"id": "b1", "purpose": "建立办公场景与商品选择", "message": "桌面搭配",
                    "visual_role": "办公桌上摆放同一瓶子", "narration": "给桌面，留一点自己喜欢的样子。", "evidence_ids": ["appearance"]}]}
            return chat_output(with_action_timing(script))
        if stage == "script_review":
            assert not progressive
            assert "evidence_ids" not in context["schema"]["$defs"]["AdScript"]["properties"]
            assert "evidence_ids" not in context["draft_script"]
            assert context["selected_direction"] == selected_direction
            # The wire schema does not request this summary. Even a stale echo
            # has no authority over the validated, unchanged beat references.
            reviewed_script = {**context["draft_script"], "evidence_ids": ["appearance", "stale_summary_fact"]}
            return chat_output({"script": reviewed_script, "ready": True, "execution_check": execution_check(),
                "checks": [{"criterion": name, "passed": True, "note": "明确的离线验收夹具"} for name in CRITERIA]})
        if stage == "storyboard":
            assert context["selected_direction"] == selected_story
            if progressive:
                assert context["brief"] == candidates.brief.model_dump()
                assert "expression_schedule" not in context and "approved_ad_script" not in context
                assert set(context["schema"]["properties"]) == {"title", "video_flow", "ad_script", "visual_direction", "shots"}
                assert "evidence_ids" not in context["schema"]["$defs"]["AdScript"]["properties"]
                script = deepcopy(context["draft_script"])
                for field in ("evidence_ids", "skill_version"):
                    script.pop(field, None)
                people = [person["id"] for person in (selected_direction.get("production") or {}).get("characters", [])]
                description = ("办公者将同一瓶子摆在办公桌留出的位置，松手后保留完整瓶身外观。" if people
                               else "同一瓶子直立在办公桌留出的位置，镜头缓缓靠近，完整瓶身外观保持可见。")
                script["beats"][0]["timing"] = {
                    "visual_seconds": 3, "speech_overlap": True,
                    "action_plan": description + "画外音可同时说明桌面搭配。",
                    "start_state": "同一瓶子位于办公桌上方，办公者持稳瓶身。" if people else "同一瓶子直立在办公桌上。",
                    "end_state": "同一瓶子直立在办公桌留出的位置。", "time_transition": "continuous"}
                return chat_output({"title": "办公桌的一角", "ad_script": script,
                    "visual_direction": {
                        "version": "1.0.0", "product_anchor_ids": ["appearance"],
                        "scene": "办公桌与远处的办公椅", "lighting": "左侧柔和窗光",
                        "color_palette": "灰色桌面保留商品本色", "camera_style": "中景看清摆放动作与完整瓶身",
                        "sound": "自然普通话画外音，低声介绍桌面搭配"},
                    "shots": [{"id": "shot_1", "coverage": [{"beat_id": "b1", "weight": 1}],
                        "description": description, "purpose": "呈现办公桌与瓶身的搭配关系",
                        "evidence_ids": ["appearance"], "character_ids": people,
                        "continuity": {"start_state": script["beats"][0]["timing"]["start_state"],
                            "allowed_changes": "瓶子摆稳后手离开，商品结构不改变。" if people else "商品静置，仅摄影机缓慢靠近。",
                            "end_state": script["beats"][0]["timing"]["end_state"],
                            "camera_move": "中景缓慢靠近", "entry_transition": "opening",
                            "entry_reason": "建立商品与办公桌的外观搭配"}}]})
            assert "selected_direction" not in context["creative_strategy"]
            assert context["creative_strategy"]["brief"] == candidates.brief.model_dump()
            compiled = compiled_from_reservations(context)
            return chat_output(compiled)
        if stage == "plan_review":
            assert progressive
            assert set(context["schema"]["properties"]) == {
                "ready", "checks", "execution_check", "alignment", "policy_version", "suggestions"}
            assert all(item["type"] != "image_url" for item in payload["messages"][1]["content"])
            compiled = context["final_plan"]
            assert set(compiled["ad_script"]) == {"beats"}
            assert all("purpose" not in beat and "message" not in beat for beat in compiled["ad_script"]["beats"])
            assert all("purpose" not in shot for segment in compiled["segments"] for shot in segment["shots"])
            assert all("visual_evidence" not in anchor for segment in compiled["segments"]
                       for span in segment["speech_spans"] for anchor in span.get("anchors", []))
            assert context["execution_windows"]
            return chat_output({"ready": True, "policy_version": 1, "suggestions": [],
                "execution_check": execution_check(),
                "checks": [{"criterion": name, "passed": True, "note": "明确的离线最终方案审核夹具"} for name in CRITERIA],
                "alignment": progressive_review_alignment(compiled)})
        assert stage == "alignment_review"
        assert not progressive
        return chat_output(directed_review(compiled))

    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: real_client(*args, transport=httpx.MockTransport(handle), **kwargs))
    gateway = Gateway(settings)
    pipeline = Pipeline(app.state.store, gateway, settings)
    first = post_turn(env, content="请策划广告").json()["job"]
    if historical_choice:
        # An old persisted offer must remain selectable without a new strategy
        # request or a fabricated synopsis, under either configured protocol.
        assert app.state.store.claim_job(first["id"])
        app.state.store.finish_decision(first["id"], {"kind": "directions", "message": "历史方向",
            "direction_candidates": candidates.model_dump()})
    else:
        result = asyncio.run(pipeline.execute(first["id"]))
        assert result["status"] == "awaiting_input", result.get("error")
        assert calls == ["strategy", "direction_review"]
        saved_candidates = app.state.store.get_job(first["id"])["payload"]["direction_candidates"]
        assert saved_candidates["direction_review"]["ready"]
    assert app.state.store.get_project(pid)["plan"] is None
    gate = app.state.store.pending_gate(pid)
    continuation = select(env, gate).json()["job"]
    # A new worker instance continues the durable choice without rerunning strategy.
    restored = Pipeline(Store(app.state.store.db_path, app.state.store.media_root), Gateway(settings), settings)
    result = asyncio.run(restored.execute(continuation["id"]))
    assert result["status"] == "ready", result.get("error")
    plan_stages = ["ad_script", "storyboard", "plan_review"] if progressive else ["ad_script", "script_review", "storyboard", "alignment_review"]
    assert calls == ([] if historical_choice else ["strategy", "direction_review"]) + plan_stages
    plan = app.state.store.get_project(pid)["plan"]
    assert plan["strategy"]["selected_direction_id"] == "direction_1"
    assert plan["skill_trace"]
    if progressive:
        assert plan["plan_review"]["ready"]
        assert plan["plan_review"]["policy_version"] == 1 and plan["plan_review"]["suggestions"] == []
        assert len(plan["plan_review"]["reviewed_plan_sha256"]) == 64
        assert plan["plan_review"]["alignment"] == progressive_review_alignment(compiled)
        assert plan.get("script_review") is None and plan.get("alignment_review") is None
        assert plan["segments"][0]["shots"][0]["description"] == compiled["segments"][0]["shots"][0]["description"]
        for saved_segment, wire_segment in zip(plan["segments"], compiled["segments"], strict=True):
            for saved_span, wire_span in zip(saved_segment["speech_spans"], wire_segment["speech_spans"], strict=True):
                keys = ("text", "start", "end", "shot_ids")
                assert {key: saved_span[key] for key in keys} == {key: wire_span[key] for key in keys}
                saved_anchors = [{key: value for key, value in anchor.items() if key != "visual_evidence"}
                                 for anchor in saved_span["anchors"]]
                if "anchors" in wire_span:
                    assert wire_span["anchors"] == saved_anchors
                else:
                    # Omission is valid only for one exactly equivalent anchor.
                    assert saved_anchors == [{key: wire_span[key] for key in keys}]
    else:
        assert plan["script_review"]["ready"] and plan["alignment_review"]["ready"]
        assert plan.get("plan_review") is None
    assert plan["skill_trace"][0]["stage"] == ("ad_script" if historical_choice else "orchestrator")
    assert next(d for d in plan["strategy"]["directions"] if d["id"] == "direction_1") == selected_direction
    assert all("content" not in trace for trace in plan["skill_trace"])
    assert app.state.store.pending_gate(pid) is None
    assert [a["kind"] for a in app.state.store.project_detail(pid)["assets"]] == ["product"]


def test_candidate_builder_accepts_wire_dicts_without_preselecting(env):
    candidates = candidates_for(env)
    result = build_direction_candidates(candidates.brief.model_dump(),
        [direction.model_dump() for direction in candidates.directions], None, select_rules(None))
    assert len(result.directions) == 3 and result.recommended_direction_id is None
    assert "selected_direction_id" not in result.model_dump()


def test_product_supplement_keeps_direction_and_does_not_open_clarification(env, monkeypatch):
    _, app, pid, asset = env
    candidate = candidates_for(env)
    current = {"strategy": candidate.select("direction_1").model_dump()}
    newer = {**asset, "id": "asset_later", "name": "补充蛋糕.png", "created_at": asset["created_at"] + 5}
    calls = []

    async def planned(self, system, context, originals, max_tokens=7000):
        calls.append(context)
        assert context["stage"] == "strategy"
        assert context["allowed_decisions"] == ["plan"]
        assert context["existing_direction"]["id"] == "direction_1"
        assert context["product_supplement"]
        assert "direction_reopen" not in context
        return {"kind": "clarify", "message": "请重新确认需求。", "questions": [{"id": "again", "prompt": "再选一次方向？", "options": [], "allow_custom": True, "allow_skip": True}]}

    monkeypatch.setattr(Gateway, "_planner_json", planned)
    with pytest.raises(ProviderError):
        asyncio.run(Gateway(app.state.settings).decide(app.state.store.get_project(pid), [asset, newer],
            [{"role": "user", "content": "这是草莓蛋糕，表面有奶油和水果。"}], [], current))
    assert [item["stage"] for item in calls] == ["strategy", "strategy"]
    assert app.state.store.pending_gate(pid) is None


def test_first_directions_do_not_claim_old_ones_were_set_aside(env, monkeypatch):
    _, app, pid, asset = env
    with app.state.store._connection(True) as connection:
        connection.execute(
            "UPDATE projects SET ad_form=?, ad_form_updated_context_version=1 WHERE id=?",
            ("presenter_pitch", pid))
    project = app.state.store.get_project(pid)
    candidate = candidates_for(env)

    async def planned(self, system, context, originals, max_tokens=7000):
        if context["stage"] == "direction_review":
            return approved_direction_review(context["directions"])
        assert "先前的方向先放下" not in context.get("direction_reopen", "")
        return {"kind": "directions", "message": "请选择方向", "brief": candidate.brief.model_dump(),
            "directions": [item.model_dump() for item in candidate.directions],
            "concept_pool": concept_pool_for(candidate.directions)}

    monkeypatch.setattr(Gateway, "_planner_json", planned)
    result = asyncio.run(Gateway(app.state.settings).decide(
        project, [asset], [{"role": "user", "content": "人物讲解"}], [], None))
    assert result["kind"] == "directions"
    assert "先前的方向先放下" not in result["message"]
    assert "准备了三个不同的广告方向" in result["message"]


def test_explicit_reselection_says_the_previous_direction_is_set_aside(env, monkeypatch):
    _, app, pid, asset = env
    candidate = candidates_for(env)
    current = {"strategy": candidate.select("direction_1").model_dump()}
    calls = []

    async def planned(self, system, context, originals, max_tokens=7000):
        calls.append(context)
        if context["stage"] == "direction_review":
            return approved_direction_review(context["directions"])
        assert context["existing_direction"] is None
        assert "directions" in context["allowed_decisions"]
        assert "先前" in context["direction_reopen"]
        return {"kind": "directions", "message": "请选择新方向", "brief": candidate.brief.model_dump(),
            "directions": [item.model_dump() for item in candidate.directions],
            "concept_pool": concept_pool_for(candidate.directions)}

    monkeypatch.setattr(Gateway, "_planner_json", planned)
    result = asyncio.run(Gateway(app.state.settings).decide(app.state.store.get_project(pid), [asset],
        [{"role": "user", "content": "重新给我三个创意方向"}], [], current))
    assert result["kind"] == "directions"
    assert "先前的选择先放下" in result["message"]
    assert calls[0]["stage"] == "strategy" and calls[1]["stage"] == "direction_review"


@pytest.mark.parametrize("text,kept", [
    ("这是草莓蛋糕，表面有奶油和水果。", True),
    ("把台词改短一些", False),
    ("第一个方向是什么意思？", False),
    ("重新给我三个创意方向", False),
])
def test_product_note_keeps_direction_unless_the_merchant_asks_to_change_it(text, kept):
    assert keeps_selected_direction([{"role": "user", "content": text}]) is kept
    assert keeps_selected_direction([{"role": "user", "content": "把台词改短一些"}], has_new_image=True) is True


@pytest.mark.parametrize("content,reselect", [("把台词改短一些", False), ("重新给我三个创意方向", True)])
@pytest.mark.parametrize("historical_selected", [False, True])
def test_gateway_reopens_choices_only_on_explicit_reselection(env, monkeypatch, content, reselect, historical_selected):
    _, app, pid, asset = env
    candidate = candidates_for(env)
    current = {"strategy": candidate.select("direction_1").model_dump()}
    if historical_selected:
        current["strategy"]["brief"].pop("creative_intent")
        for direction in current["strategy"]["directions"]:
            direction.pop("film_concept")
            direction.pop("story_summary")
            direction.pop("attraction_mechanism")
            direction.pop("audience_value")
            direction.pop("product_expression")
    calls = []
    async def planned(self, system, context, originals, max_tokens=7000):
        calls.append(context)
        if context["stage"] == "direction_review":
            return approved_direction_review(context["directions"])
        if reselect:
            assert context["existing_direction"] is None
            assert context["direction_selection_required"] is True
            return {"kind": "directions", "message": "请选择新方向", "brief": candidate.brief.model_dump(),
                "directions": [item.model_dump() for item in candidate.directions],
                "concept_pool": concept_pool_for(candidate.directions)}
        assert context["existing_direction"]["id"] == "direction_1"
        assert context["allowed_decisions"] == ["reply", "clarify", "plan"]
        required = context["schema"]["$defs"]["CreativeDirection"]["required"]
        assert ("story_summary" not in required) is historical_selected
        assert ("attraction_mechanism" not in required) is historical_selected
        return {"kind": "reply", "message": "沿用当前方向，台词可以缩短。"}
    monkeypatch.setattr(Gateway, "_planner_json", planned)
    result = asyncio.run(Gateway(app.state.settings).decide(app.state.store.get_project(pid), [asset],
        [{"role": "user", "content": content}], [], current))
    assert result["kind"] == ("directions" if reselect else "reply") and len(calls) == (2 if reselect else 1)


@pytest.mark.parametrize("field", ["story_summary", "attraction_mechanism", "audience_value", "product_expression"])
@pytest.mark.parametrize("value", [None, "", " \n\t "])
def test_local_edit_cannot_silently_drop_selected_story(env, monkeypatch, field, value):
    _, app, pid, asset = env
    candidate = candidates_for(env)
    selected = candidate.directions[1].model_dump()
    if value is None:
        selected.pop(field)
    else:
        selected[field] = value
    calls = []
    async def planned(self, system, context, originals, max_tokens=7000):
        calls.append(context["stage"])
        assert context["stage"] == "strategy"
        required = context["schema"]["$defs"]["CreativeDirection"]["required"]
        assert {"story_summary", "attraction_mechanism"} <= set(required)
        return {"kind": "plan", "message": "只缩短台词", "brief": candidate.brief.model_dump(),
            "directions": [selected], "selected_direction_id": selected["id"]}
    monkeypatch.setattr(Gateway, "_planner_json", planned)
    current = {"strategy": candidate.select("direction_1").model_dump()}
    original = deepcopy(current)
    with pytest.raises(ProviderError, match="策划回复未通过"):
        asyncio.run(Gateway(app.state.settings).decide(app.state.store.get_project(pid), [asset],
            [{"role": "user", "content": "把台词改短一点"}], [], current))
    # One internal correction is allowed; repeating the same invalid output
    # still stops before authoring and cannot replace the selected story.
    assert current == original and calls == ["strategy", "strategy"]


@pytest.mark.parametrize("answered", [False, True])
def test_direction_gate_ui_flags_never_enter_planner_context(env, monkeypatch, answered):
    _, app, pid, asset = env
    candidate = candidates_for(env)
    gate = {"id": "old-direction-gate", "kind": "direction", "status": "answered" if answered else "pending",
        "options": [{**d.model_dump(), "recommended": i == 0, "ui_badge": "UNRELATED_UI_DATA"}
            for i, d in enumerate(candidate.directions)], "selected_option_id": "direction_1" if answered else None}
    original = deepcopy(gate)
    async def planned(self, system, context, originals, max_tokens=7000):
        for i, option in enumerate(context["gates"][0]["options"]):
            assert option == candidate.directions[i].model_dump()
        if answered:
            assert context["existing_direction"] == candidate.directions[1].model_dump()
        assert "UNRELATED_UI_DATA" not in json.dumps(context)
        return {"kind": "reply", "message": "解释方向。"}
    monkeypatch.setattr(Gateway, "_planner_json", planned)
    result = asyncio.run(Gateway(app.state.settings).decide(app.state.store.get_project(pid), [asset],
        [{"role": "user", "content": "这个方向是什么意思？"}], [gate], None))
    assert result["kind"] == "reply" and gate == original


@pytest.mark.parametrize("message,replace", [
    ("重新给我三个创意方向", True), ("第二个方向是什么意思？", False), ("只调整第二个方向", False)])
def test_reselection_omits_rejected_candidate_prose_but_preserves_evidence_history(env, monkeypatch, message, replace):
    _, app, pid, asset = env
    candidate = candidates_for(env)
    gate = {"id": "previous-directions", "kind": "direction", "status": "pending",
        "message": "REJECTED_CANDIDATE_SUMMARY", "options": [d.model_dump() for d in candidate.directions],
        "selected_option_id": None}
    clarification = {"id": "confirmed-channel", "kind": "clarification", "status": "answered",
        "message": "选择渠道", "questions": [{"id": "channel", "prompt": "主要用在哪里？", "options": []}],
        "answers": [{"question_id": "channel", "text": "办公室展示"}]}
    messages = [{"role": "user", "content": "原始商品资料要保留"},
        {"role": "user", "content": "主要用在哪里？：办公室展示"}, {"role": "user", "content": message}]
    gates_before, messages_before = deepcopy([clarification, gate]), deepcopy(messages)
    async def planned(self, system, context, originals, max_tokens=7000):
        historical = context["gates"][1]
        if replace:
            assert historical == {"id": gate["id"], "kind": "direction", "status": "pending"}
            assert context["existing_direction"] is None
            assert "REJECTED_CANDIDATE_SUMMARY" not in json.dumps(context)
        else:
            assert historical["options"] == gate["options"]
            assert historical["message"] == gate["message"]
        assert context["gates"][0] == clarification
        assert context["conversation"] == messages
        assert "原始商品资料要保留" in context["evidence_sources"].values()
        assert any("办公室展示" in value for value in context["evidence_sources"].values())
        return {"kind": "reply", "message": "读取资料"}
    monkeypatch.setattr(Gateway, "_planner_json", planned)
    result = asyncio.run(Gateway(app.state.settings).decide(app.state.store.get_project(pid), [asset], messages,
        [clarification, gate], None))
    assert result["kind"] == "reply"
    assert [clarification, gate] == gates_before and messages == messages_before


def test_private_schema_diagnostic_has_only_bounded_locations_and_types(env, monkeypatch, caplog):
    _, app, pid, asset = env
    candidate = candidates_for(env)
    directions = [direction.model_dump() for direction in candidate.directions]
    private_value = "NEVER_LOG_MODEL_VALUE_OR_CREDENTIAL"
    invalid_key = "https://private.example/path?token=NEVER_LOG_UNKNOWN_URL"
    directions[0]["recommended"] = {"api_key": private_value}
    directions[1][invalid_key] = private_value
    directions[2]["X" * 200] = private_value
    async def planned(self, system, context, originals, max_tokens=7000):
        if context.get("stage") == "direction_review":
            return approved_direction_review(context["directions"])
        return {"kind": "directions", "message": "选择方向", "brief": candidate.brief.model_dump(),
            "directions": directions, "concept_pool": concept_pool_for(candidate.directions)}
    monkeypatch.setattr(Gateway, "_planner_json", planned)
    with caplog.at_level(logging.WARNING, logger="app.providers.gateway"):
        result = asyncio.run(Gateway(app.state.settings).decide(
            app.state.store.get_project(pid), [asset], [], [], None))
    assert result["kind"] == "directions"
    saved = result["direction_candidates"]["directions"]
    assert "recommended" not in saved[0]
    assert invalid_key not in saved[1]
    assert "X" * 200 not in saved[2]
    blob = json.dumps(result, ensure_ascii=False) + caplog.text
    for sensitive in (private_value, invalid_key, "X" * 200):
        assert sensitive not in blob


@pytest.mark.parametrize("kind", ["reply", "clarify"])
def test_production_without_image_still_replies_or_clarifies(env, monkeypatch, kind):
    _, app, pid, _ = env
    async def planned(self, system, context, originals, max_tokens=7000):
        assert context["allowed_decisions"] == ["reply", "clarify"] and originals == []
        result = {"kind": kind, "message": "请补充商品图片。"}
        if kind == "clarify":
            result["questions"] = [{"id": "product", "prompt": "要展示哪款商品？", "options": [], "allow_custom": True}]
        return result
    monkeypatch.setattr(Gateway, "_planner_json", planned)
    result = asyncio.run(Gateway(app.state.settings).decide(app.state.store.get_project(pid), [], [], [], None))
    assert result["kind"] == kind and result.get("plan") is None
