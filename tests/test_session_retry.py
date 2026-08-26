from cursor_sdk.errors import AgentBusyError, InternalServerError

from cursor_bridge.sessions import (
    AGENT_BUSY_BACKOFF_SEC,
    INSTANT_EMPTY_ERROR_SEC,
    _is_agent_busy,
    _is_instant_empty_error,
    _is_internal_server_error,
    _is_stuck_agent,
)


def test_busy_backoff_cap_under_15s() -> None:
    assert len(AGENT_BUSY_BACKOFF_SEC) == 2
    assert sum(AGENT_BUSY_BACKOFF_SEC) <= 15.0


def test_agent_busy_does_not_count_as_internal_error() -> None:
    busy = AgentBusyError("agent busy")
    ise = InternalServerError("internal server error")
    assert _is_agent_busy(busy)
    assert _is_agent_busy(RuntimeError("Agent Busy"))
    assert not _is_internal_server_error(busy)
    assert _is_internal_server_error(ise)
    assert not _is_agent_busy(ise)
    assert _is_stuck_agent(busy)
    assert _is_stuck_agent(ise)


def test_instant_empty_window_is_10s() -> None:
    assert INSTANT_EMPTY_ERROR_SEC == 10.0
    kwargs = dict(
        stalled=False,
        rstatus="error",
        final="",
        text_parts=[],
        tool_hits=0,
    )
    assert _is_instant_empty_error(elapsed_sec=9.9, **kwargs)
    assert not _is_instant_empty_error(elapsed_sec=10.0, **kwargs)
    assert not _is_instant_empty_error(elapsed_sec=4.0, stalled=True, **{
        k: v for k, v in kwargs.items() if k != "stalled"
    })
    assert not _is_instant_empty_error(
        elapsed_sec=2.0,
        stalled=False,
        rstatus="error",
        final="oops",
        text_parts=[],
        tool_hits=0,
    )
