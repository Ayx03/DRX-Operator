"""Scrollable session, task, and worker dashboard."""

from typing import Any

from rich.text import Text
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widgets import Static

from drx_agent.event_bus import EventBus, EventType, Event


_TERMINAL = frozenset({"completed", "done", "error", "failed", "timeout", "cancelled", "canceled"})
_RUNNING = frozenset({"running", "in_progress"})
_STATUS_LABELS = {
    "completed": ("完成", "#53d7c3"),
    "done": ("完成", "#53d7c3"),
    "running": ("运行", "#53d7c3"),
    "in_progress": ("运行", "#53d7c3"),
    "pending": ("待办", "#92a4bb"),
    "queued": ("排队", "#92a4bb"),
    "error": ("错误", "#ff7f8a"),
    "failed": ("失败", "#ff7f8a"),
    "timeout": ("超时", "#ffc36a"),
    "cancelled": ("取消", "#92a4bb"),
    "canceled": ("取消", "#92a4bb"),
}


class Sidebar(VerticalScroll):
    """Keep unfinished work visible before bounded recent history."""

    HISTORY_LIMIT = 20
    DEFAULT_CSS = """
    Sidebar {
        background: #111a2c;
        color: #e7edf7;
        border-left: solid #26354b;
        padding: 0 1;
        overflow-x: hidden;
        overflow-y: auto;
    }
    Sidebar:focus { border-left: solid #53d7c3; }
    Sidebar Static { width: 1fr; height: auto; }
    Sidebar .sidebar-heading {
        color: #53d7c3;
        text-style: bold;
        margin-top: 1;
        margin-bottom: 1;
    }
    #sidebar-summary { color: #92a4bb; }
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
        self._event_types = (
            EventType.STATUS_UPDATE,
            EventType.SUB_AGENT_DISPATCH,
            EventType.SUB_AGENT_RESULT,
            EventType.TARGET_SWITCH,
        )

    def compose(self):
        yield Static("当前会话", classes="sidebar-heading")
        yield Static("等待会话状态", id="sidebar-summary", markup=False)
        yield Static("任务", id="sidebar-task-header", classes="sidebar-heading")
        yield Static("暂无任务", id="sidebar-task-list", markup=False)
        yield Static("Agent", id="sidebar-agent-header", classes="sidebar-heading")
        yield Static("暂无 Agent", id="sidebar-agent-list", markup=False)

    def on_mount(self) -> None:
        for event_type in self._event_types:
            self.event_bus.subscribe(event_type, self._receive_event)
        self._render_summary()
        self._render_tasks()
        self._render_agents()

    def on_unmount(self) -> None:
        for event_type in self._event_types:
            self.event_bus.unsubscribe(event_type, self._receive_event)

    def _receive_event(self, event: Event) -> None:
        # EventBus publishers may run outside Textual's UI thread.
        self.post_message(self.BusEvent(event))

    def on_sidebar_bus_event(self, message: BusEvent) -> None:
        message.stop()
        event = message.event
        if event.type == EventType.STATUS_UPDATE:
            self._on_status(event)
        elif event.type == EventType.SUB_AGENT_DISPATCH:
            self._on_agent_dispatch(event)
        elif event.type == EventType.SUB_AGENT_RESULT:
            self._on_agent_result(event)
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
            for index, task in enumerate(data.get("tasks") or []):
                if not isinstance(task, dict):
                    continue
                key = str(task.get("id") or task.get("content") or task.get("name") or index)
                tasks[key] = dict(task)
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
        data = event.data
        agent_id = str(data.get("agent_id") or "unknown")
        agent = self._agents.pop(agent_id, {"name": agent_id})
        agent.update({key: data[key] for key in ("type", "target", "task") if key in data})
        agent["status"] = "running"
        self._agents[agent_id] = agent
        self._update_agents()

    def _on_agent_result(self, event: Event) -> None:
        data = event.data
        agent_id = str(data.get("agent_id") or "unknown")
        agent = self._agents.pop(agent_id, {"name": agent_id})
        agent["status"] = str(data.get("status") or "done")
        self._agents[agent_id] = agent
        self._update_agents()

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
        if not self.is_mounted:
            return
        summary = Text()
        for key, label in (("session_name", "会话"), ("mode", "模式"),
                           ("target", "目标"), ("active_targets", "目标数")):
            if key in self._summary:
                summary.append(f"{label}  {self._summary[key]}\n")
        running = sum(row.get("status") in _RUNNING for row in self._active_agents)
        summary.append(f"Agent  {running} 运行 / {len(self._active_agents)} 最近")
        if self._summary.get("text"):
            summary.append("\n" + str(self._summary["text"]))
        self.query_one("#sidebar-summary", Static).update(summary)

    @staticmethod
    def _append_status(text: Text, status: str) -> None:
        label, color = _STATUS_LABELS.get(status, (status, "#92a4bb"))
        text.append(f"[{label}] ", style=color)

    def _render_tasks(self) -> None:
        if not self.is_mounted:
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
            text.append(f"\n\n仅显示最近 {self.HISTORY_LIMIT} 条已结束任务", style="#92a4bb")
        self.query_one("#sidebar-task-list", Static).update(text if text else Text("暂无任务"))

    def _render_agents(self) -> None:
        if not self.is_mounted:
            return
        text = Text()
        for index, agent in enumerate(self._active_agents):
            if index:
                text.append("\n\n")
            self._append_status(text, str(agent["status"]))
            text.append(str(agent["name"]))
            details = " · ".join(str(agent[key]) for key in ("type", "target") if agent.get(key))
            if details:
                text.append("\n" + details, style="#92a4bb")
            if agent.get("task"):
                text.append("\n" + str(agent["task"]))
        self.query_one("#sidebar-agent-list", Static).update(text if text else Text("暂无 Agent"))
