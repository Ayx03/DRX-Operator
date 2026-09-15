"""Cell-width-aware status bar, with essential usage information first."""

from copy import deepcopy
import threading
import math
from typing import Any

from drx_agent.session.usage import cache_text

from rich.text import Text as RichText
from textual.message import Message
from textual.widgets import Static

from drx_agent.event_bus import EventBus, EventType, Event



def _number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _fmt_tokens(n: int | None) -> str:
    n = _number(n)
    if n is None:
        return "--"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)




class StatusFooter(Static):
    """Prioritize mode, cost and tokens; optional details never force overflow."""

    DEFAULT_RENDER = "ACT │ cost: -- │ tokens: --"
    COMPONENT_CLASSES = {"status-mode", "status-plan", "status-text", "status-muted", "status-divider"}
    DEFAULT_CSS = """
    StatusFooter {
        height: 1;
        width: 1fr;
        color: $text;
        background: $background;
        overflow: hidden hidden;
    }
    StatusFooter > .status-mode { color: $primary; text-style: bold; }
    StatusFooter > .status-plan { color: $warning; text-style: bold; }
    StatusFooter > .status-text { color: $text; }
    StatusFooter > .status-muted { color: $text-muted; }
    StatusFooter > .status-divider { color: $panel; }
    """

    class BusEvent(Message):
        pass

    def __init__(self, event_bus: EventBus):
        super().__init__(self.DEFAULT_RENDER, markup=False)
        self.event_bus = event_bus
        self._state: dict[str, Any] = {
            "cost": None,
            "tokens_in": None,
            "tokens_out": None,
            "tokens_total": None,
            "cache_hits": None,
            "cache_known_input_tokens": None,
            "cache_unknown_input_tokens": None,
            "cache_unknown_requests": None,
            "rate": None,
            "active_targets": None,
            "requests": None,
            "mode": "act",
            "model": None,
            "text": "",
        }
        self._pending_lock = threading.Lock()
        self._pending_status: dict[str, Any] = {}
        self._notification_pending = False

    def on_mount(self) -> None:
        self.event_bus.subscribe(EventType.STATUS_UPDATE, self._receive_event)
        self._refresh()

    def on_unmount(self) -> None:
        self.event_bus.unsubscribe(EventType.STATUS_UPDATE, self._receive_event)
        with self._pending_lock:
            self._pending_status.clear()
            self._notification_pending = False

    def on_resize(self, event) -> None:
        self._refresh()

    def _receive_event(self, event: Event) -> None:
        with self._pending_lock:
            self._pending_status.update({
                key: deepcopy(value) for key, value in event.data.items() if key in self._state
            })
            if self._notification_pending:
                return
            self._notification_pending = True
            if not self.post_message(self.BusEvent()):
                self._notification_pending = False

    def on_status_footer_bus_event(self, message: BusEvent) -> None:
        message.stop()
        with self._pending_lock:
            status, self._pending_status = self._pending_status, {}
            self._notification_pending = False
        if not self.is_mounted or not self.is_attached:
            return
        self._on_status(Event(EventType.STATUS_UPDATE, status))

    def _on_status(self, event: Event) -> None:
        for key in self._state:
            if key in event.data:
                self._state[key] = event.data[key]
        self._refresh()

    def _build_text(self) -> RichText:
        state = self._state
        width = self.content_size.width if self.is_mounted else (self.size.width or 140)
        if width <= 0:
            return RichText()
        mode = str(state["mode"]).upper().replace("\n", " ")
        cost = str(state["cost"] if state["cost"] is not None else "--").replace("\n", " ")
        total = _fmt_tokens(state["tokens_total"])
        mode_style = self.get_component_rich_style("status-plan" if state["mode"] == "plan" else "status-mode")
        separator = " │ "
        # Start with full essential fields, falling back together to preserve all three.
        variants = (
            (mode, f"cost: {cost}", f"tokens: {_fmt_tokens(state['tokens_in'])} in / "
             f"{_fmt_tokens(state['tokens_out'])} out / {total} total"),
            (mode, f"cost: {cost}", f"tokens: {total}"),
            (mode, cost, f"T:{total}"),
        )
        essentials = variants[-1]
        for candidate in variants:
            if RichText(separator.join(candidate)).cell_len <= width:
                essentials = candidate
                break
        if RichText(separator.join(essentials)).cell_len > width:
            separator = " "
        text = RichText(essentials[0], style=mode_style, no_wrap=True)
        for field in essentials[1:]:
            text.append(separator, style=self.get_component_rich_style("status-divider"))
            text.append(field, style=self.get_component_rich_style("status-text"))
        extras = []
        if state["model"]:
            extras.append(f"model: {str(state['model']).replace(chr(10), ' ').replace(chr(13), ' ')}")
        coverage = cache_text(
            _number(state["cache_hits"]), _number(state["cache_known_input_tokens"]),
            _number(state["cache_unknown_input_tokens"]), _number(state["cache_unknown_requests"]),
        )
        extras.append(f"缓存命中: {coverage}")
        if state["rate"] is not None:
            extras.append(f"rate: {state['rate']} r/min")
        if state["active_targets"] is not None:
            extras.append(f"targets: {state['active_targets']}")
        for field in extras:
            if text.cell_len + RichText(separator + field).cell_len <= width:
                text.append(separator, style=self.get_component_rich_style("status-divider"))
                text.append(field, style=self.get_component_rich_style("status-muted"))
        text.truncate(width, overflow="ellipsis", pad=False)
        return text

    def _refresh(self) -> None:
        self.update(self._build_text())
