# -*- coding: utf-8 -*-
"""Python implementation of the NI GPIB-ENET/100 wire protocol.

This module talks the proprietary TCP protocol of the National Instruments
GPIB-ENET/100 Ethernet-to-GPIB bridge. It is **not** compatible with the
older GPIB-ENET (10 MBit/s, libnienet target), which uses a similar frame
layout but different verb opcodes and a single-step open.

The wire format was reverse-engineered; the frame layout, verb opcodes and
status fields are documented inline alongside the constants and packers in
this module.

All multi-byte fields are big-endian (network byte order).

:copyright: 2026 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

import logging
import socket
import struct
from collections.abc import Callable
from typing import TYPE_CHECKING

LOGGER = logging.getLogger("pyvisa_py.protocols.nienet100")

#: Main TCP port (synchronous request/response).
PORT_MAIN = 5000

#: Wait socket TCP port (synchronous ibwait polling and async register).
PORT_WAIT = 5003

#: Control socket TCP port (notify-off async, ibsic, ibwait re-arm).
PORT_CONTROL = 5005

#: Companion socket TCP port (hello-only, mandatory for FW >= A8).
PORT_COMPANION = 5015

#: Fixed length of every command frame sent to the box.
COMMAND_FRAME_SIZE = 12

#: Fixed length of every status header received from the box.
STATUS_HEADER_SIZE = 12

#: Fixed length of every payload chunk header in a read stream.
CHUNK_HEADER_SIZE = 4


# --- NI-488.2 ibsta bits (subset relevant to this protocol) -----------------

STA_ERR = 0x8000  # operation error, ``err`` field carries the code
STA_TIMO = 0x4000  # timeout during operation
STA_END = 0x2000  # EOI or EOS match (talker signaled end-of-message)
STA_SRQI = 0x1000  # SRQ detected while controller-in-charge
STA_RQS = 0x0800  # device RQS asserted (set in ibrsp/ibwait responses)
STA_CMPL = 0x0100  # operation complete
STA_LOK = 0x0080  # lockout state
STA_REM = 0x0040  # remote state
STA_CIC = 0x0020  # controller-in-charge
STA_ATN = 0x0010  # ATN line asserted
STA_TACS = 0x0008  # talker active
STA_LACS = 0x0004  # listener active
STA_DTAS = 0x0002  # device trigger state
STA_DCAS = 0x0001  # device clear state


# --- NI-488.2 iberr codes (subset relevant to this protocol) ----------------

ERR_EDVR = 0  # OS error (rare)
ERR_ECIC = 1  # function requires controller-in-charge
ERR_ENOL = 2  # no listener on the bus
ERR_EADR = 3  # address error
ERR_EARG = 4  # invalid argument to API
ERR_ESAC = 5  # function requires system controller
ERR_EABO = 6  # I/O aborted / timeout
ERR_ENEB = 7  # non-existent board
ERR_EBUS = 0xA  # bus error
ERR_ECAP = 0xB  # capability disabled
ERR_EFSO = 0xC  # file-system error
ERR_EBNP = 0xD  # board not present
ERR_ESTB = 0xE  # serial-poll status byte lost
ERR_ESRQ = 0xF  # SRQ stuck on


# --- NI-488.2 timeout codes (TMO index, not milliseconds) -------------------
# Used in SetConfig Frame A byte[8] and in the ``'P 03'`` property setter.

TMO_NONE = 0
TMO_10us = 1
TMO_30us = 2
TMO_100us = 3
TMO_300us = 4
TMO_1ms = 5
TMO_3ms = 6
TMO_10ms = 7
TMO_30ms = 8
TMO_100ms = 9
TMO_300ms = 10
TMO_1s = 11
TMO_3s = 12
TMO_10s = 13
TMO_30s = 14
TMO_100s = 15
TMO_300s = 16
TMO_1000s = 17

#: Discrete timeout values in seconds, indexed by TMO code. ``None`` = disabled.
TIMETABLE: tuple = (
    None,  # TMO_NONE
    10e-6,
    30e-6,
    100e-6,
    300e-6,
    1e-3,
    3e-3,
    10e-3,
    30e-3,
    100e-3,
    300e-3,
    1.0,
    3.0,
    10.0,
    30.0,
    100.0,
    300.0,
    1000.0,
)


def seconds_to_tmo_code(timeout: float) -> int:
    """Round a timeout (in seconds) up to the closest discrete TMO code.

    Values larger than ``TIMETABLE[-1]`` are clamped to ``TMO_1000s``.
    ``None`` or ``0`` map to ``TMO_NONE``.

    """
    if not timeout:
        return TMO_NONE
    for code in range(1, len(TIMETABLE)):
        if TIMETABLE[code] >= timeout * 0.999:
            return code
    return TMO_1000s


# --- Chunk header flags (read stream after a status header) -----------------

CHUNK_FLAG_DATA = 0  # data chunk; ``length`` bytes of payload follow
CHUNK_FLAG_END = 1  # END marker; ``length`` must be 0, read complete
CHUNK_FLAG_SIGNAL = 2  # out-of-band signal byte (1 byte follows), defensively skip


# --- 12-byte command-frame layout -------------------------------------------
# Byte:  0     1     2-3      4-5       6-7      8-11
#        +-----+-----+--------+---------+--------+---------+
#        | id  |  b1 | ushort | ushort  | ushort |  ulong  |   12 B, big-endian
#        +-----+-----+--------+---------+--------+---------+

_COMMAND_FRAME_FMT = "!BBHHHL"


def pack_command(
    cmd_id: int,
    b1: int = 0,
    w1: int = 0,
    w2: int = 0,
    w3: int = 0,
    dw: int = 0,
) -> bytes:
    """Build a 12-byte command frame.

    All fields default to 0. Unused fields **must** stay zero — the box
    accepts non-zeroed buffers only inconsistently.

    """
    return struct.pack(_COMMAND_FRAME_FMT, cmd_id, b1, w1, w2, w3, dw)


# --- 12-byte status-header layout -------------------------------------------
# Byte:  0-1   2-3   4-7         8-11
#        +-----+-----+-----------+---------+
#        | sta | err | 4 padding |  count  |   12 B, big-endian
#        +-----+-----+-----------+---------+

_STATUS_HEADER_FMT = "!HH4xL"


def parse_status_header(buf: bytes) -> tuple[int, int, int]:
    """Decode a 12-byte status header into ``(sta, err, cnt)``.

    ``err`` is only meaningful when ``sta & STA_ERR`` is set; otherwise it
    may carry sentinel values such as 0xFFFF that the caller must ignore.

    """
    if len(buf) != STATUS_HEADER_SIZE:
        raise ValueError(
            "status header must be exactly %d bytes, got %d"
            % (STATUS_HEADER_SIZE, len(buf))
        )
    return struct.unpack(_STATUS_HEADER_FMT, buf)


def parse_chunk_header(buf: bytes) -> tuple[int, int]:
    """Decode a 4-byte chunk header into ``(flags, length)``."""
    if len(buf) != CHUNK_HEADER_SIZE:
        raise ValueError(
            "chunk header must be exactly %d bytes, got %d"
            % (CHUNK_HEADER_SIZE, len(buf))
        )
    return struct.unpack("!HH", buf)


# --- Chunk reader -----------------------------------------------------------
# Every response from the bridge is framed as a sequence of chunks: each chunk
# is a 4-byte header (flags, length) followed by ``length`` bytes of data.
# Status headers are themselves data chunks with length=12; payload bytes for
# ibrd / ibrsp are additional data chunks; the end-of-stream marker is its
# own chunk (flags=1, length=0). The chunk stream is stateful: chunks may be
# split across TCP segments and several chunks may arrive in one segment.
# Callers drive recv via the ``read_exactly`` callable so this layer is
# socket-agnostic and testable.


def read_chunks_until_end(read_exactly: Callable[[int], bytes]) -> bytes:
    """Consume a chunk stream until the END marker (flags=1).

    Tolerates out-of-band signal chunks (flags=2) by reading and discarding
    their single payload byte. Unknown flag values with ``length==0`` are
    treated as end-of-stream terminators with a warning — hardware has
    been observed to use flag 0x0004 on timeouts (and other terminal
    conditions the wire spec does not enumerate). The caller's
    subsequent status-header read carries the real outcome (e.g. STA_ERR
    + iberr=EABO for a timeout). Unknown flags carrying a non-zero
    length still raise :class:`NIEnet100ProtocolError` because we
    cannot stay frame-aligned without knowing how to consume the data.

    Parameters
    ----------
    read_exactly : Callable[[int], bytes]
        Reader returning exactly ``n`` bytes or raising on short read /
        timeout. Pass a bound socket helper here.

    Returns
    -------
    bytes
        Concatenated payload of all data chunks (END chunk excluded).

    """
    payload = bytearray()
    while True:
        flags, length = parse_chunk_header(read_exactly(CHUNK_HEADER_SIZE))
        if flags == CHUNK_FLAG_DATA:
            if length:
                payload.extend(read_exactly(length))
        elif flags == CHUNK_FLAG_END:
            if length != 0:
                raise NIEnet100ProtocolError(
                    "END chunk has non-zero length %d" % length
                )
            return bytes(payload)
        elif flags == CHUNK_FLAG_SIGNAL:
            # Defensive: per spec, a signal chunk carries exactly 1 OOB byte
            # which we log and skip. Never observed in practice.
            signal_byte = read_exactly(1)
            LOGGER.debug("NI-ENET/100 signal byte received: 0x%02x", signal_byte[0])
        elif length == 0:
            # Undocumented zero-length flag — treat as a terminal marker so
            # the caller's status-header read can report the real outcome
            # instead of us crashing on a flag we have not characterized.
            LOGGER.warning(
                "treating unknown chunk flag 0x%04x (length=0) as end-of-stream",
                flags,
            )
            return bytes(payload)
        else:
            raise NIEnet100ProtocolError(
                "unknown chunk flag 0x%04x with non-zero length %d "
                "(cannot stay aligned, aborting)" % (flags, length)
            )


def read_one_data_chunk(read_exactly: Callable[[int], bytes]) -> bytes:
    """Read exactly one data chunk and return its payload.

    Use this for verbs whose response carries a single fixed-size data chunk
    and may omit the END marker (e.g. ``ibrsp`` returns a single STB byte).
    Signal chunks (flags=2) are still tolerated.

    """
    while True:
        flags, length = parse_chunk_header(read_exactly(CHUNK_HEADER_SIZE))
        if flags == CHUNK_FLAG_DATA:
            return read_exactly(length) if length else b""
        elif flags == CHUNK_FLAG_SIGNAL:
            signal_byte = read_exactly(1)
            LOGGER.debug("NI-ENET/100 signal byte received: 0x%02x", signal_byte[0])
        else:
            raise NIEnet100ProtocolError(
                "expected data chunk, got flags=0x%04x length=%d" % (flags, length)
            )


def read_status_chunk(read_exactly: Callable[[int], bytes]) -> tuple[int, int, int]:
    """Read a chunk-wrapped 12-byte status header and parse it.

    The bridge wraps every status header in the standard chunk framing
    (``[flags=0, length=12][12B body]``) — there is no such thing as a
    "raw" 12-byte status read on the wire. Callers must use this helper
    rather than reading 12 bytes directly, or successive operations will
    accumulate a 4-byte misalignment per status read.

    """
    body = read_one_data_chunk(read_exactly)
    if len(body) != STATUS_HEADER_SIZE:
        raise NIEnet100ProtocolError(
            "expected %d-byte status chunk body, got %d bytes"
            % (STATUS_HEADER_SIZE, len(body))
        )
    return parse_status_header(body)


class NIEnet100Error(Exception):
    """Base exception for NI GPIB-ENET/100 protocol errors."""


class NIEnet100ProtocolError(NIEnet100Error):
    """The peer sent something we cannot parse (bad magic, bad chunk flag)."""


class NIEnet100IOError(NIEnet100Error):
    """The box returned a status header with ``STA_ERR`` set.

    Attributes
    ----------
    sta : int
        Raw ``ibsta`` bitmask from the status header.
    err : int
        Raw ``iberr`` code from the status header.

    """

    def __init__(self, sta: int, err: int, operation: str = ""):
        self.sta = sta
        self.err = err
        msg = "NI-ENET/100 operation %r failed: sta=0x%04x err=%d" % (
            operation,
            sta,
            err,
        )
        super().__init__(msg)


# --- Connection -------------------------------------------------------------
# The box uses up to four parallel TCP sockets per session. The main socket
# (5000) carries all synchronous Device-I/O. The companion socket (5015) is
# mandatory on every firmware shipped in the last ~20 years and only carries
# a single hello frame; it must stay open for the session lifetime. Wait
# (5003) and control (5005) are lazy and only needed for ibwait / async
# notify-off; they are not opened by this base class.


def _u32_from_ip(ip: str) -> int:
    """Convert dotted-quad IP to a 32-bit integer in host order.

    The result is meant to be re-emitted via ``struct.pack('!L', ...)``,
    which puts the high byte first on the wire — matching the box's
    convention (e.g. 192.0.2.5 -> ``c0 00 02 05``).

    """
    return int.from_bytes(socket.inet_aton(ip), "big")


class EnetConnection:
    """Synchronous TCP transport to a single GPIB-ENET/100 box.

    Opens the main socket (port 5000) and the companion socket (port 5015)
    on instantiation and sends the mandatory companion hello frame. Wait
    (port 5003) and control (port 5005) sockets are opened lazily by
    :meth:`ensure_wait_socket` / :meth:`ensure_control_socket` — they are
    only needed for ibwait-based SRQ polling and the few 'O' verbs
    (notify-off async, ibsic, ibwait re-arm).

    The class is **not** thread-safe. Concurrent calls into a single
    instance (e.g. one thread issuing ibwrt while another polls ibwait)
    will interleave bytes on the sockets and corrupt the protocol state.

    Parameters
    ----------
    host : str
        Box IP or hostname.
    open_timeout : float
        Per-socket connect timeout in seconds.
    timeout : float | None
        Per-operation socket timeout in seconds applied after connect.
        ``None`` means blocking without timeout.

    Attributes
    ----------
    host : str
        The host string passed at construction time.
    main : socket.socket
        The synchronous main socket.
    companion : socket.socket
        The hello-only companion socket; kept open for the session lifetime.
    wait : socket.socket | None
        The ibwait polling socket; ``None`` until :meth:`ensure_wait_socket`.
    control : socket.socket | None
        The control socket for 'O' verbs; ``None`` until
        :meth:`ensure_control_socket`.

    """

    if TYPE_CHECKING:
        # GPIB verbs are implemented as module-level functions and bound onto
        # the class at the end of this file. Declare their signatures here so
        # type checkers see the public ``conn.ibXXX`` API; the runtime
        # implementations come from the assignments below.
        def ibwrt(self, data: bytes) -> int: ...

        def ibrd(self, tmo_ms: int = ...) -> bytes: ...

        def ibclr(self) -> None: ...

        def ibtrg(self) -> None: ...

        def ibloc(self) -> None: ...

        def ibrsp(self) -> int: ...

        def ibwait(self, mask: int) -> int: ...

        def ibsic(self) -> None: ...

        def notify_off_async_device(self) -> None: ...

        def set_io_timeout(self, tmo_code: int) -> None: ...

        def transact_main_status(
            self, operation: str = ...
        ) -> tuple[int, int, int]: ...

    #: Companion-hello flag word for device-mode sessions (single resource).
    COMPANION_FLAGS_DEVICE = 2

    #: Async-register flag word for device-mode SRQ routing.
    ASYNC_REGISTER_FLAGS_DEVICE = 2

    def __init__(
        self,
        host: str,
        open_timeout: float = 10.0,
        timeout: float | None = 10.0,
    ) -> None:
        self.host = host
        self._open_timeout = open_timeout
        self._timeout = timeout
        self.main: socket.socket | None = None
        self.companion: socket.socket | None = None
        self.wait: socket.socket | None = None
        self.control: socket.socket | None = None
        # Tracks whether a Frame F bracket-open has been acked by the box
        # without a matching Frame X close yet. Owned by _transact_bracket
        # so failures between bracket-open and the session-layer marker
        # (e.g., Frame G of open_gpib_session) still trigger a bracket
        # close on the way out — otherwise the bridge leaks a session
        # slot per such failure.
        self._bracket_open: bool = False

    # --- lifecycle ------------------------------------------------------

    def open(self) -> None:
        """Open main and companion sockets and send the companion hello."""
        self.main = self._connect(PORT_MAIN)
        try:
            self.companion = self._connect(PORT_COMPANION)
        except Exception:
            self.main.close()
            self.main = None
            raise
        try:
            self._send_companion_hello()
        except Exception:
            self.close()
            raise

    def ensure_wait_socket(self) -> None:
        """Open port 5003 and register the main session for async events.

        Sends the ``'U 01'`` device-mode async-register frame (which tells
        the box that SRQ events for the session identified by the main
        socket's address should surface via ibwait on this socket) and the
        ``'P 10 01'`` online re-confirm. Idempotent: a no-op if the wait
        socket is already open.

        Requires the main socket to be open (the async-register frame
        carries the main socket's ``getsockname()`` so the box can match
        SRQs back to the session).

        """
        if self.wait is not None:
            return
        if self.main is None:
            raise NIEnet100Error("cannot open wait socket: main socket is not open")

        sock = self._connect(PORT_WAIT)
        try:
            self._send_async_register_device(sock)
            self._send_online_reconfirm(sock)
        except Exception:
            sock.close()
            raise
        self.wait = sock

    def ensure_control_socket(self) -> None:
        """Open port 5005. No setup frames — first 'O' verb carries its own.

        Idempotent.

        """
        if self.control is not None:
            return
        self.control = self._connect(PORT_CONTROL)

    def close(self) -> None:
        """Close every open socket. Idempotent.

        If a Frame F bracket is currently open on the box, best-effort
        sends the matching ``X 00 01`` bracket-close before tearing the
        sockets down — otherwise the bridge leaks the session slot. This
        runs unconditionally so error paths in higher layers cannot skip
        it. If the wait socket was opened (and the async-register frame
        was therefore sent), best-effort sends the 'O 4e' notify-off
        frame on the control socket too. Errors during cleanup are
        logged and swallowed so socket teardown always runs.

        """
        if self._bracket_open and self.main is not None:
            try:
                self.close_gpib_session()
            except Exception as e:
                LOGGER.debug("bracket close during teardown failed: %s", e)

        if self.wait is not None and self.main is not None:
            try:
                self.notify_off_async_device()
            except (NIEnet100Error, OSError) as e:
                LOGGER.debug("notify-off cleanup failed: %s", e)

        # Close in reverse open-order so the box sees the auxiliary sockets
        # disappear before main. The box does not require a goodbye frame.
        for attr in ("control", "wait", "companion", "main"):
            sock = getattr(self, attr, None)
            if sock is not None:
                try:
                    sock.close()
                except OSError as e:
                    LOGGER.debug("error closing %s socket: %s", attr, e)
                setattr(self, attr, None)

    def set_socket_timeout(self, timeout: float | None) -> None:
        """Apply ``timeout`` (in seconds) to all currently open sockets.

        Use ``None`` for blocking without timeout. The value is cached so
        sockets opened later (wait/control) pick up the same setting.

        """
        self._timeout = timeout
        for sock in (self.main, self.companion, self.wait, self.control):
            if sock is not None:
                sock.settimeout(timeout)

    # --- low-level helpers ---------------------------------------------

    def _connect(self, port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self._open_timeout)
        try:
            sock.connect((self.host, port))
        except Exception:
            sock.close()
            raise
        sock.settimeout(self._timeout)
        return sock

    @staticmethod
    def _recv_exactly(sock: socket.socket, n: int) -> bytes:
        """Read exactly ``n`` bytes from ``sock`` or raise."""
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise NIEnet100Error(
                    "connection closed by peer after %d/%d bytes" % (len(buf), n)
                )
            buf.extend(chunk)
        return bytes(buf)

    def recv_main_exactly(self, n: int) -> bytes:
        """Read exactly ``n`` bytes from the main socket or raise.

        At DEBUG log level the bytes received are hex-dumped — invaluable
        for diagnosing wire-protocol surprises against real hardware. Set
        ``--log-cli-level=DEBUG`` on pytest to see the dumps, or attach a
        handler to ``pyvisa_py.protocols.nienet100`` in your own code.

        """
        if self.main is None:
            raise NIEnet100Error("main socket is not open")
        data = self._recv_exactly(self.main, n)
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug("← main: %s", data.hex())
        return data

    def send_main(self, data: bytes) -> None:
        """Send ``data`` on the main socket in a single ``sendall``.

        At DEBUG log level the bytes sent are hex-dumped — see
        :meth:`recv_main_exactly` for details.

        """
        if self.main is None:
            raise NIEnet100Error("main socket is not open")
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug("→ main: %s", data.hex())
        self.main.sendall(data)

    def read_status_main(self) -> tuple[int, int, int]:
        """Read a chunk-wrapped status header from the main socket and parse it."""
        return read_status_chunk(self.recv_main_exactly)

    def transact_main(self, frame: bytes, operation: str = "") -> tuple[int, int, int]:
        """Send a command frame and read the status header on the main socket.

        Raises :class:`NIEnet100IOError` if the status header has ``STA_ERR``
        set. Returns ``(sta, err, cnt)`` on success.

        """
        self.send_main(frame)
        sta, err, cnt = self.read_status_main()
        if sta & STA_ERR:
            raise NIEnet100IOError(sta, err, operation)
        return sta, err, cnt

    # --- companion socket ----------------------------------------------

    def _send_companion_hello(self) -> None:
        """Send the 'U 02' hello on the companion socket and read the status.

        Sub-op layout: ``55 02 [htons(flags)] 00 00 [htons(port)] [ip:4]``.
        ``port``/``ip`` are ``getsockname()`` of the companion socket — the
        box does not validate the values, so NAT'd addresses are fine.

        """
        if self.companion is None:
            raise NIEnet100Error("companion socket is not open")
        local_ip, local_port = self.companion.getsockname()
        frame = pack_command(
            cmd_id=0x55,  # 'U'
            b1=0x02,
            w1=self.COMPANION_FLAGS_DEVICE,
            w2=0,
            w3=local_port,
            dw=_u32_from_ip(local_ip),
        )
        self.companion.sendall(frame)
        companion = self.companion
        sta, err, _cnt = read_status_chunk(lambda n: self._recv_exactly(companion, n))
        if sta & STA_ERR:
            raise NIEnet100IOError(sta, err, "companion hello")

    def _send_async_register_device(self, wait_sock: socket.socket) -> None:
        """Send the 'U 01' device-mode async-register on a wait socket.

        Sub-op layout: ``55 01 [htons(flags)] 00 00 [htons(port)] [ip:4]``.
        ``port``/``ip`` come from the **main** socket's ``getsockname()``
        — the box uses that address to identify the session whose async
        events should surface on ``wait_sock``.

        """
        assert self.main is not None  # caller guarantees this
        main_ip, main_port = self.main.getsockname()
        frame = pack_command(
            cmd_id=0x55,  # 'U'
            b1=0x01,
            w1=self.ASYNC_REGISTER_FLAGS_DEVICE,
            w2=0,
            w3=main_port,
            dw=_u32_from_ip(main_ip),
        )
        wait_sock.sendall(frame)
        sta, err, _cnt = read_status_chunk(lambda n: self._recv_exactly(wait_sock, n))
        if sta & STA_ERR:
            raise NIEnet100IOError(sta, err, "async register device")

    def _send_online_reconfirm(self, wait_sock: socket.socket) -> None:
        """Send the 'P 10 01' online re-confirm on a wait socket.

        Same property frame as Frame D of the open sequence; the wait
        socket needs its own confirmation that the bracket is online
        before the box will accept ibwait polls.

        """
        wait_sock.sendall(_pack_property_set(0x10, 0x01))
        sta, err, _cnt = read_status_chunk(lambda n: self._recv_exactly(wait_sock, n))
        if sta & STA_ERR:
            raise NIEnet100IOError(sta, err, "wait online re-confirm")

    # --- GPIB-session open / close (Frames A-G of the spec) -----------

    #: Default Frame-C board flags. Sets HS488 marker + an EOI/EOS bit and
    #: leaves everything else off — the conservative baseline for a generic
    #: instrument session.
    DEFAULT_BOARD_FLAGS = 0x1801

    #: Default Frame-E event-queue depth.
    DEFAULT_EVENT_QUEUE_DEPTH = 0x0B

    def open_gpib_session(
        self,
        primary_address: int,
        secondary_address: int = 0,
        tmo_code: int = TMO_10s,
        board_flags: int = DEFAULT_BOARD_FLAGS,
        event_queue_depth: int = DEFAULT_EVENT_QUEUE_DEPTH,
        mode_byte: int = 0,
    ) -> None:
        """Run the seven-frame open sequence on the main socket.

        After ``open()`` (which establishes main + companion sockets and
        sends the companion hello), this method makes the bus ready for
        Device-I/O against the given primary/secondary address. The bracket
        opened by Frame F stays open until :meth:`close_gpib_session`.

        Parameters
        ----------
        primary_address : int
            Target GPIB primary address (0-30).
        secondary_address : int
            Target GPIB secondary address (0 means none).
        tmo_code : int
            NI-488.2 timeout code (see ``TIMETABLE``). Default ``TMO_10s``
            matches NI's measurement-equipment default.
        board_flags : int
            Frame-C bitmask. Default ``0x1801`` is the standard
            single-instrument baseline.
        event_queue_depth : int
            Frame-E event-queue depth. Default ``0x0b`` (= 11).
        mode_byte : int
            Frame-B mode byte. ``0`` is standard.

        """
        # Frame A: SetConfig with SC bit and target address.
        # Wire bytes: 07 02 00 01 [PAD] [SAD] 00 00 [tmo] 00 04 00
        frame_a = pack_command(
            cmd_id=0x07,
            b1=0x02,
            w1=0x0001,
            w2=(primary_address << 8) | (secondary_address & 0xFF),
            w3=0,
            dw=(tmo_code << 24) | 0x0400,
        )
        self.transact_main(frame_a, "open Frame A SetConfig SC")

        # Frame B: Property 'Mode' (PPC, idx 0x05).
        # Wire bytes: 50 05 [mode_byte] 00*9
        self.transact_main(_pack_property_set(0x05, mode_byte), "open Frame B PPC")

        # Frame C: SetConfig (non-SC variant) with board flags.
        # Wire bytes: 07 00 [htons(flags)] 00 00 00 00 [tmo] 00 00 00
        frame_c = pack_command(
            cmd_id=0x07,
            b1=0x00,
            w1=board_flags,
            w2=0,
            w3=0,
            dw=tmo_code << 24,
        )
        self.transact_main(frame_c, "open Frame C SetConfig non-SC")

        # Frame D: Property 'Online' (PP2, idx 0x10) with value 1.
        # Wire bytes: 50 10 01 00*9
        self.transact_main(_pack_property_set(0x10, 0x01), "open Frame D online")

        # Frame E: Property 'Event-Queue depth' (idx 0x15).
        # Wire bytes: 50 15 [depth] 00*9
        self.transact_main(
            _pack_property_set(0x15, event_queue_depth),
            "open Frame E event-queue depth",
        )

        # Frame F: Operation-bracket open ('X', idx 0x58).
        # Wire bytes: 58 01 01 00*9
        self._transact_bracket(enter=True)

        # Frame G: Notify-Off sync ('N', idx 0x4e). Defensive reset against
        # any pending async notifies the box may have queued.
        # Wire bytes: 4e 01 00*10
        self.transact_main(pack_command(0x4E, 0x01), "open Frame G notify-off sync")

    def close_gpib_session(self) -> None:
        """Close the operation bracket opened by :meth:`open_gpib_session`.

        Sockets are not closed here — call :meth:`close` for that. No-op
        when no bracket is currently open, so callers can invoke this on
        any cleanup path without first probing state.

        """
        if not self._bracket_open or self.main is None:
            return
        try:
            self._transact_bracket(enter=False)
        except (NIEnet100Error, OSError) as e:
            LOGGER.debug("error closing GPIB bracket: %s", e)
            # Clear the flag even when the wire transact failed, so the
            # subsequent close() does not re-attempt on a wedged socket.
            self._bracket_open = False

    def _transact_bracket(self, enter: bool) -> None:
        # Wire bytes: 58 [01|00] 01 00*9
        frame = struct.pack("!BBB9x", 0x58, 0x01 if enter else 0x00, 0x01)
        self.transact_main(frame, "bracket %s" % ("open" if enter else "close"))
        # Flip the flag only after the box acked the frame, so a failing
        # open does not leave us thinking we owe a close, and vice-versa.
        self._bracket_open = enter


def _pack_property_set(prop_idx: int, value_byte: int) -> bytes:
    """Build a 'P' property-set frame (0x50).

    Wire layout: ``50 [prop_idx] [value_byte] 00*9``.

    """
    return struct.pack("!BBB9x", 0x50, prop_idx, value_byte)


def _pack_o_verb(sub_op: int, leading_u16: int, ip_u32: int, port: int) -> bytes:
    """Build an 'O' control-socket verb with the IP-before-port layout.

    Wire layout: ``4f [sub_op] [htons(leading_u16):2] [ip:4] [htons(port):2] 00 00``.

    Used by ibsic, notify-off-async-board, notify-off-async-device, and
    ibwait re-arm. Note that the layout differs from 'U' verbs (which put
    port before ip); the inconsistency is part of the wire protocol.

    """
    return struct.pack("!BBHLH2x", 0x4F, sub_op, leading_u16, ip_u32, port)


# --- Device-level verbs -----------------------------------------------------
# These methods are added to EnetConnection via assignment below. They cover
# the minimal pyvisa-Resource API surface: write, read, clear, trigger,
# serial poll, local-lockout release, and the I/O timeout setter. Async
# verbs (ibwait, ibnotify) and board-level verbs (ibsic, ibcmd) live in
# later commits since they require the wait/control sockets.


def _ibwrt(self: EnetConnection, data: bytes) -> int:
    """Write ``data`` to the addressed device.

    Wire layout: ``62 00 00 00 [htonl(byte_count):4] 00 00 00 00`` followed
    immediately by the raw payload in the same ``sendall``. The payload is
    sent unpadded, exactly ``byte_count`` bytes — the genuine NI software
    sends odd-length payloads with no padding (e.g. ``*SRE 16`` = 7 bytes,
    count=7), and padding an odd payload makes the box reject the frame
    (it replies with a malformed 22-byte chunk).

    Returns the number of bytes the box reports as transferred.

    """
    byte_count = len(data)
    # Frame: 62 00 00 00 [htonl(byte_count):4] 00 00 00 00
    header = struct.pack("!BBHL4x", 0x62, 0x00, 0x0000, byte_count)
    self.send_main(header + data)
    _sta, _err, cnt = self.transact_main_status("ibwrt")
    return cnt


#: Default per-call read timeout in milliseconds when the caller passes
#: nothing. ``tmo_ms=0`` on the wire is **not** "use the session default"
#: — the bridge interprets it as "do not wait" and returns immediately
#: with cnt=0 and no END marker. 10 s matches NI's default IbcTMO for
#: measurement equipment and is the conservative choice for callers that
#: do not know better.
DEFAULT_IBRD_TMO_MS = 10_000


def _ibrd(self: EnetConnection, tmo_ms: int = DEFAULT_IBRD_TMO_MS) -> bytes:
    """Read one message from the addressed device.

    The wire-level read pulls bytes until the device signals end-of-message
    (EOI/EOS) or the bridge's per-read timeout fires. The box does not
    take a maximum-byte argument — callers that want to truncate must do
    so after the fact.

    Wire layout: ``16 00 00 00 [htonl(tmo_ms):4] 00 00 00 00``. ``tmo_ms``
    is the bridge-side timeout for waiting on device data — **not** a
    session default fallback. ``0`` makes the bridge return immediately
    with no data; pass a positive value (the default is 10 s) to give
    the device time to respond.

    Response shape depends on whether the device returned data within
    the timeout:

    - **With data** (per wire spec): preliminary status chunk, then one
      or more data chunks (each a chunk-wrapped block of device bytes),
      then an END marker (flags=1 length=0), then the final status
      chunk whose ``cnt`` equals the total payload length.
    - **Without data** (timeout or no response): preliminary status
      chunk, then the final status chunk directly — no END marker, no
      intermediate data chunks. The parser distinguishes the two paths
      by inspecting the body of each candidate-data chunk: a 12-byte
      chunk whose body parses as a status header with CMPL/ERR/END/TIMO
      bits set is the final status, not data.

    """
    # Frame: 16 00 00 00 [htonl(tmo_ms):4] 00 00 00 00
    frame = struct.pack("!BBHL4x", 0x16, 0x00, 0x0000, tmo_ms)
    self.send_main(frame)
    # Preliminary status (typically sta=0x0100 cnt=0, err may be 0xFFFF).
    sta_p, err_p, _ = self.read_status_main()
    if sta_p & STA_ERR:
        raise NIEnet100IOError(sta_p, err_p, "ibrd preliminary")

    payload = bytearray()
    _status_bits = STA_CMPL | STA_ERR | STA_END | STA_TIMO
    while True:
        flags, length = parse_chunk_header(self.recv_main_exactly(CHUNK_HEADER_SIZE))

        if flags == CHUNK_FLAG_END:
            # Spec path: END marker, followed by the final status chunk.
            if length != 0:
                raise NIEnet100ProtocolError(
                    "END chunk has non-zero length %d" % length
                )
            sta_f, err_f, _cnt = self.read_status_main()
            if sta_f & STA_ERR:
                raise NIEnet100IOError(sta_f, err_f, "ibrd final")
            return bytes(payload)

        if flags == CHUNK_FLAG_SIGNAL:
            self.recv_main_exactly(1)
            continue

        if flags != CHUNK_FLAG_DATA:
            if length == 0:
                LOGGER.warning(
                    "treating unknown chunk flag 0x%04x (length=0) as end-of-stream",
                    flags,
                )
                return bytes(payload)
            raise NIEnet100ProtocolError(
                "unknown chunk flag 0x%04x with non-zero length %d" % (flags, length)
            )

        body = self.recv_main_exactly(length) if length else b""

        # No-data path: the bridge sends the final status as a length-12
        # data chunk without a preceding END marker. Detect by parsing
        # the body as a status header and checking for CMPL/ERR/END/TIMO
        # bits. Real device data of exactly 12 bytes whose first u16 is
        # one of those status values is in principle ambiguous, but the
        # leading 0x0100/0x8100/etc. patterns are rare enough in raw
        # instrument data that this heuristic is reliable in practice.
        if length == STATUS_HEADER_SIZE:
            sta_c, err_c, _cnt_c = parse_status_header(body)
            if sta_c & _status_bits:
                if sta_c & STA_ERR:
                    raise NIEnet100IOError(sta_c, err_c, "ibrd final")
                return bytes(payload)

        payload.extend(body)


def _ibclr(self: EnetConnection) -> None:
    """Clear the addressed device.

    Wire layout: ``04 00*11``.

    """
    self.transact_main(pack_command(0x04), "ibclr")


def _ibtrg(self: EnetConnection) -> None:
    """Assert the device trigger on the addressed device.

    Wire layout: ``20 00*11``.

    """
    self.transact_main(pack_command(0x20), "ibtrg")


def _ibloc(self: EnetConnection) -> None:
    """Send go-to-local to the addressed device.

    Wire layout: ``10 00*11``.

    """
    self.transact_main(pack_command(0x10), "ibloc")


def _ibrsp(self: EnetConnection) -> int:
    """Serial-poll the addressed device and return the status byte (STB).

    Wire layout (request): ``19 00*11``. The response is **one** data
    chunk whose length is 13: the first 12 bytes are the standard status
    header (with ``cnt=1``), and the trailing byte is the STB. Other
    verbs that the status header always comes alone do not apply here —
    ibrsp is special in that the response payload is glued to the status
    inside the same chunk.

    """
    self.send_main(pack_command(0x19))
    chunk = read_one_data_chunk(self.recv_main_exactly)
    if len(chunk) < STATUS_HEADER_SIZE + 1:
        raise NIEnet100ProtocolError(
            "ibrsp chunk has %d bytes, expected at least %d"
            % (len(chunk), STATUS_HEADER_SIZE + 1)
        )
    sta, err, cnt = parse_status_header(chunk[:STATUS_HEADER_SIZE])
    if sta & STA_ERR:
        raise NIEnet100IOError(sta, err, "ibrsp")
    if cnt != 1:
        raise NIEnet100ProtocolError("ibrsp cnt=%d, expected 1" % cnt)
    return chunk[STATUS_HEADER_SIZE]


def _set_io_timeout(self: EnetConnection, tmo_code: int) -> None:
    """Set the wire-level I/O timeout via the IbcTMO property (idx 0x03).

    ``tmo_code`` is a discrete NI-488.2 timeout index, not milliseconds —
    use :func:`seconds_to_tmo_code` to convert.

    """
    self.transact_main(_pack_property_set(0x03, tmo_code), "set IbcTMO")


def _transact_main_status(
    self: EnetConnection, operation: str = ""
) -> tuple[int, int, int]:
    """Read a status header on the main socket and raise on error.

    Sibling of :meth:`EnetConnection.transact_main` for verbs that have
    already sent their frame (and any payload) via :meth:`send_main`.

    """
    sta, err, cnt = self.read_status_main()
    if sta & STA_ERR:
        raise NIEnet100IOError(sta, err, operation)
    return sta, err, cnt


def _ibsic(self: EnetConnection) -> None:
    """Pulse the GPIB IFC (Interface Clear) line on the bridge.

    Sends ``'O 49'`` on the control socket (lazily opened). The frame
    carries the main socket's ``getsockname()`` so the box knows which
    session is asking. Wire layout::

        4f 49 00 00 [ip_main:4] [htons(port_main):2] 00 00

    """
    self.ensure_control_socket()
    assert self.control is not None
    if self.main is None:
        raise NIEnet100Error("cannot ibsic: main socket is not open")
    main_ip, main_port = self.main.getsockname()
    frame = _pack_o_verb(
        sub_op=0x49,
        leading_u16=0,
        ip_u32=_u32_from_ip(main_ip),
        port=main_port,
    )
    self.control.sendall(frame)
    control = self.control
    sta, err, _cnt = read_status_chunk(lambda n: self._recv_exactly(control, n))
    if sta & STA_ERR:
        raise NIEnet100IOError(sta, err, "ibsic")


def _notify_off_async_device(self: EnetConnection) -> None:
    """Deregister the device-mode async event channel.

    Sends ``'O 4e'`` on the control socket. Pairs with the async-register
    fired by :meth:`ensure_wait_socket`. Wire layout::

        4f 4e 00 01 [ip_main:4] [htons(port_main):2] 00 00

    Best-effort cleanup; callers typically ignore errors and close the
    sockets anyway.

    """
    self.ensure_control_socket()
    assert self.control is not None
    if self.main is None:
        raise NIEnet100Error("cannot notify-off: main socket is not open")
    main_ip, main_port = self.main.getsockname()
    frame = _pack_o_verb(
        sub_op=0x4E,
        leading_u16=1,
        ip_u32=_u32_from_ip(main_ip),
        port=main_port,
    )
    self.control.sendall(frame)
    control = self.control
    sta, err, _cnt = read_status_chunk(lambda n: self._recv_exactly(control, n))
    if sta & STA_ERR:
        raise NIEnet100IOError(sta, err, "notify-off async device")


def _ibwait(self: EnetConnection, mask: int) -> int:
    """Issue one ibwait round-trip on the wait socket and return ``sta``.

    Sends a single ``0x54`` poll frame carrying ``mask`` (a 16-bit ibsta
    bitmask of the events the caller is interested in — typically
    ``STA_RQS`` for SRQ, optionally OR'd with ``STA_TIMO`` so the box's
    own IbcTMO surfaces as a timeout event). The box responds
    synchronously with a 12-byte status header that the caller inspects
    against ``mask``:

        sta = conn.ibwait(STA_RQS | STA_TIMO)
        if sta & STA_RQS:
            stb = conn.ibrsp()   # quittiert RQS
        elif sta & STA_TIMO:
            ...   # no SRQ within IbcTMO

    Polling-loop semantics are not built in here — see the wire spec
    section 3.9.5 for the standard pattern. A poll interval of 0.2-0.5 s
    is plenty for single-threaded adapters.

    Wire layout: ``54 00 [htons(mask):2] 00*8``. The wait socket is
    opened lazily via :meth:`ensure_wait_socket` on first call.

    """
    self.ensure_wait_socket()
    assert self.wait is not None  # ensure_wait_socket guarantees this
    self.wait.sendall(pack_command(cmd_id=0x54, b1=0x00, w1=mask))
    wait = self.wait
    sta, err, _cnt = read_status_chunk(lambda n: self._recv_exactly(wait, n))
    if sta & STA_ERR:
        raise NIEnet100IOError(sta, err, "ibwait")
    return sta


# Attach verbs to EnetConnection. Keeping them as module-level functions
# makes the wire-bytes-per-verb mapping straightforward to read in this
# file; binding them here gives users the familiar `conn.ibwrt(...)` API.
# The signatures are declared under TYPE_CHECKING in the class body, so the
# assignments below need to silence mypy's method-reassignment guard.
EnetConnection.ibwrt = _ibwrt  # type: ignore[method-assign]
EnetConnection.ibrd = _ibrd  # type: ignore[method-assign]
EnetConnection.ibclr = _ibclr  # type: ignore[method-assign]
EnetConnection.ibtrg = _ibtrg  # type: ignore[method-assign]
EnetConnection.ibloc = _ibloc  # type: ignore[method-assign]
EnetConnection.ibrsp = _ibrsp  # type: ignore[method-assign]
EnetConnection.ibwait = _ibwait  # type: ignore[method-assign]
EnetConnection.ibsic = _ibsic  # type: ignore[method-assign]
EnetConnection.notify_off_async_device = _notify_off_async_device  # type: ignore[method-assign]
EnetConnection.set_io_timeout = _set_io_timeout  # type: ignore[method-assign]
EnetConnection.transact_main_status = _transact_main_status  # type: ignore[method-assign]
