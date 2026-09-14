"""Compact startup identity and conversation guidance."""

from rich.text import Text


def build_banner() -> Text:
    out = Text("DRX-Operator", style="bold #53d7c3")
    out.append("  by BushSEC · github.com/BushANQ\n", style="#92a4bb")
    out.append("描述目标开始对话，输入 / 查看命令。", style="#e7edf7")
    return out
