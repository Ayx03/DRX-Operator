"""Canonical session counters and conservative migration of pre-cache accounting."""

from copy import deepcopy
from typing import Any


USAGE_SCHEMA_VERSION = 2


def empty_usage() -> dict[str, Any]:
    return {
        "schema_version": USAGE_SCHEMA_VERSION,
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "cache_hit_tokens": 0, "cache_write_tokens": 0,
        "cache_known_input_tokens": 0, "cache_unknown_input_tokens": 0,
        "cache_known_requests": 0, "cache_write_known_requests": 0,
        "cost_usd": 0.0, "priced_tokens": 0, "unpriced_tokens": 0,
        "requests": 0, "by_model": {}, "by_actor": {},
        "usage_unknown_requests": 0,
    }


def restore_usage(saved: dict | None) -> dict[str, Any]:
    """Legacy hit totals lack a denominator, and legacy costs ignored caching.

    Retain these historical observations separately rather than mixing them into
    measured ratios or presenting their old estimates as correctly priced usage.
    """
    saved = saved or {}
    result = {**empty_usage(), **deepcopy(saved)}
    if saved.get("schema_version") == USAGE_SCHEMA_VERSION:
        return result
    for old, new in (("prompt", "prompt_tokens"), ("completion", "completion_tokens"),
                     ("total", "total_tokens")):
        if new not in saved and old in saved:
            result[new] = saved[old]
        result.pop(old, None)
    if "total_tokens" not in saved and "total" not in saved:
        result["total_tokens"] = result["prompt_tokens"] + result["completion_tokens"]
    result["schema_version"] = USAGE_SCHEMA_VERSION
    result["legacy_cache_hit_tokens"] = saved.get("cache_hit_tokens", 0)
    result["legacy_cost_usd"] = saved.get("cost_usd", saved.get("cost", 0.0))
    result["legacy_requests"] = result["requests"]
    result.pop("cost", None)
    result.update(
        cache_hit_tokens=0, cache_write_tokens=0, cache_known_input_tokens=0,
        cache_unknown_input_tokens=result["prompt_tokens"], cache_known_requests=0,
        cache_write_known_requests=0,
        cost_usd=0.0, priced_tokens=0, unpriced_tokens=result["total_tokens"],
    )
    result["by_model"] = {
        name: restore_usage(slot) for name, slot in saved.get("by_model", {}).items()
    }
    result["by_actor"] = {}
    if result["requests"] or result["total_tokens"]:
        historical = {k: v for k, v in result.items() if k not in ("by_model", "by_actor")}
        result["by_actor"]["legacy / unknown"] = historical
    return result


def cost_text(usage: dict) -> str:
    """The dollar amount is only the known-priced subtotal, never an invoice."""
    priced = usage.get("priced_tokens", 0)
    unpriced = usage.get("unpriced_tokens", 0)
    if (unpriced or usage.get("legacy_requests", 0) or usage.get("legacy_cost_usd", 0)
            or usage.get("usage_unknown_requests", 0)):
        return f"${usage.get('cost_usd', 0):.4f} (partial)" if priced else "unavailable"
    if not priced and usage.get("requests", 0):
        return "unavailable"
    return f"${usage.get('cost_usd', 0):.4f} (est.)"


def cache_text(
    hits: int | None, known: int | None, unknown: int | None, unknown_requests: int | None,
) -> str:
    if hits is None or not known:
        return "unavailable"
    ratio = f"{hits / known * 100:.1f}%"
    return f"{ratio} (partial)" if unknown or unknown_requests else ratio


def usage_status(usage: dict) -> dict[str, Any]:
    return {
        "cost": cost_text(usage),
        "cache_hits": usage["cache_hit_tokens"] if usage["cache_known_requests"] else None,
        "cache_known_input_tokens": usage["cache_known_input_tokens"],
        "cache_unknown_input_tokens": usage["cache_unknown_input_tokens"],
        "cache_unknown_requests": usage["requests"] - usage["cache_known_requests"],
        "usage_unknown_requests": usage.get("usage_unknown_requests", 0),
        "cache_write_tokens": (
            usage["cache_write_tokens"] if usage.get("cache_write_known_requests") else None
        ),
        "tokens_in": usage["prompt_tokens"], "tokens_out": usage["completion_tokens"],
        "tokens_total": usage["total_tokens"], "requests": usage["requests"],
        "by_model": deepcopy(usage["by_model"]), "by_actor": deepcopy(usage["by_actor"]),
    }
