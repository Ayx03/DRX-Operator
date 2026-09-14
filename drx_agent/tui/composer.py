"""Multiline message editor with reversible drafts and request-scoped approvals."""

from threading import Lock
from typing import Any

from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.widgets import TextArea
from textual.widgets.text_area import Selection

from drx_agent.event_bus import EventBus, EventType, Event
from drx_agent.tui.approval_screen import ApprovalScreen
from drx_agent.tui.command_palette import CommandPalette, dispatch_command


class Composer(TextArea):
    """Enter sends; Shift+Enter / Ctrl+J edit a newline without submitting."""

    BINDINGS = [
        Binding("up", "history_previous", "上一条", show=False),
        Binding("down", "history_next", "下一条", show=False),
        Binding("escape", "restore_draft", "恢复草稿", show=False),
        Binding("super+x", "cut", "剪切", show=False),
        Binding("super+v", "paste", "粘贴", show=False),
        Binding("super+a", "select_all", "全选", show=False),
        Binding("super+z", "undo", "撤销", show=False),
        Binding("super+shift+z", "redo", "重做", show=False),
    ]

    DEFAULT_CSS = """
    Composer {
        height: 3;
        min-height: 3;
        max-height: 8;
        padding: 0 1;
        background: $surface;
        color: $text;
        border: round $panel;
    }
    Composer:focus { border: round $primary; }
    Composer .text-area--placeholder { color: $text-muted; }
    Composer .text-area--selection { background: $primary 30%; }
    """

    def __init__(self, event_bus: EventBus):
        super().__init__(
            placeholder="输入消息或 /命令 · Enter 发送 · Ctrl+J 换行",
            tooltip="Enter 发送 · Shift+Enter / Ctrl+J 换行 · Tab 补全 · Ctrl+P 命令 · Esc 恢复草稿",
            soft_wrap=True,
            tab_behavior="focus",
            highlight_cursor_line=False,
        )
        self.event_bus = event_bus
        self._history: list[str] = []
        self._history_index: int | None = None
        self._history_draft: tuple[str, Selection] | None = None
        self._replacement_draft: tuple[str, Selection] | None = None
        self._completion_seed: str | None = None
        self._completion_value: str | None = None
        self._completion_index = -1
        self._approval_requests: dict[str, tuple[int, dict[str, Any]]] = {}
        self._seen_approvals: set[str] = set()
        self._approval_screen: ApprovalScreen | None = None
        self._approval_lock = Lock()
        self._approval_generation = 0
        self._revoked_approvals: set[str] = set()
        self._subscribed = False

    class ApprovalChanged(Message):
        """Transfer backend approval events onto the Textual thread."""

        def __init__(self, data: dict[str, Any], generation: int, *, resolved: bool = False) -> None:
            super().__init__()
            self.data = data
            self.resolved = resolved
            self.generation = generation

    class ApprovalsInvalidated(Message):
        """Invalidate the current UI queue after a session boundary."""

    @property
    def approval_pending(self) -> bool:
        return bool(self._approval_requests) or self._approval_screen is not None

    def on_mount(self) -> None:
        self._subscribed = True
        self.event_bus.subscribe(EventType.APPROVAL_REQUEST, self._on_approval_request)
        self.event_bus.subscribe(EventType.APPROVAL_RESOLVED, self._on_approval_resolved)
        self.event_bus.subscribe(EventType.SESSION_RESTORED, self._on_session_restored)
        self.call_after_refresh(self._resize_editor)

    def on_unmount(self) -> None:
        self._subscribed = False
        with self._approval_lock:
            self._approval_generation += 1
        self._approval_requests.clear()
        if self._approval_screen is not None:
            self._approval_screen.resolve()
        self._approval_screen = None
        self.event_bus.unsubscribe(EventType.APPROVAL_REQUEST, self._on_approval_request)
        self.event_bus.unsubscribe(EventType.APPROVAL_RESOLVED, self._on_approval_resolved)
        self.event_bus.unsubscribe(EventType.SESSION_RESTORED, self._on_session_restored)

    def on_resize(self) -> None:
        self.call_after_refresh(self._resize_editor)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area is self:
            self._resize_editor()

    def _resize_editor(self) -> None:
        if not (self.is_mounted and self.is_attached) or not self._subscribed:
            return
        # Wrapped rows are already maintained by TextArea; don't re-scan the draft.
        rows = min(6, max(1, self.wrapped_document.height))
        self.styles.height = rows + 2

    def _on_approval_request(self, event: Event) -> None:
        if self._subscribed and event.data.get("request_id"):
            with self._approval_lock:
                generation = self._approval_generation
                self.post_message(self.ApprovalChanged(dict(event.data), generation))

    def _on_approval_resolved(self, event: Event) -> None:
        if self._subscribed and event.data.get("request_id"):
            with self._approval_lock:
                generation = self._approval_generation
                self._revoked_approvals.add(str(event.data["request_id"]))
                self.post_message(self.ApprovalChanged(dict(event.data), generation, resolved=True))

    def _on_session_restored(self, event: Event) -> None:
        self._invalidate_approvals()

    def _invalidate_approvals(self) -> None:
        if self._subscribed:
            with self._approval_lock:
                self._approval_generation += 1
                self.post_message(self.ApprovalsInvalidated())

    def on_composer_approvals_invalidated(self, message: ApprovalsInvalidated) -> None:
        message.stop()
        if not self._subscribed:
            return
        self._approval_requests.clear()
        if self._approval_screen is not None:
            self._approval_screen.resolve()

    def on_composer_approval_changed(self, message: ApprovalChanged) -> None:
        message.stop()
        with self._approval_lock:
            current = message.generation == self._approval_generation
        if not self._subscribed or not current:
            return
        request_id = str(message.data["request_id"])
        if message.resolved:
            self._seen_approvals.add(request_id)
            self._approval_requests.pop(request_id, None)
            screen = self._approval_screen
            if screen is not None and screen.request_id == request_id:
                screen.resolve()
            return
        if request_id in self._seen_approvals or request_id in self._revoked_approvals:
            return
        self._seen_approvals.add(request_id)
        self._approval_requests[request_id] = (message.generation, message.data)
        self._show_next_approval()

    def _show_next_approval(self) -> None:
        if not self._subscribed or not (self.is_mounted and self.is_attached) or self._approval_screen is not None or not self._approval_requests:
            return
        generation, request = next(iter(self._approval_requests.values()))
        with self._approval_lock:
            if generation != self._approval_generation:
                return
        screen = ApprovalScreen(request)
        self._approval_screen = screen
        self.app.push_screen(screen, lambda response: self._approval_closed(screen, generation, response))

    def _approval_closed(self, screen: ApprovalScreen, generation: int, response: str | None) -> None:
        if not self._subscribed or screen is not self._approval_screen:
            return
        request_id = screen.request_id
        pending = self._approval_requests.pop(request_id, None)
        self._approval_screen = None
        with self._approval_lock:
            valid = generation == self._approval_generation and request_id not in self._revoked_approvals
        if pending is not None and response is not None and valid:
            self.event_bus.publish(Event(
                type=EventType.APPROVAL_RESPONSE,
                data={"request_id": request_id, "response": response},
            ))
        if self._approval_requests:
            self.call_after_refresh(self._show_next_approval)
        elif (self.is_mounted and self.is_attached) and self.app.screen is self.screen:
            self.focus()

    def action_submit(self) -> None:
        text = self.text
        if self.approval_pending or not text.strip():
            return
        if not self._history or self._history[-1] != text:
            self._history.append(text)
            del self._history[:-100]
        self._reset_navigation()
        self.load_text("")
        # Only a single literal slash-command line is parsed as a command. Code,
        # indentation, and all other user messages retain their exact whitespace.
        if text.startswith("/") and "\n" not in text and "\r" not in text:
            dispatch_command(self.event_bus, text)
        else:
            self.event_bus.publish(Event(
                type=EventType.AGENT_MESSAGE,
                data={"text": text, "source": "user"},
            ))

    def _reset_navigation(self) -> None:
        self._history_index = None
        self._history_draft = None
        self._replacement_draft = None
        self._completion_seed = None
        self._completion_value = None
        self._completion_index = -1

    def _replace_text(self, text: str) -> None:
        self.load_text(text)
        self.move_cursor(self.document.end)

    def _at_history_boundary(self, *, previous: bool) -> bool:
        if self.selection.start != self.selection.end:
            return False
        # Editing a recalled entry must not let an arrow discard those edits.
        if self._history_index is not None and self.text != self._history[self._history_index]:
            return False
        row = self.wrapped_document.location_to_offset(self.cursor_location).y
        return row == (0 if previous else self.wrapped_document.height - 1)

    def action_history_previous(self) -> None:
        if self.approval_pending or not self._history or not self._at_history_boundary(previous=True):
            self.action_cursor_up()
            return
        if self._history_index == 0:
            return
        if self._history_index is None:
            self._history_draft = (self.text, self.selection)
            self._history_index = len(self._history)
        self._history_index = max(0, self._history_index - 1)
        self._replace_text(self._history[self._history_index])

    def action_history_next(self) -> None:
        if self.approval_pending or self._history_index is None or not self._at_history_boundary(previous=False):
            self.action_cursor_down()
            return
        self._history_index += 1
        if self._history_index >= len(self._history):
            draft = self._history_draft
            self._history_index = None
            self._history_draft = None
            if draft is not None:
                self.load_text(draft[0])
                self.selection = draft[1]
        else:
            self._replace_text(self._history[self._history_index])

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "restore_draft":
            return not self.approval_pending and (
                self._history_index is not None or self._replacement_draft is not None
            )
        return super().check_action(action, parameters)

    def action_restore_draft(self) -> None:
        if self.approval_pending:
            return
        draft = self._history_draft if self._history_index is not None else self._replacement_draft
        if draft is not None:
            self.load_text(draft[0])
            self.selection = draft[1]
        replacement = self._replacement_draft if self._history_index is not None else None
        self._reset_navigation()
        self._replacement_draft = replacement

    def set_command_draft(self, command: str) -> bool:
        """Prefill an editable command; Escape restores the prior draft and cursor."""
        if self.approval_pending:
            return False
        draft = self._history_draft if self._history_index is not None else (self.text, self.selection)
        if self._replacement_draft is not None:
            draft = self._replacement_draft
        self._reset_navigation()
        self._replacement_draft = draft
        self._replace_text(command)
        self.focus()
        return True

    def on_key(self, event: events.Key) -> None:
        if event.key in ("enter", "shift+enter", "ctrl+j"):
            event.prevent_default()
            event.stop()
            if self.approval_pending:
                return
            if event.key == "enter":
                self.action_submit()
            else:
                self.replace("\n", *self.selection, maintain_selection_offset=False)
            return
        if event.key != "tab" or self.approval_pending:
            return
        continuing = self._completion_value == self.text and self._completion_seed is not None
        seed = self._completion_seed if continuing else self.text
        if not seed or not seed.startswith("/") or "\n" in seed or not self.cursor_at_end_of_text:
            return
        if self.selection.start != self.selection.end:
            return
        matches = [cmd for cmd, _ in CommandPalette.SLASH_COMMANDS if cmd.startswith(seed)]
        if not matches:
            return
        event.prevent_default()
        event.stop()
        if not continuing:
            self._completion_seed = seed
            self._completion_index = -1
            if self._replacement_draft is None and self._history_index is None:
                self._replacement_draft = (self.text, self.selection)
        self._completion_index = (self._completion_index + 1) % len(matches)
        self._completion_value = matches[self._completion_index] + " "
        self._replace_text(self._completion_value)
