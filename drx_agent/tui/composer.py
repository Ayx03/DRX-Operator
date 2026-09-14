"""Input box — user messages/commands/approval responses"""

from textual.widgets import Input
from textual.binding import Binding
from textual.message import Message

from drx_agent.event_bus import EventBus, EventType, Event


class Composer(Input):
    """Input box — user messages/commands/approval responses"""

    BINDINGS = [
        Binding("ctrl+k", "command_palette", "Command palette"),
    ]

    def __init__(self, event_bus: EventBus):
        super().__init__(placeholder="Enter message or command... (/help for commands)")
        self.event_bus = event_bus
        self._approval_request: dict | None = None

    class ApprovalChanged(Message):
        """Refresh the input hint on the Textual thread."""

    def on_mount(self) -> None:
        self.event_bus.subscribe(EventType.APPROVAL_REQUEST, self._on_approval_request)
        self.event_bus.subscribe(EventType.APPROVAL_RESOLVED, self._on_approval_resolved)

    def on_unmount(self) -> None:
        self.event_bus.unsubscribe(EventType.APPROVAL_REQUEST, self._on_approval_request)
        self.event_bus.unsubscribe(EventType.APPROVAL_RESOLVED, self._on_approval_resolved)

    def _on_approval_request(self, event: Event) -> None:
        if not event.data.get("request_id"):
            return
        self._approval_request = dict(event.data)
        self.post_message(self.ApprovalChanged())

    def _on_approval_resolved(self, event: Event) -> None:
        if self._approval_request and (
            self._approval_request["request_id"] == event.data.get("request_id")
        ):
            self._approval_request = None
            self.post_message(self.ApprovalChanged())

    def on_composer_approval_changed(self, message: ApprovalChanged) -> None:
        request = self._approval_request
        if request is None:
            self.placeholder = "Enter message or command... (/help for commands)"
            return
        request_id = str(request["request_id"])[:8]
        agent = request.get("agent_id", "master")
        operation = request.get("operation", "")
        target = request.get("target", "")
        response_hint = (
            "输入精确确认短语，n 拒绝"
            if request.get("requires_confirmation_phrase")
            else "y 批准 / n 拒绝 / v 详情"
        )
        self.placeholder = f"[审批 {request_id}] {agent}: {operation} → {target}；{response_hint}"

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return

        if text.startswith("/"):
            self._handle_command(text)
        elif self._approval_request and (
            self._approval_request.get("requires_confirmation_phrase")
            or text.lower() in ("y", "n", "v", "a", "yes", "no", "always", "继续", "总是")
        ):
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

    def action_command_palette(self) -> None:
        self.app.push_screen("command_palette")
