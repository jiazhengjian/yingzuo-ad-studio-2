"""Per-type ad playbooks: which one to inject, and the speech budget it uses.

Rates are engineering capacity, not measured delivery. Unknown or unset forms
keep the historical 2.9 characters/second budget so older plans still load.
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import os


BEAT_CHAR_CEILING = 80
LEGACY_TENTH = 29
LEGACY_BEAT_CHARACTERS = 29
TAGLINE_MAX_CHARACTERS = 10

STRUCTURE_TO_FORM = {
    "product_visual": "brand_tvc",
    "world_intercut": "brand_tvc",
    "lifestyle": "brand_tvc",
    "scene_action": "scene_story",
    "presenter_explanation": "presenter_pitch",
}
CONCRETE_FORMS = ("brand_tvc", "scene_story", "presenter_pitch")
_SUGGESTION_KINDS = frozenset({
    "ordinary_wording", "expression_quality", "optional_pacing", "shot_variety", "type_fit", "shot_craft",
})
# Share of runtime with the product in frame. Scene films keep the playbook's
# lower target; a storyboard that never opted into the new fields is not scored.
SCREEN_TIME_TARGETS = {"brand_tvc": 0.80, "scene_story": 0.50, "presenter_pitch": 0.70}
_RATE_ENV = {
    "brand_tvc": ("AD_SPEECH_RATE_BRAND_TVC", "3.0", "AD_BEAT_CHARS_BRAND_TVC", 30),
    "scene_story": ("AD_SPEECH_RATE_SCENE_STORY", "3.8", "AD_BEAT_CHARS_SCENE_STORY", 38),
    "presenter_pitch": ("AD_SPEECH_RATE_PRESENTER_PITCH", "4.8", "AD_BEAT_CHARS_PRESENTER_PITCH", 48),
}


@dataclass(frozen=True)
class SpeechProfile:
    form: str
    characters_per_tenth: int
    max_beat_characters: int

    @property
    def characters_per_second(self) -> float:
        if self.characters_per_tenth == LEGACY_TENTH:
            return 2.9
        return self.characters_per_tenth / 10


LEGACY_SPEECH_PROFILE = SpeechProfile("legacy", LEGACY_TENTH, LEGACY_BEAT_CHARACTERS)


def _field(value, key, default=None):
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _rate_tenth(name: str, default: str) -> int:
    raw = os.environ.get(name, default)
    if not isinstance(raw, str) or not raw.strip():
        raw = default
    try:
        value = Decimal(raw.strip())
    except InvalidOperation:
        value = Decimal(default)
    if value < Decimal("2.0") or value > Decimal("6.0"):
        value = Decimal(default)
    return int((value * 10).to_integral_value(rounding=ROUND_HALF_UP))


def _beat_characters(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    if not 8 <= value <= BEAT_CHAR_CEILING:
        return default
    return value


def screen_time_target(form: str | None) -> float:
    """In-frame product share for one form. Unknown forms use the general floor."""
    return SCREEN_TIME_TARGETS.get(form or "", 0.70)


def speech_profile(form: str | None) -> SpeechProfile:
    """Return the configured profile for one concrete form, else the legacy cap."""
    spec = _RATE_ENV.get(form or "")
    if spec is None:
        return LEGACY_SPEECH_PROFILE
    rate_env, rate_default, beat_env, beat_default = spec
    return SpeechProfile(form, _rate_tenth(rate_env, rate_default), _beat_characters(beat_env, beat_default))


def speech_ceiling_tenth() -> int:
    """Widest configured rate, used only where a plan's own form is not yet known."""
    tenths = [LEGACY_TENTH, *(speech_profile(form).characters_per_tenth for form in CONCRETE_FORMS)]
    return max(tenths)


def resolve_playbook_form(ad_form, structure=None) -> str | None:
    """Choose one playbook id. An explicit form wins over a mismatched structure."""
    if ad_form in CONCRETE_FORMS:
        return ad_form
    if ad_form == "auto":
        return STRUCTURE_TO_FORM.get(structure or "") or "auto"
    return None


def _selected_structure(strategy):
    if strategy is None:
        return None
    selected_id = _field(strategy, "selected_direction_id")
    directions = _field(strategy, "directions") or []
    selected = next((item for item in directions if _field(item, "id") == selected_id), None)
    concept = _field(selected, "film_concept") if selected is not None else None
    structure = _field(concept, "structure") if concept is not None else None
    return structure if isinstance(structure, str) and structure else None


def speech_profile_for_strategy(strategy) -> SpeechProfile:
    """Use the brief's form. Auto adopts the selected structure; otherwise stay legacy."""
    if strategy is None:
        return LEGACY_SPEECH_PROFILE
    brief = _field(strategy, "brief")
    ad_form = _field(brief, "ad_form") if brief is not None else None
    form = resolve_playbook_form(ad_form, _selected_structure(strategy))
    if form in CONCRETE_FORMS:
        return speech_profile(form)
    return LEGACY_SPEECH_PROFILE


# Nested speech spans validate before a plan can attach its own rate. The plan
# validator sets this for the duration of that validation only.
_plan_speech_tenth: ContextVar[int | None] = ContextVar("plan_speech_tenth", default=None)


def bind_plan_speech_tenth(strategy) -> Token:
    return _plan_speech_tenth.set(speech_profile_for_strategy(strategy).characters_per_tenth)


def bind_speech_tenth(tenth: int) -> Token:
    return _plan_speech_tenth.set(tenth)


def reset_plan_speech_tenth(token: Token) -> None:
    _plan_speech_tenth.reset(token)


def active_speech_tenth() -> int:
    """Rate for the plan currently being validated; lone segments stay at 2.9."""
    value = _plan_speech_tenth.get()
    return LEGACY_TENTH if value is None else value


def accepted_submission_tenth(value) -> int:
    """Honor a server-stamped rate. Anything else keeps the historical cap."""
    if type(value) is not int:
        return LEGACY_TENTH
    allowed = {LEGACY_TENTH, *(speech_profile(form).characters_per_tenth for form in CONCRETE_FORMS)}
    return value if value in allowed else LEGACY_TENTH


def structure_from_stage_data(data) -> str | None:
    """Read a structure the stage already has, without guessing from prose."""
    if not isinstance(data, dict):
        return None
    for path in (
        ("selected_direction", "film_concept", "structure"),
        ("creative_strategy", "selected_direction", "film_concept", "structure"),
    ):
        cursor = data
        for key in path:
            if not isinstance(cursor, dict):
                cursor = None
                break
            cursor = cursor.get(key)
        if isinstance(cursor, str) and cursor:
            return cursor
    for plan_key in ("final_plan", "plan", "current_plan"):
        plan = data.get(plan_key)
        if not isinstance(plan, dict):
            continue
        strategy = plan.get("strategy") if isinstance(plan.get("strategy"), dict) else plan
        structure = _selected_structure(strategy)
        if structure:
            return structure
    return None


def tagline_policy() -> dict:
    """Optional closing spoken line. Default keeps the current no-slogan behaviour."""
    enabled = os.environ.get("AD_TAGLINE_ENABLED", "false").strip().lower() == "true"
    if enabled:
        instruction = (
            f"可以在收束拍加一句不超过{TAGLINE_MAX_CHARACTERS}字的口播短句，点出已经确认的商品记忆；"
            "不另做结尾字卡，不把文字做进画面，不为这句话向商家提问。"
        )
    else:
        instruction = "保持现状：不写口号、购买引导或独立结尾字卡。收束靠产品在画面中的英雄定格。"
    return {
        "enabled": enabled,
        "max_characters": TAGLINE_MAX_CHARACTERS,
        "placement": "closing_spoken_line",
        "instruction": instruction,
    }


def review_finding_disposition(kind: str) -> str:
    """Type fit is advice. It must not become another refinement gate."""
    if kind in _SUGGESTION_KINDS:
        return "suggestion"
    return "blocking"


def apply_beat_narration_cap(schema: dict, max_beat_characters: int) -> dict:
    """Tighten the wire schema to this form's beat cap. Stored models keep the ceiling."""
    definitions = schema.get("$defs")
    beat = definitions.get("AdBeat") if isinstance(definitions, dict) else None
    narration = beat.get("properties", {}).get("narration") if isinstance(beat, dict) else None
    if isinstance(narration, dict):
        narration["maxLength"] = max_beat_characters
    return schema
