"""跨挑战经验库：通关后沉淀可复用打法，后续同类题启动时注入参考。

运行期学习（不是内置知识）：条目只来自本次跑分中已通关题目的复盘提取，
存放在工作目录的 JSON 文件里，随跑分进程持久化。
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid

MAX_ENTRIES = 100


class Playbook:
    def __init__(self, path: str):
        self.path = path
        self.entries: list = []
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.entries = [e for e in (data.get("entries") or []) if isinstance(e, dict)]
        except Exception:
            self.entries = []

    def save(self) -> None:
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(
                    {"entries": self.entries[-MAX_ENTRIES:]},
                    f,
                    ensure_ascii=False,
                    indent=1,
                )
        except Exception:
            pass

    @staticmethod
    def _tokens(text: str) -> set:
        t = (text or "").lower()
        norm = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", t)
        grams = {norm[i:i + 2] for i in range(len(norm) - 1)} if len(norm) >= 2 else {norm}
        words = set(re.findall(r"[a-z0-9_]+", t))
        return grams | words

    def query(self, text: str, top_k: int = 3, threshold: float = 0.25) -> list:
        t = self._tokens(text)
        if not t:
            return []
        scored: list = []
        for e in self.entries:
            et = self._tokens(str(e.get("kind", "")) + " " + str(e.get("category", "")))
            denom = min(len(t), len(et))
            if not denom:
                continue
            sim = len(t & et) / denom
            if sim >= threshold:
                scored.append((sim, e))
        scored.sort(key=lambda x: -x[0])
        return [e for _, e in scored[:top_k]]

    def render(self, text: str, top_k: int = 3) -> str:
        hits = self.query(text, top_k)
        if not hits:
            return ""
        lines = ["【历史同类题经验（本次跑分中已通关题目沉淀，供参考）】"]
        for e in hits:
            lines.append(
                f"- [{e.get('category', '?')}] {e.get('kind', '')}: {e.get('lesson', '')}"
            )
            for ev in (e.get("evidence") or [])[:2]:
                lines.append(f"    例: {str(ev)[:160]}")
        return "\n".join(lines)

    def add(self, category: str, kind: str, lesson: str, evidence: list) -> str:
        eid = f"pb-{uuid.uuid4().hex[:6]}"
        self.entries.append(
            {
                "id": eid,
                "category": (category or "general")[:40],
                "kind": (kind or "")[:200],
                "lesson": (lesson or "")[:800],
                "evidence": [str(x)[:300] for x in (evidence or [])[:3]],
                "created_at": time.time(),
            }
        )
        self.save()
        return eid
