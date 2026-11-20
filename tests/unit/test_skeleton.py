"""S0 (design §15): the test setup refuses the network, and the pinned lite-model has the
`turn()` interface the loop will be built on (design §13)."""

import socket

import pytest
from lite_model import AssistantTurn, Message, Text, ToolCall, ToolResult, ToolResults, ToolSpec
from lite_model.testing import AsyncFakeModel


def test_a_test_cannot_open_a_network_connection() -> None:
    with pytest.raises(AssertionError, match="tried to open a network connection"):
        socket.create_connection(("127.0.0.1", 9))
    with pytest.raises(AssertionError, match="tried to open a network connection"):
        socket.socket().connect(("127.0.0.1", 9))


def test_a_test_cannot_send_udp_or_resolve_a_name() -> None:
    # Codex review 2026-11-21: connect() is not the only way out.
    with pytest.raises(AssertionError, match="tried to open a network connection"):
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("127.0.0.1", 9))
    with pytest.raises(AssertionError, match="tried to open a network connection"):
        socket.getaddrinfo("example.invalid", 80)


async def test_the_pinned_lite_model_scripts_a_tool_round() -> None:
    clock = ToolSpec("get_time", "Current time.", {"type": "object"})
    model = AsyncFakeModel(
        agent=[AssistantTurn((ToolCall("c1", "get_time", {}),)), AssistantTurn((Text("Noon."),))]
    )
    ask = [Message("user", "Time?")]

    first = await model.turn("agent", ask, [clock])
    assert (first.stop, [c.id for c in first.turn.tool_calls]) == ("tool_calls", ["c1"])
    done = ToolResults((ToolResult("c1", "12:00"),))
    second = await model.turn("agent", [*ask, first.turn, done], [clock])

    assert (second.stop, second.turn.text) == ("end", "Noon.")
    assert model.turn_calls[1].messages[-1] == done
