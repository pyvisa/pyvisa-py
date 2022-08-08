"""
    Python implementation of HiSLIP protocol.  Based on the HiSLIP spec:

    http://www.ivifoundation.org/downloads/Class%20Specifications/IVI-6.1_HiSLIP-1.1-2011-02-24.pdf
"""

import socket
import struct
import time
from typing import Dict, Union

PORT = 4880

LookupTable = Dict[Union[int, str], Union[int, str]]

MESSAGETYPE: LookupTable = {
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
    # reserved for future use         26-127 inclusive
    # VendorSpecific                  128-255 inclusive
}
MESSAGETYPE.update({value: key for (key, value) in MESSAGETYPE.items()})

FATALERRORCODE: LookupTable = {
    0: "Unidentified error",
    1: "Poorly formed message",
    2: "Attempt to use connection without both channels established",
    3: "Invalid initialization sequence",
    4: "Server refused connection due to maximum number of clients exceeded",
    # 5-127:   reserved for HiSLIP extensions
    # 128-255: device defined errors
}
FATALERRORCODE.update({value: key for (key, value) in FATALERRORCODE.items()})

ERRORCODE: LookupTable = {
    0: "Undefined error",
    1: "Unrecognized message type",
    2: "Unrecognized control code",
    3: "Unrecognized vendor defined message",
    4: "Message too large",
    # 5-127:   Reserved
    # 128-255: Device defined errors
}
ERRORCODE.update({value: key for (key, value) in ERRORCODE.items()})

LOCKCONTROLCODE: LookupTable = {
    0: "release",
    1: "request",
}
LOCKCONTROLCODE.update({value: key for (key, value) in LOCKCONTROLCODE.items()})

LOCKRESPONSECONTROLCODE: LookupTable = {
    0: "fail",
    1: "success",
    2: "successSharedLock",
    3: "error",
}
LOCKRESPONSECONTROLCODE.update(
    {value: key for (key, value) in LOCKRESPONSECONTROLCODE.items()}
)

REMOTELOCALCONTROLCODE: LookupTable = {
    0: "disableRemote",
    1: "enableRemote",
    2: "disableAndGTL",
    3: "enableAndGotoRemote",
    4: "enableAndLockoutLocal",
    5: "enableAndGTRLLO",
    6: "justGTL",
}
REMOTELOCALCONTROLCODE.update(
    {value: key for (key, value) in REMOTELOCALCONTROLCODE.items()}
)

HEADER_FORMAT = "!2sBBIQ"
# !  = network order,
# 2s = prologue ('HS'),
# B  = message type (unsigned byte),
# B  = control code (unsigned byte),
# I  = message parameter (unsigned int),
# Q  = payload length (unsigned long long)
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)


class Struct(dict):
    def __init__(self, **kwargs):
        super(Struct, self).__init__(**kwargs)
        self.__dict__ = self


class Instrument:  # pylint: disable=too-many-instance-attributes
    """
    this is the principal export from this module.  it opens up a HiSLIP connection
    to the instrument at the specified IP address.
    """

    def __init__(self, ip_addr, open_timeout=None, port=PORT):
        # init transaction:
        #     C->S: Initialize
        #     S->C: InitializeResponse
        #     C->S: AsyncInitialize
        #     S->C: AsyncInitializeResponse

        timeout = 1e-3 * (open_timeout or 1000)

        # open the synchronous socket and send an initialize packet
        self._sync = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sync.connect((ip_addr, port))
        self._sync.settimeout(timeout)
        self._sync.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        init = self.initialize()
        if init.overlap != 0:
            print("**** prefer overlap = %d" % init.overlap)

        # open the asynchronous socket and send an initialize packet
        self._async = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._async.connect((ip_addr, port))
        self._async.settimeout(timeout)
        self._async.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._async_init = self.async_initialize(session_id=init.session_id)

        # get the maximum message size
        max_msg_size = self.async_maximum_message_size(1 << 24)
        # print("HEADER_SIZE = %s, async_maximum_message_size = %s" % (HEADER_SIZE, max_msg_size))
        # print("max_payload_size = %s" % (max_msg_size - HEADER_SIZE))
        self.max_payload_size = max_msg_size - HEADER_SIZE

        self.timeout = 10
        self.rmt = 0
        self.receiver = None
        self.message_id = 0xFFFFFF00

    # ================ #
    # MEMBER FUNCTIONS #
    # ================ #

    def close(self):
        self._sync.close()
        self._async.close()

    @property
    def timeout(self):
        """returns the timeout value in seconds for both the sync and async sockets"""
        return self._timeout

    @timeout.setter
    def timeout(self, val):
        """sets the timeout value in seconds for both the sync and async sockets"""
        self._timeout = val
        self._sync.settimeout(self._timeout)
        self._async.settimeout(self._timeout)

    def send(self, data):
        """Sends the data on the synchronous channel.  More than one packet
        may be necessary in order to not exceed max_payload_size."""
        data_view = memoryview(data)
        num_bytes_to_send = len(data)

        # send the data in chunks of self.max_payload_size bytes at a time
        while num_bytes_to_send > 0:
            if num_bytes_to_send <= self.max_payload_size:
                assert len(data_view) == num_bytes_to_send
                self.send_data_end_packet(data_view)
                bytes_sent = num_bytes_to_send
            else:
                self.send_data_packet(data_view[: self.max_payload_size])
                bytes_sent = self.max_payload_size

            data_view = data_view[bytes_sent:]
            num_bytes_to_send -= bytes_sent

        return len(data)

    def receive(self, max_len=4096):
        """
        receive data on the synchronous channel, terminating after
        max_len bytes or after receiving a DataEnd message
        """

        # if we don't already have a Receiver object, create one
        if self.receiver is None:
            self.receiver = Receiver(self._sync, self.last_message_id)

        # allocate a buffer and receive the data into it
        recv_buffer = bytearray(max_len)
        result = self.receiver.receive(recv_buffer)

        # if there is no data remaining, get rid of the Receiver object and set the RMT flag
        if self.receiver.payload_remaining == 0 and self.receiver.msg_type == "DataEnd":
            #
            # From IEEE Std 488.2: Response Message Terminator.
            #
            # RMT is the new-line accompanied by END sent from the server
            # to the client at the end of a response. Note that with HiSLIP
            # this is implied by the DataEND message.
            #
            self.rmt = 1
            self.receiver = None

        return result

    def device_clear(self):
        feature = self._async_device_clear()
        # Abandon pending messages and wait for in-process synchronous messages to complete
        time.sleep(0.1)
        # Indicate to server that synchronous channel is cleared out.
        self.device_clear_complete(feature)
        # reset messageID and resume normal opreation
        self.message_id = 0xFFFFFF00

    def initialize(self, version=(1, 0), vendor_id=b"xx", sub_address=b"hislip0"):
        """
        perform an Initialize transaction.
        returns the InitializeResponse header.
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
        return receive_header(self._sync, expected_message_type="InitializeResponse")

    def async_initialize(self, session_id):
        """
        perform an AsyncInitialize transaction.
        returns the AsyncInitializeResponse header.
        """
        header = struct.pack(
            "!2sBBIQ", b"HS", MESSAGETYPE["AsyncInitialize"], 0, session_id, 0
        )
        self._async.sendall(header)
        return receive_header(
            self._async, expected_message_type="AsyncInitializeResponse"
        )

    def async_maximum_message_size(self, size):
        """
        perform an AsyncMaxMsgSize transaction.
        returns the max_msg_size from the AsyncMaxMsgSizeResponse packet.
        """
        # maximum_message_size transaction:
        #     C->S: AsyncMaxMsgSize
        #     S->C: AsyncMaxMsgSizeResponse
        header = struct.pack(
            "!2sBBIQQ", b"HS", MESSAGETYPE["AsyncMaxMsgSize"], 0, 0, 8, size
        )
        self._async.sendall(header)
        response_header = receive_header(
            self._async, expected_message_type="AsyncMaxMsgSizeResponse"
        )
        # print("max_msg_size = %s, type(%s)" % (response_header.max_msg_size,
        #                                       type(response_header.max_msg_size)))
        return response_header.max_msg_size

    def async_lock_info(self):
        """
        perform an AsyncLockInfo transaction.
        returns the exclusive_lock from the AsyncLockInfoResponse packet.
        """
        # async_lock_info transaction:
        #     C->S: AsyncLockInfo
        #     S->C: AsyncLockInfoResponse
        header = struct.pack("!2sBBIQ", b"HS", MESSAGETYPE["AsyncLockInfo"], 0, 0, 0)
        self._async.sendall(header)
        response_header = receive_header(
            self._async, expected_message_type="AsyncLockInfoResponse"
        )
        return response_header.exclusive_lock

    def async_lock_request(self, timeout, lock_string=""):
        """
        perform an AsyncLock request transaction.
        returns the lock_response from the AsyncLockResponse packet.
        """
        # async_lock transaction:
        #     C->S: AsyncLock
        #     S->C: AsyncLockResponse
        header = struct.pack(
            "!2sBBIQ",
            b"HS",
            MESSAGETYPE["AsyncLock"],
            1,
            1e3 * timeout,
            len(lock_string),
        )
        self._async.sendall(header + lock_string)
        response_header = receive_header(
            self._async, expected_message_type="AsyncLockResponse"
        )
        return response_header.lock_response

    def async_lock_release(self):
        """
        perform an AsyncLock release transaction.
        returns the lock_response from the AsyncLockResponse packet.
        """
        # async_lock transaction:
        #     C->S: AsyncLock
        #     S->C: AsyncLockResponse
        header = struct.pack(
            "!2sBBIQ", b"HS", MESSAGETYPE["AsyncLock"], 0, self.last_message_id, 0
        )
        self._async.sendall(header)
        response_header = receive_header(
            self._async, expected_message_type="AsyncLockResponse"
        )
        return response_header.lock_response

    def async_remote_local_control(self, remotelocalcontrol):
        """
        perform an AsyncRemoteLocalControl transaction.
        """
        # remote_local transaction:
        #     C->S: AsyncRemoteLocalControl
        #     S->C: AsyncRemoteLocalResponse
        header = struct.pack(
            "!2sBBIQ",
            b"HS",
            MESSAGETYPE["AsyncRemoteLocalControl"],
            REMOTELOCALCONTROLCODE[remotelocalcontrol],
            self.last_message_id,
            0,
        )
        self._async.sendall(header)
        response_header = receive_header(
            self._async, expected_message_type="AsyncRemoteLocalResponse"
        )
        return response_header.server_status

    def async_status_query(self):
        """
        perform an AsyncStatusQuery transaction.
        returns the server_status from the AsyncStatusResponse packet.
        """
        # async_status_query transaction:
        #     C->S: AsyncStatusQuery
        #     S->C: AsyncStatusResponse
        header = struct.pack(
            "!2sBBIQ",
            b"HS",
            MESSAGETYPE["AsyncStatusQuery"],
            self.rmt,
            self.message_id,
            0,
        )
        self.rmt = 0
        self._async.sendall(header)
        response_header = receive_header(
            self._async, expected_message_type="AsyncStatusResponse"
        )
        return response_header.server_status

    def async_device_clear(self):
        """
        perform an AsyncDeviceClear transaction.
        returns the feature_bitmap from the AsyncDeviceClearAcknowledge packet.
        """
        header = struct.pack("!2sBBIQ", b"HS", MESSAGETYPE["AsyncDeviceClear"], 0, 0, 0)
        self._async.sendall(header)
        response_header = receive_header(
            self._async, expected_message_type="AsyncDeviceClearAcknowledge"
        )
        return response_header.feature_bitmap

    def device_clear_complete(self, feature_bitmap):
        """
        perform a DeviceClear transaction.
        returns the feature_bitmap from the DeviceClearAcknowledge packet.
        """
        header = struct.pack(
            "!2sBBIQ", b"HS", MESSAGETYPE["DeviceClearComplete"], feature_bitmap, 0, 0
        )
        self._sync.sendall(header)
        response_header = receive_header(
            self._sync, expected_message_type="DeviceClearAcknowledge"
        )
        return response_header.feature_bitmap

    def trigger(self):
        """sends a Trigger packet on the sync channel"""
        header = struct.pack(
            "!2sBBIQ", b"HS", MESSAGETYPE["Trigger"], self.rmt, self.message_id, 0
        )
        self.rmt = 0
        self.message_id = (self.message_id + 2) & 0xFFFFFFFF
        self._sync.sendall(header)

    def send_data_packet(self, payload):
        """sends a Data packet on the sync channel"""
        header = struct.pack(
            "!2sBBIQ",
            b"HS",
            MESSAGETYPE["Data"],
            self.rmt,
            self.message_id,
            len(payload),
        )
        self.rmt = 0
        self.last_message_id = self.message_id
        self.message_id = (self.message_id + 2) & 0xFFFFFFFF
        self._sync.sendall(header + payload)

    def send_data_end_packet(self, payload):
        """sends a DataEnd packet on the sync channel"""
        header = struct.pack(
            "!2sBBIQ",
            b"HS",
            MESSAGETYPE["DataEnd"],
            self.rmt,
            self.message_id,
            len(payload),
        )
        self.rmt = 0
        self.last_message_id = self.message_id
        self.message_id = (self.message_id + 2) & 0xFFFFFFFF
        self._sync.sendall(header + payload)

    def fatal_error(self, error, error_message=""):
        header = struct.pack(
            "!2sBBIQ",
            b"HS",
            MESSAGETYPE["FatalError"],
            FATALERRORCODE[error],
            0,
            len(error_message),
        )
        self._sync.sendall(header + error_message.encode())

    def error(self, error, error_message=""):
        header = struct.pack(
            "!2sBBIQ",
            b"HS",
            MESSAGETYPE["Error"],
            ERRORCODE[error],
            0,
            len(error_message),
        )
        self._sync.sendall(header + error_message.encode())


#########################################################################################


def receive_flush(sock, recv_len):
    """
    receive exactly 'recv_len' bytes from 'sock'.
    no explicit timeout is specified, since it is assumed
    that a call to select indicated that data is available.
    received data is thrown away and nothing is returned
    """
    recv_buffer = bytearray(recv_len)
    receive_exact_into(sock, recv_buffer)


def receive_exact(sock, recv_len):
    """
    receive exactly 'recv_len' bytes from 'sock'.
    no explicit timeout is specified, since it is assumed
    that a call to select indicated that data is available.
    returns a bytearray containing the received data.
    """
    recv_buffer = bytearray(recv_len)
    receive_exact_into(sock, recv_buffer)
    return recv_buffer


def receive_exact_into(sock, recv_buffer):
    """
    receive data from 'sock' to exactly fill 'recv_buffer'.
    no explicit timeout is specified, since it is assumed
    that a call to select indicated that data is available.
    """
    view = memoryview(recv_buffer)
    recv_len = len(recv_buffer)
    bytes_recvd = 0

    while bytes_recvd < recv_len:
        request_size = recv_len - bytes_recvd
        data_len = sock.recv_into(view, request_size)
        bytes_recvd += data_len
        view = view[data_len:]

    if bytes_recvd > recv_len:
        raise MemoryError("socket.recv_into scribbled past end of recv_buffer")


def receive_header(  # noqa: C901
    sock, expected_message_type=None
):  # pylint: disable=too-many-statements,too-many-branches
    """receive and decode the HiSLIP message header"""
    header = receive_exact(sock, HEADER_SIZE)
    (
        prologue,
        msg_type,
        control_code,
        message_parameter,
        payload_length,
    ) = struct.unpack(HEADER_FORMAT, header)

    if prologue != b"HS":
        # XXX we should send a 'Fatal Error' to the server, close the sockets, then raise an exception
        raise RuntimeError("protocol synchronization error")

    elif msg_type not in MESSAGETYPE:
        # XXX we should send 'Unrecognized message type' to the
        #     server and discard this packet plus any payload.
        raise RuntimeError("unrecognized message type: %d" % msg_type)

    result = Struct(msg_type=MESSAGETYPE[msg_type])

    if expected_message_type is not None and result.msg_type != expected_message_type:
        # XXX we should send an 'Error: Unidentified Error' to the server and discard this packet plus any payload
        payload = (
            (": " + str(receive_exact(sock, payload_length)))
            if payload_length > 0
            else b""
        )
        raise RuntimeError(
            "expected message type '%s', received '%s%s'"
            % (expected_message_type, result.msg_type, payload)
        )

    if result.msg_type == "InitializeResponse":
        assert payload_length == 0
        result.overlap = bool(control_code)
        result.version, result.session_id = struct.unpack("!4xHH8x", header)

    elif result.msg_type == "AsyncInitializeResponse":
        assert control_code == 0
        assert payload_length == 0
        result.vendor_id = struct.unpack("!4x4s8x", header)

    elif result.msg_type == "AsyncMaxMsgSizeResponse":
        assert control_code == 0
        assert message_parameter == 0
        assert payload_length == 8
        payload = receive_exact(sock, payload_length)
        result.max_msg_size = struct.unpack("!Q", payload)[0]

    elif result.msg_type == "DataEnd" or result.msg_type == "Data":
        assert control_code == 0
        result.message_id = message_parameter
        result.payload_length = payload_length

    elif result.msg_type == "AsyncDeviceClearAcknowledge":
        result.feature_bitmap = control_code
        assert message_parameter == 0
        assert payload_length == 0

    elif result.msg_type == "AsyncInterrupted":
        assert control_code == 0
        result.message_id = message_parameter
        assert payload_length == 0

    elif result.msg_type == "AsyncLockInfoResponse":
        result.exclusive_lock = control_code  # 0: no lock, 1: lock granted
        result.clients_holding_locks = message_parameter
        assert payload_length == 0

    elif result.msg_type == "AsyncLockResponse":
        result.lock_response = LOCKRESPONSECONTROLCODE[control_code]
        assert message_parameter == 0
        assert payload_length == 0

    elif result.msg_type == "AsyncRemoteLocalResponse":
        assert control_code == 0
        assert message_parameter == 0
        assert payload_length == 0

    elif result.msg_type == "AsyncServiceRequest":
        result.server_status = control_code
        assert message_parameter == 0
        assert payload_length == 0

    elif result.msg_type == "AsyncStatusResponse":
        result.server_status = control_code
        assert message_parameter == 0
        assert payload_length == 0

    elif result.msg_type == "DeviceClearAcknowledge":
        result.feature_bitmap = control_code
        assert message_parameter == 0
        assert payload_length == 0

    elif result.msg_type == "Interrupted":
        assert control_code == 0
        result.message_id = message_parameter
        assert payload_length == 0

    elif result.msg_type == "Error":
        result.error_code = ERRORCODE[control_code]
        assert message_parameter == 0
        result.error_message = receive_exact(sock, payload_length)

    elif result.msg_type == "FatalError":
        result.error_code = FATALERRORCODE[control_code]
        assert message_parameter == 0
        result.error_message = receive_exact(sock, payload_length)

    else:
        # XXX we should send 'FatalError' to the server,
        #     close the sockets, then raise an exception
        raise RuntimeError("unrecognized message type")

    return result


class Receiver(object):
    """
    the Receiver class hides the fact that the data is packetized.
    we can receive an arbitrary number of bytes without caring
    how many packets (or partial packets) are received.
    """

    def __init__(self, sock, expected_message_id):
        self.sock = sock
        self.expected_message_id = expected_message_id
        self.payload_remaining = self.get_data_header()

    def get_data_header(self):
        """
        receive a data header (either Data or DataEnd), check the message_id, and
        return the payload_length.
        """
        while True:
            header = receive_header(self.sock)
            self.msg_type = header.msg_type
            assert self.msg_type == "Data" or self.msg_type == "DataEnd"

            # When receiving Data messages if the MessageID is not 0xffff ffff, then verify that the
            # MessageID indicated in the Data message is the MessageID that the client sent to the
            # server with the most recent Data, DataEND or Trigger message.
            #
            # If the MessageIDs do not match, the client shall clear any Data responses already
            # buffered and discard the offending Data message.

            if (
                header.message_id != 0xFFFFFFFF
                and header.message_id != self.expected_message_id
            ):
                if header.message_id < self.expected_message_id:
                    # we're out of sync.  flush this message and continue.
                    receive_flush(self.sock, header.payload_length)
                    continue
                else:
                    # XXX we should send a 'Fatal Error' to the server,
                    #     close the sockets, then raise an exception
                    err_msg = "expected message ID = 0x%x" % self.expected_message_id
                    err_msg += ", received message ID = 0x%x" % header.message_id
                    raise RuntimeError(err_msg)
            return header.payload_length

    def receive(self, recv_buffer):
        """
        receive data, terminating after len(recv_buffer) bytes or
        after receiving a DataEnd message.

        note the use of receive_exact_into (which calls socket.recv_into),
        avoiding unnecessary copies.
        """
        max_len = len(recv_buffer)
        view = memoryview(recv_buffer)
        bytes_recvd = 0

        while bytes_recvd < max_len:
            if self.payload_remaining <= 0:
                if self.msg_type == "DataEnd":
                    # truncate the recv_buffer to the actual number of bytes received
                    recv_buffer = recv_buffer[:bytes_recvd]
                    break
                else:
                    self.payload_remaining = self.get_data_header()

            request_size = min(self.payload_remaining, max_len - bytes_recvd)
            receive_exact_into(self.sock, view[:request_size])
            self.payload_remaining -= request_size
            bytes_recvd += request_size
            view = view[request_size:]

        if bytes_recvd > max_len:
            raise MemoryError("scribbled past end of recv_buffer")

        return recv_buffer
