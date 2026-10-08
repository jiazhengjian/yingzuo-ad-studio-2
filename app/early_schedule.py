"""Who chooses the next creative step.

Clarification, direction offers, dialogue and storyboard can be named by the
model. Timing, review, and paid image or video work stay with the program.
When the model names nothing, the caller keeps its existing order.
"""
from __future__ import annotations


MODEL_STEPS = ("clarify", "directions", "speech", "storyboard")
PROGRAM_STEPS = ("timing", "review", "spend")

_ALIASES = {
    "clarify": "clarify",
    "clarification": "clarify",
    "directions": "directions",
    "direction": "directions",
    "speech": "speech",
    "dialogue": "speech",
    "ad_script": "speech",
    "storyboard": "storyboard",
    "timing": "timing",
    "schedule": "timing",
    "review": "review",
    "plan_review": "review",
    "direction_review": "review",
    "script_review": "review",
    "alignment_review": "review",
    "spend": "spend",
    "generate": "spend",
    "image": "spend",
    "video": "spend",
    "澄清": "clarify",
    "出方向": "directions",
    "台词": "speech",
    "分镜": "storyboard",
    "排时": "timing",
    "审核": "review",
    "花钱": "spend",
}


class EarlySchedule:
    def __init__(self, steps=(), refused=None):
        self.steps = tuple(steps)
        self.refused = refused

    @property
    def step(self):
        return self.steps[0] if self.steps else None


def classify_step(name):
    if not isinstance(name, str):
        return None
    token = name.strip()
    return _ALIASES.get(token) or _ALIASES.get(token.lower())


def _collect(value, steps, refused):
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        return
    for item in items:
        kind = classify_step(item)
        if kind in PROGRAM_STEPS:
            if refused[0] is None:
                refused[0] = kind
        elif kind in MODEL_STEPS and kind not in steps:
            steps.append(kind)


def detach_model_schedule(payload):
    """Drop schedule fields from a model object. Other values are left as-is."""
    if not isinstance(payload, dict):
        return EarlySchedule()
    refused = [None]
    listed = []
    if "next_steps" in payload:
        _collect(payload.pop("next_steps"), listed, refused)
    immediate = []
    if "next_step" in payload:
        _collect(payload.pop("next_step"), immediate, refused)
    steps = [*immediate, *[step for step in listed if step not in immediate]]
    return EarlySchedule(steps, refused[0])


def schedule_from_choice(choice):
    """Read a stored choice: a name, a list of names, or a payload fragment."""
    if isinstance(choice, dict):
        return detach_model_schedule(dict(choice))
    if choice is None:
        return EarlySchedule()
    return detach_model_schedule({"next_step": choice})


def creative_steps(schedule, historical):
    """Model-owned steps for this turn. A program-only name cannot replace them."""
    if schedule is not None and schedule.steps:
        return tuple(schedule.steps)
    return tuple(historical)
