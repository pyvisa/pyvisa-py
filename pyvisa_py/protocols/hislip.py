"""Implement HiSLIP instrument communication over two TCP channels.

HiSLIP references identify sections, tables, or locally numbered rules in
IVI-6.1 HiSLIP 2.0 (2020-04-23):
https://www.ivifoundation.org/downloads/Protocol%20Specifications/IVI-6.1_HiSLIP-2.0-2020-04-23.pdf

VPP-4.3 references retain the document's RULE, RECOMMENDATION, PERMISSION,
and OBSERVATION labels, using revision 7.2.1 (2024-01-04):
https://www.ivifoundation.org/downloads/VISA/vpp43_2024-01-04.pdf

The client advertises HiSLIP 1.0 by default. Citing the 2.0 specification does
not imply that its secure-connection capability is implemented.

Response classes accept a connected ``sock`` and inherit the transport and
validation errors documented by ``RxHeader``. Unless stated otherwise, they
consume only the header, leaving any payload on the socket.
"""

import queue
import select
import socket
import struct
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Optional, Tuple

from pyvisa_py.common import LOGGER, BytesBuffer, MutableBytesBuffer, connect_timeout

# HiSLIP 2.2 assigns this default port but permits other configured ports.
PORT = 4880

# HiSLIP 2.5, Table 4; the maximum-message-size names are abbreviated locally.
MESSAGETYPE_STR: Dict[int, str] = {
    0: "Initialize",
    1: "InitializeResponse",
    2: "FatalError",
    3: "Error",
    4: "AsyncLock",
    5: "AsyncLockResponse",
    6: "Data",
    7: "DataEnd",
    8: "DeviceClearComplete",
    9: "DeviceClearAcknowledge",
    10: "AsyncRemoteLocalControl",
    11: "AsyncRemoteLocalResponse",
    12: "Trigger",
    13: "Interrupted",
    14: "AsyncInterrupted",
    15: "AsyncMaxMsgSize",
    16: "AsyncMaxMsgSizeResponse",
    17: "AsyncInitialize",
    18: "AsyncInitializeResponse",
    19: "AsyncDeviceClear",
    20: "AsyncServiceRequest",
    21: "AsyncStatusQuery",
    22: "AsyncStatusResponse",
    23: "AsyncDeviceClearAcknowledge",
    24: "AsyncLockInfo",
    25: "AsyncLockInfoResponse",
    26: "GetDescriptors",
    27: "GetDescriptorsResponse",
    28: "StartTLS",
    29: "AsyncStartTLS",
    30: "AsyncStartTLSResponse",
    31: "EndTLS",
    32: "AsyncEndTLS",
    33: "AsyncEndTLSResponse",
    34: "GetSaslMechanismList",
    35: "GetSaslMechanismListResponse",
    36: "AuthenticationStart",
    37: "AuthenticationExchange",
    38: "AuthenticationResult",
    # 39-127: reserved for future use; 128-255: vendor-specific messages.
}
MESSAGETYPE: Dict[str, int] = {value: key for (key, value) in MESSAGETYPE_STR.items()}

# HiSLIP 6.2, Table 14.
FATALERRORMESSAGE: Dict[int, str] = {
    0: "Unidentified error",
    1: "Poorly formed message header",
    2: "Attempt to use connection without both channels established",
    3: "Invalid Initialization sequence",
    4: "Server refused connection due to maximum number of clients exceeded",
    5: "Secure connection failed",
    # 6-127: reserved for extensions; 128-255: device-defined errors.
}
FATALERRORCODE: Dict[str, int] = {
    value: key for (key, value) in FATALERRORMESSAGE.items()
}

# HiSLIP 6.3, Table 16.
ERRORMESSAGE: Dict[int, str] = {
    0: "Unidentified error",
    1: "Unrecognized Message Type",
    2: "Unrecognized control code",
    3: "Unrecognized Vendor Defined Message",
    4: "Message too large",
    5: "Authentication failed",
    # 6-127: reserved for extensions; 128-255: device-defined errors.
}
ERRORCODE: Dict[str, int] = {value: key for (key, value) in ERRORMESSAGE.items()}

# HiSLIP 6.5, Table 21 defines both operation and response control codes.
LOCKCONTROLCODE: Dict[str, int] = {
    "release": 0,
    "request": 1,
}

LOCKRESPONSE: Dict[int, str] = {
    0: "failure",
    1: "success",  # For release, this specifically means exclusive-lock success.
    2: "success shared",
    3: "error",
}

# HiSLIP 6.7, Table 25 maps these operations to VISA remote/local controls.
REMOTELOCALCONTROLCODE: Dict[str, int] = {
    "disableRemote": 0,
    "enableRemote": 1,
    "disableAndGTL": 2,
    "enableAndGotoRemote": 3,
    "enableAndLockoutLocal": 4,
    "enableAndGTRLLO": 5,
    "justGTL": 6,
}

HEADER_FORMAT = "!2sBBIQ"
# HiSLIP 2.3, Table 2: network-order "HS", type (8 bits), control (8 bits),
# parameter (32 bits), and payload length (64 bits).
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

# VPP-4.3 5.1.2: VI_ATTR_TCPIP_HISLIP_MAX_MESSAGE_KB defaults to 1024 KiB.
DEFAULT_MAX_MSG_SIZE = 1 << 20


class HiSLIPInterruptedError(Exception):
    """Raised when I/O is cancelled locally or interrupted by a HiSLIP server.

    Parameters
    ----------
    message_id : int
        Interrupted message ID, or zero for local cancellation.

    The session layer translates this exception to VISA ``VI_ERROR_ABORT``;
    VPP-4.3 3.5.1.1 describes that result for local operation termination.
    """

    def __init__(self, message_id: int = 0):
        self.message_id = message_id
        super().__init__(f"HiSLIP I/O terminated (message_id={message_id:#x})")


class CancellableSocket(socket.socket):
    """Take ownership of a socket and allow cross-thread read cancellation.

    Parameters
    ----------
    sock : socket.socket
        Connected socket whose file descriptor is transferred to this object.
        Its kernel options and Python-level timeout are preserved.

    A socket pair implements the self-pipe technique portably: ``cancel()``
    wakes ``recv_into()``, which raises ``HiSLIPInterruptedError``. Socket
    creation and configuration errors propagate as ``OSError``.

    This is a local mechanism for VPP-4.3 3.5.1.1 ``viTerminate`` behaviour with
    a VI_NULL job ID, not a HiSLIP Interrupted transaction (HiSLIP 6.11).
    """

    def __init__(self, sock: socket.socket) -> None:
        # Detaching preserves kernel options, but not Python-level timeout state.
        family, type_, proto = sock.family, sock.type, sock.proto
        timeout = sock.gettimeout()
        fd = sock.detach()
        super().__init__(family=family, type=type_, proto=proto, fileno=fd)
        self.settimeout(timeout)
        self._cancel_r, self._cancel_w = socket.socketpair()
        self._cancel_r.setblocking(False)
        self._cancel_w.setblocking(False)
        self._cancel_enabled = True

    def recv_into(self, buffer, nbytes: int = 0, flags: int = 0) -> int:
        """Receive into a writable buffer, allowing another thread to cancel.

        Parameters
        ----------
        buffer : writable buffer
            Destination for the received bytes.
        nbytes : int
            Maximum bytes to receive; zero uses the buffer length.
        flags : int
            Socket receive flags, including ``socket.MSG_PEEK``.

        Returns
        -------
        int
            Number of bytes received, or zero if the peer closed the connection.

        Raises
        ------
        HiSLIPInterruptedError
            A cancellation signal was received.
        socket.timeout
            No data or cancellation arrived within the socket timeout.
        OSError
            The socket operation failed.
        """
        if not self._cancel_enabled:
            return super().recv_into(buffer, nbytes, flags)
        timeout = self.gettimeout()
        readable, _, _ = select.select([self, self._cancel_r], [], [], timeout)
        if not readable:
            raise socket.timeout("timed out")
        if self._cancel_r in readable:
            self.drain_cancel()
            raise HiSLIPInterruptedError(0)
        return super().recv_into(buffer, nbytes, flags)

    def cancel(self) -> None:
        """Signal the wakeup socket to unblock a pending ``recv_into()`` call."""
        try:
            self._cancel_w.send(b"\x00")
        except BlockingIOError:
            pass  # A full wakeup socket already carries a cancellation signal.

    def drain_cancel(self) -> None:
        """Discard queued cancellation signals so later reads can proceed."""
        try:
            while self._cancel_r.recv(1024):
                pass
        except BlockingIOError:
            pass

    def close(self) -> None:
        """Close the wakeup socket pair and the owned connection."""
        self._cancel_r.close()
        self._cancel_w.close()
        super().close()


def receive_flush(sock: socket.socket, recv_len: int) -> None:
    """Receive and discard exactly ``recv_len`` bytes using the socket timeout.

    Parameters
    ----------
    sock : socket.socket
        Connected socket to drain.
    recv_len : int
        Non-negative number of bytes to discard.

    Raises
    ------
    HiSLIPInterruptedError
        A read on a ``CancellableSocket`` was cancelled.
    RuntimeError
        The peer closed the connection before all bytes were discarded.
    OSError
        A socket read failed, including a timeout.
    """
    # Bound temporary memory even when discarding a very large payload.
    recv_buffer = bytearray(min(1 << 20, recv_len))
    bytes_recvd = 0

    while bytes_recvd < recv_len:
        request_size = min(len(recv_buffer), recv_len - bytes_recvd)
        data_len = sock.recv_into(recv_buffer, request_size)
        if data_len == 0:
            raise RuntimeError("Connection was dropped by server.")
        bytes_recvd += data_len


def receive_exact(sock: socket.socket, recv_len: int) -> bytearray:
    """Receive exactly ``recv_len`` bytes using the socket's current timeout.

    Parameters
    ----------
    sock : socket.socket
        Connected socket to read.
    recv_len : int
        Non-negative number of bytes to receive.

    Returns
    -------
    bytearray
        The requested bytes, or an empty buffer when ``recv_len`` is zero.

    Raises
    ------
    HiSLIPInterruptedError
        A read on a ``CancellableSocket`` was cancelled.
    RuntimeError
        The peer closed the connection before the buffer was filled.
    OSError
        A socket read failed, including a timeout.
    """
    recv_buffer = bytearray(recv_len)
    receive_exact_into(sock, recv_buffer)
    return recv_buffer


def receive_exact_into(sock: socket.socket, recv_buffer: MutableBytesBuffer) -> None:
    """Fill ``recv_buffer`` completely using the socket's current timeout.

    Parameters
    ----------
    sock : socket.socket
        Connected socket to read.
    recv_buffer : MutableBytesBuffer
        Writable destination buffer.

    Raises
    ------
    HiSLIPInterruptedError
        A read on a ``CancellableSocket`` was cancelled.
    RuntimeError
        The peer closed the connection before the buffer was filled.
    MemoryError
        The socket reported more bytes than the buffer could hold.
    OSError
        A socket read failed, including a timeout.
    """
    view = memoryview(recv_buffer)
    recv_len = len(recv_buffer)
    bytes_recvd = 0

    while bytes_recvd < recv_len:
        request_size = recv_len - bytes_recvd
        data_len = sock.recv_into(view, request_size)
        if data_len == 0:
            raise RuntimeError("Connection was dropped by server.")
        bytes_recvd += data_len
        view = view[data_len:]

    if bytes_recvd > recv_len:
        raise MemoryError("socket.recv_into scribbled past end of recv_buffer")


def send_msg(
    sock: socket.socket,
    msg_type: str,
    control_code: int,
    message_parameter: Optional[int],
    payload: BytesBuffer = b"",
) -> None:
    """Pack and send a complete HiSLIP frame.

    Parameters
    ----------
    sock : socket.socket
        Connected channel on which to send the frame.
    msg_type : str
        Message name in ``MESSAGETYPE``.
    control_code : int
        Unsigned 8-bit control field.
    message_parameter : Optional[int]
        Unsigned 32-bit parameter; ``None`` is encoded as zero.
    payload : BytesBuffer
        Optional bytes following the header.

    Raises
    ------
    KeyError
        The message name is unknown.
    struct.error
        A field does not fit its wire representation.
    OSError
        Sending failed, including a socket timeout.
    """
    msg = bytearray(
        struct.pack(
            HEADER_FORMAT,
            b"HS",
            MESSAGETYPE[msg_type],
            control_code,
            message_parameter or 0,
            len(payload),
        )
    )
    msg.extend(payload)
    sock.sendall(msg)


class RxHeader:
    """Receive and decode a HiSLIP header, leaving its payload unread.

    Parameters
    ----------
    sock : socket.socket
        Connected channel from which to receive the header.
    expected_message_type : Optional[str]
        Required message name, or ``None`` to accept any recognised type.

    Raises
    ------
    RuntimeError
        The connection closed, the header is invalid, or the type is unexpected.
        An unexpected recognised message has its payload consumed before raising.
    AssertionError
        A Data/DataEnd control code, or a subclass-specific fixed field, is invalid.
    HiSLIPInterruptedError
        A read on a ``CancellableSocket`` was cancelled.
    OSError
        Receiving failed, including a socket timeout.

    The layout and reserved-field defaults are defined by HiSLIP 2.3, Table 2.
    Server Data/DataEnd control codes are zero per HiSLIP 6.4, Table 18.
    """

    def __init__(
        self,
        sock: socket.socket,
        expected_message_type: Optional[str] = None,
    ) -> None:
        """Read the header and optionally require ``expected_message_type``."""
        self.header = receive_exact(sock, HEADER_SIZE)
        (
            prologue,
            msg_type,
            self.control_code,
            self.message_parameter,
            self.payload_length,
        ) = struct.unpack(HEADER_FORMAT, self.header)

        if prologue != b"HS":
            # TODO (HiSLIP 6.2): Send FatalError on both channels and close them;
            # this parser currently only raises.
            raise RuntimeError("protocol synchronization error")

        if msg_type not in MESSAGETYPE_STR:
            # TODO (HiSLIP 6.3): Discard the payload, then reply with Error on
            # the channel that received the unrecognised message.
            raise RuntimeError("unrecognized message type: %d" % msg_type)

        self.msg_type = MESSAGETYPE_STR[msg_type]

        if expected_message_type is not None and self.msg_type != expected_message_type:
            # TODO (HiSLIP 6.3): Reply with Error after discarding this payload.
            payload = (
                (": " + str(receive_exact(sock, self.payload_length)))
                if self.payload_length > 0
                else ""
            )
            raise RuntimeError(
                "expected message type '%s', received '%s%s'"
                % (expected_message_type, self.msg_type, payload)
            )

        if self.msg_type == "DataEnd" or self.msg_type == "Data":
            assert self.control_code == 0
            self.message_id = self.message_parameter


class InitializeResponse(RxHeader):
    """Read the server's reported mode, encoded version, and session ID.

    ``sock`` is the synchronous channel awaiting an InitializeResponse
    (HiSLIP 6.1, Table 12).
    """

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "InitializeResponse")
        assert self.payload_length == 0
        self.overlap = bool(self.control_code)
        self.version, self.session_id = struct.unpack("!4xHH8x", self.header)


class AsyncInitializeResponse(RxHeader):
    """Read the async initialisation reply from ``sock``.

    HiSLIP 6.1, Table 12 places capability bits in the control code and the vendor
    ID in the message parameter. This parser requires a zero control code and
    retains the upper parameter word as ``server_capabilities`` instead; that
    attribute is not the HiSLIP 2.0 capability field. Secure connections are not
    implemented.
    """

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "AsyncInitializeResponse")
        assert self.control_code == 0
        assert self.payload_length == 0
        message_param = struct.unpack("!4x2s2s8x", self.header)
        self.server_capabilities = message_param[0]
        self.vendor_id = message_param[1]


class AsyncMaxMsgSizeResponse(RxHeader):
    """Read the server's receive limit from ``sock``, consuming its payload.

    HiSLIP 6.10, Table 28 defines the payload as an eight-byte network-order size.
    """

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "AsyncMaxMsgSizeResponse")
        assert self.control_code == 0
        assert self.message_parameter == 0
        assert self.payload_length == 8
        payload = receive_exact(sock, self.payload_length)
        self.max_msg_size = struct.unpack("!Q", payload)[0]


class AsyncDeviceClearAcknowledge(RxHeader):
    """Read the server's preferred device-clear features from ``sock``.

    See HiSLIP 6.12, Table 30 and feature negotiation in 6.12.1, Table 31.
    """

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "AsyncDeviceClearAcknowledge")
        self.feature_bitmap = self.control_code
        assert self.message_parameter == 0
        assert self.payload_length == 0


class AsyncInterrupted(RxHeader):
    """Read the interrupted message ID from the async channel ``sock``.

    This is one half of the HiSLIP 6.11, Table 29 interruption transaction.
    """

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "AsyncInterrupted")
        assert self.control_code == 0
        self.message_id = self.message_parameter
        assert self.payload_length == 0


class AsyncLockInfoResponse(RxHeader):
    """Read exclusive-lock status and the lock-holding client count from ``sock``.

    HiSLIP 6.6, Table 23 counts each client once, even if it holds both lock types.
    """

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "AsyncLockInfoResponse")
        self.exclusive_lock = self.control_code
        self.clients_holding_locks = self.message_parameter
        assert self.payload_length == 0


class AsyncLockResponse(RxHeader):
    """Read a lock result from ``sock`` and translate it through ``LOCKRESPONSE``.

    HiSLIP 6.5, Table 21 defines the result codes for requests and releases.
    An unknown result code raises ``KeyError``.
    """

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "AsyncLockResponse")
        self.lock_response = LOCKRESPONSE[self.control_code]
        assert self.message_parameter == 0
        assert self.payload_length == 0


class AsyncRemoteLocalResponse(RxHeader):
    """Read a remote/local-control acknowledgement from ``sock``.

    The fixed zero fields are defined by HiSLIP 6.7, Table 24.
    """

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "AsyncRemoteLocalResponse")
        assert self.control_code == 0
        assert self.message_parameter == 0
        assert self.payload_length == 0


class AsyncServiceRequest(RxHeader):
    """Read an unsolicited service request's status byte from ``sock``.

    HiSLIP 6.13, Table 32 specifies no acknowledgement. Receipt alone does not
    clear RQS; an AsyncStatusQuery is needed to enable further requests.
    """

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "AsyncServiceRequest")
        self.server_status = self.control_code
        assert self.message_parameter == 0
        assert self.payload_length == 0


class AsyncStatusResponse(RxHeader):
    """Read the server's status byte from an AsyncStatusResponse on ``sock``.

    HiSLIP 6.14, Table 33 places the status byte in the control code.
    """

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "AsyncStatusResponse")
        self.server_status = self.control_code
        assert self.message_parameter == 0
        assert self.payload_length == 0


@dataclass(frozen=True, slots=True)
class AsyncMessage:
    """A complete async frame with its decoded header and raw payload.

    Parameters
    ----------
    msg_type : str
        Recognised HiSLIP message name.
    control_code : int
        Unsigned 8-bit control field.
    message_parameter : int
        Unsigned 32-bit parameter.
    payload : bytes
        Bytes following the header, possibly empty.
    """

    msg_type: str
    control_code: int
    message_parameter: int
    payload: bytes


@dataclass
class _PendingRequest:
    expected_response: str
    response: Optional[AsyncMessage] = None
    error: Optional[Exception] = None
    done: bool = False
    abandoned: bool = False


class AsyncChannel:
    """Own the async socket, match replies, and dispatch unsolicited messages.

    Parameters
    ----------
    sock : socket.socket
        Initialised async channel; this object takes responsibility for closing it.
    event_callback : Optional[Callable[[int], None]]
        Receive service-request status bytes on a separate dispatcher thread.
    interrupt_callback : Optional[Callable[[int], None]]
        Receive interrupted message IDs on the reader thread.
    send_condition : Optional[threading.Condition]
        Shared condition serialising sends with interruption handling.
    before_send : Optional[Callable[[], None]]
        Called under ``send_condition`` before each request is sent.

    Replies are matched in FIFO order within each response type. A separate
    event dispatcher prevents user callbacks from blocking reply reception.
    Callback exceptions are logged; callers must use ``close()`` to release
    the socket and request worker shutdown.

    HiSLIP 6.13 service requests are unsolicited and unacknowledged, so they
    are dispatched independently of request/reply traffic.
    """

    def __init__(
        self,
        sock: socket.socket,
        event_callback: Optional[Callable[[int], None]] = None,
        interrupt_callback: Optional[Callable[[int], None]] = None,
        send_condition: Optional[threading.Condition] = None,
        before_send: Optional[Callable[[], None]] = None,
    ) -> None:
        self._sock = sock
        self._event_callback = event_callback
        self._interrupt_callback = interrupt_callback
        self._send_condition = send_condition or threading.Condition()
        self._before_send = before_send
        self._state_lock = threading.Condition()
        self._pending_requests: Dict[str, Deque[_PendingRequest]] = defaultdict(deque)
        self._failure: Optional[Exception] = None
        self._device_clear_in_progress = False
        self._stop = threading.Event()
        self._event_queue: queue.Queue[Optional[int]] = queue.Queue()
        self._event_thread: Optional[threading.Thread] = None
        if self._event_callback is not None:
            self._event_thread = threading.Thread(
                target=self._dispatch_events, daemon=True
            )
            self._event_thread.start()
        self._thread = threading.Thread(target=self._run, daemon=True)
        if not self._thread.is_alive():
            self._thread.start()

    def close(self) -> None:
        """Request worker shutdown, fail pending calls, and close the owned socket."""
        self._stop.set()
        self._fail_pending(lambda: RuntimeError("async channel closed"), terminal=True)
        self._event_queue.put(None)
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        current_thread = threading.current_thread()
        if self._thread.is_alive() and self._thread is not current_thread:
            self._thread.join(timeout=1.0)
        if (
            self._event_thread is not None
            and self._event_thread.is_alive()
            and self._event_thread is not current_thread
        ):
            self._event_thread.join(timeout=1.0)
        try:
            self._sock.close()
        except Exception:
            pass

    def begin_device_clear(self) -> None:
        """Suppress queued service requests during a device-clear handshake.

        Discard queued events as part of HiSLIP 6.12 client step 4, which clears
        traffic other than FatalError and the two clear acknowledgements.
        Callbacks already running are not cancelled.
        """
        with self._state_lock:
            self._device_clear_in_progress = True
        while True:
            try:
                event = self._event_queue.get_nowait()
            except queue.Empty:
                break
            if event is None:
                self._event_queue.put(None)
                break

    def end_device_clear(self) -> None:
        """Resume service-request delivery after the device-clear handshake."""
        with self._state_lock:
            self._device_clear_in_progress = False

    def request(
        self,
        msg_type: str,
        control_code: int,
        message_parameter: int,
        payload: BytesBuffer = b"",
        expected_response: Optional[str] = None,
    ) -> AsyncMessage:
        """Send an async request and wait for its matching response.

        Parameters
        ----------
        msg_type : str
            Request message name in ``MESSAGETYPE``.
        control_code : int
            Unsigned 8-bit request control field.
        message_parameter : int
            Unsigned 32-bit request parameter.
        payload : BytesBuffer
            Optional request payload.
        expected_response : Optional[str]
            Required response name; omitting it is an error.

        Returns
        -------
        AsyncMessage
            The next matching reply for this request.

        Raises
        ------
        ValueError
            ``expected_response`` was not supplied.
        socket.timeout
            The response did not arrive within the socket's current timeout.
        HiSLIPInterruptedError
            The server interrupted the pending transaction.
        RuntimeError
            The channel closed, failed, or received a fatal/protocol error.
        KeyError, struct.error
            The request name or wire fields are invalid.
        OSError
            Sending failed.

        Exceptions from ``before_send`` also propagate to the caller.
        """
        if expected_response is None:
            raise ValueError("expected_response is required for async requests")

        timeout = self._sock.gettimeout()
        pending = _PendingRequest(expected_response)

        try:
            with self._send_condition:
                if self._before_send is not None:
                    self._before_send()
                with self._state_lock:
                    if self._failure is not None:
                        raise self._failure
                    if self._stop.is_set():
                        raise RuntimeError("async channel closed")
                    self._pending_requests[expected_response].append(pending)
                try:
                    send_msg(
                        self._sock,
                        msg_type,
                        control_code,
                        message_parameter,
                        payload,
                    )
                except Exception:
                    self._fail_pending(
                        lambda: RuntimeError("async channel send failed"),
                        terminal=True,
                    )
                    raise

            deadline = None if timeout is None else time.monotonic() + float(timeout)
            with self._state_lock:
                while not pending.done:
                    if deadline is None:
                        self._state_lock.wait()
                    else:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self._state_lock.wait(remaining)

                if not pending.done:
                    pending.abandoned = True
                    raise socket.timeout("timed out")

                if pending.error is not None:
                    raise pending.error
                response = pending.response
                assert response is not None
                return response
        finally:
            with self._state_lock:
                if not pending.done:
                    pending.abandoned = True

    def _read_exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            if self._stop.is_set():
                raise OSError("async channel stopped")
            # Poll so shutdown remains observable even with no socket timeout.
            readable, _, _ = select.select([self._sock], [], [], 0.5)
            if not readable:
                continue
            chunk = self._sock.recv(size - len(data))
            if not chunk:
                raise RuntimeError("async channel closed")
            data.extend(chunk)
        return bytes(data)

    def _read_message(self) -> AsyncMessage:
        header = self._read_exact(HEADER_SIZE)
        prologue, msg_type, control_code, message_parameter, payload_length = (
            struct.unpack(HEADER_FORMAT, header)
        )

        if prologue != b"HS":
            raise RuntimeError("protocol synchronization error on async channel")

        if msg_type not in MESSAGETYPE_STR:
            raise RuntimeError("unrecognized async message type: %d" % msg_type)

        payload = self._read_exact(payload_length) if payload_length else b""
        return AsyncMessage(
            msg_type=MESSAGETYPE_STR[msg_type],
            control_code=control_code,
            message_parameter=message_parameter,
            payload=payload,
        )

    def _deliver_event(self, message: AsyncMessage) -> None:
        if (
            message.msg_type == "AsyncServiceRequest"
            and self._event_callback is not None
        ):
            self._event_queue.put(message.control_code)

    def _dispatch_events(self) -> None:
        while True:
            status_byte = self._event_queue.get()
            if status_byte is None:
                break
            # Discard queued callbacks during shutdown until the sentinel arrives.
            if self._stop.is_set():
                continue
            with self._state_lock:
                discard_event = self._device_clear_in_progress
            if discard_event:
                continue
            try:
                assert self._event_callback is not None
                self._event_callback(status_byte)
            except Exception:
                LOGGER.exception("Error handling async service request")

    def _deliver_interrupt(self, message: AsyncMessage) -> None:
        if self._interrupt_callback is None:
            return
        try:
            self._interrupt_callback(message.message_parameter)
        except Exception:
            LOGGER.exception("Error handling async interruption")

    def _complete_pending(self, message: AsyncMessage) -> bool:
        with self._state_lock:
            requests = self._pending_requests.get(message.msg_type)
            if not requests:
                return False
            # Timed-out requests still consume their FIFO slot for a late reply.
            pending = requests.popleft()
            if not pending.abandoned:
                pending.response = message
                pending.done = True
            self._state_lock.notify_all()
            return True

    def _fail_pending(
        self,
        error_factory: Callable[[], Exception],
        terminal: bool = False,
        preserve_responses: Optional[set[str]] = None,
    ) -> None:
        with self._state_lock:
            if terminal and self._failure is None:
                self._failure = error_factory()
            for response_type, requests in list(self._pending_requests.items()):
                if preserve_responses and response_type in preserve_responses:
                    continue
                for pending in requests:
                    if not pending.abandoned:
                        pending.error = error_factory()
                        pending.done = True
                del self._pending_requests[response_type]
            self._state_lock.notify_all()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                message = self._read_message()
            except socket.timeout:
                continue
            except OSError:
                if not self._stop.is_set():
                    self._fail_pending(
                        lambda: RuntimeError("async channel closed"), terminal=True
                    )
                break
            except Exception as e:
                if not self._stop.is_set():
                    LOGGER.exception(
                        f"Async channel reader stopped due to protocol error: {e}"
                    )
                    self._fail_pending(
                        lambda: RuntimeError("async channel protocol error"),
                        terminal=True,
                    )
                break

            if message.msg_type == "AsyncInterrupted":
                # HiSLIP 3.1.2 rule 4 invalidates buffered data. Pending calls are
                # aborted here, but 6.12 client step 4 preserves clear acknowledgements.
                with self._send_condition:
                    self._deliver_interrupt(message)
                    self._fail_pending(
                        lambda: HiSLIPInterruptedError(message.message_parameter),
                        preserve_responses={"AsyncDeviceClearAcknowledge"},
                    )
                continue

            if message.msg_type == "FatalError":
                error = FATALERRORMESSAGE.get(
                    message.control_code,
                    f"device-defined error {message.control_code}",
                )
                error_message = message.payload.decode(errors="replace")
                self._fail_pending(
                    lambda: RuntimeError(
                        f"HiSLIP fatal error {error}: {error_message}"
                    ),
                    terminal=True,
                )
                break

            if self._complete_pending(message):
                continue

            if message.msg_type in {"AsyncServiceRequest"}:
                with self._state_lock:
                    if not self._device_clear_in_progress:
                        self._deliver_event(message)
                continue

            LOGGER.debug("Ignoring unsolicited async message %s", message.msg_type)


class DeviceClearAcknowledge(RxHeader):
    """Read the final device-clear feature bitmap from synchronous ``sock``.

    HiSLIP 6.12.1, Table 31 requires the client to use this final feature setting.
    """

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "DeviceClearAcknowledge")
        self.feature_bitmap = self.control_code
        assert self.message_parameter == 0
        assert self.payload_length == 0


class Interrupted(RxHeader):
    """Read the interrupted message ID from the synchronous channel ``sock``.

    This is the synchronous half of HiSLIP 6.11, Table 29.
    """

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "Interrupted")
        assert self.control_code == 0
        self.message_id = self.message_parameter
        assert self.payload_length == 0


class Error(RxHeader):
    """Read a non-fatal error name and its raw payload from ``sock``.

    HiSLIP 6.3, Tables 15-16 define the frame and error codes. An unknown code
    raises ``KeyError``.
    """

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "Error")
        self.error_code = ERRORMESSAGE[self.control_code]
        assert self.message_parameter == 0
        self.error_message = receive_exact(sock, self.payload_length)


class FatalError(RxHeader):
    """Read a fatal error name and its raw payload from ``sock``.

    HiSLIP 6.2, Tables 13-14 define the frame and error codes. An unknown code
    raises ``KeyError``.
    """

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "FatalError")
        self.error_code = FATALERRORMESSAGE[self.control_code]
        assert self.message_parameter == 0
        self.error_message = receive_exact(sock, self.payload_length)


class Instrument:
    """Open and manage the two HiSLIP channels for an instrument.

    Parameters
    ----------
    ip_addr : str
        Instrument host name or IPv4 address.
    open_timeout : Optional[float]
        Connection and initialisation timeout in milliseconds for each channel.
        ``None`` or zero selects the 2000 ms transport default.
    timeout : Optional[float]
        Subsequent I/O timeout in seconds; ``None`` or zero selects five seconds.
    port : int
        Server TCP port, normally 4880.
    sub_address : str
        ASCII HiSLIP sub-address, normally ``"hislip0"``.
    event_callback : Optional[Callable[[int], None]]
        Callback receiving service-request status bytes.
    interrupt_callback : Optional[Callable[[int], None]]
        Callback receiving server-interrupted message IDs.

    Raises
    ------
    OSError
        Connecting or initialising either channel failed, including a timeout.
    RuntimeError
        An initialisation response was invalid or the connection closed.
    AssertionError
        A response contained invalid fixed fields.
    ValueError
        A timeout is invalid or the sub-address is not ASCII.

    Notes
    -----
    Initialisation follows HiSLIP 6.1, Table 12. Applying the open timeout to
    network connection setup is optional under VPP-4.3 PERMISSION 4.3.2 and
    OBSERVATION 4.3.8; the zero-timeout default follows RECOMMENDATION 4.3.3.

    Synchronous operations are serialised by ``sync_lock``. The session layer
    also holds this lock while reporting read status and recovering from an
    aborted read. Async transactions and ``terminate()`` remain independent.

    Async transaction methods propagate the transport, interruption, and protocol
    errors documented by ``AsyncChannel.request``. Response-specific validation
    may additionally raise ``AssertionError`` or ``KeyError``.

    Synchronous I/O methods propagate socket errors, including timeouts, and
    ``HiSLIPInterruptedError`` on cancellation or server interruption. Header
    validation errors propagate as documented by ``RxHeader``.
    """

    def __init__(
        self,
        ip_addr: str,
        open_timeout: Optional[float] = None,
        timeout: Optional[float] = None,
        port: int = PORT,
        sub_address: str = "hislip0",
        event_callback: Optional[Callable[[int], None]] = None,
        interrupt_callback: Optional[Callable[[int], None]] = None,
    ) -> None:
        timeout = timeout or 5.0
        self.sync_lock = threading.RLock()
        connecting = connect_timeout(open_timeout)

        # HiSLIP 6.1, Table 12: initialise sync first to obtain the async SessionID.
        raw_sync = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sync.settimeout(connecting)
        raw_sync.connect((ip_addr, port))
        raw_sync.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        self._sync: CancellableSocket = CancellableSocket(raw_sync)

        init = self.initialize(sub_address=sub_address.encode("ascii"))
        if init.overlap != 0:
            print("**** prefer overlap = %d" % init.overlap)
        # Keep the open timeout through initialisation, then apply the I/O timeout.
        self._sync.settimeout(timeout)

        self._async = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._async.settimeout(connecting)
        self._async.connect((ip_addr, port))
        self._async.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._async_init = self.async_initialize(session_id=init.session_id)
        self._async.settimeout(timeout)

        self._timeout = timeout
        self._interrupt_callback = interrupt_callback
        self._interrupt_condition = threading.Condition(threading.RLock())
        self._async_interrupted_seen = False
        self._sync_interrupted_seen = False
        self._async_channel = AsyncChannel(
            self._async,
            event_callback=event_callback,
            interrupt_callback=self._handle_async_interrupted,
            send_condition=self._interrupt_condition,
            before_send=self._wait_for_async_interrupted,
        )
        self.max_msg_size = DEFAULT_MAX_MSG_SIZE
        self.keepalive = False
        self.timeout = timeout
        self._rmt = 0
        # HiSLIP 3.1.2 and 3.2.2 prescribe this initial ID and unsigned +2 updates.
        self._message_id = 0xFFFF_FF00
        self._last_message_id: Optional[int] = None
        self._feature_bitmap = 0
        self._msg_type: str = ""
        self._payload_remaining: int = 0
        self._last_read_rmt = False
        self._last_read_termchar = False
        self._receiving = threading.Event()
        self._receive_state_lock = threading.Lock()
        self._status_query_lock = threading.Lock()

    def close(self) -> None:
        """Close both channels and request shutdown of the async workers."""
        self._async_channel.close()
        self._sync.close()

    @property
    def timeout(self) -> float:
        """float: I/O timeout in seconds for both channels."""
        return self._timeout

    @timeout.setter
    def timeout(self, val: float) -> None:
        """Set both channel timeouts to ``val`` seconds.

        Invalid values raise ``ValueError``; socket errors propagate as ``OSError``.
        """
        self._timeout = val
        self._sync.settimeout(self._timeout)
        self._async.settimeout(self._timeout)

    @property
    def max_msg_size(self) -> int:
        """int: Maximum synchronous frame size the server accepts, including header."""
        return self._max_msg_size

    @max_msg_size.setter
    def max_msg_size(self, size: int) -> None:
        """Advertise a receive limit of ``size`` bytes and store the server's limit.

        HiSLIP 6.10, Table 28 negotiates the two directions independently. The
        stored server receive limit bounds outgoing synchronous frames.
        Propagates errors from ``async_maximum_message_size()``.
        """
        self._max_msg_size = self.async_maximum_message_size(size)

    @property
    def last_message_id(self) -> Optional[int]:
        """Optional[int]: Last sent Data, DataEnd, or Trigger ID; initially ``None``."""
        return self._last_message_id

    @last_message_id.setter
    def last_message_id(self, message_id: Optional[int]) -> None:
        """Store ``message_id`` and reset read state, including RMT and END flags.

        HiSLIP 3.1.2 rule 3 clears buffered responses on Data/DataEnd/Trigger sends;
        HiSLIP 6.4 defines the RMT-delivered flag reported by the next such message.
        """
        self._last_message_id = message_id
        self._rmt = 0
        self._payload_remaining = 0
        self._msg_type = ""
        self._last_read_rmt = False
        self._last_read_termchar = False

    def _message_id_for_async_request(self) -> int:
        """Use the last sent ID, or the predecessor of the next ID before any send.

        HiSLIP 6.5, 6.7, and 6.14 require 0xfffffefe after initialisation/clear.
        """
        message_id = self.last_message_id
        if message_id is not None:
            return message_id
        return (self._message_id - 2) & 0xFFFF_FFFF

    def _handle_async_interrupted(self, message_id: int) -> None:
        with self._interrupt_condition:
            self._async_interrupted_seen = True
            self._interrupt_condition.notify_all()

        if self._interrupt_callback is not None:
            self._interrupt_callback(message_id)

    def _mark_sync_interrupted(self) -> None:
        with self._interrupt_condition:
            self._sync_interrupted_seen = True
            if self._async_interrupted_seen:
                self._sync_interrupted_seen = False
                self._async_interrupted_seen = False
            self._interrupt_condition.notify_all()

    def _wait_for_async_interrupted(self) -> None:
        """Gate sends after a sync-first interruption until both channels agree.

        HiSLIP 3.1.2 rule 4 prohibits sends until AsyncInterrupted arrives.
        """
        timeout = getattr(self, "_timeout", None)
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._interrupt_condition:
            while self._sync_interrupted_seen and not self._async_interrupted_seen:
                if deadline is None:
                    self._interrupt_condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout("timed out waiting for AsyncInterrupted")
                self._interrupt_condition.wait(remaining)

            if self._sync_interrupted_seen and self._async_interrupted_seen:
                self._sync_interrupted_seen = False
                self._async_interrupted_seen = False
                self._interrupt_condition.notify_all()

    @property
    def feature_bitmap(self) -> int:
        """int: Final device-clear features (HiSLIP 6.12.1, Table 31), initially zero."""
        return self._feature_bitmap

    @property
    def keepalive(self) -> bool:
        """bool: Whether TCP keepalive probes are enabled on both channels.

        Corresponds to VPP-4.3 5.1.2 ``VI_ATTR_TCPIP_KEEPALIVE``.
        """
        return self._keepalive

    @keepalive.setter
    def keepalive(self, keepalive: bool) -> None:
        """Enable or disable TCP keepalive on both channels; propagate ``OSError``."""
        self._keepalive = bool(keepalive)
        self._sync.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, bool(keepalive))
        self._async.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, bool(keepalive))

    @property
    def nodelay(self) -> bool:
        """bool: Whether Nagle's algorithm is disabled on both channels.

        Corresponds to VPP-4.3 5.1.2 ``VI_ATTR_TCPIP_NODELAY``.
        """
        return bool(self._sync.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY))

    @nodelay.setter
    def nodelay(self, nodelay: bool) -> None:
        """Set TCP_NODELAY on both channels; propagate socket errors as ``OSError``."""
        self._sync.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, bool(nodelay))
        self._async.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, bool(nodelay))

    def send(self, data: BytesBuffer, send_end: bool = True) -> int:
        """Send data in frames bounded by the negotiated message size.

        Parameters
        ----------
        data : BytesBuffer
            Bytes to send on the synchronous channel.
        send_end : bool
            Use DataEnd for the final frame when true; otherwise use Data.

        Returns
        -------
        int
            Number of bytes sent. Empty input returns zero without sending a frame.

        Raises
        ------
        OSError, HiSLIPInterruptedError
            Sending or discarding a pending response failed.
        RuntimeError
            The peer closed the connection while a pending payload was discarded.

        HiSLIP 6.4, Table 17 defines Data/DataEnd and the final-byte END indication.
        HiSLIP 6.10 limits complete synchronous frames, including the 2.3 header.
        """
        with self.sync_lock:
            data_view = memoryview(data)
            num_bytes_to_send = len(data)
            max_payload_size = self._max_msg_size - HEADER_SIZE

            while num_bytes_to_send > 0:
                if num_bytes_to_send <= max_payload_size:
                    assert len(data_view) == num_bytes_to_send
                    if send_end:
                        self._send_data_end_packet(data_view)
                    else:
                        self._send_data_packet(data_view)
                    bytes_sent = num_bytes_to_send
                else:
                    self._send_data_packet(data_view[:max_payload_size])
                    bytes_sent = max_payload_size

                data_view = data_view[bytes_sent:]
                num_bytes_to_send -= bytes_sent

            return len(data)

    def receive(
        self,
        max_len: int = 4096,
        termination_char: Optional[int] = None,
        suppress_end: bool = False,
    ) -> bytes:
        """Receive data until a count, termination character, or END condition.

        Parameters
        ----------
        max_len : int
            Maximum bytes to return; zero returns an empty result.
        termination_char : Optional[int]
            Byte value ending the read, included in the result; ``None`` disables it.
        suppress_end : bool
            Continue past DataEnd messages when true.

        Returns
        -------
        bytes
            Data received before the first enabled stop condition.

        Raises
        ------
        HiSLIPInterruptedError
            A local cancellation or server interruption aborted the read.
        OSError
            Receiving failed, including a socket timeout.
        RuntimeError
            The peer closed the connection or a header was invalid.
        AssertionError
            A Data/DataEnd header contained an invalid control code.
        MemoryError
            ``max_len`` is negative or a read exceeded the requested count.
        ValueError
            ``termination_char`` is outside the byte range.

        The stop conditions correspond to VPP-4.3 OBSERVATION 6.1.1 and
        RULES 6.1.1-6.1.5. OBSERVATION 6.1.3 notes that count/termchar completion
        remains possible with END suppressed; the session layer selects the VISA
        status code when multiple conditions coincide. HiSLIP 6.4, Table 18
        associates END with the final DataEnd payload byte.
        """
        with self.sync_lock:
            with self._receive_state_lock:
                self._receiving.set()
            try:
                recv_buffer = bytearray()

                term_byte = (
                    bytes((termination_char,)) if termination_char is not None else None
                )
                self._last_read_rmt = False
                self._last_read_termchar = False

                if max_len <= 0:
                    if max_len < 0:
                        raise MemoryError("scribbled past end of recv_buffer")
                    return b""

                while True:
                    # HiSLIP 3.1.2 rule 4 invalidates buffered data on interruption;
                    # make the return decision atomic with async notification.
                    with self._interrupt_condition:
                        interrupted = (
                            self._async_interrupted_seen
                            and not self._sync_interrupted_seen
                        )
                        if not interrupted:
                            if (
                                self._payload_remaining == 0
                                and self._msg_type == "DataEnd"
                            ):
                                self._rmt = 1
                                self._last_read_rmt = True
                                self._msg_type = ""
                            if (
                                len(recv_buffer) >= max_len
                                or self._last_read_termchar
                                or (self._last_read_rmt and not suppress_end)
                            ):
                                if len(recv_buffer) > max_len:
                                    raise MemoryError(
                                        "scribbled past end of recv_buffer"
                                    )
                                return bytes(recv_buffer)

                    if interrupted:
                        recv_buffer.clear()
                        self._rmt = 0
                        self._last_read_rmt = False
                        self._last_read_termchar = False
                        self._discard_pending_sync_payload()

                    if self._payload_remaining <= 0:
                        self._msg_type, self._payload_remaining = (
                            self._next_data_header()
                        )
                        if self._payload_remaining == 0:
                            continue

                    start = len(recv_buffer)
                    request_size = min(self._payload_remaining, max_len - start)
                    recv_buffer.extend(b"\x00" * request_size)

                    if term_byte is None:
                        received = self._sync.recv_into(
                            memoryview(recv_buffer)[start : start + request_size],
                            request_size,
                        )
                        if received == 0:
                            raise RuntimeError("Connection was dropped by server.")
                    else:
                        # Leave bytes after the terminator available to the next read.
                        peeked = self._sync.recv_into(
                            memoryview(recv_buffer)[start : start + request_size],
                            request_size,
                            socket.MSG_PEEK,
                        )
                        if peeked == 0:
                            raise RuntimeError("Connection was dropped by server.")
                        term_index = recv_buffer.find(term_byte, start, start + peeked)
                        received = term_index - start + 1 if term_index >= 0 else peeked
                        receive_exact_into(
                            self._sync,
                            memoryview(recv_buffer)[start : start + received],
                        )
                        self._last_read_termchar = term_index >= 0

                    # Retain frame progress across read boundaries and timeouts.
                    self._payload_remaining -= received
                    del recv_buffer[start + received :]

            finally:
                with self._receive_state_lock:
                    self._receiving.clear()

    def _next_data_header(self) -> Tuple[str, int]:
        """Return the next matching Data/DataEnd type and payload length."""
        while True:
            header = RxHeader(self._sync)

            if header.msg_type in ("Data", "DataEnd"):
                with self._interrupt_condition:
                    discard_until_interrupted = (
                        self._async_interrupted_seen and not self._sync_interrupted_seen
                    )

                if discard_until_interrupted:
                    # HiSLIP 3.1.2 rule 4: async-first interruption discards data
                    # until the synchronous Interrupted marker is encountered.
                    self._msg_type = header.msg_type
                    self._payload_remaining = header.payload_length
                    self._discard_pending_sync_payload()
                    continue

                # HiSLIP 3.1.2 rules 1-2 require the latest sent ID; only Data may
                # use the wildcard. This check also accepts wildcard DataEnd IDs,
                # an existing leniency rather than a specification permission.
                if (
                    header.message_parameter == 0xFFFF_FFFF
                    or header.message_parameter == self.last_message_id
                ):
                    break

            if header.msg_type == "Interrupted":
                # HiSLIP 3.1.2 rule 4: sync-first interruption clears buffered data
                # and gates sends until AsyncInterrupted arrives.
                self._msg_type = ""
                self._rmt = 0
                self._last_read_rmt = False
                self._last_read_termchar = False
                self._mark_sync_interrupted()
                raise HiSLIPInterruptedError(header.message_parameter)

            # Discard stale or unexpected frames without losing the next boundary.
            receive_flush(self._sync, header.payload_length)

        return header.msg_type, header.payload_length

    def device_clear(self) -> None:
        """Complete a two-channel device clear and reset message/read state.

        HiSLIP 6.12 client steps 1-9 specify the handshake; step 8 resets MessageID.
        Propagates errors from ``async_device_clear()`` and
        ``device_clear_complete()``.
        """
        with self.sync_lock:
            self._perform_device_clear()
            self._message_id = 0xFFFF_FF00
            self.last_message_id = None

    def _perform_device_clear(self) -> None:
        self._async_channel.begin_device_clear()
        try:
            feature = self.async_device_clear()
            self._feature_bitmap = self.device_clear_complete(feature)
        finally:
            self._async_channel.end_device_clear()

    def terminate(self) -> None:
        """Cancel an active synchronous receive from another thread.

        The wakeup socket makes a pending ``recv_into()`` raise
        ``HiSLIPInterruptedError``, translated to ``VI_ERROR_ABORT`` by the
        session layer. An idle session is left unchanged.

        VPP-4.3 3.5.1.1 recommends aborting active calls for a VI_NULL job ID.
        Local cancellation is distinct from the server's HiSLIP 6.11 interruption.

        Call ``complete_terminate()`` after the read thread has exited and before
        reusing the session. This method deliberately does not take ``sync_lock``,
        which the blocked read holds.

        Raises
        ------
        OSError
            Signalling the wakeup socket failed.
        """
        with self._receive_state_lock:
            if self._receiving.is_set():
                self._sync.cancel()

    def complete_terminate(self) -> None:
        """Reset protocol state after a cancelled read has exited.

        Discard cancellation signals and buffered frame fragments, perform the
        HiSLIP device-clear handshake, and reset message/read state before reuse.
        Transport and protocol errors propagate from the recovery operations.

        HiSLIP 6.12 specifies the clear handshake. The fragment drain and bounded
        Interrupted wait are local cancellation-recovery choices, not extra
        requirements of that transaction.
        """
        with self.sync_lock:
            self._sync.drain_cancel()

            # Recovery reads must not be interrupted by another cancellation signal.
            self._sync._cancel_enabled = False
            try:
                # A cancelled read can leave fragments that are not header-aligned.
                self._sync.setblocking(False)
                try:
                    while True:
                        try:
                            chunk = self._sync.recv(65536)
                            if not chunk:
                                break
                        except BlockingIOError:
                            break
                finally:
                    self._sync.setblocking(True)
                    self._sync.settimeout(self._timeout)
                self._payload_remaining = 0
                self._msg_type = ""

                self._async_channel.begin_device_clear()
                try:
                    feature = self.async_device_clear()
                    # Local recovery wait: HiSLIP 6.12 does not require the server
                    # to send Interrupted during a clear.
                    saved_timeout = self._sync.gettimeout()
                    self._sync.settimeout(2.0)
                    try:
                        while True:
                            header = RxHeader(self._sync)
                            if header.msg_type == "Interrupted":
                                self._mark_sync_interrupted()
                                break
                            if header.payload_length > 0:
                                receive_flush(self._sync, header.payload_length)
                    except socket.timeout:
                        # Continue the clear handshake if Interrupted never arrives.
                        pass
                    finally:
                        self._sync.settimeout(saved_timeout)

                    self._feature_bitmap = self.device_clear_complete(feature)
                finally:
                    self._async_channel.end_device_clear()
            finally:
                self._sync._cancel_enabled = True

            self._message_id = 0xFFFF_FF00
            self.last_message_id = None

    def initialize(
        self,
        version: tuple = (1, 0),
        vendor_id: bytes = b"xx",
        sub_address: bytes = b"hislip0",
    ) -> InitializeResponse:
        """Initialise the synchronous channel and return the server's response.

        Parameters
        ----------
        version : tuple
            Protocol major and minor version bytes.
        vendor_id : bytes
            Two-byte client vendor identifier.
        sub_address : bytes
            Encoded HiSLIP sub-address sent as the payload.

        Returns
        -------
        InitializeResponse
            Server version, overlap preference, and session ID.

        Raises
        ------
        struct.error
            A version component does not fit its wire field.
        OSError, RuntimeError, AssertionError
            Sending or validating the response failed.

        HiSLIP 6.1, Table 12 steps 2-3 define this synchronous exchange and the
        session ID subsequently used to attach the async channel.
        """
        major, minor = version
        header = struct.pack(
            "!2sBBBB2sQ",
            b"HS",
            MESSAGETYPE["Initialize"],
            0,
            major,
            minor,
            vendor_id,
            len(sub_address),
        )
        self._sync.sendall(header + sub_address)
        return InitializeResponse(self._sync)

    def async_initialize(self, session_id: int) -> AsyncInitializeResponse:
        """Attach the async channel to the synchronous channel's session.

        Parameters
        ----------
        session_id : int
            Session identifier returned by ``initialize()``.

        Returns
        -------
        AsyncInitializeResponse
            Decoded reply; see ``AsyncInitializeResponse`` for its layout limitation.

        Raises
        ------
        struct.error
            ``session_id`` does not fit the message parameter.
        OSError, RuntimeError, AssertionError
            Sending or validating the response failed.

        HiSLIP 6.1, Table 12 steps 5-6 bind this channel to the synchronous session.
        """
        send_msg(self._async, "AsyncInitialize", 0, session_id)
        return AsyncInitializeResponse(self._async)

    def async_maximum_message_size(self, size: int) -> int:
        """Negotiate the maximum HiSLIP message size through AsyncMaxMsgSize.

        Parameters
        ----------
        size : int
            Maximum synchronous frame size this client accepts, including the header.

        Returns
        -------
        int
            Maximum synchronous frame size the server accepts, including the header.

        Raises
        ------
        struct.error
            ``size`` does not fit an unsigned 64-bit field.
        AssertionError
            The response payload is not eight bytes long.

        HiSLIP 6.10, Table 28 defines these independent receive limits. HiSLIP 2.7
        recommends negotiating shortly after initialisation to avoid buffer overflow.
        Other async transaction errors propagate from ``AsyncChannel.request``.
        """
        payload = struct.pack("!Q", size)
        response = self._async_channel.request(
            "AsyncMaxMsgSize",
            0,
            0,
            payload,
            expected_response="AsyncMaxMsgSizeResponse",
        )
        assert len(response.payload) == 8
        return struct.unpack("!Q", response.payload)[0]

    def async_lock_info(self) -> int:
        """Return the server's exclusive-lock flag through AsyncLockInfo.

        Returns
        -------
        int
            Zero if no exclusive lock is held, otherwise one.

        HiSLIP 6.6, Table 23 also returns a count of all lock-holding clients in
        the response parameter; that count is not exposed by this method.
        Async transaction errors propagate from ``AsyncChannel.request``.
        """
        response = self._async_channel.request(
            "AsyncLockInfo",
            0,
            0,
            expected_response="AsyncLockInfoResponse",
        )
        return response.control_code

    def async_lock_request(self, timeout_ms: int, lock_string: str = "") -> str:
        """Request a lock and return the server's named result.

        Parameters
        ----------
        timeout_ms : int
            Server-side lock wait in milliseconds. Reply waiting still uses the
            channel's I/O timeout.
        lock_string : str
            Shared-lock identifier; an empty string requests an exclusive lock
            (HiSLIP 6.5, Tables 19 and 21). The protocol specifies ASCII, but this
            method uses UTF-8 encoding without validating that restriction.

        Returns
        -------
        str
            Result from ``LOCKRESPONSE``: failure, success, success shared, or error.

        Raises
        ------
        KeyError
            The server returned an unknown lock result.
        struct.error
            ``timeout_ms`` does not fit the message parameter.

        HiSLIP 6.5, Table 19 defines the request and Table 21 the result codes.
        Other async transaction errors propagate from ``AsyncChannel.request``.
        """
        ctrl_code = LOCKCONTROLCODE["request"]
        response = self._async_channel.request(
            "AsyncLock",
            ctrl_code,
            timeout_ms,
            lock_string.encode(),
            expected_response="AsyncLockResponse",
        )
        return LOCKRESPONSE[response.control_code]

    def async_lock_release(self) -> str:
        """Release a lock and return the server's result from ``LOCKRESPONSE``.

        Returns
        -------
        str
            Named lock result; an unknown result code raises ``KeyError``.

        HiSLIP 6.5, Tables 20-21 distinguish exclusive and shared release results.
        Async transaction errors propagate from ``AsyncChannel.request``.
        """
        ctrl_code = LOCKCONTROLCODE["release"]
        response = self._async_channel.request(
            "AsyncLock",
            ctrl_code,
            self._message_id_for_async_request(),
            expected_response="AsyncLockResponse",
        )
        return LOCKRESPONSE[response.control_code]

    def async_remote_local_control(self, remotelocalcontrol: str) -> None:
        """Apply a remote/local-control operation and wait for acknowledgement.

        Parameters
        ----------
        remotelocalcontrol : str
            Operation name in ``REMOTELOCALCONTROLCODE``.

        Raises
        ------
        KeyError
            The operation name is unknown.

        HiSLIP 6.7, Table 24 defines the exchange; Table 25 defines the controls.
        The server may acknowledge before acting if another client holds a lock.
        Async transaction errors propagate from ``AsyncChannel.request``.
        """
        ctrl_code = REMOTELOCALCONTROLCODE[remotelocalcontrol]
        self._async_channel.request(
            "AsyncRemoteLocalControl",
            ctrl_code,
            self._message_id_for_async_request(),
            expected_response="AsyncRemoteLocalResponse",
        )

    def async_status_query(self) -> int:
        """Query the server status byte, reporting and then clearing the RMT flag.

        Returns
        -------
        int
            Status byte from AsyncStatusResponse.

        The RMT flag is reset only after a successful response. Async transaction
        errors propagate from ``AsyncChannel.request``.

        HiSLIP 6.14, Table 33 reports RMT-delivered once. The query uses the
        synchronised-mode last-sent MessageID; overlapped mode instead needs the
        last fully delivered response ID (HiSLIP 3.2.2 rule 1).
        """
        # HiSLIP 6.14 reports RMT only once; serialise the report and reset.
        with self._status_query_lock:
            response = self._async_channel.request(
                "AsyncStatusQuery",
                self._rmt,
                self._message_id_for_async_request(),
                expected_response="AsyncStatusResponse",
            )
            self._rmt = 0
            return response.control_code

    def async_device_clear(self) -> int:
        """Start the async phase of a device-clear handshake.

        Returns
        -------
        int
            Server's preferred features from AsyncDeviceClearAcknowledge.

        HiSLIP 6.12 client steps 2 and 5 require this acknowledgement before the
        caller finishes with ``device_clear_complete()``. Async transaction
        errors propagate from ``AsyncChannel.request``.
        """
        response = self._async_channel.request(
            "AsyncDeviceClear",
            0,
            0,
            expected_response="AsyncDeviceClearAcknowledge",
        )
        return response.control_code

    def device_clear_complete(self, feature_bitmap: int) -> int:
        """Finish a device clear on the synchronous channel.

        Parameters
        ----------
        feature_bitmap : int
            Client feature request, normally the server preference from the
            async phase.

        Returns
        -------
        int
            Feature bitmap from DeviceClearAcknowledge.

        Raises
        ------
        OSError, HiSLIPInterruptedError
            Sending or receiving during recovery failed.
        RuntimeError
            The peer closed, sent a fatal error, or supplied an invalid response.

        HiSLIP 6.12 client steps 6-7 order completion before acknowledgement;
        6.12.1 defines feature negotiation. This implementation also accepts an
        acknowledgement queued before DeviceClearComplete, a compatibility
        leniency rather than the specified ordering.
        """
        with self.sync_lock:
            final_feature_bitmap = self._receive_device_clear_ack(wait=False)
            self._send_sync_message("DeviceClearComplete", feature_bitmap, 0)
            if final_feature_bitmap is not None:
                return final_feature_bitmap
            final_feature_bitmap = self._receive_device_clear_ack(wait=True)
            assert final_feature_bitmap is not None
            return final_feature_bitmap

    def _discard_pending_sync_payload(self) -> None:
        """Discard the current payload, retaining progress if a socket read fails.

        Used for buffered-response clearing in HiSLIP 3.1.2 rule 3 and 6.12
        client step 4, while preserving the next header boundary.
        """
        if self._payload_remaining > 0:
            buffer = bytearray(min(1 << 20, self._payload_remaining))
            while self._payload_remaining > 0:
                received = self._sync.recv_into(
                    buffer, min(len(buffer), self._payload_remaining)
                )
                if received == 0:
                    raise RuntimeError("Connection was dropped by server.")
                self._payload_remaining -= received
        self._msg_type = ""

    def _receive_device_clear_ack(self, wait: bool) -> Optional[int]:
        self._discard_pending_sync_payload()
        while True:
            if not wait and not select.select([self._sync], [], [], 0)[0]:
                return None

            response = RxHeader(self._sync)
            if response.msg_type == "DeviceClearAcknowledge":
                if response.message_parameter != 0 or response.payload_length != 0:
                    raise RuntimeError("invalid DeviceClearAcknowledge message")
                return response.control_code

            if response.msg_type == "FatalError":
                error_message = receive_exact(self._sync, response.payload_length)
                error = FATALERRORMESSAGE.get(
                    response.control_code,
                    f"device-defined error {response.control_code}",
                )
                raise RuntimeError(f"HiSLIP fatal error {error}: {error_message!r}")

            if response.payload_length > 0:
                receive_flush(self._sync, response.payload_length)

    def trigger(self) -> None:
        """Send a synchronous Trigger with the current RMT flag and advance its ID.

        HiSLIP 6.8, Table 26 defines Trigger. HiSLIP 3.1.2 and 3.2.2 prescribe
        advancing MessageID by two with unsigned 32-bit wrap-around.
        Discards any unread payload first. Transport, interruption, and protocol
        errors propagate from the synchronous I/O helpers.
        """
        with self.sync_lock:
            self._discard_pending_sync_payload()
            self._send_sync_message("Trigger", self._rmt, self._message_id)
            self.last_message_id = self._message_id
            self._message_id = (self._message_id + 2) & 0xFFFF_FFFF

    def _send_sync_message(
        self,
        msg_type: str,
        control_code: int,
        message_parameter: int,
        payload: BytesBuffer = b"",
    ) -> None:
        """Serialise writes with the HiSLIP 3.1.2 rule 4 interruption send gate."""
        with self.sync_lock, self._interrupt_condition:
            self._wait_for_async_interrupted()
            send_msg(self._sync, msg_type, control_code, message_parameter, payload)

    def _send_data_packet(self, payload: BytesBuffer) -> None:
        """Send a synchronous Data frame without END and advance the message ID."""
        with self.sync_lock:
            self._discard_pending_sync_payload()
            self._send_sync_message("Data", self._rmt, self._message_id, payload)
            self.last_message_id = self._message_id
            self._message_id = (self._message_id + 2) & 0xFFFF_FFFF

    def _send_data_end_packet(self, payload: BytesBuffer) -> None:
        """Send a synchronous DataEnd frame and advance the message ID."""
        with self.sync_lock:
            self._discard_pending_sync_payload()
            self._send_sync_message("DataEnd", self._rmt, self._message_id, payload)
            self.last_message_id = self._message_id
            self._message_id = (self._message_id + 2) & 0xFFFF_FFFF

    def fatal_error(self, error: str, error_message: str = "") -> None:
        """Send a FatalError frame; the caller remains responsible for closing.

        Parameters
        ----------
        error : str
            Error name in ``FATALERRORCODE``.
        error_message : str
            UTF-8 description; an empty value uses ``error`` itself.

        Raises
        ------
        KeyError
            The error name is unknown.

        HiSLIP 6.2, Table 13 requires FatalError on both channels followed by
        closure; this helper sends only on sync and does not complete recovery.
        The specification requires ASCII diagnostics; UTF-8 encoding here does
        not validate that restriction.
        Synchronous transport and interruption errors propagate to the caller.
        """
        err_msg = (error_message or error).encode()
        self._send_sync_message("FatalError", FATALERRORCODE[error], 0, err_msg)

    def error(self, error: str, error_message: str = "") -> None:
        """Send a non-fatal Error frame on the synchronous channel.

        Parameters
        ----------
        error : str
            Error name in ``ERRORCODE``.
        error_message : str
            UTF-8 description; an empty value uses ``error`` itself.

        Raises
        ------
        KeyError
            The error name is unknown.

        HiSLIP 6.3, Table 15 requires a reply on the offending message's channel;
        this helper addresses only sync. ASCII diagnostics are required by the
        specification, but UTF-8 encoding here does not validate that restriction.
        Synchronous transport and interruption errors propagate to the caller.
        """
        err_msg = (error_message or error).encode()
        self._send_sync_message("Error", ERRORCODE[error], 0, err_msg)
