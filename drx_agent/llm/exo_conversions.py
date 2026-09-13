"""Pure conversion helpers for the EXO provider (no network, no SDK).

Everything here transforms between OpenAI chat shapes and EXO's Responses
API shapes so the provider stays thin and the conversions stay unit-testable.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

from drx_agent.llm.base import AgentEvent, AgentEventType


def _to_dict(obj) -> dict:
    """Coerce a value (dict / pydantic model / str) to a plain dict."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            result = dump()
        except Exception:
            result = None
        if isinstance(result, dict):
            return result
    if isinstance(obj, str):
        return {}
    try:
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    except TypeError:
        return {}


def _messages_to_responses_input(messages: list[dict]) -> tuple[list[dict], str]:
    """Convert OpenAI chat messages into Responses `input` items + `instructions`.

    System messages are folded into `instructions`; assistant tool_calls become
    `function_call` items; tool results become `function_call_output` items;
    everything else becomes a `message` item.
    """
    instructions: list[str] = []
    items: list[dict] = []
    for m in messages:
        role = m.get("role", "")
        if role == "system":
            content = m.get("content")
            if isinstance(content, str) and content.strip():
                instructions.append(content)
        elif role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": m.get("tool_call_id", ""),
                "output": m.get("content", ""),
            })
        elif role == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                items.append({
                    "type": "function_call",
                    "call_id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments", "{}"),
                })
            if m.get("content"):
                items.append({"type": "message", "role": "assistant", "content": m["content"]})
        else:
            items.append({"type": "message", "role": role or "user", "content": m.get("content", "")})
    return items, "\n\n".join(instructions)


def _tool_to_responses(tool: dict) -> dict:
    """Convert an OpenAI chat tool schema into the flat Responses tool shape.

    `{"type": "function", "function": {...}}` -> `{"type": "function",
    "name": ..., "description": ..., "parameters": ...}`. Already-flat
    schemas pass through unchanged.
    """
    if tool.get("type") == "function" and "function" in tool and "name" not in tool:
        fn = tool["function"]
        return {
            "type": "function",
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
        }
    return tool


def _parse_function_call(item: dict) -> tuple[str, dict]:
    name = item.get("name") or ""
    raw = item.get("arguments") or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {"_raw": raw}
    return name, parsed


def _usage_to_dict(usage) -> dict | None:
    """Normalize a Responses usage (object or dict) to chat-style token counts."""
    u = _to_dict(usage)
    if not u:
        return None
    out: dict = {}
    for key, sources in (
        ("prompt_tokens", ("prompt_tokens", "input_tokens")),
        ("completion_tokens", ("completion_tokens", "output_tokens")),
        ("total_tokens", ("total_tokens",)),
    ):
        for src in sources:
            v = u.get(src)
            if v is not None:
                out[key] = int(v)
                break
    details = _to_dict(u.get("input_tokens_details"))
    for out_key, src in (
        ("prompt_cache_hit_tokens", "cached_tokens"),
        ("prompt_cache_miss_tokens", "uncached_tokens"),
    ):
        v = details.get(src)
        if v is not None:
            out[out_key] = int(v)
    return out or None


def _output_items_to_events(output_items, finish_reason, usage, model) -> list[AgentEvent]:
    """Convert Responses output items into TEXT/TOOL_CALL/DONE AgentEvents.

    The DONE event carries an OpenAI-chat-format assistant_message so existing
    consumers can treat it like any other provider's output.
    """
    events: list[AgentEvent] = []
    text_parts: list[str] = []
    serialized_calls: list[dict] = []
    for raw_item in output_items or []:
        item = _to_dict(raw_item)
        itype = item.get("type")
        if itype == "message":
            for raw_part in item.get("content") or []:
                part = _to_dict(raw_part)
                if part.get("type") in ("output_text", "text"):
                    text = part.get("text") or ""
                    if text:
                        text_parts.append(text)
                        events.append(AgentEvent(type=AgentEventType.TEXT, content=text))
        elif itype == "function_call":
            name, parsed = _parse_function_call(item)
            call_id = item.get("call_id") or ""
            raw_args = item.get("arguments") or "{}"
            events.append(AgentEvent(
                type=AgentEventType.TOOL_CALL,
                tool_name=name,
                tool_input=parsed,
                metadata={"tool_call_id": call_id},
            ))
            serialized_calls.append({
                "id": call_id, "type": "function",
                "function": {"name": name, "arguments": raw_args},
            })

    assistant_message: dict = {"role": "assistant", "content": "".join(text_parts)}
    if serialized_calls:
        assistant_message["tool_calls"] = serialized_calls
    events.append(AgentEvent(
        type=AgentEventType.DONE,
        metadata={
            "finish_reason": finish_reason,
            "assistant_message": assistant_message,
            "usage": _usage_to_dict(usage),
            "model": model,
        },
    ))
    return events


def _parse_sse_data(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def parse_sse_lines(lines: AsyncIterator[str]):
    """Parse EXO's SSE framing (`event:` / `data:` line pairs).

    Yields (event_type, data_dict) tuples. Pure async generator — no network.
    """
    event_type = "message"
    data_lines: list[str] = []
    async for raw_line in lines:
        if raw_line is None:
            continue
        line = str(raw_line).rstrip("\r\n")
        if line == "":
            if data_lines:
                yield event_type, _parse_sse_data("".join(data_lines))
            event_type = "message"
            data_lines = []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event_type = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())
    if data_lines:
        yield event_type, _parse_sse_data("".join(data_lines))


def _sse_event_to_tuple(ev) -> tuple[str, dict]:
    """Normalize one SDK stream event into an (event_type, data_dict) tuple.

    Handles typed SDK models, raw ServerSentEvent-like objects, and dicts.
    """
    if isinstance(ev, dict):
        if "data" in ev:
            raw = ev["data"]
            return (
                ev.get("type") or ev.get("event") or "",
                _parse_sse_data(raw) if isinstance(raw, str) else _to_dict(raw),
            )
        return ev.get("type") or ev.get("event") or "", ev
    ev_type = getattr(ev, "type", None) or getattr(ev, "event", None) or ""
    data = getattr(ev, "data", None)
    if data is None:
        data = _to_dict(ev)
    elif isinstance(data, str):
        data = _parse_sse_data(data)
    else:
        data = _to_dict(data)
    return ev_type, data


async def _consume_event_stream(
    event_iter: AsyncIterator[tuple[str, dict]], model: str
) -> AsyncIterator[AgentEvent]:
    """Drive the shared stream state machine over (event_type, data) tuples.

    TEXT deltas stream as they arrive; function calls are accumulated and
    emitted after the stream completes; DONE carries an OpenAI-chat-format
    assistant_message.
    """
    text_parts: list[str] = []
    tool_slots: dict[str, dict] = {}
    tool_order: list[str] = []
    finish_reason = None
    usage = None
    error_msg: str | None = None

    async for ev_type, data in event_iter:
        if ev_type == "response.output_text.delta":
            delta = data.get("delta") or ""
            if delta:
                text_parts.append(delta)
                yield AgentEvent(type=AgentEventType.TEXT, content=delta)
        elif ev_type == "response.output_item.done":
            item = _to_dict(data.get("item"))
            if item.get("type") == "function_call":
                call_id = item.get("call_id") or ""
                if call_id not in tool_slots:
                    tool_order.append(call_id)
                tool_slots[call_id] = {
                    "name": item.get("name") or "",
                    "arguments": item.get("arguments") or "{}",
                }
        elif ev_type == "response.completed":
            resp = _to_dict(data.get("response")) or data
            finish_reason = resp.get("status")
            usage = _usage_to_dict(resp.get("usage"))
        elif ev_type == "response.failed":
            resp = _to_dict(data.get("response")) or data
            err = resp.get("error")
            error_msg = (
                str(err.get("message") or err)
                if isinstance(err, dict)
                else str(err or "response failed")
            )

    if error_msg:
        yield AgentEvent(type=AgentEventType.ERROR, content=error_msg)
        return

    serialized_calls: list[dict] = []
    for call_id in tool_order:
        slot = tool_slots[call_id]
        name, parsed = _parse_function_call(slot)
        yield AgentEvent(
            type=AgentEventType.TOOL_CALL,
            tool_name=name,
            tool_input=parsed,
            metadata={"tool_call_id": call_id},
        )
        serialized_calls.append({
            "id": call_id, "type": "function",
            "function": {"name": name, "arguments": slot["arguments"]},
        })

    assistant_message: dict = {"role": "assistant", "content": "".join(text_parts)}
    if serialized_calls:
        assistant_message["tool_calls"] = serialized_calls
    yield AgentEvent(
        type=AgentEventType.DONE,
        metadata={
            "finish_reason": finish_reason,
            "assistant_message": assistant_message,
            "usage": usage,
            "model": model,
        },
    )
