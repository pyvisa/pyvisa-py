# -*- coding: utf-8 -*-
"""Offline unit tests for the NI GPIB-ENET/100 wire protocol primitives.

These tests cover :mod:`pyvisa_py.protocols.nienet100` without touching the
network: frame pack/unpack, chunk parsing, IP/TMO conversion helpers, and
the device verbs driven against scripted in-memory peers over a Unix
``socketpair``. Hardware-gated integration tests against a real bridge
live elsewhere.

:copyright: 2026 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

import io
import socket
import struct
import threading
from typing import List, Tuple

import pytest

from pyvisa_py.protocols import nienet100

# --- frame pack / unpack ----------------------------------------------------


def test_pack_command_zeroes_unset_fields():
    frame = nienet100.pack_command(0x04)
    assert frame == b"\x04\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    assert len(frame) == nienet100.COMMAND_FRAME_SIZE


def test_pack_command_layout():
    # Frame A (SetConfig SC) skeleton: 07 02 00 01 [PAD=16] [SAD=0] 00 00
    # [tmo=0x0d] 00 04 00. PAD/SAD pack into the w2 ushort as (PAD<<8)|SAD.
    frame = nienet100.pack_command(
        cmd_id=0x07,
        b1=0x02,
        w1=0x0001,
        w2=(16 << 8) | 0,
        w3=0,
        dw=(nienet100.TMO_10s << 24) | 0x0400,
    )
    assert frame.hex() == "07020001" + "10000000" + "0d000400"


def test_parse_status_header_ok():
    raw = struct.pack("!HH4xL", nienet100.STA_CMPL, 0x0000, 42)
    sta, err, cnt = nienet100.parse_status_header(raw)
    assert sta == nienet100.STA_CMPL
    assert err == 0
    assert cnt == 42


def test_parse_status_header_err_sentinel():
    # err=0xFFFF is a documented sentinel that callers must ignore unless
    # STA_ERR is set.
    raw = struct.pack("!HH4xL", nienet100.STA_CMPL, 0xFFFF, 0)
    sta, err, cnt = nienet100.parse_status_header(raw)
    assert sta == nienet100.STA_CMPL
    assert err == 0xFFFF
    assert cnt == 0


def test_parse_status_header_rejects_wrong_size():
    with pytest.raises(ValueError):
        nienet100.parse_status_header(b"\x00" * 11)


def test_parse_chunk_header():
    flags, length = nienet100.parse_chunk_header(b"\x00\x01\x00\x40")
    assert flags == 1
    assert length == 0x40


# --- chunk reader -----------------------------------------------------------


def _reader(blob: bytes):
    """Build a `read_exactly`-style callable backed by an in-memory blob."""
    stream = io.BytesIO(blob)

    def read_exactly(n: int) -> bytes:
        data = stream.read(n)
        if len(data) != n:
            raise EOFError("short read")
        return data

    return read_exactly


def _chunk(flags: int, payload: bytes) -> bytes:
    return struct.pack("!HH", flags, len(payload)) + payload


def test_read_chunks_until_end_concatenates():
    blob = _chunk(0, b"ABC") + _chunk(0, b"DE") + _chunk(1, b"")
    assert nienet100.read_chunks_until_end(_reader(blob)) == b"ABCDE"


def test_read_chunks_until_end_tolerates_signal_chunk():
    # signal-byte chunks (flags=2) carry 1 OOB byte; the reader logs and
    # skips them rather than treating them as protocol errors.
    blob = _chunk(0, b"X") + b"\x00\x02\x00\x00\xff" + _chunk(0, b"Y") + _chunk(1, b"")
    assert nienet100.read_chunks_until_end(_reader(blob)) == b"XY"


def test_read_chunks_until_end_rejects_unknown_flag():
    with pytest.raises(nienet100.NIEnet100ProtocolError):
        nienet100.read_chunks_until_end(_reader(b"\x00\x99\x00\x00"))


def test_read_chunks_until_end_rejects_end_with_payload():
    with pytest.raises(nienet100.NIEnet100ProtocolError):
        nienet100.read_chunks_until_end(_reader(b"\x00\x01\x00\x05XXXXX"))


def test_read_one_data_chunk_returns_first_payload():
    blob = _chunk(0, b"\x42") + _chunk(1, b"")
    assert nienet100.read_one_data_chunk(_reader(blob)) == b"\x42"


def test_read_one_data_chunk_skips_signal_chunks():
    blob = b"\x00\x02\x00\x00\xff" + _chunk(0, b"OK")
    assert nienet100.read_one_data_chunk(_reader(blob)) == b"OK"


# --- conversion helpers -----------------------------------------------------


@pytest.mark.parametrize(
    "ip, want_hex",
    [
        ("0.0.0.0", 0x00000000),
        ("127.0.0.1", 0x7F000001),
        ("192.0.2.5", 0xC0000205),
        ("255.255.255.255", 0xFFFFFFFF),
    ],
)
def test_u32_from_ip(ip: str, want_hex: int):
    assert nienet100._u32_from_ip(ip) == want_hex


@pytest.mark.parametrize(
    "seconds, want",
    [
        (None, nienet100.TMO_NONE),
        (0, nienet100.TMO_NONE),
        (10e-6, nienet100.TMO_10us),
        (1e-3, nienet100.TMO_1ms),
        (1.0, nienet100.TMO_1s),
        (10.0, nienet100.TMO_10s),
        # 0.5 s rounds up to 1 s (next discrete value)
        (0.5, nienet100.TMO_1s),
        # 5 s rounds up to 10 s
        (5.0, nienet100.TMO_10s),
        # Clamp to the largest available code
        (5000.0, nienet100.TMO_1000s),
    ],
)
def test_seconds_to_tmo_code(seconds, want: int):
    assert nienet100.seconds_to_tmo_code(seconds) == want


# --- IO errors --------------------------------------------------------------


def test_iberr_exception_carries_fields():
    e = nienet100.NIEnet100IOError(
        nienet100.STA_ERR | nienet100.STA_CMPL,
        nienet100.ERR_ENOL,
        "ibwrt",
    )
    assert e.sta == nienet100.STA_ERR | nienet100.STA_CMPL
    assert e.err == nienet100.ERR_ENOL
    assert "ibwrt" in str(e)


# --- end-to-end against a scripted peer over socketpair --------------------
# Only runs on platforms with socket.socketpair (Unix; Windows 3.5+).


ScriptStep = Tuple[str, bytes]  # ("send", payload) or ("recv", payload)


def _run_scripted_peer(script: List[ScriptStep]):
    """Return (client_sock, thread). The thread plays ``script`` on the peer.

    On ``recv`` steps the thread asserts the exact bytes the client sent;
    on ``send`` steps it pushes bytes to the client.
    """
    a, b = socket.socketpair()

    def play():
        try:
            for direction, payload in script:
                if direction == "recv":
                    got = bytearray()
                    while len(got) < len(payload):
                        chunk = b.recv(len(payload) - len(got))
                        if not chunk:
                            raise AssertionError(
                                "peer disconnected after %d/%d bytes"
                                % (len(got), len(payload))
                            )
                        got.extend(chunk)
                    assert bytes(got) == payload, "client sent %r, expected %r" % (
                        bytes(got),
                        payload,
                    )
                elif direction == "send":
                    b.sendall(payload)
                else:
                    raise AssertionError("bad direction %r" % direction)
        finally:
            b.close()

    t = threading.Thread(target=play, daemon=True)
    t.start()
    return a, t


def _status_ok(cnt: int = 0) -> bytes:
    return struct.pack("!HH4xL", nienet100.STA_CMPL, 0, cnt)


def _make_bound_connection(client_sock: socket.socket) -> nienet100.EnetConnection:
    """Build an EnetConnection bound to ``client_sock`` as its main socket,
    skipping the normal connect/companion-hello path so individual verbs can
    be tested in isolation."""
    conn = nienet100.EnetConnection.__new__(nienet100.EnetConnection)
    conn.main = client_sock
    conn.companion = None
    conn.host = "test-peer"
    conn._open_timeout = 1.0
    conn._timeout = 1.0
    return conn


def test_ibwrt_sends_header_and_payload_combined():
    payload = b"HELLO"
    expected = (
        struct.pack("!BBHL4x", 0x62, 0x00, 0x0000, len(payload)) + payload + b"\x00"
    )
    script = [("recv", expected), ("send", _status_ok(cnt=5))]
    sock, t = _run_scripted_peer(script)
    conn = _make_bound_connection(sock)
    try:
        assert conn.ibwrt(payload) == 5
    finally:
        sock.close()
        t.join(timeout=2.0)


def test_ibrd_reads_until_end_marker_and_consumes_final_status():
    script = [
        ("recv", struct.pack("!BBHL4x", 0x16, 0x00, 0x0000, 0)),
        ("send", _status_ok()),  # preliminary status
        ("send", _chunk(0, b"WORLD\n")),
        ("send", _chunk(1, b"")),  # END
        (
            "send",
            struct.pack("!HH4xL", nienet100.STA_END | nienet100.STA_CMPL, 0xFFFF, 6),
        ),  # final status
    ]
    sock, t = _run_scripted_peer(script)
    conn = _make_bound_connection(sock)
    try:
        assert conn.ibrd() == b"WORLD\n"
    finally:
        sock.close()
        t.join(timeout=2.0)


def test_ibrsp_returns_first_data_byte():
    script = [
        ("recv", nienet100.pack_command(0x19)),
        ("send", _status_ok()),
        ("send", _chunk(0, b"\x42")),
    ]
    sock, t = _run_scripted_peer(script)
    conn = _make_bound_connection(sock)
    try:
        assert conn.ibrsp() == 0x42
    finally:
        sock.close()
        t.join(timeout=2.0)


def test_ibclr_raises_iberr_on_error_status():
    script = [
        ("recv", nienet100.pack_command(0x04)),
        (
            "send",
            struct.pack(
                "!HH4xL",
                nienet100.STA_ERR | nienet100.STA_CMPL,
                nienet100.ERR_ENOL,
                0,
            ),
        ),
    ]
    sock, t = _run_scripted_peer(script)
    conn = _make_bound_connection(sock)
    try:
        with pytest.raises(nienet100.NIEnet100IOError) as excinfo:
            conn.ibclr()
        assert excinfo.value.err == nienet100.ERR_ENOL
    finally:
        sock.close()
        t.join(timeout=2.0)


@pytest.mark.parametrize(
    "opcode, method",
    [
        (0x04, "ibclr"),
        (0x20, "ibtrg"),
        (0x10, "ibloc"),
    ],
)
def test_simple_verbs_roundtrip(opcode, method):
    script = [("recv", nienet100.pack_command(opcode)), ("send", _status_ok())]
    sock, t = _run_scripted_peer(script)
    conn = _make_bound_connection(sock)
    try:
        getattr(conn, method)()
    finally:
        sock.close()
        t.join(timeout=2.0)


def test_set_io_timeout_sends_property_set_frame():
    expected = struct.pack("!BBB9x", 0x50, 0x03, nienet100.TMO_10s)
    script = [("recv", expected), ("send", _status_ok())]
    sock, t = _run_scripted_peer(script)
    conn = _make_bound_connection(sock)
    try:
        conn.set_io_timeout(nienet100.TMO_10s)
    finally:
        sock.close()
        t.join(timeout=2.0)
