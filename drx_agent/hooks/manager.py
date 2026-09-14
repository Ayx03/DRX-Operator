"""User-defined hooks fired around tool calls and LLM calls.

Python hooks are cooperative async callables; blocking/synchronous callbacks must
use command hooks (event JSON on stdin). In-process async hooks must yield and
must not suppress cancellation: Python cannot forcibly stop arbitrary code while
preserving its shared-memory side effects. Command hooks have a killable process
boundary. Events: pre_tool / post_tool / pre_llm / post_llm / agent_message.
A pre_tool hook may return {"deny": "reason"} to short-circuit the call.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import shlex
import math
import time
from dataclasses import dataclass
from typing import Any, Callable

from drx_agent.engine.process import run_process

logger = logging.getLogger(__name__)


EVENTS = (
    "pre_tool",
    "post_tool",
    "pre_llm",
    "post_llm",
    "agent_message",
)


HookCallable = Callable[[dict], Any]


@dataclass
class HookSpec:
    event: str
    handler: HookCallable
    name: str = ""
    tool_glob: str = "*"
    timeout: float = 5.0
    command: bool = False


class HookManager:
    def __init__(self) -> None:
        self._hooks: dict[str, list[HookSpec]] = {e: [] for e in EVENTS}


    def register(
        self,
        event: str,
        handler: HookCallable,
        name: str = "",
        tool_glob: str = "*",
        timeout: float = 5.0,
    ) -> None:
        if event not in self._hooks:
            raise ValueError(f"unknown hook event: {event!r}")
        if not (inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(getattr(handler, "__call__", None))):
            raise TypeError("Python hooks must be cooperative async callables; use register_command() for synchronous or blocking callbacks")
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("hook timeout must be finite and positive")
        self._hooks[event].append(
            HookSpec(event=event, handler=handler, name=name or getattr(handler, "__name__", "hook"), tool_glob=tool_glob, timeout=timeout)
        )

    def register_command(
        self,
        event: str,
        command: str | list[str],
        name: str = "",
        tool_glob: str = "*",
        timeout: float = 5.0,
    ) -> None:
        """Register a shell-command hook. The event JSON is piped to stdin."""
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        if not argv:
            raise ValueError("hook command must not be empty")

        async def runner(event_data: dict) -> Any:
            try:
                proc = await run_process(
                    argv,
                    input=json.dumps(event_data, ensure_ascii=False).encode("utf-8"),
                    timeout=timeout,
                )
                stdout = proc.stdout.decode("utf-8", errors="replace").strip()
                if not stdout:
                    return None
                try:
                    return json.loads(stdout)
                except json.JSONDecodeError:
                    return {"stdout": stdout}
            except Exception as e:
                logger.warning("Hook command %r failed: %s", argv, e)
                return None

        self.register(event, runner, name=name or argv[0], tool_glob=tool_glob, timeout=timeout)
        self._hooks[event][-1].command = True

    def load_from_config(self, hooks_cfg: list[dict]) -> None:
        """Load declarative hook specs from config."""
        for cfg in hooks_cfg or []:
            if not isinstance(cfg, dict):
                continue
            event = cfg.get("event")
            cmd = cfg.get("command")
            if not event or not cmd:
                continue
            self.register_command(
                event,
                cmd,
                name=cfg.get("name", ""),
                tool_glob=cfg.get("tool_glob", "*"),
                timeout=float(cfg.get("timeout", 5.0)),
            )


    @staticmethod
    def _tool_matches(spec_glob: str, tool_name: str) -> bool:
        import fnmatch
        return fnmatch.fnmatchcase(tool_name, spec_glob)


    @staticmethod
    async def _invoke(spec: HookSpec, payload: dict) -> Any:
        if spec.command:
            # run_process owns the total deadline and cancellation cleanup.
            return await spec.handler(payload)
        task = asyncio.create_task(spec.handler(payload))
        try:
            done, _ = await asyncio.wait({task}, timeout=spec.timeout)
            if not done:
                raise asyncio.TimeoutError(f"hook exceeded {spec.timeout}s")
            return task.result()
        finally:
            if not task.done():
                task.cancel()
            # Do not detach side effects or interrupt cooperative cleanup when
            # a caller presses cancel repeatedly.
            cancelled = False
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    if not task.done():
                        cancelled = True
                except Exception:
                    break
            if task.done() and not task.cancelled():
                task.exception()
            if cancelled:
                raise asyncio.CancelledError
    async def dispatch(self, event: str, payload: dict) -> list[Any]:
        """Run all hooks for *event*; returns their return values."""
        specs = self._hooks.get(event) or []
        if not specs:
            return []
        payload = dict(payload)
        payload.setdefault("event", event)
        payload.setdefault("ts", time.time())
        tool_name = payload.get("tool", "")
        results: list[Any] = []
        for spec in specs:
            if event in ("pre_tool", "post_tool") and not self._tool_matches(spec.tool_glob, tool_name):
                continue
            try:
                results.append(await self._invoke(spec, payload))
            except Exception as exc:
                logger.exception("Hook %r raised on %s", spec.name, event)
                results.append({"error": str(exc), "hook": spec.name})
        return results

