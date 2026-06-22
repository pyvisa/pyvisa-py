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


def test_read_chunks_until_end_treats_unknown_zero_length_flag_as_terminator():
    # Real hardware emits flag 0x0004 (length=0) on timeouts. Treating it
    # as end-of-stream (rather than raising) lets the caller's subsequent
    # status-header read carry the real error code.
    blob = _chunk(0, b"PARTIAL") + b"\x00\x04\x00\x00"
    assert nienet100.read_chunks_until_end(_reader(blob)) == b"PARTIAL"


def test_read_chunks_until_end_rejects_unknown_flag_with_non_zero_length():
    # Non-zero length on an unknown flag would desync the byte stream
    # (we cannot tell how to consume the payload), so it still raises.
    with pytest.raises(nienet100.NIEnet100ProtocolError):
        nienet100.read_chunks_until_end(_reader(b"\x00\x99\x00\x05" + b"XXXXX"))


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


ScriptStep = tuple[str, bytes]  # ("send", payload) or ("recv", payload)


def _run_scripted_peer(script: list[ScriptStep]):
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


def _wrap_status(body: bytes) -> bytes:
    """Wrap a 12-byte status body in the on-wire chunk header.

    Every status header the bridge emits is delivered as a data chunk
    (flags=0, length=12); the scripted peer must therefore prepend the
    chunk header for the bridge driver to read aligned.

    """
    return struct.pack("!HH", 0, 12) + body


def _status_ok(cnt: int = 0) -> bytes:
    body = struct.pack("!HH4xL", nienet100.STA_CMPL, 0, cnt)
    return _wrap_status(body)


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


def _make_empty_connection() -> nienet100.EnetConnection:
    """Build an EnetConnection with no sockets bound, for tests that drive
    socket-lifecycle methods via a monkey-patched ``_connect``."""
    conn = nienet100.EnetConnection.__new__(nienet100.EnetConnection)
    conn.main = None
    conn.companion = None
    conn.host = "test-peer"
    conn._open_timeout = 1.0
    conn._timeout = 1.0
    conn._bracket_open = False
    return conn


def _bound_inet_socket() -> socket.socket:
    """Return an AF_INET socket bound to an ephemeral loopback port.

    The 'O'/'U' verbs embed the main socket's ``getsockname()`` address in
    the wire frame, so tests that exercise them need a main socket whose
    ``getsockname()`` returns a real ``(ip, port)`` tuple. ``socket.
    socketpair()`` yields AF_UNIX sockets on Unix whose ``getsockname()``
    is the empty string, so it cannot stand in for the main socket here.
    The socket is only queried for its address (never read/written), so a
    bind without connect is enough.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    return sock


def test_ibwrt_sends_header_and_payload_combined():
    # Odd-length payload must be sent UNPADDED (count=5, 5 payload bytes) —
    # padding makes the box reject the frame. Mirrors the NI capture.
    payload = b"HELLO"
    expected = struct.pack("!BBHL4x", 0x62, 0x00, 0x0000, len(payload)) + payload
    script = [("recv", expected), ("send", _status_ok(cnt=5))]
    sock, t = _run_scripted_peer(script)
    conn = _make_bound_connection(sock)
    try:
        assert conn.ibwrt(payload) == 5
    finally:
        sock.close()
        t.join(timeout=2.0)


def test_ibrd_with_data_consumes_prelim_data_end_and_final():
    """Spec path: preliminary status, data chunks, END marker, final status."""
    expected_frame = struct.pack(
        "!BBHL4x", 0x16, 0x00, 0x0000, nienet100.DEFAULT_IBRD_TMO_MS
    )
    script = [
        ("recv", expected_frame),
        ("send", _status_ok()),  # preliminary status
        ("send", _chunk(0, b"WORLD\n")),
        ("send", _chunk(1, b"")),  # END
        (
            "send",
            _wrap_status(
                struct.pack("!HH4xL", nienet100.STA_END | nienet100.STA_CMPL, 0xFFFF, 6)
            ),
        ),  # final status
    ]
    sock, t = _run_scripted_peer(script)
    conn = _make_bound_connection(sock)
    try:
        assert conn.ibrd() == b"WORLD\n"
    finally:
        sock.close()
        t.join(timeout=2.0)


def test_ibrd_no_data_path_accepts_final_status_without_end_marker():
    """No-data path: the bridge sends preliminary + final without an
    intervening END marker. The parser must recognize the second
    length-12 chunk as the final status by inspecting its body."""
    expected_frame = struct.pack("!BBHL4x", 0x16, 0x00, 0x0000, 100)
    script = [
        ("recv", expected_frame),
        ("send", _status_ok()),  # preliminary
        ("send", _status_ok()),  # final directly, no END between
    ]
    sock, t = _run_scripted_peer(script)
    conn = _make_bound_connection(sock)
    try:
        assert conn.ibrd(tmo_ms=100) == b""
    finally:
        sock.close()
        t.join(timeout=2.0)


def test_ibrd_no_data_path_propagates_error_status():
    """No-data path with an error final status — STA_ERR must raise."""
    expected_frame = struct.pack("!BBHL4x", 0x16, 0x00, 0x0000, 100)
    error_status = _wrap_status(
        struct.pack(
            "!HH4xL",
            nienet100.STA_ERR | nienet100.STA_CMPL,
            nienet100.ERR_EABO,
            0,
        )
    )
    script = [
        ("recv", expected_frame),
        ("send", _status_ok()),  # preliminary
        ("send", error_status),  # final with timeout
    ]
    sock, t = _run_scripted_peer(script)
    conn = _make_bound_connection(sock)
    try:
        with pytest.raises(nienet100.NIEnet100IOError) as excinfo:
            conn.ibrd(tmo_ms=100)
        assert excinfo.value.err == nienet100.ERR_EABO
    finally:
        sock.close()
        t.join(timeout=2.0)


@pytest.mark.parametrize("cnt", [1, 0])
def test_ibrsp_returns_stb_from_combined_chunk(cnt: int):
    # The bridge packs the 12-byte status header and the 1-byte STB into one
    # chunk with length=13; the STB is always the trailing byte. cnt is not
    # reliably 1 (a serial poll right after an SRQ wait reports cnt=0 with a
    # valid STB), so the STB must be read by position regardless of cnt.
    status_body = struct.pack("!HH4xL", nienet100.STA_CMPL, 0, cnt)
    response = _chunk(0, status_body + b"\x42")
    script = [
        ("recv", nienet100.pack_command(0x19)),
        ("send", response),
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
            _wrap_status(
                struct.pack(
                    "!HH4xL",
                    nienet100.STA_ERR | nienet100.STA_CMPL,
                    nienet100.ERR_ENOL,
                    0,
                )
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


# --- ibwait (B2) -----------------------------------------------------------


def test_ibwait_sends_mask_and_returns_sta():
    # ibwait polls with 0x22 on the companion socket (the event channel).
    mask = nienet100.STA_RQS | nienet100.STA_TIMO
    expected_frame = nienet100.pack_command(cmd_id=0x22, b1=0x00, w1=mask)
    response_sta = nienet100.STA_RQS | nienet100.STA_CMPL
    script = [
        ("recv", expected_frame),
        ("send", _wrap_status(struct.pack("!HH4xL", response_sta, 0xFFFF, 0))),
    ]
    companion_sock, t = _run_scripted_peer(script)
    try:
        conn = _make_bound_connection(socket.socket())  # main present, unused here
        conn.companion = companion_sock
        sta = conn.ibwait(mask)
        assert sta == response_sta
    finally:
        companion_sock.close()
        t.join(timeout=2.0)


def test_ibwait_raises_on_error_status():
    script = [
        ("recv", nienet100.pack_command(cmd_id=0x22, b1=0x00, w1=nienet100.STA_RQS)),
        (
            "send",
            _wrap_status(
                struct.pack(
                    "!HH4xL",
                    nienet100.STA_ERR | nienet100.STA_CMPL,
                    nienet100.ERR_EARG,
                    0,
                )
            ),
        ),
    ]
    companion_sock, t = _run_scripted_peer(script)
    try:
        conn = _make_bound_connection(socket.socket())
        conn.companion = companion_sock
        with pytest.raises(nienet100.NIEnet100IOError) as excinfo:
            conn.ibwait(nienet100.STA_RQS)
        assert excinfo.value.err == nienet100.ERR_EARG
    finally:
        companion_sock.close()
        t.join(timeout=2.0)


# --- ibsic (B3) -----------------------------------------------------------


def test_ibsic_sends_1c_on_main():
    # IFC is a bare 0x1c command on the main socket (verified against the
    # genuine NI software); the box replies CMPL|CIC|ATN.
    expected = nienet100.pack_command(0x1C)
    reply_sta = nienet100.STA_CMPL | nienet100.STA_CIC | nienet100.STA_ATN
    main_sock, t = _run_scripted_peer(
        [
            ("recv", expected),
            ("send", _wrap_status(struct.pack("!HH4xL", reply_sta, 0, 0))),
        ]
    )
    try:
        conn = _make_bound_connection(main_sock)
        conn.ibsic()
    finally:
        main_sock.close()
        t.join(timeout=2.0)


def test_close_drops_all_sockets_without_extra_frames():
    # close() must not emit anything on the companion socket; it just tears
    # the sockets down. A companion peer read therefore sees only EOF.
    main_a = _bound_inet_socket()
    companion_a, companion_b = socket.socketpair()
    try:
        conn = _make_empty_connection()
        conn.main = main_a
        conn.companion = companion_a
        conn.close()
        assert conn.main is None
        assert conn.companion is None
        companion_b.settimeout(2.0)
        assert companion_b.recv(64) == b"", "close() unexpectedly sent a frame"
    finally:
        main_a.close()
        companion_b.close()


def test_close_swallows_socket_errors():
    # Sockets already closed before teardown: close() logs and proceeds
    # without raising.
    main_a = _bound_inet_socket()
    try:
        fake_companion = socket.socket()
        fake_companion.close()  # already closed
        conn = _make_empty_connection()
        conn.main = main_a
        conn.companion = fake_companion
        conn.close()  # must not raise
        assert conn.companion is None
    finally:
        main_a.close()
