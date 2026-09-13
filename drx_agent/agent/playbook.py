"""跨挑战经验库：带准入门控、作用域与 TTL 的可复用打法沉淀。

运行期学习（不是内置知识）：条目只来自本次跑分中已通关题目的复盘提取，
存放在工作目录的 JSON 文件里，随跑分进程持久化。

准入门控（EXOBoost 报告核心原则）：原始任务输出永远不能自动成为长期知识。
``add()`` 只产生 ``candidate``；只有通过 ``admit()`` 门控（有 provenance 的
run/challenge 标识、有 scope.category、且未过期）才会成为 ``active``，而只有
``active`` 条目才会被注入 prompt。反思可以保持 ``unresolved`` 而不被强制成
"经验教训"；被排除的候选（negative finding）走独立车道，避免重复调查已知的
误报，但 revision 变化时负面记忆会被作废。
"""

# allow: SIZE_OK — 任务强制的单一数据契约文件：入口 schema（~15 字段）+
# 准入门控 + 作用域匹配 + TTL 重验 + 负记忆车道 + 迁移/持久化是不可分割的
# 一个 API 面，无法拆分而不破坏交付规格。

from __future__ import annotations

import json
import os
import re
import time
import uuid

MAX_ENTRIES = 100

_VALID_EPISTEMIC = ("observed", "evidence_supported", "unresolved", "causally_verified")
_VALID_ADMISSION = ("candidate", "active", "rejected")

# 裁剪优先级：rejected 最先被裁，active 只要有 candidate 在就绝不裁剪。
_ADMISSION_RANK = {"rejected": 0, "candidate": 1, "active": 2}


class Playbook:
    def __init__(self, path: str):
        self.path = path
        self.entries: list = []
        self.load()

    # ------------------------------------------------------------------ #
    # persistence
    # ------------------------------------------------------------------ #
    def load(self) -> None:
        data = None
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = None
        raw: list = []
        if isinstance(data, dict):
            raw = data.get("entries") or []
        elif isinstance(data, list):
            raw = data
        migrated: list = []
        for e in raw:
            if isinstance(e, dict):
                migrated.append(self._migrate_entry(e))
        self.entries = migrated

    def save(self) -> None:
        try:
            self._prune()
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"entries": self.entries}, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def _prune(self) -> None:
        if len(self.entries) <= MAX_ENTRIES:
            return
        self.entries.sort(
            key=lambda e: (
                _ADMISSION_RANK.get(e.get("admission"), 1),
                float(e.get("created_at", 0) or 0),
            )
        )
        self.entries = self.entries[-MAX_ENTRIES:]

    # ------------------------------------------------------------------ #
    # migration / coercion
    # ------------------------------------------------------------------ #
    def _migrate_entry(self, e: dict) -> dict:
        """旧扁平 schema（{id, category, kind, lesson, evidence, created_at}）
        迁移为新 schema，admission=candidate、provenance 无 run/challenge ——
        旧条目绝不自动准入。新 schema 条目则做字段兜底（不崩溃、填默认值）。"""
        if "admission" not in e and "version" not in e:
            now = float(e.get("created_at", time.time()) or time.time())
            return {
                "id": str(e.get("id", "") or f"pb-{uuid.uuid4().hex[:6]}"),
                "created_at": now,
                "last_verified_at": now,
                "expires_after": 0,
                "revalidate_policy": "",
                "version": {"schema": 2, "entry": 1},
                "admission": "candidate",
                "epistemic_status": "observed",
                "confidence": 0.5,
                "provenance": {
                    "run_id": "",
                    "challenge": "",
                    "evidence_refs": [str(x)[:300] for x in (e.get("evidence") or [])],
                    "producer": "",
                },
                "scope": {
                    "category": str(e.get("category", "general") or "general")[:40],
                    "applies_to": [],
                    "does_not_apply_to": [],
                    "revision": "",
                },
                "kind": str(e.get("kind", "") or "")[:200],
                "lesson": str(e.get("lesson", "") or "")[:800],
                "counter_explanations": [],
                "negative": False,
                "rejection_reason": "",
            }
        return self._coerce_entry(e)

    def _coerce_entry(self, e: dict) -> dict:
        now = time.time()
        raw_prov = e.get("provenance")
        prov = raw_prov if isinstance(raw_prov, dict) else {}
        raw_scope = e.get("scope")
        scope = raw_scope if isinstance(raw_scope, dict) else {}
        raw_version = e.get("version")
        version = raw_version if isinstance(raw_version, dict) else {}
        epistemic = e.get("epistemic_status", "observed")
        if epistemic not in _VALID_EPISTEMIC:
            epistemic = "observed"
        admission = e.get("admission", "candidate")
        if admission not in _VALID_ADMISSION:
            admission = "candidate"
        confidence = float(e.get("confidence", 0.5) or 0.5)
        confidence = max(0.0, min(1.0, confidence))
        created = float(e.get("created_at", now) or now)
        return {
            "id": str(e.get("id", "") or f"pb-{uuid.uuid4().hex[:6]}"),
            "created_at": created,
            "last_verified_at": float(e.get("last_verified_at", created) or created),
            "expires_after": int(e.get("expires_after", 0) or 0),
            "revalidate_policy": str(e.get("revalidate_policy", "") or ""),
            "version": {
                "schema": int(version.get("schema", 2) or 2),
                "entry": int(version.get("entry", 1) or 1),
            },
            "admission": admission,
            "epistemic_status": epistemic,
            "confidence": confidence,
            "provenance": {
                "run_id": str(prov.get("run_id", "") or ""),
                "challenge": str(prov.get("challenge", "") or ""),
                "evidence_refs": [str(x) for x in (prov.get("evidence_refs") or [])],
                "producer": str(prov.get("producer", "") or ""),
            },
            "scope": {
                "category": str(scope.get("category", "") or ""),
                "applies_to": [str(x) for x in (scope.get("applies_to") or [])],
                "does_not_apply_to": [str(x) for x in (scope.get("does_not_apply_to") or [])],
                "revision": str(scope.get("revision", "") or ""),
            },
            "kind": str(e.get("kind", "") or ""),
            "lesson": str(e.get("lesson", "") or ""),
            "counter_explanations": [str(x) for x in (e.get("counter_explanations") or [])],
            "negative": bool(e.get("negative", False)),
            "rejection_reason": str(e.get("rejection_reason", "") or ""),
        }

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _find(self, entry_id: str):
        for e in self.entries:
            if e.get("id") == entry_id:
                return e
        return None

    @staticmethod
    def _tokens(text: str) -> set:
        t = (text or "").lower()
        norm = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", t)
        grams = {norm[i:i + 2] for i in range(len(norm) - 1)} if len(norm) >= 2 else {norm}
        words = set(re.findall(r"[a-z0-9_]+", t))
        return grams | words

    @staticmethod
    def _contains(text: str, needle: str) -> bool:
        n = (needle or "").strip().lower()
        if not n:
            return False
        return n in (text or "").lower()

    @staticmethod
    def _is_expired(e: dict, now: float) -> bool:
        ttl = int(e.get("expires_after", 0) or 0)
        if ttl <= 0:
            return False
        return (now - float(e.get("last_verified_at", 0) or 0)) > ttl

    # ------------------------------------------------------------------ #
    # write path: candidate -> admit/reject
    # ------------------------------------------------------------------ #
    def add(
        self,
        category: str,
        kind: str,
        lesson: str,
        evidence=None,
        *,
        provenance=None,
        scope=None,
        epistemic_status="observed",
        confidence=0.5,
        counter_explanations=None,
        negative=False,
    ) -> str:
        """创建一条 ``candidate``（永不 ``active``）。lesson 为空时拒绝返回 ""。"""
        lesson = (lesson or "").strip()
        if not lesson:
            return ""
        now = time.time()
        scope = dict(scope or {})
        scope.setdefault("category", (category or "general")[:40])
        scope.setdefault("applies_to", [])
        scope.setdefault("does_not_apply_to", [])
        scope.setdefault("revision", "")
        prov = dict(provenance or {})
        prov.setdefault("run_id", "")
        prov.setdefault("challenge", "")
        prov.setdefault("producer", "")
        evidence_refs = [str(x)[:300] for x in (evidence or [])[:3]]
        if evidence_refs and not prov.get("evidence_refs"):
            prov["evidence_refs"] = evidence_refs
        prov.setdefault("evidence_refs", evidence_refs)
        if epistemic_status not in _VALID_EPISTEMIC:
            epistemic_status = "observed"
        confidence = max(0.0, min(1.0, float(confidence or 0.5)))
        eid: str = f"pb-{uuid.uuid4().hex[:6]}"
        entry = {
            "id": eid,
            "created_at": now,
            "last_verified_at": now,
            "expires_after": 0,
            "revalidate_policy": "",
            "version": {"schema": 2, "entry": 1},
            "admission": "candidate",
            "epistemic_status": epistemic_status,
            "confidence": confidence,
            "provenance": prov,
            "scope": scope,
            "kind": (kind or "")[:200],
            "lesson": lesson[:800],
            "counter_explanations": [str(c)[:300] for c in (counter_explanations or [])],
            "negative": bool(negative),
            "rejection_reason": "",
        }
        self.entries.append(entry)
        self._prune()
        self.save()
        return eid

    def admit(self, entry_id: str) -> bool:
        """准入门控：candidate -> active 仅当 (a) provenance 有 run/challenge 标识、
        (b) scope 有 category、(c) 未过期。否则保持 candidate 返回 False。"""
        e = self._find(entry_id)
        if e is None:
            return False
        if e.get("admission") == "active":
            return True
        if e.get("admission") != "candidate":
            return False
        prov = e.get("provenance", {})
        has_provenance = bool((prov.get("run_id") or "").strip() or (prov.get("challenge") or "").strip())
        scope = e.get("scope", {})
        has_category = bool((scope.get("category") or "").strip())
        if not has_provenance or not has_category:
            return False
        now = time.time()
        if self._is_expired(e, now):
            return False
        e["admission"] = "active"
        e["last_verified_at"] = now
        self.save()
        return True

    def reject(self, entry_id: str, reason: str) -> bool:
        """负记忆路径：显式否决一条 candidate，记录原因。"""
        e = self._find(entry_id)
        if e is None:
            return False
        e["admission"] = "rejected"
        e["rejection_reason"] = (reason or "")[:200]
        self.save()
        return True

    # ------------------------------------------------------------------ #
    # read path: only active, with TTL + scope + revision filters
    # ------------------------------------------------------------------ #
    def demote_expired(self, now=None) -> int:
        """把过期 active 打回 candidate（需重新验证）。返回受影响条数。"""
        now = time.time() if now is None else now
        count = 0
        for e in self.entries:
            if e.get("admission") != "active":
                continue
            if self._is_expired(e, now):
                e["admission"] = "candidate"
                count += 1
        return count

    def invalidate_for_revision(self, revision: str) -> int:
        """revision 变化时，把 scope.revision 不匹配且非 causally_verified 的
        active 条目打回 candidate。返回受影响条数。"""
        revision = str(revision or "")
        count = 0
        for e in self.entries:
            if e.get("admission") != "active":
                continue
            if e.get("epistemic_status") == "causally_verified":
                continue
            if str(e.get("scope", {}).get("revision", "") or "") != revision:
                e["admission"] = "candidate"
                count += 1
        return count

    def negative_entries(self) -> list:
        """负记忆车道：negative=True 且 active 的条目（已知误报，别重复调查）。"""
        return [
            e for e in self.entries
            if e.get("negative") and e.get("admission") == "active"
        ]

    def query(self, text: str, top_k: int = 3, threshold: float = 0.25, *, category=None, revision=None) -> list:
        """仅返回 active 条目，应用 TTL / does_not_apply_to 排除 / applies_to 与
        category 加权 / revision 不匹配降权。"""
        self.demote_expired()
        t = self._tokens(text)
        if not t:
            return []
        category = (category or "").strip().lower()
        scored: list = []
        for e in self.entries:
            if e.get("admission") != "active":
                continue
            scope = e.get("scope", {})
            blocked = False
            for neg in scope.get("does_not_apply_to") or []:
                if self._contains(text, neg):
                    blocked = True
                    break
            if blocked:
                continue
            et = self._tokens(str(e.get("kind", "")) + " " + str(scope.get("category", "")))
            denom = min(len(t), len(et))
            if not denom:
                continue
            sim = len(t & et) / denom
            if sim < threshold:
                continue
            for pos in scope.get("applies_to") or []:
                if self._contains(text, pos):
                    sim += 0.2
                    break
            if category and category == str(scope.get("category", "") or "").lower():
                sim += 0.2
            entry_rev = str(scope.get("revision", "") or "")
            if (
                revision
                and entry_rev
                and entry_rev != revision
                and e.get("epistemic_status") != "causally_verified"
            ):
                sim *= 0.5
            scored.append((sim, e))
        scored.sort(key=lambda x: -x[0])
        return [e for _, e in scored[:top_k]]

    def render(self, text: str, top_k: int = 3) -> str:
        hits = self.query(text, top_k)
        if not hits:
            return ""
        lines = ["【已准入经验 — 范围受限的启发式，非本任务已证实事实（仅供同类题参考）】"]
        for e in hits:
            scope = e.get("scope", {})
            lines.append(
                f"- [{scope.get('category', '?')}] {e.get('kind', '')}: {e.get('lesson', '')}"
                f" [epistemic={e.get('epistemic_status', 'observed')}]"
            )
        return "\n".join(lines)
