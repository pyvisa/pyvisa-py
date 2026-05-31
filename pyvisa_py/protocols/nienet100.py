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

import struct
from typing import Tuple

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
