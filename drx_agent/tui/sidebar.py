"""Scrollable session, task, and worker dashboard."""

from typing import Any

from rich.text import Text
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from drx_agent.event_bus import EventBus, EventType, Event
from drx_agent.tui.agent_detail import AgentDetailScreen


_TERMINAL = frozenset({"completed", "done", "error", "failed", "timeout", "cancelled", "canceled"})
_RUNNING = frozenset({"running", "in_progress"})
_STATUS_LABELS = {
    "completed": "完成",
    "done": "完成",
    "running": "运行",
    "in_progress": "运行",
    "pending": "待办",
    "queued": "排队",
    "waiting": "等待",
    "stopping": "停止中",
    "error": "错误",
    "failed": "失败",
    "timeout": "超时",
    "cancelled": "取消",
    "canceled": "取消",
}


class Sidebar(VerticalScroll):
    """Keep unfinished work visible before bounded recent history."""

    HISTORY_LIMIT = 20
    COMPONENT_CLASSES = {"sidebar-running", "sidebar-error", "sidebar-muted"}
    DEFAULT_CSS = """
    Sidebar {
        background: $background;
        color: $text;
        border-left: solid $panel;
        padding: 0 1;
        overflow-x: hidden;
        overflow-y: auto;
    }
    Sidebar:focus { border-left: solid $primary; }
    Sidebar > .sidebar-running { color: $primary; }
    Sidebar > .sidebar-error { color: $error; }
    Sidebar > .sidebar-muted { color: $text-muted; }
    Sidebar Static { width: 1fr; height: auto; }
    Sidebar .sidebar-heading {
        color: $primary;
        text-style: bold;
        margin-top: 1;
        margin-bottom: 1;
    }
    #sidebar-summary, #sidebar-agent-empty { color: $text-muted; }
    #sidebar-agent-list {
        height: auto; width: 1fr; border: none; padding: 0;
        background: $background; color: $text; overflow-x: hidden;
    }
    #sidebar-agent-list > .option-list--option { padding: 0 0 1 0; }
    #sidebar-agent-list > .option-list--option-highlighted {
        background: $panel; color: $primary;
    }
    #sidebar-agent-list:focus > .option-list--option-highlighted {
        background: $panel; color: $primary; text-style: bold;
    }
    """

    class BusEvent(Message):
        def __init__(self, event: Event) -> None:
            super().__init__()
            self.event = event

    def __init__(self, event_bus: EventBus):
        super().__init__()
        self.event_bus = event_bus
        self._tasks: list[dict[str, Any]] = []
        self._agents: dict[str, dict[str, Any]] = {}
        self._active_agents: list[dict[str, Any]] = []
        self._summary: dict[str, Any] = {}
        self._task_counts = (0, 0, 0)
        self._detail: AgentDetailScreen | None = None
        self._subscribed = False
        self._event_types = (
            EventType.STATUS_UPDATE,
            EventType.SUB_AGENT_DISPATCH,
            EventType.SUB_AGENT_RESULT,
            EventType.TARGET_SWITCH,
            EventType.TOOL_CALL,
            EventType.TOOL_RESULT,
        )

    class SessionReset(Message):
        """Clear runtime inspection after a session replacement."""

    def compose(self):
        yield Static("当前会话", classes="sidebar-heading")
        yield Static("等待会话状态", id="sidebar-summary", markup=False)
        yield Static("任务", id="sidebar-task-header", classes="sidebar-heading")
        yield Static("暂无任务", id="sidebar-task-list", markup=False)
        yield Static("Worker · ↑↓ 选择 / Enter 详情", id="sidebar-agent-header", classes="sidebar-heading")
        yield Static("暂无 Worker", id="sidebar-agent-empty", markup=False)
        yield OptionList(id="sidebar-agent-list")

    def on_mount(self) -> None:
        self._subscribed = True
        self.event_bus.subscribe(EventType.SESSION_RESTORED, self._receive_reset)
        for event_type in self._event_types:
            self.event_bus.subscribe(event_type, self._receive_event)
        self._render_summary()
        self._render_tasks()
        self._render_agents()

    def on_unmount(self) -> None:
        self._subscribed = False
        self._detail = None
        self.event_bus.unsubscribe(EventType.SESSION_RESTORED, self._receive_reset)
        for event_type in self._event_types:
            self.event_bus.unsubscribe(event_type, self._receive_event)

    def _receive_event(self, event: Event) -> None:
        # EventBus publishers may run outside Textual's UI thread.
        if self._subscribed:
            self.post_message(self.BusEvent(event))

    def _receive_reset(self, event: Event) -> None:
        if self._subscribed:
            self.post_message(self.SessionReset())

    def on_sidebar_session_reset(self, message: SessionReset) -> None:
        message.stop()
        if not self._subscribed:
            return
        self._tasks.clear()
        self._agents.clear()
        self._active_agents.clear()
        self._summary.clear()
        self._task_counts = (0, 0, 0)
        if self._detail is not None:
            self._detail.invalidate()
        self._render_summary()
        self._render_tasks()
        self._render_agents()

    def on_sidebar_bus_event(self, message: BusEvent) -> None:
        message.stop()
        if not self._subscribed:
            return
        event = message.event
        if event.type == EventType.STATUS_UPDATE:
            self._on_status(event)
        elif event.type == EventType.SUB_AGENT_DISPATCH:
            self._on_agent_dispatch(event)
        elif event.type == EventType.SUB_AGENT_RESULT:
            self._on_agent_result(event)
        elif event.type in (EventType.TOOL_CALL, EventType.TOOL_RESULT):
            self._on_tool_event(event)
        elif event.type == EventType.TARGET_SWITCH:
            if "target" in event.data:
                self._summary["target"] = event.data["target"]
                self._render_summary()

    @staticmethod
    def _ordered(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(rows, key=lambda row: (
            0 if row.get("status") in _RUNNING else
            2 if row.get("status") in _TERMINAL else 1
        ))

    def _on_status(self, event: Event) -> None:
        data = event.data
        for key in ("mode", "active_targets", "target", "session_name", "text"):
            if key in data:
                self._summary[key] = data[key]
        if "tasks" in data:
            tasks = {}
            task_values = data.get("tasks")
            for index, task in enumerate(task_values if isinstance(task_values, list) else []):
                if not isinstance(task, dict):
                    continue
                key = str(task.get("id") or task.get("content") or task.get("name") or index)
                tasks[key] = {**task, "status": str(task.get("status") or "pending")}
            rows = list(tasks.values())
            self._task_counts = (
                sum(t.get("status") in {"done", "completed"} for t in rows),
                len(rows),
                sum(t.get("status") in _RUNNING for t in rows),
            )
            active = [t for t in rows if t.get("status") not in _TERMINAL]
            history = [t for t in rows if t.get("status") in _TERMINAL]
            self._tasks = self._ordered(active) + list(reversed(history[-self.HISTORY_LIMIT:]))
            self._render_tasks()
        self._render_summary()

    def _on_agent_dispatch(self, event: Event) -> None:
        self._merge_agent(event, "running")

    def _on_agent_result(self, event: Event) -> None:
        self._merge_agent(event, "done")

    def _merge_agent(self, event: Event, default_status: str) -> None:
        data = event.data
        # A display name cannot disambiguate overlapping workers.
        if not data.get("agent_id"):
            return
        agent_id = str(data["agent_id"])
        agent = self._agents.pop(agent_id, {"agent_id": agent_id, "name": agent_id, "tools": {}})
        agent.update({
            key: data[key]
            for key in ("name", "role", "type", "target", "task", "text", "result", "error")
            if key in data
        })
        agent["status"] = str(data.get("status") or default_status)
        self._agents[agent_id] = agent
        self._update_agents()
        self._refresh_detail(agent)

    def _on_tool_event(self, event: Event) -> None:
        data = event.data
        agent = self._agents.get(str(data.get("agent_id") or ""))
        if agent is None:
            return
        call_id = data.get("invocation_id") or data.get("call_seq", data.get("tool_call_id"))
        if call_id is None:
            return
        tools = agent["tools"]
        key = str(call_id)
        tool = tools.pop(key, {"call_id": key})
        tool["tool"] = str(data.get("tool") or tool.get("tool") or "tool")
        tool["status"] = str(data.get("status") or (
            "running" if event.type == EventType.TOOL_CALL else "done"
        ))
        for field in ("input", "output", "error"):
            if field in data:
                tool[field] = data[field]
        if "input" not in tool:
            for field in ("args", "code"):
                if field in data:
                    tool["input"] = data[field]
                    break
        tools[key] = tool
        # Preserve unfinished calls as well as the most recent finished calls.
        finished = [key for key, row in tools.items() if row["status"] in _TERMINAL or row["status"] == "success"]
        for key in finished[:-self.HISTORY_LIMIT]:
            del tools[key]
        # A tool finishing says nothing about the worker's lifecycle.
        self._refresh_detail(agent)

    def _refresh_detail(self, agent: dict[str, Any]) -> None:
        if self._detail is not None and (self._detail.is_mounted and self._detail.is_attached):
            if self._detail.agent["agent_id"] == agent["agent_id"]:
                self._detail.refresh_agent(agent)

    def _update_agents(self) -> None:
        history = [key for key, row in self._agents.items() if row.get("status") in _TERMINAL]
        for key in history[:-self.HISTORY_LIMIT]:
            del self._agents[key]
        rows = list(self._agents.values())
        active = [row for row in rows if row.get("status") not in _TERMINAL]
        history_rows = [row for row in rows if row.get("status") in _TERMINAL]
        self._active_agents = self._ordered(active) + list(reversed(history_rows))
        self._render_agents()
        self._render_summary()

    def _render_summary(self) -> None:
        if not (self.is_mounted and self.is_attached):
            return
        summary = Text()
        for key, label in (("session_name", "会话"), ("mode", "模式"),
                           ("target", "目标"), ("active_targets", "目标数")):
            if key in self._summary:
                summary.append(f"{label}  {self._summary[key]}\n")
        running = sum(row.get("status") in _RUNNING for row in self._active_agents)
        summary.append(f"Agent  {running} 运行 / {len(self._active_agents)} 最近")
        self.query_one("#sidebar-summary", Static).update(summary)

    def _append_status(self, text: Text, status: str) -> None:
        label = _STATUS_LABELS.get(status, status)
        component = "sidebar-error" if status in {"error", "failed", "timeout"} else (
            "sidebar-running" if status in _RUNNING else "sidebar-muted"
        )
        text.append(f"[{label}] ", style=self.get_component_rich_style(component))

    def _render_tasks(self) -> None:
        if not (self.is_mounted and self.is_attached):
            return
        done, total, running = self._task_counts
        self.query_one("#sidebar-task-header", Static).update(
            Text(f"任务  {done}/{total} 完成 · {running} 运行")
        )
        text = Text()
        for index, task in enumerate(self._tasks):
            if index:
                text.append("\n\n")
            self._append_status(text, str(task.get("status") or "pending"))
            text.append(str(task.get("content") or task.get("name") or "未命名任务"))
        if total > len(self._tasks):
            text.append(f"\n\n仅显示最近 {self.HISTORY_LIMIT} 条已结束任务", style=self.get_component_rich_style("sidebar-muted"))
        self.query_one("#sidebar-task-list", Static).update(text if text else Text("暂无任务"))

    def _render_agents(self) -> None:
        if not (self.is_mounted and self.is_attached):
            return
        options = self.query_one("#sidebar-agent-list", OptionList)
        selected = (
            options.get_option_at_index(options.highlighted).id
            if options.highlighted is not None and 0 <= options.highlighted < options.option_count else None
        )
        prompts = []
        for agent in self._active_agents:
            text = Text()
            self._append_status(text, str(agent["status"]))
            role = agent.get("role") or agent.get("type") or agent.get("name")
            if role and role != agent["agent_id"]:
                text.append(f"{role} · ")
            text.append(agent["agent_id"])
            if agent.get("target"):
                text.append("\n" + str(agent["target"]), style=self.get_component_rich_style("sidebar-muted"))
            if agent.get("task"):
                # Full task and outputs live in the detail, never in a truncated model.
                text.append("\n" + " ".join(str(agent["task"]).split())[:100], style=self.get_component_rich_style("sidebar-muted"))
            prompts.append((agent["agent_id"], text))
        identities = [identity for identity, _ in prompts]
        previous = [options.get_option_at_index(index).id for index in range(options.option_count)]
        if previous == identities:
            for identity, text in prompts:
                if options.get_option(identity).prompt != text:
                    options.replace_option_prompt(identity, text)
        else:
            options.clear_options()
            options.add_options(Option(text, id=identity) for identity, text in prompts)
            options.highlighted = identities.index(selected) if selected in identities else (0 if identities else None)
        options.display = bool(prompts)
        self.query_one("#sidebar-agent-empty", Static).display = not prompts

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "sidebar-agent-list":
            return
        event.stop()
        agent = self._agents.get(event.option.id or "")
        if agent is not None and self._subscribed and self.app.screen is self.screen and self._detail is None:
            screen = AgentDetailScreen(agent)
            self._detail = screen
            self.app.push_screen(screen, lambda result: self._detail_closed(screen))

    def _detail_closed(self, screen: AgentDetailScreen) -> None:
        if self._detail is screen:
            self._detail = None
