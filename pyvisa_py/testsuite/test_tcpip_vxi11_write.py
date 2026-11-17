# -*- coding: utf-8 -*-
"""Unit tests for TCPIPInstrVxi11.write byte counting and status.

:copyright: 2014-2024 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest

from pyvisa.constants import ResourceAttribute, StatusCode
from pyvisa_py.protocols import vxi11
from pyvisa_py.tcpip import TCPIPInstrVxi11

NO_ERROR = vxi11.ErrorCodes.no_error


def _make_session(
    device_write_results, *, max_recv_size: int = 1024, send_end: bool = True
) -> TCPIPInstrVxi11:
    sess = object.__new__(TCPIPInstrVxi11)
    sess.interface = MagicMock()
    sess.interface.device_write.side_effect = device_write_results
    sess.link = 1
    sess.max_recv_size = max_recv_size
    sess._io_timeout = 5000
    sess.attrs = {
        ResourceAttribute.send_end_enabled: send_end,
        ResourceAttribute.lockwait: 0,  # type: ignore[attr-defined]
    }
    return sess


def _sent_blocks(sess: TCPIPInstrVxi11) -> list[bytes]:
    return [c.args[4] for c in sess.interface.device_write.call_args_list]


def test_full_write():
    sess = _make_session([(NO_ERROR, 5)])

    assert sess.write(b"*RST\n") == (5, StatusCode.success)
    assert sess.interface.device_write.call_args_list == [
        call(1, 5000, 0, vxi11.OP_FLAG_END, b"*RST\n")
    ]


def test_short_write_sends_the_rest():
    """A device may accept part of a block without an error.

    write() used to return VI_SUCCESS at that point, with a count that left out
    the accepted bytes, and never sent the rest of the message.
    """
    sess = _make_session([(NO_ERROR, 2), (NO_ERROR, 3)])

    assert sess.write(b"*RST\n") == (5, StatusCode.success)
    assert _sent_blocks(sess) == [b"*RST\n", b"ST\n"]


def test_short_write_across_blocks_keeps_end_on_last_block_only():
    sess = _make_session(
        [(NO_ERROR, 3), (NO_ERROR, 1), (NO_ERROR, 4), (NO_ERROR, 2)],
        max_recv_size=4,
    )

    assert sess.write(b"0123456789") == (10, StatusCode.success)
    assert _sent_blocks(sess) == [b"0123", b"3456", b"4567", b"89"]
    flags = [c.args[3] for c in sess.interface.device_write.call_args_list]
    assert flags == [0, 0, 0, vxi11.OP_FLAG_END]


def test_no_progress_without_error_is_an_io_error():
    """Accepting nothing without an error must not make write() loop forever."""
    sess = _make_session([(NO_ERROR, 2), (NO_ERROR, 0)])

    assert sess.write(b"*RST\n") == (2, StatusCode.error_io)
    assert sess.interface.device_write.call_count == 2


@pytest.mark.parametrize(
    "error, expected_status",
    [
        (vxi11.ErrorCodes.io_timeout, StatusCode.error_timeout),
        (vxi11.ErrorCodes.device_not_accessible, StatusCode.error_connection_lost),
    ],
)
def test_error_counts_bytes_accepted_in_the_failing_call(error, expected_status):
    sess = _make_session([(NO_ERROR, 4), (error, 3)], max_recv_size=4)

    assert sess.write(b"0123456789") == (7, expected_status)


def test_count_never_exceeds_the_data_sent():
    sess = _make_session([(NO_ERROR, 99)])

    assert sess.write(b"*RST\n") == (5, StatusCode.success)
