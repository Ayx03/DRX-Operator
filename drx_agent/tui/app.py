"""DRX-Operator main TUI application — thin presentation shell over EventBus"""

import logging
import platform
import subprocess

from textual.app import App, ComposeResult, SkipAction
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.theme import Theme
from textual.widgets import Button, Static

from drx_agent.event_bus import EventBus, EventType, Event
from drx_agent.tui.chat_panel import ChatPanel
from drx_agent.tui.sidebar import Sidebar
from drx_agent.tui.composer import Composer
from drx_agent.tui.footer import StatusFooter
from drx_agent.tui.ring import RingIndicator
from drx_agent.tui.transcript_screen import TranscriptScreen
from drx_agent.tui.command_palette import CommandPalette

logger = logging.getLogger(__name__)

_CLIPBOARD_COMMANDS = {
    "Darwin": ["pbcopy"],
    "Linux": ["xclip", "-selection", "clipboard"],
}


def _copy_to_system_clipboard(text: str, _run=None) -> bool:
    """Write text to the OS clipboard via a native tool.

    Textual's built-in copy uses OSC 52, which macOS Terminal.app does not
    support — so we fall back to pbcopy / xclip for terminals without it.
    """
    run = _run or subprocess.run
    command = _CLIPBOARD_COMMANDS.get(platform.system())
    if command is None:
        return False
    try:
        run(command, input=text, text=True, check=True, timeout=5)
        return True
    except Exception:
        return False


class DrxAgentApp(App[None]):
    """DRX-Operator main TUI application"""

    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen {
        background: $background;
        color: $text;
        scrollbar-size: 1 1;
    }
    #workspace-header {
        height: 3;
        padding: 0 1;
        align-vertical: middle;
        background: $surface;
        border-bottom: solid #26354b;
    }
    #workspace-brand {
        width: 1fr;
        height: 1;
        text-style: bold;
        color: $primary;
    }
    #workspace-context {
        width: auto;
        height: 1;
        margin-right: 2;
        color: $text-muted;
    }
    #workspace-header Button, #conversation-header Button {
        width: auto;
        min-width: 6;
        height: 1;
        min-height: 1;
        margin-left: 1;
        padding: 0 1;
        border: none;
        background: $panel;
        color: $text;
    }
    #workspace-header Button:hover, #conversation-header Button:hover {
        background: $primary 20%;
        color: $primary;
    }
    #workspace-header Button:focus, #conversation-header Button:focus {
        text-style: bold;
        background: $primary;
        color: $background;
    }
    #workspace-header #stop-task {
        color: $error;
    }
    #main-container {
        layout: horizontal;
        height: 1fr;
    }
    #chat-container {
        width: 1fr;
        height: 1fr;
    }
    #conversation-header {
        height: 2;
        padding: 0 1;
        border-bottom: solid #26354b;
    }
    #conversation-label {
        width: 1fr;
        height: 1;
        color: $text-muted;
    }
    #jump-latest.has-unread {
        color: $primary;
        text-style: bold;
    }
    ChatPanel {
        height: 1fr;
    }
    Sidebar {
        width: 32;
        height: 1fr;
        border-left: solid #26354b;
    }
    Composer {
        height: 3;
        margin: 0 1;
        padding: 0 1;
        background: $surface;
        border: round #26354b;
    }
    Composer:focus {
        border: round $primary;
    }
    #input-hints {
        height: 1;
        padding: 0 2;
        color: $text-muted;
    }
    #footer-row {
        height: 1;
        padding: 0 1;
        background: $surface;
    }
    #footer-row StatusFooter {
        width: 1fr;
        height: 1;
        border: none;
        padding: 0;
    }
    Screen.compact #workspace-context {
        display: none;
    }
    Screen.compact Sidebar {
        width: 1fr;
        border-left: none;
    }
    Screen.short #workspace-header {
        height: 2;
    }
    Screen.short #conversation-header {
        height: 1;
        border-bottom: none;
    }
    Screen.short #input-hints {
        display: none;
    }
    Screen.tiny #fold-tools {
        display: none;
    }
    """

    BINDINGS = [
        Binding("ctrl+s", "interrupt", "停止任务", show=False, priority=True),
        Binding("ctrl+k,f1", "open_commands", "命令", show=False, priority=True),
        Binding("ctrl+t", "toggle_transcript", "会话记录", show=False, priority=True),
        Binding("ctrl+b", "toggle_sidebar", "工作台", show=False, priority=True),
        Binding("ctrl+l", "jump_latest", "回到最新", show=False, priority=True),
        Binding("f2", "show_metrics", "运行指标", show=False, priority=True),
        Binding("super+c,ctrl+shift+c", "copy_selection", "Copy selected text", show=False),
        Binding("ctrl+shift+a", "copy_last_message", "Copy last reply", show=False),
        Binding("ctrl+shift+t", "copy_transcript", "Copy transcript", show=False),
    ]

    def __init__(self, event_bus: EventBus, drx_agent=None):
        super().__init__()
        self.event_bus = event_bus
        self.drx_agent = drx_agent
        self.title = "DRX-Operator"
        self._main_screen = None
        self._sidebar_preference: bool | None = None
        self._tools_collapsed = True
        self.register_theme(Theme(
            name="drx",
            primary="#53d7c3",
            secondary="#92a4bb",
            accent="#53d7c3",
            foreground="#e7edf7",
            background="#0b1020",
            surface="#111a2c",
            panel="#17243a",
            warning="#ffc36a",
            error="#ff7f8a",
            success="#53d7c3",
            variables={"text-muted": "#92a4bb", "border": "#26354b"},
        ))
        self.theme = "drx"

    def _transcript_texts(self) -> list[str]:
        master = getattr(self.drx_agent, "master", None)
        messages = getattr(master, "messages", None) or []
        texts: list[str] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "")
            content = msg.get("content")
            if isinstance(content, str):
                body = content
            elif isinstance(content, list):
                body = "\n".join(
                    str(b.get("text", ""))
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                body = ""
            if body:
                texts.append(f"[{role}] {body}")
        return texts

    def action_interrupt(self) -> None:
        self.event_bus.publish(Event(
            type=EventType.AGENT_MESSAGE,
            data={"text": "/stop", "source": "user"},
        ))

    def action_toggle_transcript(self) -> None:
        if isinstance(self.screen, TranscriptScreen):
            self.pop_screen()
        else:
            self.push_screen("transcript_view")

    def action_open_commands(self) -> None:
        if not isinstance(self.screen, CommandPalette):
            self.push_screen("command_palette", self._receive_command_draft)

    def _receive_command_draft(self, command: str | None) -> None:
        if command is None or self._main_screen is None:
            return
        composer = self._main_screen.query_one(Composer)
        if composer.set_command_draft(command):
            self._main_screen.set_focus(composer, scroll_visible=False)

    def action_toggle_sidebar(self) -> None:
        if self._main_screen is None or self.screen is not self._main_screen:
            return
        sidebar = self._main_screen.query_one(Sidebar)
        self._sidebar_preference = not sidebar.display
        self._apply_layout()
        if sidebar.display:
            sidebar.focus()
        else:
            self._main_screen.query_one(Composer).focus()

    def action_jump_latest(self) -> None:
        if self._main_screen is not None and self.screen is self._main_screen:
            self._main_screen.query_one(ChatPanel).jump_latest()

    def action_show_metrics(self) -> None:
        if self._main_screen is not None and self.screen is self._main_screen:
            self._main_screen.query_one(RingIndicator).action_show_metrics()

    def on_chat_panel_unread_changed(self, message: ChatPanel.UnreadChanged) -> None:
        if self._main_screen is None:
            return
        button = self._main_screen.query_one("#jump-latest", Button)
        button.label = f"最新 +{message.count}" if message.count else "回到最新"
        button.set_class(message.count > 0, "has-unread")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "open-commands": self.action_open_commands,
            "open-transcript": self.action_toggle_transcript,
            "toggle-sidebar": self.action_toggle_sidebar,
            "stop-task": self.action_interrupt,
            "jump-latest": self.action_jump_latest,
            "fold-tools": self._toggle_tools,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            event.stop()
            action()

    def _toggle_tools(self) -> None:
        if self._main_screen is None:
            return
        self._tools_collapsed = not self._tools_collapsed
        self._main_screen.query_one(ChatPanel).set_tools_collapsed(self._tools_collapsed)
        self._main_screen.query_one("#fold-tools", Button).label = (
            "展开工具" if self._tools_collapsed else "收起工具"
        )

    def on_resize(self) -> None:
        self.call_after_refresh(self._apply_layout)

    def _apply_layout(self) -> None:
        if self._main_screen is None:
            return
        width, height = self.size
        compact = width < 100
        sidebar_visible = (
            not compact if self._sidebar_preference is None else self._sidebar_preference
        )
        sidebar = self._main_screen.query_one(Sidebar)
        conversation = self._main_screen.query_one("#chat-container")
        conversation_visible = not (compact and sidebar_visible)
        focus_will_hide = (
            (not sidebar_visible and sidebar.has_focus_within)
            or (not conversation_visible and conversation.has_focus_within)
        )
        self._main_screen.set_class(compact, "compact")
        self._main_screen.set_class(height <= 24, "short")
        self._main_screen.set_class(width < 70, "tiny")
        sidebar.display = sidebar_visible
        conversation.display = conversation_visible
        if focus_will_hide:
            self._main_screen.set_focus(
                sidebar if not conversation_visible else self._main_screen.query_one(Composer),
                scroll_visible=False,
            )
        self._main_screen.query_one("#workspace-brand", Static).update(
            "DRX" if width < 70 else "DRX / OPERATOR"
        )
        self._main_screen.query_one("#toggle-sidebar", Button).label = (
            "回对话" if compact and sidebar_visible else "工作台"
        )
        hints = (
            "Enter 发送 · ↑↓ 历史 · Ctrl K 命令 · Ctrl B 工作台 · Ctrl S 停止"
            if width >= 100 else "Enter 发送 · ↑↓ 历史 · Ctrl K 命令 · Ctrl B 工作台"
        )
        self._main_screen.query_one("#input-hints", Static).update(hints)

    def on_click(self, event) -> None:
        if self._main_screen is None or self.screen is not self._main_screen:
            return
        widget = event.widget
        while widget is not None and widget is not self._main_screen:
            if widget.can_focus:
                return
            widget = widget.parent
        self._main_screen.query_one(Composer).focus(scroll_visible=False)

    def on_text_selected(self, event) -> None:
        if self.screen is not self._main_screen:
            return
        from textual.widgets import Input

        selections = getattr(self.screen, "selections", {}) or {}
        if any(isinstance(w, Input) for w, sel in selections.items() if sel):
            return
        try:
            text = self.screen.get_selected_text()
        except Exception:
            return
        if text:
            self._copy_text(text, "复制")

    def _copy_text(self, text: str, title: str) -> None:
        self.copy_to_clipboard(text)
        if _copy_to_system_clipboard(text):
            self.notify(f"已复制 {len(text)} 字符", title=title)
        else:
            self.notify(
                "未找到系统剪贴板工具（macOS 需 pbcopy，Linux 需 xclip）",
                title="复制",
                severity="warning",
            )

    def action_copy_selection(self) -> None:
        try:
            text = self.screen.get_selected_text()
        except Exception:
            text = None
        if not text:
            raise SkipAction()
        self._copy_text(text, "复制")

    def action_copy_last_message(self) -> None:
        texts = self._transcript_texts()
        assistant = [t for t in texts if t.startswith("[assistant]")]
        if not assistant:
            self.notify("还没有 Agent 回复可复制", title="复制")
            return
        self._copy_text(assistant[-1][len("[assistant] "):], "复制最近回复")

    def action_copy_transcript(self) -> None:
        texts = self._transcript_texts()
        if not texts:
            self.notify("会话记录为空", title="复制")
            return
        self._copy_text("\n".join(texts), "复制完整记录")

    def compose(self) -> ComposeResult:
        with Horizontal(id="workspace-header"):
            yield Static("DRX / OPERATOR", id="workspace-brand", markup=False)
            yield Static("安全研究工作区", id="workspace-context", markup=False)
            yield Button("命令", id="open-commands", tooltip="Ctrl K / F1 · 搜索命令")
            yield Button("记录", id="open-transcript", tooltip="Ctrl T · 检索会话")
            yield Button("工作台", id="toggle-sidebar", tooltip="Ctrl B · 任务与 Agent")
            yield Button("停止", id="stop-task", tooltip="Ctrl S · 停止当前任务")
        with Container(id="main-container"):
            with Container(id="chat-container"):
                with Horizontal(id="conversation-header"):
                    yield Static("对话 / CONVERSATION", id="conversation-label", markup=False)
                    yield Button("展开工具", id="fold-tools", tooltip="展开或收起所有工具结果")
                    yield Button("回到最新", id="jump-latest", tooltip="Ctrl L · 恢复跟随输出")
                yield ChatPanel(self.event_bus)
            yield Sidebar(self.event_bus)
        yield Composer(self.event_bus)
        yield Static("", id="input-hints", markup=False)
        with Horizontal(id="footer-row"):
            yield StatusFooter(self.event_bus)
            yield RingIndicator(self.event_bus)

    async def on_mount(self) -> None:
        self._main_screen = self.screen
        self.install_screen(TranscriptScreen(self), name="transcript_view")
        self.install_screen(CommandPalette(self.event_bus), name="command_palette")
        self._apply_layout()
        self.query_one(Composer).border_title = "输入 / COMMAND"
        try:
            self.set_focus(self.query_one(Composer), scroll_visible=False)
        except Exception:
            pass
        self.event_bus.publish(
            Event(type=EventType.STATUS_UPDATE, data={"text": "就绪"})
        )
        if self.drx_agent is not None and hasattr(self.drx_agent, "async_setup"):
            try:
                await self.drx_agent.async_setup()
            except Exception as exc:
                self.event_bus.publish(
                    Event(type=EventType.ERROR, data={"message": f"MCP setup failed: {exc}"})
                )

    async def on_unmount(self) -> None:
        self._auto_save()
        if self.drx_agent is not None and hasattr(self.drx_agent, "async_teardown"):
            try:
                await self.drx_agent.async_teardown()
            except Exception:
                pass

    def _auto_save(self) -> None:
        try:
            self.event_bus.publish(Event(type=EventType.SESSION_SAVE, data={}))
        except Exception:
            logger.exception("Session auto-save failed during shutdown")
