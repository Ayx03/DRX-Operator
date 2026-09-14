"""Directed point-to-point IRC channel — the complement to the broadcast forum.

Design principles (EXOBoost research report):
- Forum 是共享信道，IRC 是定向点对点信道：one agent asks ONE specific agent a
  targeted question (e.g. a verifier asking the author to confirm a detail).
  There is no broadcast, no topic, no thread — only ``from_agent -> to_agent``.
- 只有原始收件人能回复：a reply must target an OPEN message addressed to the
  replier. This prevents thread hijacking and keeps a directed exchange bounded
  to the two parties that own it.
- IRC 消息是工作状态，不是长期记忆：directed messages never enter long-term
  memory and must never carry raw chain-of-thought. ``render_for`` labels every
  message as working state, not verified fact.
- 收件人的答复即终点：replying flips the original to ``answered`` and the reply
  itself is terminal — an answered exchange does not spawn a new obligation for
  the other party.

Self-contained: imports only stdlib.
"""

# allow: SIZE_OK — task-mandated single-file data contract: IRCStatus enum +
# IRCMessage dataclass + IRC (send/reply/inbox/pending/close/render,
# serialization, prune) are one indivisible API surface that cannot be split
# without violating the deliverable spec.

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

MAX_MESSAGES = 1000
EXCERPT_CHARS = 400
CLOSE_REASON_CHARS = 512


class IRCStatus(str, Enum):
    OPEN = "open"  # awaiting a reply from the recipient
    ANSWERED = "answered"  # the recipient has replied
    CLOSED = "closed"  # manually closed by sender or recipient


@dataclass
class IRCMessage:
    """One directed message. ``reply_to`` points at the message it answers."""

    id: int
    from_agent: str
    to_agent: str
    content: str
    reply_to: int = 0
    status: str = "open"
    created_at: float = 0.0
    read: bool = False
    closed_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "content": self.content,
            "reply_to": self.reply_to,
            "status": self.status,
            "created_at": self.created_at,
            "read": self.read,
            "closed_reason": self.closed_reason,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "IRCMessage":
        raw_status = data.get("status", "open")
        status = raw_status.value if isinstance(raw_status, IRCStatus) else str(raw_status)
        return cls(
            id=int(data.get("id", 0) or 0),
            from_agent=str(data.get("from_agent", "")),
            to_agent=str(data.get("to_agent", "")),
            content=str(data.get("content", "")),
            reply_to=int(data.get("reply_to", 0) or 0),
            status=status if status in ("open", "answered", "closed") else "open",
            created_at=float(data.get("created_at", 0.0) or 0.0),
            read=bool(data.get("read", False)),
            closed_reason=str(data.get("closed_reason", "")),
        )


class IRC:
    """Directed point-to-point channel. No broadcast, no topics, no threads."""

    def __init__(self, max_messages: int = MAX_MESSAGES, excerpt_chars: int = EXCERPT_CHARS) -> None:
        self.max_messages: int = int(max_messages or MAX_MESSAGES)
        self.excerpt_chars: int = int(excerpt_chars or EXCERPT_CHARS)
        self._messages: dict[int, IRCMessage] = {}
        self._next_id: int = 1

    # ------------------------------------------------------------- internals

    @staticmethod
    def _now(now: float | None) -> float:
        return time.time() if now is None else float(now)

    def _prune(self) -> None:
        """Drop oldest CLOSED messages past ``max_messages``. OPEN (and answered)
        messages are never pruned — an unanswered obligation must not vanish."""
        if len(self._messages) <= self.max_messages:
            return
        overflow = len(self._messages) - self.max_messages
        droppable = [m for m in self._messages.values() if m.status == IRCStatus.CLOSED]
        droppable.sort(key=lambda m: m.created_at)
        for m in droppable[:overflow]:
            del self._messages[m.id]


    # ----------------------------------------------------------------- send

    def send(
        self,
        from_agent,
        to_agent,
        content,
        *,
        reply_to=0,
        now=None,
    ) -> int | None:
        """Append a directed message; return its id, or ``None`` when rejected.

        Rejected when ``content`` is empty, or ``from_agent == to_agent`` (no
        self-DMs). When ``reply_to`` is set it must reference an OPEN message
        addressed to ``from_agent`` (you may only reply to a message sent TO
        you); a valid reply flips the original to ``answered``.
        """
        now = self._now(now)
        from_agent = str(from_agent)
        to_agent = str(to_agent)
        content = (content or "").strip()
        if not content or from_agent == to_agent:
            return None
        reply_to = int(reply_to or 0)
        target: IRCMessage | None = None
        if reply_to != 0:
            target = self._messages.get(reply_to)
            if (
                target is None
                or target.to_agent != from_agent
                or target.from_agent != to_agent
                or target.status != IRCStatus.OPEN
            ):
                return None
        mid = self._next_id
        self._next_id += 1
        self._messages[mid] = IRCMessage(
            id=mid,
            from_agent=from_agent,
            to_agent=to_agent,
            content=content,
            reply_to=reply_to,
            status=IRCStatus.ANSWERED.value if reply_to != 0 else IRCStatus.OPEN.value,
            created_at=now,
        )
        if target is not None:
            target.status = IRCStatus.ANSWERED.value
        self._prune()
        return mid

    # ----------------------------------------------------------------- read

    def inbox(self, agent, *, unread_only=False, after_id=0, limit=20) -> list[dict]:
        """Earliest page after ``after_id``, in id order, with complete content."""
        limit = max(int(limit or 20), 1)
        agent = str(agent)
        after_id = int(after_id or 0)
        msgs = [
            m for m in self._messages.values()
            if m.to_agent == agent and m.id > after_id
        ]
        if unread_only:
            msgs = [m for m in msgs if not m.read]
        msgs.sort(key=lambda m: m.id)
        return [m.to_dict() for m in msgs[:limit]]

    def pending_for(self, agent, *, limit=20) -> list[dict]:
        """OPEN messages addressed to ``agent`` that it still owes an answer to."""
        limit = max(int(limit or 20), 1)
        agent = str(agent)
        msgs = [
            m for m in self._messages.values()
            if m.to_agent == agent and m.status == IRCStatus.OPEN
        ]
        msgs.sort(key=lambda m: m.id)
        return [m.to_dict() for m in msgs[:limit]]

    def reply(self, agent, message_id, content, *, now=None) -> int | None:
        """Answer a message sent TO ``agent``. The "only the original recipient
        may reply" rule is enforced by ``send`` (which rejects a ``reply_to``
        target not addressed to the replier)."""
        target = self._messages.get(int(message_id or 0))
        if target is None:
            return None
        return self.send(agent, target.from_agent, content, reply_to=target.id, now=now)

    # ------------------------------------------------------------- read state

    def mark_read(self, agent, *, up_to_id=0, message_ids=None) -> int:
        """Mark only delivered ``message_ids`` when supplied; otherwise mark
        through ``up_to_id`` (0 means all). Return the number newly marked."""
        agent = str(agent)
        up_to_id = int(up_to_id or 0)
        delivered = None if message_ids is None else {int(mid) for mid in message_ids}
        count = 0
        for m in self._messages.values():
            if m.to_agent == agent and not m.read:
                if (m.id in delivered if delivered is not None else up_to_id == 0 or m.id <= up_to_id):
                    m.read = True
                    count += 1
        return count

    def unread_count(self, agent) -> int:
        agent = str(agent)
        return sum(1 for m in self._messages.values() if m.to_agent == agent and not m.read)

    # ----------------------------------------------------------------- close

    def close(self, message_id, *, agent="", reason="") -> bool:
        """Close a message; only its sender or recipient may close it."""
        m = self._messages.get(int(message_id or 0))
        if m is None:
            return False
        if str(agent) not in (m.from_agent, m.to_agent):
            return False
        m.status = IRCStatus.CLOSED.value
        m.closed_reason = (reason or "")[:CLOSE_REASON_CHARS]
        return True

    def admin_close(self, message_id, *, actor, reason) -> bool:
        """Retire an orphaned obligation without impersonating either participant.

        The runtime checks that neither participant is still active. The
        original message remains available with an explicit supervisory audit.
        """
        reason = str(reason or "").strip()
        if actor != "master" or not reason:
            return False
        message = self._messages.get(int(message_id or 0))
        if message is None or message.status != IRCStatus.OPEN:
            return False
        message.status = IRCStatus.CLOSED.value
        message.closed_reason = f"Administrative close by master: {reason}"[:CLOSE_REASON_CHARS]
        return True

    # ----------------------------------------------------------------- render

    def render_for(self, agent, *, max_chars=1000) -> str:
        """Bounded Chinese view: pending (unanswered) messages addressed to
        ``agent`` first, then recent replies. Explicitly labels directed
        messages as working state, not verified fact."""
        agent = str(agent)
        max_chars = max(int(max_chars or 1000), 1)
        pending = [
            m for m in self._messages.values()
            if m.to_agent == agent and m.status == IRCStatus.OPEN
        ]
        pending.sort(key=lambda m: m.id)
        replies = [
            m for m in self._messages.values()
            if m.to_agent == agent and m.status == IRCStatus.ANSWERED
        ]
        replies.sort(key=lambda m: m.id, reverse=True)
        lines = ["【IRC 定向消息 — 工作状态，非已验证事实；未验证内容不可当作结论】"]
        if pending:
            lines.append("【待回复（你仍需作答）】")
            for m in pending:
                lines.append(f"- #{m.id} <{m.from_agent}> {m.content[: self.excerpt_chars]}")
        else:
            lines.append("  (暂无待回复的定向消息)")
        if replies:
            lines.append("【近期往来（已回复）】")
            for m in replies:
                lines.append(f"- #{m.id} <{m.from_agent}> {m.content[: self.excerpt_chars]}")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[: max_chars - 1] + "…"
        return text

    # ---------------------------------------------------------- serialization

    def to_dict(self) -> dict:
        return {
            "next_id": self._next_id,
            "max_messages": self.max_messages,
            "excerpt_chars": self.excerpt_chars,
            "messages": [
                m.to_dict()
                for m in sorted(self._messages.values(), key=lambda m: m.id)
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "IRC":
        data = data or {}
        irc = cls(
            max_messages=int(data.get("max_messages", MAX_MESSAGES) or MAX_MESSAGES),
            excerpt_chars=int(data.get("excerpt_chars", EXCERPT_CHARS) or EXCERPT_CHARS),
        )
        try:
            irc._next_id = int(data.get("next_id", 1) or 1)
        except (TypeError, ValueError):
            irc._next_id = 1
        for raw in data.get("messages") or []:
            if not isinstance(raw, dict) or raw.get("id") is None:
                continue
            m = IRCMessage.from_dict(raw)
            irc._messages[m.id] = m
            irc._next_id = max(irc._next_id, m.id + 1)
        return irc

    # ---------------------------------------------------------- bounded size

    def __len__(self) -> int:
        return len(self._messages)

    def count(self) -> int:
        return len(self._messages)
