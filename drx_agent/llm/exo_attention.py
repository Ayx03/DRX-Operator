"""EXO Attention/Score-Bias shadow reader + safety guard.

Attention Bias is the LAST capability to enable — never by default. The
verified EXO mode list is ``off`` / ``trajectory_shadow`` / ``trajectory_active``
(see ``_SCORE_BIAS_MODES`` in the booster config). The safe posture is:

    off  →  shadow  →  A/B on task outcome  →  active (last, never default)

This module enforces that ordering: `is_safe_default` accepts only ``off``,
`shadow_only` downgrades any non-off config to shadow (observe "what would
have been recalled" without injecting it), and `ExoAttention.assert_safe`
refuses any live config whose score-bias mode is not ``off``. Observation is
best-effort via `shadow_report` and never raises.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

import httpx

from drx_agent.llm.exo_observability import _normalize_control_url

logger = logging.getLogger(__name__)

# The verified shadow mode from the booster config's _SCORE_BIAS_MODES.
_SHADOW_MODE = "trajectory_shadow"
_ACTIVE_MODE = "trajectory_active"


@dataclass
class AttentionBiasConfig:
    """Mirror of the EXO score_bias fields (safe defaults: mode off)."""

    mode: str = "off"
    min_relevance: float = 0.01
    max_weight: float = 0.05
    tail_tokens: int = 4096
    tail_ratio: float = 0.15
    selected_blocks: int = 2
    query_window: int = 8
    relevance_margin: float = 0.005
    anchor_bias: float = 0.01


def is_safe_default(cfg: AttentionBiasConfig) -> bool:
    """True only when Attention Bias is off — it must never be on by default."""
    return cfg.mode == "off"


def shadow_only(cfg: AttentionBiasConfig) -> AttentionBiasConfig:
    """Return a copy forced to the safest shadow/off posture.

    An ``off`` config stays ``off`` (already safest); any active config is
    downgraded to shadow mode so candidates are observed but never injected.
    All other fields are preserved for observation.
    """
    guarded = replace(cfg)
    if guarded.mode != "off":
        guarded.mode = _SHADOW_MODE
    return guarded


def _to_float(mapping: dict, key: str, default: float) -> float:
    try:
        return float(mapping.get(key, default))
    except (TypeError, ValueError):
        return default


def _to_int(mapping: dict, key: str, default: int) -> int:
    try:
        return int(mapping.get(key, default))
    except (TypeError, ValueError):
        return default


def _score_bias_from_dict(mapping: dict) -> AttentionBiasConfig:
    """Extract score_bias fields from a service-config dict, tolerant of gaps."""
    mapping = mapping if isinstance(mapping, dict) else {}
    mode = str(mapping.get("score_bias_mode") or "off")
    return AttentionBiasConfig(
        mode=mode,
        min_relevance=_to_float(mapping, "score_bias_min_relevance", 0.01),
        max_weight=_to_float(mapping, "score_bias_max", 0.05),
        tail_tokens=_to_int(mapping, "score_bias_tail_tokens", 4096),
        tail_ratio=_to_float(mapping, "score_bias_tail_ratio", 0.15),
        selected_blocks=_to_int(mapping, "score_bias_selected_blocks", 2),
        query_window=_to_int(mapping, "score_bias_query_window", 8),
        relevance_margin=_to_float(mapping, "score_bias_relevance_margin", 0.005),
        anchor_bias=_to_float(mapping, "score_bias_anchor_bias", 0.01),
    )


def _as_list(value) -> list:
    return value if isinstance(value, list) else []


def _shadow_report_from_telemetry(data: dict) -> dict:
    """Extract candidate / weight / rejection observations from telemetry.

    Tolerant key lookup: unknown event shapes yield empty lists. Shadow means
    "compute what would have been recalled" without handing it to the model.
    """
    data = data if isinstance(data, dict) else {}
    events = data.get("events") if isinstance(data.get("events"), list) else []
    candidates: list = []
    weights: list = []
    rejections: list = []
    for event in events:
        if not isinstance(event, dict):
            continue
        candidates.extend(_as_list(event.get("candidates")))
        candidates.extend(_as_list(event.get("shadow_candidates")))
        candidates.extend(_as_list(event.get("score_bias_candidates")))
        weights.extend(_as_list(event.get("weights")))
        weights.extend(_as_list(event.get("score_bias_weights")))
        weights.extend(_as_list(event.get("bias_weights")))
        rejections.extend(_as_list(event.get("rejections")))
        rejections.extend(_as_list(event.get("rejected")))
        rejections.extend(_as_list(event.get("score_bias_rejections")))
    return {
        "candidates": candidates,
        "weights": weights,
        "rejections": rejections,
    }


class ExoAttention:
    """Never-raising shadow reader + safety guard for EXO Attention Bias."""

    def __init__(self, control_url: str, timeout: float = 3.0):
        self.control_root: str = _normalize_control_url(control_url)
        self.timeout: float = timeout
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def _request(self, method: str, path: str, params: dict | None = None):
        try:
            resp = await self._get_client().request(
                method, f"{self.control_root}{path}", params=params
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # never raise out of a public method
            logger.debug("exo attention %s %s failed: %s", method, path, exc)
            return None

    async def service_config(self) -> dict:
        data = await self._request("GET", "/qwen-exo/service-config")
        return data if isinstance(data, dict) else {}

    async def score_bias_config(self) -> AttentionBiasConfig:
        cfg = await self.service_config()
        return _score_bias_from_dict(cfg)

    async def assert_safe(self) -> tuple[bool, str]:
        cfg = await self.score_bias_config()
        if cfg.mode == "off":
            return True, ""
        reason = (
            f"Attention Bias is enabled (mode={cfg.mode!r}); it must be "
            "shadow-tested and A/B-validated on task outcome before use"
        )
        return False, reason

    async def shadow_report(self, limit: int = 100) -> dict:
        data = await self._request("GET", "/qwen-exo/telemetry", {"limit": limit})
        return _shadow_report_from_telemetry(data)

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception as exc:
                logger.debug("exo attention close failed: %s", exc)
            finally:
                self._client = None
