# -*- coding: utf-8 -*-
"""TCPIP Session implementation using Python Standard library.


:copyright: 2014-2020 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""
import random
import select
import socket
import time
from typing import Any, List, Optional, Tuple

from pyvisa import attributes, constants, errors, rname
from pyvisa.constants import ResourceAttribute, StatusCode

from . import common
from .protocols import rpc, vxi11
from .sessions import Session, UnknownAttribute

# Conversion between VXI11 error codes and VISA status
# TODO this is so far a best guess, in particular 6 and 29 are likely wrong
VXI11_ERRORS_TO_VISA = {
    0: StatusCode.success,  # no_error
    1: StatusCode.error_invalid_format,  # syntax_error
    3: StatusCode.error_connection_lost,  # device_no_accessible
    4: StatusCode.error_invalid_access_key,  # invalid_link_identifier
    5: StatusCode.error_invalid_parameter,  # parameter_error
    6: StatusCode.error_handler_not_installed,  # channel_not_established
    8: StatusCode.error_nonsupported_operation,  # operation_not_supported
    9: StatusCode.error_allocation,  # out_of_resources
    11: StatusCode.error_resource_locked,  # device_locked_by_another_link
    12: StatusCode.error_session_not_locked,  # no_lock_held_by_this_link
    15: StatusCode.error_timeout,  # io_timeout
    17: StatusCode.error_io,  # io_error
    23: StatusCode.error_abort,  # abort
    29: StatusCode.error_window_already_mapped,  # channel_already_established
}


@Session.register(constants.InterfaceType.tcpip, "INSTR")
class TCPIPInstrSession(Session):
    """A TCPIP Session built on socket standard library using VXI-11 protocol."""

    #: Maximum size of a chunk of data in bytes.
    max_recv_size: int

    #: Time to wait before erroring with a timeout when trying to acquire a lock
    lock_timeout: int

    #: Unique ID of the client used to authenticate messages.
    client_id: int

    #: ID of the link used for VXI-11 communication
    link: int

    # Override parsed to take into account the fact that this class is only used
    # for a specific kind of resource
    parsed: rname.TCPIPInstr

    @staticmethod
    def list_resources() -> List[str]:
        # TODO: is there a way to get this?
        return []

    def after_parsing(self) -> None:
        # TODO: board_number not handled
        # vx11 expect all timeouts to be expressed in ms and should be integers
        try:
            self.interface = vxi11.CoreClient(
                self.parsed.host_address, self.open_timeout
            )
        except rpc.RPCError:
            raise errors.VisaIOError(constants.VI_ERROR_RSRC_NFOUND)

        self.lock_timeout = 10000
        self.client_id = random.getrandbits(31)

        error, link, abort_port, max_recv_size = self.interface.create_link(
            self.client_id, 0, self.lock_timeout, self.parsed.lan_device_name
        )

        if error:
            raise Exception("error creating link: %d" % error)

        self.link = link
        self.max_recv_size = min(max_recv_size, 2 ** 30)  # 1GB

        for name in ("SEND_END_EN", "TERMCHAR", "TERMCHAR_EN"):
            attribute = getattr(constants, "VI_ATTR_" + name)
            self.attrs[attribute] = attributes.AttributesByID[attribute].default

    def close(self) -> StatusCode:
        try:
            self.interface.destroy_link(self.link)
        except (errors.VisaIOError, socket.error, rpc.RPCError) as e:
            print("Error closing VISA link: {}".format(e))

        self.interface.close()
        self.link = 0
        self.interface = None

        return StatusCode.success

    def read(self, count: int) -> Tuple[bytes, StatusCode]:
        """Reads data from device or interface synchronously.

        Corresponds to viRead function of the VISA library.

        Parameters
        ----------
        count : int
            Number of bytes to be read.

        Returns
        -------
        bytes
            Data read
        StatusCode
            Return value of the library call.

        """
        if count < self.max_recv_size:
            chunk_length = count
        else:
            chunk_length = self.max_recv_size

        if self.get_attribute(ResourceAttribute.termchar_enabled)[0]:
            term_char, _ = self.get_attribute(ResourceAttribute.termchar)
            flags = vxi11.OP_FLAG_TERMCHAR_SET
        else:
            term_char = flags = 0

        read_data = bytearray()
        reason = 0
        # Stop on end of message or when a termination character has been
        # encountered.
        end_reason = vxi11.RX_END | vxi11.RX_CHR
        read_fun = self.interface.device_read
        status = StatusCode.success

        timeout = self._io_timeout
        start_time = time.time()
        while reason & end_reason == 0:
            # Decrease timeout so that the total timeout does not get larger
            # than the specified timeout.
            timeout = max(0, timeout - int((time.time() - start_time) * 1000))
            error, reason, data = read_fun(
                self.link, chunk_length, timeout, self.lock_timeout, flags, term_char
            )

            if error == vxi11.ErrorCodes.io_timeout:
                return bytes(read_data), StatusCode.error_timeout
            elif error:
                return bytes(read_data), StatusCode.error_io

            read_data.extend(data)
            count -= len(data)

            if count <= 0:
                status = StatusCode.success_max_count_read
                break

            chunk_length = min(count, chunk_length)

        return bytes(read_data), status

    def write(self, data: bytes) -> Tuple[int, StatusCode]:
        """Writes data to device or interface synchronously.

        Corresponds to viWrite function of the VISA library.

        Parameters
        ----------
        data : bytes
            Data to be written.

        Returns
        -------
        int
            Number of bytes actually transferred
        StatusCode
            Return value of the library call.

        """
        send_end, _ = self.get_attribute(ResourceAttribute.send_end_enabled)
        chunk_size = 1024

        try:
            flags = 0
            num = len(data)
            offset = 0

            while num > 0:
                if num <= chunk_size:
                    flags |= vxi11.OP_FLAG_END

                block = data[offset : offset + self.max_recv_size]

                error, size = self.interface.device_write(
                    self.link, self._io_timeout, self.lock_timeout, flags, block
                )

                if error == vxi11.ErrorCodes.io_timeout:
                    return offset, StatusCode.error_timeout

                elif error or size < len(block):
                    return offset, StatusCode.error_io

                offset += size
                num -= size

            return offset, StatusCode.success

        except vxi11.Vxi11Error:
            return 0, StatusCode.error_timeout

    def _get_attribute(self, attribute: ResourceAttribute) -> Tuple[Any, StatusCode]:
        """Get the value for a given VISA attribute for this session.

        Use to implement custom logic for attributes.

        Parameters
        ----------
        attribute :
            Resource attribute for which the state query is made

        Returns
        -------
        Any
            The state of the queried attribute for a specified resource
        StatusCode
            Return value of the library call.

        """
        if attribute == constants.VI_ATTR_TCPIP_ADDR:
            return self.parsed.host_address, StatusCode.success

        elif attribute == constants.VI_ATTR_TCPIP_DEVICE_NAME:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_TCPIP_HOSTNAME:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_TCPIP_KEEPALIVE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_TCPIP_NODELAY:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_TCPIP_PORT:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_SUPPRESS_END_EN:
            raise NotImplementedError

        raise UnknownAttribute(attribute)

    def _set_attribute(
        self, attribute: ResourceAttribute, attribute_state: Any
    ) -> StatusCode:
        """Sets the state of an attribute.

        Corresponds to viSetAttribute function of the VISA library.

        Parameters
        ----------
        attribute : ResourceAttribute
             Attribute for which the state is to be modified.
        attribute_state : Any
            The state of the attribute to be set for the specified object.

        Returns
        -------
        StatusCode
            Return value of the library call.

        """
        raise UnknownAttribute(attribute)

    def assert_trigger(self, protocol: constants.TriggerProtocol):
        """Asserts software or hardware trigger.

        Corresponds to viAssertTrigger function of the VISA library.

        Parameters
        ----------
        protocol : constants.TriggerProtocol
            Trigger protocol to use during assertion. Only default is supported.

        Returns
        -------
        StatusCode
            Return value of the library call.

        """
        # XXX make this nicer (either validate protocol or pass it)
        error = self.interface.device_trigger(
            self.link, 0, self.lock_timeout, self._io_timeout
        )

        return VXI11_ERRORS_TO_VISA[error]

    def clear(self) -> StatusCode:
        """Clears a device.

        Corresponds to viClear function of the VISA library.

        """
        error = self.interface.device_clear(
            self.link, 0, self.lock_timeout, self._io_timeout
        )

        return VXI11_ERRORS_TO_VISA[error]

    def read_stb(self) -> Tuple[int, StatusCode]:
        """Reads a status byte of the service request.

        Corresponds to viReadSTB function of the VISA library.

        Returns
        -------
        int
            Service request status byte
        StatusCode
            Return value of the library call.

        """
        error, stb = self.interface.device_read_stb(
            self.link, 0, self.lock_timeout, self._io_timeout
        )

        return stb, VXI11_ERRORS_TO_VISA[error]

    def lock(
        self,
        lock_type: constants.Lock,
        timeout: int,
        requested_key: Optional[str] = None,
    ) -> Tuple[str, constants.StatusCode]:
        """Establishes an access mode to the specified resources.

        Corresponds to viLock function of the VISA library.

        Parameters
        ----------
        session : VISASession
            Unique logical identifier to a session.
        lock_type : constants.Lock
            Specifies the type of lock requested.
        timeout : int
            Absolute time period (in milliseconds) that a resource waits to get
            unlocked by the locking session before returning an error.
        requested_key : Optional[str], optional
            Requested locking key in the case of a shared lock. For an exclusive
            lock it should be None.

        Returns
        -------
        Optional[str]
            Key that can then be passed to other sessions to share the lock, or
            None for an exclusive lock.
        StatusCode
            Return value of the library call.

        """
        #  TODO: lock type not implemented
        flags = 0

        error = self.interface.device_lock(self.link, flags, self.lock_timeout)

        return "", VXI11_ERRORS_TO_VISA[error]

    def unlock(self) -> constants.StatusCode:
        """Relinquish a lock for the specified resource.

        Corresponds to viUnlock function of the VISA library.

        Returns
        -------
        StatusCode
            Return value of the library call.

        """
        error = self.interface.device_unlock(self.link)

        return VXI11_ERRORS_TO_VISA[error]

    def _set_timeout(self, attribute: ResourceAttribute, value: int) -> StatusCode:
        """Sets timeout calculated value from python way to VI_ way"""
        if value == constants.VI_TMO_INFINITE:
            self.timeout = None
            self._io_timeout = 2 ** 32 - 1
        elif value == constants.VI_TMO_IMMEDIATE:
            self.timeout = 0
            self._io_timeout = 0
        else:
            self.timeout = value / 1000.0
            self._io_timeout = int(self.timeout * 1000)
        return StatusCode.success


@Session.register(constants.InterfaceType.tcpip, "SOCKET")
class TCPIPSocketSession(Session):
    """A TCPIP Session that uses the network standard library to do the low
    level communication.

    """

    # Details about implementation:
    # On Windows, select is not interrupted by KeyboardInterrupt, to avoid
    # blocking for very long time, we use a decreasing timeout in select.
    # A minimum select timeout which prevents using too short select interval
    # is also calculated and select timeout is not lower that that minimum
    # timeout. The absolute minimum is 1 ms as a consequence.
    # This is valid for connect and read operations

    #: Maximum size of a chunk of data in bytes.
    max_recv_size: int

    # Override parsed to take into account the fact that this class is only used
    # for a specific kind of resource
    parsed: rname.TCPIPSocket

    @staticmethod
    def list_resources() -> List[str]:
        # TODO: is there a way to get this?
        return []

    def after_parsing(self) -> None:
        # TODO: board_number not handled

        ret_status = self._connect()
        if ret_status != StatusCode.success:
            self.close()
            raise Exception("could not connect: {0}".format(str(ret_status)))

        self.max_recv_size = 4096
        # This buffer is used to store the bytes that appeared after
        # termination char
        self._pending_buffer = bytearray()

        self.attrs[ResourceAttribute.tcpip_address] = self.parsed.host_address
        self.attrs[ResourceAttribute.tcpip_port] = self.parsed.port
        self.attrs[ResourceAttribute.interface_number] = self.parsed.board
        self.attrs[ResourceAttribute.tcpip_nodelay] = (
            self._get_tcpip_nodelay,
            self._set_attribute,
        )
        self.attrs[ResourceAttribute.tcpip_hostname] = ""
        self.attrs[ResourceAttribute.tcpip_keepalive] = (
            self._get_tcpip_keepalive,
            self._set_tcpip_keepalive,
        )
        # to use default as ni visa driver (NI-VISA 15.0)
        self.attrs[ResourceAttribute.suppress_end_enabled] = True

        for name in ("TERMCHAR", "TERMCHAR_EN"):
            attribute = getattr(constants, "VI_ATTR_" + name)
            self.attrs[attribute] = attributes.AttributesByID[attribute].default

    def _connect(self) -> StatusCode:
        timeout = self.open_timeout / 1000.0 if self.open_timeout else 10.0
        try:
            self.interface = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.interface.setblocking(False)
            self.interface.connect_ex((self.parsed.host_address, int(self.parsed.port)))
        except Exception as e:
            raise Exception("could not connect: {0}".format(str(e)))
        finally:
            self.interface.setblocking(True)

        # minimum is in interval 100 - 500ms based on timeout
        min_select_timeout = max(min(timeout / 10.0, 0.5), 0.1)
        # initial 'select_timout' is half of timeout or max 2 secs
        # (max blocking time). min is from 'min_select_timeout'
        select_timout = max(min(timeout / 2.0, 2.0), min_select_timeout)
        # time, when loop shall finish
        finish_time = time.time() + timeout
        while True:
            # use select to wait for socket ready, max `select_timout` seconds
            r, w, x = select.select(
                [self.interface], [self.interface], [], select_timout
            )
            if self.interface in r or self.interface in w:
                return StatusCode.success

            if time.time() >= finish_time:
                # reached timeout
                return StatusCode.error_timeout

            # `select_timout` decreased to 50% of previous or
            # min_select_timeout
            select_timout = max(select_timout / 2.0, min_select_timeout)

    def close(self) -> StatusCode:
        self.interface.close()
        self.interface = None
        return StatusCode.success

    def read(self, count: int) -> Tuple[bytes, StatusCode]:
        """Reads data from device or interface synchronously.

        Corresponds to viRead function of the VISA library.

         Parameters
        -----------
        count : int
            Number of bytes to be read.

        Returns
        -------
        bytes
            Data read from the device
        StatusCode
            Return value of the library call.

        """
        if count < self.max_recv_size:
            chunk_length = count
        else:
            chunk_length = self.max_recv_size

        term_char, _ = self.get_attribute(ResourceAttribute.termchar)
        term_byte = common.int_to_byte(term_char) if term_char else b""
        term_char_en, _ = self.get_attribute(ResourceAttribute.termchar_enabled)
        suppress_end_en, _ = self.get_attribute(ResourceAttribute.suppress_end_enabled)

        read_fun = self.interface.recv

        # minimum is in interval 1 - 100ms based on timeout, 1sec if no timeout
        # defined
        min_select_timeout = (
            1 if self.timeout is None else max(min(self.timeout / 100.0, 0.1), 0.001)
        )
        # initial 'select_timout' is half of timeout or max 2 secs
        # (max blocking time). min is from 'min_select_timeout'
        select_timout = (
            2.0
            if self.timeout is None
            else max(min(self.timeout / 2.0, 2.0), min_select_timeout)
        )
        # time, when loop shall finish, None means never ending story if no
        # data arrives
        finish_time = None if self.timeout is None else (time.time() + self.timeout)
        while True:

            # check, if we have any data received (from pending buffer or
            # further reading)
            if term_char_en and term_byte in self._pending_buffer:
                term_byte_index = self._pending_buffer.index(term_byte) + 1
                if term_byte_index > count:
                    term_byte_index = count
                    status = StatusCode.success_max_count_read
                else:
                    status = StatusCode.success_termination_character_read
                out = bytes(self._pending_buffer[:term_byte_index])
                self._pending_buffer = self._pending_buffer[term_byte_index:]
                return out, status

            if len(self._pending_buffer) >= count:
                out = bytes(self._pending_buffer[:count])
                self._pending_buffer = self._pending_buffer[count:]
                return out, StatusCode.success_max_count_read

            # use select to wait for read ready, max `select_timout` seconds
            r, w, x = select.select([self.interface], [], [], select_timout)

            read_data = b""
            if self.interface in r:
                read_data = read_fun(chunk_length)
                self._pending_buffer.extend(read_data)

            if not read_data:
                # can't read chunk or timeout
                if self._pending_buffer and not suppress_end_en:
                    # we have some data without termchar but no further data
                    # expected
                    out = bytes(self._pending_buffer[:count])
                    self._pending_buffer = self._pending_buffer[count:]
                    return out, StatusCode.success

                if finish_time and time.time() >= finish_time:
                    # reached timeout
                    out = bytes(self._pending_buffer[:count])
                    self._pending_buffer = self._pending_buffer[count:]
                    return out, StatusCode.error_timeout

                # `select_timout` decreased to 50% of previous or
                # min_select_timeout
                select_timout = max(select_timout / 2.0, min_select_timeout)

    def write(self, data: bytes) -> Tuple[int, StatusCode]:
        """Writes data to device or interface synchronously.

        Corresponds to viWrite function of the VISA library.

        Parameters
        ----------
        data : bytes
            Data to be written.

        Returns
        -------
        int
            Number of bytes actually transferred
        StatusCode
            Return value of the library call.

        """
        chunk_size = 4096

        num = sz = len(data)

        offset = 0

        while num > 0:

            block = data[offset : min(offset + chunk_size, sz)]

            try:
                # use select to wait for write ready
                select.select([], [self.interface], [])
                size = self.interface.send(block)
            except socket.timeout:
                return offset, StatusCode.error_io

            if size < len(block):
                return offset, StatusCode.error_io

            offset += size
            num -= size

        return offset, StatusCode.success

    def clear(self) -> StatusCode:
        """Clears a device.

        Corresponds to viClear function of the VISA library.

        """
        self._pending_buffer.clear()
        while True:
            r, w, x = select.select([self.interface], [], [], 0.1)
            if not r:
                break
            r[0].recv(4096)

        return StatusCode.success

    def _get_tcpip_nodelay(
        self, attribute: ResourceAttribute
    ) -> Tuple[constants.VisaBoolean, StatusCode]:
        if self.interface:
            value = self.interface.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
            return (
                constants.VisaBoolean.true
                if value == 1
                else constants.VisaBoolean.false,
                StatusCode.success,
            )
        return constants.VisaBoolean.false, StatusCode.error_nonsupported_attribute

    def _set_tcpip_nodelay(
        self, attribute: ResourceAttribute, attribute_state: bool
    ) -> StatusCode:
        if self.interface:
            self.interface.setsockopt(
                socket.IPPROTO_TCP, socket.TCP_NODELAY, 1 if attribute_state else 0
            )
            return StatusCode.success
        return StatusCode.error_nonsupported_attribute

    def _get_tcpip_keepalive(
        self, attribute: ResourceAttribute
    ) -> Tuple[constants.VisaBoolean, StatusCode]:
        if self.interface:
            value = self.interface.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
            return (
                constants.VisaBoolean.true
                if value == 1
                else constants.VisaBoolean.false,
                StatusCode.success,
            )
        return constants.VisaBoolean.false, StatusCode.error_nonsupported_attribute

    def _set_tcpip_keepalive(
        self, attribute: ResourceAttribute, attribute_state: bool
    ) -> StatusCode:
        if self.interface:
            self.interface.setsockopt(
                socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1 if attribute_state else 0
            )
            return StatusCode.success
        return StatusCode.error_nonsupported_attribute

    def _get_attribute(self, attribute: ResourceAttribute) -> Tuple[Any, StatusCode]:
        """Get the value for a given VISA attribute for this session.

        Use to implement custom logic for attributes.

        Parameters
        ----------
        attribute : ResourceAttribute
            Attribute for which the state query is made

        Returns
        -------
        Any
            State of the queried attribute for a specified resource
        StatusCode
            Return value of the library call.

        """
        raise UnknownAttribute(attribute)

    def _set_attribute(
        self, attribute: ResourceAttribute, attribute_state: Any
    ) -> StatusCode:
        """Sets the state of an attribute.

        Corresponds to viSetAttribute function of the VISA library.

        Parameters
        ----------
        attribute : constants.ResourceAttribute
            Attribute for which the state is to be modified. (Attributes.*)
        attribute_state : Any
            The state of the attribute to be set for the specified object.

        Returns
        -------
        StatusCode
            Return value of the library call.

        """
        raise UnknownAttribute(attribute)
