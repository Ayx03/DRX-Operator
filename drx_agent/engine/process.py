"""Owned one-shot subprocesses with cancellation and process-group cleanup.

POSIX children get a new session. Descendants must not deliberately escape that
session; Windows cleanup is limited to the direct child. Thread callers signal
CANCEL_EVENT and join the thread before abandoning its result.
"""
from __future__ import annotations

import asyncio
import math
import os
import signal
import subprocess
import threading
import time
import weakref
from contextvars import ContextVar

CANCEL_EVENT: ContextVar[threading.Event | None] = ContextVar("process_cancel_event", default=None)
_POLL = 0.05
_KILL = getattr(signal, "SIGKILL", 9)
_CANCEL_REQUESTS: weakref.WeakSet = weakref.WeakSet()


def cancel_task(task: asyncio.Future) -> None:
    """Request cancellation once, including on Python 3.10 without cancelling()."""
    if task.done() or task in _CANCEL_REQUESTS:
        return
    cancelling = getattr(task, "cancelling", None)
    if cancelling is not None and cancelling():
        return
    _CANCEL_REQUESTS.add(task)
    task.cancel()


def check_cancelled() -> None:
    event = CANCEL_EVENT.get()
    if event is not None and event.is_set():
        raise asyncio.CancelledError


def _duration(value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError("process timeout must be finite and nonnegative")
    return value


def _signal(proc, sig: int) -> bool:
    try:
        if os.name == "posix":
            # Keep the original group id even after its leader exits.
            os.killpg(proc.pid, sig)
        elif proc.returncode is None:
            proc.terminate() if sig == signal.SIGTERM else proc.kill()
        return True
    except ProcessLookupError:
        return False


def _group_alive(proc) -> bool:
    if os.name != "posix":
        return proc.returncode is None
    try:
        return _signal(proc, 0)
    except PermissionError:
        # EPERM means the group exists, not that it is signalable. Darwin
        # also reports it while an exiting group is not yet waitable.
        return True


async def _settle(task):
    """Join cleanup even if the owning task is cancelled repeatedly."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


async def terminate_process(proc, *, grace: float = 0.2) -> None:
    grace = _duration(grace)

    async def cleanup():
        try:
            _signal(proc, signal.SIGTERM)
        except PermissionError:
            # Allow an already-exiting group to settle during the grace period.
            # The final KILL below must succeed or report a real permission error.
            pass
        deadline = time.monotonic() + grace
        while _group_alive(proc) and time.monotonic() < deadline:
            await asyncio.sleep(min(_POLL, max(0, deadline - time.monotonic())))
        _signal(proc, _KILL)
        # Waiting before the child watcher sets returncode can also wait for
        # paused pipe readers. Poll the reaped status first; readers belong to
        # the caller and must not prevent termination.
        while proc.returncode is None:
            await asyncio.sleep(_POLL)
        await proc.wait()

    _, cancelled = await _settle(asyncio.create_task(cleanup()))
    if cancelled:
        raise asyncio.CancelledError


def terminate_process_sync(proc, *, grace: float = 0.2) -> None:
    grace = _duration(grace)
    proc.poll()
    try:
        _signal(proc, signal.SIGTERM)
    except PermissionError:
        # Same settling rule as the asynchronous cleanup; KILL is not suppressed.
        pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        proc.poll()
        if not _group_alive(proc):
            break
        time.sleep(min(_POLL, max(0, deadline - time.monotonic())))
    _signal(proc, _KILL)
    proc.wait()


async def run_process(argv, *, input: bytes | None = None, timeout: float, env=None, cwd=None) -> subprocess.CompletedProcess[bytes]:
    timeout = _duration(timeout)
    check_cancelled()
    deadline = time.monotonic() + timeout
    # Shield creation as well: cancellation must not lose a just-spawned child.
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=env, cwd=cwd, start_new_session=os.name == "posix",
    ))
    proc, cancelled = await _settle(spawn)
    communication = asyncio.create_task(proc.communicate(input))
    try:
        if cancelled:
            raise asyncio.CancelledError
        while not communication.done():
            check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            if proc.returncode is not None:
                # A background child may still hold stdout/stderr open.
                break
            await asyncio.wait({communication}, timeout=min(_POLL, remaining))
        check_cancelled()
        if time.monotonic() >= deadline:
            raise asyncio.TimeoutError
    finally:
        # Join both cleanup and pipe draining before propagating cancellation.
        async def cleanup():
            await terminate_process(proc)
            return await communication
        (stdout, stderr), cleanup_cancelled = await _settle(asyncio.create_task(cleanup()))
        if cleanup_cancelled:
            raise asyncio.CancelledError
    check_cancelled()
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def run_process_sync(args, *, input: str | None = None, timeout: float, env=None, cwd=None, shell=False, executable=None) -> subprocess.CompletedProcess[str]:
    timeout = _duration(timeout)
    check_cancelled()
    deadline = time.monotonic() + timeout
    proc = subprocess.Popen(
        args, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=env, cwd=cwd, shell=shell, executable=executable,
        start_new_session=os.name == "posix",
    )
    first = True
    try:
        while True:
            check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(args, timeout)
            try:
                stdout, stderr = proc.communicate(input if first else None, timeout=min(_POLL, remaining))
                break
            except subprocess.TimeoutExpired:
                first = False
                if proc.poll() is not None:
                    terminate_process_sync(proc)
                    stdout, stderr = proc.communicate()
                    break
        check_cancelled()
    finally:
        terminate_process_sync(proc)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
    check_cancelled()
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
