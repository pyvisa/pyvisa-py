"""Tests for HiSLIP asynchronous control transactions."""

from unittest.mock import MagicMock

import pytest

from pyvisa_py.protocols.hislip import AsyncMessage, Instrument


@pytest.mark.parametrize(
    ("method_name", "arguments", "message_type", "expected_response"),
    [
        (
            "async_lock_release",
            (),
            "AsyncLock",
            "AsyncLockResponse",
        ),
        (
            "async_remote_local_control",
            ("disableRemote",),
            "AsyncRemoteLocalControl",
            "AsyncRemoteLocalResponse",
        ),
    ],
)
@pytest.mark.parametrize(
    ("last_message_id", "next_message_id", "expected_message_id"),
    [
        (None, 0xFFFF_FF00, 0xFFFF_FEFE),
        (0xFFFF_FFFE, 0, 0xFFFF_FFFE),
        (0, 2, 0),
    ],
)
def test_async_control_uses_most_recent_message_id(
    method_name,
    arguments,
    message_type,
    expected_response,
    last_message_id,
    next_message_id,
    expected_message_id,
):
    instrument = object.__new__(Instrument)
    instrument._async_channel = MagicMock()
    instrument._async_channel.request.return_value = AsyncMessage(
        expected_response, 0, 0, b""
    )
    instrument._message_id = next_message_id
    instrument.last_message_id = last_message_id

    getattr(instrument, method_name)(*arguments)

    instrument._async_channel.request.assert_called_once_with(
        message_type,
        0,
        expected_message_id,
        expected_response=expected_response,
    )
