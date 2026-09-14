"""SubAgent: a self-contained ReAct loop with isolated message history.

Receives a task from the master, runs its own LLM + tool loop using the
parent's tool executor, publishes SUB_AGENT_DISPATCH / SUB_AGENT_RESULT."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from drx_agent.event_bus import Activity, Event, EventBus, EventType, activity_model_stream

logger = logging.getLogger(__name__)


class SubAgentStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    TIMEOUT = "timeout"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class SubAgentResult:
    agent_id: str
    status: SubAgentStatus
    findings: list = field(default_factory=list)
    scripts_executed: int = 0
    new_targets: list = field(default_factory=list)
    error: str = ""
    text: str = ""


ToolExecutor = Callable[[str, dict], Awaitable[str]]


async def _wait_owned(awaitable: Awaitable, timeout: float | None):
    """Bound owned work, then join its cleanup even across repeated cancellation."""
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            raise asyncio.TimeoutError
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            drain = asyncio.gather(task, return_exceptions=True)
            cancelled = False
            while not drain.done():
                try:
                    await asyncio.shield(drain)
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError
        elif not task.cancelled():
            task.exception()


class SubAgent:
    """A self-contained ReAct loop with isolated message history."""

    def __init__(
        self,
        agent_type: str,
        target: str,
        task: str,
        event_bus: EventBus,
        llm_provider: Any = None,
        tool_executor: Optional[ToolExecutor] = None,
        tool_schemas: Optional[list[dict]] = None,
        system_prompt: str = "",
        ttl: float = 300,
        max_iterations: int = 12,
        parallel_tool_calls: bool = True,
        usage_callback: Optional[Callable[..., None]] = None,
        llm_call_timeout: float = 600.0,
        notification_provider: Optional[Callable[[], str]] = None,
        stop_on_pattern: Optional[re.Pattern] = None,
    ) -> None:
        self.agent_id = f"{agent_type}-{uuid.uuid4().hex[:12]}"
        self.agent_type = agent_type
        self.target = target
        self.task = task
        self.event_bus = event_bus
        self.llm_provider = llm_provider
        self.tool_executor = tool_executor
        self.tool_schemas = tool_schemas or []
        self.system_prompt = system_prompt
        self.ttl = ttl
        self.max_iterations = max_iterations
        self.parallel_tool_calls = parallel_tool_calls
        self.usage_callback = usage_callback
        self.llm_call_timeout = llm_call_timeout
        self.notification_provider = notification_provider
        self.stop_on_pattern = stop_on_pattern
        self.status = SubAgentStatus.QUEUED
        self._interrupt = False
        self.started_at: float | None = None
        self.last_activity_at: float | None = None
        self._activity: Activity | None = None

    def queue(self) -> None:
        self._activity = Activity(
            self.event_bus, "worker", f"{self.agent_type} · {self.target}",
            agent_id=self.agent_id, state="queued",
        )
        self._publish_dispatch()

    def _publish_dispatch(self) -> None:
        self.event_bus.publish(Event(EventType.SUB_AGENT_DISPATCH, {
            "agent_id": self.agent_id, "type": self.agent_type, "role": self.agent_type,
            "target": self.target, "task": self.task, "status": self.status.value,
            "text": "", "error": "",
        }))

    def publish_result(self, result: SubAgentResult) -> None:
        self.event_bus.publish(Event(EventType.SUB_AGENT_RESULT, {
            "agent_id": self.agent_id, "type": self.agent_type, "role": self.agent_type,
            "target": self.target, "task": self.task, "status": result.status.value,
            "scripts_executed": result.scripts_executed,
            "text": result.text, "error": result.error,
        }))

    def request_stop(self) -> None:
        """Cooperative stop; the owning task should also be cancelled."""
        self._interrupt = True
        if self._activity is not None:
            self._activity.update("stopping")

    def _mark_cancelled(self, error_seen: str) -> str:
        self.status = SubAgentStatus.CANCELLED
        return error_seen or "interrupted"

    async def run(self) -> SubAgentResult:
        self.status = SubAgentStatus.RUNNING
        started = asyncio.get_running_loop().time()
        self.started_at = self.last_activity_at = time.time()
        if self._activity is None:
            self._activity = Activity(
                self.event_bus, "worker", f"{self.agent_type} · {self.target}",
                agent_id=self.agent_id,
            )
        self._activity.update("running")
        self._publish_dispatch()
        result = SubAgentResult(agent_id=self.agent_id, status=self.status)

        try:
            if self._interrupt:
                result.error = self._mark_cancelled(result.error)
            # No-LLM fallback (used by /scan, /exploit and unit tests).
            elif self.llm_provider is None or self.tool_executor is None:
                self.status = SubAgentStatus.DONE
                result.scripts_executed = 1
            else:
                remaining = (
                    max(0.0, self.ttl - (asyncio.get_running_loop().time() - started))
                    if self.ttl else None
                )
                await _wait_owned(self._react_loop(result), remaining)
        except asyncio.TimeoutError:
            self.status = SubAgentStatus.TIMEOUT
            result.error = f"ttl ({self.ttl}s) exceeded"
        except asyncio.CancelledError:
            result.error = self._mark_cancelled(result.error)
        except Exception as exc:
            self.status = SubAgentStatus.ERROR
            result.error = str(exc)
            raise
        finally:
            if self.status == SubAgentStatus.RUNNING:
                self.status = SubAgentStatus.DONE
            result.status = self.status
            self.publish_result(result)
            self._activity.update(
                "done" if self.status is SubAgentStatus.DONE else
                "cancelled" if self.status is SubAgentStatus.CANCELLED else "error",
                output=bool(result.text or result.error),
            )
        return result

    async def _react_loop(self, result: SubAgentResult) -> None:
        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": self.task},
        ]

        for _iteration in range(self.max_iterations):
            if self._interrupt:
                result.error = self._mark_cancelled(result.error)
                break

            if self.notification_provider is not None:
                try:
                    note = self.notification_provider() or ""
                except Exception:
                    logger.exception("Sub-agent %s notification_provider failed", self.agent_id)
                    note = ""
                if note:
                    messages.append(
                        {"role": "user", "content": "<新论坛通知>\n" + note}
                    )

            text_parts: list[str] = []
            pending_calls: list[dict] = []
            assistant_msg: Optional[dict] = None
            saw_error = False

            try:
                async def _consume():
                    nonlocal assistant_msg, saw_error
                    async with aclosing(activity_model_stream(
                        self.event_bus, self.llm_provider, messages,
                        label=f"Model · {self.agent_type}", agent_id=self.agent_id,
                        tools=self.tool_schemas, stream=False,
                    )) as stream:
                        async for ev in stream:
                            self.last_activity_at = time.time()
                            if self._interrupt:
                                result.error = self._mark_cancelled(result.error)
                                break
                            kind = getattr(ev, "type", None)
                            kind_value = kind.value if hasattr(kind, "value") else kind
                            if kind_value == "text" and ev.content:
                                text_parts.append(ev.content)
                            elif kind_value == "tool_call":
                                pending_calls.append({
                                    "id": (ev.metadata or {}).get("tool_call_id", ""),
                                    "name": ev.tool_name,
                                    "input": ev.tool_input or {},
                                })
                            elif kind_value in {"done", "error"}:
                                meta = ev.metadata or {}
                                assistant_msg = meta.get("assistant_message")
                                if self.usage_callback is not None:
                                    try:
                                        self.usage_callback(
                                            meta.get("usage"), meta.get("model"),
                                            actor=self.agent_type, provider=meta.get("provider"),
                                        )
                                    except Exception:
                                        logger.exception("sub-agent usage_callback failed")
                                if kind_value == "error":
                                    result.error = ev.content or "unknown LLM error"
                                    saw_error = True
                                break
                await _wait_owned(_consume(), self.llm_call_timeout)
            except asyncio.TimeoutError:
                logger.error(
                    "Sub-agent %s LLM call timed out after %ss",
                    self.agent_id, self.llm_call_timeout,
                )
                result.error = f"LLM call timed out after {self.llm_call_timeout}s"
                saw_error = True
            except Exception as exc:
                logger.exception("Sub-agent %s LLM call failed", self.agent_id)
                result.error = str(exc)
                saw_error = True
            finally:
                text_now = "".join(text_parts).strip()
                if text_now:
                    result.text = text_now

            if self.status is SubAgentStatus.CANCELLED:
                break
            if saw_error:
                self.status = SubAgentStatus.ERROR
                break

            if not pending_calls:
                break
            if self._interrupt:
                result.error = self._mark_cancelled(result.error)
                break

            for call in pending_calls:
                call["id"] = call["id"] or f"call_{uuid.uuid4().hex}"
            assistant_msg = dict(assistant_msg or {"role": "assistant", "content": text_now})
            # The advertised calls must match exactly the calls we execute,
            # including fallback IDs absent from provider-supplied messages.
            assistant_msg["tool_calls"] = [
                {
                    "id": c["id"],
                    "type": "function",
                    "function": {
                        "name": c["name"],
                        "arguments": json.dumps(c["input"], ensure_ascii=False),
                    },
                }
                for c in pending_calls
            ]
            messages.append(assistant_msg)
            tool_start = len(messages)
            await self._run_tool_calls(pending_calls, messages, result)

            if self.stop_on_pattern is not None:
                tool_texts = [
                    str(m.get("content") or "")
                    for m in messages[tool_start:]
                    if m.get("role") == "tool"
                ]
                combined = text_now + "\n" + "\n".join(tool_texts)
                match = self.stop_on_pattern.search(combined)
                if match:
                    token = match.group(0)
                    if token not in result.text:
                        result.text = f"{result.text}\n{token}".strip()
                    break

    async def _run_tool_calls(
        self, pending_calls: list[dict], messages: list[dict], result: SubAgentResult
    ) -> None:
        executor = self.tool_executor
        if executor is None:
            return
        outputs: list[str | None] = [None] * len(pending_calls)

        async def execute(index: int, call: dict):
            try:
                try:
                    output = await executor(call["name"], call["input"])
                except Exception as exc:
                    output = json.dumps(
                        {"error": f"tool raised: {exc}"}, ensure_ascii=False
                    )
                outputs[index] = output
                result.scripts_executed += 1
            finally:
                self.last_activity_at = time.time()

        try:
            if self.parallel_tool_calls and len(pending_calls) > 1:
                await asyncio.gather(
                    *(execute(index, call) for index, call in enumerate(pending_calls)),
                    return_exceptions=True,
                )
            else:
                for index, call in enumerate(pending_calls):
                    if self._interrupt:
                        result.error = self._mark_cancelled(result.error)
                        break
                    await execute(index, call)
        finally:
            # The owning execution task is cancelled only once and joined by run().
            # All children have cleaned up here; retain successful sibling output
            # and pair even unstarted calls before exposing the terminal result.
            for call, output in zip(pending_calls, outputs):
                if output is None:
                    output = json.dumps({"error": "Tool interrupted", "status": "cancelled"})
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": call["name"],
                    "content": output,
                })

