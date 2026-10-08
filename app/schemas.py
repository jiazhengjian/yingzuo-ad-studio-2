from typing import Literal
import re
import math
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator
from .strategy import DirectionCandidates, ProductBrief, StrategyRecord
from .ad_script import AdScript, ScriptReview, script_narration, validate_script
from .generation_policy import generation_groups, validate_generation_durations
from .directing import StateChange, VisualDirection, ShotContinuity, validate_directing_plan
from .production import ProductionPlan, validate_production_plan
from .ad_form import AdForm


def segment_durations(duration_seconds):
    if type(duration_seconds) is not int or not 10 <= duration_seconds <= 60:
        raise ValueError("视频时长须为10至60秒的整数")
    full, tail = divmod(duration_seconds, 10)
    return [10] * full + ([tail] if tail else [])


def script_character_limit(duration_seconds, characters_per_tenth=29):
    return duration_seconds * characters_per_tenth // 10


def shot_character_limit(start, end, characters_per_tenth=29):
    if characters_per_tenth == 29:
        return math.floor((end - start) * 2.9 + 1e-7)
    return math.floor((end - start) * (characters_per_tenth / 10) + 1e-7)


def is_custom_answer_option(label):
    """A typing instruction is not an answer; free text belongs in the composer."""
    return bool(re.search(r"自填|自行填写|自己填写|手动填写|自定义|我来补充|我来填写|由我填写|在.{0,8}输入框.{0,6}输入", str(label)))


class Shot(BaseModel):
    continuity: ShotContinuity | None = Field(default=None, exclude_if=lambda value: value is None, description="导演约定下的主体状态与切换依据，历史方案可缺省")
    id: str = Field(default="", max_length=80, description="片段内镜头ID；使用speech_spans时须非空且唯一")
    start: float = Field(ge=0, lt=30, description="当前片段内的局部开始秒数，范围0至30秒，不是整条广告的累计时间")
    end: float = Field(gt=0, le=30, description="当前生成段内的局部结束秒数，最大30秒")
    description: str = Field(min_length=1, max_length=1500)
    narration: str = Field(default="", max_length=360, description="在本镜头时段内完整说出的广告台词，表达商品特点或商品与顾客场景的关系；画面为表达提供支撑，不要求复述画面动作，可留空")
    purpose: str = Field(default="", max_length=300)
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    beat_id: str = Field(default="", max_length=80, description="对应已审查广告脚本的内容节拍ID；同一节拍可有连续多个镜头，新方案完整台词由speech_spans独立编排")
    beat_ids: list[str] | None = Field(default=None, min_length=1, max_length=12, exclude_if=lambda value: value is None, description="本镜头实际覆盖的相邻内容节拍，按顺序列出；一镜可跨多个节拍，beat_id保留为首项；历史镜头可缺省")
    character_ids: list[str] = Field(default_factory=list, max_length=8, exclude_if=lambda value: not value, description="实际出现在本镜的已确定角色ID；无人画面为空")
    product_visible: bool | None = Field(default=None, exclude_if=lambda value: value is None,
        description="本镜商品是否在画面里；讲解时拿在手里或放在身前也算。历史镜头可缺省")
    visual_motif: str = Field(default="", max_length=16, exclude_if=lambda value: not value,
        description="本镜承接的一个贯穿元素。历史镜头可缺省")
    match_cut: Literal["motion", "color", "scale"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="与上一镜的轻匹配：motion动作、color颜色、scale尺度。历史镜头可缺省")

    @model_validator(mode="after")
    def beat_membership(self):
        if self.beat_ids is not None and (any(not beat.strip() or len(beat) > 80 for beat in self.beat_ids)
                or len(set(self.beat_ids)) != len(self.beat_ids) or self.beat_id != self.beat_ids[0]):
            raise ValueError("镜头内容节拍须非空且不重复，beat_id须保留为beat_ids首项")
        return self


def shot_beat_ids(shot):
    """Read new multi-beat membership without changing historical serialization."""
    if isinstance(shot, dict):
        return shot.get("beat_ids") or [shot.get("beat_id", "")]
    return shot.beat_ids or [shot.beat_id]


def shot_beat_sequence(shots):
    """Join ordered, overlapping coverage, not one shot per content beat."""
    result = []
    for shot in shots:
        ids = shot_beat_ids(shot)
        overlap = next((size for size in range(min(len(result), len(ids)), 0, -1)
                        if result[-size:] == ids[:size]), 0)
        remaining = ids[overlap:]
        if any(beat_id in result for beat_id in remaining):
            raise ValueError("分镜须按顺序完整呈现广告脚本，不能遗漏、倒序或反复拼接")
        result.extend(remaining)
    return result


class SpeechAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=360, description="整句中连续的一部分；按顺序无遗漏拼成speech span的完整text")
    start: float = Field(ge=0, lt=30, allow_inf_nan=False)
    end: float = Field(gt=0, le=30, allow_inf_nan=False)
    shot_ids: list[str] = Field(min_length=1, max_length=18)
    visual_evidence: str = Field(min_length=1, max_length=1000, description="这些镜头中具体什么画面支撑这部分表达；仅为计划说明，不表示成片已验证")

    @model_validator(mode="after")
    def meaningful_anchor(self):
        if not self.text.strip() or not self.visual_evidence.strip() or self.end <= self.start:
            raise ValueError("台词锚点须包含非空文字、具体画面依据和有效时段")
        if round(self.end * 24) <= round(self.start * 24):
            raise ValueError("台词短语时段至少需要一个有效视频帧")
        return self


class SpeechSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    beat_id: str = Field(min_length=1, max_length=80)
    speaker_id: str = Field(default="", max_length=80, exclude_if=lambda value: not value, description="角色对白的已确定说话人ID；画外音与历史方案为空")
    text: str = Field(min_length=1, max_length=360, description="在当前生成段内完整说出的已审查整句，允许跨同一内容节拍的连续镜头")
    start: float = Field(ge=0, lt=30, allow_inf_nan=False)
    end: float = Field(gt=0, le=30, allow_inf_nan=False)
    shot_ids: list[str] = Field(min_length=1, max_length=18)
    anchors: list[SpeechAnchor] = Field(min_length=1, max_length=12)
    merchant_insisted: bool | None = Field(default=None, exclude_if=lambda value: not value,
        description="商家坚持保留这句原文。只豁免这一句，其余内容仍过硬门。")

    @model_validator(mode="after")
    def complete_speech(self):
        if not self.id.strip() or not self.beat_id.strip() or not self.text.strip() or self.end <= self.start:
            raise ValueError("旁白时段须包含非空ID、完整台词和有效时段；静音画面使用镜头表示")
        from .line_quality import ends_unfinished
        if ends_unfinished(self.text):
            raise ValueError("旁白时段须保留完整表达，不能以未完标点结束")
        from .ad_form_playbook import active_speech_tenth
        if len(self.text) > shot_character_limit(self.start, self.end, active_speech_tenth()):
            raise ValueError("旁白台词超过对应朗读时段的长度，请精简或重新安排时段")
        if "".join(anchor.text for anchor in self.anchors) != self.text:
            raise ValueError("台词锚点须按顺序完整拼接旁白整句，不得遗漏、重复或改写")
        previous = self.start
        for anchor in self.anchors:
            if anchor.start < previous - 1e-7 or anchor.end > self.end + 1e-7:
                raise ValueError("台词锚点须按顺序排列且不重叠，并位于旁白时段内")
            previous = anchor.end
        return self


def _speech_shots(shot_ids, start, end, beat_id, shots):
    """Validate planned references, not semantic support or generated AV."""
    positions = {shot.id: index for index, shot in enumerate(shots)}
    if len(set(shot_ids)) != len(shot_ids) or any(shot_id not in positions for shot_id in shot_ids):
        raise ValueError("旁白及锚点只能引用本段存在且不重复的镜头ID")
    indices = [positions[shot_id] for shot_id in shot_ids]
    if indices != list(range(indices[0], indices[-1] + 1)):
        raise ValueError("旁白及锚点引用的镜头须按顺序连续排列")
    selected = [shots[index] for index in indices]
    if any(beat_id not in shot_beat_ids(shot) for shot in selected):
        raise ValueError("旁白及锚点只能跨同一内容节拍的镜头")
    if any(shot.end <= start or shot.start >= end for shot in selected):
        raise ValueError("旁白及锚点时段须与每个引用镜头的时段重叠")
    if start < selected[0].start - 1e-7 or end > selected[-1].end + 1e-7:
        raise ValueError("旁白及锚点时段须由对应镜头覆盖")


class Segment(BaseModel):
    production: ProductionPlan | None = Field(default=None, exclude_if=lambda value: value is None)
    reference_roles: list[dict] | None = Field(default=None, exclude_if=lambda value: value is None, description="服务端提供的实际参考素材角色映射，不由分镜模型编写")
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,48}$")
    duration_seconds: int = Field(ge=1, le=30, strict=True, description="该段用于成片的整数秒数；新连续性策略2至30秒，历史策略1至10秒由Plan校验")
    purpose: str = Field(min_length=1, max_length=300)
    prompt: str = Field(min_length=1, max_length=8000)
    narration: str = Field(default="", max_length=360, description="本段实际要说的中文台词，禁止夹入镜头说明；短尾段可为空以保留自然收束")
    shots: list[Shot] = Field(min_length=1, max_length=18, description="使用当前生成段的局部0至duration_seconds秒时间轴，最大30秒；从0连续覆盖本段完整时长，不使用整条广告的累计时间；镜头切换不等于独立生成")
    speech_spans: list[SpeechSpan] | None = Field(default=None, max_length=12, description="独立于镜头切换的完整旁白时段；None保留历史镜头台词，空列表表示静音段")
    reference_asset_ids: list[str] = Field(min_length=1, max_length=10)
    speech_characters_per_tenth: int | None = Field(default=None, exclude_if=lambda value: value is None,
        description="服务端按方案广告类型写入的朗读容量。缺省时按历史2.9字/秒。不由分镜模型填写")

    @field_validator("speech_characters_per_tenth", mode="before")
    @classmethod
    def server_speech_rate(cls, value):
        if value is None:
            return None
        from .ad_form_playbook import LEGACY_TENTH, accepted_submission_tenth
        tenth = accepted_submission_tenth(value)
        return None if tenth == LEGACY_TENTH else tenth

    @model_validator(mode="wrap")
    @classmethod
    def bind_segment_speech_budget(cls, data, handler):
        """Use the rate stored on this segment when no plan validator has bound one."""
        from .ad_form_playbook import (
            accepted_submission_tenth, bind_speech_tenth, reset_plan_speech_tenth, speech_rate_is_bound,
        )
        token = None
        if not speech_rate_is_bound():
            raw = data.get("speech_characters_per_tenth") if isinstance(data, dict) else getattr(data, "speech_characters_per_tenth", None)
            if raw is not None:
                token = bind_speech_tenth(accepted_submission_tenth(raw))
        try:
            return handler(data)
        finally:
            if token is not None:
                reset_plan_speech_tenth(token)

    @model_validator(mode="after")
    def shot_order(self):
        previous = 0
        for shot in self.shots:
            if abs(shot.start - previous) > .05 or shot.end <= shot.start or shot.end > self.duration_seconds:
                raise ValueError("镜头时段必须从0连续排列，且不超过片段时长")
            previous = shot.end
        if abs(previous - self.duration_seconds) > .05:
            raise ValueError("镜头计划须覆盖完整片段")
        if self.speech_spans is not None:
            if any(not shot.id.strip() for shot in self.shots) or len({shot.id for shot in self.shots}) != len(self.shots):
                raise ValueError("独立旁白方案的镜头ID须非空且在片段内唯一")
            if any(shot.narration for shot in self.shots):
                raise ValueError("独立旁白方案的镜头台词须为空，完整台词只保留在speech_spans")
            if len({span.id for span in self.speech_spans}) != len(self.speech_spans):
                raise ValueError("旁白时段ID在片段内不能重复")
            if len({span.beat_id for span in self.speech_spans}) != len(self.speech_spans):
                raise ValueError("每个内容节拍的完整台词只能有一个旁白时段")
            if self.narration != "".join(span.text for span in self.speech_spans):
                raise ValueError("每段台词须由旁白时段的完整文字按顺序拼接")
            from .ad_form_playbook import speech_ceiling_tenth
            if len(self.narration) > script_character_limit(self.duration_seconds, speech_ceiling_tenth()):
                raise ValueError("单段台词超过该段的朗读长度，请精简后确认")
            previous = 0
            for span in self.speech_spans:
                if span.start < previous - 1e-7 or span.end > self.duration_seconds:
                    raise ValueError("旁白时段须在片段内按顺序排列且不重叠，完整句不能跨生成段")
                previous = span.end
                _speech_shots(span.shot_ids, span.start, span.end, span.beat_id, self.shots)
                for anchor in span.anchors:
                    if not set(anchor.shot_ids).issubset(span.shot_ids):
                        raise ValueError("台词锚点只能引用其旁白时段对应的镜头")
                    _speech_shots(anchor.shot_ids, anchor.start, anchor.end, span.beat_id, self.shots)
        return self


class Facts(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    claims: list[str] = Field(default_factory=list, max_length=12)


def _bound_skill_trace(value):
    from .planner_recovery import SKILL_TRACE_LIMIT, bound_server_list
    if value is None or not isinstance(value, list):
        return value
    return bound_server_list(value, SKILL_TRACE_LIMIT)


class Plan(BaseModel):
    production: ProductionPlan | None = Field(default=None, exclude_if=lambda value: value is None)
    skill_trace: list[dict] | None = Field(default=None, max_length=30, exclude_if=lambda value: value is None)
    alignment_review: dict | None = Field(default=None, exclude_if=lambda value: value is None)
    plan_review: dict | None = Field(default=None, exclude_if=lambda value: value is None,
        description="最终方案的综合审核及服务端内容绑定，历史方案可缺省")
    review_status: Literal["passed", "repaired", "not_ready", "conservative_fallback"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="终审落点。passed 是一次通过；repaired 是调整后再审通过；not_ready 是还不能确认、也不能出图，包括硬门没过，或综合检查没有做完；conservative_fallback 是综合检查已经做完但仍有未通过项，硬门已经通过，只记在后台。没有完整审核结果的方案不能确认，也不能出图。")
    merchant_locked_lines: list[str] = Field(default_factory=list, max_length=12, exclude_if=lambda value: not value,
        description="商家指定后必须原样保留的台词。系统可以重排时间和其余句子，不能改写这些句子。")
    merchant_insisted_lines: list[str] = Field(default_factory=list, max_length=12, exclude_if=lambda value: not value,
        description="商家明知碰到硬门仍坚持保留的台词。豁免只覆盖这些句子。")
    review_status_note: str | None = Field(default=None, max_length=200, exclude_if=lambda value: value is None,
        description="终审落点的一句后台说明，不展示给商家。综合检查没通过时列出没通过的项；检查没有做完时写没有完整的综合检查。")
    auto_release: dict | None = Field(default=None, exclude_if=lambda value: value is None,
        description="综合检查仍未通过时的自动放行记录，不展示给商家。含 note、failed_checks、repair_rounds、ad_form、project_id。")
    visual_direction: VisualDirection | None = Field(default=None, exclude_if=lambda value: value is None, description="统一视觉约定，供场景图和全部镜头复用，不新增商家表单")
    generation_strategy: Literal["legacy_10s", "continuity_v1"] = Field(default="legacy_10s", exclude_if=lambda value: value == "legacy_10s", description="服务端决定的生成策略，旧任务保持原10秒回执，新任务按连续性分组")
    title: str = Field(min_length=1, max_length=100)
    script: str = Field(min_length=0, max_length=360)
    language: Literal["zh-CN"] = "zh-CN"
    aspect_ratio: Literal["9:16"] = "9:16"
    segments: list[Segment] = Field(min_length=1, max_length=6)
    facts: Facts
    audio_strategy: Literal["native", "voiceover"] = "voiceover"
    strategy: StrategyRecord | None = None
    ad_script: AdScript | None = None
    script_review: ScriptReview | None = None
    video_flow: str = Field(default="", max_length=400, exclude_if=lambda value: not value,
        description="整条成片的观看顺序，供分镜对照；历史方案可缺省")
    state_changes: list[StateChange] = Field(default_factory=list, max_length=8, exclude_if=lambda value: not value,
        description="文字锁不住的可见外观变化。代码只按这份列表做状态参考，不从镜头正文猜测。历史方案可缺省")

    @field_validator("skill_trace", mode="before")
    @classmethod
    def cap_skill_trace(cls, value):
        return _bound_skill_trace(value)

    @model_validator(mode="wrap")
    @classmethod
    def bind_form_speech_budget(cls, data, handler):
        """Let nested speech spans use this plan's rate, then restore the caller."""
        from .ad_form_playbook import bind_plan_speech_tenth, reset_plan_speech_tenth
        strategy = data.get("strategy") if isinstance(data, dict) else getattr(data, "strategy", None)
        token = bind_plan_speech_tenth(strategy)
        try:
            return handler(data)
        finally:
            reset_plan_speech_tenth(token)

    @model_validator(mode="after")
    def unique_segments(self, info: ValidationInfo):
        if not self.script and not (self.production is not None and self.production.speech_mode == "none"):
            raise ValueError("有声广告方案须保留完整台词；仅明确无台词的制作约定允许空文案")
        validate_directing_plan(self, required=(info.context or {}).get("require_directing_contract", False))
        if (info.context or {}).get("require_beat_ids") and any(shot.beat_ids is None for segment in self.segments for shot in segment.shots):
            raise ValueError("新分镜须明确每镜覆盖的内容节拍beat_ids，不按内容节拍强制切镜")
        if any(segment.speech_spans is not None for segment in self.segments) and self.audio_strategy != "native":
            raise ValueError("独立旁白时段只适用于原生声音方案")
        speech_beats = [span.beat_id for segment in self.segments for span in (segment.speech_spans or [])]
        if len(speech_beats) != len(set(speech_beats)):
            raise ValueError("每个内容节拍的整句只能出现在一个生成段的一个旁白时段中")
        if self.ad_script is not None:
            if self.strategy is None or self.audio_strategy != "native":
                raise ValueError("广告内容脚本需要商品策划依据与原生声音方案")
            self.ad_script = validate_script(self.ad_script, self.strategy,
                                            sum(s.duration_seconds for s in self.segments),
                                            require_current_version=False, generation_strategy=self.generation_strategy)
            beats = {beat.id: beat for beat in self.ad_script.beats}
            order = shot_beat_sequence([shot for segment in self.segments for shot in segment.shots])
            spoken = {beat_id: [] for beat_id in beats}
            spoken_order = []
            beat_evidence = {beat_id: set() for beat_id in beats}
            for segment in self.segments:
                for shot in segment.shots:
                    ids = shot_beat_ids(shot)
                    if any(beat_id not in beats for beat_id in ids):
                        raise ValueError("镜头必须对应已确定的广告内容节拍")
                    positions = [list(beats).index(beat_id) for beat_id in ids]
                    if positions != list(range(positions[0], positions[-1] + 1)):
                        raise ValueError("同一镜头只能覆盖相邻且顺序一致的内容节拍")
                    if any(beats[beat_id].boundary_before == "scene_change" for beat_id in ids[1:]):
                        raise ValueError("同一镜头不能跨越脚本明确的场景转换")
                    if segment.speech_spans is None and shot.narration:
                        if len(ids) > 1:
                            raise ValueError("跨内容节拍的镜头须使用独立旁白时段")
                        spoken[shot.beat_id].append(shot.narration)
                        spoken_order.append(shot.beat_id)
                    for beat_id in ids:
                        beat_evidence[beat_id].update(shot.evidence_ids)
                for span in segment.speech_spans or []:
                    if span.beat_id not in beats:
                        raise ValueError("旁白必须对应已确定的广告内容节拍")
                    spoken[span.beat_id].append(span.text)
                    spoken_order.append(span.beat_id)
            if order != list(beats):
                raise ValueError("分镜须按顺序完整呈现广告脚本，不能遗漏、倒序或反复拼接")
            for beat_id, beat in beats.items():
                expected = [beat.narration] if beat.narration else []
                if spoken[beat_id] != expected:
                    if any(segment.speech_spans is not None for segment in self.segments):
                        raise ValueError("每个内容节拍的完整台词须在一个生成段的一个旁白时段中说出，不能改写、重复或拆成碎句")
                    raise ValueError("每个内容节拍的完整台词须在一个镜头中说出，不能改写或拆成碎句")
                if not set(beat.evidence_ids).issubset(beat_evidence[beat_id]):
                    raise ValueError("广告内容依据须落实到对应节拍的镜头")
            if spoken_order != [beat.id for beat in self.ad_script.beats if beat.narration]:
                raise ValueError("旁白须按广告内容脚本的顺序完整说出")
            if self.script != script_narration(self.ad_script):
                raise ValueError("最终台词须与广告内容脚本一致")
        if self.script_review is not None:
            if self.ad_script is None or self.script_review.script != self.ad_script or not self.script_review.ready:
                raise ValueError("脚本审查记录必须对应当前定稿；修改后须清除旧审查记录")
        if self.strategy is not None:
            facts = {fact.id for fact in self.strategy.brief.facts}
            selected = next(d for d in self.strategy.directions if d.id == self.strategy.selected_direction_id)
            used = set()
            for segment in self.segments:
                for shot in segment.shots:
                    if not shot.purpose.strip() or not set(shot.evidence_ids).issubset(facts):
                        raise ValueError("镜头必须说明信息作用，且只能引用已有商品事实")
                    used.update(shot.evidence_ids)
            if not set(selected.evidence_ids).issubset(used):
                raise ValueError("选中创意的事实依据必须落实到具体镜头")
            if self.facts.name != self.strategy.brief.product_name or self.facts.claims != [f.text for f in self.strategy.brief.facts]:
                raise ValueError("方案商品事实必须与本轮策划依据一致")
        if len({s.id for s in self.segments}) != len(self.segments):
            raise ValueError("片段ID重复")
        from .ad_form_playbook import LEGACY_TENTH, speech_profile_for_strategy
        tenth = speech_profile_for_strategy(self.strategy).characters_per_tenth
        for segment in self.segments:
            segment.speech_characters_per_tenth = None if tenth == LEGACY_TENTH else tenth
        durations = [s.duration_seconds for s in self.segments]
        validate_generation_durations(durations, self.generation_strategy)
        if self.generation_strategy == "legacy_10s":
            if any(len(segment.shots) > 6 or len(segment.speech_spans or []) > 6 for segment in self.segments):
                raise ValueError("历史10秒方案每段最多6个镜头和旁白时段")
        elif self.ad_script is not None:
            grouping = generation_groups(self.ad_script, sum(durations), self.generation_strategy,
                                         characters_per_tenth=tenth)
            actual_groups = [shot_beat_sequence(segment.shots) for segment in self.segments]
            expected_groups = [[self.ad_script.beats[index].id for index in group]
                               for group in grouping["beat_groups"]]
            if durations != grouping["durations"] or actual_groups != expected_groups:
                raise ValueError("实际生成段必须保留已审脚本的连续性分组，不能切开连续动作或移动完整表达边界")
        if not (info.context or {}).get("allow_script_repair") and len(self.script) > script_character_limit(sum(durations), tenth):
            raise ValueError("文案超过所选时长的朗读长度，请精简后确认")
        if self.audio_strategy == "native" and not (info.context or {}).get("allow_script_repair"):
            if re.sub(r"\s", "", self.script) != re.sub(r"\s", "", "".join(s.narration for s in self.segments)):
                raise ValueError("整条文案必须与逐段台词一致")
            if any(len(s.narration) > script_character_limit(s.duration_seconds, tenth) for s in self.segments):
                raise ValueError("单段台词超过该段的朗读长度，请精简后确认")
            # Historical native drafts remain readable. New provider plans opt
            # into this contract, and any shot-bound edit must keep it intact.
            bound = (info.context or {}).get("require_shot_narration") or any(shot.narration for segment in self.segments for shot in segment.shots)
            if bound:
                for segment in self.segments:
                    if segment.speech_spans is not None:
                        continue
                    if re.sub(r"\s", "", segment.narration) != re.sub(r"\s", "", "".join(shot.narration for shot in segment.shots)):
                        raise ValueError("每段台词必须与对应镜头台词一致")
                    if any(len(shot.narration) > shot_character_limit(shot.start, shot.end, tenth) for shot in segment.shots):
                        raise ValueError("镜头台词超过对应画面时长，请精简或重新安排镜头")
        validate_production_plan(self)
        if self.plan_review is not None:
            # A model response must first pass validate_plan_review, which binds
            # the reviewed content on the server. Never auto-bind an attached
            # record here: that would silently reuse it after a content edit.
            # A record that is not a review at all is dropped. A hash mismatch
            # with the current plan still rejects the plan.
            from .plan_review import review_record_incomplete, validate_plan_review
            if review_record_incomplete(self.plan_review):
                self.plan_review = None
            else:
                if self.plan_review.get("reviewed_plan_sha256") is None:
                    raise ValueError("综合审核记录须先由服务端绑定最终方案")
                self.plan_review = validate_plan_review(self.plan_review, self).model_dump(mode="json")
        return self


class NewProject(BaseModel):
    ad_form: AdForm | None = None
    brief: str = Field(min_length=2, max_length=6000)
    title: str | None = Field(default=None, max_length=100)
    route: Literal["product", "presenter"] = "product"
    duration_seconds: int = Field(default=20, ge=10, le=60, strict=True)


class ProjectTitleInput(BaseModel):
    title: str = Field(min_length=1, max_length=80)

    @field_validator("title", mode="before")
    @classmethod
    def trim_title(cls, value):
        return value.strip() if isinstance(value, str) else value


class MessageInput(BaseModel):
    content: str = Field(min_length=1, max_length=6000)
    asset_id: str | None = None


class PlanInput(BaseModel):
    plan: Plan
    accepted_revision: int = Field(ge=1)


class GenerateInput(BaseModel):
    accepted_revision: int = Field(ge=1)
    pause_for_creative: bool = True


class ConversationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QuestionOption(ConversationModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,48}$")
    label: str = Field(min_length=1, max_length=160)


class Question(ConversationModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,48}$")
    prompt: str = Field(min_length=1, max_length=500)
    options: list[QuestionOption] = Field(default_factory=list, max_length=5, description="实际答案选项；不得添加自填、其他或让用户转到输入框的伪选项")
    allow_custom: bool = True
    allow_skip: bool = True

    @field_validator("options", mode="before")
    @classmethod
    def actual_answers_only(cls, value):
        if isinstance(value, list):
            return [option for option in value if not is_custom_answer_option(option.get("label", "") if isinstance(option, dict) else getattr(option, "label", ""))]
        return value

    @model_validator(mode="after")
    def unique_options(self):
        if len({o.id for o in self.options}) != len(self.options):
            raise ValueError("选项ID不能重复")
        if not self.options and not self.allow_custom and not self.allow_skip:
            raise ValueError("问题必须允许选择、自由回答或跳过")
        return self


class Answer(ConversationModel):
    question_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,48}$")
    option_id: str | None = Field(default=None, max_length=48)
    text: str | None = Field(default=None, max_length=500)
    skipped: bool = False

    @model_validator(mode="after")
    def one_answer(self):
        if sum((bool(self.option_id), bool(self.text and self.text.strip()), self.skipped)) != 1:
            raise ValueError("每个问题只能选择、填写或跳过其中一种")
        if self.text is not None:
            self.text = self.text.strip()
        return self


class CreativeOption(ConversationModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,48}$")
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=1000)
    segment: Segment


class AgentDecision(ConversationModel):
    kind: Literal["clarify", "plan", "reply", "directions"]
    message: str = Field(min_length=1, max_length=3000)
    questions: list[Question] = Field(default_factory=list, max_length=4)
    plan: Plan | None = None
    creative_options: list[CreativeOption] = Field(default_factory=list, max_length=2)
    planning_brief: ProductBrief | None = None
    direction_candidates: DirectionCandidates | None = Field(default=None, exclude_if=lambda value: value is None)
    skill_trace: list[dict] | None = Field(default=None, max_length=30, exclude_if=lambda value: value is None)

    @field_validator("skill_trace", mode="before")
    @classmethod
    def cap_skill_trace(cls, value):
        return _bound_skill_trace(value)

    @model_validator(mode="after")
    def decision_shape(self, info: ValidationInfo):
        if self.kind == "directions":
            if self.direction_candidates is None or self.plan is not None or self.questions or self.creative_options:
                raise ValueError("方向选择只包含三个未选定方向，不预先创建脚本或媒体")
        elif self.direction_candidates is not None:
            raise ValueError("方向候选只能用于方向选择阶段")
        elif self.kind == "clarify":
            if not self.questions or self.plan is not None or self.creative_options:
                raise ValueError("澄清只包含问题")
        elif self.kind == "plan":
            if self.plan is None or self.questions:
                raise ValueError("方案必须包含完整计划且不同时提问")
            if self.plan.strategy is not None and self.creative_options:
                raise ValueError("策略方案已包含创意方向，不再附带固定台词的开场备选")
            if self.plan.strategy is None and len(self.creative_options) != 2:
                raise ValueError("方案必须包含完整计划和两个创意备选")
            if any(o.id == "current" or o.segment.id != self.plan.segments[0].id or o.segment.duration_seconds != 10 for o in self.creative_options):
                raise ValueError("创意备选必须对应首段且不能使用current标识")
            if self.plan.audio_strategy == "native" and not (info.context or {}).get("allow_script_repair") and any(o.segment.narration != self.plan.segments[0].narration for o in self.creative_options):
                raise ValueError("开场备选需保留首段相同台词")
            first_spans = self.plan.segments[0].speech_spans
            if self.plan.audio_strategy == "native" and first_spans is not None:
                def speech_timing(spans):
                    return [(span.beat_id, span.text, span.start, span.end, span.shot_ids,
                             [(anchor.text, anchor.start, anchor.end, anchor.shot_ids) for anchor in span.anchors])
                            for span in spans]
                for option in self.creative_options:
                    if option.segment.speech_spans is None or speech_timing(option.segment.speech_spans) != speech_timing(first_spans):
                        raise ValueError("开场备选需保留首段完整旁白、时段与镜头对应关系")
            elif self.plan.audio_strategy == "native" and ((info.context or {}).get("require_shot_narration") or any(s.narration for s in self.plan.segments[0].shots)):
                first = self.plan.segments[0].shots
                for option in self.creative_options:
                    if [(s.start, s.end) for s in option.segment.shots] != [(s.start, s.end) for s in first]:
                        raise ValueError("开场备选需保留首段镜头时段，避免台词错位")
                    if not (info.context or {}).get("allow_script_repair") and [s.narration for s in option.segment.shots] != [s.narration for s in first]:
                        raise ValueError("开场备选需保留每个镜头的对应台词")
        elif self.plan is not None or self.questions or self.creative_options:
            raise ValueError("普通答复不包含问题或方案")
        if len({q.id for q in self.questions}) != len(self.questions):
            raise ValueError("问题ID不能重复")
        if len({o.id for o in self.creative_options}) != len(self.creative_options):
            raise ValueError("创意ID不能重复")
        return self


class TurnInput(ConversationModel):
    ad_form: AdForm | None = Field(default=None, description="明确选择或修改的广告倾向；省略/null保留当前偏好，不代表制作路线")
    client_turn_id: str = Field(min_length=1, max_length=100)
    expected_context_version: int = Field(ge=0)
    content: str = Field(default="", max_length=6000)
    reference_asset_ids: list[str] = Field(default_factory=list, max_length=10)
    reply_to_gate_id: str | None = Field(default=None, max_length=100)
    answers: list[Answer] = Field(default_factory=list, max_length=4)


class CreativeConfirmInput(ConversationModel):
    gate_id: str = Field(min_length=1, max_length=100)
    accepted_revision: int = Field(ge=1)
    option_id: str = Field(min_length=1, max_length=48)
    note: str = Field(default="", max_length=1000)
    client_turn_id: str = Field(min_length=1, max_length=100)


class DirectionSelectInput(ConversationModel):
    client_turn_id: str = Field(min_length=1, max_length=100)
    gate_id: str = Field(min_length=1, max_length=100)
    direction_id: str = Field(min_length=1, max_length=160)
    expected_context_version: int = Field(ge=0, strict=True)
    accepted_revision: int = Field(ge=0, strict=True)


class RetryInput(BaseModel):
    job_id: str
    retry_failed_segments: bool = Field(default=False, strict=True)


class CredentialsInput(BaseModel):
    deepseek_api_key: str = Field(default="", max_length=512)
    tudou_api_key: str = Field(default="", max_length=512)


class ContinuityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepted_revision: int = Field(ge=1, strict=True)
    source_job_id: str = Field(min_length=1, max_length=100)
    request_id: str = Field(min_length=1, max_length=120)
