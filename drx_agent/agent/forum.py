"""Typed, threaded collaboration forum — replaces free-text blackboard chatter.

Design principles (EXOBoost research report):
- Forum 是共享信道，而不是共享上下文：agents share claims/evidence/questions,
  never raw chain-of-thought. The forum is working state only — Peer Messages
  never enter long-term memory.
- Forum 消息必须类型化：every message carries a ``msg_type`` AND an
  ``epistemic_status``. A ``candidate`` can never be read as a fact — render
  paths annotate non-verified items explicitly (``[候选·未验证]``).
- Forum 应避免广播风暴：no @all broadcast except critical evidence /
  project-wide correction / stage transition. Everything else is topic-scoped
  or directed. Enforced in ``wait``/``notifications``/``render_for``: only
  ``announcement=True`` (or ``correction``/``evidence`` with ``to=""``) is
  treated as addressing every agent.
- Forum 需要 Digest，但 Digest 不是事实：the digest is a topic index (counts)
  so a worker reads only what it needs; it is explicitly labeled an index,
  not truth.
- Pin limit is small (max 3 pinned threads). Closed threads keep history but
  reject replies.

Self-contained: imports only stdlib (no master/sub_agent coupling). ``stage``
is carried as a plain string (the value of ``Stage`` from ``stage.py``) so this
module stays cycle-free.
"""

# allow: SIZE_OK — task-mandated single-file data contract: enums + Message
# dataclass + Forum (posting/threading, pin/close, notifications, digest,
# render, serialization) are one indivisible API surface that cannot be split
# without violating the deliverable spec.

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum

MAX_PINNED = 3
MAX_MESSAGES = 2000
MAX_CONTENT = 4000
EXCERPT_CHARS = 256
CLOSE_REASON_CHARS = 512

# msg_type values that may address everyone without announcement=True.
_BROADCAST_TYPES = ("correction", "evidence")


class MessageType(str, Enum):
    CLAIM = "claim"
    QUESTION = "question"
    EVIDENCE = "evidence"
    CANDIDATE = "candidate"
    CORRECTION = "correction"
    WORK_OFFER = "work_offer"
    WORK_CLAIM = "work_claim"
    HELP = "help"
    STATUS = "status"


class EpistemicStatus(str, Enum):
    RAW = "raw"
    HYPOTHESIS = "hypothesis"
    OBSERVED = "observed"
    VERIFIED = "verified"
    REJECTED = "rejected"


# Render annotation per epistemic status: candidate/hypothesis/raw MUST carry
# an explicit "未验证" (unverified) marker so they are never read as facts.
_EPISTEMIC_TAG = {
    "candidate": "[候选·未验证]",
    "hypothesis": "[假设·未验证]",
    "raw": "[原始·未验证]",
    "observed": "[观察]",
    "verified": "[已验证]",
    "rejected": "[已排除]",
}


def _coerce_enum(enum_cls: type, value: object, default: str) -> str:
    try:
        return enum_cls(str(value).lower()).value
    except (ValueError, KeyError):
        return default


@dataclass
class Message:
    """One typed forum message. Thread roots have ``thread_id == id``."""

    id: int
    thread_id: int
    agent_id: str
    stage: str = ""
    to: str = ""  # recipient; "" = topic/public (NOT a broadcast)
    reply_to: int = 0
    topic: str = ""
    content: str = ""
    msg_type: str = "claim"
    epistemic_status: str = "hypothesis"
    scope: str = ""
    references: tuple[str, ...] = ()  # e.g. E-xxxx / intent ids
    ttl: float = 0.0  # 0 = no deadline; elapsed deadlines never delete obligations
    created_at: float = field(default_factory=time.time)
    pinned: bool = False
    closed: bool = False
    announcement: bool = False
    moderation_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "agent_id": self.agent_id,
            "stage": self.stage,
            "to": self.to,
            "reply_to": self.reply_to,
            "topic": self.topic,
            "content": self.content,
            "msg_type": self.msg_type,
            "epistemic_status": self.epistemic_status,
            "scope": self.scope,
            "references": list(self.references),
            "ttl": self.ttl,
            "created_at": self.created_at,
            "pinned": self.pinned,
            "closed": self.closed,
            "announcement": self.announcement,
            "moderation_reason": self.moderation_reason,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        refs = data.get("references") or ()
        if isinstance(refs, str):
            refs = (refs,)
        return cls(
            id=int(data.get("id", 0) or 0),
            thread_id=int(data.get("thread_id", 0) or 0),
            agent_id=str(data.get("agent_id", "")),
            stage=str(data.get("stage", "")),
            to=str(data.get("to", "")),
            reply_to=int(data.get("reply_to", 0) or 0),
            topic=str(data.get("topic", "")),
            content=str(data.get("content", "")),
            msg_type=str(data.get("msg_type", "claim")),
            epistemic_status=str(data.get("epistemic_status", "hypothesis")),
            scope=str(data.get("scope", "")),
            references=tuple(str(r) for r in refs),
            ttl=float(data.get("ttl", 0.0) or 0.0),
            created_at=float(data.get("created_at", 0.0) or 0.0),
            pinned=bool(data.get("pinned", False)),
            closed=bool(data.get("closed", False)),
            announcement=bool(data.get("announcement", False)),
            moderation_reason=str(data.get("moderation_reason", "")),
        )


class Forum:
    """Typed, threaded, pin-able collaboration channel for cross-agent state.

    Anti-broadcast rule: a message "addresses everyone" only when
    ``announcement=True`` OR its ``msg_type`` is ``correction``/``evidence``
    with an explicit ``to=""``. A plain public post (``to=""``, ``msg_type``
    default) is topic-scoped, NOT a broadcast — it does not land in every
    agent's ``wait``/``notifications``. This keeps the shared channel from
    degenerating into an @all storm.
    """

    def __init__(self, max_pinned: int = MAX_PINNED, max_messages: int = MAX_MESSAGES) -> None:
        self.max_pinned: int = int(max_pinned or MAX_PINNED)
        self.max_messages: int = int(max_messages or MAX_MESSAGES)
        self._messages: dict[int, Message] = {}
        self._next_id: int = 1
        self._subscriptions: dict[str, set[str]] = {}

    # ------------------------------------------------------------- internals

    def _is_broadcast(self, m: Message) -> bool:
        if m.announcement:
            return True
        return m.msg_type in _BROADCAST_TYPES and m.to == ""

    def _addresses(self, m: Message, agent_id: str) -> bool:
        return (
            m.to == agent_id or self._is_broadcast(m)
            or (not m.to and m.topic in self._subscriptions.get(agent_id, ()))
        )

    def _root_of(self, message_id: int) -> Message | None:
        m = self._messages.get(message_id)
        if m is None:
            return None
        return self._messages.get(m.thread_id)

    def _set_pin(self, thread_id: int, value: bool) -> None:
        for m in self._messages.values():
            if m.thread_id == thread_id:
                m.pinned = value

    def _set_closed(self, thread_id: int, value: bool, reason: str = "") -> None:
        for m in self._messages.values():
            if m.thread_id == thread_id:
                m.closed = value
                if value:
                    m.moderation_reason = reason
                else:
                    m.moderation_reason = ""

    def _make_room(self, protected_thread: int = 0) -> bool:
        """Reserve one slot by evicting whole safe threads, or change nothing."""
        needed = len(self._messages) + 1 - self.max_messages
        if needed <= 0:
            return True
        roots = [
            m for m in self._messages.values()
            if m.reply_to == 0 and not m.pinned and m.id != protected_thread
            and (m.closed or m.msg_type not in ("question", "help"))
        ]
        roots.sort(key=lambda m: (m.created_at, m.id))
        members: dict[int, list[int]] = {m.id: [] for m in roots}
        for m in self._messages.values():
            if m.thread_id in members:
                members[m.thread_id].append(m.id)
        evict: list[int] = []
        for root in roots:
            evict.extend(members[root.id])
            if len(evict) >= needed:
                break
        if len(evict) < needed:
            return False
        for mid in evict:
            del self._messages[mid]
        return True

    # ----------------------------------------------------------------- post

    def post(
        self,
        agent_id: str,
        content: str,
        *,
        msg_type: str = "claim",
        epistemic_status: str = "hypothesis",
        topic: str = "",
        to: str = "",
        reply_to: int = 0,
        scope: str = "",
        references: tuple[str, ...] = (),
        ttl: float = 0.0,
        stage: str = "",
        announcement: bool = False,
    ) -> int | None:
        """Append a typed message; return its id, or ``None`` when rejected.

        Rejected when ``content`` is empty, the target thread is closed, or
        capacity cannot be freed without losing a pinned/unresolved thread.
        ``announcement=True`` or ``correction``/``evidence`` with ``to=""``
        addresses everyone; a plain public post is topic-scoped.
        """
        content = (content or "").strip()
        if not content:
            return None
        msg_type = _coerce_enum(MessageType, msg_type, "claim")
        epistemic_status = _coerce_enum(EpistemicStatus, epistemic_status, "hypothesis")
        if isinstance(references, str):
            references = (references,)
        references = tuple(str(r) for r in (references or ()))
        reply_to = int(reply_to or 0)
        topic = str(topic or "")
        to = str(to or "")

        ttl = float(ttl)
        if not math.isfinite(ttl) or ttl < 0:
            raise ValueError("ttl must be finite and nonnegative")
        created_at = time.time()
        if not math.isfinite(created_at + ttl):
            raise ValueError("ttl must produce a finite deadline")
        thread_id = 0
        root = None
        if reply_to != 0:
            root = self._root_of(reply_to)
            if root is None or root.closed:
                return None  # unknown target or closed thread
            thread_id = root.id
            if not topic:
                topic = root.topic

        if not self._make_room(thread_id):
            return None
        mid = self._next_id
        self._next_id += 1
        if reply_to == 0:
            thread_id = mid  # root: thread_id == own id

        self._messages[mid] = Message(
            id=mid,
            thread_id=thread_id,
            agent_id=str(agent_id),
            stage=str(stage or ""),
            to=to,
            reply_to=reply_to,
            topic=topic,
            content=content[:MAX_CONTENT],
            msg_type=msg_type,
            epistemic_status=epistemic_status,
            scope=str(scope or ""),
            references=references,
            ttl=ttl,
            created_at=created_at,
            pinned=bool(root and root.pinned),
            announcement=bool(announcement),
        )
        return mid

    # ----------------------------------------------------------------- read

    def read(self, thread_id: int = 0, *, limit: int = 20, offset: int = 0) -> list[dict]:
        """Paged read. ``thread_id=0`` returns the most recent roots; otherwise
        the messages of that thread (root + replies) in chronological order."""
        limit = max(int(limit or 20), 1)
        offset = max(int(offset or 0), 0)
        thread_id = int(thread_id or 0)
        if thread_id == 0:
            msgs = [m for m in self._messages.values() if m.reply_to == 0]
            msgs.sort(key=lambda m: m.id, reverse=True)
        else:
            msgs = [m for m in self._messages.values() if m.thread_id == thread_id]
            msgs.sort(key=lambda m: m.id)
        return [m.to_dict() for m in msgs[offset : offset + limit]]

    def threads(self, *, stage: str = "", limit: int = 50) -> list[dict]:
        """Root messages with ``reply_count``/``last_at``, pinned-first then
        most-recent. Optionally filtered by ``stage``."""
        roots = [m for m in self._messages.values() if m.reply_to == 0]
        if stage:
            roots = [m for m in roots if m.stage == stage]
        out: list[dict] = []
        for root in roots:
            replies = [
                m for m in self._messages.values()
                if m.thread_id == root.id and m.id != root.id
            ]
            last_at = max([root.created_at] + [r.created_at for r in replies])
            d = root.to_dict()
            d["reply_count"] = len(replies)
            d["last_at"] = last_at
            out.append(d)
        out.sort(key=lambda d: (not d["pinned"], -d["last_at"]))
        return out[: int(limit)]

    def wait(self, agent_id: str, *, after_id: int = 0, limit: int = 20) -> list[dict]:
        """Earliest messages after the cursor: directed, broadcast, or public
        posts in a subscribed topic. Polling never blocks or consumes messages."""
        after_id = int(after_id or 0)
        limit = max(int(limit or 20), 1)
        msgs = [
            m for m in self._messages.values()
            if m.id > after_id and self._addresses(m, agent_id)
        ]
        msgs.sort(key=lambda m: m.id)
        return [m.to_dict() for m in msgs[:limit]]

    def subscribe(self, agent_id: str, topic: str, subscribed: bool = True) -> bool:
        """Enable or disable exact-topic delivery; return False for empty ids."""
        agent_id = str(agent_id or "").strip()
        topic = str(topic or "").strip()
        if not agent_id or not topic:
            return False
        if subscribed:
            self._subscriptions.setdefault(agent_id, set()).add(topic)
        else:
            topics = self._subscriptions.get(agent_id)
            if topics is not None:
                topics.discard(topic)
                if not topics:
                    del self._subscriptions[agent_id]
        return True

    def subscriptions(self, agent_id: str) -> list[str]:
        return sorted(self._subscriptions.get(str(agent_id), ()))

    def pending(self, agent_id: str | None = None, *, now: float | None = None) -> list[dict]:
        """Unclosed question/help roots, with their owner and derived deadline state."""
        now = time.time() if now is None else float(now)
        if not math.isfinite(now):
            raise ValueError("now must be finite")
        out = []
        for m in sorted(self._messages.values(), key=lambda m: m.id):
            if m.reply_to or m.closed or m.msg_type not in ("question", "help"):
                continue
            owner = m.to or "master"
            if agent_id is not None and owner != agent_id:
                continue
            item = m.to_dict()
            item["owner"] = owner
            item["overdue"] = m.ttl > 0 and now >= m.created_at + m.ttl
            out.append(item)
        return out

    def roster(self) -> list[str]:
        """Distinct agent ids that have posted, most-recent first."""
        seen: list[str] = []
        for m in sorted(self._messages.values(), key=lambda m: m.id, reverse=True):
            if m.agent_id and m.agent_id not in seen:
                seen.append(m.agent_id)
        return seen

    # ------------------------------------------------------- pin / close

    def pin(self, message_id: int, pinned: bool = True) -> bool:
        """Pin (or unpin) the thread of ``message_id``. At most ``max_pinned``
        threads stay pinned (oldest is unpinned when full). Only roots hold the
        pin; replies mirror their root's pin state."""
        root = self._root_of(message_id)
        if root is None:
            return False
        if pinned:
            if root.pinned:
                return True
            pinned_roots = [
                m for m in self._messages.values() if m.reply_to == 0 and m.pinned
            ]
            if len(pinned_roots) >= self.max_pinned:
                oldest = min(pinned_roots, key=lambda m: m.created_at)
                self._set_pin(oldest.id, False)
            self._set_pin(root.id, True)
        else:
            self._set_pin(root.id, False)
        return True

    def close(self, message_id: int, reason: str = "") -> bool:
        """Close a thread: rejects replies but keeps history. ``reason`` is
        bounded to <=512 chars."""
        root = self._root_of(message_id)
        if root is None:
            return False
        self._set_closed(root.id, True, (reason or "")[:CLOSE_REASON_CHARS])
        return True

    def reopen(self, message_id: int) -> bool:
        root = self._root_of(message_id)
        if root is None:
            return False
        self._set_closed(root.id, False)
        return True

    # ---------------------------------------------------- notifications

    def notifications(self, agent_id: str, *, limit: int = 10) -> dict:
        """Bounded literal excerpts (<=256 chars each) for messages addressed
        to ``agent_id`` (or broadcasts), plus a ``cursor`` (last message id).
        Excerpts are verbatim content slices — never a generated summary."""
        limit = max(int(limit or 10), 1)
        msgs = [
            m for m in self._messages.values()
            if self._addresses(m, agent_id)
        ]
        msgs.sort(key=lambda m: m.id)
        items = [
            {
                "id": m.id,
                "from": m.agent_id,
                "topic": m.topic,
                "msg_type": m.msg_type,
                "epistemic_status": m.epistemic_status,
                "excerpt": m.content[:EXCERPT_CHARS],
            }
            for m in msgs[:limit]
        ]
        cursor = items[-1]["id"] if items else 0
        return {"agent": agent_id, "cursor": cursor, "items": items}

    def digest(self, *, limit: int = 20) -> str:
        """Topic index (counts) — explicitly labeled an index, not fact."""
        counts: dict[str, int] = {}
        for m in self._messages.values():
            key = (m.topic or "").strip()
            counts[key] = counts.get(key, 0) + 1
        items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[: int(limit)]
        header = "【论坛主题索引（Digest）— 仅计数索引，非事实；读取原文请用 forum_read】"
        if not items:
            return f"{header}\n(空)"
        parts = [f"{key.upper() if key else 'GENERAL'}: {n}" for key, n in items]
        return f"{header}\n{' / '.join(parts)}"

    def render_for(self, agent_id: str, *, max_chars: int = 2000) -> str:
        """What a worker sees: pinned/announcement items + items addressed to
        ``agent_id``, with an explicit unverified annotation for
        candidate/hypothesis/raw items."""
        visible = [
            m for m in self._messages.values()
            if m.pinned or self._addresses(m, agent_id)
        ]
        visible.sort(key=lambda m: (not (m.pinned or m.announcement), m.id))
        lines = ["【论坛 — 你可见的消息（未标注 verified 的项均视为未验证）】"]
        if not visible:
            lines.append("  (空 — 暂无与你相关或置顶/公告的消息)")
        for m in visible:
            tag = _EPISTEMIC_TAG.get(m.epistemic_status, f"[{m.epistemic_status}]")
            flags = []
            if m.pinned:
                flags.append("置顶")
            if m.announcement:
                flags.append("公告")
            flag = f"({'/'.join(flags)})" if flags else ""
            lines.append(f"- {tag}{flag} #{m.id} <{m.agent_id}> {m.content}")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n...(截断，用 forum_read 看全文)"
        return text

    # ------------------------------------------------------- serialization

    def to_dict(self) -> dict:
        return {
            "next_id": self._next_id,
            "max_pinned": self.max_pinned,
            "max_messages": self.max_messages,
            "subscriptions": {
                agent: sorted(topics) for agent, topics in sorted(self._subscriptions.items())
            },
            "messages": [
                m.to_dict()
                for m in sorted(self._messages.values(), key=lambda m: m.id)
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Forum":
        data = data or {}
        f = cls(
            max_pinned=int(data.get("max_pinned", MAX_PINNED) or MAX_PINNED),
            max_messages=int(data.get("max_messages", MAX_MESSAGES) or MAX_MESSAGES),
        )
        try:
            f._next_id = int(data.get("next_id", 1) or 1)
        except (TypeError, ValueError):
            f._next_id = 1
        for raw in data.get("messages") or []:
            if not isinstance(raw, dict) or raw.get("id") is None:
                continue
            m = Message.from_dict(raw)
            f._messages[m.id] = m
            f._next_id = max(f._next_id, m.id + 1)
        for agent, topics in (data.get("subscriptions") or {}).items():
            if isinstance(topics, list):
                for topic in topics:
                    f.subscribe(agent, topic)
        return f

    # ---------------------------------------------------------- bounded size

    def __len__(self) -> int:
        return len(self._messages)

    def count(self) -> int:
        return len(self._messages)
