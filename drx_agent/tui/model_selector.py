"""Request-scoped model picker over public model metadata only."""

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Select, Static
from textual.widgets.option_list import Option


class ModelSelector(ModalScreen[str | None]):
    """Choose a concrete configured source/model key; the controller applies it."""

    BINDINGS = [
        Binding("escape", "cancel", "返回", show=False),
        Binding("up", "select_previous", "上一条", show=False, priority=True),
        Binding("down", "select_next", "下一条", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    ModelSelector { align: center middle; background: $background 85%; }
    ModelSelector #model-dialog {
        width: 94%; max-width: 100; height: 92%; max-height: 34;
        padding: 0 1; border: round $panel; background: $surface;
    }
    ModelSelector #model-title { height: 1; color: $primary; text-style: bold; }
    ModelSelector #model-current { height: 1; color: $text-muted; }
    ModelSelector #model-search, ModelSelector #model-provider { height: 3; }
    ModelSelector #model-list { height: 1fr; min-height: 3; border: none; }
    ModelSelector #model-status { height: auto; color: $text-muted; }
    ModelSelector #model-error { height: auto; max-height: 3; overflow-y: auto; color: $error; }
    ModelSelector #model-details { height: auto; max-height: 5; overflow-y: auto; color: $text; }
    ModelSelector #model-hint { height: auto; color: $text-muted; }
    """

    def __init__(self, snapshot: dict[str, Any]) -> None:
        super().__init__()
        self.request_id = str(snapshot.get("request_id", ""))
        self._snapshot = dict(snapshot)
        self._answered = False
        self._closed = False
        self._invalidated = False
        self._providers: list[str] = []
        self._visible_rows: dict[str, dict[str, Any]] = {}

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="model-dialog"):
            yield Static("选择模型", id="model-title", markup=False)
            yield Static(id="model-current", markup=False)
            yield Input(
                value=str(self._snapshot.get("query") or ""),
                placeholder="搜索模型、提供方或主机…", id="model-search",
            )
            yield Select([(Text("全部提供方"), "")], value="", allow_blank=False, id="model-provider")
            yield Static(id="model-status", markup=False)
            yield Static(id="model-error", markup=False)
            yield OptionList(id="model-list", markup=False)
            yield Static(id="model-details", markup=False)
            yield Static("↑↓ 选择 · Enter 确认 · Tab 切换筛选 · Esc 返回", id="model-hint", markup=False)

    def on_mount(self) -> None:
        if self._invalidated:
            self.call_after_refresh(self.invalidate)
        else:
            self._render_snapshot()
            self.query_one("#model-search", Input).focus()

    def on_unmount(self) -> None:
        self._closed = True

    def on_screen_resume(self) -> None:
        if self._invalidated and self.is_mounted and self.is_attached:
            self.call_after_refresh(self.invalidate)

    def invalidate(self) -> None:
        """Revoke this picker without dismissing a covering approval screen."""
        self._invalidated = True
        if not self._answered and self.is_mounted and self.is_attached and self.app.screen is self:
            self._answered = True
            self.dismiss(None)

    def update_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Refresh this request without resetting the user's query or selection."""
        if self._answered or self._closed or self._invalidated or str(snapshot.get("request_id", "")) != self.request_id:
            return
        self._snapshot = dict(snapshot)
        if self.is_mounted and self.is_attached:
            self._render_snapshot()

    def _render_snapshot(self) -> None:
        rows = self._snapshot.get("models") or []
        providers = sorted({str(row["provider"]) for row in rows})
        provider_filter = self.query_one("#model-provider", Select)
        if providers != self._providers:
            selected = provider_filter.value
            with self.prevent(Select.Changed):
                provider_filter.set_options([(Text("全部提供方"), "")] + [(Text(name), name) for name in providers])
                provider_filter.value = selected if selected in providers else ""
            self._providers = providers
        current_key = self._snapshot.get("current")
        current = next((row for row in rows if row["key"] == current_key), None)
        current_label = f"{current['provider']} / {current['model']}" if current else (current_key or "未选择")
        self.query_one("#model-current", Static).update(f"当前: {current_label}")
        error = str(self._snapshot.get("error") or "")
        error_widget = self.query_one("#model-error", Static)
        error_widget.update(error)
        error_widget.display = bool(error)
        self._filter_models()

    def _filter_models(self) -> None:
        options = self.query_one("#model-list", OptionList)
        previous = self._highlighted_key()
        terms = self.query_one("#model-search", Input).value.casefold().split()
        provider = self.query_one("#model-provider", Select).value
        rows = [
            row for row in self._snapshot.get("models", [])
            if (not provider or row["provider"] == provider)
            and all(term in " ".join(str(row.get(field) or "") for field in ("key", "provider", "model", "endpoint")).casefold() for term in terms)
        ]
        self._visible_rows = {row["key"]: row for row in rows}
        current = self._snapshot.get("current")
        with self.prevent(OptionList.OptionHighlighted):
            options.clear_options()
            options.add_options(
                Option(Text(f"{'* ' if row['key'] == current else '  '}{row['model']}  ·  {row['provider']}"), id=row["key"])
                for row in rows
            )
            keys = list(self._visible_rows)
            preferred = previous if previous in self._visible_rows else current
            options.highlighted = keys.index(preferred) if preferred in self._visible_rows else (0 if rows else None)
        options.scroll_to_highlight()
        loading = self._snapshot.get("state") == "loading"
        if rows:
            status = f"{len(rows)} 个模型 · * 当前模型"
            if loading:
                status += " · 正在发现模型，可选择已列出的模型…"
        elif loading:
            status = "正在发现模型… 暂无匹配模型"
        else:
            status = "无匹配模型 · 修改搜索或提供方筛选" if self._snapshot.get("models") else "暂无可用模型"
        self.query_one("#model-status", Static).update(status)
        self._render_details()

    def _highlighted_key(self) -> str | None:
        options = self.query_one("#model-list", OptionList)
        if options.highlighted is None or options.highlighted >= options.option_count:
            return None
        return options.get_option_at_index(options.highlighted).id

    def _render_details(self) -> None:
        row = self._visible_rows.get(self._highlighted_key() or "")
        details = ""
        if row is not None:
            context = row.get("context_window")
            output = row.get("max_tokens")
            details = (
                f"{row['key']}\n"
                f"提供方: {row['provider']} · 主机: {row.get('endpoint') or '—'}\n"
                f"上下文: {context if context is not None else '未知'} · "
                f"最大输出能力: {output if output is not None else '未知'} tokens"
            )
        self.query_one("#model-details", Static).update(details)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "model-search":
            event.stop()
            if self.is_mounted and self.is_attached and not self._answered:
                self._filter_models()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "model-provider":
            event.stop()
            if self.is_mounted and self.is_attached and not self._answered:
                self._filter_models()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("select_previous", "select_next"):
            if not (self.is_mounted and self.is_attached) or self._answered or self._invalidated or self.app.screen is not self:
                return False
            provider_filter = self.query_one("#model-provider", Select)
            return not (provider_filter.has_focus or provider_filter.expanded)
        return super().check_action(action, parameters)

    def action_select_previous(self) -> None:
        self._move_selection(-1)

    def action_select_next(self) -> None:
        self._move_selection(1)

    def _move_selection(self, delta: int) -> None:
        if self._answered or self._invalidated or not (self.is_mounted and self.is_attached) or self.app.screen is not self:
            return
        options = self.query_one("#model-list", OptionList)
        if options.option_count:
            options.highlighted = ((options.highlighted or 0) + delta) % options.option_count
            options.scroll_to_highlight()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id == "model-list":
            event.stop()
            if self.is_mounted and self.is_attached and not self._answered:
                self._render_details()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "model-list":
            event.stop()
            self._choose(event.option.id)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "model-search":
            event.stop()
            self._choose(self._highlighted_key())

    def _choose(self, key: str | None) -> None:
        if key not in self._visible_rows or self._answered or self._invalidated or not (self.is_mounted and self.is_attached) or self.app.screen is not self:
            return
        self._answered = True
        self.dismiss(key)

    def action_cancel(self) -> None:
        if not self._answered and self.is_mounted and self.is_attached and self.app.screen is self:
            self._answered = True
            self.dismiss(None)
