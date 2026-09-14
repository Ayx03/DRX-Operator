"""Lifecycle-driven activity indicator and live, keyboard-accessible metrics."""

from copy import deepcopy
import math
import threading
from typing import Any

from drx_agent.session.usage import cache_text, cost_text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Button, Static

from drx_agent.event_bus import EventBus, EventType, Event

_SPIN_FRAMES = ("|", "/", "-", "\\")


def _number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _fmt_tokens(n: int | None) -> str:
    n = _number(n)
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
        background: $background;
        color: $primary;
    }
    RingIndicator:focus { background: $panel; text-style: bold reverse; }
    """

    class BusEvent(Message):
        def __init__(self, event: Event, generation: int) -> None:
            super().__init__()
            self.event = event
            self.generation = generation

    def __init__(self, event_bus: EventBus):
        super().__init__("o", markup=False)
        self.event_bus = event_bus
        self._frame = 0
        self._busy = False
        self._timer: Timer | None = None
        self._streams: set[tuple[str, str]] = set()
        self._tools: set[tuple[str, str, str]] = set()
        self._workers: set[str] = set()
        self._receive_lock = threading.Lock()
        self._received_streams: set[tuple[str, str]] = set()
        self._activity_pending = False
        self._activity_generation = event_bus.activity_generation
        self._metrics: dict[str, Any] = {
            "cache_hits": None,
            "cache_known_input_tokens": None,
            "cache_unknown_input_tokens": None,
            "cache_unknown_requests": None,
            "usage_unknown_requests": None,
            "cache_write_tokens": None,
            "by_model": {},
            "by_actor": {},
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
            EventType.ACTIVITY_UPDATE,
        )
        self.tooltip = "运行指标 · Enter / Space"

    def on_mount(self) -> None:
        for event_type in self._event_types:
            self.event_bus.subscribe(event_type, self._receive_event)
        self._timer = self.set_interval(0.25, self._tick)
        self._sync_activity()

    def on_unmount(self) -> None:
        for event_type in self._event_types:
            self.event_bus.unsubscribe(event_type, self._receive_event)
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        with self._receive_lock:
            self._received_streams.clear()
            self._activity_pending = False
        self._streams.clear()
        self._tools.clear()
        self._workers.clear()

    def _receive_event(self, event: Event) -> None:
        # A stream's first token and final marker change liveness; intermediate
        # deltas do not. Never enqueue an indicator message per generated token.
        with self._receive_lock:
            data = event.data
            if event.type is EventType.AGENT_MESSAGE:
                if not data.get("streaming"):
                    return
                key = (str(data.get("agent_id") or ""), str(data.get("stream_id") or "default"))
                if data.get("final"):
                    self._received_streams.discard(key)
                elif key in self._received_streams:
                    return
                else:
                    self._received_streams.add(key)
                data = {key: data[key] for key in ("agent_id", "stream_id", "streaming", "final") if key in data}
            elif event.type is EventType.ACTIVITY_UPDATE:
                if data.get("state") == "reset":
                    self._received_streams.clear()
                if self._activity_pending:
                    return
                self._activity_pending = True
                data = {}
            elif event.type in (EventType.TOOL_CALL, EventType.TOOL_RESULT):
                data = {key: data[key] for key in (
                    "agent_id", "invocation_id", "call_seq", "tool_call_id", "script_num", "tool", "status",
                ) if key in data}
            elif event.type in (EventType.SUB_AGENT_DISPATCH, EventType.SUB_AGENT_RESULT):
                data = {"agent_id": data.get("agent_id")}
            elif event.type is EventType.STATUS_UPDATE:
                data = {key: value for key, value in data.items() if key in self._metrics}
            self.post_message(self.BusEvent(
                Event(event.type, deepcopy(data), event.timestamp), self.event_bus.activity_generation,
            ))

    def on_ring_indicator_bus_event(self, message: BusEvent) -> None:
        message.stop()
        if not self.is_mounted or not self.is_attached:
            return
        event = message.event
        if event.type not in (EventType.STATUS_UPDATE, EventType.ACTIVITY_UPDATE):
            if message.generation != self.event_bus.activity_generation:
                return
        if event.type == EventType.STATUS_UPDATE:
            self._on_status(event)
        elif event.type == EventType.ACTIVITY_UPDATE:
            with self._receive_lock:
                self._activity_pending = False
            generation, activities = self.event_bus.activity_snapshot()
            if generation != self._activity_generation or not activities:
                self._streams.clear()
                self._tools.clear()
                self._workers.clear()
            self._activity_generation = generation
            self._sync_activity()
        elif event.type == EventType.AGENT_MESSAGE:
            self._on_agent_msg(event)
        else:
            self._on_activity(event)

    def _on_status(self, event: Event) -> None:
        for key in (
            "cache_hits", "tokens_in", "tokens_out", "tokens_total",
            "cost", "requests", "rate", "mode",
            "cache_known_input_tokens", "cache_unknown_input_tokens", "cache_write_tokens",
            "by_model", "by_actor",
            "cache_unknown_requests", "usage_unknown_requests",
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
        for field in ("invocation_id", "call_seq", "tool_call_id", "script_num"):
            if data.get(field) is not None:
                return owner, field, str(data[field])
        return owner, "tool", str(data.get("tool") or "unknown")

    def _on_activity(self, event: Event) -> None:
        if event.type == EventType.TOOL_CALL:
            if str(event.data.get("status")) not in {"done", "completed", "error", "timeout", "cancelled"}:
                self._tools.add(self._tool_key(event.data))
        elif event.type == EventType.TOOL_RESULT:
            self._tools.discard(self._tool_key(event.data))
        elif event.type == EventType.SUB_AGENT_DISPATCH:
            self._workers.add(str(event.data.get("agent_id") or "unknown"))
        elif event.type == EventType.SUB_AGENT_RESULT:
            self._workers.discard(str(event.data.get("agent_id") or "unknown"))
        self._sync_activity()

    def _sync_activity(self) -> None:
        _, activities = self.event_bus.activity_snapshot()
        self._busy = bool(activities or self._streams or self._tools or self._workers)
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
        border: round $panel;
        background: $surface;
        color: $text;
        padding: 0 1;
    }
    #metrics-title { height: 2; color: $primary; text-style: bold; }
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
        self._dismiss_requested = False

    def compose(self) -> ComposeResult:
        with Container(id="metrics-dialog"):
            yield Static("运行指标", id="metrics-title", markup=False)
            with VerticalScroll(id="metrics-scroll"):
                yield Static(self._build_metrics_text(), id="metrics-body", markup=False)
            yield Button("关闭 (Esc)", id="metrics-close")

    def on_mount(self) -> None:
        self._dismiss_requested = False
        self.query_one("#metrics-scroll", VerticalScroll).focus()
        self._refresh_metrics()
        self._timer = self.set_interval(0.25, self._refresh_metrics)

    def on_unmount(self) -> None:
        self._dismiss_requested = True
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _refresh_metrics(self) -> None:
        if not self.is_attached or not self.is_mounted or not self.is_current or self._dismiss_requested:
            return
        text = self._build_metrics_text()
        if text != self._last_text:
            self.query_one("#metrics-body", Static).update(text)
            self._last_text = text

    def action_close(self) -> None:
        if self._dismiss_requested or not self.is_attached or not self.is_current:
            return
        self._dismiss_requested = True
        self.dismiss()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "metrics-close":
            event.stop()
            self.action_close()

    def _build_metrics_text(self) -> str:
        numeric_keys = (
            "tokens_in", "tokens_out", "tokens_total", "cache_hits",
            "cache_known_input_tokens", "cache_unknown_input_tokens", "cache_write_tokens",
            "cache_unknown_requests", "usage_unknown_requests",
        )
        metrics = {**self._metrics, **{key: _number(self._metrics.get(key)) for key in numeric_keys}}
        tokens_in = metrics.get("tokens_in")
        cache_hits = metrics.get("cache_hits")
        hit_pct = cache_text(
            cache_hits, metrics.get("cache_known_input_tokens"),
            metrics.get("cache_unknown_input_tokens"), metrics.get("cache_unknown_requests"),
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
            f"缓存已测输入: {_fmt_tokens(metrics.get('cache_known_input_tokens'))}",
            f"缓存未知输入: {_fmt_tokens(metrics.get('cache_unknown_input_tokens'))}",
            f"缓存写入: {_fmt_tokens(metrics.get('cache_write_tokens'))} tokens",
            f"缓存未上报请求: {reported('cache_unknown_requests')}",
            f"用量不完整请求: {reported('usage_unknown_requests')}",
            "成本为已知定价部分的估算；DeepSeek 按记录时的 UTC 时段费率",
            f"请求数: {reported('requests')}",
            f"请求速率: {reported('rate')}" + (" r/min" if metrics.get("rate") is not None else ""),
        ]
        for key, title in (("by_model", "按模型"), ("by_actor", "按角色")):
            groups = metrics.get(key) or {}
            if not isinstance(groups, dict) or not groups:
                continue
            lines.extend(("", title))
            for name, slot in groups.items():
                if not isinstance(slot, dict):
                    continue
                slot = {key: _number(value) for key, value in slot.items()}
                hits = slot.get("cache_hit_tokens") if slot.get("cache_known_requests") else None
                coverage = cache_text(
                    hits, slot.get("cache_known_input_tokens"),
                    slot.get("cache_unknown_input_tokens"),
                    (slot.get("requests") or 0) - (slot.get("cache_known_requests") or 0),
                )
                lines.extend((
                    str(name),
                    f"  {slot.get('requests', 0)} req · "
                    f"{_fmt_tokens(slot.get('prompt_tokens'))} in / "
                    f"{_fmt_tokens(slot.get('completion_tokens'))} out",
                    f"  缓存命中率: {coverage} · 成本: "
                    + (cost_text(slot) if slot.get("cost_usd") is not None else "unavailable"),
                    f"  已测: {_fmt_tokens(slot.get('cache_known_input_tokens'))} / "
                    f"未知: {_fmt_tokens(slot.get('cache_unknown_input_tokens'))}",
                ))
        return "\n".join(lines)
