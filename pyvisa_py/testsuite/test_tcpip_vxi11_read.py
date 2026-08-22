# -*- coding: utf-8 -*-
"""Unit tests for TCPIPInstrVxi11.read status resolution."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pyvisa.constants import ResourceAttribute, StatusCode
from pyvisa_py.protocols import vxi11
from pyvisa_py.tcpip import TCPIPInstrVxi11


class TestTCPIPInstrVxi11Read:
    def _make_session(
        self, *, termchar_enabled: bool = False, suppress_end_enabled: bool = False
    ) -> TCPIPInstrVxi11:
        sess = object.__new__(TCPIPInstrVxi11)
        sess.interface = MagicMock()
        sess.link = 1
        sess.lock_timeout = 10000
        sess.max_recv_size = 1024
        sess._io_timeout = 5000
        sess.timeout = 5
        sess.attrs = {
            ResourceAttribute.termchar_enabled: termchar_enabled,
            ResourceAttribute.termchar: ord("\n"),
            ResourceAttribute.suppress_end_enabled: suppress_end_enabled,
        }
        return sess

    def test_read_returns_termination_char_status_when_rx_chr_before_count(self):
        sess = self._make_session(termchar_enabled=True)
        sess.interface.device_read.return_value = (
            0,
            vxi11.RX_CHR,
            b"abc",
        )

        data, status = sess.read(10)

        assert data == b"abc"
        assert status == StatusCode.success_termination_character_read

    def test_read_with_negative_count_returns_invalid_parameter(self):
        sess = self._make_session(termchar_enabled=False)

        data, status = sess.read(-1)

        assert data == b""
        assert status == StatusCode.error_invalid_parameter
        sess.interface.device_read.assert_not_called()

    def test_read_with_zero_count_returns_success_max_count(self):
        sess = self._make_session(termchar_enabled=False)

        data, status = sess.read(0)

        assert data == b""
        assert status == StatusCode.success_max_count_read
        sess.interface.device_read.assert_not_called()

    def test_read_returns_success_when_rx_end_before_count(self):
        sess = self._make_session(termchar_enabled=False)
        sess.interface.device_read.return_value = (
            0,
            vxi11.RX_END,
            b"abc",
        )

        data, status = sess.read(10)

        assert data == b"abc"
        assert status == StatusCode.success

    @pytest.mark.parametrize("chunk_size", range(1, 11))
    def test_read_handles_exact_ten_byte_content_for_all_chunk_sizes(
        self, chunk_size: int
    ):
        sess = self._make_session(termchar_enabled=False, suppress_end_enabled=False)
        sess.max_recv_size = chunk_size
        content = b"abcdefghij"
        sess.interface.device_read.side_effect = [
            (0, 0, content[offset : offset + chunk_size])
            for offset in range(0, len(content), chunk_size)
        ]

        data, status = sess.read(len(content))

        assert data == content
        assert status == StatusCode.success_max_count_read

    @pytest.mark.parametrize("chunk_size", range(1, 11))
    def test_read_with_suppress_end_enabled_handles_exact_ten_byte_content_for_all_chunk_sizes(
        self, chunk_size: int
    ):
        sess = self._make_session(termchar_enabled=False, suppress_end_enabled=True)
        sess.max_recv_size = chunk_size
        content = b"abcdefghij"
        sess.interface.device_read.side_effect = [
            (0, vxi11.RX_END, content[offset : offset + chunk_size])
            for offset in range(0, len(content), chunk_size)
        ]

        data, status = sess.read(len(content))

        assert data == content
        assert status == StatusCode.success_max_count_read

    def test_read_with_suppress_end_enabled_ignores_rx_end(self):
        sess = self._make_session(termchar_enabled=False, suppress_end_enabled=True)
        sess.interface.device_read.side_effect = [
            (0, vxi11.RX_END, b"ab"),
            (vxi11.ErrorCodes.io_timeout, 0, b""),
        ]

        data, status = sess.read(10)

        assert data == b"ab"
        assert status == StatusCode.error_timeout

    def test_read_with_suppress_end_enabled_returns_max_count_when_count_is_reached(
        self,
    ):
        sess = self._make_session(termchar_enabled=False, suppress_end_enabled=True)
        sess.interface.device_read.return_value = (0, vxi11.RX_END, b"abc")

        data, status = sess.read(3)

        assert data == b"abc"
        assert status == StatusCode.success_max_count_read

    def test_read_with_suppress_end_enabled_still_honors_rx_chr(self):
        sess = self._make_session(termchar_enabled=True, suppress_end_enabled=True)
        sess.interface.device_read.return_value = (
            0,
            vxi11.RX_END | vxi11.RX_CHR,
            b"abc",
        )

        data, status = sess.read(10)

        assert data == b"abc"
        assert status == StatusCode.success_termination_character_read

    def test_read_returns_success_max_count_without_end_reason(self):
        sess = self._make_session(termchar_enabled=False)
        sess.interface.device_read.return_value = (
            0,
            0,
            b"abcd",
        )

        data, status = sess.read(4)

        assert data == b"abcd"
        assert status == StatusCode.success_max_count_read

    def test_read_timeout_returns_partial_data(self):
        sess = self._make_session(termchar_enabled=False)
        sess.interface.device_read.side_effect = [
            (0, 0, b"ab"),
            (vxi11.ErrorCodes.io_timeout, 0, b""),
        ]

        data, status = sess.read(10)

        assert data == b"ab"
        assert status == StatusCode.error_timeout

    def test_read_returns_timeout_when_total_timeout_is_expired(self, monkeypatch):
        sess = self._make_session(termchar_enabled=False)
        sess.timeout = 0.001
        sess._io_timeout = 1

        times = iter([100.0, 100.003])
        monkeypatch.setattr("pyvisa_py.tcpip.time.time", lambda: next(times))

        data, status = sess.read(10)

        assert data == b""
        assert status == StatusCode.error_timeout
        sess.interface.device_read.assert_not_called()

    def test_read_io_error_returns_partial_data(self):
        sess = self._make_session(termchar_enabled=False)
        sess.interface.device_read.side_effect = [
            (0, 0, b"ab"),
            (vxi11.ErrorCodes.io_error, 0, b""),
        ]

        data, status = sess.read(10)

        assert data == b"ab"
        assert status == StatusCode.error_io
