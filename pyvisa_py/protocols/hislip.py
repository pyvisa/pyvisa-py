"""Implement HiSLIP instrument communication over two TCP channels.

Message names and field layouts refer to IVI-6.1 HiSLIP 2.0 (2020-04-23):
https://www.ivifoundation.org/downloads/Protocol%20Specifications/IVI-6.1_HiSLIP-2.0-2020-04-23.pdf

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

PORT = 4880

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

LOCKCONTROLCODE: Dict[str, int] = {
    "release": 0,
    "request": 1,
}

LOCKRESPONSE: Dict[int, str] = {
    0: "failure",
    1: "success",  # Also denotes exclusive-lock success.
    2: "success shared",
    3: "error",
}

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
# Network byte order: "HS", type (8 bits), control (8 bits), parameter (32 bits),
# and payload length (64 bits), as defined by the HiSLIP message header.
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

DEFAULT_MAX_MSG_SIZE = 1 << 20  # Includes the header when limiting payload size.


class HiSLIPInterruptedError(Exception):
    """Raised when I/O is cancelled locally or interrupted by a HiSLIP server.

    Parameters
    ----------
    message_id : int
        Interrupted message ID, or zero for local cancellation.

    The session layer translates this exception to VISA ``VI_ERROR_ABORT``.
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
            # TODO: Send FatalError and close both channels before raising.
            raise RuntimeError("protocol synchronization error")

        if msg_type not in MESSAGETYPE_STR:
            # TODO: Send Error and discard the unrecognised frame's payload.
            raise RuntimeError("unrecognized message type: %d" % msg_type)

        self.msg_type = MESSAGETYPE_STR[msg_type]

        if expected_message_type is not None and self.msg_type != expected_message_type:
            # TODO: Report an unidentified Error before discarding this payload.
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
    """Read the server's overlap preference, encoded version, and session ID.

    ``sock`` is the synchronous channel awaiting an InitializeResponse.
    """

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "InitializeResponse")
        assert self.payload_length == 0
        self.overlap = bool(self.control_code)
        self.version, self.session_id = struct.unpack("!4xHH8x", self.header)


class AsyncInitializeResponse(RxHeader):
    """Read server capability and vendor bytes from ``sock``.

    HiSLIP 2.0 splits the message parameter into two 16-bit fields. Capability
    bit zero advertises secure connections, which this client does not implement.
    """

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "AsyncInitializeResponse")
        assert self.control_code == 0
        assert self.payload_length == 0
        message_param = struct.unpack("!4x2s2s8x", self.header)
        self.server_capabilities = message_param[0]
        self.vendor_id = message_param[1]


class AsyncMaxMsgSizeResponse(RxHeader):
    """Read the server's maximum message size from ``sock``, consuming its payload."""

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "AsyncMaxMsgSizeResponse")
        assert self.control_code == 0
        assert self.message_parameter == 0
        assert self.payload_length == 8
        payload = receive_exact(sock, self.payload_length)
        self.max_msg_size = struct.unpack("!Q", payload)[0]


class AsyncDeviceClearAcknowledge(RxHeader):
    """Read the async device-clear feature bitmap from ``sock``."""

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "AsyncDeviceClearAcknowledge")
        self.feature_bitmap = self.control_code
        assert self.message_parameter == 0
        assert self.payload_length == 0


class AsyncInterrupted(RxHeader):
    """Read the interrupted message ID from the async channel ``sock``."""

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "AsyncInterrupted")
        assert self.control_code == 0
        self.message_id = self.message_parameter
        assert self.payload_length == 0


class AsyncLockInfoResponse(RxHeader):
    """Read exclusive-lock status and the number of lock-holding clients from ``sock``."""

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "AsyncLockInfoResponse")
        self.exclusive_lock = self.control_code
        self.clients_holding_locks = self.message_parameter
        assert self.payload_length == 0


class AsyncLockResponse(RxHeader):
    """Read a lock result from ``sock`` and translate it through ``LOCKRESPONSE``.

    An unknown result code raises ``KeyError``.
    """

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "AsyncLockResponse")
        self.lock_response = LOCKRESPONSE[self.control_code]
        assert self.message_parameter == 0
        assert self.payload_length == 0


class AsyncRemoteLocalResponse(RxHeader):
    """Read and validate a remote/local-control acknowledgement from ``sock``."""

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "AsyncRemoteLocalResponse")
        assert self.control_code == 0
        assert self.message_parameter == 0
        assert self.payload_length == 0


class AsyncServiceRequest(RxHeader):
    """Read an unsolicited service request's status byte from ``sock``."""

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "AsyncServiceRequest")
        self.server_status = self.control_code
        assert self.message_parameter == 0
        assert self.payload_length == 0


class AsyncStatusResponse(RxHeader):
    """Read the server's status byte from an AsyncStatusResponse on ``sock``."""

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

        Events already queued are discarded; callbacks already running are not
        cancelled.
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
                # Preserve the clear acknowledgement needed to finish HiSLIP's
                # two-channel recovery handshake; other pending calls are aborted.
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
    """Read the synchronous device-clear feature bitmap from ``sock``."""

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "DeviceClearAcknowledge")
        self.feature_bitmap = self.control_code
        assert self.message_parameter == 0
        assert self.payload_length == 0


class Interrupted(RxHeader):
    """Read the interrupted message ID from the synchronous channel ``sock``."""

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "Interrupted")
        assert self.control_code == 0
        self.message_id = self.message_parameter
        assert self.payload_length == 0


class Error(RxHeader):
    """Read a non-fatal error name and its raw payload from ``sock``.

    An unknown error code raises ``KeyError``.
    """

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "Error")
        self.error_code = ERRORMESSAGE[self.control_code]
        assert self.message_parameter == 0
        self.error_message = receive_exact(sock, self.payload_length)


class FatalError(RxHeader):
    """Read a fatal error name and its raw payload from ``sock``.

    An unknown error code raises ``KeyError``.
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
        ``None`` or zero selects the transport default.
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

        # HiSLIP requires sync initialisation before the async channel can attach.
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
        """int: Negotiated maximum message size in bytes, including the header."""
        return self._max_msg_size

    @max_msg_size.setter
    def max_msg_size(self, size: int) -> None:
        """Negotiate ``size`` bytes and store the server's returned limit.

        Propagates errors from ``async_maximum_message_size()``.
        """
        self._max_msg_size = self.async_maximum_message_size(size)

    @property
    def last_message_id(self) -> Optional[int]:
        """Optional[int]: Last sent Data, DataEnd, or Trigger ID; initially ``None``."""
        return self._last_message_id

    @last_message_id.setter
    def last_message_id(self, message_id: Optional[int]) -> None:
        """Store ``message_id`` and reset read state, including RMT and END flags."""
        self._last_message_id = message_id
        self._rmt = 0
        self._payload_remaining = 0
        self._msg_type = ""
        self._last_read_rmt = False
        self._last_read_termchar = False

    def _message_id_for_async_request(self) -> int:
        """Use the last sent ID, or the predecessor of the next ID before any send."""
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
        """Gate sends after a sync-first interruption until both channels agree."""
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
        """int: Features from the latest device-clear handshake, initially zero."""
        return self._feature_bitmap

    @property
    def keepalive(self) -> bool:
        """bool: Whether TCP keepalive probes are enabled on both channels."""
        return self._keepalive

    @keepalive.setter
    def keepalive(self, keepalive: bool) -> None:
        """Enable or disable TCP keepalive on both channels; propagate ``OSError``."""
        self._keepalive = bool(keepalive)
        self._sync.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, bool(keepalive))
        self._async.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, bool(keepalive))

    @property
    def nodelay(self) -> bool:
        """bool: Whether Nagle's algorithm is disabled on both channels."""
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
                    # Make return decisions atomically with async interruption
                    # detection so buffered data cannot escape after an abort.
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
                    self._msg_type = header.msg_type
                    self._payload_remaining = header.payload_length
                    self._discard_pending_sync_payload()
                    continue

                # HiSLIP permits the wildcard ID; other IDs must match the latest
                # Data, DataEnd, or Trigger sent by this client.
                if (
                    header.message_parameter == 0xFFFF_FFFF
                    or header.message_parameter == self.last_message_id
                ):
                    break

            if header.msg_type == "Interrupted":
                # Sync-first interruptions gate sends until the async
                # notification arrives; no buffered response may be delivered.
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
                    # Discard sync traffic until the interruption marker arrives.
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
            Server capability and vendor fields.

        Raises
        ------
        struct.error
            ``session_id`` does not fit the message parameter.
        OSError, RuntimeError, AssertionError
            Sending or validating the response failed.
        """
        send_msg(self._async, "AsyncInitialize", 0, session_id)
        return AsyncInitializeResponse(self._async)

    def async_maximum_message_size(self, size: int) -> int:
        """Negotiate the maximum HiSLIP message size through AsyncMaxMsgSize.

        Parameters
        ----------
        size : int
            Requested limit in bytes, including the header.

        Returns
        -------
        int
            Server's returned message-size limit.

        Raises
        ------
        struct.error
            ``size`` does not fit an unsigned 64-bit field.
        AssertionError
            The response payload is not eight bytes long.

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

        The shared-lock client count in the response parameter is not exposed.
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
            Lock string encoded as UTF-8 in the request payload.

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
        """
        # Serialise the RMT report and reset so concurrent queries cannot reuse it.
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
            Feature bitmap from AsyncDeviceClearAcknowledge.

        The caller must finish with ``device_clear_complete()``. Async transaction
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
            Feature flags returned by the async phase.

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

        An acknowledgement already queued before DeviceClearComplete is accepted
        as well as one received afterwards.
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
        """Discard the current payload, retaining progress if a socket read fails."""
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

        Synchronous transport and interruption errors propagate to the caller.
        """
        err_msg = (error_message or error).encode()
        self._send_sync_message("Error", ERRORCODE[error], 0, err_msg)
