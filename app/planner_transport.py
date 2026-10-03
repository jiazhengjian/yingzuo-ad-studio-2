"""Fixed planner destinations and provider-specific wire options.

Provider selection never falls back to another account or model. Existing
DeepSeek settings remain readable; Tudou uses its own key and OpenAI endpoint.
"""
from copy import deepcopy

import httpx


def planner_provider(settings):
    provider = getattr(settings, "planner_provider", "deepseek")
    if provider not in ("deepseek", "tudou"):
        raise ValueError("Unsupported planner provider")
    return provider


def planner_api_key(settings):
    field = "tudou_api_key" if planner_provider(settings) == "tudou" else "deepseek_api_key"
    return getattr(settings, field, "")


def planner_endpoint(settings):
    return ("https://api.ai-tudou.net/v1/chat/completions" if planner_provider(settings) == "tudou"
            else "https://api.deepseek.com/chat/completions")


def planner_label(settings):
    provider = "土豆 API" if planner_provider(settings) == "tudou" else "DeepSeek"
    return f"{provider} · {settings.planner_model}"


def planner_request_timing(settings):
    """Bound slow non-streaming SOL replies without changing model semantics.

    The read limit covers network inactivity, so SOL also gets one wall-clock
    deadline. Other transports retain their historical 300-second behavior.
    These local waiting limits are not part of a completed response's identity.
    """
    if planner_provider(settings) == "tudou" and getattr(settings, "planner_model", "") == "gpt-6-sol":
        return httpx.Timeout(600, connect=15, write=60, pool=15), 660
    return httpx.Timeout(300), None


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
        if "max_tokens" in result:
            result["max_completion_tokens"] = result.pop("max_tokens")
    return result
