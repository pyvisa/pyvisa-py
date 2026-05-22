"""Serial loopback tests for pyvisa-py backend."""

import pytest

from pyvisa import ResourceManager
from tests import BaseTestCase

# TODO move this to pyvisa-tester


class TestSerial(BaseTestCase):
    """Test serial support through pyserial loopback."""

    serial = pytest.importorskip("serial", reason="PySerial not installed")

    def test_serial(self):
        msg = b"Test01234567890"

        available = ["loop://"]
        expected = []
        exp_missing = []
        missing = {}

        rm = ResourceManager("@py")
        try:
            dut = rm.open_resource("ASRLloop://::INSTR")
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
