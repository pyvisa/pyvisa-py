"""Tests for HiSLIP asynchronous status queries."""

import threading
from unittest.mock import MagicMock

import pytest

from pyvisa_py.protocols.hislip import AsyncMessage, Instrument


@pytest.mark.parametrize(
    ("last_message_id", "next_message_id", "expected_message_id"),
    [
        (None, 0xFFFF_FF00, 0xFFFF_FEFE),
        (0xFFFF_FFFE, 0, 0xFFFF_FFFE),
        (0, 2, 0),
    ],
)
def test_status_query_uses_most_recent_message_id(
    last_message_id, next_message_id, expected_message_id
):
    instrument = object.__new__(Instrument)
    instrument._async_channel = MagicMock()
    instrument._async_channel.request.return_value = AsyncMessage(
        "AsyncStatusResponse", 0x42, 0, b""
    )
    instrument._message_id = next_message_id
    instrument.last_message_id = last_message_id
    instrument._rmt = 1
    instrument._status_query_lock = threading.Lock()

    assert instrument.async_status_query() == 0x42

    instrument._async_channel.request.assert_called_once_with(
        "AsyncStatusQuery",
        1,
        expected_message_id,
        expected_response="AsyncStatusResponse",
    )
