"""Progressive planning contracts and real gateway transport, entirely offline."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest

from app.ad_script import AD_EXPRESSION_VERSION
from app.av_review import review_execution_windows
from app.db import StaleRevision
from app.generation_policy import generation_groups
from app.plan_review import plan_review_fingerprint
from app.progressive_planning import compile_director, director_schema, fixed_speech
from app.providers.gateway import Gateway, ProviderError
from app.runtime_skills import stage_skill_context
from app.schemas import Plan
from app.strategy import StrategyRecord, build_strategy, select_rules
from ad_script_fixtures import CRITERIA, execution_check, with_action_timing
from test_direction_selection import candidates_for
from test_providers import settings, source, install_transport, chat_output, request_context


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    async def denied(*args, **kwargs):
        pytest.fail("Progressive planning tests must not contact a real supplier")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied)


def chosen_strategy(source, mode="voiceover"):
    candidates = candidates_for((None, None, None, source))
    strategy = build_strategy(candidates.brief, candidates.directions, "direction_1",
                              select_rules(None), merchant_choice=True)
    raw = strategy.model_dump()
    production = next(d for d in raw["directions"] if d["id"] == "direction_1")["production"]
    production.update(speech_mode=mode, speaker_id="char_worker" if mode == "character_dialogue" else "",
                      voice_description="" if mode == "none" else "自然普通话，平缓介绍。")
    return StrategyRecord.model_validate(raw)


def director_output(strategy, *, count=3):
    selected = next(d for d in strategy.directions if d.id == strategy.selected_direction_id)
    script = with_action_timing({"core_message": "瓶身轮廓与办公桌的搭配关系",
        "audience_takeaway": "记住桌角的同一瓶身轮廓", "product_role": "可辨认瓶身承担桌面搭配",
        "evidence_ids": ["appearance"], "skill_version": AD_EXPRESSION_VERSION,
        "beats": [{"id": f"b{i}", "purpose": f"观察瓶身第{i + 1}处轮廓",
            "message": f"辨认瓶身第{i + 1}处外观关系", "visual_role": "同一瓶子静置办公桌角",
            "narration": "" if selected.production.speech_mode == "none" else ["看轮廓。", "看桌角。", "留一处。"][i % 3],
            "evidence_ids": ["appearance"]} for i in range(count)]})
    shots = [{"id": f"shot_{i}", "coverage": [{"beat_id": f"b{i}", "weight": 1}],
        "description": f"办公桌角，同一瓶子直立，展示第{i + 1}处轮廓，办公者在旁。",
        "purpose": f"辨认第{i + 1}处轮廓", "evidence_ids": ["appearance"], "character_ids": ["char_worker"],
        "continuity": {"start_state": "同一商品静置", "end_state": "同一商品静置",
            "allowed_changes": "商品保持直立，不发生取放", "camera_move": "缓慢移近同一瓶身",
            "entry_transition": "opening" if i == 0 else "continuous", "entry_reason": "辨认轮廓"}}
        for i in range(count)]
    return {"title": "桌角轮廓", "ad_script": script,
        "visual_direction": {"product_anchor_ids": ["appearance"], "scene": "办公桌与办公椅",
            "lighting": "左侧自然窗光", "color_palette": "浅灰桌面，保留商品原色",
            "camera_style": "靠近瓶身，保持空间方位", "sound": "低环境声与轻音乐"}, "shots": shots}


def accepted_review(plan):
    return {"policy_version": 1, "suggestions": [], "ready": True, "checks": [{"criterion": item, "passed": True,
        "note": "明确的离线审核夹具，检查实际画面与台词。"} for item in CRITERIA],
        "execution_check": execution_check(), "alignment": {"ready": True,
            "ad_expression_passed": True, "summary": "离线审核夹具，全部实际镜头与时段均已逐项覆盖。",
            "spans": [{"segment_id": segment["id"], "span_id": span["id"], "passed": True,
                "speaker_visibility": "visible_speaking" if (plan.get("production") or {}).get("speech_mode") == "character_dialogue" else "not_applicable",
                "note": "完整台词对应实际瓶身画面。"} for segment in plan["segments"] for span in segment["speech_spans"]],
            "shots": [{"segment_id": segment["id"], "shot_id": shot["id"], "passed": True,
                "visual_execution": {"verdict": "clear", "source": "description",
                    "quote": shot["description"][:240], "explanation": "离线夹具明确审查当前镜头，实际画面与摄影变化相容。"},
                "temporal_omission": "no_omission",
                "note": "同一商品保持静置。", "pacing": {"action_seconds_required": min(1, shot["end"] - shot["start"]),
                    "action_timing_passed": True, "cut_necessity_passed": True, "note": "辨认时间足够，连续观察。"}}
                for segment in plan["segments"] for shot in segment["shots"]]}}


def reject_first_shot(review):
    review["ready"] = review["alignment"]["ready"] = False
    review["alignment"]["shots"][0].update(passed=False, note="首镜描述中动作需要简化。")


@pytest.fixture
def progressive_gateway(settings, source, install_transport, tmp_path):
    settings.data_root = tmp_path / "data"
    settings.direction_selection_enabled = True
    settings.progressive_planning_enabled = True
    settings.planning_generation_strategy = "continuity_v1"
    state = SimpleNamespace(strategy=chosen_strategy(source), calls=[], contexts=[], phases=[], traces=[],
        duration=20, brief="蓝瓶商品", conversation=[], current_plan=None, first_director_mutation=None,
        draft_mutation=None, refinement_mutation=None, first_review_mutation=None,
        second_review_mutation=None, third_review_mutation=None, refinement_mutations=[], empty_stage=None, empty_count=0,
        empty_finish="stop", empty_forever=False, inherited_skill_trace=(), ad_form=None,
        review_suffix=None, project_id=None, job_id=None)

    def handler(request):
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        _, context = request_context(request)
        stage = context["stage"]
        state.calls.append(stage)
        state.contexts.append(deepcopy(context))
        if stage == state.empty_stage and (state.empty_forever or state.empty_count == 0):
            if not state.empty_forever:
                state.empty_count += 1
            body = {"choices": [{"finish_reason": state.empty_finish, "message": {"content": ""}}]}
            if state.empty_finish == "length":
                body["usage"] = {"completion_tokens": 32768,
                    "completion_tokens_details": {"reasoning_tokens": 32766}}
            return httpx.Response(200, json=body)
        if stage == "ad_script" and context.get("task_kind") == "line_jargon_rewrite":
            value = {"lines": [{"beat_id": item["beat_id"], "narration": "吹完头发顺了。"}
                               for item in context["lines"]]}
            return chat_output(value)
        if stage == "ad_script":
            value = director_output(state.strategy)["ad_script"]
            if state.draft_mutation: state.draft_mutation(value)
            return chat_output(value)
        if stage == "storyboard":
            value = director_output(state.strategy)
            if state.first_director_mutation: state.first_director_mutation(value)
            return chat_output(value)
        if stage == "plan_refinement":
            value = deepcopy(context["draft_storyboard"])
            refinements = sum(item["stage"] == "plan_refinement" for item in state.contexts)
            if state.refinement_mutations: state.refinement_mutations[refinements - 1](value)
            elif state.refinement_mutation: state.refinement_mutation(value)
            else: value["shots"][0]["description"] += "仅保留静置辨认。"
            return chat_output(value)
        assert stage == "plan_review", f"Unexpected stage: {stage}"
        value = accepted_review(context["final_plan"])
        reviews = sum(item["stage"] == "plan_review" for item in state.contexts)
        mutation = {1: state.first_review_mutation, 2: state.second_review_mutation}.get(reviews, state.third_review_mutation)
        if mutation: mutation(value)
        if state.review_suffix:
            text = json.dumps(value, ensure_ascii=False) + state.review_suffix
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": text}}]})
        return chat_output(value)

    install_transport(handler)

    def run(progress=None):
        def report(phase):
            state.phases.append(phase)
            if progress: progress(phase)
        project = {"brief": state.brief, "duration_seconds": state.duration}
        if state.ad_form:
            project["ad_form"] = state.ad_form
        if state.project_id:
            project["id"] = state.project_id
        return asyncio.run(Gateway(settings).decide(
            project, [source], state.conversation, [],
            state.current_plan, selected_strategy=state.strategy.model_dump(), on_progress=report,
            inherited_skill_trace=state.inherited_skill_trace,
            on_skill_trace=lambda trace: state.traces.append(deepcopy(trace)),
            job_id=state.job_id))["plan"]
    state.run = run
    state.records = lambda: [json.loads(path.read_text()) for path in (settings.data_root / "diagnostics").glob("*.json")]
    return state


@pytest.mark.parametrize("duration", [10, 12, 20, 30, 31, 45, 60])
def test_three_real_stages_own_final_seconds_and_preserve_generation_policy(progressive_gateway, duration):
    state = progressive_gateway
    state.duration = duration
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review"]
    assert plan.get("script_review") is None and plan.get("alignment_review") is None
    assert plan["plan_review"]["ready"] is True
    assert plan["plan_review"]["reviewed_plan_sha256"] == plan_review_fingerprint(plan)
    grouping = generation_groups(plan["ad_script"], duration, "continuity_v1")
    assert [s["duration_seconds"] for s in plan["segments"]] == grouping["durations"]
    assert sum(s["duration_seconds"] for s in plan["segments"]) == duration
    assert len(plan["segments"]) == (1 if duration <= 30 else 2)
    assert plan["strategy"] == state.strategy.model_dump()
    director = state.contexts[1]
    for field in ("expression_schedule", "segment_durations", "approved_ad_script", "draft_generation_groups"):
        assert field not in director
    for segment in plan["segments"]:
        assert segment["shots"][0]["start"] == 0
        assert segment["shots"][-1]["end"] == segment["duration_seconds"]
        for span in segment["speech_spans"]:
            expected = [shot["id"] for shot in segment["shots"] if shot["start"] < span["end"] and shot["end"] > span["start"]]
            assert span["shot_ids"] == expected
    assert plan["skill_trace"] == state.traces
    expected_loaded = [(context["stage"], skill) for context in state.contexts
                       for skill in [*context["stage_skills"]["skills"],
                                     *context["stage_skills"].get("shared_references", [])]]
    assert len(state.traces) == len(expected_loaded)
    for (stage, loaded), trace in zip(expected_loaded, state.traces):
        bundle = stage_skill_context(stage)
        server_loaded = next(item for item in [*bundle["skills"], *bundle.get("shared_references", [])]
                             if item["id"] == loaded["id"])
        assert trace["stage"] == stage and trace["id"] == loaded["id"]
        assert trace["content_sha256"] == server_loaded["content_sha256"]
        assert loaded["content"] == server_loaded["content"]
        assert not {"content_sha256", "source_content_sha256", "resources", "version"}.intersection(loaded)
        if "loaded_sections" in server_loaded:
            assert trace["loaded_sections"] == server_loaded["loaded_sections"]
        else:
            assert trace["execution_kind"] == "shared_reference" and trace["path"] == server_loaded["path"]


def test_schema_has_no_model_owned_final_seconds_or_review_results():
    schema = director_schema()
    assert set(schema["properties"]) == {"title", "ad_script", "visual_direction", "shots", "state_changes"}
    shot = schema["$defs"]["DirectedShot"]["properties"]
    assert not {"start", "end", "speech_spans", "narration", "passed"}.intersection(shot)


def test_schema_valid_snap_is_kept_when_the_schedule_check_fails(source, monkeypatch):
    strategy = chosen_strategy(source)
    raw = director_output(strategy, count=2)
    from app import progressive_planning as planning_module

    def mark(segments, schedule):
        segments[0]["shots"][0]["description"] += "已对齐。"
        return True

    def refuse(plan, schedule):
        raise ValueError("schedule")

    monkeypatch.setattr("app.story_logic.snap_declared_speech", mark)
    monkeypatch.setattr(planning_module, "validate_expression_schedule", refuse)
    plan, _, _ = compile_director(raw, strategy, 12, "continuity_v1", [source["id"]])
    assert "已对齐。" in plan["segments"][0]["shots"][0]["description"]


def test_one_shot_covers_two_beats_without_forced_cut(source):
    strategy = chosen_strategy(source)
    raw = director_output(strategy, count=2)
    raw["shots"] = [raw["shots"][0]]
    raw["shots"][0]["coverage"].append({"beat_id": "b1", "weight": 1})
    before = deepcopy(raw)
    plan, _, _ = compile_director(raw, strategy, 12, "continuity_v1", [source["id"]])
    segment = plan["segments"][0]
    assert raw == before and len(segment["shots"]) == 1
    assert segment["shots"][0]["beat_ids"] == ["b0", "b1"]
    assert segment["shots"][0]["end"] == 12
    assert len(segment["speech_spans"]) == 2
    assert all(span["shot_ids"] == ["shot_0"] for span in segment["speech_spans"])


def test_one_beat_across_shots_retains_one_whole_sentence_and_actual_intersections(source):
    strategy = chosen_strategy(source)
    raw = director_output(strategy, count=1)
    second = deepcopy(raw["shots"][0])
    second.update(id="later")
    second["continuity"]["entry_transition"] = "continuous"
    raw["shots"].append(second)
    plan, _, _ = compile_director(raw, strategy, 10, "continuity_v1", [source["id"]])
    segment = plan["segments"][0]
    assert [(s["start"], s["end"]) for s in segment["shots"]] == [(0, 5), (5, 10)]
    assert len(segment["speech_spans"]) == 1
    assert segment["speech_spans"][0]["shot_ids"] == ["shot_0"]
    assert segment["speech_spans"][0]["text"] == "看轮廓。"


def test_shared_middle_shot_can_cover_end_of_one_beat_and_start_of_next(source):
    strategy = chosen_strategy(source)
    raw = director_output(strategy, count=2)
    middle = deepcopy(raw["shots"][1])
    middle.update(id="bridge", coverage=[{"beat_id": "b0", "weight": 1}, {"beat_id": "b1", "weight": 1}])
    raw["shots"].insert(1, middle)
    plan, _, _ = compile_director(raw, strategy, 12, "continuity_v1", [source["id"]])
    shots = plan["segments"][0]["shots"]
    assert [(s["start"], s["end"]) for s in shots] == [(0, 3), (3, 9), (9, 12)]
    assert shots[1]["beat_ids"] == ["b0", "b1"]


def test_nonoverlap_keeps_speech_after_action_and_reviews_exact_windows(source):
    strategy = chosen_strategy(source)
    raw = director_output(strategy, count=1)
    raw["ad_script"]["beats"][0]["timing"].update(visual_seconds=6, speech_overlap=False)
    plan, schedule, _ = compile_director(raw, strategy, 10, "continuity_v1", [source["id"]])
    span = plan["segments"][0]["speech_spans"][0]
    window = review_execution_windows(schedule, plan["segments"])[0]["beats"][0]
    assert window["visual_start"] == 0 and window["visual_end"] == 6
    assert window["speech_overlap"] is False and span["start"] >= 6
    assert (span["start"], span["end"]) == (window["speech_start"], window["speech_end"])


@pytest.mark.parametrize("mode", ["character_dialogue", "none"])
def test_confirmed_speech_modes_survive_all_three_stages(progressive_gateway, source, mode):
    state = progressive_gateway
    state.strategy = chosen_strategy(source, mode)
    plan = state.run()
    spans = [span for segment in plan["segments"] for span in segment["speech_spans"]]
    assert plan["production"]["speech_mode"] == mode
    if mode == "none":
        assert plan["script"] == "" and not spans
        assert len(plan["plan_review"]["alignment"]["shots"]) == 3
    else:
        assert all(span["speaker_id"] == "char_worker" for span in spans)


def test_character_dialogue_cannot_hide_the_speaker_during_speech(source):
    strategy = chosen_strategy(source, "character_dialogue")
    raw = director_output(strategy)
    raw["shots"][0]["character_ids"] = []
    with pytest.raises(ValueError, match="说话角色"):
        compile_director(raw, strategy, 20, "continuity_v1", [source["id"]])


@pytest.mark.parametrize("invalid", ["unknown_beat", "duplicate", "reverse", "missing", "foreign_fact", "scene_change", "request_boundary", "model_seconds"])
def test_authored_coverage_and_ownership_failures_never_get_silently_repaired(source, invalid):
    strategy = chosen_strategy(source)
    raw = director_output(strategy)
    duration = 20
    if invalid == "unknown_beat": raw["shots"][0]["coverage"][0]["beat_id"] = "invented"
    elif invalid == "duplicate": raw["shots"][0]["coverage"] *= 2
    elif invalid == "reverse": raw["shots"].reverse()
    elif invalid == "missing": raw["shots"].pop()
    elif invalid == "foreign_fact": raw["ad_script"]["beats"][0]["evidence_ids"] = ["invented"]
    elif invalid == "model_seconds": raw["shots"][0]["start"] = 0
    else:
        raw["shots"] = [raw["shots"][0]]
        raw["shots"][0]["coverage"] = [{"beat_id": f"b{i}", "weight": 1} for i in range(3)]
        if invalid == "scene_change": raw["ad_script"]["beats"][1]["boundary_before"] = "scene_change"
        else: duration = 45
    if invalid == "model_seconds":
        plan, _, _ = compile_director(raw, strategy, duration, "continuity_v1", [source["id"]])
        assert plan["segments"]
        return
    with pytest.raises(ValueError):
        compile_director(raw, strategy, duration, "continuity_v1", [source["id"]])


def test_independent_review_sees_final_timeline_without_director_self_justifications(progressive_gateway):
    state = progressive_gateway
    plan = state.run()
    reviewed = state.contexts[-1]["final_plan"]
    assert "core_message" not in reviewed["ad_script"]
    assert all("purpose" not in beat and "message" not in beat for beat in reviewed["ad_script"]["beats"])
    for actual, segment in zip(plan["segments"], reviewed["segments"]):
        assert "prompt" not in segment and "purpose" not in segment
        assert all("purpose" not in shot and "entry_reason" not in shot["continuity"] for shot in segment["shots"])
        assert [(s["start"], s["end"], s["description"]) for s in actual["shots"]] == [(s["start"], s["end"], s["description"]) for s in segment["shots"]]
        assert len(segment["speech_spans"]) == len(actual["speech_spans"])
        for expected_span, span in zip(actual["speech_spans"], segment["speech_spans"]):
            # The independent reviewer still receives the complete sentence,
            # actual speaking window and intersecting shots. A missing anchor
            # is valid only when it exactly duplicates those same fields.
            assert {key: value for key, value in span.items() if key != "anchors"} == {
                key: value for key, value in expected_span.items() if key != "anchors"}
            expected_anchors = [{key: value for key, value in anchor.items() if key != "visual_evidence"}
                                for anchor in expected_span["anchors"]]
            if "anchors" in span:
                assert span["anchors"] == expected_anchors
            else:
                assert expected_anchors == [{key: span[key] for key in ("text", "start", "end", "shot_ids")}]


def test_local_failure_gets_scoped_refinement_and_fresh_whole_plan_review(progressive_gateway):
    state = progressive_gateway
    state.first_review_mutation = reject_first_shot
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement", "plan_review"]
    assert state.phases.count("refining_storyboard") == 1
    repair = state.contexts[3]
    assert repair["repair_scope"] == {"beat_ids": ["b0"], "shot_ids": ["shot_0"],
        "adjacent_state_shot_ids": ["shot_1"], "adjacent_state_beat_ids": ["b1"], "allow_summary_update": False}
    assert plan["segments"][0]["shots"][0]["description"].endswith("仅保留静置辨认。")
    assert len(plan["plan_review"]["alignment"]["shots"]) == 3
    assert "script_review" not in state.calls and "strategy" not in state.calls


def test_repeated_unimproved_rejection_stops_after_five_calls(progressive_gateway):
    state = progressive_gateway
    state.first_review_mutation = state.second_review_mutation = reject_first_shot
    plan = state.run()
    assert plan["segments"]
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement", "plan_review"]


def pacing_review(seconds):
    def mutate(review):
        passed = seconds <= 6.7
        review["ready"] = review["alignment"]["ready"] = passed
        review["execution_check"]["action_capacity"] = passed
        shot = review["alignment"]["shots"][0]
        shot["passed"] = passed
        shot["pacing"].update(action_seconds_required=seconds, action_timing_passed=passed)
    return mutate


@pytest.mark.parametrize("second_required, succeeds", [(8, True), (10, False)])
def test_same_shot_overrun_can_use_remaining_round_only_with_measurable_progress(
        progressive_gateway, second_required, succeeds):
    state = progressive_gateway
    state.first_director_mutation = lambda value: value["shots"][0].update(
        description="办公者取放商品两次，整理桌角，最后辨认瓶身轮廓。")
    state.first_review_mutation = pacing_review(10)
    state.second_review_mutation = pacing_review(second_required)
    state.third_review_mutation = pacing_review(6)
    state.refinement_mutations = [
        lambda value: value["shots"][0].update(description="办公者取放商品一次，再辨认瓶身轮廓。"),
        lambda value: value["shots"][0].update(description="商品已在桌角静置，办公者站定辨认瓶身轮廓。")]
    if succeeds:
        plan = state.run()
        assert plan["plan_review"]["ready"] is True
        assert state.calls.count("plan_refinement") == 2
        assert state.calls.count("plan_review") == 3
        assert "已在桌角静置" in plan["segments"][0]["shots"][0]["description"]
    else:
        plan = state.run()
        assert plan["segments"]
        assert state.calls.count("plan_refinement") == 1
        assert state.calls.count("plan_review") == 2
    first = next(item for item in state.contexts if item["stage"] == "plan_review")
    shot = first["final_plan"]["segments"][0]["shots"][0]
    assert 6.5 <= shot["end"] - shot["start"] <= 6.7


def reject_shot(review, index):
    review["ready"] = review["alignment"]["ready"] = False
    review["alignment"]["shots"][index].update(passed=False, note="该镜头动作需要进一步调整。")


def simplify_shot(index):
    def mutate(value):
        value["shots"][index]["description"] += "人物保持站定，只观察商品。"
    return mutate


def test_two_scoped_rounds_can_resolve_different_issues_and_review_all_content(progressive_gateway):
    state = progressive_gateway
    state.first_review_mutation = lambda review: reject_shot(review, 0)
    state.second_review_mutation = lambda review: reject_shot(review, 2)
    state.refinement_mutations = [simplify_shot(0), simplify_shot(2)]
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement", "plan_review", "plan_refinement", "plan_review"]
    assert state.contexts[5]["repair_scope"]["beat_ids"] == ["b2"]
    assert state.contexts[3]["draft_storyboard"]["shots"][1] == state.contexts[5]["draft_storyboard"]["shots"][1]
    assert len(plan["plan_review"]["alignment"]["shots"]) == 3
    assert len(plan["plan_review"]["alignment"]["spans"]) == 3


def test_partly_resolved_feedback_can_use_second_round(progressive_gateway):
    state = progressive_gateway
    def reject_two(review):
        reject_shot(review, 0)
        reject_shot(review, 2)
    state.first_review_mutation = reject_two
    state.second_review_mutation = lambda review: reject_shot(review, 2)
    state.refinement_mutations = [simplify_shot(0), simplify_shot(2)]
    assert state.run()["plan_review"]["ready"] is True
    assert state.calls.count("plan_refinement") == 2


def split_first_beat(value):
    extra = deepcopy(value["shots"][0])
    extra["id"] = "same_beat_followup"
    extra["continuity"]["entry_transition"] = "continuous"
    value["shots"].insert(1, extra)


def reject_first_two_shots(review):
    reject_shot(review, 0)
    reject_shot(review, 1)


def test_one_of_two_failed_shots_in_same_beat_is_real_progress(progressive_gateway):
    state = progressive_gateway
    state.first_director_mutation = split_first_beat
    state.first_review_mutation = reject_first_two_shots
    state.second_review_mutation = lambda review: reject_shot(review, 1)
    state.refinement_mutations = [simplify_shot(0), simplify_shot(1)]
    plan = state.run()
    assert state.calls.count("plan_refinement") == 2
    assert state.calls.count("plan_review") == 3
    assert len(plan["plan_review"]["alignment"]["shots"]) == 4
    assert plan["plan_review"]["ready"] is True
    assert state.contexts[5]["repair_scope"]["beat_ids"] == ["b0"]


@pytest.mark.parametrize("rename_only", [True, False])
def test_renaming_failed_shots_cannot_reset_same_content_problem_count(progressive_gateway, rename_only):
    state = progressive_gateway
    state.first_director_mutation = split_first_beat
    state.first_review_mutation = state.second_review_mutation = reject_first_two_shots
    def rename(value):
        for shot in value["shots"][:2]:
            shot["id"] += "_renamed"
        if not rename_only:
            # Even a real wording edit must not treat renamed failing shots
            # as new issues and thereby claim the old ones were resolved.
            simplify_shot(0)(value)
    state.refinement_mutation = rename
    plan = state.run()
    assert plan["segments"]
    assert state.calls.count("plan_refinement") == 1
    assert state.calls.count("plan_review") == (1 if rename_only else 2)


def test_review_repair_stops_at_two_even_when_the_setting_asks_for_more(progressive_gateway, settings):
    settings.planner_max_refinements = 9
    state = progressive_gateway
    state.first_review_mutation = lambda review: reject_shot(review, 0)
    state.second_review_mutation = lambda review: reject_shot(review, 2)
    state.third_review_mutation = lambda review: reject_shot(review, 1)
    state.refinement_mutations = [simplify_shot(0), simplify_shot(2), simplify_shot(1), simplify_shot(1)]
    plan = state.run()
    assert plan["segments"]
    assert state.calls.count("plan_refinement") == 2
    assert state.calls.count("plan_review") == 3


def test_two_round_budget_stops_even_if_remaining_issue_is_new(progressive_gateway):
    state = progressive_gateway
    state.first_review_mutation = lambda review: reject_shot(review, 0)
    state.second_review_mutation = lambda review: reject_shot(review, 2)
    state.third_review_mutation = lambda review: reject_shot(review, 1)
    state.refinement_mutations = [simplify_shot(0), simplify_shot(2)]
    plan = state.run()
    assert plan["segments"]
    assert state.calls.count("plan_refinement") == 2
    assert state.calls.count("plan_review") == 3


def test_nonblocking_warning_is_written_for_the_plan_job(progressive_gateway, settings):
    state = progressive_gateway
    state.project_id = "proj_r23"
    state.job_id = "job_r23"

    def coat(value):
        value["shots"][0]["description"] += "，身上披着外套。"

    state.first_director_mutation = coat
    plan = state.run()
    assert plan["segments"]
    files = list((settings.data_root / "diagnostics" / "plan-warnings").glob("*.json"))
    assert len(files) == 1
    assert files[0].name.startswith("proj_r23_job_r23_")
    saved = json.loads(files[0].read_text())
    assert saved["project_id"] == "proj_r23" and saved["job_id"] == "job_r23"
    assert saved["timestamp"] in files[0].name
    assert saved["warnings"]
    assert any("外套" in item.get("message", "") or "外套" in "".join(item.get("issues") or [])
               for item in saved["warnings"])
    assert list((settings.data_root / "diagnostics").glob("*.json")) == []


def test_cosmetic_refinement_does_not_spend_another_review_or_round(progressive_gateway):
    state = progressive_gateway
    state.first_review_mutation = reject_first_shot
    def justify(value):
        value["shots"][0]["purpose"] += "说明已经改好了。"
        value["shots"][0]["continuity"]["entry_reason"] += "这次调整可以通过检查。"
        value["ad_script"]["beats"][0]["timing"]["action_plan"] += "重新确认可以执行。"
    state.refinement_mutation = justify
    plan = state.run()
    assert plan["segments"]
    assert "说明已经改好了" not in json.dumps(plan, ensure_ascii=False)
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement"]


def test_alignment_that_never_settles_stops_and_compile_stays_off_the_loop(source, monkeypatch):
    import time
    from app import progressive_planning as planning
    from app.progressive_planning import progressive_plan
    strategy = chosen_strategy(source)
    raw = director_output(strategy)
    counts = {"align": 0, "compile": 0}
    real_compile = planning.compile_director

    def spin(container):
        counts["align"] += 1
        revised = deepcopy(container)
        shots = revised.get("shots") or []
        if shots:
            shots[-1]["continuity"]["end_state"] = f"再改一次{counts['align']}"
        return revised, True

    monkeypatch.setattr(planning, "MAX_DIRECTOR_PASSES", 6)
    monkeypatch.setattr("app.story_logic.align_declared_continuity", spin)

    async def stage_call(system, context, assets):
        if context["stage"] == "plan_review":
            return accepted_review(context["final_plan"])
        return deepcopy(raw)

    async def run():
        loop = asyncio.get_running_loop()
        compile_started = asyncio.Event()

        def slow_compile(*args, **kwargs):
            counts["compile"] += 1
            if counts["compile"] == 1:
                loop.call_soon_threadsafe(compile_started.set)
                time.sleep(0.3)
            return real_compile(*args, **kwargs)

        monkeypatch.setattr(planning, "compile_director", slow_compile)
        task = asyncio.create_task(progressive_plan(
            stage_call=stage_call, on_progress=None, record_failure=lambda _item: None,
            strategy=strategy, draft=raw["ad_script"], draft_issues=[], duration=20,
            generation_strategy="continuity_v1", assets=[source], conversation=[],
            merchant_request="蓝瓶商品", current_plan=None))
        started = time.perf_counter()
        await asyncio.wait_for(compile_started.wait(), timeout=2)
        responsive = time.perf_counter() - started
        plan = await task
        return responsive, plan

    started = time.perf_counter()
    responsive, plan = asyncio.run(run())
    assert time.perf_counter() - started < 8
    assert responsive < 0.2
    assert 1 <= counts["align"] <= 6
    assert counts["compile"] <= 8
    assert plan["segments"]


def test_content_failure_exposes_only_owned_summary_and_is_not_retryable(source):
    from app.progressive_planning import ProgressivePlanningError, progressive_plan
    from app.strategy import StrategyRecord
    strategy = chosen_strategy(source)
    raw = strategy.model_dump()
    raw["brief"]["ad_form"] = "brand_tvc"
    strategy = StrategyRecord.model_validate(raw)
    raw = director_output(strategy)
    calls = []
    async def stage_call(system, context, assets):
        calls.append(context["stage"])
        if context["stage"] == "storyboard": return deepcopy(raw)
        if context["stage"] == "plan_refinement":
            revised = deepcopy(context["draft_storyboard"])
            simplify_shot(0)(revised)
            return revised
        review = accepted_review(context["final_plan"])
        reject_first_shot(review)
        review["alignment"]["shots"][0]["note"] = "PRIVATE_MODEL_RECEIPT_DO_NOT_SHOW"
        return review
    recorded = []
    plan = asyncio.run(progressive_plan(stage_call=stage_call, on_progress=None, record_failure=recorded.append,
        strategy=strategy, draft=raw["ad_script"], draft_issues=[], duration=20,
        generation_strategy="continuity_v1", assets=[source], conversation=[], merchant_request="蓝瓶商品", current_plan=None))
    assert plan["segments"]
    assert recorded and recorded[-1]["nonblocking"] is True
    assert recorded[-1]["error_code"] == "planning_content_failed"
    assert "PRIVATE_MODEL_RECEIPT" not in json.dumps(recorded, ensure_ascii=False)
    assert plan["plan_review"]["ready"] is False
    # Soft review failure with the hard-gate table clear is an honest conservative
    # label. The note names the failed review item. It does not claim the review never ran.
    assert plan["review_status"] == "conservative_fallback"
    assert plan["review_status_note"].startswith("综合检查没通过（")
    assert "镜头" in plan["review_status_note"]
    assert plan["review_status_note"].endswith("硬门已通过")
    assert "没有完整的综合检查" not in plan["review_status_note"]
    assert plan["auto_release"]["note"] == plan["review_status_note"]
    assert "镜头" in plan["auto_release"]["failed_checks"]
    assert plan["auto_release"]["repair_rounds"] >= 1


@pytest.mark.parametrize("outside", ["beat", "shot", "global_message"])
def test_local_revision_cannot_change_unrelated_content(progressive_gateway, outside):
    state = progressive_gateway
    state.first_review_mutation = reject_first_shot
    def mutate(value):
        if outside == "beat": value["ad_script"]["beats"][2]["narration"] = "改掉末句。"
        elif outside == "shot": value["shots"][2]["description"] += "又换了整套环境。"
        else: value["ad_script"]["core_message"] = "改成全新商品故事。"
    state.refinement_mutation = mutate
    plan = state.run()
    blob = json.dumps(plan, ensure_ascii=False)
    assert "改掉末句" not in blob and "又换了整套环境" not in blob and "改成全新商品故事" not in blob
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement"]


@pytest.mark.parametrize("missing", ["checks", "execution", "shot", "span", "pacing"])
def test_incomplete_independent_reviews_never_release_or_invent_passes(progressive_gateway, missing):
    state = progressive_gateway
    def mutate(value):
        if missing == "checks": value["checks"].pop()
        elif missing == "execution": value.pop("execution_check")
        elif missing == "shot": value["alignment"]["shots"].pop()
        elif missing == "span": value["alignment"]["spans"].pop()
        else: value["alignment"]["shots"][0].pop("pacing")
    state.first_review_mutation = state.second_review_mutation = mutate
    plan = state.run()
    assert plan.get("plan_review", {}).get("ready") is not True
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_review"]


def test_unknown_fact_stops_before_review_or_refinement(progressive_gateway):
    state = progressive_gateway
    state.first_director_mutation = lambda value: value["ad_script"]["beats"][0].update(evidence_ids=["invented"])
    with pytest.raises(ProviderError, match="这次没生成出来"):
        state.run()
    assert state.calls[0] == "ad_script" and "storyboard" in state.calls
    assert state.calls.count("plan_refinement") <= 1


def test_fixed_merchant_line_is_checked_before_independent_review(progressive_gateway):
    state = progressive_gateway
    state.brief = "保留台词“看轮廓。”，继续安排。"
    state.first_director_mutation = lambda value: value["ad_script"]["beats"][0].update(narration="看外形。")
    with pytest.raises(ProviderError): state.run()
    assert state.calls == ["ad_script", "storyboard"]
    assert state.contexts[1]["fixed_speech"] == ["看轮廓。"]


def test_explicit_unlock_and_retain_current_copy_are_deterministic(source):
    raw = director_output(chosen_strategy(source))
    assert fixed_speech("固定台词“看轮廓。”", [], None) == ["看轮廓。"]
    assert fixed_speech("固定台词“看轮廓。”", [{"role": "user", "content": "台词可以改。"}], None) == []
    assert fixed_speech("", [{"role": "user", "content": "台词保持不变。"}], {"ad_script": raw["ad_script"]}) == ["看轮廓。", "看桌角。", "留一处。"]


@pytest.mark.parametrize("stage", ["ad_script", "storyboard", "plan_review"])
def test_empty_response_retries_current_stage_and_still_runs_real_independent_review(progressive_gateway, stage):
    state = progressive_gateway
    state.empty_stage = stage
    plan = state.run()
    expected = ["ad_script", "storyboard", "plan_review"]
    expected.insert(expected.index(stage), stage)
    assert state.calls == expected and plan["plan_review"]["ready"] is True
    assert state.phases.count("retrying_planner") == 1
    assert list(dict.fromkeys(item["stage"] for item in plan["skill_trace"])) == ["ad_script", "storyboard", "plan_review"]
    expected_ids = [skill["id"] for context in {item["stage"]: item for item in state.contexts}.values()
                    for skill in [*context["stage_skills"]["skills"],
                                  *context["stage_skills"].get("shared_references", [])]]
    assert [item["id"] for item in plan["skill_trace"]] == expected_ids


def test_cancel_before_refinement_prevents_extra_call(progressive_gateway):
    state = progressive_gateway
    state.first_review_mutation = reject_first_shot
    def progress(phase):
        if phase == "refining_storyboard": raise StaleRevision("TEST canceled")
    with pytest.raises(StaleRevision): state.run(progress)
    assert state.calls == ["ad_script", "storyboard", "plan_review"]


def test_final_review_cannot_be_attached_to_changed_plan(progressive_gateway):
    plan = progressive_gateway.run()
    before = deepcopy(plan)
    plan["segments"][0]["shots"][0]["description"] += "无关额外动作。"
    with pytest.raises(ValueError, match="当前最终方案"):
        Plan.model_validate(plan)
    assert Plan.model_validate(before).plan_review["ready"] is True


def overburden_first_beat(value):
    value["ad_script"]["beats"][0]["timing"].update(visual_seconds=21,
        action_plan="缓慢完成多次重复摆放，再辨认商品。")


def simplify_first_beat(value):
    value["ad_script"]["beats"][0]["timing"].update(visual_seconds=3,
        action_plan="省去重复摆放，只保留同一商品静置辨认。")
    value["shots"][0]["description"] = "省去重复取放，同一瓶子在办公桌角静置，办公者在旁辨认。"


def test_capacity_repair_happens_before_final_review_with_bounded_budget(progressive_gateway):
    state = progressive_gateway
    state.first_director_mutation = overburden_first_beat
    state.refinement_mutation = simplify_first_beat
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_refinement", "plan_review"]
    assert plan["ad_script"]["beats"][0]["timing"]["visual_seconds"] == 3
    feedback = state.contexts[2]["review_feedback"]
    assert feedback["kind"] == "deterministic_timing" and feedback["capacity"]["overrun_seconds"] > 0
    assert state.contexts[2]["repair_scope"]["allow_summary_update"] is True


def test_director_sees_computed_draft_capacity_without_prelocked_windows(progressive_gateway):
    state = progressive_gateway
    state.draft_mutation = lambda value: value["beats"][0]["timing"].update(visual_seconds=21)
    state.run()
    context = state.contexts[1]
    assert context["draft_capacity"]["target_seconds"] == 20
    assert context["draft_capacity"]["overrun_seconds"] > 0
    assert "expression_schedule" not in context
    assert state.calls == ["ad_script", "storyboard", "plan_review"]


def test_capacity_repair_leaves_one_semantic_repair_with_final_full_review(progressive_gateway):
    state = progressive_gateway
    state.first_director_mutation = overburden_first_beat
    state.refinement_mutations = [simplify_first_beat,
        lambda value: value["shots"][0].update(description="办公者在桌旁，瓶子保持静置，仅观察瓶身轮廓。")]
    state.first_review_mutation = reject_first_shot
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_refinement", "plan_review", "plan_refinement", "plan_review"]
    assert plan["plan_review"]["ready"] is True
    assert len(plan["plan_review"]["alignment"]["shots"]) == 3
    assert [item["repair_attempt"] for item in state.contexts if item["stage"] == "plan_refinement"] == [1, 2]
    assert state.contexts[4]["repair_scope"]["beat_ids"] == ["b0"]


def test_capacity_repair_cannot_shrink_estimates_without_changing_actual_actions(progressive_gateway):
    state = progressive_gateway
    state.first_director_mutation = overburden_first_beat
    def merely_lower_estimate(value):
        value["ad_script"]["beats"][0]["timing"].update(visual_seconds=3,
            action_plan="重新估计本拍三秒可完成。")
    state.refinement_mutation = merely_lower_estimate
    with pytest.raises(ProviderError, match="这次没生成出来"):
        state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_refinement", "plan_refinement"]


def test_capacity_repair_stops_if_action_changes_do_not_reduce_overrun(progressive_gateway):
    state = progressive_gateway
    state.first_director_mutation = overburden_first_beat
    state.refinement_mutation = simplify_shot(0)
    with pytest.raises(ProviderError, match="这次没生成出来"):
        state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_refinement"]


def test_two_capacity_refinements_remain_bounded_and_need_independent_review(progressive_gateway):
    state = progressive_gateway
    state.first_director_mutation = overburden_first_beat
    def partial(value):
        value["ad_script"]["beats"][0]["timing"]["visual_seconds"] = 20
        value["shots"][0]["description"] = "取消最后一次摆放，仍完整保留主要取放动作。"
    state.refinement_mutations = [partial, simplify_first_beat]
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_refinement", "plan_refinement", "plan_review"]
    assert plan["plan_review"]["ready"] is True


def test_new_capacity_conflict_receives_previous_content_feedback_together(progressive_gateway):
    state = progressive_gateway
    state.first_review_mutation = reject_first_shot
    def overload_during_repair(value):
        overburden_first_beat(value)
        value["shots"][0]["description"] += "额外完成多次取放。"
    state.refinement_mutations = [overload_during_repair, simplify_first_beat]
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement", "plan_refinement", "plan_review"]
    combined = state.contexts[4]["review_feedback"]
    assert combined["kind"] == "deterministic_timing" and combined["capacity"]["overrun_seconds"] > 0
    assert combined["previous_review"]["alignment"]["shots"][0]["passed"] is False
    assert plan["plan_review"]["ready"] is True


@pytest.mark.parametrize("change", ["still_overloaded", "summary", "visual_direction", "fact"])
def test_deterministic_repair_cannot_bypass_capacity_or_stable_contract(progressive_gateway, change):
    state = progressive_gateway
    state.first_director_mutation = overburden_first_beat
    def mutate(value):
        if change == "still_overloaded": return
        simplify_first_beat(value)
        if change == "summary": value["ad_script"]["core_message"] = "省去重复取放，直接辨认同一商品与桌角的关系。"
        elif change == "visual_direction": value["visual_direction"]["scene"] = "改到陌生厨房"
        elif change == "fact": value["ad_script"]["beats"][0]["evidence_ids"] = ["invented"]
    state.refinement_mutation = mutate
    if change in ("summary", "visual_direction"):
        plan = state.run()
        assert plan["plan_review"]["ready"] is True
        assert plan["visual_direction"]["scene"] == "办公桌与办公椅"
        if change == "summary":
            assert plan["ad_script"]["core_message"] == "省去重复取放，直接辨认同一商品与桌角的关系。"
        assert state.calls == ["ad_script", "storyboard", "plan_refinement", "plan_review"]
    else:
        with pytest.raises(ProviderError, match="这次没生成出来"):
            state.run()
        assert state.calls == ["ad_script", "storyboard", "plan_refinement"]


def test_unnecessary_cut_can_be_removed_only_inside_failed_beat(progressive_gateway):
    state = progressive_gateway
    def extra_cut(value):
        extra = deepcopy(value["shots"][0])
        extra["id"] = "redundant_cut"
        extra["continuity"]["entry_transition"] = "cut"
        value["shots"].insert(1, extra)
    def reject_cut(review):
        review["ready"] = review["alignment"]["ready"] = False
        check = review["alignment"]["shots"][1]
        check["passed"] = check["pacing"]["cut_necessity_passed"] = False
        check["note"] = "同一静置动作无必要反复切镜。"
    state.first_director_mutation = extra_cut
    state.first_review_mutation = reject_cut
    state.refinement_mutation = lambda value: value["shots"].pop(1)
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement", "plan_review"]
    assert [shot["id"] for shot in plan["segments"][0]["shots"]] == ["shot_0", "shot_1", "shot_2"]
    assert set(state.contexts[3]["repair_scope"]["shot_ids"]) == {"shot_0", "redundant_cut"}


def test_omitted_continuous_start_states_derive_across_final_provider_boundary(source):
    strategy = chosen_strategy(source)
    raw = director_output(strategy)
    for shot in raw["shots"][1:]: shot["continuity"].pop("start_state")
    plan, _, _ = compile_director(raw, strategy, 45, "continuity_v1", [source["id"]])
    shots = [shot for segment in plan["segments"] for shot in segment["shots"]]
    assert len(plan["segments"]) == 2
    assert all(current["continuity"]["start_state"] == previous["continuity"]["end_state"]
               for previous, current in zip(shots, shots[1:]))


def test_coordinator_can_rewrite_unconfirmed_draft_but_review_receives_final_words(progressive_gateway):
    state = progressive_gateway
    state.first_director_mutation = lambda value: value["ad_script"]["beats"][0].update(narration="这处轮廓，慢慢看。")
    plan = state.run()
    expected = "这处轮廓，慢慢看。"
    assert state.contexts[1]["draft_script"]["beats"][0]["narration"] == "看轮廓。"
    assert plan["ad_script"]["beats"][0]["narration"] == expected
    reviewed = state.contexts[-1]["final_plan"]
    assert reviewed["ad_script"]["beats"][0]["narration"] == expected
    assert reviewed["segments"][0]["speech_spans"][0]["text"] == expected


def test_reviewable_draft_enum_issue_goes_directly_to_coordinator_without_old_review(progressive_gateway):
    state = progressive_gateway
    state.draft_mutation = lambda value: value["beats"][0].update(boundary_before="ellipsis")
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review"]
    assert state.contexts[1]["draft_validation_issues"]
    assert state.contexts[1]["draft_script"]["beats"][0]["boundary_before"] == "ellipsis"
    assert plan["ad_script"]["beats"][0].get("boundary_before", "complete") == "complete"


def test_stacked_brand_voiceover_is_collapsed_before_the_plan_is_ready(progressive_gateway):
    state = progressive_gateway
    raw = state.strategy.model_dump()
    raw["brief"]["ad_form"] = "brand_tvc"
    state.strategy = StrategyRecord.model_validate(raw)

    def stack(value):
        beats = value["ad_script"]["beats"]
        beats[0]["narration"] = "夜里吹，几下就干，这风不吵。"
        beats[1]["narration"] = "带负离子，吹完是顺的。"
        beats[2]["narration"] = "点下面链接，把它看清楚。"
        value["shots"][0]["description"] = "同一瓶子停在夜里的桌面，机位不动。"
        value["shots"][1]["description"] = "带负离子的光扫过。旁白说这句话时画面基本停住。"
        value["shots"][1]["visual_motif"] = "负离子"

    state.first_director_mutation = stack
    plan = state.run()
    narrations = [beat["narration"] for beat in plan["ad_script"]["beats"]]
    assert narrations[0] == "夜里吹，几下就干。"
    assert narrations[1] == ""
    assert "点下面链接" in narrations[2]
    assert "不吵" not in "".join(narrations) and "负离子" not in "".join(narrations)
    assert "这风不吵" not in plan["segments"][0]["shots"][0]["description"]
    shot_text = " ".join(shot.get("description") or "" for shot in plan["segments"][0]["shots"])
    assert "负离子" not in shot_text and "旁白说这句话" not in shot_text
    assert all((shot.get("visual_motif") or "") != "负离子" for shot in plan["segments"][0]["shots"])
    assert plan["plan_review"]["ready"] is True
    assert "plan_review" in state.calls


def _lock_speech(strategy_record, mode):
    raw = strategy_record.model_dump()
    production = next(item for item in raw["directions"] if item["id"] == raw["selected_direction_id"])["production"]
    production.update(
        speech_mode=mode,
        speaker_id="char_worker" if mode == "character_dialogue" else "",
        voice_description="" if mode == "none" else "自然普通话，平缓介绍。",
    )
    return StrategyRecord.model_validate(raw)


def test_voice_identity_mixup_is_not_marked_ready(progressive_gateway):
    state = progressive_gateway
    raw = state.strategy.model_dump()
    raw["brief"]["ad_form"] = "brand_tvc"
    state.strategy = _lock_speech(StrategyRecord.model_validate(raw), "character_dialogue")
    plan = state.run()
    from app.release_gate import images_blocked_reason
    assert images_blocked_reason(plan, strategy=plan.get("strategy")) is None
    assert plan["production"]["speech_mode"] != "character_dialogue"
    assert "plan_review" not in state.calls


def test_presenter_voiceover_is_not_marked_ready(progressive_gateway):
    state = progressive_gateway
    raw = state.strategy.model_dump()
    raw["brief"]["ad_form"] = "presenter_pitch"
    state.strategy = StrategyRecord.model_validate(raw)
    plan = state.run()
    from app.release_gate import images_blocked_reason
    assert images_blocked_reason(plan, strategy=plan.get("strategy")) is None
    assert plan["production"]["speech_mode"] != "voiceover"
    assert "plan_review" not in state.calls


def test_story_voiceover_is_planned_as_the_leads_own_voice(progressive_gateway):
    state = progressive_gateway
    raw = state.strategy.model_dump()
    raw["brief"]["ad_form"] = "scene_story"
    state.strategy = StrategyRecord.model_validate(raw)
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review"]
    assert plan["production"]["speech_mode"] == "character_dialogue"
    assert plan["production"]["speaker_id"] == "char_worker"
    selected = next(item for item in plan["strategy"]["directions"]
                    if item["id"] == plan["strategy"]["selected_direction_id"])
    assert selected["production"]["speech_mode"] == "character_dialogue"
    assert selected["production"]["speaker_id"] == "char_worker"
    descriptions = [shot["description"] for shot in plan["segments"][0]["shots"]]
    assert len(descriptions) >= 2 and len(set(descriptions)) > 1
    assert all("放在画面中间，光线稳定" not in item for item in descriptions)
    assert plan["plan_review"]["ready"] is True
    assert plan.get("script_review") is None
    if any("内心独白" in item for item in descriptions):
        assert plan["production"]["speech_mode"] == "character_dialogue"


def test_a_voiceover_story_without_the_speaker_in_frame_is_storyboarded_again(source):
    from app.progressive_planning import ProgressivePlanningError, progressive_plan
    strategy = chosen_strategy(source)
    raw_strategy = strategy.model_dump()
    raw_strategy["brief"]["ad_form"] = "scene_story"
    strategy = StrategyRecord.model_validate(raw_strategy)
    calls = []

    async def stage_call(system, data, assets, max_tokens=7000):
        kind = data.get("task_kind") or data.get("stage")
        calls.append(kind)
        if data.get("stage") == "plan_review":
            return accepted_review(data["final_plan"])
        if data.get("task_kind") == "storyboard_regeneration":
            return director_output(strategy)
        if data.get("stage") == "storyboard":
            board = director_output(strategy)
            for shot in board["shots"]:
                shot["character_ids"] = []
            return board
        return {}

    plan = asyncio.run(progressive_plan(
        stage_call=stage_call, on_progress=lambda _phase: None, record_failure=lambda _item: None,
        strategy=strategy, draft=None, draft_issues=[], duration=20, generation_strategy="continuity_v1",
        assets=[source], conversation=[], merchant_request="看清蛋糕", current_plan=None,
        cta_target="none"))
    assert "storyboard" in calls and "storyboard_regeneration" in calls and "plan_review" in calls
    assert plan["plan_review"]["ready"] is True
    assert plan["production"]["speech_mode"] == "character_dialogue"
    assert plan["production"]["speaker_id"] == "char_worker"
    descriptions = [shot["description"] for shot in plan["segments"][0]["shots"]]
    assert len(set(descriptions)) > 1
    assert all("放在画面中间，光线稳定" not in item for item in descriptions)


def test_identical_shot_text_is_not_marked_ready(source):
    from app.progressive_planning import ProgressivePlanningError, is_degenerate_plan, progressive_plan
    from app.strategy import StrategyRecord
    strategy = chosen_strategy(source)
    raw_strategy = strategy.model_dump()
    raw_strategy["brief"]["ad_form"] = "brand_tvc"
    strategy = StrategyRecord.model_validate(raw_strategy)
    calls = []

    async def stage_call(system, data, assets, max_tokens=7000):
        calls.append(data.get("stage"))
        if data.get("stage") == "plan_review":
            return accepted_review(data["final_plan"])
        if data.get("stage") == "storyboard":
            board = director_output(strategy)
            for shot in board["shots"]:
                shot["description"] = "巧克力蛋糕放在画面中间，光线稳定。"
            return board
        return {}

    plan = asyncio.run(progressive_plan(
        stage_call=stage_call, on_progress=lambda _phase: None, record_failure=lambda _item: None,
        strategy=strategy, draft=None, draft_issues=[], duration=20, generation_strategy="continuity_v1",
        assets=[source], conversation=[], merchant_request="看清蛋糕", current_plan=None,
        cta_target="none"))
    from app.release_gate import images_blocked_reason
    assert images_blocked_reason(plan, strategy=plan.get("strategy")) is None
    assert not is_degenerate_plan(plan)
    assert "storyboard" in calls
    board = director_output(strategy)
    for shot in board["shots"]:
        shot["description"] = "巧克力蛋糕放在画面中间，光线稳定。"
    compiled, _, _ = compile_director(board, strategy, 20, "continuity_v1", [source["id"]], cta_target="none")
    assert is_degenerate_plan(compiled)


def test_story_explainer_is_rewritten_before_the_plan_is_ready(progressive_gateway, monkeypatch):
    state = progressive_gateway
    raw = state.strategy.model_dump()
    raw["brief"]["ad_form"] = "scene_story"
    state.strategy = _lock_speech(StrategyRecord.model_validate(raw), "character_dialogue")

    def explain(value):
        beats = value["ad_script"]["beats"]
        beats[0]["narration"] = "不敢开，怕吵醒她。"
        beats[1]["narration"] = "太吵了。"
        beats[2]["narration"] = "那我吹了。"
        value["shots"][0]["description"] = "夜里，室友侧躺在床上睡着。她站在镜前，手指停在开关上。"

    async def speak_as_the_character(stage_call, director, strategy, duration_seconds, locked_lines=(), plan=None):
        revised = deepcopy(director)
        for beat in revised["ad_script"]["beats"]:
            if beat.get("narration") == "不敢开，怕吵醒她。":
                beat["narration"] = "太响了。"
        return True, revised

    monkeypatch.setattr("app.line_quality.rewrite_director_if_needed", speak_as_the_character)
    state.first_director_mutation = explain
    plan = state.run()
    narrations = [beat["narration"] for beat in plan["ad_script"]["beats"]]
    assert narrations[0] == "太响了。"
    assert "不敢开" not in "".join(narrations)
    assert plan["plan_review"]["ready"] is True


def test_story_explainer_is_not_marked_ready_when_the_rewrite_stays(progressive_gateway, monkeypatch):
    state = progressive_gateway
    raw = state.strategy.model_dump()
    raw["brief"]["ad_form"] = "scene_story"
    state.strategy = _lock_speech(StrategyRecord.model_validate(raw), "character_dialogue")

    def explain(value):
        beats = value["ad_script"]["beats"]
        beats[0]["narration"] = "不敢开，怕吵醒她。"
        beats[1]["narration"] = "太吵了。"
        beats[2]["narration"] = "那我吹了。"
        value["shots"][0]["description"] = "夜里，室友侧躺在床上睡着。她站在镜前，手指停在开关上。"

    async def keep_explainer(*args, **kwargs):
        return True, None

    monkeypatch.setattr("app.line_quality.rewrite_director_if_needed", keep_explainer)
    state.first_director_mutation = explain
    plan = state.run()
    assert any("不敢开" in beat["narration"] for beat in plan["ad_script"]["beats"])


def test_story_punchline_is_not_marked_ready(progressive_gateway, monkeypatch):
    state = progressive_gateway
    raw = state.strategy.model_dump()
    raw["brief"]["ad_form"] = "scene_story"
    state.strategy = _lock_speech(StrategyRecord.model_validate(raw), "character_dialogue")

    def punch(value):
        beats = value["ad_script"]["beats"]
        beats[0]["narration"] = "算了，先吹干。"
        beats[1]["narration"] = "轻点。"
        beats[2]["narration"] = "它让我没白折回来。"

    async def keep_punchline(*args, **kwargs):
        return True, None

    monkeypatch.setattr("app.line_quality.rewrite_director_if_needed", keep_punchline)
    state.first_director_mutation = punch
    plan = state.run()
    assert any("没白" in beat["narration"] for beat in plan["ad_script"]["beats"])


def test_stacked_brand_voiceover_is_not_marked_ready_when_it_cannot_be_rewritten(progressive_gateway, monkeypatch):
    state = progressive_gateway
    raw = state.strategy.model_dump()
    raw["brief"]["ad_form"] = "brand_tvc"
    state.strategy = StrategyRecord.model_validate(raw)

    def stack(value):
        beats = value["ad_script"]["beats"]
        beats[0]["narration"] = "夜里吹，几下就干，这风不吵。"
        beats[1]["narration"] = "带负离子，吹完是顺的。"
        beats[2]["narration"] = "点下面链接，把它看清楚。"

    async def keep_stack(*args, **kwargs):
        return True, None

    monkeypatch.setattr(
        "app.line_quality.apply_brand_claim_collapse",
        lambda director, strategy, locked_lines=(), duration_seconds=None: (director, False))
    monkeypatch.setattr("app.line_quality.rewrite_director_if_needed", keep_stack)
    state.first_director_mutation = stack
    plan = state.run()
    from app.release_gate import images_blocked_reason
    assert images_blocked_reason(plan, strategy=plan.get("strategy")) is None
    assert plan["review_status"] == "conservative_fallback"
    spoken = "".join(beat.get("narration") or "" for beat in (plan.get("ad_script") or {}).get("beats") or [])
    assert "负离子" not in spoken and "不吵" not in spoken
    assert "plan_review" not in state.calls


def test_planner_jargon_is_rewritten_once_before_review(progressive_gateway):
    state = progressive_gateway
    state.first_director_mutation = lambda value: value["ad_script"]["beats"][0].update(
        narration="采用等离子技术，关注秀发护理。")
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "ad_script", "plan_review"]
    rewrite = state.contexts[2]
    assert rewrite["task_kind"] == "line_jargon_rewrite"
    assert rewrite["stage"] == "ad_script"
    assert [item["id"] for item in rewrite["stage_skills"]["skills"]] == ["ads-video-dialogue-writing"]
    assert rewrite["stage_skills"]["shared_references"] == []
    assert "说出口的对照" in rewrite["stage_skills"]["skills"][0]["content"]
    loaded = [trace for trace in state.traces if trace["stage"] == "line_rewrite"]
    assert len(loaded) == 1
    assert loaded[0]["loaded_sections"] == ["SKILL.md", "references/ad-dialogue-examples.md"]
    assert loaded[0]["id"] == "ads-video-dialogue-writing"
    assert "关注" not in plan["script"]
    assert plan["ad_script"]["beats"][0]["narration"] == "吹完头发顺了。"
    assert plan["ad_script"]["beats"][1]["narration"] == "看桌角。"


def test_fixed_line_is_also_protected_during_local_refinement(progressive_gateway):
    state = progressive_gateway
    state.brief = "台词逐字保留“看轮廓。”"
    state.first_review_mutation = reject_first_shot
    state.refinement_mutation = lambda value: value["ad_script"]["beats"][0].update(narration="改过的轮廓。")
    plan = state.run()
    spoken = json.dumps(plan, ensure_ascii=False)
    assert "看轮廓。" in spoken
    assert "改过的轮廓" not in spoken
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement"]


def test_craft_refine_that_still_fails_is_accepted_and_the_job_continues(progressive_gateway):
    state = progressive_gateway
    raw = state.strategy.model_dump()
    raw["brief"]["ad_form"] = "presenter_pitch"
    state.strategy = _lock_speech(StrategyRecord.model_validate(raw), "character_dialogue")

    def show_product_late(value):
        value["video_flow"] = "先看见吹风机，再讲完停住。"
        for index, shot in enumerate(value["shots"]):
            shot["product_visible"] = index != 0
            shot["continuity"]["camera_move"] = "中近景，镜头缓慢推进" if index < 2 else "中景，机位停住"
            if index == len(value["shots"]) - 1:
                shot["description"] += "机位停住。"

    state.first_director_mutation = show_product_late
    state.refinement_mutation = lambda value: None
    plan = state.run()
    assert plan["plan_review"]["ready"] is True
    repair = next(item for item in state.contexts if item["stage"] == "plan_refinement")
    assert repair["repair_scope"]["shot_ids"] == ["shot_0"]
    assert "b0" in repair["repair_scope"]["beat_ids"]
    assert any(record.get("kind") == "craft_accept" for record in state.records())
    assert "请重新策划" not in json.dumps(plan, ensure_ascii=False)
    notes = [item.get("note", "") for item in plan["plan_review"].get("suggestions") or []]
    assert any("建议" in note for note in notes)


def test_scene_story_revision_keeps_a_long_skill_trace_valid(progressive_gateway):
    state = progressive_gateway
    raw = state.strategy.model_dump()
    raw["brief"]["ad_form"] = "scene_story"
    state.strategy = _lock_speech(StrategyRecord.model_validate(raw), "character_dialogue")
    state.ad_form = "scene_story"
    state.inherited_skill_trace = [
        {"stage": "strategy", "id": f"legacy-{index}", "version": "1", "content_sha256": f"{index:064x}"}
        for index in range(18)]
    state.first_review_mutation = reject_first_shot
    state.refinement_mutation = lambda value: value["shots"][0].update(
        description=value["shots"][0]["description"] + "只保留静置。")
    plan = state.run()
    produced = len(state.inherited_skill_trace) + len(state.traces)
    assert produced > 30
    assert len(plan["skill_trace"]) <= 30
    assert plan["skill_trace"][-1]["id"] == state.traces[-1]["id"]
    assert plan["skill_trace"][0]["id"] != "legacy-0"
    Plan.model_validate(plan)
    assert any(item["id"] == "ad-form-playbook-scene-story" for item in plan["skill_trace"])
    assert plan["plan_review"]["ready"] is True


def test_review_that_burns_its_budget_on_reasoning_retries_low_and_can_pass(progressive_gateway):
    state = progressive_gateway
    state.empty_stage = "plan_review"
    state.empty_finish = "length"
    plan = state.run()
    reviews = [item for item in state.contexts if item["stage"] == "plan_review"]
    assert len(reviews) == 2
    assert "上一轮没有返回正文" in json.dumps(reviews[1], ensure_ascii=False)
    assert "contentless_retried" not in reviews[1]
    assert plan["plan_review"]["ready"] is True


def test_incomplete_review_record_returns_the_compiled_plan(progressive_gateway):
    state = progressive_gateway

    def incomplete(value):
        value.clear()
        value["ready"] = True

    state.first_review_mutation = state.second_review_mutation = state.third_review_mutation = incomplete
    plan = state.run()
    assert plan["segments"]
    assert plan.get("plan_review", {}).get("ready") is not True
    assert "看这里" not in "".join(beat.get("narration") or "" for beat in plan["ad_script"]["beats"])


def test_inner_voice_is_already_rewritten_when_the_plan_is_reviewed(progressive_gateway):
    state = progressive_gateway
    raw = state.strategy.model_dump()
    raw["brief"]["ad_form"] = "scene_story"
    state.strategy = _lock_speech(StrategyRecord.model_validate(raw), "character_dialogue")

    def alone(value):
        for shot in value["shots"]:
            shot["description"] = "她一个人坐着，开口说第一句。"
            shot["character_ids"] = ["char_worker"]

    state.first_director_mutation = alone
    plan = state.run()
    review = next(item for item in state.contexts if item.get("stage") == "plan_review")
    text = review["final_plan"]["segments"][0]["shots"][0]["description"]
    assert "开口说" not in text
    assert "心里想" in text and "嘴没有动" in text
    assert plan["segments"][0]["shots"][0]["description"] == text
    assert plan["plan_review"]["ready"] is True
    assert plan["plan_review"]["reviewed_plan_sha256"] == plan_review_fingerprint(plan)


def test_review_json_with_an_extra_brace_is_accepted_on_the_first_call(progressive_gateway):
    state = progressive_gateway
    state.review_suffix = "}"
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review"]
    assert plan["plan_review"]["ready"] is True


def test_review_with_no_content_keeps_the_compiled_plan(progressive_gateway):
    state = progressive_gateway
    state.empty_stage = "plan_review"
    state.empty_finish = "length"
    state.empty_forever = True
    plan = state.run()
    assert [item["stage"] for item in state.contexts].count("plan_review") == 2
    assert plan["segments"]
    assert plan.get("plan_review", {}).get("ready") is not True
    assert "看这里" not in "".join(beat.get("narration") or "" for beat in plan["ad_script"]["beats"])
