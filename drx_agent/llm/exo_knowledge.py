"""EXO control-plane knowledge client + recall-precision observation.

Two concerns, one file:

1. `ExoKnowledge` — a dependency-light, never-raising async client for the
   QWEN-EXO long-term knowledge and PolicyData lanes (paths under
   `/qwen-exo`, NOT the `/v1` model gateway). Every method catches all
   exceptions and returns an empty value (`{}` / `False`) so a dead control
   plane can never break the agent.

2. Pure helpers — `classify_project_doc` maps a project file onto a retrieval
   category, `ingest_project_memory` refuses to ingest ephemeral task state
   into long-term memory, and `recall_precision` distills a recall trace into
   a single precision number (precision matters more than recall count).

The long-term lane is for HIGH-CONFIDENCE, STABLE documents only —
architecture, API specs, coding policy, framework docs. Ephemeral task state
(TODO lists, in-progress notes, per-run session/playbook files) must never
enter it; that guard is the whole point of this module.
"""

from __future__ import annotations

import base64
import logging
import os

import httpx

from drx_agent.llm.exo_observability import _normalize_control_url, summarize_recall

logger = logging.getLogger(__name__)

# Content markers that indicate a file describes ephemeral, in-flight task
# state rather than durable project knowledge. Any occurrence → skip.
_EPHEMERAL_MARKERS = ("TODO", "正在", "本次", "当前任务", "刚刚")

# Filename shapes that are inherently ephemeral: playbooks and per-run
# session/state artifacts.
_EPHEMERAL_NAME_HINTS = ("session", "state")


def _build_ingest_payload(
    files: list[tuple[str, str]], retrieval_category: str = ""
) -> dict:
    """Build the EXO ``/knowledge/ingest`` body from ``(filename, content)`` pairs.

    Content is base64-encoded per the EXO ``KnowledgeUploadItem`` contract.
    An empty ``retrieval_category`` is sent as ``None`` (the schema enforces
    ``min_length=1`` on non-null values).
    """
    items = []
    for filename, content in files or []:
        items.append(
            {
                "filename": str(filename),
                "content_base64": base64.b64encode(
                    str(content).encode("utf-8")
                ).decode("ascii"),
                "retrieval_category": retrieval_category or None,
            }
        )
    return {"files": items}


def classify_project_doc(path: str) -> str:
    """Map a project file path onto a retrieval category.

    Returns one of ``architecture`` / ``api`` / ``policy`` / ``framework`` /
    ``unknown`` based on filename/path keywords. This is a documented
    heuristic: it only ever ADVISES a category; it never blocks ingestion.
    """
    name = (path or "").lower()

    if any(
        k in name
        for k in (
            "policy",
            "coding",
            "style",
            "guideline",
            "convention",
            "standard",
            "constitution",
            "rule",
        )
    ):
        return "policy"
    if any(
        k in name
        for k in (
            "api",
            "openapi",
            "swagger",
            "schema",
            "spec",
            "protocol",
            "endpoint",
        )
    ):
        return "api"
    if any(
        k in name
        for k in ("architect", "design", "roadmap", "component", "system", "module")
    ):
        return "architecture"
    if any(
        k in name
        for k in (
            "framework",
            "guide",
            "tutorial",
            "reference",
            "readme",
            "doc",
            "manual",
            "howto",
        )
    ):
        return "framework"
    return "unknown"


def _looks_ephemeral(path: str, content: str) -> tuple[bool, str]:
    """Decide whether a file is ephemeral task state (must NOT be ingested).

    Returns ``(is_ephemeral, reason)``. Reasons are stable strings so callers
    can surface exactly why a file was refused.
    """
    name = os.path.basename(path or "").lower()
    if name.startswith("playbook") and name.endswith(".json"):
        return True, "playbook state is ephemeral"
    for hint in _EPHEMERAL_NAME_HINTS:
        if hint in name:
            return True, f"{hint} file is ephemeral"
    text = content or ""
    for marker in _EPHEMERAL_MARKERS:
        if marker in text:
            return True, f"contains ephemeral marker {marker!r}"
    return False, ""


def recall_precision(recall_entries: list[dict]) -> dict:
    """Compute recall precision from EXO recall-trace entries.

    Precision = accepted / candidates (0.0 when there are no candidates).
    Recall PRECISION matters more than recall COUNT: a lane that proposes
    many candidates but accepts few is polluting the context, not helping it.
    """
    summary = summarize_recall(recall_entries or [])
    candidates = int(summary.get("memory_candidates", 0))
    accepted = int(summary.get("memory_accepted", 0))
    rejected = int(summary.get("memory_rejected", 0))
    precision = (accepted / candidates) if candidates else 0.0
    return {
        "candidates": candidates,
        "accepted": accepted,
        "rejected": rejected,
        "precision": precision,
    }


class ExoKnowledge:
    """Never-raising async client for EXO long-term knowledge + PolicyData lanes."""

    def __init__(self, control_url: str, timeout: float = 5.0):
        self.control_root: str = _normalize_control_url(control_url)
        self.timeout: float = timeout
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json: dict | None = None,
    ):
        try:
            resp = await self._get_client().request(
                method, f"{self.control_root}{path}", params=params, json=json
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # never raise out of a public method
            logger.debug("exo knowledge %s %s failed: %s", method, path, exc)
            return None

    async def list_knowledge(self, q: str = "") -> dict:
        data = await self._request("GET", "/qwen-exo/knowledge", {"q": q or ""})
        return data if isinstance(data, dict) else {}

    async def ingest(
        self, files: list[tuple[str, str]], retrieval_category: str = ""
    ) -> dict:
        payload = _build_ingest_payload(files, retrieval_category)
        data = await self._request("POST", "/qwen-exo/knowledge/ingest", json=payload)
        return data if isinstance(data, dict) else {}

    async def ingest_text(
        self, filename: str, content: str, retrieval_category: str = ""
    ) -> dict:
        return await self.ingest([(filename, content)], retrieval_category)

    async def preview(self, files: list[tuple[str, str]]) -> dict:
        payload = _build_ingest_payload(files)
        data = await self._request("POST", "/qwen-exo/knowledge/preview", json=payload)
        return data if isinstance(data, dict) else {}

    async def reindex(self) -> dict:
        data = await self._request("POST", "/qwen-exo/knowledge/reindex")
        return data if isinstance(data, dict) else {}

    async def get_document(self, relative_path: str) -> dict:
        data = await self._request("GET", f"/qwen-exo/knowledge/{relative_path}")
        return data if isinstance(data, dict) else {}

    async def put_document(self, relative_path: str, content: str) -> dict:
        data = await self._request(
            "PUT",
            f"/qwen-exo/knowledge/{relative_path}",
            json={"content": content, "tags": []},
        )
        return data if isinstance(data, dict) else {}

    async def delete_document(self, relative_path: str) -> bool:
        data = await self._request("DELETE", f"/qwen-exo/knowledge/{relative_path}")
        return isinstance(data, dict)

    async def list_policydata(self) -> dict:
        data = await self._request("GET", "/qwen-exo/policydata")
        return data if isinstance(data, dict) else {}

    async def put_policydata(self, relative_path: str, content: str) -> dict:
        data = await self._request(
            "PUT",
            f"/qwen-exo/policydata/{relative_path}",
            json={"content": content, "tags": []},
        )
        return data if isinstance(data, dict) else {}

    async def reindex_policydata(self) -> dict:
        data = await self._request("POST", "/qwen-exo/policydata/reindex")
        return data if isinstance(data, dict) else {}

    async def ingest_project_memory(
        self, paths: list[str], *, category: str = ""
    ) -> dict:
        """Read project files from disk and ingest the stable ones.

        Refuses anything that looks ephemeral (the guard in `_looks_ephemeral`):
        playbook/session/state files and any content bearing a TODO / in-flight
        marker. Never ingests ephemeral task state into long-term knowledge.
        """
        ingested: list[str] = []
        skipped: list[dict] = []
        for path in paths or []:
            path = str(path)
            filename = os.path.basename(path)
            try:
                with open(path, "r", encoding="utf-8") as fp:
                    content = fp.read()
            except Exception as exc:
                skipped.append({"path": path, "reason": f"unreadable: {exc}"})
                continue
            ephemeral, reason = _looks_ephemeral(path, content)
            if ephemeral:
                skipped.append({"path": path, "reason": reason})
                continue
            resolved_category = category or classify_project_doc(path)
            await self.ingest_text(filename, content, resolved_category)
            ingested.append(filename)
        return {"ingested": ingested, "skipped": skipped}

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception as exc:
                logger.debug("exo knowledge close failed: %s", exc)
            finally:
                self._client = None
