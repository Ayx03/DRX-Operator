"""Searchable, literal conversation record, with full-transcript copying."""

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Button, Input, Static


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
            elif block.get("type") == "image_url":
                parts.append("[image attached]")
            else:
                parts.append(str(block.get("text", "")))
        return "\n".join(part for part in parts if part)
    if content is None:
        return ""
    return str(content)


class TranscriptScreen(ModalScreen[None]):
    """Filter readable turns without losing the complete copyable transcript."""

    BINDINGS = [
        Binding("escape", "pop_screen", "返回", show=False),
        Binding("ctrl+f", "focus_search", "搜索", show=False),
        Binding("ctrl+shift+c", "copy_full", "复制完整记录", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    TranscriptScreen {
        align: center middle;
        background: #0b1020 85%;
    }
    TranscriptScreen #transcript-dialog {
        width: 96%;
        max-width: 120;
        height: 95%;
        background: #111a2c;
        border: round #26354b;
        padding: 0 1;
    }
    TranscriptScreen #transcript-title {
        height: 2;
        content-align: left middle;
        text-style: bold;
        color: #53d7c3;
    }
    TranscriptScreen #transcript-search {
        height: 3;
    }
    TranscriptScreen #transcript-count {
        height: 1;
        color: #92a4bb;
    }
    TranscriptScreen #transcript-scroll {
        height: 1fr;
        padding: 1 0;
    }
    TranscriptScreen #transcript-content {
        height: auto;
    }
    TranscriptScreen #transcript-actions {
        height: 3;
    }
    TranscriptScreen Button {
        min-width: 8;
        margin-right: 1;
    }
    TranscriptScreen #transcript-hint {
        height: 1;
        color: #92a4bb;
    }
    """

    def __init__(self, app: object) -> None:
        super().__init__()
        self._app = app
        self.transcript_text = ""
        self._entries: list[str] = []
        self._query = ""
        self._refresh_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="transcript-dialog"):
            yield Static("会话记录", id="transcript-title", markup=False)
            yield Input(placeholder="筛选关键词（不区分大小写）", id="transcript-search")
            yield Static(id="transcript-count", markup=False)
            with VerticalScroll(id="transcript-scroll"):
                yield Static(id="transcript-content", markup=False)
            with Horizontal(id="transcript-actions"):
                yield Button("复制完整记录", id="transcript-copy", variant="primary")
                yield Button("返回", id="transcript-close")
            yield Static("Ctrl+F 搜索 · Esc 返回", id="transcript-hint", markup=False)

    def on_mount(self) -> None:
        self._refresh()
        self.action_focus_search()
        # The authoritative message list also changes during streaming, without
        # a dedicated transcript event. Poll on the UI thread, never in bus callbacks.
        self._refresh_timer = self.set_interval(1.0, self._refresh)

    def on_unmount(self) -> None:
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
            self._refresh_timer = None

    def on_screen_resume(self) -> None:
        if self.is_mounted:
            if self._refresh_timer is not None:
                self._refresh_timer.resume()
            self._refresh()
            self.action_focus_search()

    def on_screen_suspend(self) -> None:
        if self._refresh_timer is not None:
            self._refresh_timer.pause()

    def action_pop_screen(self) -> None:
        self.dismiss(None)

    def action_focus_search(self) -> None:
        self.query_one("#transcript-search", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "transcript-search":
            self._query = event.value.strip()
            self._render_filtered()
            self.query_one("#transcript-scroll", VerticalScroll).scroll_home(animate=False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "transcript-search":
            event.stop()
            self.query_one("#transcript-scroll", VerticalScroll).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "transcript-copy":
            event.stop()
            self.action_copy_full()
        elif event.button.id == "transcript-close":
            event.stop()
            self.action_pop_screen()

    def action_copy_full(self) -> None:
        self._refresh()
        copier = getattr(self._app, "_copy_text", None)
        if callable(copier):
            copier(self.transcript_text, "复制完整记录")
        else:
            self.app.copy_to_clipboard(self.transcript_text)
            self.notify("已发送完整记录到终端剪贴板", title="复制")

    def _collect_messages(self) -> list[dict[str, object]]:
        agent = getattr(self._app, "drx_agent", None)
        master = getattr(agent, "master", None)
        return list(getattr(master, "messages", None) or [])

    @staticmethod
    def _readable_entries(messages: list[dict[str, object]]) -> list[str]:
        entries: list[str] = []
        for msg in messages or []:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role", "")).strip().lower()
            if role == "user":
                prefix = "用户"
            elif role == "assistant":
                prefix = "Agent"
            else:
                continue
            text = _message_text(msg.get("content")).strip()
            if text:
                entries.append(f"{prefix}: {text}")
        return entries

    def _build_transcript_text(self, messages: list[dict[str, object]]) -> str:
        return "\n\n".join(self._readable_entries(messages)) or "（暂无会话记录）"

    def _refresh(self) -> None:
        entries = self._readable_entries(self._collect_messages())
        if entries == self._entries and self.transcript_text:
            return
        self._entries = entries
        self.transcript_text = "\n\n".join(entries) or "（暂无会话记录）"
        self._render_filtered()

    def _render_filtered(self) -> None:
        needle = self._query.casefold()
        matches = [entry for entry in self._entries if needle in entry.casefold()]
        content = Text("\n\n".join(matches) or ("（无匹配记录）" if needle else "（暂无会话记录）"))
        if needle:
            content.highlight_words([self._query], style="bold #53d7c3", case_sensitive=False)
        self.query_one("#transcript-content", Static).update(content)
        self.query_one("#transcript-count", Static).update(
            f"{len(matches)} / {len(self._entries)} 条消息"
            + (f" · {sum(entry.casefold().count(needle) for entry in matches)} 处匹配" if needle else "")
        )
