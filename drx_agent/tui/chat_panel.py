"""Conversation cards, lossless tool output and anchored streaming."""

import json
from typing import Any

from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text
from rich.theme import Theme
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Collapsible, Static

from drx_agent.event_bus import Event, EventBus, EventType
from drx_agent.tui.banner import build_banner


class BannerBubble(Static):
    """Compact startup identity and next step."""

    DEFAULT_CSS = """
    BannerBubble {
        height: auto;
        width: 100%;
        margin-bottom: 1;
        padding: 1;
        border-left: solid #26354b;
        color: #92a4bb;
    }
    """

    def __init__(self) -> None:
        super().__init__(build_banner())


class DiffBubble(Static):
    """Literal unified diff with restrained, high-contrast line colors."""

    DEFAULT_CSS = """
    DiffBubble {
        height: auto;
        width: 100%;
        margin-bottom: 1;
        padding: 0 1;
        border-left: solid #26354b;
        background: #111a2c;
    }
    """

    def __init__(self, title: str, diff_text: str) -> None:
        super().__init__()
        self._title = title
        self._diff = diff_text
        self._refresh()

    def _refresh(self) -> None:
        rendered = Text("变更 ", style="bold #53d7c3")
        rendered.append(self._title + "\n")
        if not self._diff.strip():
            rendered.append("无文本变更", style="#92a4bb")
        for line in self._diff.splitlines():
            if line.startswith(("+++", "---")):
                style = "#92a4bb"
            elif line.startswith("@@"):
                style = "bold #53d7c3"
            elif line.startswith("+"):
                style = "#53d7c3"
            elif line.startswith("-"):
                style = "#ff7f8a"
            else:
                style = "#e7edf7"
            rendered.append(line + "\n", style=style)
        self.update(rendered)


class TodoBubble(Static):
    """A literal snapshot of the current task list."""

    DEFAULT_CSS = """
    TodoBubble {
        height: auto;
        width: 100%;
        margin-bottom: 1;
        padding: 0 1;
        border-left: solid #26354b;
    }
    """

    _STATUS_GLYPH = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
    _STATUS_STYLE = {
        "pending": "#92a4bb", "in_progress": "#ffc36a", "completed": "#53d7c3",
    }

    def __init__(self, todos: list[dict[str, Any]]) -> None:
        super().__init__()
        self._todos = todos or []
        self._refresh()

    def _refresh(self) -> None:
        rendered = Text(f"任务 ({len(self._todos)})\n", style="bold #53d7c3")
        if not self._todos:
            rendered.append("暂无任务", style="#92a4bb")
        for todo in self._todos:
            status = todo.get("status", "pending")
            rendered.append(
                self._STATUS_GLYPH.get(status, "[ ]") + " ",
                style=self._STATUS_STYLE.get(status, "#92a4bb"),
            )
            rendered.append(
                str(todo.get("content", "")) + "\n",
                style="strike #92a4bb" if status == "completed" else "#e7edf7",
            )
        self.update(rendered)


class ToolOutputScreen(ModalScreen[None]):
    """Scrollable original tool output; clipboard never receives the preview."""

    BINDINGS = [Binding("escape", "close", "关闭", show=False)]
    DEFAULT_CSS = """
    ToolOutputScreen { align: center middle; background: #0b1020 80%; }
    ToolOutputScreen > #tool-output-dialog {
        width: 92%; max-width: 110; height: 90%;
        background: #111a2c; border: solid #26354b; padding: 0 1;
    }
    ToolOutputScreen #tool-output-title {
        height: auto; max-height: 3; color: #53d7c3; text-style: bold;
    }
    ToolOutputScreen #tool-output-scroll { height: 1fr; }
    ToolOutputScreen #tool-output-text { height: auto; color: #e7edf7; }
    ToolOutputScreen #tool-output-actions { height: 3; align-horizontal: right; }
    ToolOutputScreen Button { min-width: 8; width: auto; margin-left: 1; }
    """

    def __init__(self, tool_name: str, output: str) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.output_text = output

    def compose(self) -> ComposeResult:
        with Vertical(id="tool-output-dialog"):
            yield Static(Text(f"完整输出 · {self.tool_name}"), id="tool-output-title")
            with VerticalScroll(id="tool-output-scroll"):
                yield Static(Text(self.output_text), id="tool-output-text")
            with Horizontal(id="tool-output-actions"):
                yield Button("复制原文", id="tool-output-copy", variant="primary")
                yield Button("关闭", id="tool-output-close")

    def on_mount(self) -> None:
        self.query_one("#tool-output-scroll", VerticalScroll).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "tool-output-copy":
            copy_text = getattr(self.app, "_copy_text", None)
            if callable(copy_text):
                copy_text(self.output_text, "复制完整输出")
            else:
                self.app.copy_to_clipboard(self.output_text)
                self.notify("完整输出已复制")
        elif event.button.id == "tool-output-close":
            self.action_close()

    def action_close(self) -> None:
        self.dismiss(None)


class ToolCard(Collapsible):
    """Foldable tool result with a bounded preview and lossless detail view."""

    DEFAULT_CSS = """
    ToolCard {
        height: auto; width: 100%; margin-bottom: 1;
        background: #111a2c; border-top: none;
        border-left: solid #26354b; padding: 0;
    }
    ToolCard.tool-error { border-left: solid #ff7f8a; }
    ToolCard > CollapsibleTitle { background: #17243a; padding: 0 1; }
    ToolCard > CollapsibleTitle:focus { background: #26354b; color: #e7edf7; }
    ToolCard Contents { padding: 0 1; }
    ToolCard .tool-preview { height: auto; }
    ToolCard .tool-full-output { width: auto; min-width: 12; height: 3; }
    """

    _STATUS_LABEL = {
        "running": "运行中", "done": "完成", "error": "失败", "pending": "待运行",
    }
    _STATUS_STYLE = {
        "running": "#ffc36a", "done": "#53d7c3",
        "error": "#ff7f8a", "pending": "#92a4bb",
    }

    def __init__(self, tool_name: str, preview: str) -> None:
        self._tool_name = str(tool_name)
        self._preview = preview or ""
        self._status = "running"
        self._output = ""
        self._body_widget = Static(Text(self._preview[:600]), classes="tool-preview")
        self._output_button = Button("完整输出", classes="tool-full-output", disabled=True)
        super().__init__(
            self._body_widget, self._output_button,
            title=self._format_title(), collapsed=True,
            collapsed_symbol="+", expanded_symbol="-",
        )

    def _format_title(self) -> str:
        title = Text(
            self._STATUS_LABEL.get(self._status, self._status) + " ",
            style=self._STATUS_STYLE.get(self._status, "#92a4bb"),
        )
        title.append(self._tool_name, style="bold #e7edf7")
        line = self._preview.split("\n", 1)[0]
        title.append("  " + line[:80] + ("…" if len(line) > 80 else ""), style="#92a4bb")
        return title.markup

    def _watch_collapsed(self, collapsed: bool) -> None:
        # Collapsible's default watcher scrolls the card into view. An automatic
        # error expansion must never steal the reader's viewport from older text.
        self._update_collapsed(collapsed)
        self.post_message(self.Collapsed(self) if collapsed else self.Expanded(self))

    def update_status(self, status: str) -> None:
        if status:
            self._status = status
            self.title = self._format_title()
            self.set_class(status == "error", "tool-error")
            if status == "error":
                self.collapsed = False

    def apply_result(self, output: str, status: str = "done") -> None:
        self._output = str(output)
        self.update_status(status)
        body = Text(self._preview[:600] + "\n", style="#92a4bb")
        body.append("------\n", style="#26354b")
        body.append(self._render_output(self._output, status))
        self._body_widget.update(body)
        self._output_button.disabled = False

    def _render_output(self, output: str, status: str) -> Text:
        if not output:
            return Text(f"无输出 · {status}", style="#92a4bb")
        text = output
        # Formatting is cosmetic; the original bytes remain in _output.
        if len(output) <= 4000:
            try:
                text = json.dumps(json.loads(output), ensure_ascii=False, indent=2)
            except (json.JSONDecodeError, TypeError):
                pass
        lines = text[:4001].splitlines()
        body = "\n".join(lines[:80])[:4000]
        truncated = len(text) > 4000 or len(lines) > 80
        rendered = Text(body, style="#ff7f8a" if status == "error" else "#e7edf7")
        if truncated:
            rendered.append("\n预览已折叠；选择「完整输出」查看和复制原文。", style="#92a4bb")
        return rendered

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button is self._output_button:
            event.stop()
            self.app.push_screen(ToolOutputScreen(self._tool_name, self._output))


class StreamingCursor(Static):
    """Small streaming indicator, without focus or scrolling side effects."""

    DEFAULT_CSS = """
    StreamingCursor { height: 1; width: 3; margin-bottom: 1; color: #53d7c3; }
    """

    def __init__(self) -> None:
        super().__init__(Text("|"))
        self._on = True

    def on_mount(self) -> None:
        self.set_interval(0.5, self._blink)

    def _blink(self) -> None:
        self._on = not self._on
        self.update(Text("|" if self._on else " "))


_MARKDOWN_THEME = Theme({
    **{f"markdown.h{level}": "bold #53d7c3" for level in range(1, 7)},
    "markdown.code": "#e7edf7 on #17243a",
    "markdown.code_block": "#e7edf7 on #17243a",
    "markdown.block_quote": "#92a4bb",
    "markdown.link": "underline #53d7c3",
    "markdown.link_url": "#92a4bb",
    "markdown.hr": "#26354b",
})


class _ConversationMarkdown:
    def __init__(self, text: str) -> None:
        self.markdown = Markdown(text, code_theme="github-dark", hyperlinks=False)

    def __rich_console__(self, console, options):
        # Render recursively inside the theme context; do not leak a global
        # console theme into unrelated widgets or interpret Rich markup.
        with console.use_theme(_MARKDOWN_THEME):
            yield from console.render(self.markdown, options)


class MessageBubble(Static):
    """One literal message, rendered as Markdown only after completion."""

    DEFAULT_CSS = """
    MessageBubble {
        height: auto; width: 100%; margin-bottom: 1;
        padding: 0 1; border-left: solid #26354b; color: #e7edf7;
    }
    MessageBubble.user-message { background: #17243a; border-left: solid #92a4bb; }
    MessageBubble.agent-message { border-left: solid #53d7c3; }
    MessageBubble.system-message { color: #92a4bb; }
    MessageBubble.error-message { border-left: solid #ff7f8a; background: #111a2c; }
    MessageBubble.approval-message { border-left: solid #ffc36a; background: #111a2c; }
    MessageBubble.approval-resolved { border-left: solid #26354b; }
    """

    def __init__(
        self, marker: str = "Agent", marker_style: str = "#53d7c3",
        text: str = "", body_style: str | None = None, markdown: bool = False,
    ) -> None:
        super().__init__()
        self._marker = marker
        self._marker_style = marker_style
        self._body_style = body_style
        self._markdown = markdown
        self._md_ready = False
        self._text = text
        self._refresh_render()

    def append(self, delta: str) -> None:
        if delta:
            self._text += delta
            self._refresh_render()

    def set_text(self, text: str) -> None:
        self._text = text
        self._refresh_render()

    def finalize(self) -> None:
        if self._markdown and not self._md_ready:
            self._md_ready = True
            self._refresh_render()

    def _refresh_render(self) -> None:
        marker = Text(self._marker, style=f"bold {self._marker_style}")
        if self._markdown and self._md_ready and self._text.strip():
            self.update(Group(marker, _ConversationMarkdown(self._text)))
        else:
            marker.append("\n" + self._text, style=self._body_style or "#e7edf7")
            self.update(marker)


class ChatPanel(VerticalScroll):
    """Anchored conversation; scrolling away suspends following until bottom."""

    DEFAULT_CSS = """
    ChatPanel { background: #0b1020; padding: 1 1 0 1; }
    ChatPanel > #conversation-body { height: auto; min-height: 100%; }
    """

    class UnreadChanged(Message):
        def __init__(self, count: int) -> None:
            self.count = count
            super().__init__()

    class BusEvent(Message):
        """Thread-safe event handoff into Textual's message queue."""
        bubble = False

        def __init__(self, event: Event) -> None:
            self.event = event
            super().__init__()

    def __init__(self, event_bus: EventBus):
        super().__init__()
        self.event_bus = event_bus
        self._streams: dict[str, MessageBubble] = {}
        self._open_tools: dict[int, ToolCard] = {}
        self._approvals: dict[str, MessageBubble] = {}
        self._approval_details: dict[str, str] = {}
        self._cursor: StreamingCursor | None = None
        self._active_streams: set[str] = set()
        self._unread: set[Widget] = set()
        self._tools_collapsed = True
        self._subscribed = False
        self._handlers = {
            EventType.AGENT_MESSAGE: self._on_agent_message,
            EventType.TOOL_CALL: self._on_tool_call,
            EventType.TOOL_RESULT: self._on_tool_result,
            EventType.SUB_AGENT_DISPATCH: self._on_sub_dispatch,
            EventType.SUB_AGENT_RESULT: self._on_sub_result,
            EventType.APPROVAL_REQUEST: self._on_approval,
            EventType.APPROVAL_RESOLVED: self._on_approval_resolved,
            EventType.ERROR: self._on_error,
        }

    def compose(self) -> ComposeResult:
        # Native anchoring may use a negative offset for short content. A full
        # viewport body keeps the first messages at the top without overriding
        # Textual's scroll-follow and scrollback behavior.
        yield Vertical(id="conversation-body")

    def on_mount(self) -> None:
        self.anchor()
        self.query_one("#conversation-body", Vertical).mount(BannerBubble())
        for event_type in self._handlers:
            self.event_bus.subscribe(event_type, self._queue_event)
        self._subscribed = True

    def on_unmount(self) -> None:
        self._subscribed = False
        for event_type in self._handlers:
            self.event_bus.unsubscribe(event_type, self._queue_event)

    def _queue_event(self, event: Event) -> None:
        # post_message is thread-safe, unlike mounting/updating a widget. Never
        # fall back to calling UI handlers on the publishing worker's thread.
        if self._subscribed:
            self.post_message(self.BusEvent(event))

    def on_chat_panel_bus_event(self, message: BusEvent) -> None:
        message.stop()
        if self._subscribed:
            self._handlers[message.event.type](message.event)

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if new_value >= self.max_scroll_y and self._unread:
            self._clear_unread()

    def _clear_unread(self) -> None:
        if self._unread:
            self._unread.clear()
            self.post_message(self.UnreadChanged(0))

    def jump_latest(self) -> None:
        """Resume native bottom-following without changing keyboard focus."""
        self._clear_unread()
        self.scroll_end(animate=False)

    def set_tools_collapsed(self, collapsed: bool) -> None:
        """Set current and future tool-card expansion state."""
        self._tools_collapsed = collapsed
        for card in self.query(ToolCard):
            card.collapsed = collapsed

    def _record_activity(self, bubble: Widget) -> None:
        # Textual's anchor handles reflow, queued layouts, mouse/keyboard and
        # scrollbar navigation. Count changed messages, never individual tokens.
        if self._anchor_released and self.max_scroll_y > 0 and bubble not in self._unread:
            self._unread.add(bubble)
            self.post_message(self.UnreadChanged(len(self._unread)))

    def _append(self, bubble: Widget) -> None:
        self._record_activity(bubble)
        self.query_one("#conversation-body", Vertical).mount(bubble)


    def _on_agent_message(self, event: Event) -> None:
        data = event.data
        if data.get("streaming"):
            self._handle_stream(data)
            return
        text = data.get("text") or data.get("content", "")
        if not text:
            return
        is_agent = data.get("role") == "assistant" or data.get("source") == "agent"
        if is_agent:
            marker, style, css = "Agent", "#53d7c3", "agent-message"
        elif data.get("source") == "system" or data.get("role") == "system":
            marker, style, css = "系统", "#92a4bb", "system-message"
        else:
            marker, style, css = "你", "#92a4bb", "user-message"
        bubble = MessageBubble(marker, style, str(text), markdown=is_agent)
        bubble.add_class(css)
        if is_agent:
            bubble.finalize()
        self._append(bubble)

    def _ensure_cursor(self) -> None:
        if self._cursor is None:
            self._cursor = StreamingCursor()
            self.query_one("#conversation-body", Vertical).mount(self._cursor)

    def _remove_cursor(self) -> None:
        if self._cursor is not None:
            self._cursor.remove()
            self._cursor = None

    def _handle_stream(self, data: dict[str, Any]) -> None:
        sid = data.get("stream_id")
        if not sid:
            return
        bubble = self._streams.get(sid)
        if data.get("final"):
            self._active_streams.discard(sid)
            self._streams.pop(sid, None)
            full = data.get("text")
            if full is None:
                full = data.get("content")
            if bubble is None and full:
                bubble = MessageBubble(text=str(full), markdown=True)
                bubble.add_class("agent-message")
                self._append(bubble)
            if bubble is not None:
                if full is not None and str(full) != bubble._text:
                    bubble.set_text(str(full))
                bubble.finalize()
                self._record_activity(bubble)
            if not self._active_streams:
                self._remove_cursor()
            return
        delta = data.get("delta", "")
        if not delta:
            return
        if bubble is None:
            bubble = MessageBubble(markdown=True)
            bubble.add_class("agent-message")
            self._streams[sid] = bubble
            self._append(bubble)
        bubble.append(str(delta))
        self._record_activity(bubble)
        self._active_streams.add(sid)
        self._ensure_cursor()

    def _on_tool_call(self, event: Event) -> None:
        data = event.data
        tool_name = data.get("tool") or (
            f"execute_{data.get('language', 'script')}_script#{data.get('script_num', '?')}"
        )
        card = ToolCard(tool_name, str(data.get("code", "")))
        card.collapsed = self._tools_collapsed
        card.update_status(data.get("status", "running"))
        call_seq = data.get("call_seq")
        if call_seq is not None:
            self._open_tools[call_seq] = card
        self._append(card)

    _DIFF_TOOLS = {"write_file", "edit_file", "multi_edit_file"}

    def _on_tool_result(self, event: Event) -> None:
        data = event.data
        tool = str(data.get("tool", "工具"))
        output = data.get("output", "")
        status = data.get("status", "done")
        if not output:
            output = str(data.get("stdout", ""))
            stderr = str(data.get("stderr", ""))
            if stderr:
                output += ("\n" if output else "") + stderr
        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=False)
        call_seq = data.get("call_seq")
        card = self._open_tools.pop(call_seq, None) if call_seq is not None else None
        if card is None:
            card = ToolCard(tool, "")
            card.collapsed = self._tools_collapsed
            self._append(card)
        card.apply_result(output, status)
        self._record_activity(card)
        if tool in self._DIFF_TOOLS and output and status != "error":
            try:
                parsed = json.loads(output)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, dict) and not parsed.get("error"):
                self._append(DiffBubble(
                    title=str(parsed.get("summary") or parsed.get("path") or tool),
                    diff_text=str(parsed.get("diff") or ""),
                ))

    def _on_sub_dispatch(self, event: Event) -> None:
        data = event.data
        bubble = MessageBubble(
            "协作", "#53d7c3",
            f"{data.get('type', '?')} -> {data.get('target', '?')}",
        )
        bubble.add_class("system-message")
        self._append(bubble)

    def _on_sub_result(self, event: Event) -> None:
        bubble = MessageBubble(
            "协作", "#92a4bb", str(event.data.get("status", "")), "#92a4bb",
        )
        bubble.add_class("system-message")
        self._append(bubble)

    def _on_approval(self, event: Event) -> None:
        data = event.data
        request_id = str(data.get("request_id", ""))
        detail = (
            f"[{data.get('risk_level', 'L2')}] {request_id[:8]} · {data.get('agent_id', 'master')}\n"
            f"{data.get('operation', '')} -> {data.get('target') or '(local)'}"
        )
        choices = (
            "输入 I CONFIRM DESTRUCTIVE ACTION 批准；[n]拒绝 [v]详情"
            if data.get("requires_confirmation_phrase") else "[y]批准 [n]拒绝 [v]详情"
        )
        bubble = self._approvals.get(request_id) if request_id else None
        if bubble is None:
            bubble = MessageBubble("待审批", "#ffc36a", detail + "\n" + choices)
            bubble.add_class("approval-message")
            self._append(bubble)
        else:
            bubble.set_text(detail + "\n" + choices)
            self._record_activity(bubble)
        if request_id:
            self._approvals[request_id] = bubble
            self._approval_details[request_id] = detail

    def _on_approval_resolved(self, event: Event) -> None:
        request_id = str(event.data.get("request_id", ""))
        bubble = self._approvals.pop(request_id, None)
        detail = self._approval_details.pop(request_id, "")
        if bubble is None:
            return
        approved = event.data.get("approved") is True
        bubble._marker = "已批准" if approved else "已拒绝"
        bubble._marker_style = "#53d7c3" if approved else "#ff7f8a"
        bubble.add_class("approval-resolved")
        bubble.set_text(detail)
        self._record_activity(bubble)

    def _on_error(self, event: Event) -> None:
        bubble = MessageBubble(
            "错误", "#ff7f8a", str(event.data.get("message", "")), "#ff7f8a",
        )
        bubble.add_class("error-message")
        self._append(bubble)
