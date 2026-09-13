"""Standalone EXO control-plane observability client + worker-trace mapping.

Two concerns, one file:

1. `ExoObservability` — a dependency-light, never-raising async client for the
   QWEN-EXO control plane (paths under `/qwen-exo`, NOT the `/v1` model
   gateway). Every method catches all exceptions and returns an empty value
   (`{}` / `[]` / `False`) so a dead control plane can never break the agent.

2. Pure mapping helpers — join EXO recall/telemetry onto the agent's own
   `run / stage / worker / turn` trace. Recall must be observable, never
   silent: `summarize_recall` distills a recall-trace list into counts, and
   `build_worker_trace` folds it into the unified trace shape so a question
   like "why did audit-7 start investigating JWT?" is answerable from the
   trace alone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass
class ExoTraceContext:
    """Identity of the agent worker/turn that issued a model request."""

    project_id: str = ""
    run_id: str = ""
    stage: str = ""
    worker_id: str = ""
    turn: int = 0
    model_request_id: str = ""


def _normalize_control_url(url: str) -> str:
    """Normalize a control-plane URL to its root, stripping `/v1` if present.

    A caller may hand us the model gateway base (``.../v1``) or the control
    root; both must resolve to the same control root so the `/qwen-exo/...`
    paths can be appended.
    """
    value = (url or "").strip().rstrip("/")
    if value.endswith("/v1"):
        value = value[: -len("/v1")]
    return value


def _first_text(mapping: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return str(value)
    return ""


def _first_int(mapping: dict, keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _first_list(mapping: dict, keys: tuple[str, ...]) -> list:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, list):
            return value
    return []


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def summarize_recall(recall_entries: list[dict]) -> dict:
    """Aggregate EXO recall-trace entries into one counts summary.

    Tolerant of unknown shapes: looks for the common candidate / accepted /
    rejected keys and the real EXO turn shape (``knowledge_candidates`` +
    ``semantic_support``). Never raises; unknown shapes yield counts of 0 and
    keep whatever raw entry id is present.
    """
    trace_id = ""
    candidates = 0
    accepted = 0
    rejected = 0
    rejections: list[dict] = []

    for raw in recall_entries or []:
        entry = raw if isinstance(raw, dict) else {}
        if not entry:
            continue

        entry_id = _first_text(
            entry,
            (
                "recall_trace_id",
                "turn_id",
                "request_id",
                "response_id",
                "trajectory_id",
                "trace_id",
                "id",
                "entry_id",
            ),
        )
        if entry_id:
            trace_id = entry_id

        cand_int = _first_int(
            entry,
            ("candidate_count", "memory_candidates", "num_candidates", "top_k"),
        )
        cand_list = _first_list(
            entry, ("candidates", "knowledge_candidates", "proposed_candidates")
        )
        candidates += cand_int if cand_int is not None else len(cand_list)

        acc_int = _first_int(
            entry,
            (
                "accepted_count",
                "memory_accepted",
                "injected_count",
                "admitted_count",
                "num_accepted",
            ),
        )
        acc_list = _first_list(entry, ("accepted", "injected", "admitted"))
        accepted += acc_int if acc_int is not None else len(acc_list)

        rej_int = _first_int(
            entry, ("rejected_count", "memory_rejected", "num_rejected")
        )
        rej_list = _first_list(entry, ("rejected", "rejections"))
        rejected += rej_int if rej_int is not None else len(rej_list)
        for item in rej_list:
            item = item if isinstance(item, dict) else {}
            rid = _first_text(
                item, ("id", "candidate_id", "document_id", "rejection_id")
            )
            reason = _first_text(
                item, ("reason", "rejection_reason", "reject_reason")
            )
            if rid or reason:
                rejections.append({"id": rid, "reason": reason})

        # Real EXO turn shape: the semantic Judge marks each candidate
        # supported (accepted) or not (rejected) in ``semantic_support``.
        for support in _first_list(entry, ("semantic_support",)):
            support = support if isinstance(support, dict) else {}
            supported = support.get("supported")
            if supported is True:
                accepted += 1
            elif supported is False:
                rejected += 1
                sid = _first_text(
                    support, ("candidate_id", "id", "section_id", "document_id")
                )
                reason = _first_text(
                    support, ("reason", "rejection_reason")
                ) or "semantic_judge_rejected"
                rejections.append({"id": sid, "reason": reason})

    return {
        "recall_trace_id": trace_id,
        "memory_candidates": candidates,
        "memory_accepted": accepted,
        "memory_rejected": rejected,
        "rejections": rejections,
    }


def build_worker_trace(
    ctx: ExoTraceContext,
    recall: dict,
    *,
    tool: str = "",
    context_tokens: int = 0,
) -> dict:
    """Fold a recall summary onto the agent worker/turn into the unified trace."""
    recall = recall if isinstance(recall, dict) else {}
    return {
        "project_id": ctx.project_id,
        "run_id": ctx.run_id,
        "stage": ctx.stage,
        "worker_id": ctx.worker_id,
        "turn": int(ctx.turn or 0),
        "model_request_id": ctx.model_request_id,
        "exo": {
            "recall_trace_id": _first_text(recall, ("recall_trace_id",))
            or ctx.model_request_id,
            "memory_candidates": _to_int(recall.get("memory_candidates")),
            "memory_accepted": _to_int(recall.get("memory_accepted")),
            "memory_rejected": _to_int(recall.get("memory_rejected")),
        },
        "agent": {
            "tool": tool,
            "context_tokens": _to_int(context_tokens),
        },
    }


def _format_tokens(count: int) -> str:
    if count >= 1000:
        return f"{count // 1000}k"
    return str(count)


def _bounded(text: str, limit: int = 160) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def render_trace_line(trace: dict) -> str:
    """One-line human-readable Chinese summary of a worker trace, bounded.

    Example: ``[research-7 turn 18] 召回 3 / 采纳 1 / 拒绝 2 — read_file (52k ctx)``
    """
    trace = trace if isinstance(trace, dict) else {}
    exo_raw = trace.get("exo")
    exo = exo_raw if isinstance(exo_raw, dict) else {}
    agent_raw = trace.get("agent")
    agent = agent_raw if isinstance(agent_raw, dict) else {}

    worker = _first_text(trace, ("worker_id",))
    turn = _to_int(trace.get("turn"))
    candidates = _to_int(exo.get("memory_candidates"))
    accepted = _to_int(exo.get("memory_accepted"))
    rejected = _to_int(exo.get("memory_rejected"))
    tool = _first_text(agent, ("tool",))
    context_tokens = _to_int(agent.get("context_tokens"))

    head = f"[{worker} turn {turn}]" if worker else f"[turn {turn}]"
    body = f"{head} 召回 {candidates} / 采纳 {accepted} / 拒绝 {rejected}"
    if tool:
        body += f" — {tool}"
    if context_tokens:
        body += f" ({_format_tokens(context_tokens)} ctx)"
    return _bounded(body)


class ExoObservability:
    """Never-raising async client for the QWEN-EXO control plane."""

    def __init__(self, control_url: str, timeout: float = 3.0):
        self.control_url: str = control_url
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
            logger.debug("exo observability %s %s failed: %s", method, path, exc)
            return None

    async def status(self) -> dict:
        data = await self._request("GET", "/qwen-exo/status")
        return data if isinstance(data, dict) else {}

    async def health(self) -> dict:
        data = await self._request("GET", "/qwen-exo/health")
        return data if isinstance(data, dict) else {}

    async def recall_trace(self, limit: int = 10) -> list[dict]:
        data = await self._request("GET", "/qwen-exo/recall-trace", {"limit": limit})
        if isinstance(data, dict):
            turns = data.get("turns")
            return turns if isinstance(turns, list) else []
        return data if isinstance(data, list) else []

    async def telemetry(
        self, limit: int = 100, request_id: str | None = None
    ) -> dict:
        params: dict = {"limit": limit}
        if request_id:
            params["request_id"] = request_id
        data = await self._request("GET", "/qwen-exo/telemetry", params)
        return data if isinstance(data, dict) else {}

    async def request_traces(self, limit: int = 20, q: str = "") -> list[dict]:
        data = await self._request(
            "GET", "/qwen-exo/request-traces", {"limit": limit, "q": q}
        )
        if isinstance(data, dict):
            requests = data.get("requests")
            return requests if isinstance(requests, list) else []
        return data if isinstance(data, list) else []

    async def clear_recall_trace(self) -> bool:
        data = await self._request("DELETE", "/qwen-exo/recall-trace")
        return isinstance(data, dict)

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception as exc:
                logger.debug("exo observability close failed: %s", exc)
            finally:
                self._client = None
