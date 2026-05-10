"""Cross-cut: persist session state to disk after every successful MCP tool
that received a session_id.

Decorator wraps each @mcp.tool() so that on success — and only on success —
the in-memory session whose id matches a parameter named ``session_id`` is
mirrored to ``.specfs/sessions/<id>.json``. This makes every tool call a
checkpoint: an OpenCode / MCP-server restart can rebuild the runtime state
of any in-flight workflow purely from disk.

Failures inside the underlying tool propagate untouched; persistence is
silent on filesystem errors so that telemetry overhead never blocks the
caller. Decorator is idempotent (tagged with ``__specfs_persist_patched__``)
and is meant to wrap OUTSIDE both the metrics and the timeout wrappers so
that we only persist after a real (non-timeout, non-error) return.
"""
from __future__ import annotations

import functools
import inspect
from typing import Any, Callable

import state as _state_mod  # noqa: F401  -- typing only


def install_persist(mcp: Any, session_lookup: Callable[[str], Any]) -> None:
    """Wrap @mcp.tool() so successful tool calls auto-mirror sessions to disk.

    Args:
        mcp: the FastMCP instance whose ``.tool`` we patch.
        session_lookup: callable taking a session_id and returning either the
            in-memory Session (so we can serialize its current state) or
            None when no such session exists in this process. Typically the
            server's ``_get`` cache lookup, NOT the disk-fallback ``_get``.

    Idempotent.
    """
    orig_tool = mcp.tool
    if getattr(orig_tool, "__specfs_persist_patched__", False):
        return

    def patched_tool(*args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Any]:
        decorator = orig_tool(*args, **kwargs)

        def outer(fn: Callable[..., Any]) -> Any:
            try:
                fn_sig = inspect.signature(fn)
            except (TypeError, ValueError):
                fn_sig = None

            @functools.wraps(fn)
            def wrapper(*a: Any, **kw: Any) -> Any:
                if fn_sig is not None:
                    try:
                        bound = fn_sig.bind_partial(*a, **kw)
                        params = dict(bound.arguments)
                    except TypeError:
                        params = dict(kw)
                else:
                    params = dict(kw)

                result = fn(*a, **kw)

                sid = params.get("session_id")
                if isinstance(sid, str) and sid:
                    sess = session_lookup(sid)
                    if sess is not None:
                        import state as _state
                        _state.save_session(sess)
                return result

            return decorator(wrapper)

        return outer

    patched_tool.__specfs_persist_patched__ = True  # type: ignore[attr-defined]
    mcp.tool = patched_tool  # type: ignore[assignment]
