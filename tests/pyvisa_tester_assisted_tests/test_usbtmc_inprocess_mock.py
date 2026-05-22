# -*- coding: utf-8 -*-
"""In-process USBTMC integration tests via pyvisa-tester pyusb monkeypatch."""

from __future__ import annotations

import pathlib
import sys
import time

import pytest

from pyvisa.constants import RENLineOperation, StatusCode, TriggerProtocol

from . import require_pyvisa_tester_assisted

pytestmark = [
    require_pyvisa_tester_assisted,
    pytest.mark.pyvisa_tester_assisted,
    pytest.mark.pyvisa_tester_usb,
]

pytest.importorskip("usb")

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_TESTER_ROOT = _REPO_ROOT.parent / "pyvisa-tester"
if _TESTER_ROOT.exists():
    sys.path.insert(0, str(_TESTER_ROOT))

usb_mock = pytest.importorskip("pyvisa_tester.usb_mock")
from pyvisa_py.protocols.usbtmc import USBTMC


@pytest.fixture()
def fake_usb(monkeypatch):
    return usb_mock.install_pyusb_mock(monkeypatch)


@pytest.fixture()
def fake_usb_permissive(monkeypatch):
    return usb_mock.install_pyusb_mock(monkeypatch, profile="permissive")


def test_open_query_close(fake_usb):
    dev = USBTMC(0xF4EC, 0xEE3A, "PYVISA0001")

    dev.write(b"*IDN?")
    answer = dev.read(256)
    assert b"Cyberdyne systems" in answer

    dev.write(b"QUERY?")
    assert dev.read(64).strip() == b"RESPONSE"

    dev.close()


def test_status_byte_and_trigger(fake_usb):
    dev = USBTMC(0xF4EC, 0xEE3A, "PYVISA0001")

    dev.write(b"EVEN:SRQ:ARM 1")
    dev.write(b"EVEN:SRQ:TRIG")

    stb, status = dev.read_stb()
    assert status == StatusCode.success
    assert stb & 0x40

    trigger_status = dev.assert_trigger(TriggerProtocol.default)
    assert trigger_status == StatusCode.success

    dev.close()


def test_ren_control(fake_usb):
    dev = USBTMC(0xF4EC, 0xEE3A, "PYVISA0001")

    status = dev.gpib_control_ren(RENLineOperation.asrt)
    assert status == StatusCode.success

    status = dev.gpib_control_ren(RENLineOperation.deassert)
    assert status == StatusCode.success

    dev.close()


def test_binary_generation_cfg_and_read(fake_usb):
    dev = USBTMC(0xF4EC, 0xEE3A, "PYVISA0001")

    dev.write(b"DATA:BIN? u16,4,le,ieee,none,ramp,7")
    direct = dev.read(128)

    dev.write(b"DATA:BIN:CFG u16,4,le,ieee,none,ramp,7")
    dev.write(b"DATA:BIN:READ?")
    staged = dev.read(128)

    assert direct == staged
    assert direct.startswith(b"#18")

    dev.close()


def test_timeout_injection(fake_usb):
    dev = USBTMC(0xF4EC, 0xEE3A, "PYVISA0001")

    dev.write(b"INJect:TIMEOut 25")
    start = time.perf_counter()
    dev.write(b"QUERY?")
    _ = dev.read(64)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert elapsed_ms >= 15

    dev.close()


def test_runtime_behavior_toggle(fake_usb_permissive):
    dev = USBTMC(0xF4EC, 0xEE3A, "PYVISA0001")

    dev.write(b"NOT_A_REAL_COMMAND")
    assert dev.read(64).startswith(b"ERR:UNSUPPORTED")

    dev.write(b"MOCK:BEHAVIOR reverse_unknown,1")
    dev.write(b"NOT_A_REAL_COMMAND")
    assert dev.read(64) == b"DNAMMOC_LAER_A_TON"

    dev.write(b"MOCK:PROFILE?")
    assert dev.read(64).strip() == b"permissive"

    dev.close()
