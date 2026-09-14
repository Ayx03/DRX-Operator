"""Bounded, anchored conversation views over the lossless shared transcript."""

from typing import Any
from threading import Lock

from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Static

from drx_agent.event_bus import EventBus
from drx_agent.tui.banner import build_banner
from drx_agent.tui.transcript import TranscriptLog, literal_text, tool_summary


class BannerBubble(Static):
    DEFAULT_CSS = """
    BannerBubble { height: auto; padding: 1; margin-bottom: 1;
        border-left: solid $panel; color: $text-muted; }
    """

    def __init__(self) -> None:
        super().__init__(build_banner())


class ToolOutputScreen(ModalScreen[None]):
    """Bounded literal pages; copy actions always use the complete original data."""

    PAGE_CHARS = 20000
    PAGE_LINES = 300
    BINDINGS = [Binding("escape", "close", "关闭", show=False)]
    DEFAULT_CSS = """
    ToolOutputScreen { align: center middle; background: $background 85%; }
    ToolOutputScreen > #tool-output-dialog {
        width: 92%; max-width: 110; height: 90%;
        background: $surface; border: solid $panel; padding: 0 1;
    }
    ToolOutputScreen #tool-output-title { height: auto; color: $primary; text-style: bold; }
    ToolOutputScreen #tool-output-page { height: auto; color: $text-muted; }
    ToolOutputScreen #tool-output-scroll { height: 1fr; min-height: 3; }
    ToolOutputScreen .tool-detail { height: auto; color: $text; }
    ToolOutputScreen #tool-output-actions {
        layout: grid; grid-size: 3; grid-columns: 1fr 1fr 1fr; grid-rows: 3 3; height: 6;
    }
    ToolOutputScreen.-narrow #tool-output-actions {
        grid-size: 1 5; grid-columns: 1fr; grid-rows: 3; height: 15;
    }
    ToolOutputScreen Button { min-width: 0; width: 1fr; padding: 0; }
    """

    def __init__(self, tool_name: str, output: str, input_text: str = "") -> None:
        super().__init__()
        self.tool_name = literal_text(tool_name)
        self.output_text = literal_text(output)
        self.input_text = literal_text(input_text)
        self._pages = [(0, 0)]
        self._page = 0
        self._next_offsets = (0, 0)
        self._dismiss_requested = False
        self._dismissed = False

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="tool-output-dialog"):
            yield Static(Content("完整记录 · " + bounded_preview(self.tool_name, 240, 2)), id="tool-output-title")
            yield Static("", id="tool-output-page", markup=False)
            with VerticalScroll(id="tool-output-scroll"):
                yield Static("", id="tool-input-text", classes="tool-detail", markup=False)
                yield Static("", id="tool-output-text", classes="tool-detail", markup=False)
            with Horizontal(id="tool-output-actions"):
                yield Button("上一段", id="tool-page-previous")
                yield Button("下一段", id="tool-page-next")
                yield Button("复制输入", id="tool-input-copy", disabled=not self.input_text)
                yield Button("复制输出", id="tool-output-copy", variant="primary")
                yield Button("关闭", id="tool-output-close")

    def on_mount(self) -> None:
        if self._dismiss_requested:
            self.action_close()
            return
        self.set_class(self.size.width < 40, "-narrow")
        self._render_page()
        self.query_one("#tool-output-scroll", VerticalScroll).focus()

    def on_resize(self) -> None:
        self.set_class(self.size.width < 40, "-narrow")

    def on_screen_resume(self) -> None:
        if self._dismiss_requested:
            self.action_close()

    def _page_end(self, text: str, start: int) -> int:
        end = min(len(text), start + self.PAGE_CHARS)
        cursor = start
        for _ in range(self.PAGE_LINES):
            newline = text.find("\n", cursor, end)
            if newline < 0:
                return end
            cursor = newline + 1
        return cursor

    def _render_page(self) -> None:
        input_start, output_start = self._pages[self._page]
        input_end = self._page_end(self.input_text, input_start)
        output_end = self._page_end(self.output_text, output_start)
        self._next_offsets = input_end, output_end
        self.query_one("#tool-input-text", Static).update(
            Content("Input\n" + self.input_text[input_start:input_end] if input_end > input_start else "")
        )
        self.query_one("#tool-output-text", Static).update(Content(self.output_text[output_start:output_end]))
        self.query_one("#tool-output-page", Static).update(
            f"第 {self._page + 1} 段 · 输入 {input_start}–{input_end}/{len(self.input_text)}"
            f" · 输出 {output_start}–{output_end}/{len(self.output_text)} 字符 · 复制不截断"
        )
        self.query_one("#tool-page-previous", Button).disabled = self._page == 0
        self.query_one("#tool-page-next", Button).disabled = (
            input_end == len(self.input_text) and output_end == len(self.output_text)
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if self._dismissed or not self.is_attached:
            return
        button_id = event.button.id
        if button_id == "tool-output-close":
            self.action_close()
            return
        if self.app.screen is not self:
            return
        if button_id in {"tool-page-previous", "tool-page-next"}:
            if event.button.disabled:
                return
            if button_id == "tool-page-previous":
                self._page -= 1
            else:
                self._page += 1
                if self._page == len(self._pages):
                    self._pages.append(self._next_offsets)
            self._render_page()
            self.query_one("#tool-output-scroll", VerticalScroll).scroll_home(animate=False)
            return
        if button_id not in {"tool-input-copy", "tool-output-copy"}:
            return
        text = self.input_text if button_id == "tool-input-copy" else self.output_text
        copy_text = getattr(self.app, "_copy_text", None)
        if callable(copy_text):
            copy_text(text, "复制完整原文")
        else:
            self.app.copy_to_clipboard(text)

    def action_close(self) -> None:
        self._dismiss_requested = True
        if not self._dismissed and self.is_attached and self.app.screen is self:
            self._dismissed = True
            self.dismiss(None)


def bounded_preview(text: Any, limit: int = 2400, lines: int = 32) -> str:
    text = literal_text(text)
    preview = "\n".join(text[:limit].splitlines()[:lines])
    if len(preview) < len(text.rstrip("\n")):
        preview += "\n… 此处仅预览；完整原文可在详情或会话记录中查看/复制"
    return preview


class ToolCard(Vertical):
    """Literal header and explicit content visibility, without Collapsible internals."""

    collapsed = reactive(True, init=False)
    DEFAULT_CSS = """
    ToolCard { height: auto; width: 100%; margin-bottom: 1;
        background: $surface; border-left: solid $panel; padding: 0; }
    ToolCard.tool-error { border-left: solid $error; }
    ToolCard > .tool-title {
        width: 100%; min-width: 0; height: auto; min-height: 1;
        border: none; background: $surface; color: $text;
        padding: 0 1; content-align: left middle; text-style: none;
    }
    ToolCard > .tool-title:focus { background: $panel; }
    ToolCard > .tool-content { height: auto; padding: 0 1; }
    ToolCard.-collapsed > .tool-content { display: none; }
    ToolCard .tool-preview { height: auto; color: $text; }
    ToolCard .tool-full-output { width: auto; min-width: 12; height: 3; }
    """

    def __init__(self, record: dict[str, Any]) -> None:
        super().__init__()
        self.record = record
        self._title_text = ""
        self._title_button = Button(Content(""), classes="tool-title")
        self._body_widget = Static(classes="tool-preview", markup=False)
        self._output_button = Button("完整输入 / 输出", classes="tool-full-output")
        self.update_record(record)

    def compose(self) -> ComposeResult:
        yield self._title_button
        with Vertical(classes="tool-content"):
            yield self._body_widget
            yield self._output_button

    def _watch_collapsed(self, collapsed: bool) -> None:
        self.set_class(collapsed, "-collapsed")
        self._refresh_title()

    def _refresh_title(self) -> None:
        self._title_button.label = Content(("+ " if self.collapsed else "- ") + self._title_text)

    def update_record(self, record: dict[str, Any]) -> None:
        previous_status = getattr(self, "_rendered_status", None)
        self.record = record
        status = record.get("status", "running")
        self._rendered_status = status
        self._title_text = bounded_preview(
            f"{status} · {record.get('actor', 'master')} · {record.get('tool', 'tool')}"
            f"  {tool_summary(record.get('tool', ''), record.get('input', ''))}",
            limit=512, lines=4,
        )
        self.set_class(status == "error", "tool-error")
        if status == "error" and previous_status != "error":
            self.collapsed = False
        self.set_class(self.collapsed, "-collapsed")
        self._refresh_title()
        body = bounded_preview(record.get("input"), 800, 10)
        if "output" in record:
            body += "\n------\n" + bounded_preview(record["output"])
        self._body_widget.update(Content(body))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button not in (self._title_button, self._output_button):
            return
        event.stop()
        if not self.is_attached or self.app.screen is not self.screen:
            return
        if event.button is self._title_button:
            self.collapsed = not self.collapsed
        else:
            self.app.push_screen(ToolOutputScreen(
                f"{self.record.get('actor')} · {self.record.get('tool')}",
                self.record.get("output", ""), literal_text(self.record.get("input")),
            ))


class MessageBubble(Static):
    """Use the same Markdown renderer for assistant deltas and final answers."""

    DEFAULT_CSS = """
    MessageBubble { height: auto; width: 100%; margin-bottom: 1;
        padding: 0 1; border-left: solid $panel; color: $text; }
    MessageBubble.user-message { background: $surface; border-left: solid $secondary; }
    MessageBubble.agent-message { border-left: solid $primary; }
    MessageBubble.system-message { color: $text-muted; }
    MessageBubble.error-message { border-left: solid $error; background: $surface; }
    MessageBubble.approval-message { border-left: solid $primary; background: $surface; }
    MessageBubble.approval-resolved { border-left: solid $panel; }
    """

    def __init__(self, record: dict[str, Any]) -> None:
        super().__init__()
        self.update_record(record)

    def update_record(self, record: dict[str, Any]) -> None:
        kind = literal_text(record.get("kind") or "system")
        role = literal_text(record.get("role") or kind)
        actor = literal_text(record.get("actor") or "master")
        self.set_classes({"user": "user-message", "assistant": "agent-message",
                          "approval": "approval-message", "error": "error-message"}.get(role, "system-message"))
        if kind == "approval":
            status = literal_text(record.get("status") or "pending")
            marker = {"pending": "待审批", "approved": "已批准", "denied": "已拒绝"}.get(status, status)
            self.set_class(status != "pending", "approval-resolved")
            data = record.get("data") if isinstance(record.get("data"), dict) else {}
            text = (f"{data.get('operation', '')} -> {data.get('target') or '(local)'}\n"
                    f"{data.get('risk_level', '')} · {actor} · {data.get('request_id', '')}")
            if status == "pending":
                text += "\n请在审批窗口响应"
                if data.get("requires_confirmation_phrase"):
                    text += " · I CONFIRM DESTRUCTIVE ACTION"
        elif kind == "worker":
            data = record.get("data") if isinstance(record.get("data"), dict) else {}
            status = literal_text(record.get("status") or data.get("status") or "queued")
            marker = "协作 · " + {
                "queued": "排队中", "running": "执行中", "done": "已完成",
                "error": "失败", "cancelled": "已取消",
            }.get(status, status)
            text = f"{data.get('role') or data.get('type') or 'worker'} → {data.get('target') or '—'}"
            if data.get("task"):
                text += "\n" + bounded_preview(str(data["task"]), limit=240, lines=2)
            outcome = data.get("error") or data.get("text")
            if outcome:
                text += "\n" + bounded_preview(str(outcome), limit=320, lines=2)
        else:
            marker = {"user": "你", "assistant": "Agent", "system": "系统", "error": "错误"}.get(role, role)
            text = literal_text(record.get("text"))
        text = bounded_preview(text, limit=20000, lines=300)
        if actor != "master":
            marker += " · " + actor
        heading = Text(marker, style="bold")
        if role == "assistant" and text.strip():
            self.update(Group(heading, Markdown(text, code_theme="github-dark", hyperlinks=False)))
        else:
            heading.append("\n" + text)
            self.update(heading)


class ChatPanel(VerticalScroll):
    """Render at most WINDOW_SIZE records; preserve a reader's frozen old page."""

    WINDOW_SIZE = 80
    DEFAULT_CSS = """
    ChatPanel { background: $background; padding: 1 1 0 1; }
    ChatPanel > #conversation-body { height: auto; min-height: 100%; }
    ChatPanel #history-navigation { height: auto; }
    ChatPanel #history-navigation Button { width: auto; min-width: 12; }
    """

    class UnreadChanged(Message):
        def __init__(self, count: int) -> None:
            self.count = count
            super().__init__()

    class RecordsReady(Message):
        bubble = False


    def __init__(self, event_bus: EventBus, transcript: TranscriptLog) -> None:
        super().__init__()
        self.event_bus = event_bus
        self.transcript = transcript
        self._widgets: dict[str, Widget] = {}
        self._unread: set[str] = set()
        self._tools_collapsed = True
        self._start = 0
        self._end = 0
        self._subscribed = False
        self._rebuilding = False
        self._pending_lock = Lock()
        self._pending_records: set[str] = set()
        self._pending_reset = False
        self._generation = transcript.generation
        self._refresh_queued = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="history-navigation"):
            yield Button("加载更早记录", id="history-older")
            yield Button("较新记录", id="history-newer")
        yield Vertical(id="conversation-body")

    async def on_mount(self) -> None:
        self.anchor()
        self._subscribed = True
        self.transcript.subscribe(self._queue_record)
        await self._show_window(max(0, self.transcript.record_count - self.WINDOW_SIZE))

    def on_unmount(self) -> None:
        with self._pending_lock:
            self._subscribed = False
            self._pending_records.clear()
            self._pending_reset = False
            self._refresh_queued = False
        self.transcript.unsubscribe(self._queue_record)

    def _queue_record(self, record_id: str | None) -> None:
        # Coalesce token bursts by record, rather than queueing one UI message per token.
        with self._pending_lock:
            if not self._subscribed:
                return
            if record_id is None:
                self._pending_reset = True
                self._pending_records.clear()
            else:
                self._pending_records.add(record_id)
        self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        with self._pending_lock:
            if (not self._subscribed or self._refresh_queued
                    or not (self._pending_reset or self._pending_records)):
                return
            self._refresh_queued = True
        self.post_message(self.RecordsReady())

    async def on_chat_panel_records_ready(self, message: RecordsReady) -> None:
        message.stop()
        try:
            await self._flush_pending()
        finally:
            with self._pending_lock:
                self._refresh_queued = False
            self._schedule_refresh()

    def _view_is_current(self, generation: int) -> bool:
        if not self._subscribed or not self.is_attached:
            return False
        if generation != self.transcript.generation:
            self._queue_record(None)
            return False
        return True

    def _make_widget(self, record: dict[str, Any]) -> Widget:
        if record["kind"] == "tool":
            widget = ToolCard(record)
            if record.get("status") != "error":
                widget.collapsed = self._tools_collapsed
            return widget
        return MessageBubble(record)

    async def _show_window(self, start: int) -> bool:
        generation = self.transcript.generation
        if not self._view_is_current(generation):
            return False
        self._rebuilding = True
        try:
            start = max(0, min(start, self.transcript.record_count))
            records = self.transcript.window(start, self.WINDOW_SIZE)
            body = self.query_one("#conversation-body", Vertical)
            await body.remove_children()
            if not self._view_is_current(generation):
                return False
            self._widgets.clear()
            widgets = []
            if not records:
                widgets.append(BannerBubble())
            for record in records:
                widget = self._make_widget(record)
                self._widgets[record["id"]] = widget
                widgets.append(widget)
            if widgets:
                await body.mount(*widgets)
            if not self._view_is_current(generation):
                return False
            self._start, self._end = start, start + len(records)
            self._generation = generation
            self._update_navigation()
            return True
        finally:
            self._rebuilding = False

    def _update_navigation(self) -> None:
        if not self._subscribed or not self.is_attached:
            return
        count = self.transcript.record_count
        self.query_one("#history-older", Button).disabled = self._start == 0
        self.query_one("#history-newer", Button).disabled = self._end >= count
        self.query_one("#history-navigation").display = count > self.WINDOW_SIZE

    async def _flush_pending(self) -> None:
        if not self._subscribed or not self.is_attached:
            return
        with self._pending_lock:
            pending, self._pending_records = self._pending_records, set()
            reset, self._pending_reset = self._pending_reset, False
        generation = self.transcript.generation
        if reset or generation != self._generation:
            self._clear_unread()
            if await self._show_window(max(0, self.transcript.record_count - self.WINDOW_SIZE)):
                self.call_after_refresh(self.scroll_end, animate=False)
            return
        if not pending:
            return
        following = not self._anchor_released
        if not following:
            previous = len(self._unread)
            self._unread.update(pending)
            if len(self._unread) != previous:
                self.post_message(self.UnreadChanged(len(self._unread)))
        count = self.transcript.record_count
        if self._end >= count or (not following and len(self._widgets) >= self.WINDOW_SIZE):
            # Stream updates touch only visible dirty records, never copy the full log.
            for record_id, widget in self._widgets.items():
                if record_id in pending:
                    record = self.transcript.get(record_id)
                    if record is not None and self._view_is_current(generation):
                        widget.update_record(record)
            self._update_navigation()
            return
        start = max(0, count - self.WINDOW_SIZE) if following else self._start
        records = self.transcript.window(start, self.WINDOW_SIZE)
        wanted = {record["id"] for record in records}
        self._rebuilding = True
        try:
            body = self.query_one("#conversation-body", Vertical)
            obsolete = [widget for record_id, widget in self._widgets.items() if record_id not in wanted]
            obsolete.extend(body.query(BannerBubble))
            if obsolete:
                await body.remove_children(obsolete)
            if not self._view_is_current(generation):
                return
            self._widgets = {key: value for key, value in self._widgets.items() if key in wanted}
            new_widgets = []
            for record in records:
                widget = self._widgets.get(record["id"])
                if widget is None:
                    widget = self._make_widget(record)
                    self._widgets[record["id"]] = widget
                    new_widgets.append(widget)
                elif record["id"] in pending:
                    widget.update_record(record)
            if new_widgets:
                await body.mount(*new_widgets)
            if not self._view_is_current(generation):
                return
            self._start, self._end = start, start + len(records)
            self._update_navigation()
        finally:
            self._rebuilding = False

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if (self._subscribed and not self._rebuilding and new_value >= self.max_scroll_y
                and self._end >= self.transcript.record_count):
            self._clear_unread()

    def _clear_unread(self) -> None:
        if self._unread:
            self._unread.clear()
            self.post_message(self.UnreadChanged(0))

    def jump_latest(self) -> None:
        """Explicitly restore bottom following without stealing focus."""
        if self._subscribed and self.is_attached:
            self.call_next(self._jump_latest)

    async def _jump_latest(self) -> None:
        if not self._subscribed or not self.is_attached:
            return
        start = max(0, self.transcript.record_count - self.WINDOW_SIZE)
        if (self._start != start or self._end < self.transcript.record_count
                or self._generation != self.transcript.generation):
            if not await self._show_window(start):
                return
        self._clear_unread()
        self.anchor()
        self.scroll_end(animate=False)
        self.call_after_refresh(self.scroll_end, animate=False)

    def set_tools_collapsed(self, collapsed: bool) -> None:
        self._tools_collapsed = collapsed
        for card in self.query(ToolCard):
            card.collapsed = collapsed

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id not in {"history-older", "history-newer"}:
            return
        if not self._subscribed or not self.is_attached:
            return
        event.stop()
        start = max(0, self._start - self.WINDOW_SIZE) if event.button.id == "history-older" else self._end
        if not await self._show_window(start):
            return
        self.scroll_home(animate=False)
        self.release_anchor()
