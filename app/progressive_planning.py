"""Compile a coordinated director draft, then review the actual final timeline.

No clocks are fixed before directing. Coverage weights express camera choices;
the existing allocator still owns content/speech windows and provider grouping.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .ad_form_playbook import BEAT_CHAR_CEILING, apply_beat_narration_cap, speech_profile_for_strategy
from .ad_script import (AD_EXPRESSION_VERSION, AdScript, derive_script_evidence,
                        script_stage_schema, script_narration, validate_script, validate_script_timing)
from .beat_timing import execution_action_bounds
from .review_policy import REVIEW_POLICY_VERSION
from .directing import StateChange, VisualDirection, ShotContinuity, derive_continuous_start_states, prepare_storyboard_wire
from .generation_policy import generation_groups
from .planning import compact_schema, expression_schedule, validate_expression_schedule
from .schemas import Plan
from .refinement_patch import (REFINEMENT_OUTPUT_VERSION, SPARSE_REFINEMENT_INSTRUCTION,
                               compose_sparse_refinement, sparse_refinement_schema, sparse_refinement_compatible)


class _DraftModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RevisionKept(Exception):
    """A revision could not be applied. Keep the plan from before that revision."""


class ProgressivePlanningError(ValueError):
    """Owned planning failures without automatic same-request retries."""

    error_code = "planning_content_failed"
    retryable = False

    def __init__(self, message, *, issue_summary=(), repair_attempts=0,
                 error_code="planning_content_failed"):
        super().__init__(message)
        self.error_code = error_code
        self.issue_summary = list(issue_summary)[:8]
        self.repair_attempts = repair_attempts


class TimingReductionError(ProgressivePlanningError):
    """Only this owned guard can consume another remaining content repair."""

    def __init__(self, beat_ids=(), *, whole_plan=False):
        super().__init__("部分内容段只缩短了时间估计，实际动作与台词没有相应精简；本轮已停止。已有方案和素材已保留，尚未开始新的图片或视频生成")
        self.beat_ids = list(beat_ids)
        self.whole_plan = whole_plan


MAX_PLAN_REFINEMENTS = 2
# One storyboard can be recompiled after a local correction. This bound stops
# a correction that still reports a change after the board is already the same.
MAX_DIRECTOR_PASSES = 24
PROGRESSIVE_REFINEMENT_POLICY_VERSION = 1


_ID = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=80)]
_FactID = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=160)]


class Coverage(_DraftModel):
    beat_id: _ID
    weight: float = Field(gt=0, le=100, allow_inf_nan=False, strict=True,
        description="同一内容节拍在各镜头中的相对时长份额；完整覆盖该拍时填1，不是秒数")


class DirectedShot(_DraftModel):
    id: _ID
    coverage: list[Coverage] = Field(min_length=1, max_length=12)
    description: str = Field(min_length=1, max_length=1500)
    product_visible: bool | None = Field(default=None, exclude_if=lambda value: value is None,
        description="本镜商品是否在画面里。讲解时拿在手里或放在身前也算在画面里")
    visual_motif: str = Field(default="", max_length=16, exclude_if=lambda value: not value,
        description="本镜承接的一个贯穿元素，可空。不写进description")
    match_cut: Literal["motion", "color", "scale"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="与上一镜的轻匹配：motion动作、color颜色、scale尺度。可空")
    purpose: str = Field(min_length=1, max_length=300)
    evidence_ids: list[_FactID] = Field(max_length=12)
    character_ids: list[_ID] = Field(max_length=8,
        description="本镜实际出镜角色的ID，包括只出现同一人的手、后脑或附着发丝；画外说话人不因此写成出镜角色。空列表只能表示实际无人物，独立发束/假发/概念材质须在description说清载体")
    continuity: ShotContinuity


class DirectorDraft(_DraftModel):
    title: str = Field(min_length=1, max_length=100)
    video_flow: str = Field(default="", max_length=400, exclude_if=lambda value: not value,
        description="先写整条成片的观看顺序：开场怎么留人，中间怎么展开，结尾停在哪。再按这个顺序写镜头")
    ad_script: AdScript
    visual_direction: VisualDirection
    shots: list[DirectedShot] = Field(min_length=1, max_length=108)
    state_changes: list[StateChange] = Field(default_factory=list, max_length=8, exclude_if=lambda value: not value,
        description="只列文字锁不住的可见外观变化。subject_label 例如头发；owner 是角色 id 或 product；start 与 result 只写外观，例如湿，发梢成缕和干顺，蓬松披肩；start_shot 早于 result_shot。开关机和视频自己能演的不要写。15秒及以内每个角色最多一条。没有就留空")


class _DialogueLine(_DraftModel):
    beat_id: str = Field(strict=True, min_length=1, max_length=80)
    narration: str = Field(strict=True, min_length=1, max_length=BEAT_CHAR_CEILING, pattern=r"\S")


class _DialoguePolish(_DraftModel):
    narrations: list[_DialogueLine] = Field(min_length=1, max_length=12)


def dialogue_narration_limits(plan):
    """Read existing speech reservations without borrowing any visual time."""
    profile = speech_profile_for_strategy(plan.get("strategy") if isinstance(plan, dict) else None)
    if profile.characters_per_tenth == 29:
        rate, beat_cap = Decimal("2.9"), 29
    else:
        rate, beat_cap = Decimal(profile.characters_per_tenth) / Decimal(10), profile.max_beat_characters
    result = []
    for segment in plan["segments"]:
        for span in segment["speech_spans"]:
            seconds = Decimal(str(span["end"])) - Decimal(str(span["start"]))
            result.append({"beat_id": span["beat_id"], "segment_id": segment["id"],
                "start": span["start"], "end": span["end"], "available_seconds": float(seconds),
                "max_characters": min(beat_cap, int(seconds * rate))})
    return result


def dialogue_polish_schema(draft, limits):
    schema = _DialoguePolish.model_json_schema()
    ids = [beat["id"] for beat in draft["ad_script"]["beats"] if beat["narration"]]
    line = schema.pop("$defs")["_DialogueLine"]
    items = []
    for limit in limits:
        item = deepcopy(line)
        item["properties"]["beat_id"]["const"] = limit["beat_id"]
        item["properties"]["narration"].update(maxLength=limit["max_characters"],
            description=f"本拍现有说话窗口{limit['available_seconds']}秒，含标点空白最多{limit['max_characters']}字符；不能扩大窗口或借用其他节拍时间")
        items.append(item)
    schema["properties"]["narrations"].update(minItems=len(ids), maxItems=len(ids), items={"oneOf": items})
    return schema


def compose_dialogue_polish(raw, before, limits):
    """Accept only a complete set of existing spoken lines; preserve all else."""
    polish = _DialoguePolish.model_validate(raw)
    expected = {beat["id"] for beat in before["ad_script"]["beats"] if beat["narration"]}
    ids = [line.beat_id for line in polish.narrations]
    if len(ids) != len(set(ids)) or set(ids) != expected:
        raise ValueError("台词收尾须完整且唯一对应现有非空台词节拍")
    budgets = {limit["beat_id"]: limit["max_characters"] for limit in limits}
    if any(len(line.narration) > budgets[line.beat_id] for line in polish.narrations):
        raise ValueError("台词收尾超过该节拍实际说话窗口的字数上限")
    revised = deepcopy(before)
    lines = {line.beat_id: line.narration for line in polish.narrations}
    for beat in revised["ad_script"]["beats"]:
        if beat["id"] in lines:
            beat["narration"] = lines[beat["id"]]
    return revised


def _only_natural_speech_failed(review):
    # The caller supplies the complete, strictly validated combined review.
    return ([check.criterion for check in review.checks if not check.passed] == ["natural_speech"]
            and review.execution_check.action_capacity and review.execution_check.state_continuity
            and review.alignment.ready)


DIALOGUE_POLISH_SYSTEM = """你只执行本轮唯一一次台词自然度收尾，只输出schema规定的JSON对象narrations，每项仅beat_id与narration。
当前完整方案除natural_speech外已通过本轮文字审核。结合实际人物关系、商品依据、画面和完整台词，把尚未固定的说法改成自然完整的口语；短于原句不代表自然或合格。保留用户固定台词原文、商品事实、核心句意、选择理由与各拍声画关系，不增加主张、不把台词移到其他节拍，不把现有有声节拍变成静音。可在本拍预算内省去用户未固定的重复修饰或卖点，不要求一句塞入全部旧关键词。
逐拍严格遵守narration_limits的max_characters，标点和空白也计入；额度按本拍现有实际说话窗口计算，不使用全片总字数或29字通用上限替代，不向其他时段借时间，不扩大窗口或下调动作估时。超过任何一拍额度会停止，不能靠下一轮再修。
列表须恰好覆盖当前全部非空台词beat，每个beat_id仅一次；无需改动的台词原样返回。不能输出或改写任何镜头、动作、状态、估时、人物、事实引用、摘要及其他字段。本轮专用schema替代plan_refinement通用Skill的完整稿输出合同，不输出ad_script或shots。
程序仅将这些台词合入原稿副本，再按原有动作和估时重新编排，并重新完整独立审核；这不是自动通过，也不能自行再请求一次润色。只返回台词列表，不返回审核结论或隐含思考过程。"""


def director_schema(*, initial=False):
    schema = DirectorDraft.model_json_schema()
    script = script_stage_schema()
    schema["$defs"].update(script.pop("$defs"))
    schema["$defs"]["AdScript"] = script
    # Continuous start states are the only omitted authored state we derive.
    from .directing import storyboard_continuity_schema
    continuity = storyboard_continuity_schema()
    if initial:
        # Initial authoring never needs to transcribe the previous end state.
        # Keep strict stored validation: an explicit conflicting value is not
        # deleted or rewritten if a provider ignores this wire instruction.
        continuity["properties"]["start_state"]["description"] = (
            "初次起稿的continuous镜头不要输出start_state，程序沿用前镜完整end_state；"
            "opening、cut、scene_change仍须给出起态。不可借连续标记跳过动作或改变状态。")
        continuity["allOf"][0]["then"] = {"not": {"required": ["start_state"]}}
        flow = schema["properties"]["video_flow"]
        flow.pop("default", None)
        flow["minLength"] = 12
        flow["maxLength"] = 400
        schema.setdefault("required", []).append("video_flow")
        shot_def = schema["$defs"]["DirectedShot"]
        visible = shot_def["properties"]["product_visible"]
        visible.clear()
        visible["type"] = "boolean"
        visible["description"] = "本镜商品是否在画面里。讲解时拿在手里或放在身前也算在画面里"
        shot_def.setdefault("required", []).append("product_visible")
    else:
        schema["properties"].pop("video_flow", None)
        if "video_flow" in schema.get("required", []):
            schema["required"].remove("video_flow")
    schema["$defs"]["ShotContinuity"] = continuity
    return compact_schema(schema)


_SUMMARY_FIELDS = ("core_message", "audience_takeaway", "product_role")
_EVIDENCE_MAPPING_ERROR = "广告内容依据须落实到对应节拍的镜头"
_CAPACITY_ERRORS = frozenset({
    "动作与完整台词无法放入对应画面时长",
    "完整台词无法为对应画面留出时间，请减少内容节拍或重新策划",
    "连续动作或完整台词无法在30秒上限内安全分组，请在完整动作或场景转换处重新安排内容",
    "广告脚本台词超过所选时长预算",
    "完整台词与内容节拍按顺序无法放入视频片段，请改写脚本而非切碎句子",
    "内容被切成过多镜头，无法为每个镜头分配有效时间",
    "同一镜头不能跨生成请求，请在完整动作边界安排分镜",
    "镜头不能跨生成请求重复出现",
})


def _is_capacity_error(error):
    text = str(error)
    if "旁白台词超过对应朗读时段的长度" in text:
        return True
    if "最后一句需在" in text and "停住" in text:
        return True
    return not isinstance(error, ValidationError) and text in _CAPACITY_ERRORS


_CONTRACT_FIELDS = {"evidence_ids", "character_ids", "product_anchor_ids"}


def _is_schema_shape_error(error):
    """A storyboard the model shaped wrong, not a broken fact or cast reference."""
    if isinstance(error, (KeyError, TypeError)):
        return True
    if not isinstance(error, ValidationError):
        return False
    items = error.errors(include_input=False, include_url=False)
    if not items:
        return False
    for item in items:
        if any(part in _CONTRACT_FIELDS for part in item.get("loc") or ()):
            return False
        if item.get("type") == "value_error":
            text = str((item.get("ctx") or {}).get("error") or "")
            if any(token in text for token in ("事实", "角色", "台词", "依据")):
                return False
    return True


def refinement_schema(scope):
    """Request only editable fields; fixed globals are owned by the server."""
    schema = director_schema()
    for field in ("title", "visual_direction"):
        schema["properties"].pop(field)
        schema["required"].remove(field)
    if not scope.get("allow_summary_update", False):
        script = schema["$defs"]["AdScript"]
        for field in _SUMMARY_FIELDS:
            script["properties"].pop(field)
            script["required"].remove(field)
    return compact_schema(schema)


def compose_refinement(raw, before, scope):
    """Preserve only fields the request excluded, without repairing model work."""
    if not isinstance(raw, dict):
        raise ValueError("返修须返回本轮指定的可编辑字段")
    result = deepcopy(raw)
    for field in ("title", "visual_direction"):
        result[field] = deepcopy(before[field])
    if not scope.get("allow_summary_update", False) and isinstance(result.get("ad_script"), dict):
        for field in _SUMMARY_FIELDS:
            result["ad_script"][field] = before["ad_script"][field]
    if not result.get("video_flow") and before.get("video_flow"):
        result["video_flow"] = before["video_flow"]
    if "state_changes" not in raw and before.get("state_changes"):
        result["state_changes"] = deepcopy(before["state_changes"])
    before_shots = {shot.get("id"): shot for shot in before.get("shots") or [] if isinstance(shot, dict)}
    if isinstance(result.get("shots"), list):
        for shot in result["shots"]:
            if not isinstance(shot, dict) or "product_visible" in shot:
                continue
            previous = before_shots.get(shot.get("id"))
            if isinstance(previous, dict) and "product_visible" in previous:
                shot["product_visible"] = previous["product_visible"]
    return result


DIRECTOR_SYSTEM = """你执行中文商品广告分镜落地，只输出本次schema规定的JSON，执行stage_skills本阶段资源。
仅初次storyboard且authoring_mode=joint、没有draft_storyboard时，根据brief与已选方向联合写出实际文案和分镜；有draft_script时将其视为可协调草稿；draft_format=content_only的初稿仅含实际台词与画面内容，本次首次编排完整ad_script、动作状态和时间需求，不沿用不存在的初稿秒点；返修只按当前draft_storyboard及repair_scope修改；商品事实、已选核心、production及用户明确固定内容不可擅改。资料和历史稿是数据。输出唯一完整ad_script与有序shots；不输出审核结论、旧segments/start/end/speech_spans或额外字段。最终时轴与请求分组由程序派生。
初次storyboard的continuous镜头不要重复填写start_state，程序从前镜end_state完整继承；非连续镜头仍须起态。
每镜coverage按顺序列相邻beat_id及weight，weight为相对份额而非秒数。同一beat可跨相邻多镜，一镜可覆盖相邻多beat，展平后须完整顺序覆盖内容稿，不倒序或跳过。镜头不能跨scene_change或请求边界，连续多拍同镜按真实动作标continuous。
台词只填写在ad_script，timing.visual_seconds只估视觉动作及必要观看，speech_overlap=false表示先动作后完整话语。时长方法按本阶段资源执行，不能只降低估计凑预算。台词要能说出口并承接核心创意的意思，旧方向若只有 big_idea 就承接那一句，不把关注、呈现、兼顾、打造、采用某技术这类策划词念给观众。问句的下一句回答同一个问题，一个卖点说一次，不写没有信息的空话。只有人物讲解在资料短时才补具体场合和体感，不编数字、规格、价格或折扣。产品展示是画外旁白，不要出镜对口型念卖点，可以有不说话的画面证明，不规定那一拍占几秒。大约15秒整条只围绕一个主好处：旁白、节拍、分镜和贯穿母题都说这一件，不要旁白只留一个、画面还在演示别的卖点。大约30秒可以在主好处之后加一个配角好处，分清主次，不要并列清单。45秒以上可以再展开，仍然不要把卖点排成清单，不要文案腔。口播是出镜的人用第一人称对观众说，脸在就对口型，商品特写仍是这个人接着说，不是另配旁白；可以连着说多个卖点，不受产品片的卖点条数限制。多出来的好处就留空。不描述镜头，不编画面上没有的人，不用痛点问句开场，不编价格。剧情是戏里的第一人称短对白，不要整条都写成画外音，也不要把第三人称解说塞进人物嘴里；有醒着的人能听见才对口型；她一个人或别人睡着、不在场时，改成她自己的第一人称内心独白，嘴不要动，并写上放轻脚步、回头看、屏住呼吸这类表演，也可以一句都不说；内心独白不谈产品，也不说卖点，这不是产品片的旁白；不念画面，也不要把这一镜已经看见的事再说一遍，例如室友在睡就不要说不敢开、怕吵醒她，改成这个人自己会说的反应或提醒；末句是当下的反应或关系，不要点题，不要用没白、终于、还好有它、幸亏，也不要让产品当主语；不要用对白评价顺、干得快、不吵、安静、护发。剧情不念点击或链接。剧情要证明的每个好处至少有一镜写明看得见的技法，例如慢动作、微距、逆光或移焦；焦点从前景拉到室友或睡着的人脸上，算安静的证明。台词仍然不念卖点。过程镜只对齐起始状态，干了或顺了这种终点写在切之后的另一镜，不要写成一半或半干，也不要在同一镜里硬切到结果。前一镜结束仍是湿的、后一镜一开始已经干了，也要出起始和结果参考。文字锁不住的可见外观变化写进 state_changes，每条给 subject_label、owner、start、result、start_shot、result_shot。subject_label 例如头发；owner 是角色 id 或 product；start 与 result 只写这一处外观，例如湿，发梢成缕和干顺，蓬松披肩，不写动作，也不写道具；start_shot 必须早于 result_shot。只写人的头发干湿、乱顺，或物体满空这种看得见、单靠文字锁不住的样子。开关机、拿起放下，以及视频自己能演的动作不要写。15秒及以内每个角色最多一条。没有这种变化就给空列表。家用物品留在该在的位置，不要跟着人出门，不要英雄定格，不要人物举着产品对镜头，结尾不要让商标或字样朝向镜头。剧情默认人物不看镜头，分镜写明需要看镜头时才看。背景人物若在床上，每镜床入画都要写上这个人；朝向以这一镜写明的为准，这一镜没改就沿用上一镜。同框的两个人，衣服颜色、花纹和发型要能一眼分开，不要都穿碎花。产品片和口播才用英雄定格。若 shared_references 含本类型 playbook，开场、节拍、景别和切节奏按那一份安排，台词密度不超过 speech_budget。entry_transition=cut只省略入镜前过程，不能据此把镜内未完成动作的end_state写成完成。
初次分镜先写 video_flow，再按它写 shots。video_flow 和开场镜头要让人看出已选方向的 hook 和核心创意，用能拍到的画面，不把这些字段的策划词念进台词。每镜 description 写清主体和动作、景别与运镜、光线、产品是否在画面里、环境和谁在开口。谁在开口就写第几句，例如她开口说第三句；这一镜不说就写她没说话。没有醒着的人听时不要写开口说，写她没有开口，并写上放轻脚步、回头看一眼这些表演。角色 description 里没有的衣服不要写进镜头；稍后才穿或拿着的也要先写进角色外观，例如外套稍后披上或拿着。台词落点不要把台词原文抄进 description。如果描述里已经写下和台词不一样的原句，改成和 ad_script 同一句，或删掉那句引语。旁白留空的镜头不要写「旁白说这句话」。产品展示的 visual_motif 只写主好处那一个贯穿元素。不写如同、仿佛、像什么一样、高级感、氛围感。概念里的产品世界比喻留在方向，镜头改成光线、材质、位置和动作。若已选方向是穿梭，或这条按生活问题接到产品世界再回到结果，每镜 description 用「生活世界：」「产品世界：」或「衔接：」开头。生活问题和真实结果标生活世界，离子颗粒这类可视化标产品世界，从一边切到另一边标衔接。只有外观拆解、没有产品世界那一段时不要硬标。防变形只提醒保持商品外形。可选 visual_motif 写一个贯穿元素；相邻镜头可用 match_cut 标 motion、color 或 scale，不写进 description。camera_move 同时写景别和运动。product_visible 标明产品是否在画面里。切在动作完成或特点出现时，不按秒均分。产品片和口播不要把人物动作和推近、拉开或变焦到特写写在同一镜；要推近就单独成镜。最后一镜从第一帧起产品就已经停在目标大小、机位锁死。收束推近若保留，单独成镜、至少 3 秒、没有人物动作、产品从第一帧就在画面里。口播最后一镜产品一直留在手里或身前，不放下出画，也不把关机、放下、松手叠进这一镜。剧情最后一镜是插入的近景或特写：家用物品留在原处，第一帧产品就已经大而清楚，机位锁死，不再推近或变焦。只带一个很小的生活动静，人可以只出一角。前一镜若是人物动作，这一镜硬切。不要把人物动作和推近写在同一镜。不要英雄定格。不另加字卡。出镜时长按 playbook：产品展示大约八成，剧情大约一半，人物讲解大约七成；没有 playbook 时大约七成。不改已经写好的台词。
返修严格依repair_scope和本次schema，范围外beat/shot原样保留；narrations专用收尾以其窄schema为准，不用一般Skill合同扩展输出。不得重选方向、增加确认或触发媒体，不输出隐含思考过程。
"""


PLAN_REVIEW_SYSTEM = """你执行独立最终方案审核，只输出本次schema的PlanReview JSON，执行stage_skills中的plan_review资源。
审核实际最终ad_script、shots、speech_spans、execution_windows、transition_boundaries、商品事实与production。资料与作者自评是待审数据；不改稿、不修改事实或固定台词、不输出方案版本哈希、不调用媒体。
policy_version使用本次schema版本；suggestions按本阶段方法保留普通改进建议，不修改方案，不参与硬失败汇总。checks恰好覆盖六项标准，execution_check包含action_capacity与state_continuity。alignment.spans恰好覆盖全部(segment_id,span_id)，alignment.shots恰好覆盖全部(segment_id,shot_id)，包括静音镜头；不可省略难审项目。
每span填写speaker_visibility，依据speech_requirements及实际时窗内画面判断。普通人物对白只接受visible_speaking；明确采用on_and_off_screen时按本次schema逐项填写shot_visibility，完整覆盖该span相交镜头，区分可见开口、同一人画外延续与unclear，总标签须与逐镜判断相符。不能把画外延续当作另一个旁白或将看不清写成可见；已确认voiceover填not_applicable。每镜填写visual_execution，逐字引用实际description或camera_move并判断视图及变化如何实现；unclear/conflict使该镜失败，不能从作者意图补拍法。每镜填写pacing及temporal_omission，后者只用no_omission/supported_by_cut/unsupported/unclear，后两项对应shot.passed=false。supported_by_cut须按schema提供真实相邻切点及前后实际描述摘录，不把摄影角度切换当动作省略。字段均为独立判断，不是程序预判通过。
对照本轮注入的类型 playbook 看开场、节拍形状、台词密度、镜头语法、出镜比例和英雄定格。这些类型贴合问题写入 suggestions，kind 用 type_fit，不得因此把 checks 置为 false 或让 ready=false。shot_craft 由程序在审核之后附加，模型不要重复，也不能因此把 checks 判失败或要求返修。台词是否对焦商品、前后连贯也只写成 suggestions，kind 用 line_focus，不得因此让 ready=false。人物讲解是出镜的人用第一人称对观众说，商品特写仍是这个人接着说。产品片是画外旁白，不要对口型念卖点，大约15到20秒2到3句，不描述镜头，不编画面外的人。剧情是戏里的第一人称，不念画面，也不把这一镜已经看见的事再说一遍，不用她、他做解说。没有醒着的听者时，内心独白不算失败，也不是产品片旁白，不要因此让 ready=false。末句不要点题，对白也不要评价卖点。每个要证明的好处需要一镜写明技法，焦点拉到室友或睡着的人脸上算安静。家用物品不要跟着人出门，不要英雄定格。剧情末镜应是插入的近景或特写，第一帧产品就已经在，机位锁死，不再推近；写成推近或同一镜又有人物动作又在推近，只作建议，不要因此让 ready=false。默认不看镜头。床入画时，床上的人、睡着的人、仍在睡都算人还在。程序会补上没写全的朝向。背景人物没写全、点题、高光、带出门和状态怎么配对，都只作建议，不要因此让 ready=false。过程镜对齐起始，结果写在后面一镜。程序会先改一次台词和点题用途。改完仍在说卖点、人物看着镜头，或结尾是英雄定格、举向镜头、商标朝向镜头、口号或链接，方案不算完成。解说画面不再因此判失败。旁白太密由程序改写，不得因此把 checks 置为 false。alignment.ready等于ad_expression_passed且所有spans/shots通过；顶层ready须全部checks、execution_check及alignment.ready通过，表示未发现需阻断的问题，不代表创作质量满分。suggestions有内容也可ready=true，任何明确失败仍不能用建议抵消。不能提升任何明确失败结论，无法确定时指出具体问题。动作与声画检查方法见本阶段参考，通过项省略重复note，失败项须给有界可定位note；所有逐项判定、数值、visual_execution与省略证据仍须完整，不输出隐含思考过程或声称已验证成片。
"""


def fixed_speech(brief, conversation, current_plan=None):
    """Protect explicit quoted speech and requests to retain the current copy.

    This narrow deterministic guard supplements the full conversation supplied
    to directing/review; it is not a general natural-language intent parser.
    """
    locked = []
    texts = [brief] + [item.get("content", "") for item in conversation if item.get("role") == "user"]
    for text in texts:
        if re.search(r"台词.{0,5}(?:可以改|允许修改|不必保留)", text):
            locked = []
        if re.search(r"(?:保留|固定|不改|不变|逐字).{0,8}台词|台词.{0,8}(?:保留|固定|不改|不变|逐字)", text):
            quoted = re.findall(r"[“\"]([^”\"\n]+)[”\"]", text)
            if not quoted and current_plan:
                quoted = [beat["narration"] for beat in (current_plan.get("ad_script") or {}).get("beats", [])
                          if beat.get("narration")]
            locked.extend(quoted)
        else:
            locked.extend(re.findall(r"(?:台词|对白|说)[^。\n“\"]{0,12}[“\"]([^”\"\n]+)[”\"]", text))
    return list(dict.fromkeys(locked))


def _shares(weights, ticks):
    if ticks < len(weights):
        raise ValueError("内容被切成过多镜头，无法为每个镜头分配有效时间")
    # Reserve one tick per appearance; largest remainder preserves exact totals.
    spare = ticks - len(weights)
    values = [spare * weight / sum(weights) for weight in weights]
    shares = [1 + int(value) for value in values]
    for index in sorted(range(len(weights)), key=lambda i: (-(values[i] % 1), i))[:ticks - sum(shares)]:
        shares[index] += 1
    return shares


def canonical_director(raw, strategy, duration_seconds=None, locked_lines=()):
    """Validate authored fields before any capacity check or scoped revision."""
    from .planner_recovery import drop_extra_forbidden
    data = drop_extra_forbidden(DirectorDraft, prepare_storyboard_wire(raw))
    data["ad_script"] = derive_script_evidence(data["ad_script"], strategy,
                                              server_skill_version=AD_EXPRESSION_VERSION,
                                              duration_seconds=duration_seconds,
                                              locked_lines=locked_lines)
    # A new film has no preceding shot to cut away from. Canonicalize only
    # this position-owned label, retaining the authored start state, actions
    # and omission text for full validation and independent review. A first
    # `continuous` entry still claims a nonexistent predecessor and is invalid.
    shots = data.get("shots")
    first = shots[0].get("continuity") if isinstance(shots, list) and shots and isinstance(shots[0], dict) else None
    if (isinstance(first, dict) and first.get("entry_transition") in ("cut", "scene_change")
            and isinstance(first.get("start_state"), str) and first["start_state"].strip()):
        first["entry_transition"] = "opening"
    inherited = derive_continuous_start_states({"segments": [{"shots": data["shots"]}]})
    data["shots"] = inherited["segments"][0]["shots"]
    return DirectorDraft.model_validate(data)


def _bind_spoken_destination(draft, strategy, cta_target, locked):
    """Say a chosen destination, and record its fact, before the contract check.

    An open destination waives that fact so “不要说去哪里” and a selected
    ``f_cta`` cannot cancel each other.
    """
    from .cta import apply_spoken_policy, cta_fact_ids, is_explicit_cta, is_open_cta
    script = apply_spoken_policy(draft.ad_script, strategy, cta_target, locked)
    facts = cta_fact_ids(strategy)
    waived = ()
    if is_explicit_cta(cta_target) and facts:
        data = script.model_dump()
        spoken = [beat for beat in data["beats"] if (beat.get("narration") or "").strip()] or data["beats"]
        last = spoken[-1]
        from .planner_recovery import EVIDENCE_LIST_LIMIT, bound_server_list
        have = list(last.get("evidence_ids") or [])
        for fact_id in facts:
            if fact_id not in have:
                have.append(fact_id)
        last["evidence_ids"] = bound_server_list(have, EVIDENCE_LIST_LIMIT)
        top = list(data.get("evidence_ids") or [])
        for fact_id in facts:
            if fact_id not in top:
                top.append(fact_id)
        data["evidence_ids"] = bound_server_list(top, EVIDENCE_LIST_LIMIT)
        script = AdScript.model_validate(data)
        beat_id = last["id"]
        for shot in draft.shots:
            if any(part.beat_id == beat_id for part in shot.coverage):
                covered = list(shot.evidence_ids)
                for fact_id in facts:
                    if fact_id not in covered:
                        covered.append(fact_id)
                shot.evidence_ids = bound_server_list(covered, EVIDENCE_LIST_LIMIT)
    elif is_open_cta(cta_target):
        waived = tuple(facts)
    draft.ad_script = script
    return waived


def _validate_authored_contract(draft, strategy, duration, generation_strategy, locked, waived_evidence_ids=()):
    """Check fixed identities before timing can mask a fatal authored error."""
    script = validate_script_timing(validate_script(draft.ad_script, strategy, duration,
        final=False, require_selected_evidence=True, generation_strategy=generation_strategy,
        waived_evidence_ids=waived_evidence_ids), required=True)
    narration = script_narration(script)
    if any(line not in narration for line in locked):
        raise ValueError("分镜协调未保留用户明确固定的台词")
    selected = next(d for d in strategy.directions if d.id == strategy.selected_direction_id)
    facts = {fact.id for fact in strategy.brief.facts}
    characters = {person.id for person in selected.production.characters} if selected.production else set()
    if not set(draft.visual_direction.product_anchor_ids).issubset(facts):
        raise ValueError("视觉约定只能引用本轮已有商品事实")
    if any(not set(shot.evidence_ids).issubset(facts) for shot in draft.shots):
        raise ValueError("镜头必须说明信息作用，且只能引用已有商品事实")
    if any(not set(shot.character_ids).issubset(characters) for shot in draft.shots):
        raise ValueError("镜头只能引用已确认的出镜角色")
    ids = [beat.id for beat in script.beats]
    seen = []
    if len({shot.id for shot in draft.shots}) != len(draft.shots):
        raise ValueError("全片镜头ID不得重复")
    for shot in draft.shots:
        covered = [part.beat_id for part in shot.coverage]
        if len(covered) != len(set(covered)) or any(beat not in ids for beat in covered):
            raise ValueError("镜头只能覆盖真实且不重复的内容节拍")
        positions = [ids.index(beat) for beat in covered]
        if positions != list(range(positions[0], positions[-1] + 1)):
            raise ValueError("同镜内容节拍必须相邻有序")
        if any(script.beats[position].boundary_before == "scene_change" for position in positions[1:]):
            raise ValueError("同一镜头不能跨越脚本明确的场景转换")
        for beat in covered:
            if not seen or seen[-1] != beat:
                seen.append(beat)
    if seen != ids:
        raise ValueError("分镜coverage须按顺序恰好覆盖全部内容，不能遗漏或反复拼接")
    # These existing validators do not need allocated seconds. Speech-window
    # visibility remains part of the complete compiled Plan validation below.
    from .directing import validate_directing_plan
    from .production import validate_production_plan
    authored = {"strategy": strategy, "production": selected.production, "ad_script": script,
        "script": narration, "visual_direction": draft.visual_direction,
        "segments": [{"shots": [{**shot.model_dump(), "beat_id": shot.coverage[0].beat_id,
            "beat_ids": [part.beat_id for part in shot.coverage]} for shot in draft.shots]}]}
    validate_directing_plan(authored, required=True)
    validate_production_plan(authored)
    return script


def compile_director(raw, strategy, duration, generation_strategy, asset_ids, locked=(), cta_target=None):
    """Own all final timings/text projections; never repair authored semantics."""
    draft = canonical_director(raw, strategy, duration_seconds=duration, locked_lines=locked)
    waived = _bind_spoken_destination(draft, strategy, cta_target, locked)
    script = _validate_authored_contract(draft, strategy, duration, generation_strategy, locked, waived)
    script = validate_script(script, strategy, duration, generation_strategy=generation_strategy,
                             waived_evidence_ids=waived)
    narration = script_narration(script)
    tenth = speech_profile_for_strategy(strategy).characters_per_tenth
    grouping = generation_groups(script, duration, generation_strategy, characters_per_tenth=tenth)
    selected = next(d for d in strategy.directions if d.id == strategy.selected_direction_id)
    from .ad_form_playbook import resolve_playbook_form
    structure = selected.film_concept.structure if selected.film_concept is not None else None
    form = resolve_playbook_form(strategy.brief.ad_form, structure)
    schedule = expression_schedule(script, grouping["durations"],
        beat_groups=grouping["beat_groups"] if generation_strategy == "continuity_v1" else None,
        characters_per_tenth=tenth,
        ad_form=form)
    segments = []
    used_shots = set()
    for index, reserved in enumerate(schedule):
        groups = reserved["groups"]
        group_ids = {group["beat_id"] for group in groups}
        authored = [shot for shot in draft.shots if any(part.beat_id in group_ids for part in shot.coverage)]
        if any(any(part.beat_id not in group_ids for part in shot.coverage) for shot in authored):
            raise ValueError("同一镜头不能跨生成请求，请在完整动作边界安排分镜")
        if used_shots.intersection(shot.id for shot in authored):
            raise ValueError("镜头不能跨生成请求重复出现")
        used_shots.update(shot.id for shot in authored)
        intervals = {shot.id: [] for shot in authored}
        for group in groups:
            contributions = [(shot.id, part.weight) for shot in authored for part in shot.coverage if part.beat_id == group["beat_id"]]
            if not contributions:
                raise ValueError("分镜遗漏实际内容窗口")
            cursor = round(group["start"] * 10)
            shares = _shares([weight for _, weight in contributions], round((group["end"] - group["start"]) * 10))
            for (shot_id, _), ticks in zip(contributions, shares):
                intervals[shot_id].append((cursor / 10, (cursor + ticks) / 10))
                cursor += ticks
        shots = []
        for shot in authored:
            parts = intervals[shot.id]
            if any(abs(a[1] - b[0]) > 1e-7 for a, b in zip(parts, parts[1:])):
                raise ValueError("同一镜头的内容覆盖必须连续")
            shots.append({**shot.model_dump(exclude={"coverage"}), "start": parts[0][0], "end": parts[-1][1],
                          "beat_ids": [part.beat_id for part in shot.coverage],
                          "beat_id": shot.coverage[0].beat_id, "narration": ""})
        spans = []
        for group in groups:
            if not group["text"]:
                continue
            start, end = group["speech_start"], group["speech_end"]
            visible = [shot for shot in shots if min(shot["end"], end) - max(shot["start"], start) > 1e-7]
            shot_ids = [shot["id"] for shot in visible]
            spans.append({"id": f"speech_{len(spans) + 1}", "beat_id": group["beat_id"],
                "speaker_id": selected.production.speaker_id if selected.production and selected.production.speech_mode == "character_dialogue" else "",
                "text": group["text"], "start": start, "end": end, "shot_ids": shot_ids,
                "anchors": [{"text": group["text"], "start": start, "end": end, "shot_ids": shot_ids,
                             "visual_evidence": "；".join(shot["description"] for shot in visible)[:1000]}]})
        segments.append({"id": f"seg_{index + 1}", "duration_seconds": reserved["duration_seconds"],
            "purpose": "；".join(shot["purpose"] for shot in shots)[:300],
            "prompt": "\n".join(f"{shot['start']:g}–{shot['end']:g}秒：{shot['description']}" for shot in shots)[:8000],
            "reference_asset_ids": list(asset_ids), "shots": shots, "speech_spans": spans,
            "narration": "".join(span["text"] for span in spans)})
    from .planning import align_ending_hold, ending_hold_seconds, fit_story_insert
    from .story_logic import snap_declared_speech
    hold = ending_hold_seconds(script, form)
    if hold and segments:
        last_beat = next((beat for beat in reversed(script.beats) if beat.narration), None)
        action = None
        if last_beat is not None and last_beat.timing is not None:
            action = float(last_beat.timing.visual_seconds)
        original = deepcopy(segments[-1])
        align_ending_hold(segments[-1], silence=hold, action_seconds=action)
        try:
            validate_expression_schedule({"segments": segments}, schedule)
        except (ValueError, KeyError, TypeError):
            segments[-1] = original
    if form == "scene_story" and segments and schedule:
        fit_story_insert(segments[-1], schedule[-1])
    speech_segments = deepcopy(segments)
    speech_schedule = deepcopy(schedule)
    snapped = snap_declared_speech(segments, schedule)
    if form == "scene_story" and segments and schedule:
        fit_story_insert(segments[-1], schedule[-1])
    payload = {"title": draft.title, "ad_script": script.model_dump(),
        "script": narration, "strategy": strategy.model_dump(), "audio_strategy": "native",
        "facts": {"name": strategy.brief.product_name, "claims": [fact.text for fact in strategy.brief.facts]},
        "production": selected.production.model_dump() if selected.production else None,
        "visual_direction": draft.visual_direction.model_dump(), "generation_strategy": generation_strategy,
        "video_flow": draft.video_flow, "segments": segments}
    if draft.state_changes:
        payload["state_changes"] = [item.model_dump() for item in draft.state_changes]
    plan = None
    schema_ok = False
    try:
        plan = Plan.model_validate(payload, context={"require_beat_ids": True, "require_directing_contract": True}).model_dump()
        schema_ok = True
        validate_expression_schedule(plan, schedule)
    except (ValueError, ValidationError, KeyError, TypeError):
        # A snap that already satisfies the plan schema stays. Restoring the
        # pre-snap clocks would put the line back on the silent shot.
        if not (schema_ok and snapped):
            segments = speech_segments
            schedule.clear()
            schedule.extend(speech_schedule)
            if form == "scene_story" and segments and schedule:
                fit_story_insert(segments[-1], schedule[-1])
            payload["segments"] = segments
            plan = Plan.model_validate(payload, context={"require_beat_ids": True, "require_directing_contract": True}).model_dump()
            validate_expression_schedule(plan, schedule)
    return plan, schedule, draft.model_dump()


def _continuity_signature(director):
    """States a later pass would write again. Identical means the board settled."""
    from .story_logic import iter_plan_shots
    rows = []
    for shot in iter_plan_shots(director):
        continuity = shot.get("continuity") if isinstance(shot, dict) else None
        if not isinstance(continuity, dict):
            rows.append(None)
            continue
        rows.append((
            continuity.get("start_state"),
            continuity.get("end_state"),
            continuity.get("entry_transition"),
        ))
    return tuple(rows)


def _rebind_reviewed_plan(plan):
    """The inner-voice rewrite happens after review. Keep the same verdict on the new text."""
    review = plan.get("plan_review") if isinstance(plan, dict) else None
    if not isinstance(review, dict) or not isinstance(review.get("reviewed_plan_sha256"), str):
        return
    from .plan_review import _fingerprint_validated_plan, _validated_review_subject
    subject = _validated_review_subject(plan)
    review["reviewed_plan_sha256"] = _fingerprint_validated_plan(
        subject, review.get("reviewed_reference_asset_ids"))


def saved_plan_review_request(plan):
    """One review payload for a film that is already saved.

    This does not invent a schedule the plan cannot reproduce. A plan without
    the director fields raises, and the caller records that as a system issue.
    """
    from .av_review import review_execution_windows, review_speech_requirements, review_transition_boundaries
    from .plan_review import plan_review_schema
    from .review_output import REVIEW_OUTPUT_VERSION, compact_review_schema
    from .review_policy import REVIEW_POLICY_VERSION

    projection = review_projection(plan)
    request = {"stage": "plan_review", "schema": compact_review_schema(plan_review_schema(plan)),
        "review_output_version": REVIEW_OUTPUT_VERSION, "review_policy_version": REVIEW_POLICY_VERSION,
        "final_plan": projection, "speech_requirements": review_speech_requirements(plan),
        "transition_boundaries": review_transition_boundaries(plan),
        "merchant_request": "", "conversation": []}
    from .av_review import _planned_action_limits
    if _planned_action_limits(plan):
        from .ad_script import AdScript
        from .generation_policy import generation_groups
        from .planning import expression_schedule
        script = AdScript.model_validate(plan["ad_script"])
        durations = [segment["duration_seconds"] for segment in plan["segments"]]
        from .ad_form_playbook import form_for_strategy, speech_profile_for_strategy
        tenth = speech_profile_for_strategy(plan.get("strategy")).characters_per_tenth
        grouping = generation_groups(script, sum(durations), plan.get("generation_strategy", "legacy_10s"),
                                     characters_per_tenth=tenth)
        schedule = expression_schedule(script, durations, beat_groups=grouping["beat_groups"],
                                       characters_per_tenth=tenth, ad_form=form_for_strategy(plan.get("strategy")))
        request["execution_windows"] = review_execution_windows(schedule, plan["segments"])
        request["execution_window_rules"] = "beat_start/beat_end是本拍实际分配的段内窗口，不是成片实测。"
    return request


def review_projection(plan):
    """Remove authors' justifications while preserving the actual reviewed work."""
    result = deepcopy(plan)
    for field in ("skill_trace", "script_review", "alignment_review", "plan_review"):
        result.pop(field, None)
    result["ad_script"] = {"beats": [{key: value for key, value in beat.items()
        if key not in ("purpose", "message")} for beat in result["ad_script"]["beats"]]}
    for segment in result["segments"]:
        segment.pop("purpose", None)
        segment.pop("prompt", None)
        for shot in segment["shots"]:
            shot.pop("purpose", None)
            continuity = shot.get("continuity")
            if isinstance(continuity, dict):
                continuity.pop("entry_reason", None)
        for span in segment["speech_spans"]:
            for anchor in span["anchors"]:
                anchor.pop("visual_evidence", None)
    return result


def capacity_repair_scope(draft):
    """A fixed whole-film duration couples all content when capacity fails."""
    return {"beat_ids": [beat["id"] for beat in draft["ad_script"]["beats"]],
        "shot_ids": [shot["id"] for shot in draft["shots"]],
        "adjacent_state_shot_ids": [], "allow_summary_update": True,
        "allow_beat_restructure": True}


def _evidence_mapping_gaps(draft):
    return {(beat["id"], fact_id) for beat in draft["ad_script"]["beats"]
        for fact_id in set(beat["evidence_ids"]) - {fact_id
            for shot in draft["shots"] if any(part["beat_id"] == beat["id"] for part in shot["coverage"])
            for fact_id in shot["evidence_ids"]}}


def _evidence_mapping_scope(error, draft, strategy):
    """A known coverage error may be authored again; foreign IDs never qualify."""
    if not isinstance(error, ValidationError):
        return None
    errors = error.errors(include_input=False, include_url=False)
    if not errors or any(item["type"] != "value_error"
            or str(item.get("ctx", {}).get("error")) != _EVIDENCE_MAPPING_ERROR for item in errors):
        return None
    allowed = {fact.id for fact in strategy.brief.facts}
    selected = next(d for d in strategy.directions if d.id == strategy.selected_direction_id)
    characters = {person.id for person in selected.production.characters} if selected.production else set()
    if (not set(draft["visual_direction"]["product_anchor_ids"]).issubset(allowed)
            or any(not set(shot["evidence_ids"]).issubset(allowed)
                or not set(shot["character_ids"]).issubset(characters) for shot in draft["shots"])):
        return None
    gaps = _evidence_mapping_gaps(draft)
    if not gaps:
        return None
    missing_beats = {beat_id for beat_id, _ in gaps}
    affected = [shot for shot in draft["shots"]
        if any(part["beat_id"] in missing_beats for part in shot["coverage"])]
    return {"beat_ids": sorted(missing_beats), "shot_ids": [shot["id"] for shot in affected],
        "adjacent_state_shot_ids": [], "allow_summary_update": False,
        "allow_existing_mapping_coverage": True}


def repair_scope(review, draft, plan):
    """Bound a single correction by failed objects plus adjacent state edges."""
    alignment = review["alignment"]
    if (not review["execution_check"]["action_capacity"]
            or any(not item["pacing"]["action_timing_passed"] for item in alignment.get("shots", []))):
        return capacity_repair_scope(draft)
    failed = {item["shot_id"] for item in alignment.get("shots", []) if not item["passed"]}
    beat_ids = set()
    for item in alignment["spans"]:
        if not item["passed"]:
            segment = next(s for s in plan["segments"] if s["id"] == item["segment_id"])
            beat_ids.add(next(s["beat_id"] for s in segment["speech_spans"] if s["id"] == item["span_id"]))
    for shot in draft["shots"]:
        if shot["id"] in failed:
            beat_ids.update(part["beat_id"] for part in shot["coverage"])
    # Global content failures genuinely affect the whole story; no invented
    # local attribution when the independent reviewer supplied only a global one.
    global_issue = any(not item["passed"] for item in review["checks"]) or not alignment["ad_expression_passed"]
    execution = review["execution_check"]
    if global_issue or (not beat_ids and not (execution["action_capacity"] and execution["state_continuity"])):
        beat_ids.update(beat["id"] for beat in draft["ad_script"]["beats"])
    affected = {shot["id"] for shot in draft["shots"] if any(part["beat_id"] in beat_ids for part in shot["coverage"])}
    neighbors = set()
    for index, shot in enumerate(draft["shots"]):
        if shot["id"] in affected:
            neighbors.update(draft["shots"][j]["id"] for j in (index - 1, index + 1) if 0 <= j < len(draft["shots"]))
    if not execution["state_continuity"]:
        # A corrected handoff may remove a repeated action in its neighbor's
        # description, not just change the declared start/end state. Expand
        # once from the original affected shots; shared beat coverage does not
        # recursively unlock further shots or the rest of the story.
        affected.update(neighbors)
        beat_ids.update(part["beat_id"] for shot in draft["shots"] if shot["id"] in affected
                        for part in shot["coverage"])
    beats = [beat["id"] for beat in draft["ad_script"]["beats"]]
    adjacent_beats = {beats[j] for i, beat in enumerate(beats) if beat in beat_ids
                      for j in (i - 1, i + 1) if 0 <= j < len(beats)} - beat_ids
    return {"beat_ids": sorted(beat_ids), "adjacent_state_beat_ids": sorted(adjacent_beats),
            "shot_ids": sorted(affected), "allow_summary_update": global_issue,
            "adjacent_state_shot_ids": sorted(neighbors - affected)}


def _beat_actions(draft, beat):
    return {"narration": beat["narration"], "pictures": [
        {"description": shot["description"], "continuity": {key: shot["continuity"][key]
            for key in ("start_state", "allowed_changes", "end_state", "entry_transition")}}
        for shot in draft["shots"] if any(part["beat_id"] == beat["id"] for part in shot["coverage"])]}


def _executed_content(draft):
    """Ignore beat/shot identities and estimates when checking a global merge."""
    return {"narration": "".join(beat["narration"] for beat in draft["ad_script"]["beats"]),
        "pictures": [{"description": shot["description"],
            "continuity": {key: shot["continuity"][key] for key in
                ("start_state", "allowed_changes", "end_state", "entry_transition")}}
            for shot in draft["shots"]]}


@dataclass(frozen=True)
class _ReviewedTimingAuthorization:
    """Server-owned estimate bounds, never part of the model repair payload."""

    before: dict
    minimum_ticks: dict


def _reviewed_timing_authorization(before, plan, review):
    """Allow only a silent, unchanged one-beat/one-shot estimate already reviewed.

    A bound negative review may authorize a passed shot while another shot needs
    repair. Its estimate is not a new verdict: the result must still be compiled
    and independently reviewed again. Missing or stale evidence grants nothing.
    """
    from .beat_timing import visual_ticks
    from .plan_review import PlanReview, validate_plan_review

    if not isinstance(review, PlanReview) or not review.reviewed_plan_sha256:
        return None
    try:
        checked = validate_plan_review(review, plan)
    except (ValueError, KeyError, TypeError):
        return None
    if (not checked.execution_check.state_continuity
            or not all(item.passed for item in checked.checks)
            or not checked.alignment.ad_expression_passed
            or not all(item.passed for item in checked.alignment.spans)):
        return None
    if any(before[key] != plan[key] for key in ("title", "visual_direction", "ad_script")):
        return None
    compiled = [(segment["id"], shot) for segment in plan["segments"] for shot in segment["shots"]]
    if [shot["id"] for _, shot in compiled] != [shot["id"] for shot in before["shots"]]:
        return None
    for authored, (_, actual) in zip(before["shots"], compiled):
        if (any(actual.get(key) != value for key, value in authored.items() if key != "coverage")
                or actual["beat_ids"] != [part["beat_id"] for part in authored["coverage"]]):
            return None
    checks = {(item.segment_id, item.shot_id): item for item in checked.alignment.shots}
    bounds = {}
    for beat in before["ad_script"]["beats"]:
        if beat["narration"] or beat["timing"]["time_transition"] != "continuous":
            continue
        covering = [(index, shot) for index, shot in enumerate(before["shots"])
                    if any(part["beat_id"] == beat["id"] for part in shot["coverage"])]
        if len(covering) != 1 or covering[0][1]["coverage"] != [{"beat_id": beat["id"], "weight": 1.0}]:
            continue
        index, shot = covering[0]
        segment_id, actual = compiled[index]
        check = checks[(segment_id, shot["id"])]
        pacing = check.pacing
        if (not check.passed or check.temporal_omission != "no_omission" or pacing is None
                or not pacing.action_timing_passed or not pacing.cut_necessity_passed
                or pacing.action_seconds_required <= 0):
            continue
        minimum = visual_ticks({"visual_seconds": pacing.action_seconds_required})
        # With no speech and one full-coverage shot, the authored visual window
        # cannot borrow another beat or a later speech reservation.
        if (minimum < visual_ticks(beat["timing"])
                and minimum <= round((actual["end"] - actual["start"]) * 10)):
            bounds[beat["id"]] = minimum
    return _ReviewedTimingAuthorization(deepcopy(before), bounds)


def _reviewed_timing_allows(before, after, beat_id, authorization):
    from .beat_timing import visual_ticks

    if (not isinstance(authorization, _ReviewedTimingAuthorization)
            or authorization.before != before or beat_id not in authorization.minimum_ticks
            or [beat["id"] for beat in before["ad_script"]["beats"]]
                != [beat["id"] for beat in after["ad_script"]["beats"]]
            or [(shot["id"], shot["coverage"]) for shot in before["shots"]]
                != [(shot["id"], shot["coverage"]) for shot in after["shots"]]):
        return False
    old = deepcopy(next(beat for beat in before["ad_script"]["beats"] if beat["id"] == beat_id))
    new = deepcopy(next(beat for beat in after["ad_script"]["beats"] if beat["id"] == beat_id))
    if visual_ticks(new["timing"]) < authorization.minimum_ticks[beat_id]:
        return False
    old["timing"].pop("visual_seconds")
    new["timing"].pop("visual_seconds")
    if old != new:
        return False
    index = next(i for i, shot in enumerate(before["shots"])
                 if any(part["beat_id"] == beat_id for part in shot["coverage"]))
    if before["shots"][index] != after["shots"][index]:
        return False
    for neighbor in (index - 1, index + 1):
        if 0 <= neighbor < len(before["shots"]):
            a, b = before["shots"][neighbor]["continuity"], after["shots"][neighbor]["continuity"]
            if any(a[key] != b[key] for key in ("start_state", "end_state", "entry_transition")):
                return False
    beats, revised_beats = before["ad_script"]["beats"], after["ad_script"]["beats"]
    beat_index = next(i for i, beat in enumerate(beats) if beat["id"] == beat_id)
    for neighbor in (beat_index - 1, beat_index + 1):
        if 0 <= neighbor < len(beats):
            a, b = beats[neighbor], revised_beats[neighbor]
            if (any(a.get(key) != b.get(key) for key in ("boundary_before", "continuity_group"))
                    or any(a["timing"][key] != b["timing"][key]
                           for key in ("start_state", "end_state", "time_transition"))):
                return False
    return True


def validate_local_refinement(before, after, scope, *, reviewed_timing=None):
    """Only reviewed content and necessary neighbor states may change."""
    from .beat_timing import beat_minimum_ticks
    for key in ("title", "visual_direction"):
        if before[key] != after[key]:
            raise ValueError("局部返修改动了未受影响的全片约定")
    if not scope.get("allow_summary_update", False):
        for key in _SUMMARY_FIELDS:
            if before["ad_script"][key] != after["ad_script"][key]:
                raise ValueError("局部返修改动了未受影响的广告主旨")
    restructure = scope.get("allow_beat_restructure") is True
    unsupported_beats = []
    unsupported_total = False
    if restructure:
        if (set(scope["beat_ids"]) != {beat["id"] for beat in before["ad_script"]["beats"]}
                or set(scope["shot_ids"]) != {shot["id"] for shot in before["shots"]}):
            raise ValueError("内容重组只允许在全部内容和镜头均受容量冲突影响时进行")
        if (sum(beat_minimum_ticks(beat) for beat in after["ad_script"]["beats"])
                < sum(beat_minimum_ticks(beat) for beat in before["ad_script"]["beats"])
                and _executed_content(before) == _executed_content(after)):
            unsupported_total = True
    for collection, allowed, identity in (("beats", set(scope["beat_ids"]), "id"),):
        old = before["ad_script"][collection]
        new = after["ad_script"][collection]
        if not restructure and [x[identity] for x in old] != [x[identity] for x in new]:
            raise ValueError("局部返修须保留内容节拍身份与顺序")
        new_by_id = {beat[identity]: beat for beat in new}
        for a in old:
            if a[identity] not in new_by_id:
                continue  # Only a complete, explicit capacity scope permits removal.
            b = new_by_id[a[identity]]
            if (beat_minimum_ticks(b) < beat_minimum_ticks(a)
                    and _beat_actions(before, a) == _beat_actions(after, b)
                    and not _reviewed_timing_allows(before, after, a[identity], reviewed_timing)):
                unsupported_beats.append(a[identity])
            if a[identity] in allowed:
                continue
            if a[identity] in scope.get("adjacent_state_beat_ids", []):
                a, b = deepcopy(a), deepcopy(b)
                for field in ("start_state", "end_state"):
                    a["timing"].pop(field, None)
                    b["timing"].pop(field, None)
            if a != b:
                raise ValueError("局部返修改动了未受影响的内容节拍")
    affected = set(scope["shot_ids"])
    allowed_coverage = ({beat["id"] for beat in after["ad_script"]["beats"]}
                        if restructure else set(scope["beat_ids"]))
    preserved = [shot["id"] for shot in before["shots"] if shot["id"] not in affected]
    if [shot["id"] for shot in after["shots"] if shot["id"] in preserved] != preserved:
        raise ValueError("局部返修须保留未受影响镜头身份与顺序")
    old_by_id = {shot["id"]: shot for shot in before["shots"]}
    for new in after["shots"]:
        if new["id"] not in preserved:
            if not {part["beat_id"] for part in new["coverage"]}.issubset(allowed_coverage):
                original = old_by_id.get(new["id"])
                # A mapping correction may retain the same shared shot without
                # unlocking its valid neighbor beat or any other neighbor shots.
                retains_mapping_coverage = (scope.get("allow_existing_mapping_coverage") is True
                    and original is not None and new["id"] in affected
                    and new["coverage"] == original["coverage"])
                if not retains_mapping_coverage:
                    raise ValueError("新增或合并镜头只能覆盖受影响内容")
            continue
        old = old_by_id[new["id"]]
        if old["id"] in scope["adjacent_state_shot_ids"]:
            old, new = deepcopy(old), deepcopy(new)
            for field in ("start_state", "end_state"):
                old["continuity"].pop(field, None)
                new["continuity"].pop(field, None)
        if old != new:
            raise ValueError("局部返修改动了未受影响镜头的动作或构图")
    # Do not hide an out-of-scope mutation behind a repairable estimate guard.
    if unsupported_beats or unsupported_total:
        raise TimingReductionError(unsupported_beats, whole_plan=unsupported_total)


def _review_issues(review, plan):
    """Stable failure identities and owned UI labels, never provider prose."""
    criteria = {"product_relevance": "商品关联", "audience_relevance": "受众关联",
        "factual_support": "商品事实依据", "audiovisual_complement": "声画互补",
        "natural_speech": "台词自然度", "narrative_progression": "叙事推进"}
    issues = {}
    occurrences = {}
    for item in review["checks"]:
        if not item["passed"]:
            issues[("content", item["criterion"])] = f"{criteria[item['criterion']]}仍需调整"
    for field, label in (("action_capacity", "动作与台词容量"), ("state_continuity", "前后状态衔接")):
        if not review["execution_check"][field]:
            issues[("execution", field)] = f"{label}仍需调整"
    alignment = review["alignment"]
    if not alignment["ad_expression_passed"]:
        issues[("alignment", "ad_expression")] = "整片商品表达仍需调整"
    for collection, identity, label in (("shots", "shot_id", "镜头"), ("spans", "span_id", "台词")):
        for item in alignment[collection]:
            if item["passed"]:
                continue
            segment_index = next(i for i, segment in enumerate(plan["segments"])
                                 if segment["id"] == item["segment_id"])
            objects = plan["segments"][segment_index]["shots" if collection == "shots" else "speech_spans"]
            index = next(i for i, obj in enumerate(objects) if obj["id"] == item[identity])
            # Shot IDs may change inside the allowed repair scope; beat coverage
            # identifies the affected story content across a merge or recut.
            subject = objects[index]
            beats = tuple(subject["beat_ids"]) if collection == "shots" else (subject["beat_id"],)
            pacing = item.get("pacing", {})
            categories = ([key for key in ("action_timing_passed", "cut_necessity_passed")
                           if pacing.get(key) is False] or ["content"])
            for category in categories:
                subject_key = (collection, *beats, category)
                occurrence = occurrences.get(subject_key, 0)
                occurrences[subject_key] = occurrence + 1
                key = (*subject_key, occurrence)
                issues[key] = f"第{segment_index + 1}段第{index + 1}个{label}的动作或声画安排仍需调整"
    return issues


def _refinement_content(draft):
    """Compare delivered content and timing, excluding authors' justifications."""
    return {"beats": [{key: deepcopy(value) for key, value in beat.items()
        if key in ("id", "narration", "boundary_before", "continuity_group")}
        | {"timing": {key: value for key, value in beat["timing"].items() if key != "action_plan"}}
        for beat in draft["ad_script"]["beats"]],
        "shots": [{key: deepcopy(value) for key, value in shot.items()
            if key not in ("id", "purpose", "evidence_ids", "continuity")}
            | {"continuity": {key: value for key, value in shot["continuity"].items()
                if key != "entry_reason"}}
            for shot in draft["shots"]]}


def _review_capacity_gaps(review, plan, windows):
    """Keep numeric progress even while the same shot still fails its timing."""
    gaps = {}
    beats = {segment["segment_id"]: segment["beats"] for segment in windows}
    for segment in plan["segments"]:
        checks = {item["shot_id"]: item for item in review["alignment"]["shots"]
                  if item["segment_id"] == segment["id"]}
        for shot in segment["shots"]:
            pacing = checks[shot["id"]]["pacing"]
            if pacing["action_timing_passed"]:
                continue
            # Beat windows are disjoint. Parallel work may use its actual
            # allocation; sequential work still cannot borrow speech time.
            available = 0
            for beat in beats[segment["id"]]:
                if beat["beat_id"] not in shot["beat_ids"]:
                    continue
                start, end = execution_action_bounds(beat)
                if start is not None and end is not None:
                    available += max(0, min(shot["end"], end) - max(shot["start"], start))
            key = tuple(shot["beat_ids"])
            gaps[key] = gaps.get(key, 0) + max(0, pacing["action_seconds_required"] - available)
    return gaps


def _accept_craft_issue(settings, request):
    """Keep the current plan. The note stays in diagnostics, not on a failure card."""
    try:
        from .providers.gateway import save_craft_accept_diagnostic
        names = []
        for key in (request or {}).get("issues") or []:
            name = key[1] if isinstance(key, tuple) and len(key) == 2 else key
            if isinstance(name, str):
                names.append(name)
        save_craft_accept_diagnostic(settings, names)
    except Exception:
        return False
    return True


def _capacity_improved(previous, current, before, after):
    if not previous or not current or not set(current).issubset(previous):
        return False
    if any(gap > previous[key] + 1e-7 for key, gap in current.items()):
        return False
    old_beats = {beat["id"]: beat for beat in before["ad_script"]["beats"]}
    new_beats = {beat["id"]: beat for beat in after["ad_script"]["beats"]}
    return any(gap < previous[key] - 1e-7 and any(
        _beat_actions(before, old_beats[beat]) != _beat_actions(after, new_beats[beat]) for beat in key)
        for key, gap in current.items())


def _restore_unsupported_estimates(before, revised, error):
    """Put unauthorized estimate cuts back when the rest of the edit can stand.

    A refinement that also changes real action or speech keeps those edits.
    Beats whose pictures and narration did not change return to the baseline
    visual estimate and speech overlap. The caller still compiles and reviews
    the restored draft; this never treats the restored numbers as approval.
    """
    restored = deepcopy(revised)
    before_beats = {beat["id"]: beat for beat in before["ad_script"]["beats"]}
    targets = set(error.beat_ids)
    if error.whole_plan:
        targets.update(before_beats)
    changed = False
    for beat in restored["ad_script"]["beats"]:
        original = before_beats.get(beat["id"])
        if (original is None or beat["id"] not in targets or "timing" not in beat
                or "timing" not in original
                or _beat_actions(before, original) != _beat_actions(restored, beat)):
            continue
        for field in ("visual_seconds", "speech_overlap"):
            if beat["timing"].get(field) != original["timing"].get(field):
                beat["timing"][field] = deepcopy(original["timing"][field])
                changed = True
    return restored if changed else None


def _sparse_format_error(error):
    return "稀疏返修" in str(error)


def _restore_selected_evidence(draft, strategy):
    """Put the selected direction's facts back onto a beat and its shots."""
    data = deepcopy(draft)
    script = data.get("ad_script")
    if not isinstance(script, dict):
        return None
    beats = script.get("beats")
    if not isinstance(beats, list) or not beats:
        return None
    selected = next(item for item in strategy.directions if item.id == strategy.selected_direction_id)
    required = [fact_id for fact_id in selected.evidence_ids if isinstance(fact_id, str) and fact_id]
    if not required:
        return None
    spoken = [beat for beat in beats if isinstance(beat, dict) and (beat.get("narration") or "").strip()]
    last = (spoken or [beat for beat in beats if isinstance(beat, dict)])[-1]
    from .planner_recovery import EVIDENCE_LIST_LIMIT, bound_server_list
    have = list(last.get("evidence_ids") or [])
    for fact_id in required:
        if fact_id not in have:
            have.append(fact_id)
    last["evidence_ids"] = bound_server_list(have, EVIDENCE_LIST_LIMIT)
    top = list(script.get("evidence_ids") or [])
    for fact_id in required:
        if fact_id not in top:
            top.append(fact_id)
    script["evidence_ids"] = bound_server_list(top, EVIDENCE_LIST_LIMIT)
    return seal_director_evidence(data)


def _shrink_overlong_narration(draft, strategy, locked=()):
    """Replace a model line that cannot fit the beat cap. Locked lines stay."""
    from .ad_form_playbook import speech_profile_for_strategy
    limit = speech_profile_for_strategy(strategy).max_beat_characters
    data = deepcopy(draft)
    beats = (data.get("ad_script") or {}).get("beats") or []
    locked_lines = {line.strip() for line in locked or () if isinstance(line, str) and line.strip()}
    changed = False
    for beat in beats:
        if not isinstance(beat, dict):
            continue
        narration = beat.get("narration") or ""
        if len(narration) <= limit:
            continue
        if narration.strip() in locked_lines:
            return None
        beat["narration"] = "看这里。" if limit >= 4 else ""
        changed = True
    return data if changed else None


def _reraise_terminal(error):
    """Credentials, cancellation and a changed revision still leave the job."""
    if error.__class__.__name__ in ("StaleRevision", "PlanningSessionError", "PlannerConfigurationError"):
        raise error


def seal_director_evidence(draft):
    """Copy each beat's facts onto the shots that already cover that beat."""
    data = deepcopy(draft)
    beats = {beat["id"]: list(beat.get("evidence_ids") or [])
             for beat in data.get("ad_script", {}).get("beats") or [] if isinstance(beat, dict)}
    for shot in data.get("shots") or []:
        if not isinstance(shot, dict):
            continue
        needed = []
        for part in shot.get("coverage") or []:
            if not isinstance(part, dict):
                continue
            for fact_id in beats.get(part.get("beat_id"), []):
                if fact_id not in needed:
                    needed.append(fact_id)
        from .planner_recovery import EVIDENCE_LIST_LIMIT, bound_server_list
        have = list(shot.get("evidence_ids") or [])
        for fact_id in needed:
            if fact_id not in have:
                have.append(fact_id)
        shot["evidence_ids"] = bound_server_list(have, EVIDENCE_LIST_LIMIT)
    return data


def _review_score(review):
    data = review.model_dump() if hasattr(review, "model_dump") else review
    if not isinstance(data, dict):
        return 0
    checks = data.get("checks") if isinstance(data.get("checks"), list) else []
    passed = sum(1 for item in checks if isinstance(item, dict) and item.get("passed") is True)
    failed = sum(1 for item in checks if isinstance(item, dict) and item.get("passed") is False)
    ready = 50 if data.get("ready") else 0
    alignment = data.get("alignment") if isinstance(data.get("alignment"), dict) else {}
    align_ready = 20 if alignment.get("ready") else 0
    suggestions = len(data.get("suggestions") or [])
    return ready + align_ready + passed * 5 - failed * 3 - suggestions


def _shot_descriptions(plan):
    shots = [shot for segment in (plan.get("segments") or []) for shot in (segment.get("shots") or [])
             if isinstance(segment, dict)]
    if not shots:
        shots = plan.get("shots") or []
    return [shot.get("description") or "" for shot in shots if isinstance(shot, dict)]


def is_placeholder_plan(plan):
    """The built-in two-line board. It is not a finished ad."""
    if not isinstance(plan, dict):
        return False
    beats = ((plan.get("ad_script") or {}).get("beats") or [])
    lines = [beat.get("narration") for beat in beats if isinstance(beat, dict) and (beat.get("narration") or "").strip()]
    if lines != ["看这里。", "就是这个。"]:
        return False
    descriptions = _shot_descriptions(plan)
    return bool(descriptions) and all("放在画面中间" in item for item in descriptions)


def is_degenerate_plan(plan):
    """A local template, or a board whose shots all say the same thing.

    The two-line placeholder is one case. A template filled with the draft's
    own lines is another: every shot still says the product sits in the middle.
    Identical shot text is the same failure even after an inner-voice cue is pasted on.
    """
    if not isinstance(plan, dict):
        return False
    if is_placeholder_plan(plan):
        return True
    descriptions = [item.strip() for item in _shot_descriptions(plan)]
    if len(descriptions) >= 2 and descriptions[0] and len(set(descriptions)) == 1:
        return True
    return bool(descriptions) and all("放在画面中间，光线稳定" in item for item in descriptions)


def choose_accepted_plan(candidates, skeleton=None):
    """Keep the best real plan. The skeleton is only for when nothing was produced."""
    reviewed, plain = [], []
    for item in candidates or []:
        if not isinstance(item, dict):
            continue
        plan = item.get("plan")
        if not isinstance(plan, dict) or is_degenerate_plan(plan):
            continue
        review = item.get("review") if item.get("review") is not None else plan.get("plan_review")
        checks = review.get("checks") if isinstance(review, dict) else None
        if isinstance(checks, list) and checks:
            reviewed.append((_review_score(review), plan))
        else:
            plain.append(plan)
    if reviewed:
        return max(reviewed, key=lambda item: item[0])[1]
    if plain:
        return plain[-1]
    return skeleton


def _draft_lines(draft):
    data = draft.model_dump() if hasattr(draft, "model_dump") else draft
    if not isinstance(data, dict):
        return []
    lines = []
    for beat in data.get("beats") or []:
        if isinstance(beat, dict) and isinstance(beat.get("narration"), str) and beat["narration"].strip():
            lines.append(beat["narration"].strip())
    return lines


def _mark_skeleton(raw):
    if isinstance(raw, dict):
        raw["_fallback_skeleton"] = True
    return raw


def fallback_director(strategy, locked_lines=()):
    """Last-resort storyboard after generation failure, not the creative bar.

    A plain product hold in a real place is only this compilable fallback.
    Direction generation still aims at the hybrid craft. It uses only facts
    and characters already on the selected direction.
    """
    selected = next(item for item in strategy.directions if item.id == strategy.selected_direction_id)
    evidence = list(dict.fromkeys(selected.evidence_ids))
    if not evidence:
        evidence = [fact.id for fact in strategy.brief.facts[:1]]
    characters = list(selected.production.characters) if selected.production else []
    speaker = ""
    if selected.production and selected.production.speech_mode == "character_dialogue":
        speaker = selected.production.speaker_id or (characters[0].id if characters else "")
    character_ids = [speaker] if speaker else ([characters[0].id] if characters else [])
    speech = selected.production.speech_mode if selected.production else "voiceover"
    lines = [line.strip() for line in locked_lines or () if isinstance(line, str) and line.strip()]
    if speech == "none":
        lines = lines or [""]
    else:
        while len(lines) < 2:
            lines.append("看这里。" if not lines else "就是这个。")
    lines = lines[:6]
    product = (strategy.brief.product_name or "商品").strip() or "商品"
    timing = {"visual_seconds": 2, "speech_overlap": True, "action_plan": "商品保持不动，供人看清",
              "start_state": "商品在画面里", "end_state": "商品在画面里", "time_transition": "continuous"}
    beats, shots = [], []
    for index, line in enumerate(lines):
        beats.append({"id": f"b{index}", "purpose": "看清商品", "message": "看清商品外观",
                      "visual_role": "商品留在画面里", "narration": "" if speech == "none" else line,
                      "evidence_ids": list(evidence), "timing": deepcopy(timing)})
        shots.append({"id": f"shot_{index}", "coverage": [{"beat_id": f"b{index}", "weight": 1}],
                      "description": f"{product}放在画面中间，光线稳定。", "product_visible": True,
                      "purpose": "看清商品", "evidence_ids": list(evidence), "character_ids": list(character_ids),
                      "continuity": {"start_state": "商品放在画面中间", "allowed_changes": "商品保持不动",
                                     "end_state": "商品放在画面中间", "camera_move": "中景，机位固定",
                                     "entry_transition": "opening" if index == 0 else "continuous",
                                     "entry_reason": "看清商品"}})
    return {"title": product[:20], "video_flow": "先看见商品，再停住。",
            "ad_script": {"core_message": "看清商品", "audience_takeaway": "记住商品",
                          "product_role": "商品出现在画面里", "evidence_ids": list(evidence), "beats": beats},
            "visual_direction": {"product_anchor_ids": list(evidence[:1]), "scene": "实际使用的场所",
                                 "lighting": "均匀自然光", "color_palette": "保留商品原色",
                                 "camera_style": "中景固定机位", "sound": "环境声很轻"},
            "shots": shots}


async def _recover_storyboard_schema(stage_call, director_system, data, assets, broken, strategy, duration, locked, progress):
    """One targeted repair, then one full regeneration, then a local storyboard."""
    feedback = "分镜结构不完整。opening、cut、scene_change必须写出start_state。值为空的多余字段不要写。只返回完整分镜。"

    def keep_going(error):
        _reraise_terminal(error)
        return True

    progress("repairing_storyboard")
    repair_payload = {**data, "stage": "storyboard_repair", "draft_storyboard": broken,
                      "validation_feedback": feedback, "repair_instruction": feedback}
    try:
        candidate = await stage_call(director_system, repair_payload, assets)
        canonical_director(candidate, strategy, duration_seconds=duration, locked_lines=locked)
        return candidate
    except Exception as error:
        if not keep_going(error):
            raise
    progress("storyboarding")
    regen_payload = {**data, "stage": "storyboard", "task_kind": "storyboard_regeneration",
                     "previous_storyboard": broken, "repair_instruction": feedback}
    try:
        candidate = await stage_call(director_system, regen_payload, assets)
        canonical_director(candidate, strategy, duration_seconds=duration, locked_lines=locked)
        return candidate
    except Exception as error:
        _reraise_terminal(error)
    return _mark_skeleton(fallback_director(strategy, locked))


async def progressive_plan(*, stage_call, on_progress, record_failure, strategy, draft,
                           draft_issues, duration, generation_strategy, assets, conversation,
                           merchant_request, current_plan, initial_brief=None, content_only=False,
                           speech_only=False, max_refinements=MAX_PLAN_REFINEMENTS,
                           refinement_format="sparse_or_full", diagnostic_settings=None,
                           skip_review=False, cta_target=None):
    from .av_review import review_execution_windows, review_speech_requirements, review_transition_boundaries
    from .beat_timing import script_capacity_budget
    from .line_quality import (
        apply_brand_claim_collapse, brand_stack_remaining, brand_visual_claim_overflow,
        drop_stale_story_lines, offending_lines, rewrite_director_if_needed, script_line_offenses,
        voice_identity_issues,
    )
    from .plan_review import plan_review_schema, lower_failed_plan_verdicts, validate_plan_review
    from .review_output import compact_review_schema, REVIEW_OUTPUT_VERSION
    from .review_recovery import review_validation_feedback, preserve_review_rejections

    def progress(phase):
        if on_progress:
            on_progress(phase)

    from .production import align_selected_story_voice
    strategy = align_selected_story_voice(strategy)
    selected = next(d for d in strategy.directions if d.id == strategy.selected_direction_id)
    profile = speech_profile_for_strategy(strategy)
    tenth = profile.characters_per_tenth
    locked = fixed_speech(initial_brief if initial_brief is not None else merchant_request,
                          conversation, current_plan)
    director_request_schema = apply_beat_narration_cap(director_schema(initial=True), profile.max_beat_characters)
    data = {"stage": "storyboard", "schema": director_request_schema,
        "authoring_mode": "joint" if draft is None else "coordinate",
        "brief": strategy.brief.model_dump(), "selected_direction": selected.model_dump(),
        "speech_requirements": review_speech_requirements({
            "production": selected.production.model_dump() if selected.production else None}),
        "duration_seconds": duration, "generation_strategy": generation_strategy,
        "generation_rules": "沿用服务端连续性分组：每请求2–30秒；30秒内优先单请求；长片只在完整动作/台词边界分组。镜头数、内容节拍数和请求数不同。" if generation_strategy == "continuity_v1" else "历史生成分组每段最多10秒。",
        "merchant_request": merchant_request, "conversation": conversation, "fixed_speech": locked,
        "speech_budget": {"characters_per_second": profile.characters_per_second,
                          "max_beat_characters": profile.max_beat_characters,
                          "basis": "工程上限，不是实测语速；动作与说话串并行由实际内容决定"}}
    if content_only:
        data["draft_format"] = "content_only"
    if speech_only:
        from .speech_draft import SPEECH_DRAFT_VERSION
        data.update(draft_format="speech_only", speech_draft_version=SPEECH_DRAFT_VERSION,
                    draft_speech=deepcopy(draft))
    elif draft is not None:
        data["draft_script"] = draft.model_dump() if isinstance(draft, AdScript) else deepcopy(draft)
        data["draft_validation_issues"] = draft_issues
    if not content_only and not speech_only:
        try:
            data["draft_capacity"] = script_capacity_budget(data["draft_script"], duration, tenth)
            data["draft_capacity_basis"] = "按初稿估计计算的串并行最低容量，不是预锁窗口或动作已可执行的证明。超过目标须精简实际动作或非固定台词、明确时间省略，不只降低估计。最终仍按协调后的稿件重新分配时间。"
        except (ValueError, TypeError, KeyError):
            # Reviewable draft errors must not acquire invented estimates.
            pass
    progress("storyboarding")
    director_system = DIRECTOR_SYSTEM
    if speech_only:
        director_system += "\ndraft_format=speech_only时，draft_speech只有完整文案，没有既定内容拍、画面或秒点；保留其话语作用，沿已选方向首次编排完整内容与镜头，可以为实际动作和时长协调未固定措辞，不得照读创作说明或改变用户固定台词。\n"
    raw = None
    storyboard_errors = 0
    while raw is None and storyboard_errors < 3:
        try:
            raw = await stage_call(director_system, data, assets)
        except Exception as error:
            _reraise_terminal(error)
            record_failure({"nonblocking": True, "message": type(error).__name__, "issues": ["storyboard"]})
            # Empty or reasoning-only replies already retried once at low effort.
            if getattr(error, "contentless", False) or getattr(error, "syntax", False):
                break
            storyboard_errors += 1

    def brand_hold(lines):
        """Fallback board whose product-ad voiceover already keeps one claim."""
        director = fallback_director(strategy, lines)
        folded, _changed = apply_brand_claim_collapse(director, strategy, locked, duration)
        return folded

    def stacked_plan(plan):
        if not isinstance(plan, dict):
            return False
        script = plan.get("ad_script") or {}
        if (brand_stack_remaining(script, strategy, locked, plan, duration)
                or brand_visual_claim_overflow(plan, strategy, locked, duration)):
            return True
        spoken = []
        for segment in plan.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            for span in segment.get("speech_spans") or []:
                if not isinstance(span, dict):
                    continue
                text = span.get("text")
                if isinstance(text, str) and text.strip():
                    spoken.append({
                        "id": span.get("beat_id") or span.get("id") or f"span_{len(spoken)}",
                        "narration": text,
                    })
        return bool(spoken) and brand_stack_remaining(
            {"beats": spoken}, strategy, locked, plan, duration)

    def unfinished_board():
        raise ProgressivePlanningError(
            "分镜没有完成，占位稿不是完成的方案。已有方案和素材已保留，尚未开始新的图片或视频生成",
            issue_summary=["占位稿不是完成的方案"], repair_attempts=repairs)

    def usable(plan):
        return isinstance(plan, dict) and bool(plan.get("segments")) and not is_degenerate_plan(plan)

    def release(plan):
        if is_degenerate_plan(plan):
            record_failure({"nonblocking": True,
                            "message": "分镜阶段没有留下可用内容，占位稿不是完成的方案",
                            "issues": ["fallback_skeleton"], "error_code": "fallback_skeleton"})
            unfinished_board()
        if stacked_plan(plan):
            record_failure(plan)
            raise ProgressivePlanningError(
                "产品片旁白还在堆卖点，这次不采用。已有方案和素材已保留，尚未开始新的图片或视频生成",
                issue_summary=["产品片旁白还在堆卖点"], repair_attempts=repairs)
        from .story_logic import settle_inner_monologue, story_plan_issues
        story_issues = story_plan_issues(plan, strategy)
        if story_issues:
            record_failure(plan)
            raise ProgressivePlanningError(
                story_issues[0] + "，这次不采用。已有方案和素材已保留，尚未开始新的图片或视频生成",
                issue_summary=story_issues, repair_attempts=repairs)
        notes = settle_inner_monologue(plan)
        if notes:
            _rebind_reviewed_plan(plan)
            record_failure({"nonblocking": True, "message": notes[0],
                            "issues": notes[:8], "error_code": "inner_monologue"})
        return plan

    if raw is None:
        record_failure({"nonblocking": True,
                        "message": "分镜阶段没有留下可用内容，占位稿不是完成的方案",
                        "issues": ["fallback_skeleton"], "error_code": "fallback_skeleton"})
        raise ProgressivePlanningError(
            "分镜没有完成，占位稿不是完成的方案。已有方案和素材已保留，尚未开始新的图片或视频生成",
            issue_summary=["占位稿不是完成的方案"])
    repairs = 0
    last_valid_plan = None
    real_candidates = []
    skeleton_logged = False

    def remember(plan, review=None):
        if not isinstance(plan, dict) or is_degenerate_plan(plan):
            return
        script = (plan or {}).get("ad_script") or {}
        from .story_logic import story_plan_issues
        if (brand_stack_remaining(script, strategy, locked, duration_seconds=duration)
                or brand_visual_claim_overflow(plan, strategy, locked, duration)
                or voice_identity_issues(strategy, plan.get("production"))
                or story_plan_issues(plan, strategy)):
            return
        stamped = deepcopy(plan)
        score_review = None
        if review is not None:
            score_review = review.model_dump() if hasattr(review, "model_dump") else deepcopy(review)
            # A rejected review can quote model text. Keep it for scoring only.
            if isinstance(score_review, dict) and score_review.get("ready") is True:
                stamped["plan_review"] = score_review
            else:
                stamped.pop("plan_review", None)
        real_candidates.append({
            "plan": stamped,
            "review": score_review if isinstance(score_review, dict) and score_review.get("checks") else stamped.get("plan_review"),
        })

    def refuse_mismatched_voice(plan):
        if voice_identity_issues(strategy, (plan or {}).get("production")):
            record_failure(plan)
            raise ProgressivePlanningError(
                "声音方式和广告类型对不上，这次不采用。已有方案和素材已保留，尚未开始新的图片或视频生成",
                issue_summary=["声音方式和广告类型对不上"], repair_attempts=repairs)

    def refuse_story_structure(plan):
        from .story_logic import story_plan_issues
        issues = story_plan_issues(plan, strategy)
        if not issues:
            return
        record_failure(plan)
        raise ProgressivePlanningError(
            issues[0] + "，这次不采用。已有方案和素材已保留，尚未开始新的图片或视频生成",
            issue_summary=issues, repair_attempts=repairs)

    def best_real():
        return choose_accepted_plan(real_candidates)

    def log_skeleton():
        nonlocal skeleton_logged
        if skeleton_logged:
            return
        skeleton_logged = True
        record_failure({"nonblocking": True,
                        "message": "分镜阶段没有留下可用内容，占位稿不是完成的方案",
                        "issues": ["fallback_skeleton"], "error_code": "fallback_skeleton"})
    previous_review = None
    previous_issues = None
    previous_capacity, previous_draft = None, None
    previous_timing_overrun = None
    evidence_mapping_repair_used = False
    dialogue_polish_used = False
    review_response_repair_used = False
    jargon_rewrite_used = False
    purpose_scrub_used = False
    background_stamp_used = False
    background_rewrite_used = False
    shot_craft_used = False
    craft_refine_used = False
    craft_accepted = False
    schema_recovery_used = False
    fallback_installed = False
    voice_board_regenerated = False
    selected_evidence_restored = False
    narration_shrunk = False
    state_warnings_recorded = False
    wardrobe_warnings_recorded = False
    clothing_notes_recorded = False
    product_sheet_notes_recorded = False
    last_align_signature = None
    stale_lines_used = False
    prop_review_repaired = False

    class _PlanAccepted(Exception):
        def __init__(self, plan):
            self.plan = plan

    async def stop(message, issues, *, error_code="planning_content_failed", accept=True):
        from .planner_recovery import needs_merchant
        if needs_merchant(message, issues):
            raise ProgressivePlanningError(message + "。已有方案和素材已保留，尚未开始新的图片或视频生成",
                issue_summary=list(issues.values()), repair_attempts=repairs,
                error_code=error_code) from None
        if error_code == "planning_review_incomplete":
            chosen = best_real()
            if not usable(chosen) and usable(last_valid_plan):
                chosen = last_valid_plan
            if usable(chosen):
                cleaned = deepcopy(chosen)
                cleaned.pop("plan_review", None)
                record_failure({"nonblocking": True, "message": message,
                                "issues": list(issues.values())[:8], "error_code": error_code})
                raise _PlanAccepted(cleaned)
        record_failure({"nonblocking": True, "message": message,
                        "issues": list(issues.values())[:8], "error_code": error_code})
        # A failed revision stays inside refine(). The caller keeps the plan
        # it already compiled, or seals the mapping, then continues the review.
        if not accept:
            raise RevisionKept from None
        chosen = best_real()
        if not usable(chosen) and usable(last_valid_plan):
            chosen = last_valid_plan
        if not usable(chosen):
            raise ProgressivePlanningError(
                message + "。已有方案和素材已保留，尚未开始新的图片或视频生成",
                issue_summary=list(issues.values()) or ["占位稿不是完成的方案"],
                repair_attempts=repairs, error_code=error_code)
        raise _PlanAccepted(chosen)

    async def refine(before, scope, feedback, issues, *, plan=None, schedule=None, bound_review=None):
        nonlocal repairs
        # The next author may build on a rejected candidate, but that candidate
        # never replaces the original baseline for scope or estimate guards.
        current = before
        initial_feedback = feedback
        format_retried = False
        call_retried = False
        force_full = False
        reviewed_timing = (_reviewed_timing_authorization(before, plan, bound_review)
                           if plan is not None and bound_review is not None else None)
        limit_names = {0: "零轮", 1: "一轮", 2: "两轮"}
        refine_steps = 0
        while True:
            refine_steps += 1
            if refine_steps > max_refinements + 3:
                await stop("自动调整次数已到上限，已停止重复尝试", issues, accept=False)
            if repairs >= max_refinements:
                await stop(f"{limit_names.get(max_refinements, str(max_refinements) + '轮')}自动调整后，本次方案仍有未解决的内容或动作问题", issues, accept=False)
            progress("refining_storyboard")
            repairs += 1
            refinement = {**data, "stage": "plan_refinement",
                "schema": apply_beat_narration_cap(refinement_schema(scope), profile.max_beat_characters),
                "draft_storyboard": current,
                "repair_capacity": script_capacity_budget(current["ad_script"], duration, tenth),
                "repair_capacity_basis": "这是当前draft_storyboard的逐拍和全片最低容量，不是初稿预算，也不是动作已经可执行的证明。改动后须重新计算：speech_overlap=true按动作与完整台词所需时长较大者计；false按两者相加。全片最低容量不得超过target_seconds，还须满足生成分段与实际动作要求。新增动作或延长台词会占用现有余量；超过容量须精简真实内容或有依据地省略过程，不能仅下调估时。",
                "review_feedback": feedback, "repair_scope": scope,
                "repair_attempt": repairs, "repair_attempt_limit": max_refinements,
                "repair_rules": "只输出本次schema的可编辑字段。title、visual_direction以及未允许更新的三项摘要由程序保留原值，不重写。一次处理全部已知问题，仅修改允许范围及必要相邻状态。保留其他内容和用户固定要求；不能只修改purpose、action_plan或审核说明。完成后程序重排时间并独立完整复核。"}
            sparse_allowed = sparse_refinement_compatible(current, scope) and not force_full
            refinement_system = DIRECTOR_SYSTEM
            if sparse_allowed:
                sparse_schema = sparse_refinement_schema(refinement["schema"], current, scope)
                use_sparse_only = (refinement_format == "sparse"
                                   and scope.get("allow_beat_restructure") is not True)
                if use_sparse_only:
                    patch_only = deepcopy(sparse_schema["oneOf"][0])
                    patch_only["$defs"] = sparse_schema["$defs"]
                    refinement["schema"] = patch_only
                    refinement["refinement_output_format"] = "sparse"
                else:
                    refinement["schema"] = sparse_schema
                    refinement["refinement_output_format"] = "sparse_or_full"
                refinement["refinement_output_version"] = REFINEMENT_OUTPUT_VERSION
                refinement_system += "\n" + SPARSE_REFINEMENT_INSTRUCTION
            if plan is not None and current == before:
                refinement["final_plan"] = review_projection(plan)
                if schedule is not None:
                    refinement["current_execution_windows"] = review_execution_windows(schedule, plan["segments"])
                    refinement["current_execution_window_basis"] = "仅对应当前draft_storyboard的已编排时窗，供定位问题和保留已通过内容；改稿后由程序重排，不能沿用这些旧秒点声称通过。并行动作可用本拍实际beat窗口与镜头的交集；非并行动作仍只用visual窗口，不能借用speech窗口或相邻拍时间。"
            try:
                candidate = await stage_call(refinement_system, refinement, assets)
            except Exception as error:
                _reraise_terminal(error)
                if getattr(error, "contentless", False) or getattr(error, "syntax", False):
                    record_failure({"nonblocking": True, "message": type(error).__name__, "issues": ["plan_refinement"]})
                    raise RevisionKept from None
                if not call_retried:
                    call_retried = True
                    repairs -= 1
                    continue
                record_failure({"nonblocking": True, "message": type(error).__name__, "issues": ["plan_refinement"]})
                raise RevisionKept from None
            try:
                if isinstance(candidate, dict) and "patch" in candidate:
                    if force_full:
                        raise ValueError("稀疏返修不能替代完整返修")
                    candidate = compose_sparse_refinement(candidate, current, scope)
                revised = canonical_director(compose_refinement(candidate, before, scope), strategy,
                                             duration_seconds=duration, locked_lines=locked).model_dump()
                validate_script_timing(revised["ad_script"], required=True)
                validate_local_refinement(before, revised, scope, reviewed_timing=reviewed_timing)
            except TimingReductionError as error:
                # An unauthorized estimate cut beside real edits does not need
                # another model round when restoring those numbers still compiles.
                restored = _restore_unsupported_estimates(before, revised, error)
                if restored is not None:
                    try:
                        validate_local_refinement(before, restored, scope, reviewed_timing=reviewed_timing)
                        await asyncio.to_thread(
                            compile_director, restored, strategy, duration, generation_strategy,
                            [a["id"] for a in assets], locked, cta_target)
                    except (TimingReductionError, ValueError, KeyError, TypeError):
                        restored = None
                    else:
                        mapping_improved = (initial_feedback.get("kind") == "evidence_mapping"
                            and _evidence_mapping_gaps(restored) < _evidence_mapping_gaps(before))
                        if _refinement_content(before) != _refinement_content(restored) or mapping_improved:
                            return restored
                # Gather the other known capacity conflict now, rather than
                # spending the last repair on one estimate and discovering an
                # overrun afterwards. Authored identity/lock errors still stop.
                capacity_issue = None
                try:
                    await asyncio.to_thread(
                        compile_director, revised, strategy, duration, generation_strategy,
                        [a["id"] for a in assets], locked, cta_target)
                except ValueError as compile_error:
                    if not _is_capacity_error(compile_error):
                        text = str(compile_error)
                        if "用户明确固定的台词" in text or "用户固定台词" in text:
                            detail = {("fixed_speech",): "用户固定台词未完整保留"}
                        elif "无人声" in text:
                            detail = {("speech",): "已选无人声方向不能增加台词"}
                        else:
                            detail = {("contract",): "方案结构、商品依据或已确认内容需要调整"}
                        record_failure(plan)
                        # This is an authored lock or identity break, not a timing miss.
                        # Raising here keeps that reason; a capacity retry would hide it
                        # and must not fill in a placeholder board.
                        raise ProgressivePlanningError(
                            "本次方案未能满足已确认要求，暂未采用。已有方案和素材已保留，尚未开始新的图片或视频生成",
                            issue_summary=list(detail.values()), repair_attempts=repairs)
                    capacity_issue = str(compile_error)
                beat_positions = {beat["id"]: index + 1 for index, beat in enumerate(before["ad_script"]["beats"])}
                guard_issues = {("estimate", beat_id): f"第{beat_positions[beat_id]}个内容段的实际动作未变，不能仅降低估时"
                                for beat_id in error.beat_ids}
                if error.whole_plan:
                    guard_issues[("estimate",)] = "实际全片内容未变，不能仅降低总估时"
                issues = {**issues, **guard_issues}
                if repairs >= max_refinements:
                    record_failure(plan)
                    await stop("自动调整后动作与台词容量仍未改善，已停止重复尝试", issues, accept=False)
                original_capacity = script_capacity_budget(before["ad_script"], duration)
                candidate_capacity = script_capacity_budget(revised["ad_script"], duration)
                original_budgets = {item["beat_id"]: item for item in original_capacity["beats"]}
                candidate_budgets = {item["beat_id"]: item for item in candidate_capacity["beats"]}
                feedback = {"kind": "unsupported_timing_reduction",
                    "issue": "以下实际动作与台词未精简，估时降低无依据；候选尚未通过，不可沿用这些数字宣称完成。",
                    "beats": [{"beat_id": beat_id, "baseline": original_budgets[beat_id],
                               "candidate": candidate_budgets[beat_id]} for beat_id in error.beat_ids],
                    "unchanged_whole_plan": error.whole_plan,
                    "baseline_capacity": original_capacity, "capacity": candidate_capacity,
                    "previous_feedback": initial_feedback,
                    "repair_contract": "保留候选中已完成的实际修正，逐项精简这些节拍的真实动作、台词或明确省略；不只改数字、action_plan或解释。校验仍以本次返修前的原稿为基准，不以已拒绝候选为基准。visual_seconds不含整句说话时长；不能并行时程序会另加完整语音时长。"}
                if capacity_issue is not None:
                    feedback["capacity_issue"] = capacity_issue
                    scope = capacity_repair_scope(before)
                current = revised
                continue
            except ProgressivePlanningError as error:
                record_failure(plan)
                raise ProgressivePlanningError(str(error), issue_summary=list(issues.values()),
                    repair_attempts=repairs, error_code=error.error_code) from None
            except (ValueError, KeyError, TypeError) as error:
                if _sparse_format_error(error) and not format_retried:
                    format_retried = True
                    force_full = True
                    repairs -= 1
                    continue
                if _sparse_format_error(error):
                    raise RevisionKept from None
                record_failure(plan)
                await stop("本次调整未能保留已确认内容或未受影响的方案，已保留调整前的成果", issues, accept=False)
            mapping_improved = (initial_feedback.get("kind") == "evidence_mapping"
                and _evidence_mapping_gaps(revised) < _evidence_mapping_gaps(before))
            if _refinement_content(before) == _refinement_content(revised) and not mapping_improved:
                record_failure(plan)
                await stop("自动调整没有产生有效的内容变化，已停止重复尝试", issues, accept=False)
            return revised

    director_passes = 0
    try:
        while True:
            director_passes += 1
            if director_passes > MAX_DIRECTOR_PASSES:
                await stop("分镜校正重复过多次，已停止", {("contract",): "分镜校正没有收敛"})
            current_is_skeleton = isinstance(raw, dict) and bool(raw.pop("_fallback_skeleton", False))
            try:
                plan, schedule, coordinated = await asyncio.to_thread(
                    compile_director, raw, strategy, duration, generation_strategy,
                    [a["id"] for a in assets], locked, cta_target)
            except (ValueError, KeyError, TypeError) as error:
                # A known reference-to-shot mapping error can use one remaining
                # repair. A storyboard that cannot even be read is repaired inside
                # the job. Nothing is auto-filled and actual work is fully reviewed.
                try:
                    before = canonical_director(raw, strategy, duration_seconds=duration, locked_lines=locked).model_dump()
                except (ValidationError, KeyError, TypeError) as shape_error:
                    # Shape problems stay inside the job: one repair, one regeneration,
                    # then a local storyboard. Dropped facts and cast are not shape.
                    if _is_schema_shape_error(shape_error):
                        if not schema_recovery_used:
                            schema_recovery_used = True
                            raw = await _recover_storyboard_schema(
                                stage_call, director_system, data, assets, raw, strategy, duration, locked, progress)
                            continue
                        if not fallback_installed:
                            chosen = best_real()
                            if usable(chosen):
                                raise _PlanAccepted(chosen)
                            log_skeleton()
                            fallback_installed = True
                            await stop("分镜没有完成，占位稿不是完成的方案",
                                       {("contract",): "占位稿不是完成的方案"})
                    record_failure(None)
                    await stop("本次方案结构不完整，暂未采用", {("contract",): "方案结构、商品依据或已确认内容需要调整"})
                except ValueError:
                    record_failure(None)
                    await stop("本次方案结构不完整，暂未采用", {("contract",): "方案结构、商品依据或已确认内容需要调整"})
                text = str(error)
                if "说话窗口须包含已确认的说话角色" in text and not voice_board_regenerated:
                    # A replayed voiceover board has no speaker in the talking shots.
                    # Ask for the storyboard again instead of filling in the template.
                    voice_board_regenerated = True
                    progress("storyboarding")
                    regen_payload = {**data, "task_kind": "storyboard_regeneration",
                        "repair_instruction": "声音是这个主角自己的对白。每个说话的镜头都要出现这个角色，不要写成没有人的产品摆拍。只返回完整分镜。"}
                    try:
                        raw = await stage_call(director_system, regen_payload, assets)
                    except Exception as regen_error:
                        _reraise_terminal(regen_error)
                        record_failure(None)
                        await stop("分镜没有完成，占位稿不是完成的方案",
                                   {("contract",): "占位稿不是完成的方案"})
                    continue
                if "无人声" in text:
                    record_failure(None)
                    await stop("本次方案未能满足已确认要求，暂未采用",
                         {("speech",): "已选无人声方向不能增加台词"})
                if "用户明确固定的台词" in text or "用户固定台词" in text:
                    record_failure(None)
                    await stop("本次方案未能满足已确认要求，暂未采用",
                         {("fixed_speech",): "用户固定台词未完整保留"})
                if "选中创意的商品依据必须落实" in text and not selected_evidence_restored:
                    selected_evidence_restored = True
                    restored = _restore_selected_evidence(before, strategy)
                    if restored is not None:
                        raw = restored
                        continue
                if "单拍台词超过" in text and not narration_shrunk:
                    narration_shrunk = True
                    shrunk = _shrink_overlong_narration(before, strategy, locked)
                    if shrunk is not None:
                        raw = shrunk
                        continue
                mapping_scope = _evidence_mapping_scope(error, before, strategy)
                if mapping_scope is not None:
                    record_failure(None)
                    gaps = sorted(_evidence_mapping_gaps(before))
                    issues = {("evidence_mapping", beat_id): "内容段与对应镜头的商品依据需要调整"
                        for beat_id, _ in gaps}
                    if evidence_mapping_repair_used:
                        raw = seal_director_evidence(before)
                        continue
                    evidence_mapping_repair_used = True
                    feedback = {"kind": "evidence_mapping", "issue": _EVIDENCE_MAPPING_ERROR,
                        "missing_mappings": [{"beat_id": beat_id, "fact_id": fact_id} for beat_id, fact_id in gaps],
                        "repair_contract": "依据实际内容检查引用：无关依据从该拍移除；实际使用的已有依据落实到覆盖该拍的镜头。不能只机械补ID、补造画面或加入新事实。"}
                    if previous_review is not None:
                        feedback["previous_review"] = previous_review
                    try:
                        raw = await refine(before, mapping_scope, feedback, issues)
                    except RevisionKept:
                        raw = seal_director_evidence(before)
                    continue
                # Other schema/ownership errors and missing locks stop.
                repairable = _is_capacity_error(error)
                record_failure(None)
                issues = {("timing",): "动作与完整台词无法放入所选时长或生成分段"}
                if not repairable:
                    if "用户明确固定的台词" in str(error):
                        issues = {("fixed_speech",): "用户固定台词未完整保留"}
                    else:
                        issues = {("contract",): "方案结构、商品依据或已确认内容需要调整"}
                    await stop("本次方案未能满足已确认要求，暂未采用", issues)
                # A global packing conflict couples all of these content windows;
                # the original story/visual contract remains protected separately.
                scope = capacity_repair_scope(before)
                feedback = {"kind": "deterministic_timing", "issue": str(error),
                            "capacity": script_capacity_budget(before["ad_script"], duration, tenth)}
                overrun = feedback["capacity"]["overrun_seconds"]
                if previous_timing_overrun is not None and overrun >= previous_timing_overrun:
                    await stop("自动调整后动作与台词容量仍未改善，已停止重复尝试", issues)
                previous_timing_overrun = overrun
                if previous_review is not None:
                    feedback["previous_review"] = previous_review
                    feedback["previous_review_basis"] = "上一稿完整审核结果；结合当前稿及容量冲突汇总仍未解决的问题，已经修好的内容须保留。"
                    issues.update(previous_issues)
                try:
                    raw = await refine(before, scope, feedback, issues)
                except RevisionKept:
                    await stop("自动调整后动作与台词容量仍未改善，已停止重复尝试", issues)
                continue
            if current_is_skeleton:
                chosen = best_real()
                if usable(chosen):
                    raise _PlanAccepted(chosen)
                log_skeleton()
                await stop("分镜没有完成，占位稿不是完成的方案",
                           {("contract",): "占位稿不是完成的方案"})
            spoken = coordinated.get("ad_script") or {}
            # One local pass, including a fallback board. A second benefit is
            # blanked before any model rewrite and before this draft can be saved.
            folded, did_fold = apply_brand_claim_collapse(
                coordinated, strategy, locked, duration)
            if did_fold:
                raw = folded
                continue
            if not stale_lines_used:
                stale_lines_used = True
                dropped, did_drop = drop_stale_story_lines(coordinated, strategy, locked)
                if did_drop:
                    raw = dropped
                    continue
            # Per-line jargon stays independent of the strategy. Cross-line craft
            # still sees the ad type, without opening the product-reference check.
            if not jargon_rewrite_used and (
                offending_lines(spoken, locked_lines=locked)
                or script_line_offenses(spoken, strategy, locked, plan)
            ):
                progress("writing_script")
                called, replacement = await rewrite_director_if_needed(
                    stage_call, coordinated, strategy, duration, locked, plan=plan)
                if called:
                    jargon_rewrite_used = True
                if replacement is not None:
                    raw = replacement
                    continue
            if not purpose_scrub_used:
                from .story_logic import scrub_story_purposes
                purpose_scrub_used = True
                scrubbed, did_scrub = scrub_story_purposes(coordinated, strategy)
                if did_scrub:
                    raw = scrubbed
                    continue
            if (brand_stack_remaining(spoken, strategy, locked, plan, duration)
                    or brand_visual_claim_overflow(coordinated, strategy, locked, duration)):
                record_failure(plan)
                raise ProgressivePlanningError(
                    "产品片旁白还在堆卖点，这次不采用。已有方案和素材已保留，尚未开始新的图片或视频生成",
                    issue_summary=["产品片旁白还在堆卖点"], repair_attempts=repairs)
            if not background_stamp_used:
                from .story_logic import stamp_background_lines
                background_stamp_used = True
                stamped, did_stamp = stamp_background_lines(coordinated)
                if did_stamp:
                    raw = stamped
                    continue
            if not background_rewrite_used:
                from .story_logic import background_issues, rewrite_background_if_needed
                if background_issues(plan):
                    background_rewrite_used = True
                    _called, replacement = await rewrite_background_if_needed(stage_call, coordinated, plan)
                    if replacement is not None:
                        background_stamp_used = False
                        raw = replacement
                        continue
            if not current_is_skeleton:
                refuse_mismatched_voice(plan)
                refuse_story_structure(plan)
                remember(plan)
                last_valid_plan = plan
            if not shot_craft_used:
                from .shot_craft import rewrite_shots_if_needed, shot_rewrite_issues
                if shot_rewrite_issues(coordinated, plan):
                    progress("storyboarding")
                    shot_craft_used = True
                    called, replacement = await rewrite_shots_if_needed(stage_call, coordinated, plan, strategy)
                    if called and replacement is not None:
                        try:
                            await asyncio.to_thread(
                                compile_director, replacement, strategy, duration, generation_strategy,
                                [a["id"] for a in assets], locked, cta_target)
                        except (ValueError, KeyError, TypeError):
                            replacement = None
                        else:
                            raw = replacement
                            continue
            from .shot_craft import craft_refine_request
            craft_request = craft_refine_request(plan, strategy)
            if craft_request is not None and not craft_refine_used and not skip_review:
                craft_refine_used = True
                try:
                    revised = await refine(coordinated, craft_request["scope"], craft_request["feedback"],
                                           craft_request["issues"])
                except (ProgressivePlanningError, RevisionKept):
                    revised = None
                if revised is None:
                    craft_accepted = _accept_craft_issue(diagnostic_settings, craft_request) or craft_accepted
                else:
                    try:
                        await asyncio.to_thread(
                            compile_director, revised, strategy, duration, generation_strategy,
                            [a["id"] for a in assets], locked, cta_target)
                    except (ValueError, KeyError, TypeError):
                        craft_accepted = _accept_craft_issue(diagnostic_settings, craft_request) or craft_accepted
                    else:
                        raw = revised
                        continue
            elif craft_request is not None and craft_refine_used and not craft_accepted:
                craft_accepted = _accept_craft_issue(diagnostic_settings, craft_request) or craft_accepted
            if skip_review:
                return release(plan)
            from .story_logic import (
                align_declared_continuity, repair_prop_handoff, state_change_warnings, wardrobe_warnings,
            )
            aligned, did_align = align_declared_continuity(coordinated)
            board = aligned if did_align else coordinated
            repaired, did_prop = repair_prop_handoff(board)
            if did_align or did_prop:
                signature = _continuity_signature(repaired)
                # A pass that rewrites and then lands on the same states is not
                # compiled again. A pass that still differs from the board in
                # hand is compiled once more, inside the same cap.
                if signature != last_align_signature or signature != _continuity_signature(coordinated):
                    last_align_signature = signature
                    raw = repaired
                    continue
            if not state_warnings_recorded:
                notes = state_change_warnings(plan)
                if notes:
                    state_warnings_recorded = True
                    record_failure({"nonblocking": True,
                                    "message": "有一条状态变化对不上镜头或角色，已放下，不猜测。",
                                    "issues": notes[:8]})
            if not wardrobe_warnings_recorded:
                clothes = wardrobe_warnings(plan)
                if clothes:
                    wardrobe_warnings_recorded = True
                    record_failure({"nonblocking": True,
                                    "message": "镜头里的衣服没有写进角色外观，已记下，不因此停下。",
                                    "issues": clothes[:8]})
            if not clothing_notes_recorded:
                from .design import state_sheet_clothing_notes
                sheet_notes = state_sheet_clothing_notes(plan)
                if sheet_notes:
                    clothing_notes_recorded = True
                    record_failure({"nonblocking": True,
                                    "message": sheet_notes[0],
                                    "issues": sheet_notes[:8]})
            if not product_sheet_notes_recorded:
                from .design import character_sheet_product_notes
                product_notes = character_sheet_product_notes(plan)
                if product_notes:
                    product_sheet_notes_recorded = True
                    record_failure({"nonblocking": True,
                                    "message": product_notes[0],
                                    "issues": product_notes[:8],
                                    "error_code": "character_sheet_product"})
            progress("reviewing_plan")
            review_input = {"stage": "plan_review", "schema": compact_review_schema(plan_review_schema(plan)),
                "review_output_version": REVIEW_OUTPUT_VERSION, "review_policy_version": REVIEW_POLICY_VERSION,
                "final_plan": review_projection(plan), "fixed_speech": locked,
                "speech_requirements": review_speech_requirements(plan),
                "transition_boundaries": review_transition_boundaries(plan),
                "merchant_request": merchant_request, "conversation": conversation,
                "execution_windows": review_execution_windows(schedule, plan["segments"]),
                "execution_window_rules": "beat_start/beat_end是本拍实际分配的段内窗口，不是成片实测。speech_overlap=true时，可用动作时间为本拍窗口与实际镜头的交集；visual_seconds是作者的需求估计，visual_end不是并行动作的硬截止，不能仅因镜头晚于它结束而判失败。须独立估计实际动作、辨认和必要停留，不能照抄估计或凭并行标记宣称通过。speech_overlap=false时，动作仍须在visual_start至visual_end内完成，再在speech窗口说话，不能借用后半镜或语音时间。并行也须满足实际动作及因果：同一人入口、吞咽与说话不可假定同时完成；不能借相邻beat时间、隐藏动作、补造省略或提前宣称取物等状态完成。逐短语按实际镜头顺序核对，不把整句有某个相关镜头当全部短语已对应。"}
            try:
                raw_review = await stage_call(PLAN_REVIEW_SYSTEM, review_input, [])
            except Exception as error:
                _reraise_terminal(error)
                record_failure({"nonblocking": True, "message": type(error).__name__, "issues": ["plan_review"]})
                await stop("本次方案的完整检查尚未完成，暂未采用", {("review",): "综合检查记录不完整或无效"},
                     error_code="planning_review_incomplete")
            review_rounds = 0
            while True:
                review_rounds += 1
                if review_rounds > 3:
                    await stop("本次方案的完整检查尚未完成，暂未采用", {("review",): "综合检查记录不完整或无效"},
                               error_code="planning_review_incomplete")
                try:
                    from .plan_review import PlanReview, normalize_plan_review_wire
                    from .planner_recovery import drop_extra_forbidden, unwrap_planner_object
                    if isinstance(raw_review, dict):
                        raw_review = normalize_plan_review_wire(unwrap_planner_object(raw_review))
                        raw_review = drop_extra_forbidden(PlanReview, raw_review)
                    review = validate_plan_review(lower_failed_plan_verdicts(raw_review, plan), plan,
                                                  require_speaker_visibility=True, require_temporal_omission=True,
                                                  require_temporal_omission_evidence=True, require_visual_execution=True,
                                                  require_review_policy=True)
                    break
                except (ValueError, KeyError, TypeError) as error:
                    record_failure(plan)
                    if review_response_repair_used:
                        await stop("本次方案的完整检查尚未完成，暂未采用", {("review",): "综合检查记录不完整或无效"},
                             error_code="planning_review_incomplete")
                    # Correct the review record, never reroll authored content or
                    # erase an explicit rejection. One correction for the entire
                    # plan shares the durable text-session request budget.
                    review_response_repair_used = True
                    correction = {**deepcopy(review_input), "task_kind": "plan_review_response_repair",
                        "previous_review": deepcopy(raw_review),
                        "validation_feedback": review_validation_feedback(error),
                        "repair_instruction": "修正上一份审核记录的结构、缺项或覆盖范围，返回完整审核JSON。审核对象final_plan保持原样，不生成改稿。所有已有明确失败项必须保留，不得为了修正格式把失败改成通过；补全漏审项，并使汇总结论与逐项结论一致。"}
                    progress("reviewing_plan")
                    try:
                        corrected_review = await stage_call(PLAN_REVIEW_SYSTEM, correction, [])
                    except Exception as error:
                        _reraise_terminal(error)
                        record_failure({"nonblocking": True, "message": type(error).__name__, "issues": ["plan_review"]})
                        await stop("本次方案的完整检查尚未完成，暂未采用", {("review",): "综合检查记录不完整或无效"},
                             error_code="planning_review_incomplete")
                    try:
                        preserve_review_rejections(raw_review, corrected_review, plan)
                    except (ValueError, KeyError, TypeError):
                        record_failure(plan)
                        await stop("本次方案的完整检查尚未完成，暂未采用", {("review",): "综合检查记录不完整或无效"},
                             error_code="planning_review_incomplete")
                    raw_review = corrected_review
            remember(plan, review)
            if review.ready and (
                    brand_stack_remaining(
                        (plan or {}).get("ad_script") or {}, strategy, locked, plan, duration)
                    or brand_visual_claim_overflow(plan, strategy, locked, duration)):
                record_failure(plan)
                raise ProgressivePlanningError(
                    "产品片旁白还在堆卖点，这次不采用。已有方案和素材已保留，尚未开始新的图片或视频生成",
                    issue_summary=["产品片旁白还在堆卖点"], repair_attempts=repairs)
            if review.ready:
                refuse_mismatched_voice(plan)
                refuse_story_structure(plan)
            if review.ready:
                from .shot_craft import with_shot_suggestions
                from .line_quality import with_line_focus_suggestions
                review = with_shot_suggestions(review, plan, strategy)
                review = with_line_focus_suggestions(review, plan, strategy, locked)
                plan["plan_review"] = review.model_dump()
                remember(plan, review)
                if is_degenerate_plan(plan):
                    chosen = best_real()
                    if usable(chosen):
                        return release(chosen)
                    await stop("分镜没有完成，占位稿不是完成的方案",
                               {("contract",): "占位稿不是完成的方案"})
                return release(plan)
            record_failure(plan)
            feedback = review.model_dump(exclude={"reviewed_plan_sha256", "reviewed_reference_asset_ids"})
            issues = _review_issues(feedback, plan)
            # A prop count or presence jump is repaired and compiled again
            # before this plan can be released. Anything else still is not
            # sent back to the model.
            execution = feedback.get("execution_check") if isinstance(feedback, dict) else None
            if (isinstance(execution, dict) and execution.get("state_continuity") is False
                    and not prop_review_repaired):
                from .story_logic import repair_prop_handoff
                prop_review_repaired = True
                repaired, did_prop = repair_prop_handoff(coordinated)
                if did_prop:
                    raw = repaired
                    continue
            issues.pop(("execution", "state_continuity"), None)
            if not issues:
                plan["plan_review"] = review.model_dump()
                remember(plan, review)
                return release(plan)
            feedback_for_scope = deepcopy(feedback)
            execution = feedback_for_scope.get("execution_check")
            if isinstance(execution, dict):
                execution["state_continuity"] = True
            if dialogue_polish_used:
                await stop("单次台词收尾后，本次方案仍有未解决的问题", issues)
            if (max_refinements > 0 and repairs == max_refinements and _only_natural_speech_failed(review)
                    and any(beat["narration"] for beat in coordinated["ad_script"]["beats"])):
                # Replaying the same completed stage reconstructs this flag after
                # restart; its request and the full review share the session budget.
                dialogue_polish_used = True
                progress("refining_storyboard")
                narration_limits = dialogue_narration_limits(plan)
                polish_input = {**data, "stage": "plan_refinement", "task_kind": "final_dialogue_polish",
                    "schema": dialogue_polish_schema(coordinated, narration_limits),
                    "narration_limits": narration_limits, "draft_storyboard": coordinated,
                    "final_plan": review_projection(plan), "review_feedback": feedback,
                    "repair_attempt": repairs, "repair_attempt_limit": max_refinements,
                    "dialogue_polish_attempt": 1, "dialogue_polish_limit": 1}
                polish_input.pop("speech_budget", None)
                try:
                    candidate = await stage_call(DIALOGUE_POLISH_SYSTEM, polish_input, [])
                except Exception as error:
                    _reraise_terminal(error)
                    record_failure({"nonblocking": True, "message": type(error).__name__, "issues": ["dialogue_polish"]})
                    await stop("台词收尾未满足仅修改现有台词及逐拍字数预算的要求，暂未采用", issues)
                try:
                    raw = compose_dialogue_polish(candidate, coordinated, narration_limits)
                except (ValueError, KeyError, TypeError):
                    await stop("台词收尾未满足仅修改现有台词及逐拍字数预算的要求，暂未采用", issues)
                if raw == coordinated:
                    await stop("台词收尾没有产生实际文字变化，已停止重复尝试", issues)
                continue  # Compile with existing locks, then review the entire plan.
            capacity = _review_capacity_gaps(feedback, plan, review_input["execution_windows"])
            improved = _capacity_improved(previous_capacity, capacity, previous_draft, coordinated)
            if previous_issues is not None and set(previous_issues).issubset(issues) and not improved:
                await stop("自动调整后同一内容问题仍未改善，已停止重复尝试", issues)
            previous_review, previous_issues = feedback, issues
            previous_capacity, previous_draft = capacity, coordinated
            try:
                raw = await refine(coordinated, repair_scope(feedback_for_scope, coordinated, plan), feedback_for_scope, issues,
                                   plan=plan, schedule=schedule, bound_review=review)
            except RevisionKept:
                chosen = best_real()
                if chosen is not None:
                    return release(chosen)
                if (brand_stack_remaining(
                        (plan or {}).get("ad_script") or {}, strategy, locked, plan, duration)
                        or brand_visual_claim_overflow(plan, strategy, locked, duration)):
                    raise ProgressivePlanningError(
                        "产品片旁白还在堆卖点，这次不采用。已有方案和素材已保留，尚未开始新的图片或视频生成",
                        issue_summary=["产品片旁白还在堆卖点"], repair_attempts=repairs)
                refuse_mismatched_voice(plan)
                refuse_story_structure(plan)
                return release(plan)
    except _PlanAccepted as accepted:
        return release(accepted.plan)
