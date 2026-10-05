"""Planner destinations and provider-specific wire options.

The primary provider handles every text stage unless PLANNER_STAGE_ROUTES
names another provider, model, or reasoning effort for that stage. A second
account is contacted only when PLANNER_FALLBACK_ON explicitly lists timeout
and/or HTTP 504. Image and video submission never uses these text routes.
Existing DeepSeek settings remain readable; Tudou uses its own key and endpoint.
"""
from copy import deepcopy
from types import SimpleNamespace

import httpx


PLANNER_ROUTE_STAGES = frozenset({
    "strategy", "direction_review", "ad_script", "script_review", "storyboard",
    "storyboard_repair", "alignment_review", "plan_review", "plan_refinement",
})
_EFFORTS = frozenset({"low", "medium", "high"})
_PROVIDERS = frozenset({"deepseek", "tudou"})


class PlannerRoute:
    """One text-stage destination. Primary routes keep the historical wire shape."""

    def __init__(self, provider, model, effort, primary):
        self.provider = provider
        self.model = model
        self.effort = effort
        self.primary = primary


def planner_provider(settings):
    provider = getattr(settings, "planner_provider", "deepseek")
    if provider not in _PROVIDERS:
        raise ValueError("Unsupported planner provider")
    return provider


def planner_api_key(settings):
    field = "tudou_api_key" if planner_provider(settings) == "tudou" else "deepseek_api_key"
    return getattr(settings, field, "")


def planner_endpoint(settings):
    return route_endpoint(planner_provider(settings))


def route_endpoint(provider):
    if provider not in _PROVIDERS:
        raise ValueError("Unsupported planner provider")
    return ("https://api.ai-tudou.net/v1/chat/completions" if provider == "tudou"
            else "https://api.deepseek.com/chat/completions")


def route_api_key(settings, route):
    field = "tudou_api_key" if route.provider == "tudou" else "deepseek_api_key"
    return getattr(settings, field, "")


def parse_stage_routes(raw):
    """Parse ``stage=provider:model[:effort]`` pairs. Empty keeps the primary model."""
    text = (raw or "").strip()
    if not text:
        return {}
    routes = {}
    for part in text.split(","):
        item = part.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError("PLANNER_STAGE_ROUTES entries must be stage=provider:model[:effort]")
        stage, spec = (piece.strip() for piece in item.split("=", 1))
        pieces = [piece.strip() for piece in spec.split(":")]
        if stage not in PLANNER_ROUTE_STAGES or stage in routes or len(pieces) not in (2, 3):
            raise ValueError("PLANNER_STAGE_ROUTES has an unknown, duplicate, or incomplete stage")
        provider, model = pieces[0].lower(), pieces[1]
        effort = pieces[2].lower() if len(pieces) == 3 else None
        if provider not in _PROVIDERS or not model or any(char.isspace() for char in model):
            raise ValueError("PLANNER_STAGE_ROUTES provider must be deepseek or tudou and include a model")
        if effort is not None and effort not in _EFFORTS:
            raise ValueError("PLANNER_STAGE_ROUTES effort must be low, medium or high")
        routes[stage] = {"provider": provider, "model": model, "effort": effort}
    return routes


def parse_fallback_on(raw):
    """Explicit timeout/504 opt-in. Empty disables fallback even if a model is set."""
    text = (raw or "").strip()
    if not text:
        return ()
    allowed = []
    for part in text.split(","):
        item = part.strip().lower()
        if item not in ("timeout", "504") or item in allowed:
            raise ValueError("PLANNER_FALLBACK_ON must list timeout and/or 504")
        allowed.append(item)
    return tuple(allowed)


def stage_route(settings, stage):
    """Return the configured destination. Unknown stages stay on the primary model."""
    routes = getattr(settings, "planner_stage_routes", None) or {}
    chosen = routes.get(stage) if isinstance(routes, dict) else None
    provider = planner_provider(settings)
    model = getattr(settings, "planner_model", "deepseek-flash")
    if not chosen:
        return PlannerRoute(provider, model, None, True)
    effort = chosen.get("effort")
    same = chosen["provider"] == provider and chosen["model"] == model and not effort
    return PlannerRoute(chosen["provider"], chosen["model"], effort, same)


def fallback_route(settings):
    """Return ``(route, triggers)`` only when fallback is fully and explicitly configured."""
    triggers = tuple(getattr(settings, "planner_fallback_on", ()) or ())
    provider = (getattr(settings, "planner_fallback_provider", "") or "").strip().lower()
    model = (getattr(settings, "planner_fallback_model", "") or "").strip()
    if not triggers:
        return None
    if provider not in _PROVIDERS or not model:
        raise ValueError("PLANNER_FALLBACK_ON requires PLANNER_FALLBACK_PROVIDER and PLANNER_FALLBACK_MODEL")
    return PlannerRoute(provider, model, None, False), triggers


# A proxy or CDN often answers a long silent POST with 502/520–524 or by
# dropping the socket. Those are the same class as a timeout for text fallback.
INTERRUPTED_STATUSES = frozenset({502, 520, 521, 522, 523, 524})


def fallback_eligible(triggers, *, status_code=None, timed_out=False, interrupted=False):
    timeout_class = timed_out or interrupted or status_code in INTERRUPTED_STATUSES
    if timeout_class and "timeout" in triggers:
        return True
    return status_code == 504 and "504" in triggers


def planner_label(settings):
    provider = "土豆 API" if planner_provider(settings) == "tudou" else "DeepSeek"
    return f"{provider} · {settings.planner_model}"


def planner_request_timing(settings, route=None):
    """Bound one text completion without changing a completed response's identity.

    Tudou gpt-6-sol keeps the historical read 600s / wall 660s limits. Other
    transports keep a 300s timeout and no extra wall clock. ``PLANNER_REQUEST_DEADLINE_SECONDS``
    lowers both so a silent request fails before an upstream gateway cuts it near 300s.
    Streaming uses the same wall clock; its read timeout is inactivity between chunks.
    """
    provider = route.provider if route is not None else planner_provider(settings)
    model = route.model if route is not None else getattr(settings, "planner_model", "")
    if provider == "tudou" and model == "gpt-6-sol":
        timeout, wall = httpx.Timeout(600, connect=15, write=60, pool=15), 660
    else:
        timeout, wall = httpx.Timeout(300), None
    deadline = getattr(settings, "planner_request_deadline_seconds", None)
    if type(deadline) is not int or deadline <= 0:
        return timeout, wall
    current = timeout.as_dict()
    timeout = httpx.Timeout(
        min(current["read"], deadline),
        connect=min(current["connect"], deadline),
        write=min(current["write"], deadline),
        pool=min(current["pool"], deadline))
    return timeout, deadline if wall is None else min(wall, deadline)


def route_settings(settings, route):
    """Preserve primary setting objects so historical effort fields stay visible."""
    if route.primary or (route.provider == planner_provider(settings)
                         and route.model == getattr(settings, "planner_model", "")
                         and not route.effort):
        return settings
    return SimpleNamespace(
        planner_provider=route.provider, planner_model=route.model,
        planner_storyboard_effort=getattr(settings, "planner_storyboard_effort", "high"),
        planner_direction_review_effort=getattr(settings, "planner_direction_review_effort", None))


def transport_options(settings, options, *, stage=None):
    result = deepcopy(options)
    if planner_provider(settings) == "tudou":
        # GPT reasoning is controlled by reasoning_effort, not DeepSeek's
        # thinking switch. The completion budget includes reasoning and JSON.
        result.pop("thinking", None)
        result.setdefault("reasoning_effort", "low")
        # Scope the SOL latency adjustment to review and local refinement;
        # other profiles (including storyboard) and stage-less callers stay intact.
        if (getattr(settings, "planner_model", "") == "gpt-6-sol"
                and stage in ("plan_review", "plan_refinement")
                and result.get("reasoning_effort") == "high"):
            result["reasoning_effort"] = "medium"
        if (getattr(settings, "planner_model", "") == "gpt-6-sol"
                and stage == "storyboard"
                and result.get("reasoning_effort") == "high"):
            effort = getattr(settings, "planner_storyboard_effort", "high")
            if effort not in ("low", "medium", "high"):
                raise ValueError("Unsupported storyboard reasoning effort")
            result["reasoning_effort"] = effort
        direction_effort = getattr(settings, "planner_direction_review_effort", None)
        if (getattr(settings, "planner_model", "") == "gpt-6-sol"
                and stage == "direction_review" and direction_effort in _EFFORTS
                and result.get("reasoning_effort") == "high"):
            result["reasoning_effort"] = direction_effort
        if "max_tokens" in result:
            result["max_completion_tokens"] = result.pop("max_tokens")
    return result


def apply_route_effort(payload, route):
    """An explicit per-stage effort wins over the primary model's stage adjustment."""
    if route is None or not route.effort:
        return payload
    result = deepcopy(payload)
    result["reasoning_effort"] = route.effort
    return result
