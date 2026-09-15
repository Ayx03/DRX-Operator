"""EXO-Booster LLM provider speaking the QWEN-EXO OpenAI Responses API.

Transport: the `openai` SDK (AsyncOpenAI) via `client.responses.create(...)`.

Auth behavior (verified against openai==2.26.0, the installed version):
  - With an empty `api_key`, the SDK sends NO Authorization header on the
    wire. This matches EXO's optional-auth contract (unauthenticated
    servers ignore the header; an empty `Bearer ` header is never sent).
  - openai<2 rejects empty keys at construction; for those versions we
    fall back to a neutral "exo-local" placeholder, which EXO ignores.
  - With a real key, `Authorization: Bearer <key>` is sent as usual.

EXO has no documented public session-binding field, so `session_binding`
stays False and no session field is ever sent to the server.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import aclosing
from dataclasses import dataclass
from urllib.parse import urlparse

from drx_agent.llm.base import (
    AgentEvent,
    AgentEventType,
    LLMConfig,
    LLMError,
    LLMProvider,
)
from drx_agent.llm.exo_conversions import (
    _close_stream,
    _consume_event_stream,
    _messages_to_responses_input,
    _output_items_to_events,
    _sse_event_to_tuple,
    _tool_to_responses,
)
from drx_agent.llm.output_tokens import resolve_output_token_params


@dataclass
class ExoCapabilities:
    """What a given EXO server supports, as discovered from its control plane.

    All fields default to False (all-safe); discovery only flips flags that
    were actually verified against the server.
    """

    responses_api: bool = False
    native_tools: bool = False
    session_binding: bool = False
    recall_trace: bool = False
    telemetry: bool = False
    reflection_control: bool = False


class EXOProvider(LLMProvider):
    def __init__(self, config: LLMConfig, control_url: str = ""):
        self.config = config
        self.control_url = (
            control_url or os.environ.get("DRX_EXO_CONTROL_URL", "")
        ).strip()
        try:
            from openai import AsyncOpenAI
        except ImportError:
            raise LLMError("openai package not installed. Run: pip install openai")
        kwargs = {"api_key": config.api_key, "timeout": 120.0, "max_retries": 0}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        try:
            self.client = AsyncOpenAI(**kwargs)
        except Exception as exc:
            if config.api_key or "api_key" not in str(exc).lower():
                raise LLMError(f"failed to initialize EXO client: {exc}")
            self.client = AsyncOpenAI(**{**kwargs, "api_key": "exo-local"})

    async def discover_capabilities(self) -> ExoCapabilities:
        """Probe the EXO control plane and report verified capabilities.

        Never raises: any failure returns all-default (all-safe) flags.
        session_binding stays False — EXO documents no public session field.
        """
        control = self.control_url.rstrip("/")
        if not control:
            return ExoCapabilities()
        caps = ExoCapabilities()
        try:
            import httpx
            async with httpx.AsyncClient(timeout=3.0) as client:
                for path, flag in (
                    ("status", None),
                    ("health", None),
                    ("recall-trace", "recall_trace"),
                    ("telemetry", "telemetry"),
                ):
                    try:
                        resp = await client.get(f"{control}/qwen-exo/{path}")
                    except httpx.HTTPError:
                        continue
                    if not (200 <= resp.status_code < 400):
                        continue
                    if flag:
                        setattr(caps, flag, True)
                    else:
                        caps.responses_api = True
                        caps.native_tools = True
        except Exception:
            return ExoCapabilities()
        return caps

    async def chat(self, messages, tools=None, stream=True):
        try:
            input_items, instructions = _messages_to_responses_input(messages)
            kwargs = {
                "model": self.config.model,
                "input": input_items if input_items else "",
                "temperature": self.config.temperature,
            }
            kwargs.update(resolve_output_token_params(
                self.config, api="responses", base_url=str(self.client.base_url),
            ))
            if instructions:
                kwargs["instructions"] = instructions
            if tools:
                kwargs["tools"] = [_tool_to_responses(t) for t in tools]
                kwargs["tool_choice"] = "auto"

            if stream:
                async with aclosing(self._stream(kwargs)) as events:
                    async for ev in events:
                        yield ev
                return

            response = await self.client.responses.create(**kwargs)
            for ev in _output_items_to_events(
                getattr(response, "output", None),
                getattr(response, "status", None),
                getattr(response, "usage", None),
                self.config.model,
            ):
                if ev.type == AgentEventType.DONE:
                    ev.metadata["provider"] = urlparse(str(self.client.base_url)).hostname
                yield ev
        except Exception as e:
            yield AgentEvent(type=AgentEventType.ERROR, content=str(e))

    async def _stream(self, kwargs):
        stream = await self.client.responses.create(**kwargs, stream=True)

        async def _tuples():
            source_failed = False
            try:
                async for ev in stream:
                    yield _sse_event_to_tuple(ev)
            except (Exception, asyncio.CancelledError):
                source_failed = True
                raise
            finally:
                try:
                    await _close_stream(stream)
                except Exception:
                    if not source_failed:
                        raise

        async with aclosing(_consume_event_stream(_tuples(), self.config.model)) as events:
            async for ev in events:
                if ev.type in (AgentEventType.DONE, AgentEventType.ERROR):
                    ev.metadata["provider"] = urlparse(str(self.client.base_url)).hostname
                yield ev

    def count_tokens(self, messages):
        return sum(len(m.get("content", "") or "") // 4 for m in messages)
