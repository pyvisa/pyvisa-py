# -*- coding: utf-8 -*-
"""Python implementation of the NI GPIB-ENET/100 wire protocol.

This module talks the proprietary TCP protocol of the National Instruments
GPIB-ENET/100 Ethernet-to-GPIB bridge. It is **not** compatible with the
older GPIB-ENET (10 MBit/s, libnienet target), which uses a similar frame
layout but different verb opcodes and a single-step open.

Wire reference: ``work/GPIB-ENET-100_Protocol.md``.

All multi-byte fields are big-endian (network byte order).

:copyright: 2026 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

import logging
import socket
import struct
from typing import Callable, Optional, Tuple

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

STA_ERR = 0x8000   # operation error, ``err`` field carries the code
STA_TIMO = 0x4000  # timeout during operation
STA_END = 0x2000   # EOI or EOS match (talker signaled end-of-message)
STA_SRQI = 0x1000  # SRQ detected while controller-in-charge
STA_RQS = 0x0800   # device RQS asserted (set in ibrsp/ibwait responses)
STA_CMPL = 0x0100  # operation complete
STA_LOK = 0x0080   # lockout state
STA_REM = 0x0040   # remote state
STA_CIC = 0x0020   # controller-in-charge
STA_ATN = 0x0010   # ATN line asserted
STA_TACS = 0x0008  # talker active
STA_LACS = 0x0004  # listener active
STA_DTAS = 0x0002  # device trigger state
STA_DCAS = 0x0001  # device clear state


# --- NI-488.2 iberr codes (subset relevant to this protocol) ----------------

ERR_EDVR = 0     # OS error (rare)
ERR_ECIC = 1     # function requires controller-in-charge
ERR_ENOL = 2     # no listener on the bus
ERR_EADR = 3     # address error
ERR_EARG = 4     # invalid argument to API
ERR_ESAC = 5     # function requires system controller
ERR_EABO = 6     # I/O aborted / timeout
ERR_ENEB = 7     # non-existent board
ERR_EBUS = 0xa   # bus error
ERR_ECAP = 0xb   # capability disabled
ERR_EFSO = 0xc   # file-system error
ERR_EBNP = 0xd   # board not present
ERR_ESTB = 0xe   # serial-poll status byte lost
ERR_ESRQ = 0xf   # SRQ stuck on


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
TIMETABLE: Tuple = (
    None,    # TMO_NONE
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

CHUNK_FLAG_DATA = 0    # data chunk; ``length`` bytes of payload follow
CHUNK_FLAG_END = 1     # END marker; ``length`` must be 0, read complete
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


def parse_status_header(buf: bytes) -> Tuple[int, int, int]:
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


def parse_chunk_header(buf: bytes) -> Tuple[int, int]:
    """Decode a 4-byte chunk header into ``(flags, length)``."""
    if len(buf) != CHUNK_HEADER_SIZE:
        raise ValueError(
            "chunk header must be exactly %d bytes, got %d"
            % (CHUNK_HEADER_SIZE, len(buf))
        )
    return struct.unpack("!HH", buf)


# --- Chunk reader -----------------------------------------------------------
# Read responses are framed as: 12-B preliminary status header, then a stream
# of payload chunks, then (typically) a 12-B final status header. The chunk
# stream is stateful: chunks may be split across TCP segments and several
# chunks may arrive in one segment. The caller drives recv via the
# ``read_exactly`` callable so this layer is socket-agnostic and testable.


def read_chunks_until_end(read_exactly: Callable[[int], bytes]) -> bytes:
    """Consume a chunk stream until the END marker (flags=1).

    Tolerates out-of-band signal chunks (flags=2) by reading and discarding
    their single payload byte. Raises :class:`NIEnet100ProtocolError` for
    unknown flag values.

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
        else:
            raise NIEnet100ProtocolError(
                "unknown chunk flag 0x%04x (length=%d)" % (flags, length)
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
    and control sockets are not opened here; subclasses or callers that
    need SRQ polling open them lazily.

    Parameters
    ----------
    host : str
        Box IP or hostname.
    open_timeout : float
        Per-socket connect timeout in seconds.
    timeout : Optional[float]
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
    """

    #: Companion-hello flag word for device-mode sessions (single resource).
    COMPANION_FLAGS_DEVICE = 2

    def __init__(
        self,
        host: str,
        open_timeout: float = 10.0,
        timeout: Optional[float] = 10.0,
    ) -> None:
        self.host = host
        self._open_timeout = open_timeout
        self._timeout = timeout
        self.main: Optional[socket.socket] = None
        self.companion: Optional[socket.socket] = None

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

    def close(self) -> None:
        """Close every open socket. Idempotent."""
        for attr in ("companion", "main"):
            sock = getattr(self, attr, None)
            if sock is not None:
                try:
                    sock.close()
                except OSError as e:
                    LOGGER.debug("error closing %s socket: %s", attr, e)
                setattr(self, attr, None)

    def set_socket_timeout(self, timeout: Optional[float]) -> None:
        """Apply ``timeout`` (in seconds) to all currently open sockets.

        Use ``None`` for blocking without timeout. The value is cached so
        sockets opened later (wait/control) pick up the same setting.
        """
        self._timeout = timeout
        for sock in (self.main, self.companion):
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
        """Read exactly ``n`` bytes from the main socket or raise."""
        if self.main is None:
            raise NIEnet100Error("main socket is not open")
        return self._recv_exactly(self.main, n)

    def send_main(self, data: bytes) -> None:
        """Send ``data`` on the main socket in a single ``sendall``."""
        if self.main is None:
            raise NIEnet100Error("main socket is not open")
        self.main.sendall(data)

    def read_status_main(self) -> Tuple[int, int, int]:
        """Read and parse a 12-byte status header from the main socket."""
        return parse_status_header(self.recv_main_exactly(STATUS_HEADER_SIZE))

    def transact_main(
        self, frame: bytes, operation: str = ""
    ) -> Tuple[int, int, int]:
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
        sta, err, _cnt = parse_status_header(
            self._recv_exactly(self.companion, STATUS_HEADER_SIZE)
        )
        if sta & STA_ERR:
            raise NIEnet100IOError(sta, err, "companion hello")

    # --- GPIB-session open / close (Frames A-G of the spec) -----------

    #: Default Frame-C board flags. Sets HS488 marker + an EOI/EOS bit and
    #: leaves everything else off — the conservative baseline for a generic
    #: instrument session.
    DEFAULT_BOARD_FLAGS = 0x1801

    #: Default Frame-E event-queue depth.
    DEFAULT_EVENT_QUEUE_DEPTH = 0x0b

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
            w2=(primary_address << 8) | (secondary_address & 0xff),
            w3=0,
            dw=(tmo_code << 24) | 0x0400,
        )
        self.transact_main(frame_a, "open Frame A SetConfig SC")

        # Frame B: Property 'Mode' (PPC, idx 0x05).
        # Wire bytes: 50 05 [mode_byte] 00*9
        self.transact_main(
            _pack_property_set(0x05, mode_byte), "open Frame B PPC"
        )

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
        self.transact_main(
            _pack_property_set(0x10, 0x01), "open Frame D online"
        )

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
        self.transact_main(
            pack_command(0x4e, 0x01), "open Frame G notify-off sync"
        )

    def close_gpib_session(self) -> None:
        """Close the operation bracket opened by :meth:`open_gpib_session`.

        Sockets are not closed here — call :meth:`close` for that.
        Safe to call if the bracket was never opened (errors are logged
        and swallowed so socket cleanup always runs).
        """
        if self.main is None:
            return
        try:
            self._transact_bracket(enter=False)
        except (NIEnet100Error, OSError) as e:
            LOGGER.debug("error closing GPIB bracket: %s", e)

    def _transact_bracket(self, enter: bool) -> None:
        # Wire bytes: 58 [01|00] 01 00*9
        frame = struct.pack(
            "!BBB9x", 0x58, 0x01 if enter else 0x00, 0x01
        )
        self.transact_main(
            frame, "bracket %s" % ("open" if enter else "close")
        )


def _pack_property_set(prop_idx: int, value_byte: int) -> bytes:
    """Build a 'P' property-set frame (0x50).

    Wire layout: ``50 [prop_idx] [value_byte] 00*9``.
    """
    return struct.pack("!BBB9x", 0x50, prop_idx, value_byte)


# --- Device-level verbs -----------------------------------------------------
# These methods are added to EnetConnection via assignment below. They cover
# the minimal pyvisa-Resource API surface: write, read, clear, trigger,
# serial poll, local-lockout release, and the I/O timeout setter. Async
# verbs (ibwait, ibnotify) and board-level verbs (ibsic, ibcmd) live in
# later commits since they require the wait/control sockets.


def _ibwrt(self: EnetConnection, data: bytes) -> int:
    """Write ``data`` to the addressed device.

    Wire layout: ``62 00 00 00 [htonl(byte_count):4] 00 00 00 00`` followed
    immediately by the raw payload in the same ``sendall``. Odd-length
    payloads are zero-padded on the wire; ``byte_count`` is the original
    unpadded length.

    Returns the number of bytes the box reports as transferred.
    """
    byte_count = len(data)
    # Frame: 62 00 00 00 [htonl(byte_count):4] 00 00 00 00
    header = struct.pack("!BBHL4x", 0x62, 0x00, 0x0000, byte_count)
    payload = data + (b"\x00" if byte_count % 2 else b"")
    self.send_main(header + payload)
    _sta, _err, cnt = self.transact_main_status("ibwrt")
    return cnt


def _ibrd(self: EnetConnection, tmo_ms: int = 0) -> bytes:
    """Read one message from the addressed device.

    The wire-level read pulls bytes until the device signals end-of-message
    (EOI/EOS). The box does not take a maximum-byte argument — callers that
    want to truncate must do so after the fact.

    Wire layout: ``16 00 00 00 [htonl(tmo_ms):4] 00 00 00 00``. ``tmo_ms``
    is a per-read override; ``0`` means use the session default timeout.

    Response sequence: preliminary status header, data chunks until END,
    final status header (whose ``cnt`` matches the payload length).
    """
    # Frame: 16 00 00 00 [htonl(tmo_ms):4] 00 00 00 00
    frame = struct.pack("!BBHL4x", 0x16, 0x00, 0x0000, tmo_ms)
    self.send_main(frame)
    # Preliminary status (typically sta=0x0100 cnt=0, err may be 0xFFFF).
    sta_p, err_p, _ = self.read_status_main()
    if sta_p & STA_ERR:
        raise NIEnet100IOError(sta_p, err_p, "ibrd preliminary")
    # Payload chunks until END.
    data = read_chunks_until_end(self.recv_main_exactly)
    # Final status with the real count.
    sta_f, err_f, _cnt = self.read_status_main()
    if sta_f & STA_ERR:
        raise NIEnet100IOError(sta_f, err_f, "ibrd final")
    return data


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

    Wire layout (request): ``19 00*11``. The response is the standard
    12-byte status header followed by a single data chunk carrying the
    STB. Per the wire spec the END marker may be omitted — we deliberately
    do not try to consume it and accept that the next operation will see
    any trailing bytes if the firmware does emit them. If that turns out
    to bite us in practice we will revisit with a peek-based consumer.
    """
    self.send_main(pack_command(0x19))
    sta, err, _ = self.read_status_main()
    if sta & STA_ERR:
        raise NIEnet100IOError(sta, err, "ibrsp")
    stb_bytes = read_one_data_chunk(self.recv_main_exactly)
    if len(stb_bytes) != 1:
        raise NIEnet100ProtocolError(
            "ibrsp returned %d bytes, expected 1" % len(stb_bytes)
        )
    return stb_bytes[0]


def _set_io_timeout(self: EnetConnection, tmo_code: int) -> None:
    """Set the wire-level I/O timeout via the IbcTMO property (idx 0x03).

    ``tmo_code`` is a discrete NI-488.2 timeout index, not milliseconds —
    use :func:`seconds_to_tmo_code` to convert.
    """
    self.transact_main(_pack_property_set(0x03, tmo_code), "set IbcTMO")


def _transact_main_status(
    self: EnetConnection, operation: str = ""
) -> Tuple[int, int, int]:
    """Read a status header on the main socket and raise on error.

    Sibling of :meth:`EnetConnection.transact_main` for verbs that have
    already sent their frame (and any payload) via :meth:`send_main`.
    """
    sta, err, cnt = self.read_status_main()
    if sta & STA_ERR:
        raise NIEnet100IOError(sta, err, operation)
    return sta, err, cnt


# Attach verbs to EnetConnection. Keeping them as module-level functions
# makes the wire-bytes-per-verb mapping straightforward to read in this
# file; binding them here gives users the familiar `conn.ibwrt(...)` API.
EnetConnection.ibwrt = _ibwrt
EnetConnection.ibrd = _ibrd
EnetConnection.ibclr = _ibclr
EnetConnection.ibtrg = _ibtrg
EnetConnection.ibloc = _ibloc
EnetConnection.ibrsp = _ibrsp
EnetConnection.set_io_timeout = _set_io_timeout
EnetConnection.transact_main_status = _transact_main_status
