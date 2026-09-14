"""Interrupt one owned execution phase without cancelling its resident agent."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import Awaitable, TypeVar

from drx_agent.engine.process import cancel_task

T = TypeVar("T")
_ACTIVE_SIGNAL: ContextVar[MessageSignal | None] = ContextVar("active_message_phase", default=None)


def is_message_interrupt() -> bool:
    signal = _ACTIVE_SIGNAL.get()
    return signal is not None and signal._interrupted


class MessageInterrupt(Exception):
    """New mail arrived; finish phase cleanup, then consume the inbox."""


class MessageSignal:
    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._interrupted = False

    @property
    def pending(self) -> bool:
        return self._event.is_set()

    def notify(self) -> None:
        self._event.set()

    def acknowledge(self) -> None:
        self._event.clear()

    async def run(self, awaitable: Awaitable[T], *, timeout: float | None = None) -> T:
        started = asyncio.Event()
        self._interrupted = False

        async def invoke() -> T:
            started.set()
            token = _ACTIVE_SIGNAL.set(self)
            try:
                return await awaitable
            finally:
                _ACTIVE_SIGNAL.reset(token)

        phase = asyncio.create_task(invoke())
        incoming = asyncio.create_task(self._event.wait())
        cancelled = False
        try:
            await asyncio.wait({phase, incoming}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            # A finished phase owns its result/error even if mail arrived in the
            # same loop turn. Its caller checks pending mail before more work.
            if phase.done():
                return phase.result()
            if incoming.done():
                self._interrupted = True
                raise MessageInterrupt
            raise asyncio.TimeoutError
        finally:
            if not phase.done() and not started.is_set():
                # Let the phase establish its finally/history ownership before
                # cancelling it, including cancellation before its first step.
                ready = asyncio.create_task(started.wait())
                while not ready.done():
                    try:
                        await asyncio.shield(ready)
                    except asyncio.CancelledError:
                        cancelled = True
            cancel_task(phase)
            cancel_task(incoming)
            drain = asyncio.gather(phase, incoming, return_exceptions=True)
            while not drain.done():
                try:
                    await asyncio.shield(drain)
                except asyncio.CancelledError:
                    cancelled = True
            self._interrupted = False
            if cancelled:
                raise asyncio.CancelledError
