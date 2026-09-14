import asyncio
from copy import deepcopy
import math
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List


class EventType(Enum):
    AGENT_MESSAGE = "agent_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    SUB_AGENT_DISPATCH = "sub_agent_dispatch"
    SUB_AGENT_RESULT = "sub_agent_result"
    APPROVAL_REQUEST = "approval_request"
    APPROVAL_RESPONSE = "approval_response"
    APPROVAL_RESOLVED = "approval_resolved"
    TARGET_SWITCH = "target_switch"
    SESSION_SAVE = "session_save"
    SESSION_RESTORE = "session_restore"
    SESSION_RESTORED = "session_restored"
    STATUS_UPDATE = "status_update"
    ERROR = "error"
    ACTIVITY_UPDATE = "activity_update"


@dataclass
class Event:
    type: EventType
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


Handler = Callable[[Event], None]


class EventBus:
    def __init__(self):
        self._subscribers: Dict[EventType, List[Handler]] = {
            t: [] for t in EventType
        }
        self._lock = threading.RLock()
        self.activity_generation = 0
        self._activities: dict[str, dict] = {}

    def subscribe(self, event_type: EventType, handler: Handler) -> None:
        with self._lock:
            if handler not in self._subscribers[event_type]:
                self._subscribers[event_type].append(handler)

    def unsubscribe(self, event_type: EventType, handler: Handler) -> None:
        with self._lock:
            try:
                self._subscribers[event_type].remove(handler)
            except ValueError:
                pass

    def publish(self, event: Event) -> None:
        # Serialize delivery as well as registry updates: concurrent publishers
        # must not deliver a tool result before its already-published call.
        with self._lock:
            if not isinstance(event.data, dict):
                raise TypeError("Event data must be an object")
            if event.type is EventType.ACTIVITY_UPDATE:
                data = event.data
                state = data.get("state")
                if not isinstance(state, str):
                    return
                if (state != "reset"
                        and data.get("generation", self.activity_generation) != self.activity_generation):
                    return
                if state == "reset":
                    self.activity_generation += 1
                    self._activities.clear()
                else:
                    identity = data.get("id")
                    if not isinstance(identity, str) or not identity:
                        return
                    if state in {"done", "error", "cancelled"}:
                        self._activities.pop(identity, None)
                    else:
                        activity = deepcopy(data)
                        activity["kind"] = str(data.get("kind") or "run")
                        activity["label"] = str(data.get("label") or "")
                        for key, default in (("started_at", event.timestamp), ("output_at", None)):
                            value = data.get(key)
                            activity[key] = value if (
                                isinstance(value, (int, float)) and not isinstance(value, bool)
                                and math.isfinite(value)
                            ) else default
                        self._activities[identity] = activity
            for handler in tuple(self._subscribers[event.type]):
                if handler not in self._subscribers[event.type]:
                    continue
                try:
                    handler(event)
                except Exception:
                    logging.exception("Handler failed")

    @property
    def activities(self) -> dict[str, dict]:
        """Detached registry for non-UI consumers such as interruption logic."""
        return self.activity_snapshot()[1]

    def activity_snapshot(self) -> tuple[int, dict[str, dict]]:
        """Read the generation and activity registry atomically."""
        with self._lock:
            return self.activity_generation, deepcopy(self._activities)

    def reset_activity(self) -> None:
        self.publish(Event(EventType.ACTIVITY_UPDATE, {
            "id": "", "kind": "run", "state": "reset", "label": "",
        }))


class Activity:
    """One real operation; reset invalidates its handle, including late updates."""

    def __init__(self, bus: EventBus, kind: str, label: str, *,
                 agent_id: str = "master", state: str = "running") -> None:
        self.bus = bus
        self.generation = bus.activity_generation
        self.data = {
            "id": f"{agent_id}:{kind}:{uuid.uuid4().hex}",
            "kind": kind, "state": state, "label": label, "agent_id": agent_id,
            "started_at": time.time(), "output_at": None,
            "generation": self.generation,
        }
        self.closed = False
        self.update(state)

    def update(self, state: str, *, output: bool = False) -> None:
        # Use the bus lock so terminal updates cannot race a second publisher or
        # be reopened by a listener re-entering this handle during delivery.
        with self.bus._lock:
            if self.closed or self.generation != self.bus.activity_generation:
                return
            self.data["state"] = state
            if output:
                self.data["output_at"] = time.time()
            if state in {"done", "error", "cancelled"}:
                self.closed = True
            self.bus.publish(Event(EventType.ACTIVITY_UPDATE, dict(self.data)))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        state = "done" if exc_type is None else (
            "cancelled" if issubclass(exc_type, (asyncio.CancelledError, GeneratorExit))
            else "error"
        )
        self.update(state)


async def activity_model_stream(bus: EventBus, provider, messages, *, label: str,
                                agent_id: str = "master", **kwargs):
    """Track provider latency before its first token as well as streamed output."""
    with Activity(bus, "model", label, agent_id=agent_id, state="waiting") as activity:
        stream = provider.chat(messages, **kwargs)
        try:
            async for event in stream:
                kind = getattr(event.type, "value", event.type)
                if kind == "tool_call" or (kind == "text" and getattr(event, "content", "")):
                    activity.update("running", output=True)
                elif kind in {"done", "error"}:
                    activity.update("error" if kind == "error" else "done")
                yield event
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()
