"""Timeout wrapper for specfs-port MCP tools.

Why this exists
---------------
v0.5.2 (2026-05-07): user reported MCP-driven code generation hangs. Root
cause survey:
  - `_git_sha` / `_git_add` had no subprocess timeout (git can block on
    file-locked repos, .git/index.lock left by a crashed prior process,
    detached-HEAD edge cases on certain refspecs).
  - All 37 @mcp.tool() entries had no caller-visible deadline; any blocking
    syscall (slow NFS, hung subprocess, runaway file scan) would freeze
    the LLM end of the conversation indefinitely.

Design
------
A decorator `with_timeout(seconds)` runs the wrapped function in a daemon
ThreadPoolExecutor and returns a structured error dict on overrun:

    {"_specfs_error": "timeout",
     "tool": "<fn_name>",
     "budget_s": <budget>,
     "hint": "Tool exceeded its budget. Background work may still be in
              progress; consider session_end() and retry with smaller scope."}

The original function's return shape is preserved on success — the timeout
key is only injected on failure, so success callers see no schema change.

Threads keep running after timeout (Python cannot safely kill threads).
They are daemon=True so they vanish when the process exits. For idempotent
work this is fine; for I/O-heavy work the worst case is a partial-write
that the next round overwrites.

Per-tool override
-----------------
Default budget is 30 s (chosen to fit `*_gen_approve` flows that touch git
+ Makefile + main.c on a large repo, with comfortable headroom). Override
per-tool by passing `timeout=N` to the @mcp.tool() decorator (see
specfs_server.py monkey-patch of `mcp.tool`).

Usage
-----
    @with_timeout(60.0)
    def slow_op(arg):
        ...

    # or in specfs_server.py the monkey-patched @mcp.tool() applies it
    # automatically with the configured default.
"""
from __future__ import annotations

import functools
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Callable


# Pool size 4 is enough for the typical specfs-port flow: only one tool
# runs at a time per session, plus headroom for parallel sub-agents.
# Daemon threads vanish on process exit so leaked timeouts don't block
# shutdown.
_EXECUTOR = ThreadPoolExecutor(
    max_workers=int(os.environ.get("SPECFS_TIMEOUT_WORKERS", "4")),
    thread_name_prefix="specfs-tool",
)


# Process-wide default budget. Override individual tools via
# `@mcp.tool(timeout=N)` (see the monkey-patched mcp.tool in specfs_server).
DEFAULT_TIMEOUT_S = float(os.environ.get("SPECFS_DEFAULT_TIMEOUT_S", "30"))


def with_timeout(seconds: float) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: run wrapped fn in a daemon thread; return structured timeout
    error if it doesn't complete in `seconds`.

    On timeout, the work continues in the background but the caller is
    immediately unblocked with an error dict. On success, the original
    return value is passed through unchanged.

    Failures inside the wrapped function bubble up as normal exceptions —
    only the wall-clock budget overrun is converted to a dict.
    """
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            future = _EXECUTOR.submit(fn, *args, **kwargs)
            try:
                return future.result(timeout=seconds)
            except FuturesTimeoutError:
                return {
                    "_specfs_error": "timeout",
                    "tool": fn.__name__,
                    "budget_s": seconds,
                    "hint": (
                        "Tool exceeded its budget. Background work may still be "
                        "in progress; consider session_end() and retry with "
                        "smaller scope, or override the budget via "
                        "SPECFS_DEFAULT_TIMEOUT_S env var."
                    ),
                }
        # Expose the original function for testing / introspection
        wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
        wrapper.__specfs_timeout_s__ = seconds  # type: ignore[attr-defined]
        return wrapper
    return decorator


def install_default_timeout(mcp: Any, default_seconds: float = DEFAULT_TIMEOUT_S) -> None:
    """Monkey-patch `mcp.tool` so every @mcp.tool() registration is wrapped
    with a default timeout. Per-tool override: `@mcp.tool(timeout=N)`.

    Idempotent: calling twice does not double-wrap (we tag the patched
    method with __specfs_patched__).
    """
    orig_tool = mcp.tool
    if getattr(orig_tool, "__specfs_patched__", False):
        return

    def patched_tool(*args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Any]:
        # Pop our extension kwarg before delegating to FastMCP, which would
        # otherwise reject an unknown keyword argument.
        timeout = kwargs.pop("timeout", default_seconds)
        inner_decorator = orig_tool(*args, **kwargs)

        def outer(fn: Callable[..., Any]) -> Any:
            wrapped = with_timeout(timeout)(fn)
            return inner_decorator(wrapped)

        return outer

    patched_tool.__specfs_patched__ = True  # type: ignore[attr-defined]
    patched_tool.__wrapped_orig__ = orig_tool  # type: ignore[attr-defined]
    mcp.tool = patched_tool  # type: ignore[assignment]
