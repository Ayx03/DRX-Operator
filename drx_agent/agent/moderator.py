"""Deterministic Moderator — a lightweight, interval-driven scheduler.

Design principles (EXOBoost research report):
- "Moderator 不应该是'大 Boss Agent'"：it does NOT re-understand the project,
  does NOT read conversations, and does NOT rewrite worker results. It is a
  *scheduler*, not an omniscient thinker. Its only input is structured metrics
  (``ModeratorMetrics``), never raw text.
- Stateless / low-state: the only mutable state is the interval clock
  (``interval_s`` + ``_last_run``). Everything else is pure.
- No LLM anywhere: ``observe`` maps metrics → suggestions through fixed
  templates only. ``render`` is a bounded Chinese block for prompt injection.

Self-contained: stdlib only. Forum / ClaimRegistry are consumed by duck-typing
via ``getattr`` so this module imports cleanly even before they exist on disk.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

# 验证队列积压阈值：超过则建议排空验证队列。
_VERIFIER_BACKLOG_THRESHOLD = 3
# 预算告警阈值（占用量比例）。
_BUDGET_WARNING_RATIO = 0.8
# 重复意图判定的 token 重叠系数（与 Frontier.score_intent 的冗余惩罚一致）。
_DUP_OVERLAP = 0.5
# 参与重复检测的 open 意图数量上限（保持 O(n^2) 有界）。
_DUP_SCAN_CAP = 60


def _tokens(text: str) -> set[str]:
    """词元 + 中文二元组（与 Frontier._tokens 同款方案，保证跨模块一致）。"""
    t = (text or "").lower()
    norm = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", t)
    grams = {norm[i : i + 2] for i in range(len(norm) - 1)} if len(norm) >= 2 else {norm}
    words = set(re.findall(r"[a-z0-9_]+", t))
    return grams | words


@dataclass
class Suggestion:
    """One deterministic scheduling suggestion emitted by the Moderator."""

    kind: str  # unassigned_work|duplicate_work|stalled_work|verifier_backlog|coverage_gap|conflict|budget_warning|idle_workers
    text: str  # short Chinese guidance (template-derived, never LLM)
    target: str = ""  # work item / worker id, when the suggestion points at one
    severity: str = "info"  # info|warn|critical


@dataclass
class ModeratorMetrics:
    """The ONLY inputs the Moderator sees — structured counters, never prose."""

    open_intents: int = 0
    claimed_intents: int = 0
    unassigned_intents: int = 0
    active_workers: int = 0
    stalled_workers: int = 0
    duplicate_intent_pairs: int = 0
    pending_verifications: int = 0
    coverage: dict = field(default_factory=dict)
    quorum: float = 0.0
    budget_used_ratio: float = 0.0
    conflicts: int = 0


class Moderator:
    """Deterministic, interval-driven scheduler over structured metrics."""

    def __init__(self, interval_s: float = 60.0) -> None:
        self.interval_s = float(interval_s) if interval_s is not None else 60.0
        self._last_run: float | None = None

    # ------------------------------------------------------------------ #
    # interval gate
    # ------------------------------------------------------------------ #
    def should_run(self, now: float | None = None) -> bool:
        """Deterministic interval gate: due on first call, then every
        ``interval_s`` seconds. Reads only; does not mutate the clock."""
        now = time.time() if now is None else now
        if self._last_run is None:
            return True
        return now - self._last_run >= self.interval_s

    # ------------------------------------------------------------------ #
    # metrics extraction (duck-typed, tolerant of None)
    # ------------------------------------------------------------------ #
    @staticmethod
    def extract_metrics(
        *,
        frontier=None,
        claims=None,
        forum=None,
        knowledge_base=None,
        coverage=None,
        quorum: float = 0.0,
        budget_used_ratio: float = 0.0,
    ) -> ModeratorMetrics:
        """Derive a ``ModeratorMetrics`` from whatever objects are passed.

        Any object may be None; unknown/absent attributes are skipped via
        ``getattr``. Metrics that have no source here (``stalled_workers``,
        ``conflicts``) default to 0 and are supplied by callers that own them.
        """
        open_intents = 0
        claimed_intents = 0
        hypotheses: list[str] = []

        if frontier is not None:
            list_open = getattr(frontier, "list_open", None)
            if callable(list_open):
                try:
                    open_items = list_open() or []
                    hypotheses = [
                        str(getattr(i, "hypothesis", "") or "") for i in open_items
                    ]
                    open_intents = len(open_items)
                except Exception:
                    open_intents = 0
            intents_map = getattr(frontier, "_intents", None) or {}
            for intent in list(intents_map.values()):
                raw = getattr(intent, "status", None)
                status = getattr(raw, "value", None) or str(raw or "")
                if status == "claimed":
                    claimed_intents += 1

        # Unassigned = open intents not yet claimed (frontier OPEN == unclaimed).
        unassigned_intents = open_intents

        # Pending verifications = findings still "suspected" (await verification).
        pending_verifications = 0
        if knowledge_base is not None:
            all_findings = getattr(knowledge_base, "all_findings", None)
            if callable(all_findings):
                try:
                    for _host, finding in all_findings():
                        if str(getattr(finding, "status", "") or "") == "suspected":
                            pending_verifications += 1
                except Exception:
                    pending_verifications = 0

        # Active workers: prefer forum roster size, else distinct claim owners.
        active_workers = 0
        if forum is not None:
            roster = getattr(forum, "roster", None)
            if callable(roster):
                try:
                    r = roster() or []
                    if isinstance(r, (dict, list, tuple, set)):
                        active_workers = len(r)
                except Exception:
                    active_workers = 0
        if active_workers == 0 and claims is not None:
            active = getattr(claims, "active", None)
            if callable(active):
                try:
                    owners: set[str] = set()
                    for it in active() or []:
                        if isinstance(it, dict):
                            o = it.get("owner") or it.get("agent_id") or it.get("worker")
                        else:
                            o = (
                                getattr(it, "owner", None)
                                or getattr(it, "agent_id", None)
                                or getattr(it, "worker", None)
                            )
                        if o:
                            owners.add(str(o))
                    active_workers = len(owners)
                except Exception:
                    active_workers = 0

        coverage = dict(coverage or {})

        return ModeratorMetrics(
            open_intents=open_intents,
            claimed_intents=claimed_intents,
            unassigned_intents=unassigned_intents,
            active_workers=active_workers,
            stalled_workers=0,
            duplicate_intent_pairs=Moderator._count_duplicate_pairs(hypotheses),
            pending_verifications=pending_verifications,
            coverage=coverage,
            quorum=float(quorum or 0.0),
            budget_used_ratio=float(budget_used_ratio or 0.0),
            conflicts=0,
        )

    @staticmethod
    def _count_duplicate_pairs(hypotheses: list[str]) -> int:
        """Token-overlap (bigram) heuristic over open-intent hypotheses.

        Cheap and bounded: at most ``_DUP_SCAN_CAP`` hypotheses, pairwise.
        """
        if not hypotheses:
            return 0
        hs = [h for h in hypotheses[: _DUP_SCAN_CAP] if (h or "").strip()]
        if len(hs) < 2:
            return 0
        toks = [_tokens(h) for h in hs]
        pairs = 0
        for i in range(len(hs)):
            a = toks[i]
            if not a:
                continue
            for j in range(i + 1, len(hs)):
                b = toks[j]
                if not b:
                    continue
                denom = min(len(a), len(b))
                if denom and (len(a & b) / denom) >= _DUP_OVERLAP:
                    pairs += 1
        return pairs

    # ------------------------------------------------------------------ #
    # deterministic rules
    # ------------------------------------------------------------------ #
    def observe(self, metrics: ModeratorMetrics) -> list[Suggestion]:
        """Map structured metrics → suggestions via fixed rules. No LLM."""
        suggestions: list[Suggestion] = []

        if metrics.unassigned_intents > 0:
            severity = "critical" if metrics.unassigned_intents >= 5 else "warn"
            suggestions.append(
                Suggestion(
                    kind="unassigned_work",
                    text=f"有 {metrics.unassigned_intents} 个意图尚未认领，建议派发给空闲 worker。",
                    severity=severity,
                )
            )

        if metrics.duplicate_intent_pairs > 0:
            suggestions.append(
                Suggestion(
                    kind="duplicate_work",
                    text=f"检测到 {metrics.duplicate_intent_pairs} 对重复意图，建议合并去重。",
                    severity="warn",
                )
            )

        if metrics.stalled_workers > 0:
            suggestions.append(
                Suggestion(
                    kind="stalled_work",
                    text=f"有 {metrics.stalled_workers} 个 worker 停滞，建议解除阻塞或回收。",
                    severity="warn",
                )
            )

        if metrics.pending_verifications > _VERIFIER_BACKLOG_THRESHOLD:
            suggestions.append(
                Suggestion(
                    kind="verifier_backlog",
                    text=f"验证队列积压 {metrics.pending_verifications} 条，建议优先排空验证队列。",
                    severity="warn",
                )
            )

        for key, bucket in (metrics.coverage or {}).items():
            if not isinstance(bucket, dict):
                continue
            covered = int(bucket.get("covered", 0) or 0)
            total = int(bucket.get("total", 0) or 0)
            if total <= 0 or covered >= total:
                continue
            critical = "critical" in str(key).lower()
            suggestions.append(
                Suggestion(
                    kind="coverage_gap",
                    text=f"覆盖率缺口：{key} 覆盖 {covered}/{total}，建议定向补充。",
                    target=str(key),
                    severity="warn" if critical else "info",
                )
            )

        if metrics.conflicts > 0:
            suggestions.append(
                Suggestion(
                    kind="conflict",
                    text=f"检测到 {metrics.conflicts} 处冲突，建议优先裁决。",
                    severity="warn",
                )
            )

        if metrics.budget_used_ratio >= 1.0:
            suggestions.append(
                Suggestion(
                    kind="budget_warning",
                    text="预算已耗尽，建议立即收束并保留进度。",
                    severity="critical",
                )
            )
        elif metrics.budget_used_ratio >= _BUDGET_WARNING_RATIO:
            suggestions.append(
                Suggestion(
                    kind="budget_warning",
                    text=f"预算已用 {metrics.budget_used_ratio:.0%}，接近上限，建议收窄范围。",
                    severity="warn",
                )
            )

        if (
            metrics.active_workers > 0
            and metrics.unassigned_intents == 0
            and metrics.claimed_intents == 0
        ):
            suggestions.append(
                Suggestion(
                    kind="idle_workers",
                    text=f"有 {metrics.active_workers} 个 worker 空闲且无待办，建议结束或派发新任务。",
                    severity="info",
                )
            )

        return suggestions

    # ------------------------------------------------------------------ #
    # render / tick
    # ------------------------------------------------------------------ #
    def render(self, suggestions: list[Suggestion], max_chars: int = 800) -> str:
        """Bounded Chinese block for prompt injection."""
        if not suggestions:
            return ""
        lines = ["【调度建议 Moderator】"]
        for s in suggestions:
            target = f" @{s.target}" if s.target else ""
            lines.append(f"- [{s.severity}] {s.kind}{target}: {s.text}")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[: max_chars - 1] + "…"
        return text

    def tick(self, metrics: ModeratorMetrics, now: float | None = None) -> list[Suggestion]:
        """``should_run`` + ``observe``; returns [] when not due."""
        now = time.time() if now is None else now
        if not self.should_run(now):
            return []
        self._last_run = now
        return self.observe(metrics)

    # ------------------------------------------------------------------ #
    # serialization (interval state only)
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        return {"interval_s": self.interval_s, "last_run": self._last_run}

    @classmethod
    def from_dict(cls, data: dict) -> "Moderator":
        data = data or {}
        m = cls(interval_s=float(data.get("interval_s", 60.0)) if data.get("interval_s") is not None else 60.0)
        lr = data.get("last_run")
        m._last_run = float(lr) if lr is not None else None
        return m
