"""Focused, request-scoped approvals which never borrow the message editor."""

import json
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


class ApprovalScreen(ModalScreen[str | None]):
    """Approve/reject one request; None means it was resolved externally."""

    BINDINGS = [Binding("escape", "reject", "拒绝", priority=True)]

    DEFAULT_CSS = """
    ApprovalScreen {
        align: center middle;
        background: $background 85%;
    }
    #approval-dialog {
        width: 76;
        max-width: 100%;
        height: 24;
        max-height: 100%;
        padding: 0 1;
        background: $surface;
        color: $text;
        border: solid $primary;
    }
    #approval-heading { height: 1; color: $primary; text-style: bold; }
    #approval-context { height: 1fr; min-height: 3; margin: 1 0; }
    #approval-summary, #approval-details { height: auto; }
    #approval-details { color: $text-muted; margin-top: 1; }
    #approval-phrase-hint { height: auto; color: $text; }
    #approval-confirmation { height: 3; background: $panel; border: solid $panel; }
    #approval-confirmation:focus { border: solid $primary; }
    #approval-error { height: auto; color: $error; }
    #approval-actions { height: auto; min-height: 3; align-horizontal: right; }
    #approval-actions Button { min-width: 1; width: 1fr; padding: 0; margin-left: 0; background: $panel; color: $text; }
    #approval-actions Button:focus { border: tall $primary; }
    #approval-approve { color: $primary; }
    #approval-reject { color: $error; }
    """

    def __init__(self, request: dict[str, Any]) -> None:
        super().__init__()
        self.request = dict(request)
        self.request_id = str(request["request_id"])
        self.confirmation_phrase = "I CONFIRM DESTRUCTIVE ACTION"
        self.requires_phrase = bool(request.get("requires_confirmation_phrase"))
        self._can_allow_session = request.get("kind") == "permission" and not self.requires_phrase
        self._resolved = False
        self._answered = False

    def compose(self) -> ComposeResult:
        actor = self.request.get("agent_id") or self.request.get("actor") or "master"
        summary = (
            f"Request: {self.request_id}\n"
            f"Actor: {actor}\n"
            f"Operation: {self.request.get('operation') or self.request.get('tool_name') or '—'}\n"
            f"Target: {self.request.get('target') or '—'}\n"
            f"Risk: {self.request.get('risk_level') or self.request.get('risk') or 'unspecified'}"
        )
        context_keys = {
            "request_id", "agent_id", "actor", "operation", "target", "risk_level", "risk",
            "requires_confirmation_phrase", "confirmation_phrase", "requires_approval",
        }
        details = {key: value for key, value in self.request.items() if key not in context_keys}
        with VerticalScroll(id="approval-dialog"):
            yield Static("Approval required · Esc 拒绝", id="approval-heading", markup=False)
            with VerticalScroll(id="approval-context"):
                yield Static(summary, id="approval-summary", markup=False)
                if details:
                    yield Static(json.dumps(details, ensure_ascii=False, indent=2, default=str), id="approval-details", markup=False)
            if self.requires_phrase:
                yield Static(
                    f"Destructive action. Type exactly to approve:\n{self.confirmation_phrase}",
                    id="approval-phrase-hint", markup=False,
                )
                yield Input(placeholder="Exact confirmation phrase", id="approval-confirmation")
            yield Static("", id="approval-error", markup=False)
            with Horizontal(id="approval-actions"):
                yield Button("Reject", id="approval-reject")
                if self._can_allow_session:
                    yield Button("Always", id="approval-always", tooltip="仅本会话允许此工具；不会覆盖 deny 规则")
                yield Button("Approve", id="approval-approve", disabled=self.requires_phrase)

    def on_mount(self) -> None:
        if self._resolved:
            self.call_after_refresh(self.resolve)
        elif self.requires_phrase:
            self.query_one("#approval-confirmation", Input).focus()
        else:
            # An incidental Enter must never approve a newly arrived request.
            self.query_one("#approval-reject", Button).focus()

    def on_screen_resume(self) -> None:
        if self._resolved and (self.is_mounted and self.is_attached):
            self.call_after_refresh(self.resolve)

    def resolve(self) -> None:
        """Close only this request, without generating an approval response."""
        self._resolved = True
        if (self.is_mounted and self.is_attached) and self.app.screen is self and not self._answered:
            self._answered = True
            self.dismiss(None)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "approval-confirmation" and (self.is_mounted and self.is_attached) and not self._answered:
            event.stop()
            self.query_one("#approval-approve", Button).disabled = event.value != self.confirmation_phrase
            self.query_one("#approval-error", Static).update("")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "approval-confirmation":
            event.stop()
            self.action_approve()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "approval-approve":
            self.action_approve()
        elif event.button.id == "approval-reject":
            self.action_reject()
        elif event.button.id == "approval-always" and self._can_allow_session:
            self._respond("always")

    def action_approve(self) -> None:
        if self._resolved or self._answered or not (self.is_mounted and self.is_attached) or self.app.screen is not self:
            return
        response = "y"
        if self.requires_phrase:
            response = self.query_one("#approval-confirmation", Input).value
            if response != self.confirmation_phrase:
                self.query_one("#approval-error", Static).update("Confirmation must match exactly; nothing was approved.")
                self.query_one("#approval-confirmation", Input).focus()
                return
        self._respond(response)

    def action_reject(self) -> None:
        self._respond("n")

    def _respond(self, response: str) -> None:
        if (self.is_mounted and self.is_attached) and self.app.screen is self and not self._resolved and not self._answered:
            self._answered = True
            self.dismiss(response)
