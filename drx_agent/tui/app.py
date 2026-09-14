"""Conversation-first terminal shell; execution stays behind the EventBus."""

import asyncio
import platform
import subprocess
from pathlib import Path
from rich.text import Text

from textual.actions import SkipAction
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.theme import Theme
from textual.widgets import Button, Static, TextArea

from drx_agent.event_bus import EventBus, EventType, Event
from drx_agent.tui.activity import ActivityBar
from drx_agent.tui.chat_panel import ChatPanel
from drx_agent.tui.sidebar import Sidebar
from drx_agent.tui.composer import Composer
from drx_agent.tui.footer import StatusFooter
from drx_agent.tui.ring import RingIndicator
from drx_agent.tui.transcript import TranscriptLog
from drx_agent.tui.transcript_screen import TranscriptScreen
from drx_agent.tui.command_palette import CommandPalette
from drx_agent.session.usage import restore_usage, usage_status

_CLIPBOARD_COMMANDS = {
    "Darwin": ["pbcopy"],
    "Linux": ["xclip", "-selection", "clipboard"],
}


def _copy_to_system_clipboard(text: str, _run=None) -> bool:
    """Use the native clipboard where the terminal cannot handle OSC 52."""
    run = _run or subprocess.run
    command = _CLIPBOARD_COMMANDS.get(platform.system())
    if command is None:
        return False
    try:
        run(command, input=text, text=True, check=True, timeout=5)
        return True
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return False


class DrxAgentApp(App[None]):
    """A quiet transcript, a real editor, and optional inspection surfaces."""

    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen {
        background: $background;
        color: $text;
        scrollbar-size: 1 1;
    }
    #workspace-header {
        height: 1;
        padding: 0 1;
        background: $background;
    }
    #workspace-brand {
        width: auto;
        min-width: 5;
        height: 1;
        text-style: bold;
        color: $primary;
    }
    #workspace-context {
        width: 1fr;
        height: 1;
        color: $text-muted;
    }
    #workspace-header Button {
        width: auto;
        min-width: 4;
        height: 1;
        min-height: 1;
        margin-left: 1;
        padding: 0 1;
        border: none;
        background: $background;
        color: $text-muted;
    }
    #workspace-header Button:hover { background: $surface; color: $text; }
    #workspace-header Button:focus { background: $primary; color: $background; }
    #workspace-header #stop-task { color: $error; }
    #main-container { layout: horizontal; height: 1fr; }
    #chat-container { width: 1fr; height: 1fr; }
    ChatPanel { height: 1fr; }
    Sidebar { width: 36; height: 1fr; border-left: solid $panel; }
    #jump-latest {
        display: none;
        width: 100%;
        height: 1;
        min-height: 1;
        border: none;
        background: $surface;
        color: $primary;
    }
    #jump-latest.has-unread { display: block; }
    ActivityBar { margin: 0 1; }
    Composer { margin: 0 1; background: $surface; }
    #input-hints { height: 1; padding: 0 2; color: $text-muted; }
    #footer-row { height: 1; padding: 0 1; background: $background; }
    #footer-row StatusFooter { width: 1fr; height: 1; border: none; padding: 0; }
    Screen.compact #workspace-context { display: none; }
    Screen.compact Sidebar { width: 1fr; border-left: none; }
    Screen.short #input-hints { display: none; }
    Screen.tiny #open-transcript, Screen.tiny #fold-tools { display: none; }
    Screen.micro #workspace-brand { display: none; }
    Screen.micro #workspace-header Button { width: 1fr; min-width: 1; margin-left: 0; padding: 0; }
    """

    BINDINGS = [
        Binding("ctrl+s", "interrupt", "停止任务", show=False, priority=True),
        Binding("escape", "interrupt_main", "停止任务", show=False),
        Binding("ctrl+p,f1", "open_commands", "命令", show=False, priority=True),
        Binding("ctrl+t", "toggle_transcript", "会话记录", show=False, priority=True),
        Binding("ctrl+b", "toggle_sidebar", "工作台", show=False, priority=True),
        Binding("ctrl+l", "jump_latest", "回到最新", show=False, priority=True),
        Binding("f2", "show_metrics", "运行指标", show=False, priority=True),
        Binding("super+c,ctrl+shift+c", "copy_selection", "复制选中内容", show=False),
        Binding("ctrl+shift+a", "copy_last_message", "复制最近回复", show=False),
        Binding("ctrl+shift+t", "copy_transcript", "复制完整记录", show=False),
    ]

    def __init__(self, event_bus: EventBus, drx_agent=None):
        super().__init__()
        self.event_bus = event_bus
        self.drx_agent = drx_agent
        self.title = "DRX"
        self._main_screen = None
        self._sidebar_preference = False
        self._tools_collapsed = True
        transcript = getattr(drx_agent, "transcript", None)
        self._owns_transcript = transcript is None
        self.transcript = transcript if transcript is not None else TranscriptLog(event_bus)
        master = getattr(drx_agent, "master", None)
        messages = getattr(master, "messages", None)
        if self._owns_transcript and messages:
            self.transcript.restore_messages(messages)
        root = getattr(master, "memory_namespace", None)
        self._workspace_name = Path(root).name if isinstance(root, (str, Path)) and root else Path.cwd().name
        self.register_theme(Theme(
            name="drx-black",
            primary="#fab283", secondary="#a0a0a0", accent="#fab283",
            foreground="#eeeeee", background="#090909", surface="#141414", panel="#1c1c1c",
            warning="#f2c97d", error="#f87171", success="#a3be8c",
            variables={"text-muted": "#a0a0a0", "border": "#303030"},
        ))
        self.theme = "drx-black"
        self._clipboard_lock = asyncio.Lock()
        self._clipboard_generation = 0

    def action_interrupt(self) -> None:
        self.event_bus.publish(Event(
            type=EventType.AGENT_MESSAGE, data={"text": "/stop", "source": "user"},
        ))

    def action_interrupt_main(self) -> None:
        if self.screen is self._main_screen:
            self.action_interrupt()

    def action_toggle_transcript(self) -> None:
        if isinstance(self.screen, TranscriptScreen):
            self.screen.action_pop_screen()
        elif self.screen is self._main_screen:
            self.push_screen("transcript_view")

    def action_open_commands(self) -> None:
        if self._main_screen is not None and self.screen is self._main_screen:
            self.push_screen(CommandPalette(self.event_bus), self._receive_command_draft)

    def _receive_command_draft(self, command: str | None) -> None:
        if command is None or self._main_screen is None or not (self._main_screen.is_mounted and self._main_screen.is_attached):
            return
        composer = next(iter(self._main_screen.query(Composer)), None)
        if composer is None or not (composer.is_mounted and composer.is_attached):
            return
        if composer.set_command_draft(command) and self.screen is self._main_screen:
            self._main_screen.set_focus(composer, scroll_visible=False)

    def action_toggle_sidebar(self) -> None:
        if self._main_screen is None or self.screen is not self._main_screen:
            return
        self._sidebar_preference = not self._sidebar_preference
        self._apply_layout()
        if self._sidebar_preference:
            sidebar = self._main_screen.query_one(Sidebar)
            agents = sidebar.query_one("#sidebar-agent-list")
            (agents if agents.display else sidebar).focus()
        else:
            self._main_screen.query_one(Composer).focus()

    def action_jump_latest(self) -> None:
        if self._main_screen is not None and self.screen is self._main_screen:
            self._main_screen.query_one(ChatPanel).jump_latest()

    def action_show_metrics(self) -> None:
        if self._main_screen is not None and self.screen is self._main_screen:
            self._main_screen.query_one(RingIndicator).action_show_metrics()

    def on_chat_panel_unread_changed(self, message: ChatPanel.UnreadChanged) -> None:
        if self._main_screen is not None and (self._main_screen.is_mounted and self._main_screen.is_attached):
            button = next(iter(self._main_screen.query("#jump-latest").results(Button)), None)
            if button is None:
                return
            button.label = f"{message.count} 条新动态 · 回到最新 (Ctrl L)"
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
            "收起" if not self._tools_collapsed else "工具"
        )

    def on_resize(self) -> None:
        self.call_after_refresh(self._apply_layout)

    def _apply_layout(self) -> None:
        if self._main_screen is None or not self._main_screen.is_attached:
            return
        width, height = self.size
        compact = width < 100
        sidebar = next(iter(self._main_screen.query(Sidebar)), None)
        conversation = next(iter(self._main_screen.query("#chat-container")), None)
        if sidebar is None or conversation is None or not sidebar.is_attached or not conversation.is_attached:
            return
        conversation_visible = not (compact and self._sidebar_preference)
        focus_will_hide = (
            (not self._sidebar_preference and sidebar.has_focus_within)
            or (not conversation_visible and conversation.has_focus_within)
        )
        self._main_screen.set_class(compact, "compact")
        self._main_screen.set_class(height <= 24, "short")
        self._main_screen.set_class(width < 70, "tiny")
        self._main_screen.set_class(width < 36, "micro")
        sidebar.display = self._sidebar_preference
        conversation.display = conversation_visible
        if focus_will_hide:
            self._main_screen.set_focus(
                sidebar if not conversation_visible else self._main_screen.query_one(Composer),
                scroll_visible=False,
            )
        self._main_screen.query_one("#toggle-sidebar", Button).label = (
            "对话" if compact and self._sidebar_preference else "Agent"
        )
        self._main_screen.query_one("#input-hints", Static).update(
            "Enter 发送 · Shift Enter / Ctrl J 换行 · Tab 补全 · Ctrl P 命令 · Esc 停止"
            if width >= 100 else "Enter 发送 · Shift Enter 换行 · Ctrl P 命令 · Esc 停止"
        )
    def copy_to_clipboard(self, text: str) -> None:
        self._copy_text(text, "复制")

    def _copy_text(self, text: str, title: str) -> None:
        # Textual stores the exact local clipboard before UTF-8/OSC52 encoding.
        # Do not repair malformed Unicode by replacing the user's original data.
        terminal_ok = True
        try:
            super().copy_to_clipboard(text)
        except (UnicodeError, OSError):
            terminal_ok = False
        self._clipboard_generation += 1
        generation = self._clipboard_generation
        if self._main_screen is not None and self._main_screen.is_attached:
            self.run_worker(self._copy_native(text, title, terminal_ok, generation), group="clipboard")

    async def _copy_native(self, text: str, title: str, terminal_ok: bool, generation: int) -> None:
        # Serialize native writers so a slow older copy cannot overwrite a newer
        # request. Never block input/rendering on a missing or hung clipboard tool.
        async with self._clipboard_lock:
            if generation != self._clipboard_generation:
                return
            native_ok = await asyncio.to_thread(_copy_to_system_clipboard, text)
        if generation != self._clipboard_generation or self._main_screen is None or not self._main_screen.is_attached:
            return
        if native_ok:
            self.notify(f"已复制 {len(text)} 字符", title=title)
        elif terminal_ok:
            self.notify("终端剪贴板已发送；系统剪贴板工具不可用", title=title, severity="warning")
        else:
            self.notify("系统与终端剪贴板写入失败；原文保留在应用剪贴板", title=title, severity="error")

    def action_copy_selection(self) -> None:
        text = self.focused.selected_text if isinstance(self.focused, TextArea) else None
        if not text:
            text = self.screen.get_selected_text()
        if not text:
            raise SkipAction()
        self._copy_text(text, "复制选中内容")

    def action_copy_last_message(self) -> None:
        text = self.transcript.last_assistant_text()
        if text:
            self._copy_text(text, "复制最近回复")
        else:
            self.notify("还没有 Agent 回复可复制", title="复制")

    def action_copy_transcript(self) -> None:
        text = self.transcript.render_text()
        if text:
            self._copy_text(text, "复制完整记录")
        else:
            self.notify("会话记录为空", title="复制")

    def compose(self) -> ComposeResult:
        with Horizontal(id="workspace-header"):
            yield Static("DRX", id="workspace-brand", markup=False)
            yield Static(self._workspace_name, id="workspace-context", markup=False)
            yield Button("命令", id="open-commands", tooltip="Ctrl P / F1")
            yield Button("记录", id="open-transcript", tooltip="Ctrl T · 搜索完整会话")
            yield Button("工具", id="fold-tools", tooltip="展开或收起工具详情")
            yield Button("Agent", id="toggle-sidebar", tooltip="Ctrl B · 查看任务与成员")
            yield Button("停止", id="stop-task", tooltip="Esc / Ctrl S")
        with Container(id="main-container"):
            with Container(id="chat-container"):
                yield ChatPanel(self.event_bus, self.transcript)
                yield Button("回到最新", id="jump-latest")
            yield Sidebar(self.event_bus)
        yield ActivityBar(self.event_bus)
        yield Composer(self.event_bus)
        yield Static("", id="input-hints", markup=False)
        with Horizontal(id="footer-row"):
            yield StatusFooter(self.event_bus)
            yield RingIndicator(self.event_bus)

    async def on_mount(self) -> None:
        self._main_screen = self.screen
        self.install_screen(TranscriptScreen(self), name="transcript_view")
        self._apply_layout()
        self.query_one(Composer).border_title = "输入"
        self.set_focus(self.query_one(Composer), scroll_visible=False)
        self.event_bus.publish(Event(type=EventType.STATUS_UPDATE, data={"text": "就绪"}))
        master = getattr(self.drx_agent, "master", None)
        saved_usage = getattr(master, "session_usage", None)
        if saved_usage is not None:
            self.event_bus.publish(Event(EventType.STATUS_UPDATE, {
                **usage_status(restore_usage(saved_usage)),
                "mode": getattr(master, "mode", "act"),
            }))
        if self.drx_agent is not None and hasattr(self.drx_agent, "async_setup"):
            self.run_worker(self._setup_agent(), name="mcp-startup", exit_on_error=False)

    async def _setup_agent(self) -> None:
        try:
            await self.drx_agent.async_setup()
        except Exception as exc:
            self.event_bus.publish(Event(
                type=EventType.ERROR, data={"message": f"MCP setup failed: {exc}"},
            ))

    async def on_unmount(self) -> None:
        self._main_screen = None
        try:
            if self.drx_agent is not None:
                try:
                    await self.drx_agent.async_teardown()
                except Exception as exc:
                    self.exit(return_code=1, message=Text(f"Runtime teardown failed: {exc}"))
                else:
                    try:
                        self.drx_agent.save_session()
                    except Exception as exc:
                        self.exit(return_code=1, message=Text(f"Session auto-save failed: {exc}"))
        finally:
            if self._owns_transcript:
                self.transcript.close()
