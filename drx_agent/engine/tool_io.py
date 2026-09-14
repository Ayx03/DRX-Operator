"""Pure tool I/O in fresh, deadline-owned Python processes.

The parent passes JSON, never agent state. Network reads, filesystem operations,
and Python regex work all run in the child so cancellation can terminate them.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import errno
import difflib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Optional

from drx_agent.engine.process import run_process


async def run_tool_io(name: str, args: dict, *, timeout: float) -> str:
    """Run one operation with a total deadline, including all network fallbacks.

    TimeoutError and CancelledError propagate only after owned-process cleanup.
    Keep the loaded package first on PYTHONPATH for source installs launched from
    another working directory, and preserve the caller's import environment.
    """
    env = os.environ.copy()
    package_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import_paths = [package_root, *sys.path]
    if env.get("PYTHONPATH"):
        import_paths.extend(env["PYTHONPATH"].split(os.pathsep))
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(import_paths))
    result = await run_process(
        [sys.executable, "-m", "drx_agent.engine.tool_io"],
        input=json.dumps({"name": name, "args": args}, ensure_ascii=False).encode("utf-8"),
        timeout=timeout,
        env=env,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        return json.dumps(
            {"error": f"{name} process exited {result.returncode}: {detail[:2000]}"},
            ensure_ascii=False,
        )
    return result.stdout.decode("utf-8")


def _open_regular(path: Path, flags: int) -> int:
    # Resolve intentional symlinks, then refuse a symlink substituted before open.
    # The precheck avoids opening devices; NONBLOCK + fstat closes the FIFO race.
    target = path.resolve()
    if not stat.S_ISREG(target.stat().st_mode):
        raise ValueError(f"Not a regular file: {path}")
    fd = os.open(target, flags | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"Not a regular file: {path}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_regular_text(path: Path, *, encoding: str = "utf-8", errors: str = "strict") -> str:
    fd = _open_regular(path, os.O_RDONLY)
    with os.fdopen(fd, "r", encoding=encoding, errors=errors) as source:
        return source.read()


def _copy_darwin_metadata(source: int, destination: int) -> None:
    """Copy native xattrs (including resource forks) and the extended ACL.

    Python's os xattr API is Linux-only. Do not use copyfile here: its
    best-effort handling can suppress metadata errors and merge inherited ACLs.
    All native calls below use the already checked, pinned file descriptors.
    """
    flags = os.fstat(source).st_flags
    # SF_NOUNLINK (0x00100000) is missing from older Python stat modules.
    # Never make the temporary impossible to rename or remove on failure.
    protected = stat.UF_IMMUTABLE | stat.UF_APPEND | stat.SF_IMMUTABLE | stat.SF_APPEND | 0x00100000
    if flags & protected:
        raise OSError(errno.EPERM, "Cannot atomically replace a rename-protected file")
    libc = ctypes.CDLL(None, use_errno=True)
    signatures = {
        "flistxattr": ([ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int], ctypes.c_ssize_t),
        "fgetxattr": ([ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                       ctypes.c_uint32, ctypes.c_int], ctypes.c_ssize_t),
        "fsetxattr": ([ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                       ctypes.c_uint32, ctypes.c_int], ctypes.c_int),
        "fremovexattr": ([ctypes.c_int, ctypes.c_char_p, ctypes.c_int], ctypes.c_int),
        "fchflags": ([ctypes.c_int, ctypes.c_uint32], ctypes.c_int),
        "acl_get_fd": ([ctypes.c_int], ctypes.c_void_p),
        "acl_init": ([ctypes.c_int], ctypes.c_void_p),
        "acl_set_fd": ([ctypes.c_int, ctypes.c_void_p], ctypes.c_int),
        "acl_free": ([ctypes.c_void_p], ctypes.c_int),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(libc, name)
        function.argtypes = arguments
        function.restype = result

    def checked(result):
        if result == -1:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return result

    def names(fd):
        # Include normally hidden compression attributes. Copying old compressed
        # data into new content is unsafe, so reject rather than silently drop it.
        size = checked(libc.flistxattr(fd, None, 0, 0x0020))
        buffer = ctypes.create_string_buffer(size)
        count = checked(libc.flistxattr(fd, buffer, size, 0x0020))
        return buffer.raw[:count].split(b"\0")[:-1]

    source_names = names(source)
    if b"com.apple.decmpfs" in source_names:
        raise OSError(errno.ENOTSUP, "Cannot preserve compressed-file metadata during an atomic edit")
    for name in names(destination):
        if name not in source_names:
            checked(libc.fremovexattr(destination, name, 0))
    for name in source_names:
        size = checked(libc.fgetxattr(source, name, None, 0, 0, 0))
        # Darwin alone supports positional resource-fork I/O. Bound the buffer
        # rather than materializing a potentially enormous fork (or old data).
        resource_fork = name == b"com.apple.ResourceFork"
        if resource_fork and size > 1 << 32:
            raise OSError(errno.EFBIG, "Resource fork exceeds Darwin's positional xattr range")
        buffer = ctypes.create_string_buffer(min(size, 1024 * 1024) if resource_fork else size)
        position = 0
        while True:
            amount = min(size - position, len(buffer))
            count = checked(libc.fgetxattr(source, name, buffer, amount, position, 0))
            if count != amount:
                raise OSError("Extended attribute changed while writing")
            checked(libc.fsetxattr(destination, name, buffer, count, position, 0))
            position += count
            if position == size:
                break
    ctypes.set_errno(0)
    acl = libc.acl_get_fd(source)
    if not acl and ctypes.get_errno() == errno.ENOENT:
        acl = libc.acl_init(0)
    if not acl:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    try:
        # Replace, rather than merge, the temporary's inherited ACL. An empty
        # source ACL must also clear any permissions inherited at creation.
        checked(libc.acl_set_fd(destination, acl))
    finally:
        libc.acl_free(acl)
    checked(libc.fchflags(destination, flags))


def _copy_extended_metadata(source: int, destination: int) -> None:
    if sys.platform == "darwin":
        _copy_darwin_metadata(source, destination)
    elif sys.platform.startswith("linux"):
        # POSIX access ACLs are system.posix_acl_access xattrs on Linux; copy
        # every namespace exposed by the kernel, including security labels.
        try:
            names = os.listxattr(source)
        except OSError as error:
            if error.errno not in (errno.ENOTSUP, errno.EOPNOTSUPP):
                raise
            # The same-directory temporary is on the same filesystem.
            return
        for name in os.listxattr(destination):
            if name not in names:
                os.removexattr(destination, name)
        for name in names:
            os.setxattr(destination, name, os.getxattr(source, name))
    else:
        raise OSError(errno.ENOTSUP, "Atomic metadata preservation is unsupported on this platform")


def _atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Publish complete content only after ownership and metadata preservation.

    Normal failures remove the private same-directory temporary. SIGKILL can
    leave a hidden .<name>.<random>.tmp, but cannot expose a partial target.
    Metadata read/copy failures reject the edit before publication.
    """
    target = path.resolve()
    with contextlib.ExitStack() as cleanup:
        try:
            source = _open_regular(target, os.O_WRONLY)
        except FileNotFoundError:
            original = None
        else:
            cleanup.callback(os.close, source)
            original = os.fstat(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding=encoding) as destination:
                destination.write(content)
                destination.flush()
                if original is not None:
                    created = os.fstat(destination.fileno())
                    if (created.st_uid, created.st_gid) != (original.st_uid, original.st_gid):
                        os.fchown(destination.fileno(), original.st_uid, original.st_gid)
                    mode = stat.S_IMODE(original.st_mode)
                else:
                    # This operation runs alone in a fresh process, not a live app.
                    mask = os.umask(0)
                    os.umask(mask)
                    mode = 0o666 & ~mask
                os.fchmod(destination.fileno(), mode)
                if original is not None:
                    _copy_extended_metadata(source, destination.fileno())
                    latest = os.fstat(source)
                    if (latest.st_ctime_ns, latest.st_mtime_ns) != (original.st_ctime_ns, original.st_mtime_ns):
                        raise OSError(f"File changed while writing: {path}")
            try:
                current = target.lstat()
            except FileNotFoundError:
                if original is not None:
                    raise
            else:
                if not stat.S_ISREG(current.st_mode):
                    raise ValueError(f"Not a regular file: {path}")
                if original is None or (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                    raise OSError(f"File changed while writing: {path}")
            os.replace(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


_GREP_IGNORE_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".idea", ".vscode",
}


def _http_fetch(
    url: str, method: str, headers: dict, body: Optional[str]
) -> str:
    import ssl
    import urllib.request
    import urllib.error
    if not url:
        return json.dumps({"error": "url is required"}, ensure_ascii=False)
    try:
        data = body.encode("utf-8") if body else None
        req = urllib.request.Request(
            url=url, data=data, method=method or "GET",
            headers={"User-Agent": "DRX-Operator/0.5"},
        )
        for k, v in (headers or {}).items():
            req.add_header(str(k), str(v))
        try:
            resp = urllib.request.urlopen(req, timeout=30)
        except urllib.error.URLError as e:
            # macOS / older Pythons frequently lack a usable CA bundle.
            # Retry with an unverified SSL context so URL fetches still
            # work (we are a red-team tool — verification posture is
            # the user's call, not the library's).
            if "CERTIFICATE_VERIFY_FAILED" in str(e):
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                resp = urllib.request.urlopen(req, timeout=30, context=ctx)
            else:
                raise
        try:
            raw = resp.read()
            status_code = resp.status
            resp_headers = dict(resp.headers.items())
        finally:
            resp.close()
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            text = repr(raw[:2000])
        result = {
            "url": url,
            "method": method,
            "status_code": status_code,
            "headers": {k: resp_headers.get(k) for k in list(resp_headers)[:20]},
            "body": text[:8000],
            "body_truncated": len(text) > 8000,
            "body_length": len(text),
        }
    except urllib.error.HTTPError as e:
        try:
            body_text = e.read().decode("utf-8", errors="replace")
        except Exception:
            body_text = ""
        finally:
            e.close()
        result = {
            "url": url,
            "method": method,
            "status_code": e.code,
            "error": str(e),
            "body": body_text[:8000],
        }
    except Exception as e:
        result = {"url": url, "error": str(e)}

    return json.dumps(result, ensure_ascii=False)


def _make_unified_diff(old: str, new: str, label: str) -> str:
    diff_iter = difflib.unified_diff(
        old.splitlines(keepends=False),
        new.splitlines(keepends=False),
        fromfile=f"a/{label}",
        tofile=f"b/{label}",
        lineterm="",
    )
    return "\n".join(diff_iter)


def _read_file(path: str, offset: int = 0, limit: int = 2000) -> str:
    if not path:
        return json.dumps({"error": "path is required"}, ensure_ascii=False)
    try:
        p = Path(path).expanduser()
        if not p.exists():
            return json.dumps({"error": f"File not found: {path}"}, ensure_ascii=False)
        if not p.is_file():
            return json.dumps({"error": f"Not a regular file: {path}"}, ensure_ascii=False)
        try:
            content = _read_regular_text(p)
        except UnicodeDecodeError:
            return json.dumps(
                {
                    "error": "binary file (use a hex/decode tool)",
                    "path": str(p.resolve()),
                    "size_bytes": p.stat().st_size,
                },
                ensure_ascii=False,
            )

        lines = content.splitlines()
        total = len(lines)
        if offset < 0:
            offset = 0
        if offset >= total and total > 0:
            return json.dumps(
                {"error": f"offset {offset} >= total lines {total}"}, ensure_ascii=False
            )
        selected = lines[offset : offset + limit] if limit > 0 else lines[offset:]
        numbered = "\n".join(
            f"{offset + i + 1:6}\t{line}" for i, line in enumerate(selected)
        )
        return json.dumps(
            {
                "path": str(p.resolve()),
                "start_line": offset + 1,
                "end_line": offset + len(selected),
                "total_lines": total,
                "content": numbered,
                "truncated": offset + len(selected) < total,
            },
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _write_file(path: str, content: str) -> str:
    if not path:
        return json.dumps({"error": "path is required"}, ensure_ascii=False)
    try:
        p = Path(path).expanduser()
        old_content = ""
        existed = p.exists()
        if existed:
            try:
                old_content = _read_regular_text(p)
            except Exception:
                old_content = ""
        _atomic_write_text(p, content)
        diff_text = _make_unified_diff(old_content, content, str(p))
        return json.dumps(
            {
                "ok": True,
                "path": str(p.resolve()),
                "existed": existed,
                "lines_written": len(content.splitlines()),
                "bytes_written": len(content.encode("utf-8")),
                "diff": diff_text,
                "summary": (
                    f"{'Overwrote' if existed else 'Created'} {path} "
                    f"({len(content.splitlines())} lines)"
                ),
            },
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _edit_file(path: str, old_string: str, new_string: str) -> str:
    if not path or not old_string:
        return json.dumps({"error": "path and old_string required"}, ensure_ascii=False)
    try:
        p = Path(path).expanduser()
        if not p.exists():
            return json.dumps({"error": f"File not found: {path}"}, ensure_ascii=False)
        old_content = _read_regular_text(p)
        count = old_content.count(old_string)
        if count == 0:
            return json.dumps(
                {"error": "old_string not found in file"}, ensure_ascii=False
            )
        if count > 1:
            return json.dumps(
                {
                    "error": (
                        f"old_string matches {count} times — add surrounding "
                        "context to make it unique, or use multi_edit_file with "
                        "replace_all"
                    )
                },
                ensure_ascii=False,
            )
        new_content = old_content.replace(old_string, new_string, 1)
        _atomic_write_text(p, new_content)
        diff_text = _make_unified_diff(old_content, new_content, str(p))
        return json.dumps(
            {
                "ok": True,
                "path": str(p.resolve()),
                "bytes_delta": len(new_content) - len(old_content),
                "diff": diff_text,
                "summary": f"Edited {path}",
            },
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _multi_edit_file(path: str, edits: list) -> str:
    if not path:
        return json.dumps({"error": "path is required"}, ensure_ascii=False)
    if not isinstance(edits, list) or not edits:
        return json.dumps({"error": "edits must be a non-empty array"}, ensure_ascii=False)
    try:
        p = Path(path).expanduser()
        if not p.exists():
            return json.dumps({"error": f"File not found: {path}"}, ensure_ascii=False)
        old_content = _read_regular_text(p)
        current = old_content
        applied = 0
        for i, edit in enumerate(edits):
            if not isinstance(edit, dict):
                return json.dumps(
                    {"error": f"edit #{i} is not an object"}, ensure_ascii=False
                )
            old_s = edit.get("old_string", "")
            new_s = edit.get("new_string", "")
            replace_all = bool(edit.get("replace_all", False))
            if not old_s:
                return json.dumps(
                    {"error": f"edit #{i}: empty old_string"}, ensure_ascii=False
                )
            if replace_all:
                if old_s not in current:
                    return json.dumps(
                        {"error": f"edit #{i}: old_string not found"}, ensure_ascii=False
                    )
                current = current.replace(old_s, new_s)
            else:
                count = current.count(old_s)
                if count == 0:
                    return json.dumps(
                        {"error": f"edit #{i}: old_string not found"}, ensure_ascii=False
                    )
                if count > 1:
                    return json.dumps(
                        {
                            "error": (
                                f"edit #{i}: old_string matches {count}x — add "
                                "context or set replace_all=true"
                            )
                        },
                        ensure_ascii=False,
                    )
                current = current.replace(old_s, new_s, 1)
            applied += 1
        _atomic_write_text(p, current)
        diff_text = _make_unified_diff(old_content, current, str(p))
        return json.dumps(
            {
                "ok": True,
                "path": str(p.resolve()),
                "edits_applied": applied,
                "diff": diff_text,
                "summary": f"Applied {applied} edits to {path}",
            },
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _grep(
    pattern: str,
    path: str = ".",
    glob: str = "**/*",
    max_results: int = 100,
    ignore_case: bool = False,
) -> str:
    if not pattern:
        return json.dumps({"error": "pattern is required"}, ensure_ascii=False)
    try:
        flags = re.IGNORECASE if ignore_case else 0
        regex = re.compile(pattern, flags)
    except re.error as e:
        return json.dumps({"error": f"Invalid regex: {e}"}, ensure_ascii=False)
    try:
        root = Path(path).expanduser().resolve()
        if not root.exists():
            return json.dumps({"error": f"Path not found: {path}"}, ensure_ascii=False)
        if root.is_file():
            candidates = [root]
        else:
            try:
                candidates = list(root.glob(glob))
            except Exception as e:
                return json.dumps({"error": f"Glob error: {e}"}, ensure_ascii=False)

        matches = []
        files_searched = 0
        for f in candidates:
            if not f.is_file():
                continue
            parts = f.parts
            if any(p in _GREP_IGNORE_DIRS for p in parts):
                continue
            if f.suffix.lower() in {
                ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip",
                ".gz", ".tar", ".so", ".pyc", ".db", ".sqlite",
                ".bin", ".exe", ".dll", ".o", ".a",
            }:
                continue
            files_searched += 1
            try:
                text = _read_regular_text(f, errors="ignore")
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    matches.append({
                        "file": str(f.relative_to(root) if root.is_dir() else f.name),
                        "line": i,
                        "text": line[:300],
                    })
                    if len(matches) >= max_results:
                        break
            if len(matches) >= max_results:
                break

        return json.dumps(
            {
                "pattern": pattern,
                "ignore_case": ignore_case,
                "files_searched": files_searched,
                "match_count": len(matches),
                "matches": matches,
                "truncated": len(matches) >= max_results,
            },
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _web_search(query: str, max_results: int = 10) -> str:
    # Preferred backend: ddgs (handles DuckDuckGo bot detection / vqd flow).
    # Fallback: Instant Answer API (no key, abstracts only) so search never breaks.
    if not query:
        return json.dumps({"error": "query is required"}, ensure_ascii=False)

    try:
        from ddgs import DDGS

        try:
            with DDGS() as ddgs:
                raw_hits = list(ddgs.text(query, max_results=max_results))
            results = [
                {
                    "title": h.get("title", "")[:200],
                    "url": h.get("href") or h.get("url", ""),
                    "snippet": (h.get("body") or h.get("snippet") or "")[:300],
                }
                for h in raw_hits
                if h.get("title")
            ]
            return json.dumps(
                {
                    "query": query,
                    "result_count": len(results),
                    "results": results,
                    "source": "ddgs",
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            primary_error: Optional[str] = f"ddgs failed: {exc}"
    except ImportError:
        primary_error = (
            "ddgs not installed (run `pip install ddgs` for full web "
            "search); falling back to DuckDuckGo Instant Answer API."
        )

    import ssl
    import urllib.error
    import urllib.parse
    import urllib.request

    api_url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode(
        {
            "q": query,
            "format": "json",
            "no_html": "1",
            "skip_disambig": "0",
            "t": "DRX-Operator",
        }
    )
    try:
        req = urllib.request.Request(
            api_url, headers={"User-Agent": "DRX-Operator/0.5"}
        )
        try:
            resp = urllib.request.urlopen(req, timeout=15)
        except urllib.error.URLError as e:
            if "CERTIFICATE_VERIFY_FAILED" in str(e):
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                resp = urllib.request.urlopen(req, timeout=15, context=ctx)
            else:
                raise
        try:
            body = resp.read().decode("utf-8", errors="replace")
        finally:
            resp.close()
        payload = json.loads(body)
    except Exception as e:
        return json.dumps(
            {
                "error": f"web search failed: {e}",
                "query": query,
                "hint": "install `ddgs` for full search (`pip install ddgs`)",
            },
            ensure_ascii=False,
        )

    results: list[dict] = []
    abstract = (payload.get("AbstractText") or "").strip()
    if abstract:
        results.append({
            "title": payload.get("Heading", query),
            "url": payload.get("AbstractURL", ""),
            "snippet": abstract[:300],
        })
    for topic in payload.get("RelatedTopics", [])[: max_results - len(results)]:
        if not isinstance(topic, dict):
            continue
        if "Topics" in topic:
            for sub in topic["Topics"]:
                if not isinstance(sub, dict):
                    continue
                text = (sub.get("Text") or "").strip()
                if text:
                    results.append({
                        "title": text.split(" - ", 1)[0][:200],
                        "url": sub.get("FirstURL", ""),
                        "snippet": text[:300],
                    })
                if len(results) >= max_results:
                    break
        else:
            text = (topic.get("Text") or "").strip()
            if text:
                results.append({
                    "title": text.split(" - ", 1)[0][:200],
                    "url": topic.get("FirstURL", ""),
                    "snippet": text[:300],
                })
        if len(results) >= max_results:
            break

    return json.dumps(
        {
            "query": query,
            "result_count": len(results),
            "results": results,
            "source": "duckduckgo-instant-answer",
            "note": primary_error,
        },
        ensure_ascii=False,
    )


def _cve_lookup(cve_id: str) -> str:

    import ssl
    import urllib.error
    import urllib.parse
    import urllib.request

    if not cve_id:
        return json.dumps({"error": "cve_id is required"}, ensure_ascii=False)

    cve_id = cve_id.strip().upper()
    if not re.fullmatch(r"CVE-\d{4}-\d{4,}", cve_id):
        return json.dumps(
            {"error": f"invalid CVE id format: {cve_id!r}"},
            ensure_ascii=False,
        )

    url = (
        "https://services.nvd.nist.gov/rest/json/cves/2.0?"
        + urllib.parse.urlencode({"cveId": cve_id})
    )
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "DRX-Operator/0.5 (CVE lookup)"}
        )
        try:
            resp = urllib.request.urlopen(req, timeout=20)
        except urllib.error.URLError as e:
            if "CERTIFICATE_VERIFY_FAILED" in str(e):
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                resp = urllib.request.urlopen(req, timeout=20, context=ctx)
            else:
                raise
        try:
            body = resp.read().decode("utf-8", errors="replace")
        finally:
            resp.close()
        payload = json.loads(body)
    except urllib.error.HTTPError as e:
        return json.dumps(
            {"error": f"NVD HTTP {e.code}: {e.reason}", "cve_id": cve_id},
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps(
            {"error": f"NVD request failed: {e}", "cve_id": cve_id},
            ensure_ascii=False,
        )

    vulns = payload.get("vulnerabilities") or []
    if not vulns:
        return json.dumps(
            {"error": "CVE not found in NVD", "cve_id": cve_id},
            ensure_ascii=False,
        )
    cve = vulns[0].get("cve") or {}

    description = ""
    for d in cve.get("descriptions", []):
        if d.get("lang") == "en":
            description = d.get("value", "")
            break

    metrics = cve.get("metrics") or {}
    cvss = None
    for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        entries = metrics.get(key) or []
        if entries:
            data = entries[0].get("cvssData") or {}
            cvss = {
                "version": data.get("version"),
                "vector": data.get("vectorString"),
                "baseScore": data.get("baseScore"),
                "baseSeverity": (
                    data.get("baseSeverity")
                    or entries[0].get("baseSeverity")
                ),
                "exploitabilityScore": entries[0].get("exploitabilityScore"),
                "impactScore": entries[0].get("impactScore"),
            }
            break

    cwes: list[str] = []
    for w in cve.get("weaknesses", []):
        for d in w.get("description", []):
            v = d.get("value")
            if v and v not in cwes:
                cwes.append(v)

    refs = [
        {
            "url": r.get("url"),
            "source": r.get("source"),
            "tags": r.get("tags", []),
        }
        for r in (cve.get("references") or [])[:10]
    ]

    affected: list[str] = []
    for conf in cve.get("configurations", []):
        for node in conf.get("nodes", []):
            for cpe in node.get("cpeMatch", []):
                name = cpe.get("criteria")
                if name and name not in affected:
                    affected.append(name)
                    if len(affected) >= 20:
                        break
            if len(affected) >= 20:
                break
        if len(affected) >= 20:
            break

    return json.dumps(
        {
            "cve_id": cve.get("id", cve_id),
            "published": cve.get("published"),
            "lastModified": cve.get("lastModified"),
            "description": description,
            "cvss": cvss,
            "cwes": cwes,
            "references": refs,
            "affected_cpe": affected,
            "source": "NVD",
        },
        ensure_ascii=False,
    )

def _read_text(args: dict) -> str:
    path = Path(args.get("path", "")).expanduser()
    content = _read_regular_text(
        path, encoding=args.get("encoding", "utf-8"), errors=args.get("errors", "replace")
    )
    offset = int(args.get("offset", 0) or 0)
    limit = int(args.get("limit", 0) or 0)
    content = content[offset:]
    if limit > 0:
        content = content[:limit]
    return json.dumps({"path": str(path.resolve()), "content": content}, ensure_ascii=False)


def _write_text(args: dict) -> str:
    path = Path(args.get("path", "")).expanduser()
    content = args.get("content", "")
    encoding = args.get("encoding", "utf-8")
    _atomic_write_text(path, content, encoding=encoding)
    return json.dumps(
        {"ok": True, "path": str(path.resolve()), "bytes_written": len(content.encode(encoding))},
        ensure_ascii=False,
    )


def _read_image(args: dict) -> str:
    path = Path(args.get("path", "")).expanduser()
    fd = _open_regular(path, os.O_RDONLY)
    with os.fdopen(fd, "rb") as source:
        content = source.read()
    return json.dumps(
        {
            "content": base64.b64encode(content).decode("ascii"),
            "size": len(content),
            "path": str(path.resolve()),
        },
        ensure_ascii=False,
    )


def _dispatch(name: str, args: dict) -> str:
    if name == "http_fetch":
        return _http_fetch(args.get("url", ""), args.get("method", "GET"), args.get("headers") or {}, args.get("body"))
    if name == "read_file":
        return _read_file(args.get("path", ""), int(args.get("offset", 0) or 0), int(args.get("limit", 2000) or 2000))
    if name == "write_file":
        return _write_file(args.get("path", ""), args.get("content", ""))
    if name == "edit_file":
        return _edit_file(args.get("path", ""), args.get("old_string", ""), args.get("new_string", ""))
    if name == "multi_edit_file":
        return _multi_edit_file(args.get("path", ""), args.get("edits") or [])
    if name == "grep":
        return _grep(args.get("pattern", ""), args.get("path", "."), args.get("glob", "**/*"), int(args.get("max_results", 100) or 100), bool(args.get("ignore_case", False)))
    if name == "web_search":
        return _web_search(args.get("query", ""), int(args.get("max_results", 10) or 10))
    if name == "cve_lookup":
        return _cve_lookup(args.get("cve_id", ""))
    if name == "read_text":
        return _read_text(args)
    if name == "write_text":
        return _write_text(args)
    if name == "read_image":
        return _read_image(args)
    return json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False)


def main() -> None:
    try:
        request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        # Search backends may print diagnostic text; stdout is the JSON protocol.
        with contextlib.redirect_stdout(sys.stderr):
            result = _dispatch(request["name"], request["args"])
    except Exception as exc:
        result = json.dumps({"error": str(exc)}, ensure_ascii=False)
    sys.stdout.buffer.write(result.encode("utf-8"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
