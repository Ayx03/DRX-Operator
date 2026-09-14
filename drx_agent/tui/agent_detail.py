"""Live, lossless inspection of one runtime worker."""

import json
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from drx_agent.tui.chat_panel import ToolOutputScreen, bounded_preview


def value_text(value: Any) -> str:
    """Keep structured tool inputs and results readable without truncating them."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


class AgentDetailScreen(ModalScreen[None]):
    """Inspect an identity, not a display name; Sidebar pushes live changes here."""

    BINDINGS = [
        Binding("escape", "close", "关闭", show=False),
        Binding("ctrl+y", "copy", "复制详情", show=False),
    ]
    DEFAULT_CSS = """
    AgentDetailScreen { align: center middle; background: $background 80%; }
    AgentDetailScreen > #agent-detail-dialog {
        width: 96%; max-width: 110; height: 92%;
        background: $surface; border: solid $primary; padding: 0 1;
    }
    AgentDetailScreen #agent-detail-title {
        height: auto; color: $primary; text-style: bold;
    }
    AgentDetailScreen #agent-detail-scroll { height: 1fr; overflow-x: hidden; }
    AgentDetailScreen #agent-detail-text { height: auto; width: 1fr; color: $text; }
    AgentDetailScreen #agent-detail-actions { height: 3; align-horizontal: right; }
    AgentDetailScreen Button { min-width: 1; width: 1fr; margin-left: 0; padding: 0; }
    """

    def __init__(self, agent: dict[str, Any]) -> None:
        super().__init__()
        self.agent = agent
        self.detail_text = ""
        self._dismissed = False
        self._dismiss_requested = False
        self._invalidated = False

    def compose(self) -> ComposeResult:
        with Vertical(id="agent-detail-dialog"):
            yield Static("Worker 详情 · Esc 返回", id="agent-detail-title", markup=False)
            with VerticalScroll(id="agent-detail-scroll"):
                yield Static(id="agent-detail-text", markup=False)
            with Horizontal(id="agent-detail-actions"):
                yield Button("完整详情", id="agent-detail-full")
                yield Button("复制详情", id="agent-detail-copy", variant="primary")
                yield Button("关闭", id="agent-detail-close")

    def on_mount(self) -> None:
        self.refresh_agent(self.agent)
        self.query_one("#agent-detail-text", Static).update(Text(bounded_preview(self.detail_text, 20000, 300)))
        if self._dismiss_requested:
            self.call_after_refresh(self.action_close)
        self.query_one("#agent-detail-scroll", VerticalScroll).focus()

    def on_screen_resume(self) -> None:
        if self._dismiss_requested:
            self.call_after_refresh(self.action_close)

    def invalidate(self) -> None:
        """A restored session no longer owns this runtime identity."""
        self._invalidated = True
        self.action_close()

    def refresh_agent(self, agent: dict[str, Any]) -> None:
        self.agent = agent
        sections = [
            f"运行身份  {agent.get('agent_id') or '未报告'}",
            f"名称  {agent.get('name') or agent.get('agent_id') or '未报告'}",
            f"角色  {agent.get('role') or agent.get('type') or '未报告'}",
            f"目标  {agent.get('target') or '未报告'}",
            f"状态  {agent.get('status') or '未报告'}",
            "\n任务\n" + value_text(agent.get("task") if agent.get("task") is not None else "未报告"),
        ]
        tools = agent.get("tools") or {}
        sections.append("\n最近工具活动" if tools else "\n最近工具活动  无")
        if not isinstance(tools, dict):
            sections.append(value_text(tools))
            tools = {}
        for identity, tool in tools.items():
            if not isinstance(tool, dict):
                sections.append(f"\n{identity}\n{value_text(tool)}")
                continue
            sections.append(
                f"\n{tool.get('tool') or 'tool'} · 调用 {tool.get('call_id', identity)} · {tool.get('status') or '未报告'}"
            )
            for key, label in (("input", "输入"), ("output", "输出"), ("error", "错误")):
                if key in tool:
                    sections.append(f"{label}\n{value_text(tool[key])}")
        for key, label in (("text", "返回结果"), ("result", "返回结果"), ("error", "错误")):
            if key in agent and agent[key] is not None:
                sections.append(f"\n{label}\n{value_text(agent[key])}")
        text = "\n".join(sections)
        if text == self.detail_text:
            return
        self.detail_text = text
        if (self.is_mounted and self.is_attached) and not self._dismissed:
            self.query_one("#agent-detail-text", Static).update(Text(bounded_preview(text, 20000, 300)))

    def action_copy(self) -> None:
        if not (self.is_mounted and self.is_attached) or self._dismissed or self._invalidated or self.app.screen is not self:
            return
        copy_text = getattr(self.app, "_copy_text", None)
        if callable(copy_text):
            copy_text(self.detail_text, "复制 Worker 详情")
        else:
            self.app.copy_to_clipboard(self.detail_text)
            self.notify("已发送完整详情到终端剪贴板")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "agent-detail-copy":
            self.action_copy()
        elif event.button.id == "agent-detail-close":
            self.action_close()
        elif event.button.id == "agent-detail-full":
            if self.is_attached and not self._dismissed and not self._invalidated and self.app.screen is self:
                self.app.push_screen(ToolOutputScreen("Worker 详情", self.detail_text))

    def action_close(self) -> None:
        self._dismiss_requested = True
        if (self.is_mounted and self.is_attached) and not self._dismissed and self.app.screen is self:
            self._dismissed = True
            self.dismiss(None)
