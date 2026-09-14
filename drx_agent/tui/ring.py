"""Lifecycle-driven activity indicator and live, keyboard-accessible metrics."""

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Button, Static

from drx_agent.event_bus import EventBus, EventType, Event

_SPIN_FRAMES = ("|", "/", "-", "\\")


def _fmt_tokens(n: int | None) -> str:
    if n is None:
        return "未上报"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


class RingIndicator(Static):
    """Unfinished streams, tool calls and workers are the sole busy signal."""

    can_focus = True
    BINDINGS = [
        Binding("enter,space", "show_metrics", "运行指标", show=False),
    ]
    DEFAULT_CSS = """
    RingIndicator {
        width: 3;
        height: 1;
        text-align: center;
        background: #111a2c;
        color: #53d7c3;
    }
    RingIndicator:focus { background: #17243a; text-style: bold reverse; }
    """

    class BusEvent(Message):
        def __init__(self, event: Event) -> None:
            super().__init__()
            self.event = event

    def __init__(self, event_bus: EventBus):
        super().__init__("o", markup=False)
        self.event_bus = event_bus
        self._frame = 0
        self._busy = False
        self._timer: Timer | None = None
        self._streams: set[tuple[str, str]] = set()
        self._tools: set[tuple[str, str, str]] = set()
        self._workers: set[str] = set()
        self._metrics: dict[str, Any] = {
            "cache_hits": None,
            "tokens_in": None,
            "tokens_out": None,
            "tokens_total": None,
            "cost": None,
            "requests": None,
            "rate": None,
            "mode": "act",
            "streams": 0,
            "tools": 0,
            "workers": 0,
        }
        self._event_types = (
            EventType.STATUS_UPDATE, EventType.AGENT_MESSAGE,
            EventType.TOOL_CALL, EventType.TOOL_RESULT,
            EventType.SUB_AGENT_DISPATCH, EventType.SUB_AGENT_RESULT,
        )
        self.tooltip = "运行指标 · Enter / Space"

    def on_mount(self) -> None:
        for event_type in self._event_types:
            self.event_bus.subscribe(event_type, self._receive_event)
        self._timer = self.set_interval(0.25, self._tick)

    def on_unmount(self) -> None:
        for event_type in self._event_types:
            self.event_bus.unsubscribe(event_type, self._receive_event)
        if self._timer is not None:
            self._timer.stop()

    def _receive_event(self, event: Event) -> None:
        self.post_message(self.BusEvent(event))

    def on_ring_indicator_bus_event(self, message: BusEvent) -> None:
        message.stop()
        event = message.event
        if event.type == EventType.STATUS_UPDATE:
            self._on_status(event)
        elif event.type == EventType.AGENT_MESSAGE:
            self._on_agent_msg(event)
        else:
            self._on_activity(event)

    def _on_status(self, event: Event) -> None:
        for key in (
            "cache_hits", "tokens_in", "tokens_out", "tokens_total",
            "cost", "requests", "rate", "mode",
        ):
            if key in event.data:
                self._metrics[key] = event.data[key]

    def _on_agent_msg(self, event: Event) -> None:
        data = event.data
        if not data.get("streaming"):
            return
        key = (str(data.get("agent_id") or ""), str(data.get("stream_id") or "default"))
        if data.get("final"):
            self._streams.discard(key)
        else:
            self._streams.add(key)
        self._sync_activity()

    @staticmethod
    def _tool_key(data: dict[str, Any]) -> tuple[str, str, str]:
        owner = str(data.get("agent_id") or "")
        for field in ("call_seq", "tool_call_id", "script_num"):
            if data.get(field) is not None:
                return owner, field, str(data[field])
        return owner, "tool", str(data.get("tool") or "unknown")

    def _on_activity(self, event: Event) -> None:
        if event.type == EventType.TOOL_CALL:
            if event.data.get("status") not in {"done", "completed", "error", "timeout", "cancelled"}:
                self._tools.add(self._tool_key(event.data))
        elif event.type == EventType.TOOL_RESULT:
            self._tools.discard(self._tool_key(event.data))
        elif event.type == EventType.SUB_AGENT_DISPATCH:
            self._workers.add(str(event.data.get("agent_id") or "unknown"))
        elif event.type == EventType.SUB_AGENT_RESULT:
            self._workers.discard(str(event.data.get("agent_id") or "unknown"))
        self._sync_activity()

    def _sync_activity(self) -> None:
        self._busy = bool(self._streams or self._tools or self._workers)
        self._metrics.update(streams=len(self._streams), tools=len(self._tools), workers=len(self._workers))
        self.tooltip = (
            f"流 {len(self._streams)} · 工具 {len(self._tools)} · Agent {len(self._workers)}"
            " · Enter 查看指标"
        )
        if not self._busy:
            self.update("o")

    def _tick(self) -> None:
        if self._busy:
            self._frame = (self._frame + 1) % len(_SPIN_FRAMES)
            self.update(_SPIN_FRAMES[self._frame])

    def action_show_metrics(self) -> None:
        if not isinstance(self.app.screen, MetricsScreen):
            self.app.push_screen(MetricsScreen(self._metrics))

    def on_click(self, event) -> None:
        event.stop()
        self.focus()
        self.action_show_metrics()


class MetricsScreen(ModalScreen[None]):
    """A scrollable live view of reported usage and observed active work."""

    BINDINGS = [
        Binding("escape", "close", "关闭"),
        Binding("q", "close", "关闭", show=False),
    ]
    DEFAULT_CSS = """
    MetricsScreen { align: center middle; }
    #metrics-dialog {
        width: 90%;
        max-width: 64;
        height: 85%;
        max-height: 28;
        border: round #26354b;
        background: #111a2c;
        color: #e7edf7;
        padding: 0 1;
    }
    #metrics-title { height: 2; color: #53d7c3; text-style: bold; }
    #metrics-scroll { height: 1fr; overflow-x: hidden; }
    #metrics-body { width: 1fr; height: auto; }
    #metrics-close { width: 100%; min-width: 0; height: 3; }
    """

    def __init__(self, metrics: dict[str, Any]):
        super().__init__()
        # Share the indicator's dictionary: it is only mutated on the UI thread.
        self._metrics = metrics
        self._last_text = ""
        self._timer: Timer | None = None

    def compose(self) -> ComposeResult:
        with Container(id="metrics-dialog"):
            yield Static("运行指标", id="metrics-title", markup=False)
            with VerticalScroll(id="metrics-scroll"):
                yield Static(self._build_metrics_text(), id="metrics-body", markup=False)
            yield Button("关闭 (Esc)", id="metrics-close")

    def on_mount(self) -> None:
        self.query_one("#metrics-scroll", VerticalScroll).focus()
        self._refresh_metrics()
        self._timer = self.set_interval(0.25, self._refresh_metrics)

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()

    def _refresh_metrics(self) -> None:
        text = self._build_metrics_text()
        if text != self._last_text:
            self.query_one("#metrics-body", Static).update(text)
            self._last_text = text

    def action_close(self) -> None:
        self.dismiss()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "metrics-close":
            event.stop()
            self.action_close()

    def _build_metrics_text(self) -> str:
        metrics = self._metrics
        tokens_in = metrics.get("tokens_in")
        cache_hits = metrics.get("cache_hits")
        hit_pct = (
            f"{cache_hits / tokens_in * 100:.1f}%"
            if cache_hits is not None and tokens_in else "未提供可计算用量"
        )

        def reported(key: str) -> str:
            value = metrics.get(key)
            return "未上报" if value is None else str(value)

        lines = [
            f"模式: {reported('mode')}",
            f"运行中流: {reported('streams')}",
            f"运行中工具: {reported('tools')}",
            f"运行中 Agent: {reported('workers')}",
            "",
            f"成本: {reported('cost')}",
            f"输入 tokens: {_fmt_tokens(tokens_in)}",
            f"输出 tokens: {_fmt_tokens(metrics.get('tokens_out'))}",
            f"总计 tokens: {_fmt_tokens(metrics.get('tokens_total'))}",
            f"缓存命中: {_fmt_tokens(cache_hits)} tokens",
            f"缓存命中率: {hit_pct}",
            f"请求数: {reported('requests')}",
            f"请求速率: {reported('rate')}" + (" r/min" if metrics.get("rate") is not None else ""),
        ]
        return "\n".join(lines)
