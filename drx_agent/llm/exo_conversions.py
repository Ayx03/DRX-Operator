"""Pure conversion helpers for the EXO provider (no network, no SDK).

Everything here transforms between OpenAI chat shapes and EXO's Responses
API shapes so the provider stays thin and the conversions stay unit-testable.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import AsyncIterator

from drx_agent.llm.base import AgentEvent, AgentEventType
from drx_agent.llm.deepseek_provider import _extract_usage


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
    out = dict(u)
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
    # Responses names its input detail object differently from Chat Completions.
    if "prompt_tokens_details" not in out:
        out["prompt_tokens_details"] = u.get("input_tokens_details")
    details = _to_dict(u.get("input_tokens_details"))
    if out.get("prompt_cache_miss_tokens") is None and details.get("uncached_tokens") is not None:
        out["prompt_cache_miss_tokens"] = details["uncached_tokens"]
    return _extract_usage(out)


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


async def _close_stream(stream) -> None:
    """Close an owned async source without abandoning cleanup on cancellation."""
    close = getattr(stream, "aclose", None) or getattr(stream, "close", None)
    if close is None:
        return
    closing = close()
    if not inspect.isawaitable(closing):
        return
    task = asyncio.ensure_future(closing)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            # Retrieve the failure below so an earlier cancellation still wins.
            break
    try:
        task.result()
    finally:
        if cancelled:
            raise asyncio.CancelledError


async def parse_sse_lines(lines: AsyncIterator[str]):
    """Parse EXO's SSE framing and own the underlying line iterator."""
    event_type = "message"
    data_lines: list[str] = []
    source_failed = False
    try:
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
    except (Exception, asyncio.CancelledError):
        source_failed = True
        raise
    finally:
        try:
            await _close_stream(lines)
        except Exception:
            if not source_failed:
                raise


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
    """Stream deltas, then close at the protocol terminal event, not TCP EOF.

    Only completed function-call items are exposed for execution. Failed or
    truncated responses retain their partial assistant message in ERROR metadata.
    """
    text_parts: list[str] = []
    tool_slots: dict[str, dict] = {}
    tool_order: dict[str, int] = {}
    finish_reason = None
    usage = None
    error_msg = "EXO stream ended before a terminal response event"

    def record_call(item: dict, output_index=None) -> None:
        if item.get("type") != "function_call" or item.get("status") not in (None, "completed"):
            return
        call_id = item.get("call_id") or ""
        if isinstance(output_index, int) or call_id not in tool_order:
            tool_order[call_id] = (
                output_index if isinstance(output_index, int) else len(tool_order)
            )
        tool_slots[call_id] = item

    try:
        async for ev_type, data in event_iter:
            if ev_type == "response.output_text.delta":
                delta = data.get("delta") or ""
                if delta:
                    text_parts.append(delta)
                    yield AgentEvent(type=AgentEventType.TEXT, content=delta)
            elif ev_type == "response.output_item.done":
                record_call(_to_dict(data.get("item")), data.get("output_index"))
            elif ev_type in ("response.completed", "response.failed", "response.incomplete"):
                resp = _to_dict(data.get("response")) or data
                finish_reason = resp.get("status") or ev_type.removeprefix("response.")
                usage = _usage_to_dict(resp.get("usage"))
                if ev_type == "response.completed" and finish_reason == "completed":
                    error_msg = ""
                    for index, raw_item in enumerate(resp.get("output") or []):
                        record_call(_to_dict(raw_item), index)
                else:
                    err = resp.get("error") or resp.get("incomplete_details")
                    error_msg = (
                        str(err.get("message") or err)
                        if isinstance(err, dict)
                        else str(err or f"response {finish_reason}")
                    )
                break
            elif ev_type == "error":
                err = data.get("error") or data
                error_msg = (
                    str(err.get("message") or err)
                    if isinstance(err, dict) else str(err)
                )
                finish_reason = "failed"
                break
    except Exception as exc:
        error_msg = str(exc) or type(exc).__name__
    finally:
        # Consumers often stop after DONE/ERROR; release the source before yield.
        try:
            await _close_stream(event_iter)
        except Exception as exc:
            # A successful response is not usable until cleanup succeeds. Keep
            # any primary protocol/transport error and all accumulated output.
            if not error_msg:
                error_msg = str(exc) or type(exc).__name__

    serialized_calls: list[dict] = []
    tool_events: list[AgentEvent] = []
    for call_id in sorted(tool_order, key=tool_order.get):
        slot = tool_slots[call_id]
        name, parsed = _parse_function_call(slot)
        tool_events.append(AgentEvent(
            type=AgentEventType.TOOL_CALL,
            tool_name=name,
            tool_input=parsed,
            metadata={"tool_call_id": call_id},
        ))
        serialized_calls.append({
            "id": call_id, "type": "function",
            "function": {"name": name, "arguments": slot.get("arguments") or "{}"},
        })

    assistant_message: dict = {"role": "assistant", "content": "".join(text_parts)}
    if serialized_calls:
        assistant_message["tool_calls"] = serialized_calls
    metadata = {
        "finish_reason": finish_reason,
        "assistant_message": assistant_message,
        "usage": usage,
        "model": model,
    }
    if error_msg:
        yield AgentEvent(type=AgentEventType.ERROR, content=error_msg, metadata=metadata)
        return
    for event in tool_events:
        yield event
    yield AgentEvent(type=AgentEventType.DONE, metadata=metadata)
