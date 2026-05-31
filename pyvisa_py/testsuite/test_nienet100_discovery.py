# -*- coding: utf-8 -*-
"""Offline unit tests for the NI GPIB-ENET/100 UDP discovery protocol.

Pack/parse helpers run pure offline. The discover() loop is exercised
via socket.socket patching so the test fixtures do not depend on
broadcast-capable network interfaces or on port 44515 being free.

:copyright: 2026 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

import socket
import struct
from unittest import mock

import pytest

from pyvisa_py.protocols import nienet100_discovery as d

# --- frame builders --------------------------------------------------------


def _build_response(
    ip: str = "192.0.2.5",
    mac_bytes: bytes = bytes.fromhex("00802f1a2b3c"),
    serial: int = 1234567,
    model: str = "GPIB-ENET/100",
    hostname: str = "lab-vna",
    comment: str = "by the door",
    subnet: str = "255.255.255.0",
    gateway: str = "192.0.2.1",
    nonce: int = 0,
    op_code: int = d.OP_OK,
) -> bytes:
    """Build a valid 184-byte discovery reply frame for testing."""
    buf = bytearray(d.FRAME_SIZE)
    buf[0:2] = d.MAGIC_HEAD
    buf[0x02] = op_code
    buf[0x05] = d.PROTOCOL_VERSION
    struct.pack_into("!L", buf, 0x06, nonce & 0xFFFFFFFF)
    struct.pack_into("!L", buf, 0x0A, serial & 0xFFFFFFFF)
    buf[0x0E : 0x0E + 6] = mac_bytes
    encoded_model = model.encode("ascii")
    buf[0x1C : 0x1C + len(encoded_model)] = encoded_model
    encoded_host = hostname.encode("ascii")
    buf[0x3C : 0x3C + len(encoded_host)] = encoded_host
    encoded_comment = comment.encode("ascii")
    buf[0x5C : 0x5C + len(encoded_comment)] = encoded_comment
    buf[0x9E : 0x9E + 4] = socket.inet_aton(ip)
    buf[0xA2 : 0xA2 + 4] = socket.inet_aton(subnet)
    buf[0xAA : 0xAA + 4] = socket.inet_aton(gateway)
    buf[0xB6:0xB8] = d.MAGIC_TAIL
    return bytes(buf)


# --- pack_discovery_request ------------------------------------------------


def test_pack_discovery_request_layout():
    frame = d.pack_discovery_request()
    assert len(frame) == d.FRAME_SIZE
    assert frame[0:2] == d.MAGIC_HEAD
    assert frame[0x02] == d.OP_DISCOVER
    assert frame[0x05] == d.PROTOCOL_VERSION
    assert frame[0xB6:0xB8] == d.MAGIC_TAIL


def test_pack_discovery_request_nonce_is_big_endian():
    frame = d.pack_discovery_request(nonce=0xDEADBEEF)
    assert frame[0x06:0x0A] == bytes.fromhex("deadbeef")


def test_pack_discovery_request_zeroes_unset_fields():
    frame = d.pack_discovery_request(nonce=0xABCD)
    # Everything outside the small set of always-on fields and the nonce
    # must remain zero.
    set_offsets = {0, 1, 2, 5, 6, 7, 8, 9, 0xB6, 0xB7}
    for i, byte in enumerate(frame):
        if i not in set_offsets:
            assert byte == 0, "byte %d should be 0, got 0x%02x" % (i, byte)


# --- parse_discovery_response (happy paths) --------------------------------


def test_parse_response_extracts_all_fields():
    info = d.parse_discovery_response(
        _build_response(
            ip="192.0.2.5",
            mac_bytes=bytes.fromhex("00802fdeadbe"),
            serial=99,
            model="GPIB-ENET/100",
            hostname="vna-01",
            comment="rack 3, shelf 2",
            subnet="255.255.255.128",
            gateway="192.0.2.129",
            nonce=0x12345678,
            op_code=d.OP_OK,
        )
    )
    assert info is not None
    assert info.ip == "192.0.2.5"
    assert info.mac == "00:80:2f:de:ad:be"
    assert info.serial == 99
    assert info.model == "GPIB-ENET/100"
    assert info.hostname == "vna-01"
    assert info.comment == "rack 3, shelf 2"
    assert info.subnet == "255.255.255.128"
    assert info.gateway == "192.0.2.129"
    assert info.nonce == 0x12345678
    assert info.op_code == d.OP_OK
    assert info.is_busy is False


def test_parse_response_busy_flag():
    info = d.parse_discovery_response(_build_response(op_code=d.OP_BUSY))
    assert info is not None
    assert info.op_code == d.OP_BUSY
    assert info.is_busy is True


def test_parse_response_handles_empty_strings():
    info = d.parse_discovery_response(_build_response(hostname="", comment=""))
    assert info is not None
    assert info.hostname == ""
    assert info.comment == ""


def test_parse_response_truncates_at_first_null():
    # Spec strings are null-terminated within their fixed-size buffer;
    # anything past the first NUL must be ignored even if it looks like
    # ASCII (which the box can leave behind from a prior longer value).
    buf = bytearray(_build_response(hostname="short"))
    # Put garbage after the NUL terminator that follows "short"
    buf[0x3C + 6 : 0x3C + 16] = b"GARBAGE!!!"
    info = d.parse_discovery_response(bytes(buf))
    assert info is not None
    assert info.hostname == "short"


# --- parse_discovery_response (validation) --------------------------------


@pytest.mark.parametrize(
    "mutator,reason",
    [
        (lambda b: b[:100], "too short"),
        (lambda b: b + b"\x00", "too long"),
        (lambda b: b"AA" + b[2:], "bad head magic"),
        (lambda b: b[:0xB6] + b"AA", "bad tail magic"),
        (
            lambda b: b[:0x02] + bytes([d.OP_DISCOVER]) + b[0x03:],
            "probe op-code, not response",
        ),
        (lambda b: b[:0x02] + b"\xff" + b[0x03:], "unknown op-code"),
    ],
)
def test_parse_response_rejects_invalid_frames(mutator, reason):
    bad = mutator(_build_response())
    assert d.parse_discovery_response(bad) is None, "should reject: %s" % reason


def test_parse_response_rejects_truly_foreign_traffic():
    # Random UDP traffic from other devices on the LAN must not raise.
    assert d.parse_discovery_response(b"") is None
    assert d.parse_discovery_response(b"hello") is None
    assert d.parse_discovery_response(b"\x00" * 184) is None


# --- discover() with patched socket layer ---------------------------------


def _make_fake_socket(recv_script):
    """Return a Mock socket whose recvfrom plays back ``recv_script``.

    Each script item is either ``bytes`` (returned as ``(data, ("peer", 0))``)
    or an Exception (raised). After the script is exhausted, recvfrom raises
    socket.timeout to terminate the discover loop.
    """
    fake = mock.MagicMock(spec=socket.socket)

    iterator = iter(recv_script)

    def fake_recvfrom(_bufsize):
        try:
            item = next(iterator)
        except StopIteration:
            raise socket.timeout from None
        if isinstance(item, BaseException):
            raise item
        return item, ("203.0.113.1", d.PORT_BROADCAST)

    fake.recvfrom.side_effect = fake_recvfrom
    return fake


def test_discover_returns_boxes_sorted_by_ip():
    fake = _make_fake_socket(
        [
            _build_response(ip="192.0.2.99"),
            _build_response(
                ip="192.0.2.5",
                mac_bytes=bytes.fromhex("00802fdeadbe"),
            ),
            _build_response(
                ip="192.0.2.50",
                mac_bytes=bytes.fromhex("00802fcafe01"),
            ),
        ]
    )
    with mock.patch("socket.socket", return_value=fake):
        boxes = d.discover(timeout=1.0)
    assert [b.ip for b in boxes] == ["192.0.2.5", "192.0.2.50", "192.0.2.99"]


def test_discover_deduplicates_by_mac():
    # Same box answers twice (e.g. multi-homed host). With dedup we get one
    # entry; with dedup=False we get both.
    mac = bytes.fromhex("00802fdeadbe")
    fake = _make_fake_socket(
        [
            _build_response(ip="192.0.2.5", mac_bytes=mac),
            _build_response(ip="192.0.2.5", mac_bytes=mac),
        ]
    )
    with mock.patch("socket.socket", return_value=fake):
        boxes = d.discover(timeout=1.0)
    assert len(boxes) == 1


def test_discover_can_disable_deduplication():
    mac = bytes.fromhex("00802fdeadbe")
    fake = _make_fake_socket(
        [
            _build_response(ip="192.0.2.5", mac_bytes=mac),
            _build_response(ip="192.0.2.5", mac_bytes=mac),
        ]
    )
    with mock.patch("socket.socket", return_value=fake):
        boxes = d.discover(timeout=1.0, deduplicate=False)
    assert len(boxes) == 2


def test_discover_skips_foreign_datagrams():
    fake = _make_fake_socket(
        [
            b"random udp from another device",  # rejected by parse
            _build_response(ip="192.0.2.5"),
        ]
    )
    with mock.patch("socket.socket", return_value=fake):
        boxes = d.discover(timeout=1.0)
    assert [b.ip for b in boxes] == ["192.0.2.5"]


def test_discover_tolerates_connection_reset():
    # Windows surfaces ICMP port-unreachable on UDP as ConnectionResetError;
    # one such error must not abort the whole scan.
    fake = _make_fake_socket(
        [
            ConnectionResetError(10054, "WSAECONNRESET"),
            _build_response(ip="192.0.2.5"),
        ]
    )
    with mock.patch("socket.socket", return_value=fake):
        boxes = d.discover(timeout=1.0)
    assert [b.ip for b in boxes] == ["192.0.2.5"]


def test_discover_returns_empty_list_on_timeout():
    fake = _make_fake_socket([])  # immediate timeout
    with mock.patch("socket.socket", return_value=fake):
        boxes = d.discover(timeout=0.05)
    assert boxes == []


def test_discover_sends_probe_to_broadcast_destination():
    fake = _make_fake_socket([])
    with mock.patch("socket.socket", return_value=fake):
        d.discover(timeout=0.01, broadcast_addr="203.0.113.255", port=44777)
    # One sendto call with the probe frame addressed to the configured
    # broadcast + port.
    assert fake.sendto.call_count == 1
    sent_bytes, dest = fake.sendto.call_args.args
    assert dest == ("203.0.113.255", 44777)
    assert len(sent_bytes) == d.FRAME_SIZE
    assert sent_bytes[0:2] == d.MAGIC_HEAD
    assert sent_bytes[0x02] == d.OP_DISCOVER


def test_discover_wraps_bind_error_as_oserror():
    fake = mock.MagicMock(spec=socket.socket)
    fake.bind.side_effect = OSError(98, "Address already in use")
    with mock.patch("socket.socket", return_value=fake):
        with pytest.raises(OSError, match="could not bind UDP port"):
            d.discover(timeout=0.1, port=44788)
