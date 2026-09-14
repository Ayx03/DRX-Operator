"""Cell-width-aware status bar, with essential usage information first."""

from typing import Any

from rich.text import Text as RichText
from textual.message import Message
from textual.widgets import Static

from drx_agent.event_bus import EventBus, EventType, Event

_AUTHOR = "BushSEC · github.com/BushANQ"


def _fmt_tokens(n: int | None) -> str:
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
    DEFAULT_CSS = """
    StatusFooter {
        height: 1;
        width: 1fr;
        color: #e7edf7;
        background: #111a2c;
        overflow: hidden hidden;
    }
    """

    class BusEvent(Message):
        def __init__(self, event: Event) -> None:
            super().__init__()
            self.event = event

    def __init__(self, event_bus: EventBus):
        super().__init__(self.DEFAULT_RENDER, markup=True)
        self.event_bus = event_bus
        self._state: dict[str, Any] = {
            "cost": None,
            "tokens_in": None,
            "tokens_out": None,
            "tokens_total": None,
            "cache_hits": None,
            "rate": None,
            "active_targets": None,
            "requests": None,
            "mode": "act",
            "text": "",
        }

    def on_mount(self) -> None:
        self.event_bus.subscribe(EventType.STATUS_UPDATE, self._receive_event)
        self._refresh()

    def on_unmount(self) -> None:
        self.event_bus.unsubscribe(EventType.STATUS_UPDATE, self._receive_event)

    def on_resize(self, event) -> None:
        self._refresh()

    def _receive_event(self, event: Event) -> None:
        self.post_message(self.BusEvent(event))

    def on_status_footer_bus_event(self, message: BusEvent) -> None:
        message.stop()
        self._on_status(message.event)

    def _on_status(self, event: Event) -> None:
        for key in self._state:
            if key in event.data:
                self._state[key] = event.data[key]
        self._refresh()

    def _build_text(self) -> str:
        state = self._state
        width = self.content_size.width if self.is_mounted else (self.size.width or 140)
        if width <= 0:
            return ""
        mode = str(state["mode"]).upper().replace("\n", " ")
        cost = str(state["cost"] if state["cost"] is not None else "--").replace("\n", " ")
        total = _fmt_tokens(state["tokens_total"])
        mode_style = "bold #ffc36a" if state["mode"] == "plan" else "bold #53d7c3"
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
            text.append(separator, style="#26354b")
            text.append(field, style="#e7edf7")
        extras = []
        if state["cache_hits"] is not None:
            hits = f"缓存命中: {_fmt_tokens(state['cache_hits'])}"
            if state["tokens_in"]:
                hits += f" ({state['cache_hits'] / state['tokens_in'] * 100:.1f}%)"
            extras.append(hits)
        if state["rate"] is not None:
            extras.append(f"rate: {state['rate']} r/min")
        if state["active_targets"] is not None:
            extras.append(f"targets: {state['active_targets']}")
        if state["text"]:
            extras.append(" ".join(str(state["text"]).splitlines()))
        for field in extras:
            if text.cell_len + RichText(separator + field).cell_len <= width:
                text.append(separator, style="#26354b")
                text.append(field, style="#92a4bb")
        author_width = RichText(_AUTHOR).cell_len
        if text.cell_len + author_width + 2 <= width:
            text.append(" " * (width - text.cell_len - author_width))
            text.append(_AUTHOR, style="#92a4bb")
        text.truncate(width, overflow="ellipsis", pad=False)
        return text.markup

    def _refresh(self) -> None:
        self.update(self._build_text())
