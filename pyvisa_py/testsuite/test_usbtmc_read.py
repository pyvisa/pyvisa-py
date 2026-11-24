"""Test USBTMC reads against a simulated device (no hardware required)."""

import errno
import struct
from collections import deque
from contextlib import closing
from types import SimpleNamespace

import pytest

import pyvisa
from pyvisa.constants import InterfaceType, StatusCode
from pyvisa_py.sessions import Session

usb = pytest.importorskip("usb")
pytestmark = pytest.mark.skipif(
    (InterfaceType.usb, "INSTR") not in dict(Session.iter_valid_session_classes()),
    reason="PyUSB and a usable backend are required",
)

from pyvisa_py.protocols import usbtmc  # noqa: E402

IDN = b"ACME,SNA5032A,TEST,1.0\n"


class FakeUSBTMCDevice:
    """Minimal USBTMC device exchanging packets like a high-speed bulk pipe.

    Bulk-IN transfers are split in wMaxPacketSize packets and a transfer whose
    length is a multiple of wMaxPacketSize is followed by a zero-length packet
    (USB 2.0 5.8.3, USBTMC 1.0 3.3). Reads behave like a libusb bulk transfer:
    they complete on a short packet (consuming it, ZLP included) or once the
    requested length has been received; an empty pipe times out.
    Pending output is never discarded, so any leftover shows up as a desync.
    """

    max_packet = 512

    def __init__(self, replies, max_transfer_payload=None):
        self.replies = replies
        self.max_transfer_payload = max_transfer_payload
        self.output = bytearray()
        self.command = bytearray()
        self.in_pipe = deque()
        self.raw_reads = []

    # Bulk-OUT
    def bulk_out(self, data, timeout=None):
        msgid, btag = data[0], data[1]
        if msgid == usbtmc.MsgID.dev_dep_msg_out:
            size, attributes = struct.unpack_from("<LB", data, 4)
            self.command.extend(data[12 : 12 + size])
            if attributes & 1:
                command = bytes(self.command).strip()
                self.command.clear()
                if command.endswith(b"?"):
                    self.output.extend(self.replies[command])
        elif msgid == usbtmc.MsgID.request_dev_dep_msg_in:
            (transfer_size,) = struct.unpack_from("<L", data, 4)
            self._send_transfer(btag, transfer_size)
        return len(data)

    def _send_transfer(self, btag, transfer_size):
        if not self.output:
            return  # A real device holds the request until a reply exists.
        size = min(transfer_size, len(self.output))
        if self.max_transfer_payload is not None:
            size = min(size, self.max_transfer_payload)
        payload = bytes(self.output[:size])
        del self.output[:size]
        eom = not self.output
        transfer = (
            struct.pack("BBBx", usbtmc.MsgID.dev_dep_msg_in, btag, ~btag & 0xFF)
            + struct.pack("<LBxxx", size, eom)
            + payload
        )
        transfer += b"\0" * (-len(transfer) % 4)
        for i in range(0, len(transfer), self.max_packet):
            self.in_pipe.append(transfer[i : i + self.max_packet])
        if len(transfer) % self.max_packet == 0:
            self.in_pipe.append(b"")

    # Bulk-IN
    def bulk_in(self, size, timeout=None):
        data = bytearray()
        while True:
            if not self.in_pipe:
                raise usb.core.USBError("Operation timed out", errno=errno.ETIMEDOUT)
            if len(data) + len(self.in_pipe[0]) > size:
                raise usb.core.USBError("Overflow", errno=errno.EOVERFLOW)
            packet = self.in_pipe.popleft()
            data.extend(packet)
            if len(packet) < self.max_packet or len(data) == size:
                break
        self.raw_reads.append(len(data))
        return memoryview(bytes(data))

    # Control endpoint (only what Bulk-IN abort needs)
    def ctrl_transfer(self, request_type, request, value, index, length, timeout=None):
        if request == usbtmc.Request.initiate_abort_bulk_in:
            if not (self.in_pipe or self.output):
                return [usbtmc.UsbTmcStatus.transfer_not_in_progress, value]
            self.in_pipe.clear()
            self.output.clear()
            self.in_pipe.append(b"")  # short packet ending the aborted transfer
            return [usbtmc.UsbTmcStatus.success, value]
        if request == usbtmc.Request.check_abort_bulk_in_status:
            return [usbtmc.UsbTmcStatus.success] + [0] * 7
        raise NotImplementedError(request)


def make_interface(device):
    interface = object.__new__(usbtmc.USBTMC)
    interface.timeout = 1000
    interface.usb_dev = SimpleNamespace(ctrl_transfer=device.ctrl_transfer)
    interface.usb_recv_ep = SimpleNamespace(
        read=device.bulk_in,
        wMaxPacketSize=device.max_packet,
        bEndpointAddress=0x81,
    )
    interface.usb_send_ep = SimpleNamespace(
        write=device.bulk_out, wMaxPacketSize=device.max_packet
    )
    interface._btag = usbtmc.BTag(1, 255)
    interface.close = lambda: None
    return interface


@pytest.fixture
def open_instrument(monkeypatch):
    resource_managers = []

    def _open(device, **kwargs):
        interface = make_interface(device)
        session_class = Session.get_session_class(InterfaceType.usb, "INSTR")
        monkeypatch.setattr(session_class, "_intf_cls", lambda *args: interface)
        rm = pyvisa.ResourceManager("@py")
        resource_managers.append(rm)
        return rm.open_resource("USB0::0x1234::0x5678::TEST::INSTR", **kwargs)

    yield _open
    for rm in resource_managers:
        rm.close()


def ascii_reply(length):
    """Comma separated values terminated by a newline, exactly ``length`` long."""
    values = ",".join("%+.10E" % (i * 1.5e-3) for i in range(length // 10))
    return values[: length - 1].encode() + b"\n"


def test_from_bytes_rejects_short_packets():
    for data in (b"", b"\x02\x01\xfe", bytes(11)):
        with pytest.raises(ValueError):
            usbtmc.BulkInMessage.from_bytes(data)
        with pytest.raises(ValueError):
            usbtmc.BulkInMessage.from_quirky(data)


def test_zero_length_packet_before_header(open_instrument):
    """A stale ZLP where the Bulk-IN header is expected is skipped."""
    reply = ascii_reply(1000)
    device = FakeUSBTMCDevice({b"DATA?": reply, b"*IDN?": IDN})
    with closing(open_instrument(device, read_termination="\n")) as inst:
        device.in_pipe.append(b"")
        assert inst.query("DATA?") == reply[:-1].decode()
        assert inst.query("*IDN?") == IDN[:-1].decode()
    assert not device.in_pipe and not device.output


@pytest.mark.parametrize("reply_length", [512 - 12, 3 * 512 - 12])
def test_transfer_multiple_of_packet_size_followed_by_zlp(
    open_instrument, reply_length
):
    """Transfers ending on a packet boundary (followed by a ZLP) that do not
    carry EOM, because the device limits the payload of each transfer."""
    reply = ascii_reply(2000)
    device = FakeUSBTMCDevice(
        {b"DATA?": reply, b"*IDN?": IDN}, max_transfer_payload=reply_length
    )
    with closing(open_instrument(device, read_termination="\n")) as inst:
        assert inst.query("DATA?") == reply[:-1].decode()
        assert inst.query("*IDN?") == IDN[:-1].decode()
    assert not device.in_pipe and not device.output


def test_read_size_with_padding_fills_packet(open_instrument):
    """Header + payload + alignment padding exactly fill the requested length:
    the ZLP that follows must not be left in the pipe."""
    reply = ascii_reply(2000)
    device = FakeUSBTMCDevice({b"DATA?": reply, b"*IDN?": IDN})
    with closing(open_instrument(device, read_termination="\n")) as inst:
        # 12 header bytes + 497 payload bytes + 3 alignment bytes == 512
        inst.chunk_size = 497
        assert inst.query("DATA?") == reply[:-1].decode()
        assert inst.query("*IDN?") == IDN[:-1].decode()
    assert not device.in_pipe and not device.output


@pytest.mark.parametrize("read_termination", ["\n", None])
def test_long_reply_spanning_several_reads(open_instrument, read_termination):
    """A reply longer than chunk_size is read completely (no END after the
    first chunk) and the next query is in sync."""
    reply = ascii_reply(38683)
    device = FakeUSBTMCDevice({b"DATA?": reply, b"*IDN?": IDN})
    with closing(open_instrument(device, read_termination=read_termination)) as inst:
        assert inst.chunk_size == 20 * 1024
        inst.write("DATA?")
        assert inst.read_raw() == reply
        assert inst.query("*IDN?").rstrip("\n") == IDN[:-1].decode()
        expected = reply.decode() if read_termination is None else reply[:-1].decode()
        assert inst.query("DATA?") == expected
        assert inst.query("*IDN?").rstrip("\n") == IDN[:-1].decode()
    assert not device.in_pipe and not device.output


def test_partial_read_reports_max_count(open_instrument):
    """Reading fewer bytes than the message returns VI_SUCCESS_MAX_CNT and the
    rest of the message is returned by the following read."""
    reply = ascii_reply(3000)
    device = FakeUSBTMCDevice({b"DATA?": reply})
    with closing(open_instrument(device)) as inst:
        inst.write("DATA?")
        with inst.ignore_warning(StatusCode.success_max_count_read):
            data, status = inst.visalib.read(inst.session, 1000)
        assert (data, status) == (reply[:1000], StatusCode.success_max_count_read)
        data, status = inst.visalib.read(inst.session, 5000)
        assert (data, status) == (reply[1000:], StatusCode.success)
