"""Only new draft enum typos reach the existing review, never final planning."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.providers.gateway import Gateway, ProviderError, reviewable_draft_enum_issues
from app.strategy import build_strategy, select_rules
from ad_script_fixtures import CRITERIA, execution_check, with_action_timing
from test_ad_script import script, strategy
from test_direction_selection import candidates_for, compiled_from_reservations, directed_review
from test_providers import settings, source, strategy_stage, install_transport, chat_output, request_context


@pytest.mark.parametrize("field", ["boundary_before", "time_transition", "both"])
def test_only_known_enum_paths_are_reported_and_original_draft_is_unchanged(script, strategy, field):
    raw = with_action_timing(script)
    if field in ("boundary_before", "both"): raw["beats"][0]["boundary_before"] = "ellipsis"
    if field in ("time_transition", "both"): raw["beats"][1]["timing"]["time_transition"] = "complete"
    before = deepcopy(raw)
    issues = reviewable_draft_enum_issues(raw, strategy, 20, "continuity_v1")
    assert raw == before
    assert len(issues) == (2 if field == "both" else 1)
    assert all(set(issue) == {"loc", "type", "allowed_values"} and issue["type"] == "literal_error" for issue in issues)
    if field in ("boundary_before", "both"):
        assert issues[0] == {"loc": ["beats", 0, "boundary_before"], "type": "literal_error",
                             "allowed_values": ["continuous", "complete", "scene_change"]}
    if field in ("time_transition", "both"):
        assert issues[-1]["loc"] == ["beats", 1, "timing", "time_transition"]


@pytest.mark.parametrize("invalid", ["later_extra", "root_extra", "unknown_fact", "duplicate_fact",
    "duplicate_beat", "missing_selected", "old_version", "missing_timing", "timing_shape",
    "empty_enum", "nonstring_enum", "oversized_enum", "long_line"])
def test_first_enum_error_cannot_hide_other_shape_or_authority_failures(script, strategy, invalid):
    raw = with_action_timing(script)
    raw["beats"][0]["boundary_before"] = "ellipsis"
    if invalid == "later_extra": raw["beats"][1]["invented_field"] = "unsupported"
    elif invalid == "root_extra": raw["production"] = {"speech_mode": "none"}
    elif invalid == "unknown_fact": raw["beats"][1]["evidence_ids"] = ["invented"]
    elif invalid == "duplicate_fact": raw["beats"][0]["evidence_ids"] = ["layers", "layers"]
    elif invalid == "duplicate_beat": raw["beats"][1]["id"] = raw["beats"][0]["id"]
    elif invalid == "missing_selected":
        for beat in raw["beats"]: beat["evidence_ids"] = []
    elif invalid == "old_version": raw["skill_version"] = "historical-version"
    elif invalid == "missing_timing": raw["beats"][1].pop("timing")
    elif invalid == "timing_shape": raw["beats"][1]["timing"]["visual_seconds"] = "lots"
    elif invalid == "empty_enum": raw["beats"][0]["boundary_before"] = " "
    elif invalid == "nonstring_enum": raw["beats"][0]["boundary_before"] = {"type": "ellipsis"}
    elif invalid == "oversized_enum": raw["beats"][0]["boundary_before"] = "x" * 81
    else: raw["beats"][1]["narration"] = "字" * 30
    before = deepcopy(raw)
    try:
        issues = reviewable_draft_enum_issues(raw, strategy, 20, "continuity_v1")
    except ValueError:
        issues = []
    assert issues == []
    assert raw == before


@pytest.fixture
def enum_gateway(settings, source, strategy_stage, install_transport, tmp_path):
    settings.data_root = tmp_path / "data"
    settings.direction_selection_enabled = True
    settings.planning_generation_strategy = "continuity_v1"
    candidates = candidates_for((None, None, None, source))
    selected = build_strategy(candidates.brief, candidates.directions, "direction_1", select_rules(None), merchant_choice=True)
    state = SimpleNamespace(calls=[], contexts=[], raw=None, compiled=None, new_flow=True,
                            initial_mutation=None, review_mutation=None, enum_field="boundary_before",
                            selected_strategy=selected, merchant_brief="蓝瓶商品")

    def handle(request):
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        _, context = request_context(request)
        stage = context["stage"]
        state.calls.append(stage)
        state.contexts.append(deepcopy(context))
        if stage == "strategy":
            assert not state.new_flow
            return chat_output(strategy_stage)
        if stage == "ad_script":
            evidence = context["selected_direction"]["evidence_ids"]
            raw = {"core_message": context["selected_direction"]["angle"], "audience_takeaway": "了解桌面与商品外观的关系",
                "product_role": "商品是主体", "skill_version": context["runtime_skill"]["version"],
                "beats": [{"id": "appearance", "purpose": "建立商品与办公桌的搭配", "message": "商品外观的选择",
                    "visual_role": "同一瓶子静置在办公桌上", "narration": "给桌面一点喜欢的样子。", "evidence_ids": evidence}]}
            raw = with_action_timing(raw)
            if state.enum_field == "boundary_before": raw["beats"][0]["boundary_before"] = "ellipsis"
            elif state.enum_field == "time_transition": raw["beats"][0]["timing"]["time_transition"] = "complete"
            if not state.new_flow: raw["evidence_ids"] = evidence
            if state.initial_mutation: state.initial_mutation(raw)
            state.raw = deepcopy(raw)
            return chat_output(raw)
        if stage == "script_review":
            raw = deepcopy(context["draft_script"])
            if "draft_validation_issues" in context:
                assert raw == {key: value for key, value in state.raw.items()
                               if key not in ("evidence_ids", "skill_version")}
                assert "schedule_feasible" not in context and "draft_beat_timing" not in context
                assert "draft_generation_groups" not in context
                assert context["draft_schedule_status"] == "unavailable_until_schema_repaired"
                assert context["draft_validation_issues"]
                # Explicit mocked review output fixes the enum; production
                # does not infer or apply this creative choice automatically.
                if state.enum_field == "boundary_before": raw["beats"][0]["boundary_before"] = "complete"
                else: raw["beats"][0]["timing"]["time_transition"] = "ellipsis"
            else:
                assert context["schedule_feasible"] is ("review_feedback" not in context)
                assert "draft_validation_issues" not in context
            if state.review_mutation: state.review_mutation(raw)
            return chat_output({"script": raw, "ready": True, "execution_check": execution_check(),
                "checks": [{"criterion": key, "passed": True, "note": "离线夹具已审核"} for key in CRITERIA]})
        if stage == "storyboard":
            state.compiled = compiled_from_reservations(context)
            return chat_output(state.compiled)
        assert stage == "alignment_review"
        return chat_output(directed_review(state.compiled))

    install_transport(handle)
    async def execute():
        gateway = Gateway(settings)
        project = {"brief": state.merchant_brief, "duration_seconds": 20}
        if state.new_flow:
            return (await gateway.decide(project, [source], [], [], None, selected_strategy=selected.model_dump()))["plan"]
        settings.direction_selection_enabled = False
        return await gateway.plan(project, [source], [])
    state.run = lambda: asyncio.run(execute())
    return state


@pytest.mark.parametrize("enum_field", ["boundary_before", "time_transition", None])
def test_existing_four_stages_review_original_draft_and_keep_final_contract_strict(enum_gateway, enum_field):
    enum_gateway.enum_field = enum_field
    result = enum_gateway.run()
    assert enum_gateway.calls == ["ad_script", "script_review", "storyboard", "alignment_review"]
    assert result["script_review"]["ready"]
    assert result["ad_script"] == result["script_review"]["script"]
    assert all(beat.get("timing") for beat in result["ad_script"]["beats"])
    if enum_field == "boundary_before": assert enum_gateway.raw["beats"][0]["boundary_before"] == "ellipsis"
    elif enum_field == "time_transition": assert enum_gateway.raw["beats"][0]["timing"]["time_transition"] == "complete"


@pytest.mark.parametrize("invalid", ["boundary", "time_transition", "missing_timing", "unknown_fact", "capacity"])
def test_review_output_cannot_use_draft_exception_or_drop_final_checks(enum_gateway, invalid):
    def mutate(raw):
        beat = raw["beats"][0]
        if invalid == "boundary": beat["boundary_before"] = "ellipsis"
        elif invalid == "time_transition": beat["timing"]["time_transition"] = "complete"
        elif invalid == "missing_timing": beat.pop("timing")
        elif invalid == "unknown_fact": beat["evidence_ids"] = ["invented"]
        elif invalid == "capacity": beat["timing"]["visual_seconds"] = 25
    enum_gateway.review_mutation = mutate
    with pytest.raises(ProviderError): enum_gateway.run()
    assert enum_gateway.calls == ["ad_script", "script_review"] + (["script_review"] if invalid == "capacity" else [])


@pytest.mark.parametrize("invalid", ["root_extra", "unknown_fact", "missing_timing", "extra_beat_field"])
def test_invalid_draft_authority_and_other_shape_errors_stop_before_review(enum_gateway, invalid):
    def mutate(raw):
        if invalid == "root_extra": raw["strategy"] = {"selected_direction_id": "unselected"}
        elif invalid == "unknown_fact": raw["beats"][0]["evidence_ids"] = ["invented"]
        elif invalid == "missing_timing": raw["beats"][0].pop("timing")
        else: raw["beats"][0]["secret_extra"] = "not allowed"
    enum_gateway.initial_mutation = mutate
    with pytest.raises(ProviderError): enum_gateway.run()
    assert enum_gateway.calls == ["ad_script"]


def test_legacy_initial_draft_keeps_its_existing_rejection_path(enum_gateway):
    enum_gateway.new_flow = False
    with pytest.raises(ProviderError): enum_gateway.run()
    assert enum_gateway.calls == ["strategy", "ad_script"]
