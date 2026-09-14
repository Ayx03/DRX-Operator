"""Searchable command picker. Parameterized commands return an editable draft."""

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from drx_agent.event_bus import Event, EventBus, EventType


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
        ("/save", "保存当前会话"),
        ("/resume", "恢复最近保存的会话"),
        ("/help", "显示命令帮助"),
    ]

    PARAMETER_COMMANDS = frozenset({"/scan", "/exploit", "/target"})

    BINDINGS = [
        Binding("escape", "pop_screen", "返回", show=False),
        Binding("up", "select_previous", "上一条", show=False, priority=True),
        Binding("down", "select_next", "下一条", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    CommandPalette {
        align: center middle;
        background: #0b1020 85%;
    }
    CommandPalette #palette-dialog {
        width: 92%;
        max-width: 80;
        height: 85%;
        max-height: 26;
        background: #111a2c;
        border: round #26354b;
        padding: 0 1;
    }
    CommandPalette #palette-title {
        height: 2;
        content-align: left middle;
        color: #53d7c3;
        text-style: bold;
    }
    CommandPalette #palette-search {
        height: 3;
        margin-bottom: 1;
    }
    CommandPalette #palette-list {
        height: 1fr;
        border: none;
        background: #111a2c;
    }
    CommandPalette #palette-hint {
        height: 2;
        color: #92a4bb;
    }
    """

    def __init__(self, event_bus: EventBus) -> None:
        super().__init__()
        self.event_bus = event_bus

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
        if self.is_mounted:
            self.query_one("#palette-search", Input).value = ""
            self._filter_commands("")
            self.query_one("#palette-search", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "palette-search":
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
        palette = self.query_one("#palette-list", OptionList)
        if palette.option_count:
            palette.highlighted = ((palette.highlighted or 0) + delta) % palette.option_count

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "palette-search":
            event.stop()
            palette = self.query_one("#palette-list", OptionList)
            if palette.highlighted is not None:
                cmd = palette.get_option_at_index(palette.highlighted).id
                if cmd:
                    self._choose_command(cmd)

    def action_pop_screen(self) -> None:
        """Esc — return to the previous screen without running a command."""
        self.dismiss(None)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id:
            self._choose_command(event.option.id)

    def _choose_command(self, cmd: str) -> None:
        if cmd in self.PARAMETER_COMMANDS:
            self.dismiss(cmd + " ")
        else:
            self._execute_command(cmd)
            self.dismiss(None)

    def _execute_command(self, cmd: str) -> None:
        if cmd in self.PARAMETER_COMMANDS:
            return
        if cmd == "/save":
            self.event_bus.publish(Event(type=EventType.SESSION_SAVE, data={}))
        elif cmd == "/resume":
            self.event_bus.publish(Event(type=EventType.SESSION_RESTORE, data={}))
        else:
            self.event_bus.publish(Event(
                type=EventType.AGENT_MESSAGE,
                data={"text": cmd, "source": "user"},
            ))
