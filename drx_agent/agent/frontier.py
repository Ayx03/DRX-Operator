"""Typed frontier: intent queue + dead-end log + append-only history ledger.

Makes "what to try next" a system asset instead of model memory. Facts
live in the knowledge base; intents live here. Backtracking = popping
the next open intent after one dies. Every state transition is appended
to an immutable history buffer (the operation ledger); a derived view
is rendered for the LLM each loop iteration. Fact retraction cascades
through the enabled_by reverse index to kill dependent intents.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

MAX_INTENTS = 60
MAX_DEAD_ENDS = 40
HISTORY_CAP = 500


class IntentStatus(str, Enum):
    OPEN = "open"
    CLAIMED = "claimed"
    DONE = "done"
    DEAD = "dead"


@dataclass
class IntentBudget:
    """Mutable counters — budget is consumed as the intent runs."""

    max_steps: int = 8
    expiry_s: float = 900.0
    created_ts: float = field(default_factory=time.time)
    steps_used: int = 0


@dataclass
class Intent:
    id: str
    hypothesis: str
    action: str
    priority: int = 3
    status: IntentStatus = IntentStatus.OPEN
    budget: IntentBudget = field(default_factory=IntentBudget)
    depends_on: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    actor: str = "master"
    spawned_from: str | None = None
    result: str = ""
    resolved_by: str | None = None

    def expired(self, now: float) -> bool:
        return now - self.budget.created_ts > self.budget.expiry_s

    def exhausted(self) -> bool:
        return self.budget.steps_used >= self.budget.max_steps


@dataclass(frozen=True)
class DeadEnd:
    intent_id: str
    hypothesis: str
    reason: str
    category: str = "unknown"
    ts: float = field(default_factory=time.time)


class Frontier:
    def __init__(self) -> None:
        self._intents: dict[str, Intent] = {}
        self._dead_ends: list[DeadEnd] = []
        self._enabled_by: dict[str, list[str]] = {}
        self._history: list[dict] = []
        self.on_invalidate: Callable[[str, str, list[str]], None] | None = None

    def _append_event(self, type_: str, payload: dict) -> str:
        eid = f"evt-{uuid.uuid4().hex[:6]}"
        self._history.append(
            {"event_id": eid, "type": type_, "ts": time.time(), "payload": payload}
        )
        if len(self._history) > HISTORY_CAP:
            del self._history[: len(self._history) - HISTORY_CAP]
        return eid

    def add_intent(
        self,
        hypothesis: str,
        action: str,
        *,
        priority: int = 3,
        max_steps: int = 8,
        expiry_s: float = 900.0,
        depends_on: tuple[str, ...] = (),
        evidence: tuple[str, ...] = (),
        actor: str = "master",
        spawned_from: str | None = None,
    ) -> str | None:
        hypothesis = (hypothesis or "").strip()[:200]
        action = (action or "").strip()[:200]
        if isinstance(depends_on, str):
            depends_on = (depends_on,)
        if not hypothesis or not action:
            return None
        for intent in self._intents.values():
            if (
                intent.status in (IntentStatus.OPEN, IntentStatus.CLAIMED)
                and intent.hypothesis == hypothesis
                and intent.action == action
            ):
                return None
        if len(self._intents) >= MAX_INTENTS:
            oldest = min(self._intents.values(), key=lambda i: i.budget.created_ts)
            self._intents.pop(oldest.id, None)
        iid = f"it-{uuid.uuid4().hex[:6]}"
        self._intents[iid] = Intent(
            id=iid,
            hypothesis=hypothesis,
            action=action,
            priority=priority,
            budget=IntentBudget(
                max_steps=max_steps, expiry_s=expiry_s, created_ts=time.time()
            ),
            depends_on=tuple(depends_on),
            evidence=tuple(evidence),
            actor=actor[:60],
            spawned_from=spawned_from,
        )
        for dep in depends_on:
            self._enabled_by.setdefault(dep, []).append(iid)
        self._append_event(
            "intent.added",
            {"intent_id": iid, "hypothesis": hypothesis, "actor": actor},
        )
        return iid

    def by_id(self, intent_id: str) -> Intent | None:
        return self._intents.get(intent_id)

    def claim(self, intent_id: str) -> bool:
        intent = self._intents.get(intent_id)
        if intent is None or intent.status is not IntentStatus.OPEN:
            return False
        intent.status = IntentStatus.CLAIMED
        self._append_event("intent.claimed", {"intent_id": intent_id})
        return True

    def release(self, intent_id: str) -> bool:
        intent = self._intents.get(intent_id)
        if intent is None or intent.status is not IntentStatus.CLAIMED:
            return False
        intent.status = IntentStatus.OPEN
        return True

    def complete(self, intent_id: str, conclusion: str) -> bool:
        intent = self._intents.get(intent_id)
        if intent is None or intent.status is not IntentStatus.CLAIMED:
            return False
        intent.status = IntentStatus.DONE
        intent.result = (conclusion or "")[:200]
        intent.resolved_by = self._append_event(
            "intent.done", {"intent_id": intent_id, "conclusion": intent.result}
        )
        return True

    def kill(self, intent_id: str, reason: str, category: str = "unknown") -> bool:
        intent = self._intents.get(intent_id)
        if intent is None or intent.status not in (IntentStatus.OPEN, IntentStatus.CLAIMED):
            return False
        intent.status = IntentStatus.DEAD
        intent.result = (reason or "")[:200]
        intent.resolved_by = self._append_event(
            "intent.killed",
            {"intent_id": intent_id, "reason": intent.result, "category": category},
        )
        self._dead_ends.append(
            DeadEnd(
                intent_id=intent_id,
                hypothesis=intent.hypothesis,
                reason=intent.result,
                category=category,
            )
        )
        if len(self._dead_ends) > MAX_DEAD_ENDS:
            del self._dead_ends[: len(self._dead_ends) - MAX_DEAD_ENDS]
        return True

    def enabled_by(self, fact_ref: str) -> list[str]:
        return list(self._enabled_by.get(fact_ref, []))

    def invalidate(self, fact_ref: str, reason: str) -> int:
        """Kill open/claimed intents depending on a retracted fact."""
        killed = 0
        killed_ids: list[str] = []
        for iid in list(self._enabled_by.get(fact_ref, [])):
            intent = self._intents.get(iid)
            if intent is not None and intent.status in (
                IntentStatus.OPEN,
                IntentStatus.CLAIMED,
            ):
                self.kill(iid, reason)
                killed += 1
                killed_ids.append(iid)
        self._append_event(
            "fact.invalidated", {"fact_ref": fact_ref, "killed": killed}
        )
        if self.on_invalidate is not None:
            self.on_invalidate(fact_ref, reason, killed_ids)
        return killed

    def tick(self, intent_id: str, steps: int = 1) -> IntentStatus | None:
        intent = self._intents.get(intent_id)
        if intent is None or intent.status is not IntentStatus.CLAIMED:
            return None
        intent.budget.steps_used += steps
        if intent.exhausted():
            self.kill(intent_id, "budget exhausted")
            return IntentStatus.DEAD
        return intent.status

    def rebase_budgets(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for intent in self._intents.values():
            intent.budget.created_ts = now

    def prune_expired(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        killed = 0
        for intent in list(self._intents.values()):
            if (
                intent.status in (IntentStatus.OPEN, IntentStatus.CLAIMED)
                and intent.expired(now)
            ):
                self.kill(intent.id, "expired")
                killed += 1
        return killed

    def view(self, max_chars: int = 1200) -> str:
        open_items = self.list_open()[:5]
        claimed = [
            i for i in self._intents.values()
            if i.status is IntentStatus.CLAIMED
        ][:3]
        lines = ["【探索前沿 Frontier — 待验证意图队列】"]
        if open_items:
            lines.append("◆ 待认领 Intent（按优先级）:")
            for i in open_items:
                lines.append(
                    f"  - [{i.id}] 假设: {i.hypothesis} | 动作: {i.action} "
                    f"| 预算 {i.budget.steps_used}/{i.budget.max_steps} 步"
                )
        if claimed:
            lines.append("◆ 进行中:")
            for i in claimed:
                lines.append(f"  - [{i.id}] {i.hypothesis} — {i.action}")
        recent = self._dead_ends[-3:]
        if recent:
            lines.append("◆ 最近死路（禁止重复）:")
            for d in recent:
                lines.append(f"  - {d.hypothesis} — {d.reason}")
        if not open_items and not claimed and not recent:
            lines.append("  (空 — 用 intent_add 提出想验证的假设)")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n  ...(前沿截断)"
        return text

    def list_open(self) -> list[Intent]:
        items = [i for i in self._intents.values() if i.status is IntentStatus.OPEN]
        items.sort(key=lambda i: (i.priority, i.budget.created_ts))
        return items

    def dead_ends(self) -> list[DeadEnd]:
        return list(self._dead_ends)

    @staticmethod
    def _tokens(text: str) -> set:
        """词元 + 中文二元组（中文改写用 bigram 比单字 Jaccard 稳）。"""
        t = (text or "").lower()
        norm = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", t)
        grams = {norm[i:i + 2] for i in range(len(norm) - 1)} if len(norm) >= 2 else {norm}
        words = set(re.findall(r"[a-z0-9_]+", t))
        return grams | words

    def find_similar_dead_end(self, hypothesis: str, threshold: float = 0.4):
        """重叠系数找已排除的同类方向（防换个说法重试死路）。"""
        h = self._tokens(hypothesis)
        if not h:
            return None
        best, best_sim = None, 0.0
        for d in self._dead_ends:
            t = self._tokens(d.hypothesis)
            if not t:
                continue
            sim = len(h & t) / min(len(h), len(t))
            if sim > best_sim:
                best, best_sim = d, sim
        return best if best is not None and best_sim >= threshold else None

    def score_intent(self, intent: "Intent") -> float:
        """启发式价值分：优先级 + 证据支撑 - 成本 - 与其他 open 意图的冗余。"""
        score = float(5 - int(intent.priority))
        if intent.depends_on:
            score += 2.0
        score -= 0.5 * intent.budget.steps_used
        h = self._tokens(intent.hypothesis)
        for other in self._intents.values():
            if other.id == intent.id or other.status is not IntentStatus.OPEN:
                continue
            o = self._tokens(other.hypothesis)
            denom = min(len(h), len(o))
            if denom and (len(h & o) / denom) >= 0.5:
                score -= 1.5
        return score

    def ranked_open(self, limit=None) -> list:
        """按价值分排序的 open 意图（Judge 决策与批量派发用）。"""
        items = [i for i in self._intents.values() if i.status is IntentStatus.OPEN]
        items.sort(key=lambda i: (-self.score_intent(i), i.budget.created_ts))
        return items[:limit] if limit else items

    def history(self) -> list[dict]:
        return list(self._history)

    def supporting_refs(
        self,
        intent_id: str,
        scope: str | None = None,
        max_depth: int = 3,
    ) -> list[str]:
        intent = self._intents.get(intent_id)
        if intent is None:
            return []
        refs: list[str] = []
        seen: set[str] = set()
        stack: list[str] = list(intent.depends_on)
        depth = 0
        while stack and depth < max_depth:
            nxt: list[str] = []
            for dep in stack:
                if dep in seen:
                    continue
                if scope and not dep.startswith(f"{scope}::"):
                    continue
                seen.add(dep)
                refs.append(dep)
                for child in self._enabled_by.get(dep, []):
                    child_intent = self._intents.get(child)
                    if child_intent is not None:
                        nxt.extend(child_intent.depends_on)
            stack = nxt
            depth += 1
        for i in self._intents.values():
            if i.id == intent_id or i.status is not IntentStatus.DONE or not i.result:
                continue
            if scope and scope not in i.hypothesis and scope not in i.action:
                continue
            refs.append(f"{i.id}: {i.hypothesis} → {i.result}")
        return refs

    def to_dict(self) -> dict:
        return {
            "intents": [
                {
                    "id": i.id,
                    "hypothesis": i.hypothesis,
                    "action": i.action,
                    "priority": i.priority,
                    "status": i.status.value,
                    "budget": {
                        "max_steps": i.budget.max_steps,
                        "expiry_s": i.budget.expiry_s,
                        "created_ts": i.budget.created_ts,
                        "steps_used": i.budget.steps_used,
                    },
                    "depends_on": list(i.depends_on),
                    "evidence": list(i.evidence),
                    "actor": i.actor,
                    "spawned_from": i.spawned_from,
                    "result": i.result,
                    "resolved_by": i.resolved_by,
                }
                for i in self._intents.values()
            ],
            "dead_ends": [
                {"intent_id": d.intent_id, "hypothesis": d.hypothesis,
                 "reason": d.reason, "category": d.category, "ts": d.ts}
                for d in self._dead_ends
            ],
            "enabled_by": self._enabled_by,
            "history": self._history,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Frontier":
        f = cls()
        for raw in (data or {}).get("intents") or []:
            if not isinstance(raw, dict):
                continue
            try:
                status = IntentStatus(raw.get("status", "open"))
            except ValueError:
                continue
            b = raw.get("budget") or {}
            intent = Intent(
                id=raw.get("id", ""),
                hypothesis=raw.get("hypothesis", ""),
                action=raw.get("action", ""),
                priority=int(raw.get("priority", 3) or 3),
                status=status,
                budget=IntentBudget(
                    max_steps=int(b.get("max_steps", 8) or 8),
                    expiry_s=float(b.get("expiry_s", 900.0) or 900.0),
                    created_ts=float(b.get("created_ts", 0.0) or 0.0),
                    steps_used=int(b.get("steps_used", 0) or 0),
                ),
                depends_on=tuple(raw.get("depends_on") or ()),
                evidence=tuple(raw.get("evidence") or ()),
                actor=raw.get("actor", "master"),
                spawned_from=raw.get("spawned_from"),
                result=raw.get("result", ""),
                resolved_by=raw.get("resolved_by"),
            )
            f._intents[intent.id] = intent
        for raw in (data or {}).get("dead_ends") or []:
            if isinstance(raw, dict) and raw.get("intent_id"):
                f._dead_ends.append(
                    DeadEnd(
                        intent_id=raw["intent_id"],
                        hypothesis=raw.get("hypothesis", ""),
                        reason=raw.get("reason", ""),
                        category=raw.get("category", "unknown"),
                        ts=float(raw.get("ts", 0.0) or 0.0),
                    )
                )
        f._enabled_by = {
            k: list(v)
            for k, v in ((data or {}).get("enabled_by") or {}).items()
            if isinstance(v, list)
        }
        f._history = [
            e for e in ((data or {}).get("history") or []) if isinstance(e, dict)
        ]
        return f
