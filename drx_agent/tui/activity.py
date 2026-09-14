"""Live activity, independent of conversation history and model token delivery."""

import math
import threading
import time

from rich.text import Text
from textual.message import Message
from textual.widgets import Static

from drx_agent.event_bus import Event, EventBus, EventType


class ActivityBar(Static):
    DEFAULT_CSS = """
    ActivityBar {
        height: 1;
        width: 1fr;
        background: $background;
        color: $primary;
        padding: 0 1;
        overflow: hidden hidden;
    }
    """
    FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    class BusEvent(Message):
        pass

    def __init__(self, event_bus: EventBus) -> None:
        super().__init__("", markup=False)
        self.event_bus = event_bus
        self._activities: dict[str, dict] = {}
        self._frame = 0
        self._timer = None
        self._last_output: float | None = None
        self._generation = event_bus.activity_generation
        self._pending_lock = threading.Lock()
        self._notification_pending = False
        self._received_output: tuple[int, float] | None = None
        self.display = False

    def on_mount(self) -> None:
        self.event_bus.subscribe(EventType.ACTIVITY_UPDATE, self._receive_event)
        self.event_bus.subscribe(EventType.AGENT_MESSAGE, self._receive_event)
        self.event_bus.subscribe(EventType.TOOL_RESULT, self._receive_event)
        self.event_bus.subscribe(EventType.SUB_AGENT_RESULT, self._receive_event)
        self._generation, self._activities = self.event_bus.activity_snapshot()
        self._last_output = max(
            (item["output_at"] for item in self._activities.values() if item.get("output_at")),
            default=None,
        )
        self._timer = self.set_interval(0.1, self._tick, pause=True)
        self._sync()

    def on_unmount(self) -> None:
        for kind in (EventType.ACTIVITY_UPDATE, EventType.AGENT_MESSAGE,
                     EventType.TOOL_RESULT, EventType.SUB_AGENT_RESULT):
            self.event_bus.unsubscribe(kind, self._receive_event)
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._activities.clear()
        with self._pending_lock:
            self._notification_pending = False
            self._received_output = None

    def _receive_event(self, event: Event) -> None:
        with self._pending_lock:
            data = event.data
            if event.type is not EventType.ACTIVITY_UPDATE and (
                event.type is not EventType.AGENT_MESSAGE or (
                    data.get("role") == "assistant" and (data.get("text") or data.get("delta"))
                )
            ):
                timestamp = event.timestamp
                if isinstance(timestamp, (int, float)) and math.isfinite(timestamp):
                    self._received_output = (self.event_bus.activity_generation, timestamp)
            if self._notification_pending:
                return
            self._notification_pending = True
            if not self.post_message(self.BusEvent()):
                self._notification_pending = False

    def on_activity_bar_bus_event(self, message: BusEvent) -> None:
        message.stop()
        with self._pending_lock:
            self._notification_pending = False
            output, self._received_output = self._received_output, None
        if not self.is_mounted or not self.is_attached:
            return
        generation, self._activities = self.event_bus.activity_snapshot()
        if self._generation != generation:
            self._generation = generation
            self._last_output = None
        if not self._activities:
            self._last_output = None
        else:
            candidates = [item["output_at"] for item in self._activities.values()
                          if item.get("output_at") is not None]
            if output is not None and output[0] == generation:
                candidates.append(output[1])
            if candidates:
                self._last_output = max(self._last_output or 0, *candidates)
        self._sync()

    def _sync(self) -> None:
        self.display = bool(self._activities)
        if self._timer is not None:
            if self._activities:
                self._timer.resume()
            else:
                self._timer.pause()
        self._render_activity()

    def _tick(self) -> None:
        if not self._activities:
            return
        self._frame = (self._frame + 1) % len(self.FRAMES)
        self._render_activity()

    def on_resize(self) -> None:
        self._render_activity()

    @staticmethod
    def _duration(seconds: float) -> str:
        seconds = max(0, int(seconds))
        return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"

    def _render_activity(self) -> None:
        if not self._activities:
            self.update("")
            return
        now = time.time()
        entries = list(self._activities.values())
        # Approval and stopping states matter more than a parent run label.
        selected = min(entries, key=lambda item: (
            0 if item["state"] == "stopping" else
            1 if item["state"] == "waiting" and item["kind"] != "model" else
            2 if item["kind"] in {"tool", "model", "vote"} else
            3 if item["kind"] == "worker" else 4
        ))
        elapsed = self._duration(now - min(item.get("started_at", now) for item in entries))
        label = " ".join(str(selected["label"]).splitlines())
        state = selected["state"]
        text = Text(f"{self.FRAMES[self._frame]} {elapsed} · {state} · ", no_wrap=True)
        label_text = Text(label, no_wrap=True)
        label_text.truncate(max(12, self.content_size.width - 60), overflow="ellipsis")
        text.append_text(label_text)
        workers = sum(item["kind"] == "worker" for item in entries)
        votes = sum(item["kind"] == "vote" for item in entries)
        if workers:
            text.append(f" · {workers} worker{'s' if workers != 1 else ''}")
        if votes:
            text.append(f" · {votes} ballot{'s' if votes != 1 else ''}")
        text.append(f" · output {self._duration(now - self._last_output)} ago"
                    if self._last_output is not None else " · awaiting output")
        width = self.content_size.width
        if width > 0:
            text.truncate(width, overflow="ellipsis")
        self.update(text)
