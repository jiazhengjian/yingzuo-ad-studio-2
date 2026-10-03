"""Pure planned action/speech capacity shared by script and director stages.

All values are estimates, not measured delivery or a semantic action verifier.
One tick is 0.1 second. Legacy beats retain their previous speech-only budget.
"""
from __future__ import annotations

import math
from decimal import Decimal, ROUND_CEILING


def field(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def speech_ticks(text, characters_per_tenth=29):
    """Conservative engineering speech limit, rounded up to a tenth.

    The default tenth is the historical 2.9 characters/second cap. A typed
    playbook passes its own tenth; 29 keeps the previous integer formula.
    """
    tenth = characters_per_tenth
    return (len(text) * 100 + tenth - 1) // tenth


def visual_ticks(timing):
    seconds = field(timing, "visual_seconds")
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("动作与辨认时长必须是大于0的有限秒数")
    return int((Decimal(str(seconds)) * 10).to_integral_value(rounding=ROUND_CEILING))


def beat_minimum_ticks(beat, characters_per_tenth=29):
    spoken = speech_ticks(field(beat, "narration", ""), characters_per_tenth)
    timing = field(beat, "timing")
    if timing is None:
        return max(10, spoken)
    overlap = field(timing, "speech_overlap")
    if type(overlap) is not bool:
        raise ValueError("动作与说话是否并行必须明确为布尔值")
    visual = visual_ticks(timing)
    return max(10, max(visual, spoken) if overlap else visual + spoken)


def execution_action_bounds(window):
    """Return planned action bounds, never a verdict that actions fit.

    Parallel work may use its whole allocated beat, intersected with each
    actual shot by the caller. The authored visual estimate is not a deadline.
    Sequential work must finish in its reserved pre-speech visual window.
    Historical groups without an overlap policy gain no inferred action bounds.
    """
    overlap = window.get("speech_overlap")
    if overlap is True:
        return window.get("beat_start"), window.get("beat_end")
    if overlap is False:
        return window.get("visual_start"), window.get("visual_end")
    return None, None


def beat_timing_budget(beat, characters_per_tenth=29):
    timing = field(beat, "timing")
    result = {
        "beat_id": field(beat, "id"),
        "character_count": len(field(beat, "narration", "")),
        "speech_seconds": speech_ticks(field(beat, "narration", ""), characters_per_tenth) / 10,
        "minimum_seconds": beat_minimum_ticks(beat, characters_per_tenth) / 10,
    }
    if timing is not None:
        result.update(visual_seconds=visual_ticks(timing) / 10,
                      speech_overlap=field(timing, "speech_overlap"))
    return result


def script_capacity_budget(script, duration_seconds, characters_per_tenth=29):
    """Report exact tick-based totals; declarations cannot override this sum."""
    beats = field(script, "beats", [])
    minimum_ticks = sum(beat_minimum_ticks(beat, characters_per_tenth) for beat in beats)
    return {
        "beats": [beat_timing_budget(beat, characters_per_tenth) for beat in beats],
        "total_minimum_seconds": minimum_ticks / 10,
        "target_seconds": duration_seconds,
        "overrun_seconds": max(0, minimum_ticks - duration_seconds * 10) / 10,
    }


def allocate_beat_ticks(needs, capacity, *, weighted=False):
    """Preserve every minimum; distribute only spare time to display holds."""
    extra = capacity - sum(needs)
    if not needs or extra < 0:
        raise ValueError("动作与完整台词无法放入对应画面时长")
    weights = needs if weighted else [1] * len(needs)
    total = sum(weights)
    shares = [extra * weight // total for weight in weights]
    remainder = extra - sum(shares)
    order = sorted(range(len(needs)), key=lambda i: (-(extra * weights[i] % total), i))
    for index in order[:remainder]:
        shares[index] += 1
    return [need + share for need, share in zip(needs, shares)]
