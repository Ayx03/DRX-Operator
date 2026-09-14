"""Search and copy the same lossless records used by live conversation cards."""

from bisect import bisect_left
import threading
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.timer import Timer
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from drx_agent.tui.transcript import TranscriptLog, record_text


class TranscriptScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "pop_screen", "返回", show=False),
        Binding("ctrl+f", "focus_search", "搜索", show=False),
        Binding("ctrl+c", "copy_visible", "复制选中 / 筛选", show=False),
        Binding("ctrl+shift+c", "copy_full", "复制完整记录", show=False, priority=True),
    ]
    DEFAULT_CSS = """
    TranscriptScreen { align: center middle; background: $background 85%; }
    TranscriptScreen #transcript-dialog {
        width: 96%; max-width: 120; height: 95%;
        background: $surface; border: round $panel; padding: 0 1;
    }
    TranscriptScreen #transcript-title { height: 2; text-style: bold; color: $primary; }
    TranscriptScreen #transcript-search { height: 3; }
    TranscriptScreen #transcript-count { height: 1; color: $text-muted; }
    TranscriptScreen #transcript-scroll { height: 1fr; padding: 1 0; }
    TranscriptScreen #transcript-content { height: auto; color: $text; }
    TranscriptScreen #transcript-actions { height: auto; }
    TranscriptScreen Button { min-width: 8; width: auto; margin-right: 1; }
    TranscriptScreen #transcript-hint { height: auto; color: $text-muted; }
    """

    def __init__(self, app: object) -> None:
        super().__init__()
        self._app = app
        self.transcript: TranscriptLog = app.transcript
        self._query = ""
        self._revision = -1
        self._entries: list[str] = []
        self._entry_indices: dict[str, int] = {}
        self._filter_query = ""
        self._matched_indices: list[int] = []
        self._matched_length = 0
        self._dirty = threading.Event()
        self._timer: Timer | None = None
        self._dismiss_requested = False

    @property
    def transcript_text(self) -> str:
        """Build the full visible snapshot only when explicitly requested."""
        return "\n\n".join(self._entries)

    def compose(self) -> ComposeResult:
        with Vertical(id="transcript-dialog"):
            yield Static("会话记录", id="transcript-title", markup=False)
            yield Input(placeholder="搜索消息、工具完整输入 / 输出、审批、协作", id="transcript-search")
            yield Static(id="transcript-count", markup=False)
            with VerticalScroll(id="transcript-scroll"):
                yield Static(id="transcript-content", markup=False)
            with Horizontal(id="transcript-actions"):
                yield Button("复制选中 / 筛选", id="transcript-copy", variant="primary")
                yield Button("复制全部", id="transcript-copy-full")
                yield Button("返回", id="transcript-close")
            yield Static("Ctrl+F 搜索 · Ctrl+C 复制选中或筛选 · Ctrl+Shift+C 全部 · Esc 返回",
                         id="transcript-hint", markup=False)

    def on_mount(self) -> None:
        self._dismiss_requested = False
        self._revision = -1
        self.transcript.subscribe(self._log_changed)
        self._dirty.set()
        self._timer = self.set_interval(0.1, self._refresh)
        self._refresh()
        self.action_focus_search()

    def on_unmount(self) -> None:
        self._dismiss_requested = True
        self.transcript.unsubscribe(self._log_changed)
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._dirty.clear()

    def on_screen_resume(self) -> None:
        if self.is_attached and self.is_mounted and self.is_current:
            self._dismiss_requested = False
            self._refresh()
            self.action_focus_search()

    def _log_changed(self, record_id: str | None) -> None:
        # A bounded wakeup flag, not one queued UI message per streaming token.
        # The next visible refresh reads one atomic, revisioned snapshot.
        self._dirty.set()

    def action_pop_screen(self) -> None:
        if self._dismiss_requested or not self.is_attached or not self.is_current:
            return
        self._dismiss_requested = True
        self.dismiss(None)

    def action_focus_search(self) -> None:
        if not self.is_attached or not self.is_current or self._dismiss_requested:
            return
        self.query_one("#transcript-search", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if self.is_attached and not self._dismiss_requested and event.input.id == "transcript-search":
            self._query = event.value.strip()
            self._render_filtered()
            self.query_one("#transcript-scroll", VerticalScroll).scroll_home(animate=False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self.is_attached and not self._dismiss_requested and event.input.id == "transcript-search":
            event.stop()
            self.query_one("#transcript-scroll", VerticalScroll).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "transcript-copy":
            event.stop()
            self.action_copy_visible()
        elif event.button.id == "transcript-copy-full":
            event.stop()
            self.action_copy_full()
        elif event.button.id == "transcript-close":
            event.stop()
            self.action_pop_screen()

    def _copy(self, text: str, title: str) -> None:
        if not text:
            self.notify("没有可复制的记录")
            return
        copier = getattr(self._app, "_copy_text", None)
        if callable(copier):
            copier(text, title)
        else:
            self.app.copy_to_clipboard(text)

    def action_copy_visible(self) -> None:
        if self._dismiss_requested or not self.is_attached or not self.is_current:
            return
        selected = self.get_selected_text()
        self._copy(selected or self.transcript.render_text(self._query), "复制选中 / 筛选记录")

    def action_copy_full(self) -> None:
        if self._dismiss_requested or not self.is_attached or not self.is_current:
            return
        self._copy(self.transcript.render_text(), "复制完整记录")

    def _refresh(self) -> None:
        if (not self.is_attached or not self.is_mounted or not self.is_current
                or self._dismiss_requested or not self._dirty.is_set()):
            return
        self._dirty.clear()
        revision, reset, records = self.transcript.snapshot(self._revision)
        if revision == self._revision:
            return
        self._revision = revision
        if reset:
            self._entries.clear()
            self._entry_indices.clear()
            self._matched_indices.clear()
            self._matched_length = 0
        for record in records:
            identity, text = record["id"], record_text(record)
            index = self._entry_indices.get(identity)
            if index is None:
                index = len(self._entries)
                self._entry_indices[identity] = index
                self._entries.append("")
            position = bisect_left(self._matched_indices, index)
            was_match = (position < len(self._matched_indices)
                         and self._matched_indices[position] == index)
            is_match = not self._filter_query or self._filter_query in text.casefold()
            if was_match:
                self._matched_length -= len(self._entries[index])
            if is_match:
                self._matched_length += len(text)
            if was_match and not is_match:
                del self._matched_indices[position]
            elif is_match and not was_match:
                self._matched_indices.insert(position, index)
            self._entries[index] = text
        self._render_filtered()

    def _render_filtered(self) -> None:
        if not self.is_attached or not self.is_mounted or self._dismiss_requested:
            return
        needle = self._query.casefold()
        if needle != self._filter_query:
            self._filter_query = needle
            self._matched_indices.clear()
            self._matched_length = 0
            for index, entry in enumerate(self._entries):
                if not needle or needle in entry.casefold():
                    self._matched_indices.append(index)
                    self._matched_length += len(entry)
        # Ordinary stream updates replace only changed entries and their lengths.
        # Sorted indices preserve transcript order even when an old record starts
        # matching. Membership transitions may shift indices, but rendering walks
        # only the bounded tail; explicit query changes and copy read all data.
        limit = 60_000
        parts = []
        length = 0
        for index in reversed(self._matched_indices):
            if length >= limit:
                break
            part = self._entries[index][-(limit - length):]
            parts.append(part)
            length += len(part) + 2
        rendered = "\n\n".join(reversed(parts))
        count = len(self._matched_indices)
        if self._matched_length + max(0, count - 1) * 2 > limit:
            rendered = "（仅显示筛选结果末尾；复制筛选 / 全部保留完整记录）\n\n" + rendered
        content = Text(rendered or ("（无匹配记录）" if needle else "（暂无会话记录）"))
        if needle:
            content.highlight_words([self._query], style="bold reverse", case_sensitive=False)
        self.query_one("#transcript-content", Static).update(content)
        self.query_one("#transcript-count", Static).update(f"{count} / {len(self._entries)} 条记录")
