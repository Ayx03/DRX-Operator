TAIL_SENTINEL = -1


class ScrollState:
    """Flat line-offset scroll; offset ``-1`` pins the view to the tail."""

    def __init__(self):
        self._offset: int = TAIL_SENTINEL

    @property
    def is_tailing(self) -> bool:
        return self._offset == TAIL_SENTINEL

    @property
    def offset(self) -> int:
        return max(0, self._offset) if self._offset != TAIL_SENTINEL else 0

    def resolve_offset(self, visible_lines: int, total_lines: int) -> int:
        max_offset = max(0, total_lines - max(0, visible_lines))
        if self.is_tailing:
            return max_offset
        self._offset = min(max(0, self._offset), max_offset)
        return self._offset

    def scroll_up(self, delta: int, visible_lines: int, total_lines: int) -> None:
        self._offset = max(0, self.resolve_offset(visible_lines, total_lines) - max(0, delta))

    def scroll_down(self, delta: int, visible_lines: int, total_lines: int) -> None:
        if self._offset == TAIL_SENTINEL:
            return
        max_offset = max(0, total_lines - max(0, visible_lines))
        self._offset = min(max_offset, self.resolve_offset(visible_lines, total_lines) + max(0, delta))
        if self._offset >= max_offset:
            self._offset = TAIL_SENTINEL

    def page_up(self, visible_lines: int, total_lines: int) -> None:
        self.scroll_up(visible_lines, visible_lines, total_lines)

    def page_down(self, visible_lines: int, total_lines: int) -> None:
        self.scroll_down(visible_lines, visible_lines, total_lines)

    def scroll_to_bottom(self) -> None:
        self._offset = TAIL_SENTINEL

    def scroll_to_top(self) -> None:
        self._offset = 0

    def on_new_content(self, visible_lines: int, total_lines: int) -> int:
        """Append-only content preserves the reader's line, unless tailing."""
        return self.resolve_offset(visible_lines, total_lines)
