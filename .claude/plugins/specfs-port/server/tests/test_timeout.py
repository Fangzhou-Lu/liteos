"""Unit tests for _timeout.py — MCP-tool timeout decorator + mcp.tool monkey-patch."""
from __future__ import annotations

import time
from typing import Any

import pytest


def test_with_timeout_passes_through_on_success():
    """Functions that finish under budget return their original value unchanged."""
    from _timeout import with_timeout

    @with_timeout(2.0)
    def fast(x: int) -> dict[str, int]:
        return {"value": x * 2}

    out = fast(21)
    assert out == {"value": 42}


def test_with_timeout_returns_structured_error_on_overrun():
    """A function that exceeds its budget yields the timeout dict, not an exception."""
    from _timeout import with_timeout

    @with_timeout(0.2)
    def slow() -> dict[str, str]:
        time.sleep(2.0)  # vastly exceeds 0.2 s budget
        return {"never": "reached"}

    start = time.monotonic()
    out = slow()
    elapsed = time.monotonic() - start

    assert isinstance(out, dict)
    assert out["_specfs_error"] == "timeout"
    assert out["tool"] == "slow"
    assert out["budget_s"] == 0.2
    assert "hint" in out

    # The wrapper should have returned within ~0.2-0.5 s, not 2 s.
    assert elapsed < 1.0, f"timeout wrapper blocked for {elapsed:.2f}s — should be ~0.2s"


def test_with_timeout_propagates_exceptions_unchanged():
    """If the wrapped function raises BEFORE the budget elapses, the exception
    propagates — only wall-clock overrun becomes a dict."""
    from _timeout import with_timeout

    @with_timeout(2.0)
    def bad() -> Any:
        raise ValueError("expected error")

    with pytest.raises(ValueError, match="expected error"):
        bad()


def test_with_timeout_preserves_function_metadata():
    """functools.wraps should preserve __name__, __doc__, plus our extra
    introspection attrs."""
    from _timeout import with_timeout

    @with_timeout(5.0)
    def documented_fn() -> str:
        """My docstring."""
        return "ok"

    assert documented_fn.__name__ == "documented_fn"
    assert documented_fn.__doc__ == "My docstring."
    assert documented_fn.__wrapped__ is not None  # type: ignore[attr-defined]
    assert documented_fn.__specfs_timeout_s__ == 5.0  # type: ignore[attr-defined]


def test_with_timeout_handles_kwargs():
    from _timeout import with_timeout

    @with_timeout(1.0)
    def echo(*, msg: str) -> dict[str, str]:
        return {"msg": msg}

    assert echo(msg="hi") == {"msg": "hi"}


def test_install_default_timeout_is_idempotent():
    """Calling install_default_timeout twice on the same mcp object should not
    double-wrap (otherwise per-tool budgets would be misreported)."""
    from _timeout import install_default_timeout

    class FakeMCP:
        def tool(self, *args, **kwargs):
            def decorator(fn):
                return fn
            return decorator

    fake = FakeMCP()
    orig_tool = fake.tool

    install_default_timeout(fake, default_seconds=10.0)
    patched_once = fake.tool

    install_default_timeout(fake, default_seconds=10.0)
    patched_twice = fake.tool

    # Second call is a no-op — the patched function is the SAME object,
    # not a doubly-wrapped variant.
    assert patched_once is patched_twice
    assert patched_once is not orig_tool


def test_install_default_timeout_default_budget():
    """Functions registered via the patched mcp.tool() get the configured default."""
    from _timeout import install_default_timeout

    registered: list[Any] = []

    class FakeMCP:
        def tool(self, *args, **kwargs):
            def decorator(fn):
                registered.append(fn)
                return fn
            return decorator

    fake = FakeMCP()
    install_default_timeout(fake, default_seconds=15.0)

    @fake.tool()
    def my_tool() -> dict[str, str]:
        return {"ok": "yes"}

    assert len(registered) == 1
    wrapped = registered[0]
    assert wrapped.__specfs_timeout_s__ == 15.0  # type: ignore[attr-defined]
    assert wrapped() == {"ok": "yes"}


def test_install_default_timeout_per_tool_override():
    """`@mcp.tool(timeout=N)` overrides the default — used for run_build_kernel
    (620s) and validator_run_holistic (920s)."""
    from _timeout import install_default_timeout

    registered: list[Any] = []

    class FakeMCP:
        def tool(self, *args, **kwargs):
            def decorator(fn):
                registered.append(fn)
                return fn
            return decorator

    fake = FakeMCP()
    install_default_timeout(fake, default_seconds=30.0)

    @fake.tool(timeout=620)
    def slow_subprocess_tool() -> dict[str, str]:
        return {"ok": "yes"}

    assert registered[0].__specfs_timeout_s__ == 620  # type: ignore[attr-defined]


def test_install_default_timeout_strips_timeout_kwarg_before_fastmcp():
    """FastMCP would reject an unknown `timeout=` kwarg. The patch must pop it
    before delegating, otherwise per-tool override breaks at registration."""
    from _timeout import install_default_timeout

    captured_kwargs: list[dict] = []

    class FakeMCP:
        def tool(self, **kwargs):
            captured_kwargs.append(dict(kwargs))
            def decorator(fn):
                return fn
            return decorator

    fake = FakeMCP()
    install_default_timeout(fake, default_seconds=30.0)

    @fake.tool(timeout=99, description="x")
    def my_tool() -> dict[str, str]:
        return {"ok": "yes"}

    # description should pass through, timeout should be popped
    assert captured_kwargs[0] == {"description": "x"}


def test_specfs_server_tools_have_timeout_attribute():
    """Smoke: specfs_server.py imports and registers tools with the timeout
    decorator applied. Pick a representative pure tool and a subprocess tool."""
    import specfs_server

    # session_status uses default budget
    assert hasattr(specfs_server.session_status, "__specfs_timeout_s__")
    # run_build_kernel overrides to 620
    assert specfs_server.run_build_kernel.__specfs_timeout_s__ == 620  # type: ignore[attr-defined]
    # validator_run_holistic overrides to 920
    assert specfs_server.validator_run_holistic.__specfs_timeout_s__ == 920  # type: ignore[attr-defined]
