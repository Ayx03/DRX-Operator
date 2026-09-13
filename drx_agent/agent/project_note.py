"""Structured Project Note — durable, typed project knowledge for the agent team.

Design principle: project knowledge is DURABLE (architecture, entrypoints,
trust boundaries, state stores, external interfaces) — it is NOT ephemeral task
state (open intents, per-run observations live on the frontier/forum/blackboard).
The note holds typed sections, not natural-language prose, and renders
deterministically into the prompt.

Each section is a list of :class:`NoteEntry` records carrying who contributed
the fact (``author``), when (``ts``), and an optional evidence/intent ref
(``source``). Sections normalize tolerantly: exact key, case-insensitive key, or
a Chinese label. The render is deterministic — identical state produces a
byte-identical prefix with no timestamps, so it is cache-friendly.

Self-contained: no imports from drx_agent.agent.master (callers pass objects in).
"""

# allow: SIZE_OK — task-mandated single-file data contract: 8 section lists +
# one dataclass + normalization/serialization/bounded-render are one indivisible
# API surface that cannot be split without violating the deliverable spec.

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

NOTE_SECTIONS: tuple[str, ...] = (
    "architecture",
    "auth",
    "trust_boundaries",
    "entrypoints",
    "state_stores",
    "external_interfaces",
    "privileged_operations",
    "open_questions",
)

NOTE_SECTION_LABELS: dict[str, str] = {
    "architecture": "架构",
    "auth": "认证与授权",
    "trust_boundaries": "信任边界",
    "entrypoints": "入口点",
    "state_stores": "状态存储",
    "external_interfaces": "外部接口",
    "privileged_operations": "特权操作",
    "open_questions": "待解问题",
}

_SECTION_SET = set(NOTE_SECTIONS)
_LABEL_TO_SECTION: dict[str, str] = {
    label: key for key, label in NOTE_SECTION_LABELS.items()
}

_MAX_TEXT_CHARS = 500
_MAX_AUTHOR_CHARS = 60
_MAX_SOURCE_CHARS = 120

_TITLE = "【项目笔记 — 持久化项目知识】"
_EMPTY_LINE = "  (空 — 尚无项目知识记录)"


def _to_int(value: object, default: int) -> int:
    if isinstance(value, (int, float, str)):
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    return default


def _norm_text(text: str) -> str:
    """Collapse internal whitespace so dedup ignores formatting differences."""
    return " ".join((text or "").split())


def _norm_section(section: str) -> str:
    """Resolve a section name from an exact key, a case-insensitive key, or a
    Chinese label. Returns "" for unknown names."""
    key = (section or "").strip()
    low = key.lower()
    if low in _SECTION_SET:
        return low
    return _LABEL_TO_SECTION.get(key, "")


@dataclass
class NoteEntry:
    """One durable project-knowledge record with contributor attribution."""

    text: str
    author: str = ""
    ts: float = field(default_factory=time.time)
    source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "NoteEntry":
        data = data or {}
        return cls(
            text=str(data.get("text", ""))[:_MAX_TEXT_CHARS],
            author=str(data.get("author", ""))[:_MAX_AUTHOR_CHARS],
            ts=float(data.get("ts", 0.0) or 0.0),
            source=str(data.get("source", ""))[:_MAX_SOURCE_CHARS],
        )


class ProjectNote:
    """Typed, serializable durable project-knowledge store."""

    def __init__(self, max_per_section: int = 40) -> None:
        self.max_per_section: int = max(int(max_per_section or 40), 1)
        self._entries: dict[str, list[NoteEntry]] = {k: [] for k in NOTE_SECTIONS}

    # ------------------------------------------------------------- mutation

    def update(self, section: str, text: str, *, author: str = "", source: str = "", now: float | None = None) -> bool:
        """Append a record to `section`. Normalizes the section name, rejects
        empty text or unknown sections, dedups by normalized text, and drops the
        oldest entry when the section exceeds `max_per_section`."""
        key = _norm_section(section)
        text = _norm_text(text)[:_MAX_TEXT_CHARS]
        if not key or not text:
            return False
        bucket = self._entries[key]
        if any(_norm_text(e.text) == text for e in bucket):
            return False
        bucket.append(
            NoteEntry(
                text=text,
                author=str(author)[:_MAX_AUTHOR_CHARS],
                ts=time.time() if now is None else float(now),
                source=str(source)[:_MAX_SOURCE_CHARS],
            )
        )
        if len(bucket) > self.max_per_section:
            del bucket[: len(bucket) - self.max_per_section]
        return True

    def remove(self, section: str, text: str) -> bool:
        """Remove every record in `section` whose normalized text matches.
        Returns True when at least one record was removed."""
        key = _norm_section(section)
        text = _norm_text(text)
        if not key or not text:
            return False
        bucket = self._entries[key]
        kept = [e for e in bucket if _norm_text(e.text) != text]
        self._entries[key] = kept
        return len(kept) < len(bucket)

    def clear(self, section: str = "") -> int:
        """Clear one section (by name) or all sections when `section` is empty.
        Returns the number of records removed."""
        if not section:
            removed = sum(len(bucket) for bucket in self._entries.values())
            self._entries = {k: [] for k in NOTE_SECTIONS}
            return removed
        key = _norm_section(section)
        if not key:
            return 0
        removed = len(self._entries[key])
        self._entries[key] = []
        return removed

    # --------------------------------------------------------------- access

    def get(self, section: str) -> list[dict]:
        """Return one section's records as plain dicts (empty list if unknown)."""
        key = _norm_section(section)
        if not key:
            return []
        return [e.to_dict() for e in self._entries.get(key, [])]

    def sections(self) -> dict[str, list[dict]]:
        """All non-empty sections as a name -> list-of-dicts mapping."""
        return {k: [e.to_dict() for e in v] for k, v in self._entries.items() if v}

    def count(self) -> int:
        return sum(len(bucket) for bucket in self._entries.values())

    def __len__(self) -> int:
        return self.count()

    # ------------------------------------------------------------- rendering

    def render(self, *, max_chars: int = 2000) -> str:
        """Deterministic Chinese block: one labeled group per non-empty section,
        entries with author attribution. No timestamps — byte-identical for
        identical state. Bounded to `max_chars`."""
        lines = [_TITLE]
        empty = True
        for key in NOTE_SECTIONS:
            bucket = self._entries.get(key, [])
            if not bucket:
                continue
            empty = False
            lines.append(f"◆ {NOTE_SECTION_LABELS[key]}:")
            lines.extend(self._entry_lines(bucket))
        if empty:
            lines.append(_EMPTY_LINE)
        return self._bounded("\n".join(lines), max_chars)

    def render_section(self, section: str, *, max_chars: int = 800) -> str:
        """Deterministic rendering of a single section. Empty string when the
        section is unknown."""
        key = _norm_section(section)
        if not key:
            return ""
        bucket = self._entries.get(key, [])
        lines = [f"◆ {NOTE_SECTION_LABELS[key]}（共 {len(bucket)} 条）"]
        lines.extend(self._entry_lines(bucket))
        return self._bounded("\n".join(lines), max_chars)

    @staticmethod
    def _entry_lines(bucket: list[NoteEntry]) -> list[str]:
        out: list[str] = []
        for e in bucket:
            who = f" ({e.author})" if e.author else ""
            src = f" [src={e.source}]" if e.source else ""
            out.append(f"  - {e.text}{who}{src}")
        return out

    @staticmethod
    def _bounded(text: str, max_chars: int) -> str:
        if len(text) > max_chars:
            return text[: max_chars - 1] + "…"
        return text

    # --------------------------------------------------------- serialization

    def to_dict(self) -> dict:
        return {
            "max_per_section": self.max_per_section,
            "entries": {k: [e.to_dict() for e in v] for k, v in self._entries.items() if v},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectNote":
        """Rebuild from a dict. Tolerant of unknown sections (skipped), missing
        fields (defaults applied), and malformed entries (skipped)."""
        data = data or {}
        note = cls(max_per_section=_to_int(data.get("max_per_section"), 40))
        entries = data.get("entries") or {}
        if isinstance(entries, dict):
            for raw_key, bucket in entries.items():
                norm = _norm_section(str(raw_key))
                if not norm or not isinstance(bucket, list):
                    continue
                loaded: list[NoteEntry] = []
                for raw in bucket:
                    if not isinstance(raw, dict):
                        continue
                    entry = NoteEntry.from_dict(raw)
                    if entry.text:
                        loaded.append(entry)
                note._entries[norm] = loaded
        return note
