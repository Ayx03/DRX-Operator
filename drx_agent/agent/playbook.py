"""跨挑战经验库：带准入门控、作用域与 TTL 的可复用打法沉淀。

项目长期记忆与跨挑战经验共用此 JSON 存储。项目命名空间严格隔离；
未指定命名空间的旧调用只访问原有的跨挑战经验。

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
from copy import deepcopy
import os
import re
import tempfile
import time
import uuid

MAX_ENTRIES = 1000

_VALID_EPISTEMIC = ("observed", "evidence_supported", "unresolved", "causally_verified")
_VALID_ADMISSION = ("candidate", "active", "rejected")

# 只有未准入或已过期的记录可以为容量让位，绝不裁剪有效 active 知识。
_ADMISSION_RANK = {"rejected": 0, "candidate": 1, "active": 2}


class Playbook:
    def __init__(self, path: str, *, max_entries: int = 1000):
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        self.path = path
        self.max_entries = max_entries
        self.entries: list[dict] = []
        self.load()

    # ------------------------------------------------------------------ #
    # persistence
    # ------------------------------------------------------------------ #
    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            self.entries = []
            return
        raw = data.get("entries") if isinstance(data, dict) else data
        if not isinstance(raw, list) or any(not isinstance(e, dict) for e in raw):
            raise ValueError("Invalid playbook: expected a list of entry objects")
        self.entries = self._bounded([self._migrate_entry(e) for e in raw])

    def save(self) -> None:
        """原子替换文件；所有读写错误向调用者传播，不能伪报保存成功。"""
        self._commit(self.entries)

    def _commit(self, entries: list[dict], *, protected: str = "") -> None:
        entries = self._bounded(entries, protected=protected)
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        temporary = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=directory,
                prefix=".playbook-", suffix=".tmp", delete=False,
            ) as f:
                temporary = f.name
                json.dump({"entries": entries}, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        # 发布仅发生在持久化成功后；失败的 add/admit 不污染当前实例。
        self.entries = entries

    def _bounded(self, entries: list[dict], *, protected: str = "") -> list[dict]:
        excess = len(entries) - self.max_entries
        if excess <= 0:
            return entries
        now = time.time()
        victims = [
            (index, e) for index, e in enumerate(entries)
            if e.get("id") != protected
            and (e.get("admission") != "active" or self._is_expired(e, now))
        ]
        if len(victims) < excess:
            raise ValueError("Playbook capacity reached; no safe entry can be removed")
        victims.sort(key=lambda item: (
            _ADMISSION_RANK.get(item[1].get("admission"), 1),
            float(item[1].get("created_at", 0) or 0),
            item[0],
        ))
        removed = {index for index, _ in victims[:excess]}
        return [e for index, e in enumerate(entries) if index not in removed]

    def _replace(self, old: dict, new: dict) -> None:
        self._commit(
            [new if e is old else e for e in self.entries], protected=new["id"],
        )

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
                    "evidence_refs": [str(x) for x in (e.get("evidence") or [])],
                    "producer": "",
                    "source": "",
                },
                "scope": {
                    "category": str(e.get("category", "general") or "general")[:40],
                    "applies_to": [],
                    "does_not_apply_to": [],
                    "revision": "",
                    "namespace": "",
                },
                "kind": str(e.get("kind", "") or ""),
                "lesson": str(e.get("lesson", "") or ""),
                "counter_explanations": [],
                "negative": False,
                "tags": [],
                "invalidation_reason": "",
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
        confidence = float(e.get("confidence", 0.5))
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
                "source": str(prov.get("source", "") or ""),
            },
            "scope": {
                "category": str(scope.get("category", "") or ""),
                "applies_to": [str(x) for x in (scope.get("applies_to") or [])],
                "does_not_apply_to": [str(x) for x in (scope.get("does_not_apply_to") or [])],
                "revision": str(scope.get("revision", "") or ""),
                "namespace": str(scope.get("namespace", "") or ""),
            },
            "kind": str(e.get("kind", "") or ""),
            "lesson": str(e.get("lesson", "") or ""),
            "counter_explanations": [str(x) for x in (e.get("counter_explanations") or [])],
            "negative": bool(e.get("negative", False)),
            "tags": list(dict.fromkeys(str(x) for x in (e.get("tags") or []))),
            "invalidation_reason": str(e.get("invalidation_reason", "") or ""),
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
        return (grams | words) - {""}

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
        return (now - float(e.get("last_verified_at", 0) or 0)) >= ttl

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
        namespace: str = "",
        source: str = "",
        tags: list[str] | None = None,
        expires_after: int = 0,
    ) -> str:
        """创建一条 ``candidate``（永不 ``active``）。lesson 为空时拒绝返回 ""。"""
        lesson = (lesson or "").strip()
        if not lesson:
            return ""
        if isinstance(expires_after, bool) or not isinstance(expires_after, int) or expires_after < 0:
            raise ValueError("expires_after must be a non-negative number of seconds")
        now = time.time()
        scope = deepcopy(dict(scope or {}))
        scope.setdefault("category", (category or "general")[:40])
        scope.setdefault("applies_to", [])
        scope.setdefault("does_not_apply_to", [])
        scope.setdefault("revision", "")
        scope["namespace"] = str(namespace)
        prov = deepcopy(dict(provenance or {}))
        prov.setdefault("run_id", "")
        prov.setdefault("challenge", "")
        prov.setdefault("producer", "")
        prov["source"] = str(source)
        evidence_refs = [str(x) for x in (evidence or [])]
        prov["evidence_refs"] = list(dict.fromkeys([
            *(str(x) for x in (prov.get("evidence_refs") or [])), *evidence_refs,
        ]))
        if epistemic_status not in _VALID_EPISTEMIC:
            epistemic_status = "observed"
        confidence = max(0.0, min(1.0, float(confidence)))
        eid: str = f"pb-{uuid.uuid4().hex}"
        entry = {
            "id": eid,
            "created_at": now,
            "last_verified_at": now,
            "expires_after": expires_after,
            "revalidate_policy": "",
            "version": {"schema": 2, "entry": 1},
            "admission": "candidate",
            "epistemic_status": epistemic_status,
            "confidence": confidence,
            "provenance": prov,
            "scope": scope,
            "kind": kind or "",
            "lesson": lesson,
            "counter_explanations": [str(c) for c in (counter_explanations or [])],
            "negative": bool(negative),
            "tags": list(dict.fromkeys(str(tag).strip() for tag in (tags or []) if str(tag).strip())),
            "invalidation_reason": "",
            "rejection_reason": "",
        }
        self._commit([*self.entries, entry], protected=eid)
        return eid

    def admit(self, entry_id: str) -> bool:
        """准入门控：candidate -> active 仅当 (a) provenance 有 run/challenge 标识、
        (b) scope 有 category、(c) 未过期。否则保持 candidate 返回 False。"""
        e = self._find(entry_id)
        if e is None:
            return False
        if e.get("admission") == "active":
            return not self._is_expired(e, time.time())
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
        self._replace(e, {**e, "admission": "active", "last_verified_at": now,
                          "invalidation_reason": ""})
        return True

    def reject(self, entry_id: str, reason: str) -> bool:
        """负记忆路径：显式否决一条 candidate，记录原因。"""
        e = self._find(entry_id)
        if e is None:
            return False
        self._replace(e, {**e, "admission": "rejected", "rejection_reason": (reason or "")[:200]})
        return True

    # ------------------------------------------------------------------ #
    # read path: only active, with TTL + scope + revision filters
    # ------------------------------------------------------------------ #
    def demote_expired(self, now=None) -> int:
        """把过期 active 打回 candidate（需重新验证），并持久化。"""
        now = time.time() if now is None else now
        entries = [
            {**e, "admission": "candidate", "invalidation_reason": "expired"}
            if e.get("admission") == "active" and self._is_expired(e, now) else e
            for e in self.entries
        ]
        count = sum(old is not new for old, new in zip(self.entries, entries))
        if count:
            self._commit(entries)
        return count

    def invalidate_for_revision(self, revision: str) -> int:
        """旧空命名空间 API：revision 不匹配的非因果验证经验需重新准入。

        负面记忆即使 causally_verified 也必须在 revision 变化后重验。
        """
        revision = str(revision or "")
        entries = []
        count = 0
        for e in self.entries:
            scope = e.get("scope", {})
            if (
                e.get("admission") == "active" and not scope.get("namespace", "")
                and (e.get("negative") or e.get("epistemic_status") != "causally_verified")
                and str(scope.get("revision", "") or "") != revision
            ):
                e = {**e, "admission": "candidate", "invalidation_reason": "revision changed"}
                count += 1
            entries.append(e)
        if count:
            self._commit(entries)
        return count

    def get(self, entry_id: str, *, namespace: str) -> dict | None:
        """按命名空间读取任意准入状态供人工/主代理审核，不是召回准入。"""
        e = self._find(entry_id)
        if e is None or e.get("scope", {}).get("namespace", "") != namespace:
            return None
        return deepcopy(e)

    @staticmethod
    def _recallable(e: dict, namespace: str, revision: str | None, now: float) -> bool:
        scope = e.get("scope", {})
        entry_revision = scope.get("revision", "")
        return (
            e.get("admission") == "active"
            and scope.get("namespace", "") == namespace
            and not Playbook._is_expired(e, now)
            and (revision is None or not entry_revision or entry_revision == revision)
        )

    @staticmethod
    def _lexical_tokens(text: str) -> set[str]:
        # 英文词精确匹配，汉字使用相邻二元组；不需要向量模型或外部服务。
        result = set(re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE))
        for span in re.findall(r"[\u4e00-\u9fff]+", text):
            result.update(span[i:i + 2] for i in range(len(span) - 1))
        return result

    def search(
        self, text: str, *, namespace: str, revision: str = "", category: str = "",
        top_k: int = 5, min_confidence: float = 0.0,
    ) -> list[dict]:
        """真实内容的确定性词法召回。限定 revision 必须匹配；空 revision
        仅召回未绑定 revision 的知识。结果保留完整来源、负记忆标记和 relevance。
        """
        tokens = self._lexical_tokens(text)
        if not tokens or top_k <= 0:
            return []
        now = time.time()
        scored = []
        for e in self.entries:
            if not self._recallable(e, namespace, revision, now):
                continue
            scope = e.get("scope", {})
            if category and str(scope.get("category", "")).casefold() != category.casefold():
                continue
            if float(e.get("confidence", 0.0)) < min_confidence:
                continue
            if any(self._contains(text, neg) for neg in scope.get("does_not_apply_to", [])):
                continue
            score = sum(
                weight * len(tokens & self._lexical_tokens(content)) / len(tokens)
                for weight, content in (
                    (3.0, e.get("lesson", "")),
                    (2.0, e.get("kind", "")),
                    (2.0, " ".join(e.get("tags", []))),
                    (1.0, scope.get("category", "")),
                )
            )
            if score <= 0:
                continue
            if any(self._contains(text, pos) for pos in scope.get("applies_to", [])):
                score += 0.2
            scored.append((score, e))
        scored.sort(key=lambda item: (
            -item[0], -float(item[1].get("confidence", 0.0)),
            float(item[1].get("created_at", 0.0)), item[1]["id"],
        ))
        return [{**deepcopy(e), "relevance": score} for score, e in scored[:top_k]]

    def invalidate(
        self, *, namespace: str, revision: str | None = None,
        source: str | None = None, reason: str,
    ) -> int:
        """精确选择 namespace 及可选 revision/source，将匹配 active 降为 candidate。

        None 是不限定该字段，"" 仅匹配空字段；不改变 rejected/candidate。
        """
        reason = reason.strip()
        if not reason:
            raise ValueError("An invalidation reason is required")
        entries = []
        count = 0
        for e in self.entries:
            scope, prov = e.get("scope", {}), e.get("provenance", {})
            if (
                e.get("admission") == "active"
                and scope.get("namespace", "") == namespace
                and (revision is None or scope.get("revision", "") == revision)
                and (source is None or prov.get("source", "") == source)
            ):
                e = {**e, "admission": "candidate", "invalidation_reason": reason}
                count += 1
            entries.append(e)
        if count:
            self._commit(entries)
        return count

    def consolidate(self, *, namespace: str) -> dict:
        """只合并完全一致的语义/来源/准入状态，保留证据并使用更早的验证时刻。

        run/challenge/producer 也需相同，避免把不同观察的出处混成同一记录。
        """
        now = time.time()
        positions: dict[str, int] = {}
        entries = []
        removed = []
        for e in self.entries:
            if e.get("scope", {}).get("namespace", "") != namespace:
                entries.append(e)
                continue
            semantic = {
                k: v for k, v in e.items()
                if k not in {"id", "created_at", "last_verified_at", "version", "provenance"}
            }
            semantic["provenance"] = {
                k: v for k, v in e.get("provenance", {}).items() if k != "evidence_refs"
            }
            semantic["expired"] = self._is_expired(e, now)
            semantic["schema"] = e.get("version", {}).get("schema", 2)
            key = json.dumps(semantic, sort_keys=True, ensure_ascii=False)
            if key not in positions:
                positions[key] = len(entries)
                entries.append(e)
                continue
            index = positions[key]
            previous = entries[index]
            prov = previous.get("provenance", {})
            refs = list(dict.fromkeys([
                *prov.get("evidence_refs", []),
                *e.get("provenance", {}).get("evidence_refs", []),
            ]))
            entries[index] = {
                **previous, "provenance": {**prov, "evidence_refs": refs},
                "created_at": min(previous["created_at"], e["created_at"]),
                "last_verified_at": min(previous["last_verified_at"], e["last_verified_at"]),
                "version": {**previous["version"], "entry": previous["version"]["entry"] + 1},
            }
            removed.append(e["id"])
        if removed:
            self._commit(entries)
        return {
            "merged": len(removed), "removed_ids": removed,
            "remaining": sum(e.get("scope", {}).get("namespace", "") == namespace for e in entries),
        }

    def negative_entries(self, *, namespace: str = "", revision: str | None = None) -> list:
        """负记忆车道：只返回命名空间内未过期且 active 的已知误报。"""
        now = time.time()
        return [
            deepcopy(e) for e in self.entries
            if e.get("negative") and self._recallable(e, namespace, revision, now)
        ]

    def query(self, text: str, top_k: int = 3, threshold: float = 0.25, *, category=None, revision=None) -> list:
        """旧空命名空间的相似题检索：仅 active、未过期、作用域兼容记录。"""
        now = time.time()
        t = self._tokens(text)
        if not t or top_k <= 0:
            return []
        category = (category or "").strip().lower()
        scored: list = []
        for e in self.entries:
            if not self._recallable(e, "", revision, now):
                continue
            scope = e.get("scope", {})
            blocked = False
            for neg in scope.get("does_not_apply_to") or []:
                if self._contains(text, neg):
                    blocked = True
                    break
            if blocked:
                continue
            et = self._tokens(" ".join((
                str(e.get("kind", "")), str(e.get("lesson", "")),
                str(scope.get("category", "")), " ".join(e.get("tags", [])),
            )))
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
            scored.append((sim, e))
        scored.sort(key=lambda x: -x[0])
        return [deepcopy(e) for _, e in scored[:top_k]]

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
                f" [negative={str(bool(e.get('negative'))).lower()}]"
            )
        return "\n".join(lines)
