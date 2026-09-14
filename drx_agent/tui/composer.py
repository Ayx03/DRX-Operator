"""Input box — user messages/commands/approval responses"""

from typing import Any

from rich.text import Text
from textual import events
from textual.widgets import Input
from textual.binding import Binding
from textual.message import Message

from drx_agent.tui.command_palette import CommandPalette

from drx_agent.event_bus import EventBus, EventType, Event


class Composer(Input):
    """Input box — user messages/commands/approval responses"""

    BINDINGS = [
        Binding("up", "history_previous", "上一条", show=False),
        Binding("down", "history_next", "下一条", show=False),
        Binding("escape", "restore_draft", "恢复草稿", show=False),
    ]

    def __init__(self, event_bus: EventBus):
        super().__init__(placeholder="输入消息或 /命令 · Tab 补全 · ↑↓ 历史")
        self.event_bus = event_bus
        self._approval_request: dict[str, Any] | None = None
        self._history: list[str] = []
        self._history_index: int | None = None
        self._history_draft = ""
        self._replacement_draft: str | None = None
        self._completion_seed: str | None = None
        self._completion_value: str | None = None
        self._completion_index = -1
        self._approval_draft = ""

    class ApprovalChanged(Message):
        """Transfer approval state changes onto the Textual thread."""

        def __init__(self, data: dict[str, Any], *, resolved: bool = False) -> None:
            super().__init__()
            self.data = data
            self.resolved = resolved

    def on_mount(self) -> None:
        self.event_bus.subscribe(EventType.APPROVAL_REQUEST, self._on_approval_request)
        self.event_bus.subscribe(EventType.APPROVAL_RESOLVED, self._on_approval_resolved)

    def on_unmount(self) -> None:
        self.event_bus.unsubscribe(EventType.APPROVAL_REQUEST, self._on_approval_request)
        self.event_bus.unsubscribe(EventType.APPROVAL_RESOLVED, self._on_approval_resolved)

    def _on_approval_request(self, event: Event) -> None:
        if event.data.get("request_id"):
            self.post_message(self.ApprovalChanged(dict(event.data)))

    def _on_approval_resolved(self, event: Event) -> None:
        self.post_message(self.ApprovalChanged(dict(event.data), resolved=True))

    def on_composer_approval_changed(self, message: ApprovalChanged) -> None:
        if message.resolved:
            if not self._approval_request or (
                self._approval_request["request_id"] != message.data.get("request_id")
            ):
                return
            self._approval_request = None
            self.value = self._approval_draft
            self.cursor_position = len(self.value)
            self._approval_draft = ""
        else:
            if self._approval_request is None:
                self._approval_draft = (
                    self._history_draft if self._history_index is not None
                    else self._replacement_draft if self._replacement_draft is not None
                    else self.value
                )
            if not self._approval_request or (
                self._approval_request["request_id"] != message.data["request_id"]
            ):
                self.value = ""
                self._reset_navigation()
            self._approval_request = message.data

        request = self._approval_request
        if request is None:
            self.placeholder = "输入消息或 /命令 · Tab 补全 · ↑↓ 历史"
            self.tooltip = None
            return
        request_id = str(request["request_id"])
        agent = request.get("agent_id", "master")
        operation = request.get("operation", "")
        target = request.get("target", "")
        response_hint = (
            "输入 I CONFIRM DESTRUCTIVE ACTION 批准；n 拒绝 / v 详情"
            if request.get("requires_confirmation_phrase")
            else "y 批准 / n 拒绝 / v 详情"
        )
        self.placeholder = f"审批 {request_id} | {agent} | {operation} → {target}；{response_hint}"
        self.tooltip = Text(self.placeholder)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.stop()
        approval_response = self._approval_request is not None and not text.startswith("/") and (
            self._approval_request.get("requires_confirmation_phrase")
            or text.lower() in ("y", "n", "v", "a", "yes", "no", "always", "继续", "总是")
        )
        if not approval_response:
            if not self._history or self._history[-1] != text:
                self._history.append(text)
                del self._history[:-100]
        self._reset_navigation()

        if text.startswith("/"):
            self._handle_command(text)
        elif approval_response:
            self._handle_approval(text)
        else:
            self.event_bus.publish(Event(
                type=EventType.AGENT_MESSAGE,
                data={"text": text, "source": "user"}
            ))
        self.value = ""

    def _handle_command(self, cmd: str) -> None:
        if cmd == "/help":
            self.event_bus.publish(Event(type=EventType.AGENT_MESSAGE, data={
                "text": (
                    "Commands: /save /resume /target <host> /plan /act /mode "
                    "/memory /memory reload /image <path> [prompt] /scan <host> "
                    "/exploit <host> /status /stop /context /progress /dream"
                ),
                "source": "system"
            }))
        elif cmd == "/save":
            self.event_bus.publish(Event(type=EventType.SESSION_SAVE, data={}))
        elif cmd == "/resume":
            self.event_bus.publish(Event(type=EventType.SESSION_RESTORE, data={}))
        elif cmd.startswith("/image "):
            rest = cmd[len("/image "):].strip()
            path, _, prompt = rest.partition(" ")
            self.event_bus.publish(Event(
                type=EventType.AGENT_MESSAGE,
                data={
                    "source": "user",
                    "text": prompt or "(image attached)",
                    "image_path": path,
                },
            ))
        else:
            self.event_bus.publish(Event(
                type=EventType.AGENT_MESSAGE,
                data={"text": cmd, "source": "user"},
            ))

    def _handle_approval(self, response: str) -> None:
        if self._approval_request is None:
            return
        self.event_bus.publish(Event(
            type=EventType.APPROVAL_RESPONSE,
            data={
                "request_id": self._approval_request["request_id"],
                "response": response,
            },
        ))

    def _reset_navigation(self) -> None:
        self._history_index = None
        self._history_draft = ""
        self._replacement_draft = None
        self._completion_seed = None
        self._completion_value = None
        self._completion_index = -1

    def _replace_value(self, value: str) -> None:
        self.value = value
        self.cursor_position = len(value)

    def action_history_previous(self) -> None:
        if self._approval_request or not self._history:
            return
        if self._history_index is None:
            self._history_draft = self.value
            self._history_index = len(self._history)
        self._history_index = max(0, self._history_index - 1)
        self._replace_value(self._history[self._history_index])

    def action_history_next(self) -> None:
        if self._approval_request or self._history_index is None:
            return
        self._history_index += 1
        if self._history_index >= len(self._history):
            self._history_index = None
            self._replace_value(self._history_draft)
        else:
            self._replace_value(self._history[self._history_index])

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "restore_draft":
            return self._approval_request is None and (
                self._history_index is not None or self._replacement_draft is not None
            )
        return super().check_action(action, parameters)

    def action_restore_draft(self) -> None:
        if self._approval_request:
            return
        draft = (
            self._history_draft if self._history_index is not None
            else self._replacement_draft
        )
        if draft is not None:
            self._replace_value(draft)
        self._reset_navigation()

    def set_command_draft(self, command: str) -> bool:
        """Prefill a command without submitting; Esc restores the previous draft."""
        if self._approval_request:
            self.notify("请先处理当前审批，命令未填入", title="审批进行中")
            self.focus()
            return False
        draft = self._history_draft if self._history_index is not None else self.value
        if self._replacement_draft is not None:
            draft = self._replacement_draft
        self._reset_navigation()
        self._replacement_draft = draft
        self._replace_value(command)
        self.focus()
        return True

    def on_key(self, event: events.Key) -> None:
        if event.key != "tab" or self._approval_request:
            return
        continuing = self._completion_value == self.value and self._completion_seed is not None
        seed = self._completion_seed if continuing else self.value
        if not seed or not seed.startswith("/") or self.cursor_position != len(self.value):
            return
        matches = [cmd for cmd, _ in CommandPalette.SLASH_COMMANDS if cmd.startswith(seed)]
        if not matches:
            return
        event.prevent_default()
        event.stop()
        if not continuing:
            self._completion_seed = seed
            self._completion_index = -1
            if self._replacement_draft is None:
                self._replacement_draft = self.value
        self._completion_index = (self._completion_index + 1) % len(matches)
        self._completion_value = matches[self._completion_index] + " "
        self._replace_value(self._completion_value)

