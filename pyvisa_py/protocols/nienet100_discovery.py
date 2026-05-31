# -*- coding: utf-8 -*-
"""NI GPIB-ENET/100 UDP discovery protocol.

A small UDP protocol exposes the bridge's IP, MAC, serial number,
hostname, subnet, and gateway without requiring a TCP session — useful
for populating ``list_resources()`` and for finding a freshly
factory-reset box that does not yet have a known IP. Default port is
**44515** (broadcast); the **44516** unicast variant works across
subnets when the box IP is known in advance. All frames are exactly
184 bytes wrapped in an ``ED ... NI`` magic sandwich.

Wire reference: ``work/GPIB-ENET-100_Protocol.md`` section 2.

This module only handles frame encoding/decoding; the UDP socket loop
lives in :func:`discover` (added in a later commit).

:copyright: 2026 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

import socket
import struct
from dataclasses import dataclass
from typing import Optional

#: Default UDP port for broadcast discovery on the local LAN.
PORT_BROADCAST = 44515

#: UDP port for the cross-subnet unicast variant (Box-IP must be known).
PORT_UNICAST = 44516

#: Fixed total length of every discovery frame.
FRAME_SIZE = 0xB8  # 184

#: First two bytes of a valid frame.
MAGIC_HEAD = b"ED"

#: Last two bytes of a valid frame.
MAGIC_TAIL = b"NI"

#: Protocol version byte (offset 0x05). The reverse-engineered firmware
#: emits and accepts this value; other values have not been observed.
PROTOCOL_VERSION = 0x02


# --- Op-codes (offset 0x02) -------------------------------------------------

OP_DISCOVER = 0x01  # request: probe the LAN for bridges
OP_OK = 0x08  # response: discovery answer (box is idle/ready)
OP_BUSY = 0x09  # response: box is busy with another client

#: Op-codes that are valid responses to a discovery probe.
_RESPONSE_OPS = frozenset({OP_OK, OP_BUSY})


# --- Frame field offsets (named here to keep pack/parse readable) -----------

_OFF_OPCODE = 0x02
_OFF_VERSION = 0x05
_OFF_NONCE = 0x06
_OFF_SERIAL = 0x0A
_OFF_MAC = 0x0E
_OFF_MODEL = 0x1C
_OFF_HOSTNAME = 0x3C
_OFF_COMMENT = 0x5C
_OFF_IP = 0x9E
_OFF_SUBNET = 0xA2
_OFF_GATEWAY = 0xAA
_OFF_TAIL = 0xB6

_LEN_MODEL = 32
_LEN_HOSTNAME = 32
_LEN_COMMENT = 64


@dataclass(frozen=True)
class BoxInfo:
    """Parsed contents of a GPIB-ENET/100 discovery reply.

    All strings have been decoded from the null-terminated ASCII byte
    blobs the box ships. Empty fields surface as the empty string.
    """

    #: Box IP in dotted-quad form (e.g. ``"192.0.2.5"``).
    ip: str

    #: MAC address as colon-separated lowercase hex (e.g. ``"00:80:2f:1a:2b:3c"``).
    mac: str

    #: Box serial number (32-bit unsigned, big-endian on the wire).
    serial: int

    #: Hardware model string reported by the box, e.g. ``"GPIB-ENET/100"``.
    model: str

    #: User-assigned hostname; empty if never configured.
    hostname: str

    #: Subnet mask in dotted-quad form.
    subnet: str

    #: Default gateway in dotted-quad form.
    gateway: str

    #: Free-form comment configured via NI MAX / EthernetConfig; usually empty.
    comment: str

    #: Echo of the probe's transaction nonce. Useful for matching replies to
    #: a specific probe in targeted (non-broadcast) operations.
    nonce: int

    #: Raw response op-code: ``OP_OK`` (0x08) or ``OP_BUSY`` (0x09).
    op_code: int

    @property
    def is_busy(self) -> bool:
        """``True`` when the box reported itself as busy with another client."""
        return self.op_code == OP_BUSY


def pack_discovery_request(nonce: int = 0) -> bytes:
    """Build a 184-byte discovery probe frame.

    All fields default to zero; only the magic sandwich, op-code, protocol
    version, and (optionally) the caller-supplied nonce are set. The nonce
    is echoed in the box's reply and lets callers correlate replies to
    probes when several probes are in flight.
    """
    buf = bytearray(FRAME_SIZE)
    buf[0:2] = MAGIC_HEAD
    buf[_OFF_OPCODE] = OP_DISCOVER
    buf[_OFF_VERSION] = PROTOCOL_VERSION
    struct.pack_into("!L", buf, _OFF_NONCE, nonce & 0xFFFFFFFF)
    buf[_OFF_TAIL : _OFF_TAIL + 2] = MAGIC_TAIL
    return bytes(buf)


def parse_discovery_response(buf: bytes) -> Optional[BoxInfo]:
    """Parse a 184-byte discovery reply into a :class:`BoxInfo`.

    Returns ``None`` for any frame that fails validation — wrong length,
    bad magic, or non-response op-code. Returning ``None`` (rather than
    raising) is intentional: the broadcast listener will receive arbitrary
    foreign UDP datagrams that should be silently discarded.
    """
    if len(buf) != FRAME_SIZE:
        return None
    if bytes(buf[0:2]) != MAGIC_HEAD:
        return None
    if bytes(buf[_OFF_TAIL : _OFF_TAIL + 2]) != MAGIC_TAIL:
        return None
    op_code = buf[_OFF_OPCODE]
    if op_code not in _RESPONSE_OPS:
        return None

    nonce = struct.unpack_from("!L", buf, _OFF_NONCE)[0]
    serial = struct.unpack_from("!L", buf, _OFF_SERIAL)[0]
    mac = ":".join("%02x" % b for b in buf[_OFF_MAC : _OFF_MAC + 6])
    model = _cstring(buf[_OFF_MODEL : _OFF_MODEL + _LEN_MODEL])
    hostname = _cstring(buf[_OFF_HOSTNAME : _OFF_HOSTNAME + _LEN_HOSTNAME])
    comment = _cstring(buf[_OFF_COMMENT : _OFF_COMMENT + _LEN_COMMENT])
    ip = socket.inet_ntoa(bytes(buf[_OFF_IP : _OFF_IP + 4]))
    subnet = socket.inet_ntoa(bytes(buf[_OFF_SUBNET : _OFF_SUBNET + 4]))
    gateway = socket.inet_ntoa(bytes(buf[_OFF_GATEWAY : _OFF_GATEWAY + 4]))

    return BoxInfo(
        ip=ip,
        mac=mac,
        serial=serial,
        model=model,
        hostname=hostname,
        subnet=subnet,
        gateway=gateway,
        comment=comment,
        nonce=nonce,
        op_code=op_code,
    )


def _cstring(buf: bytes) -> str:
    """Decode a null-terminated ASCII field from a fixed-size buffer.

    Bytes past the first NUL are ignored. Non-ASCII bytes are replaced
    with U+FFFD; the box should only ship ASCII but a robust parser does
    not crash on rubbish.
    """
    end = buf.find(b"\x00")
    if end < 0:
        end = len(buf)
    return bytes(buf[:end]).decode("ascii", errors="replace")
