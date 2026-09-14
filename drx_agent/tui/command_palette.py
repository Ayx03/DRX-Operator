"""Searchable command picker. Parameterized commands return an editable draft."""

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from drx_agent.event_bus import Event, EventBus, EventType


def dispatch_command(event_bus: EventBus, cmd: str) -> None:
    """Dispatch a complete slash command identically from either entry surface."""
    command = cmd.rstrip()
    if command == "/help":
        event_bus.publish(Event(type=EventType.AGENT_MESSAGE, data={
            "text": (
                "Commands: /save /resume /target <host> /plan /act /mode "
                "/memory /memory reload /image <path> [prompt] /scan <host> "
                "/exploit <host> /status /stop /context /progress /dream "
                "/roles /team /vote /vote cancel /memory search <query> /memory get <id>"
            ),
            "source": "system",
        }))
    elif command == "/save":
        event_bus.publish(Event(type=EventType.SESSION_SAVE, data={}))
    elif command == "/resume":
        event_bus.publish(Event(type=EventType.SESSION_RESTORE, data={}))
    elif cmd.startswith("/image "):
        rest = cmd[len("/image "):].strip()
        path, _, prompt = rest.partition(" ")
        event_bus.publish(Event(
            type=EventType.AGENT_MESSAGE,
            data={"source": "user", "text": prompt or "(image attached)", "image_path": path},
        ))
    else:
        event_bus.publish(Event(
            type=EventType.AGENT_MESSAGE,
            data={"text": cmd, "source": "user"},
        ))


class CommandPalette(ModalScreen[str | None]):
    """Search descriptions or command names, then explicitly choose with Enter."""

    SLASH_COMMANDS: list[tuple[str, str]] = [
        ("/scan", "启动侦察扫描"),
        ("/exploit", "启动漏洞利用"),
        ("/target", "管理目标主机信息"),
        ("/status", "查看当前系统状态"),
        ("/plan", "切换到 plan 模式（仅只读工具）"),
        ("/act", "切换到 act 模式（允许全部工具）"),
        ("/mode", "查看当前模式"),
        ("/stop", "中断当前任务"),
        ("/cancel", "中断当前任务"),
        ("/interrupt", "中断当前任务"),
        ("/dream", "触发深度上下文压缩（L6 层）"),
        ("/context", "查看上下文使用量"),
        ("/progress", "查看进度文档（9 段结构）"),
        ("/memory", "查看项目记忆（DRX.md/AGENTS.md/CLAUDE.md）"),
        ("/memory reload", "重新加载项目记忆文件"),
        ("/memory search", "检索本项目已准入长期经验"),
        ("/memory get", "读取长期经验原文与来源"),
        ("/roles", "列出专业角色、工具权限与预算"),
        ("/team", "查看并发队列、成员和收束状态"),
        ("/vote", "查看全员投票及缺票成员"),
        ("/vote cancel", "撤销当前投票并停止征询"),
        ("/save", "保存当前会话"),
        ("/resume", "恢复最近保存的会话"),
        ("/help", "显示命令帮助"),
    ]

    PARAMETER_COMMANDS = frozenset({
        "/scan", "/exploit", "/target", "/memory search", "/memory get",
    })

    BINDINGS = [
        Binding("escape", "pop_screen", "返回", show=False),
        Binding("up", "select_previous", "上一条", show=False, priority=True),
        Binding("down", "select_next", "下一条", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    CommandPalette {
        align: center middle;
        background: $background 85%;
    }
    CommandPalette #palette-dialog {
        width: 92%;
        max-width: 80;
        height: 85%;
        max-height: 26;
        background: $surface;
        border: round $panel;
        padding: 0 1;
    }
    CommandPalette #palette-title {
        height: 2;
        content-align: left middle;
        color: $primary;
        text-style: bold;
    }
    CommandPalette #palette-search {
        height: 3;
        margin-bottom: 1;
    }
    CommandPalette #palette-list {
        height: 1fr;
        border: none;
        background: $surface;
    }
    CommandPalette #palette-hint {
        height: 2;
        color: $text-muted;
    }
    """

    def __init__(self, event_bus: EventBus) -> None:
        super().__init__()
        self.event_bus = event_bus
        self._answered = False

    def compose(self) -> ComposeResult:
        with Vertical(id="palette-dialog"):
            yield Static("命令", id="palette-title", markup=False)
            yield Input(placeholder="搜索命令或说明…", id="palette-search")
            yield OptionList(id="palette-list")
            yield Static(id="palette-hint", markup=False)

    def on_mount(self) -> None:
        self._filter_commands("")
        self.query_one("#palette-search", Input).focus()

    def on_screen_resume(self) -> None:
        if (self.is_mounted and self.is_attached) and self.app.screen is self:
            self.query_one("#palette-search", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "palette-search" and (self.is_mounted and self.is_attached) and not self._answered:
            self._filter_commands(event.value)

    def _filter_commands(self, query: str) -> None:
        terms = query.casefold().split()
        palette = self.query_one("#palette-list", OptionList)
        palette.clear_options()
        for cmd, desc in self.SLASH_COMMANDS:
            if all(term in f"{cmd} {desc}".casefold() for term in terms):
                suffix = " · 填入参数" if cmd in self.PARAMETER_COMMANDS else ""
                palette.add_option(Option(Text(f"{cmd}  {desc}{suffix}"), id=cmd))
        palette.highlighted = 0 if palette.option_count else None
        self.query_one("#palette-hint", Static).update(
            f"{palette.option_count} 个命令 · ↑↓ 选择 · Enter 确认 · Esc 返回"
            if palette.option_count else "无匹配命令 · 修改关键词或 Esc 返回"
        )

    def action_select_previous(self) -> None:
        self._move_selection(-1)

    def action_select_next(self) -> None:
        self._move_selection(1)

    def _move_selection(self, delta: int) -> None:
        if self._answered or not (self.is_mounted and self.is_attached) or self.app.screen is not self:
            return
        palette = self.query_one("#palette-list", OptionList)
        if palette.option_count:
            palette.highlighted = ((palette.highlighted or 0) + delta) % palette.option_count

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "palette-search" and (self.is_mounted and self.is_attached) and not self._answered and self.app.screen is self:
            event.stop()
            palette = self.query_one("#palette-list", OptionList)
            if palette.highlighted is not None:
                cmd = palette.get_option_at_index(palette.highlighted).id
                if cmd:
                    self._choose_command(cmd)

    def action_pop_screen(self) -> None:
        """Esc — return to the previous screen without running a command."""
        if (self.is_mounted and self.is_attached) and self.app.screen is self and not self._answered:
            self._answered = True
            self.dismiss(None)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option_list.id == "palette-list" and event.option.id:
            self._choose_command(event.option.id)

    def _choose_command(self, cmd: str) -> None:
        if self._answered or not (self.is_mounted and self.is_attached) or self.app.screen is not self:
            return
        if cmd not in {command for command, _ in self.SLASH_COMMANDS}:
            return
        self._answered = True
        if cmd in self.PARAMETER_COMMANDS:
            self.dismiss(cmd + " ")
        else:
            # Pop this screen before a synchronous subscriber can open a modal.
            self.dismiss(None)
            dispatch_command(self.event_bus, cmd)
