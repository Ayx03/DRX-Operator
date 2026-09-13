"""Immutable, reference-based evidence store.

Findings, the blackboard, handoff, and reflection all REFERENCE evidence
by its stable ``E-xxxx`` id instead of copying raw content, so a fact is
stored exactly once and never drifts through re-summarization. Records are
immutable; identical content dedupes to the same id. Full content is
offloaded to the ArtifactStore (when provided) with only a preview kept
in the index.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from drx_agent.agent.artifact_store import ArtifactStore

VALID_ORIGINS = ("source", "runtime", "tool", "network", "file")


@dataclass
class EvidenceRecord:
    evidence_id: str
    origin: str
    kind: str
    revision: str
    location: str
    digest: str
    content_ref: str
    preview: str
    producer: str
    created_at: float
    immutable: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "EvidenceRecord":
        return cls(
            evidence_id=data.get("evidence_id", ""),
            origin=data.get("origin", ""),
            kind=data.get("kind", ""),
            revision=data.get("revision", ""),
            location=data.get("location", ""),
            digest=data.get("digest", ""),
            content_ref=data.get("content_ref", ""),
            preview=data.get("preview", ""),
            producer=data.get("producer", ""),
            created_at=data.get("created_at", 0.0),
            immutable=bool(data.get("immutable", True)),
        )


class EvidenceStore:
    def __init__(self, base_dir: str, artifact_store=None) -> None:
        self.dir = Path(base_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.artifact_store = artifact_store
        self._records: dict[str, EvidenceRecord] = {}
        self._digest_map: dict[str, str] = {}
        self._index_path = self.dir / "evidence_index.json"
        self._load_index()

    def _load_index(self) -> None:
        raw: dict = {}
        try:
            if self._index_path.is_file():
                raw = json.loads(self._index_path.read_text("utf-8"))
        except Exception:
            raw = {}
        for entry in (raw or {}).get("records") or []:
            if not isinstance(entry, dict) or not entry.get("evidence_id"):
                continue
            rec = EvidenceRecord.from_dict(entry)
            self._records[rec.evidence_id] = rec
            if rec.digest:
                self._digest_map[rec.digest] = rec.evidence_id

    def _save_index(self) -> None:
        try:
            self._index_path.write_text(
                json.dumps(
                    {
                        "records": [
                            r.to_dict()
                            for r in sorted(
                                self._records.values(),
                                key=lambda r: r.created_at,
                            )
                        ]
                    },
                    ensure_ascii=False,
                ),
                "utf-8",
            )
        except Exception:
            pass

    def put(
        self,
        content: str,
        *,
        origin: str = "",
        kind: str = "",
        location: str = "",
        revision: str = "",
        producer: str = "",
        content_ref: str = "",
    ) -> str:
        """Store *content* immutably; return the evidence id (deduped by digest)."""
        content = str(content)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        existing = self._digest_map.get(digest)
        if existing:
            return existing
        evidence_id = f"E-{uuid.uuid4().hex[:6]}"
        ref = content_ref
        if not ref and self.artifact_store is not None:
            ref = self.artifact_store.store(
                content, tool=producer or "evidence", kind=kind or "evidence"
            ) or ""
        preview = content[:300].replace("\n", " ")
        record = EvidenceRecord(
            evidence_id=evidence_id,
            origin=origin or "tool",
            kind=kind or "evidence",
            revision=revision,
            location=location,
            digest=digest,
            content_ref=ref,
            preview=preview,
            producer=producer,
            created_at=time.time(),
        )
        self._records[evidence_id] = record
        self._digest_map[digest] = evidence_id
        self._save_index()
        return evidence_id

    def get(self, evidence_id) -> EvidenceRecord | None:
        return self._records.get(evidence_id)

    def content(self, evidence_id) -> str | None:
        """Full content via the artifact store; None when not offloaded."""
        record = self._records.get(evidence_id)
        if not record or not record.content_ref or self.artifact_store is None:
            return None
        return self.artifact_store.read(record.content_ref)

    def list(self, limit: int = 0) -> list[EvidenceRecord]:
        records = sorted(self._records.values(), key=lambda r: r.created_at)
        if limit and limit > 0:
            records = records[-limit:]
        return records

    def to_dict(self) -> dict:
        return {"records": [r.to_dict() for r in self._records.values()]}

    @classmethod
    def from_dict(cls, data: dict, base_dir: str, artifact_store=None) -> "EvidenceStore":
        store = cls(base_dir=base_dir, artifact_store=artifact_store)
        for entry in (data or {}).get("records") or []:
            if not isinstance(entry, dict) or not entry.get("evidence_id"):
                continue
            rec = EvidenceRecord.from_dict(entry)
            store._records[rec.evidence_id] = rec
            if rec.digest:
                store._digest_map[rec.digest] = rec.evidence_id
        return store

    def render_index(self, max_chars: int = 800) -> str:
        """Compact 'what evidence exists' listing for the LLM."""
        records = sorted(self._records.values(), key=lambda r: r.created_at)
        if not records:
            return "(no evidence yet)"
        lines = [f"[{len(records)} evidence records]"]
        for rec in records:
            meta = f"{rec.evidence_id} {rec.kind}/{rec.origin}"
            if rec.revision:
                meta += f" rev={rec.revision}"
            if rec.location:
                meta += f" @ {rec.location}"
            lines.append(f"{meta} | {rec.preview}")
        out = "\n".join(lines)
        if len(out) > max_chars:
            out = out[: max_chars - 1] + "…"
        return out
