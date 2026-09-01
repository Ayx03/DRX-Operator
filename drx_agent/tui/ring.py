"""Bottom-right ring status indicator + secondary metrics menu."""

import time

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from drx_agent.event_bus import EventBus, EventType, Event

_SPIN_FRAMES = ("◐", "◓", "◑", "◒")


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


class RingIndicator(Button):
    """Bottom-right circular status ring; click opens the metrics menu."""

    DEFAULT_CSS = """
    RingIndicator {
        width: 3;
        height: 1;
        background: $surface;
        color: #58a6ff;
    }
    """

    def __init__(self, event_bus: EventBus):
        super().__init__("○")
        self.event_bus = event_bus
        self._frame = 0
        self._busy = False
        self._last_activity = 0.0
        self._metrics: dict = {
            "cache_hits": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "tokens_total": 0,
            "cost": "$0.0000",
            "requests": 0,
            "rate": 0,
            "mode": "act",
        }

    def on_mount(self) -> None:
        self.event_bus.subscribe(EventType.STATUS_UPDATE, self._on_status)
        self.event_bus.subscribe(EventType.AGENT_MESSAGE, self._on_agent_msg)
        self.event_bus.subscribe(EventType.TOOL_CALL, self._on_activity)
        self.event_bus.subscribe(EventType.TOOL_RESULT, self._on_activity)
        self.set_interval(0.25, self._tick)

    def _on_status(self, event: Event) -> None:
        data = event.data
        for k in (
            "cache_hits", "tokens_in", "tokens_out", "tokens_total",
            "cost", "requests", "rate", "mode",
        ):
            if k in data:
                self._metrics[k] = data[k]
        if "text" in data:
            self._last_activity = time.time()
            self._busy = True

    def _on_agent_msg(self, event: Event) -> None:
        if event.data.get("streaming"):
            self._last_activity = time.time()
            if event.data.get("final"):
                self._busy = False
            else:
                self._busy = True

    def _on_activity(self, event: Event) -> None:
        self._last_activity = time.time()
        self._busy = True

    def _tick(self) -> None:
        if self._busy:
            if time.time() - self._last_activity > 5:
                self._busy = False
                self.label = "○"
                return
            self._frame = (self._frame + 1) % len(_SPIN_FRAMES)
            self.label = _SPIN_FRAMES[self._frame]
        else:
            self.label = "○"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.app.push_screen(MetricsScreen(self._metrics))


class MetricsScreen(ModalScreen):
    """Secondary menu: cache hit rate + token usage + cost."""

    BINDINGS = [Binding("escape", "app.pop_screen", "关闭")]

    DEFAULT_CSS = """
    MetricsScreen {
        align: center middle;
    }
    MetricsScreen > Container {
        width: 46;
        height: auto;
        border: round #30363d;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, metrics: dict):
        super().__init__()
        self._metrics = metrics

    def compose(self) -> ComposeResult:
        yield Container(Static(self._build_metrics_text(), id="metrics-body"))

    def _build_metrics_text(self) -> str:
        m = self._metrics
        hit_pct = (
            (m["cache_hits"] / m["tokens_in"] * 100.0)
            if m["tokens_in"] else 0.0
        )
        return "\n".join(
            [
                "运行指标",
                f"缓存命中率: {hit_pct:.1f}%",
                f"缓存命中: {_fmt_tokens(m['cache_hits'])} tokens",
                f"tokens: {_fmt_tokens(m['tokens_in'])} in / "
                f"{_fmt_tokens(m['tokens_out'])} out / "
                f"{_fmt_tokens(m['tokens_total'])} total",
                f"cost: {m['cost']}",
                f"requests: {m['requests']}",
                f"rate: {m['rate']} r/min",
                f"mode: {m['mode']}",
                "",
                "[Esc] 关闭",
            ]
        )
