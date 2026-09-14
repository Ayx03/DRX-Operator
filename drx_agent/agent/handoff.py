"""Structured Handoff — typed cross-stage state transfer contract.

Design principles (EXOBoost research report):
- 跨阶段传状态，不传思维历史：Handoff is NOT a conversation summary — it is an
  evidence container. Every item carries epistemic status and provenance.
- Candidate 永远不是 Fact：status must say how the claim was arrived at
  (observed | inferred | hypothesis | verified | rejected); verified claims
  must carry evidence refs.
- 存在 ≠ 必须加载：the reader pokes selectively — render_index() tells a fresh
  worker "a handoff exists, read only what you need", read() pages by section
  or fetches one item by id.

Self-contained: no imports from drx_agent.agent.master (callers pass objects in).
"""

# allow: SIZE_OK — task-mandated single-file data contract: 10 section lists +
# 2 dataclasses + serialization/paging are one indivisible API surface that
# cannot be split without violating the deliverable spec.

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field

from drx_agent.agent.consensus import verification_outcome

# Epistemic status enum. "observed" = directly witnessed; "inferred" = deduced
# from evidence; "hypothesis" = proposed, unproven; "verified" = confirmed by
# evidence; "rejected" = tried and excluded.
STATUSES: tuple[str, ...] = ("observed", "inferred", "hypothesis", "verified", "rejected")

# kind (section name) -> (id prefix, human label).
_SECTION_SPECS: tuple[tuple[str, str, str], ...] = (
    ("facts", "FACT", "已确认事实"),
    ("hypotheses", "HYP", "待验证假设"),
    ("candidates", "CAND", "候选发现"),
    ("negative_findings", "NEG", "负面发现（死路/排除）"),
    ("open_questions", "Q", "待解问题"),
    ("entrypoints", "ENTRY", "入口点"),
    ("trust_boundaries", "TRUST", "信任边界"),
    ("important_files", "FILE", "重要文件"),
    ("evidence_refs", "REF", "证据引用"),
    ("recommended_work", "WORK", "建议后续工作"),
)

_SECTION_NAMES: tuple[str, ...] = tuple(spec[0] for spec in _SECTION_SPECS)
_PREFIX_TO_SECTION: dict[str, str] = {spec[1]: spec[0] for spec in _SECTION_SPECS}
_SECTION_TO_PREFIX: dict[str, str] = {spec[0]: spec[1] for spec in _SECTION_SPECS}
_LABELS: dict[str, str] = {spec[0]: spec[2] for spec in _SECTION_SPECS}

_DEDUP_THRESHOLD = 0.8
_CLAIM_MAX_CHARS = 400
_INDEX_MAX_CHARS = 800


def _str_list(value: list[str] | tuple[str, ...] | None) -> list[str]:
    return [str(e) for e in (value or [])]


def _norm_section(section: str) -> str:
    """Normalize a section name or an id prefix ("CAND" -> "candidates")."""
    key = (section or "").strip()
    low = key.lower()
    if low in _SECTION_TO_PREFIX:
        return low
    upper = key.upper()
    return _PREFIX_TO_SECTION.get(upper, "")


def _tokens(text: str) -> set[str]:
    """词元 + 中文二元组（中文改写用 bigram 比单字 Jaccard 稳）。

    Same scheme as Frontier._tokens so cross-stage dedup behaves identically.
    """
    t = (text or "").lower()
    norm = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", t)
    grams = {norm[i : i + 2] for i in range(len(norm) - 1)} if len(norm) >= 2 else {norm}
    words = set(re.findall(r"[a-z0-9_]+", t))
    return grams | words


@dataclass
class HandoffItem:
    """One cross-stage state item: claim + epistemic status + provenance."""

    id: str
    claim: str
    status: str = "inferred"
    confidence: float = 0.5
    producer: str = ""
    evidence: list[str] = field(default_factory=list)
    counterevidence: list[str] = field(default_factory=list)
    scope: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "HandoffItem":
        status = str(data.get("status", "inferred"))
        return cls(
            id=str(data.get("id", "")),
            claim=str(data.get("claim", "")),
            status=status if status in STATUSES else "inferred",
            confidence=float(data.get("confidence", 0.5) or 0.5),
            producer=str(data.get("producer", "")),
            evidence=_str_list(data.get("evidence")),
            counterevidence=_str_list(data.get("counterevidence")),
            scope=str(data.get("scope", "")),
            created_at=float(data.get("created_at", 0.0) or 0.0),
        )


@dataclass
class Handoff:
    """Typed, serializable handoff contract across agent stage transitions."""

    version: str = "1"
    phase: str = ""
    project_revision: str = ""
    facts: list[HandoffItem] = field(default_factory=list)
    hypotheses: list[HandoffItem] = field(default_factory=list)
    candidates: list[HandoffItem] = field(default_factory=list)
    negative_findings: list[HandoffItem] = field(default_factory=list)
    open_questions: list[HandoffItem] = field(default_factory=list)
    entrypoints: list[HandoffItem] = field(default_factory=list)
    trust_boundaries: list[HandoffItem] = field(default_factory=list)
    important_files: list[HandoffItem] = field(default_factory=list)
    evidence_refs: list[HandoffItem] = field(default_factory=list)
    recommended_work: list[HandoffItem] = field(default_factory=list)
    coverage: dict[str, int] = field(default_factory=dict)

    def add(
        self,
        kind: str,
        claim: str,
        *,
        status: str = "inferred",
        confidence: float = 0.5,
        producer: str = "",
        evidence: list[str] | tuple[str, ...] = (),
        counterevidence: list[str] | tuple[str, ...] = (),
        scope: str = "",
        created_at: float | None = None,
        item_id: str | None = None,
    ) -> HandoffItem | None:
        """Append one item to the section `kind` selects. Dedup by normalized
        claim (bigram containment, threshold 0.8) — returns None when the
        claim is empty or already covered by an existing item in that section.
        """
        section = _norm_section(kind)
        claim = (claim or "").strip()[:_CLAIM_MAX_CHARS]
        if not section or not claim:
            return None
        if status not in STATUSES:
            status = "inferred"
        if status == "verified" and not evidence:
            status = "inferred"  # Candidate 永远不是 Fact
        bucket: list[HandoffItem] = getattr(self, section)
        new_tokens = _tokens(claim)
        for existing in bucket:
            e = _tokens(existing.claim)
            denom = min(len(new_tokens), len(e))
            if new_tokens and denom and (len(new_tokens & e) / denom) >= _DEDUP_THRESHOLD:
                return None
        item = HandoffItem(
            id=item_id or f"{_SECTION_TO_PREFIX[section]}-{uuid.uuid4().hex[:6]}",
            claim=claim,
            status=status,
            confidence=float(confidence or 0.5),
            producer=str(producer)[:60],
            evidence=_str_list(evidence),
            counterevidence=_str_list(counterevidence),
            scope=str(scope)[:120],
            created_at=time.time() if created_at is None else created_at,
        )
        bucket.append(item)
        return item

    @classmethod
    def build_from(cls, frontier, blackboard, knowledge_base) -> "Handoff":
        """Assemble a handoff from live agent state (objects passed in, no
        imports from master — keeps the module cycle-free)."""
        h = cls()
        intents = getattr(frontier, "_intents", None) or {}
        for intent in list(intents.values()):
            raw_status = getattr(intent, "status", None)
            # IntentStatus is a str Enum: prefer .value ("done"), fall back to str.
            intent_status = getattr(raw_status, "value", None) or str(raw_status or "")
            hypothesis = str(getattr(intent, "hypothesis", "") or "")
            evidence: list[str] = [str(e) for e in (getattr(intent, "evidence", None) or ())]
            producer = str(getattr(intent, "actor", "frontier") or "frontier")
            if intent_status == "done":
                result = str(getattr(intent, "result", "") or "").strip()
                if result:
                    h.add("hypotheses", result, status="inferred",
                          producer=producer, evidence=evidence)
            elif intent_status in ("open", "claimed"):
                h.add("hypotheses", hypothesis, status="hypothesis",
                      producer=producer, evidence=evidence)
        dead_ends = frontier.dead_ends() if callable(getattr(frontier, "dead_ends", None)) else []
        for dead in dead_ends:
            if str(getattr(dead, "category", "") or "") != "strategy":
                continue
            reason = str(getattr(dead, "reason", "") or "")
            h.add("negative_findings", str(getattr(dead, "hypothesis", "") or ""),
                  status="rejected", confidence=0.9,
                  producer=f"frontier/{getattr(dead, 'intent_id', '?')}",
                  counterevidence=[reason] if reason else [])
        for host, finding in knowledge_base.all_findings():
            verdict, conflict = verification_outcome(finding)
            status = {
                "confirmed": "verified", "rejected": "rejected",
                "pending": "hypothesis",
            }.get(verdict, "inferred")
            if conflict or getattr(finding, "status", "") == "suspected":
                status = "hypothesis"
            evidence = [
                f"{e.type}: {e.value or e.cve or e.payload or e.result or e.evidence_id}"
                for e in (getattr(finding, "evidence", None) or [])
                if e.value or e.cve or e.payload or e.result or e.evidence_id
            ]
            verification = getattr(finding, "verification", None) or {}
            effective = verification.get("adjudicated", verification)
            evidence.extend(_str_list(effective.get("independent_evidence")))
            superseded = str(getattr(finding, "superseded_by", "") or "")
            counterevidence = _str_list(effective.get("counterevidence"))
            if superseded:
                counterevidence.append(f"superseded_by: {superseded}")
            h.add("candidates", str(getattr(finding, "claim", "") or ""),
                  status=status, confidence=float(getattr(finding, "confidence", 0.5) or 0.5),
                  producer="knowledge_base", evidence=evidence,
                  counterevidence=counterevidence, scope=host)
        if blackboard is not None:
            def _from_board(entries, kind, status, scope):
                for entry in entries:
                    author = str(entry.get("author", "") or "")
                    h.add(kind, str(entry.get("text", "") or ""), status=status,
                          producer=f"blackboard:{author}" if author else "blackboard",
                          scope=scope)

            for section in ("next_steps", "objective"):
                _from_board(blackboard.entries(section), "entrypoints", "inferred", section)
            _from_board(blackboard.entries("hypotheses"), "open_questions",
                        "hypothesis", "hypotheses")
        h.coverage = {name: len(getattr(h, name)) for name in _SECTION_NAMES}
        h.coverage["total"] = sum(h.coverage.values())
        return h

    def to_dict(self) -> dict:
        data: dict[str, object] = {
            "version": self.version,
            "phase": self.phase,
            "project_revision": self.project_revision,
        }
        for name in _SECTION_NAMES:
            data[name] = [item.to_dict() for item in getattr(self, name)]
        data["coverage"] = dict(self.coverage)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Handoff":
        data = data or {}
        h = cls(
            version=str(data.get("version", "1")),
            phase=str(data.get("phase", "")),
            project_revision=str(data.get("project_revision", "")),
        )
        for name in _SECTION_NAMES:
            setattr(
                h,
                name,
                [HandoffItem.from_dict(raw) for raw in (data.get(name) or [])
                 if isinstance(raw, dict)],
            )
        cov = data.get("coverage") or {}
        h.coverage = (
            {str(k): int(v) for k, v in cov.items() if isinstance(v, (int, float))}
            if isinstance(cov, dict)
            else {}
        )
        return h

    def render_index(self, max_chars: int = _INDEX_MAX_CHARS) -> str:
        """Short index: section name + item count + first 3 ids. Bounded."""
        lines = [
            f"【Handoff v{self.version}】phase={self.phase[:40] or '-'} "
            f"rev={self.project_revision[:40] or '-'}"
        ]
        total = 0
        for name in _SECTION_NAMES:
            items: list[HandoffItem] = getattr(self, name)
            if not items:
                continue
            total += len(items)
            ids = ", ".join(item.id for item in items[:3])
            more = "…" if len(items) > 3 else ""
            lines.append(f"- {name}({len(items)}) {_LABELS[name]}: {ids}{more}")
        if not total:
            lines.append("  (空 — 无交接条目)")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[: max_chars - 1] + "…"
        return text

    def read(
        self,
        section: str | None = None,
        item_id: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> str:
        """Selective paging. read(item_id=...) returns that one item as JSON;
        read(section=...) returns a page of that section as human-readable
        text with next_offset when more remain; read() returns the index."""
        offset = max(int(offset or 0), 0)
        limit = max(int(limit or 20), 1)
        if item_id:
            for name in _SECTION_NAMES:
                for item in getattr(self, name):
                    if item.id == item_id:
                        return json.dumps(item.to_dict(), ensure_ascii=False)
            return json.dumps(
                {"ok": False, "error": f"unknown item_id: {item_id}"},
                ensure_ascii=False,
            )
        if section:
            name = _norm_section(section)
            if not name:
                return json.dumps(
                    {
                        "ok": False,
                        "error": f"unknown section: {section}",
                        "valid_sections": list(_SECTION_NAMES),
                    },
                    ensure_ascii=False,
                )
            items: list[HandoffItem] = getattr(self, name)
            page = items[offset : offset + limit]
            lines = (
                [f"◆ {_LABELS[name]}（共 {len(items)} 条，"
                 f"本页 {offset}-{offset + len(page) - 1}）"]
                if page
                else [f"◆ {_LABELS[name]}（共 0 条）"]
            )
            for item in page:
                lines.append(
                    f"- [{item.id}] {item.claim} | status={item.status} "
                    f"conf={item.confidence:.2f} producer={item.producer or '-'} "
                    f"scope={item.scope or '-'}"
                )
            end = offset + len(page)
            if page and end < len(items):
                lines.append(f"next_offset: {end}")
            return "\n".join(lines)
        return self.render_index()
