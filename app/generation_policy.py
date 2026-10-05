"""Pure generation grouping policy; shots are not provider submissions.

The image-reference provider profile currently supports 2–30 second requests.
These are planning limits, not a guarantee of generated voice or visual quality.
Legacy jobs keep their original 10 second request/trim contract.
"""
from itertools import combinations
import math
from .beat_timing import beat_minimum_ticks

LEGACY_GENERATION = "legacy_10s"
CONTINUITY_GENERATION = "continuity_v1"
GENERATION_STRATEGIES = (LEGACY_GENERATION, CONTINUITY_GENERATION)


def _duration(value):
    if type(value) is not int or not 10 <= value <= 60:
        raise ValueError("视频时长须为10至60秒的整数")
    return value


def generation_durations(duration_seconds, generation_strategy=CONTINUITY_GENERATION):
    """Provisional capacities before copy exists; never final semantic cuts."""
    duration_seconds = _duration(duration_seconds)
    if generation_strategy == LEGACY_GENERATION:
        whole, tail = divmod(duration_seconds, 10)
        return [10] * whole + ([tail] if tail else [])
    if generation_strategy != CONTINUITY_GENERATION:
        raise ValueError("未知视频生成分组策略")
    count = math.ceil(duration_seconds / 30)
    whole, extra = divmod(duration_seconds, count)
    return [whole + (index < extra) for index in range(count)]


def _field(beat, key, default=None):
    return beat.get(key, default) if isinstance(beat, dict) else getattr(beat, key, default)


def boundary_allowed(previous, current):
    group = _field(previous, "continuity_group", "")
    return (_field(current, "boundary_before", "complete") != "continuous"
            and not (group and group == _field(current, "continuity_group", "")))


def _allocated_durations(groups, needs, total):
    minimums = [max(2, math.ceil(sum(needs[j] for j in group) / 10)) for group in groups]
    if any(value > 30 for value in minimums) or sum(minimums) > total or total > len(groups) * 30:
        return None
    weights = [sum(needs[j] for j in group) for group in groups]
    targets = [total * weight / sum(weights) for weight in weights]
    durations = minimums[:]
    for _ in range(total - sum(durations)):
        candidates = [i for i, value in enumerate(durations) if value < 30]
        index = max(candidates, key=lambda i: (targets[i] - durations[i], -i))
        durations[index] += 1
    return durations


def generation_groups(script, duration_seconds, generation_strategy=CONTINUITY_GENERATION,
                      characters_per_tenth=29):
    """Choose whole-beat groups, prefer few submissions and semantic boundaries.

    A continuous action or explicit shared group cannot be split. For longer
    ads, scene changes rank above ordinary complete expression boundaries. No
    sentence is sliced, and no empty provider segment is manufactured.
    """
    provisional = generation_durations(duration_seconds, generation_strategy)
    if generation_strategy == LEGACY_GENERATION:
        return {"durations": provisional, "beat_groups": None,
                "split_reasons": ["legacy_fixed_duration"] * max(0, len(provisional) - 1),
                "voice_continuity": "cross_request_unverified" if len(provisional) > 1 else "single_request_unverified"}
    beats = script["beats"] if isinstance(script, dict) else script.beats
    if not beats:
        raise ValueError("连续性分组需要完整的广告内容节拍")
    needs = [beat_minimum_ticks(beat, characters_per_tenth) for beat in beats]
    if duration_seconds <= 30:
        if sum(needs) > duration_seconds * 10 or len(beats) > 12:
            raise ValueError("完整台词无法为对应画面留出时间，请减少内容节拍或重新策划")
        return {"durations": [duration_seconds], "beat_groups": [list(range(len(beats)))],
                "split_reasons": [], "voice_continuity": "single_request_unverified"}
    allowed = [i for i in range(1, len(beats)) if boundary_allowed(beats[i - 1], beats[i])]
    best = None
    for count in range(math.ceil(duration_seconds / 30), min(6, len(beats)) + 1):
        for middle in combinations(allowed, count - 1):
            cuts = (0, *middle, len(beats))
            groups = [list(range(cuts[i], cuts[i + 1])) for i in range(count)]
            if any(len(group) > 12 for group in groups):
                continue
            durations = _allocated_durations(groups, needs, duration_seconds)
            if durations is None:
                continue
            reasons = [_field(beats[i], "boundary_before", "complete") for i in middle]
            # Prefer true scene changes; balancing only breaks semantic ties.
            cost = (sum(reason != "scene_change" for reason in reasons),
                    max(durations) - min(durations), middle)
            if best is None or cost < best[0]:
                best = (cost, durations, groups, reasons)
        if best is not None:
            return {"durations": best[1], "beat_groups": best[2],
                    "split_reasons": best[3], "voice_continuity": "cross_request_unverified"}
    raise ValueError("连续动作或完整台词无法在30秒上限内安全分组，请在完整动作或场景转换处重新安排内容")


def validate_generation_durations(durations, generation_strategy):
    total = _duration(sum(durations))
    if generation_strategy == LEGACY_GENERATION:
        if durations != generation_durations(total, LEGACY_GENERATION):
            raise ValueError("总长须为10至60秒，按完整10秒片段排列，仅末段可为剩余秒数")
    elif generation_strategy == CONTINUITY_GENERATION:
        if any(type(value) is not int or not 2 <= value <= 30 for value in durations):
            raise ValueError("连续性生成段须为2至30秒的整数，不能留下1秒尾段")
        if total <= 30 and len(durations) != 1:
            raise ValueError("30秒内的连续性方案应在一次生成中安排多个分镜")
    else:
        raise ValueError("未知视频生成分组策略")
