"""Test creating a resource manager using PyVISA-Py as a backend.


:copyright: 2014-2024 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

from unittest.mock import MagicMock

import pytest

from pyvisa import ResourceManager, constants

try:
    from pyvisa_py import serial as serial_backend
    from pyvisa_py.serial import SerialSession
except ImportError:
    pass

from pyvisa.testsuite import BaseTestCase


class TestSerial(BaseTestCase):
    """Test generic property of PyVisaLibrary."""

    serial = pytest.importorskip("serial", reason="PySerial not installed")

    @pytest.mark.parametrize("is_win", [True, False])
    @pytest.mark.parametrize(
        "board",
        [
            "loop://",
            "loop://?logging=warning",
            "socket://127.0.0.1:7777",
            "rfc2217://localhost:7000?timeout=1",
            "spy://COM54?color",
            "custom_handler://device",
        ],
    )
    def test_url_is_passed_unchanged(self, monkeypatch, is_win, board):
        """Leave URL dispatch, including custom handlers, to PySerial."""
        monkeypatch.setattr(serial_backend, "IS_WIN", is_win)
        factory = MagicMock()
        monkeypatch.setattr(serial_backend.serial, "serial_for_url", factory)

        session = SerialSession(0, f"ASRL{board}::INSTR")
        try:
            factory.assert_called_once_with(
                board, timeout=session.timeout, write_timeout=session.timeout
            )
        finally:
            session.close()

    @pytest.mark.parametrize(
        "is_win, board, expected_port",
        [
            (True, "1", "COM1"),
            (True, "12", "COM12"),
            (False, "1", "1"),
            (False, "/dev/ttyUSB0", "/dev/ttyUSB0"),
        ],
    )
    def test_native_port_mapping(self, monkeypatch, is_win, board, expected_port):
        """Preserve numeric Windows boards and native POSIX device names."""
        monkeypatch.setattr(serial_backend, "IS_WIN", is_win)
        factory = MagicMock()
        monkeypatch.setattr(serial_backend.serial, "serial_for_url", factory)

        session = SerialSession(0, f"ASRL{board}::INSTR")
        try:
            factory.assert_called_once_with(
                expected_port, timeout=session.timeout, write_timeout=session.timeout
            )
        finally:
            session.close()

    @pytest.mark.parametrize("is_win", [True, False])
    def test_url_loopback(self, monkeypatch, is_win):
        """Use the real PySerial URL handler through the ResourceManager API."""
        monkeypatch.setattr(serial_backend, "IS_WIN", is_win)
        rm = ResourceManager("@py")
        try:
            with rm.open_resource("ASRLloop://::INSTR") as resource:
                resource.timeout = 500
                resource.read_termination = "\n"
                resource.write_termination = "\n"
                assert resource.query("serial URL") == "serial URL"
        finally:
            rm.close()

    def test_serial(self):
        """Test loop://"""
        msg = b"Test01234567890"

        available = ["loop://"]
        expected = []
        exp_missing = []
        missing = {}

        rm = ResourceManager("@py")
        try:
            dut = rm.open_resource("ASRLloop://::INSTR")
            print("opened")
            dut.timeout = 3000
            dut.read_termination = "\r\n"
            dut.write_termination = "\r\n"
            dut.write(str(msg))
            ret_val = dut.read()
            if str(msg) == ret_val:
                expected = ["loop://"]

        except Exception:
            exp_missing = ["loop://"]

        assert sorted(available) == sorted(expected)
        assert sorted(missing) == sorted(exp_missing)

    @pytest.mark.parametrize(
        "io_protocol, expected_command",
        [
            (constants.VI_PROT_NORMAL, None),
            (constants.VI_PROT_4882_STRS, b"*CLS\n"),
        ],
    )
    def test_clear(self, io_protocol, expected_command):
        """Clear serial buffers and send *CLS only with the 488.2 protocol."""
        session = object.__new__(SerialSession)
        session.attrs = {constants.ResourceAttribute.io_prot: io_protocol}
        session.interface = MagicMock()
        session.write = MagicMock()

        assert session.clear() == constants.StatusCode.success

        session.interface.reset_output_buffer.assert_called_once_with()
        session.interface.sendBreak.assert_called_once_with()
        session.interface.reset_input_buffer.assert_called_once_with()
        if expected_command is None:
            session.write.assert_not_called()
        else:
            session.write.assert_called_once_with(expected_command)
