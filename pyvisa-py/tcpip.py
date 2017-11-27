# -*- coding: utf-8 -*-
"""
    pyvisa-py.tcpip
    ~~~~~~~~~~~~~~~

    TCPIP Session implementation using Python Standard library.


    :copyright: 2014 by PyVISA-py Authors, see AUTHORS for more details.
    :license: MIT, see LICENSE for more details.
"""

import random
import socket
import select
import time

from pyvisa import constants, attributes, errors

from .sessions import Session, UnknownAttribute
from .protocols import vxi11, rpc
from . import common


StatusCode = constants.StatusCode
SUCCESS = StatusCode.success


@Session.register(constants.InterfaceType.tcpip, 'INSTR')
class TCPIPInstrSession(Session):
    """A TCPIP Session that uses the network standard library to do the low level communication
    using VXI-11
    """

    lock_timeout = 1000
    timeout = 1000
    client_id = None
    link = None
    max_recv_size = 1024

    @staticmethod
    def list_resources():
        # TODO: is there a way to get this?
        return []

    def after_parsing(self):
        # TODO: board_number not handled
        # TODO: lan_device_name not handled
        self.interface = vxi11.CoreClient(self.parsed.host_address)

        self.lock_timeout = 10000
        self.timeout = 10000
        self.client_id = random.getrandbits(31)

        error, link, abort_port, max_recv_size = self.interface.create_link(
            self.client_id, 0, self.lock_timeout, self.parsed.lan_device_name)

        if error:
            raise Exception("error creating link: %d" % error)

        self.link = link
        self.max_recv_size = min(max_recv_size, 2 ** 30)  # 1GB

        for name in ('SEND_END_EN', 'TERMCHAR', 'TERMCHAR_EN'):
            attribute = getattr(constants, 'VI_ATTR_' + name)
            self.attrs[attribute] = attributes.AttributesByID[attribute].default

    def close(self):
        try:
            self.interface.destroy_link(self.link)
        except (errors.VisaIOError, socket.error, rpc.RPCError) as e:
            print("Error closing VISA link: {}".format(e))

        self.interface.close()
        self.link = None
        self.interface = None

    def read(self, count):
        """Reads data from device or interface synchronously.

        Corresponds to viRead function of the VISA library.

        :param count: Number of bytes to be read.
        :return: data read, return value of the library call.
        :rtype: bytes, VISAStatus
        """
        if count < self.max_recv_size:
            chunk_length = count
        else:
            chunk_length = self.max_recv_size

        if self.get_attribute(constants.VI_ATTR_TERMCHAR_EN)[0]:
            term_char, _ = self.get_attribute(constants.VI_ATTR_TERMCHAR)
            term_char = str(term_char).encode('utf-8')[0]
            flags = vxi11.OP_FLAG_TERMCHAR_SET
        else:
            term_char = flags = 0

        read_data = bytearray()
        reason = 0
        end_reason = vxi11.RX_END | vxi11.RX_CHR
        read_fun = self.interface.device_read
        status = SUCCESS

        while reason & end_reason == 0:
            error, reason, data = read_fun(self.link, chunk_length, self.timeout,
                                           self.lock_timeout, flags, term_char)

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

    def write(self, data):
        """Writes data to device or interface synchronously.

        Corresponds to viWrite function of the VISA library.

        :param data: data to be written.
        :type data: str
        :return: Number of bytes actually transferred, return value of the library call.
        :rtype: int, VISAStatus
        """

        send_end, _ = self.get_attribute(constants.VI_ATTR_SEND_END_EN)
        chunk_size = 1024

        try:
            if send_end:
                flags = vxi11.OP_FLAG_TERMCHAR_SET
            else:
                flags = 0

            num = len(data)
            offset = 0

            while num > 0:
                if num <= chunk_size:
                    flags |= vxi11.OP_FLAG_END

                block = data[offset:offset + self.max_recv_size]

                error, size = self.interface.device_write(
                    self.link, self.timeout, self.lock_timeout, flags, block)

                if error == vxi11.ErrorCodes.io_timeout:
                    return offset, StatusCode.error_timeout

                elif error or size < len(block):
                    return offset, StatusCode.error_io

                offset += size
                num -= size

            return offset, SUCCESS

        except vxi11.Vxi11Error:
            return 0, StatusCode.error_timeout

    def _get_attribute(self, attribute):
        """Get the value for a given VISA attribute for this session.

        Use to implement custom logic for attributes.

        :param attribute: Resource attribute for which the state query is made
        :return: The state of the queried attribute for a specified resource, return value of the library call.
        :rtype: (unicode | str | list | int, VISAStatus)
        """

        if attribute == constants.VI_ATTR_TCPIP_ADDR:
            return self.host_address, SUCCESS

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

    def _set_attribute(self, attribute, attribute_state):
        """Sets the state of an attribute.

        Corresponds to viSetAttribute function of the VISA library.

        :param attribute: Attribute for which the state is to be modified. (Attributes.*)
        :param attribute_state: The state of the attribute to be set for the specified object.
        :return: return value of the library call.
        :rtype: VISAStatus
        """

        raise UnknownAttribute(attribute)

    def assert_trigger(self, protocol):
        """Asserts software or hardware trigger.

        Corresponds to viAssertTrigger function of the VISA library.

        :param protocol: Trigger protocol to use during assertion. (Constants.PROT*)
        :return: return value of the library call.
        :rtype: VISAStatus
        """

        error = self.interface.device_trigger(self.link, 0, self.lock_timeout,
                                              self.io_timeout)

        if error:
            # TODO: Which status to return
            raise Exception("error triggering: %d" % error)

        return SUCCESS

    def clear(self):
        """Clears a device.

        Corresponds to viClear function of the VISA library.

        :return: return value of the library call.
        :rtype: VISAStatus
        """

        error = self.interface.device_clear(self.link, 0, self.lock_timeout,
                                            self.io_timeout)

        if error:
            # TODO: Which status to return
            raise Exception("error clearing: %d" % error)

        return SUCCESS

    def read_stb(self):
        """Reads a status byte of the service request.

        Corresponds to viReadSTB function of the VISA library.

        :return: Service request status byte, return value of the library call.
        :rtype: int, VISAStatus
        """

        error, stb = self.interface.device_read_stb(self.link, 0,
                                                    self.lock_timeout,
                                                    self.io_timeout)

        if error:
            # TODO: Which status to return
            raise Exception("error reading status: %d" % error)

        return stb, SUCCESS

    def lock(self, lock_type, timeout, requested_key=None):
        """Establishes an access mode to the specified resources.

        Corresponds to viLock function of the VISA library.

        :param lock_type: Specifies the type of lock requested, either Constants.EXCLUSIVE_LOCK or Constants.SHARED_LOCK.
        :param timeout: Absolute time period (in milliseconds) that a resource waits to get unlocked by the
                        locking session before returning an error.
        :param requested_key: This parameter is not used and should be set to VI_NULL when lockType is VI_EXCLUSIVE_LOCK.
        :return: access_key that can then be passed to other sessions to share the lock, return value of the library call.
        :rtype: str, VISAStatus
        """

        #  TODO: lock type not implemented
        flags = 0

        error = self.interface.device_lock(self.link, flags, self.lock_timeout)

        if error:
            # TODO: Which status to return
            raise Exception("error locking: %d" % error)

    def unlock(self):
        """Relinquishes a lock for the specified resource.

        Corresponds to viUnlock function of the VISA library.

        :return: return value of the library call.
        :rtype: VISAStatus
        """
        error = self.interface.device_unlock(self.link)

        if error:
            # TODO: Which message to return
            raise Exception("error unlocking: %d" % error)


@Session.register(constants.InterfaceType.tcpip, 'SOCKET')
class TCPIPSocketSession(Session):
    """A TCPIP Session that uses the network standard library to do the low level communication.
    """

    lock_timeout = 1000
    timeout = 1000

    max_recv_size = 4096

    # This buffer is used to store the bytes that appeared after termination char
    _pending_buffer = bytearray()

    @staticmethod
    def list_resources():
        # TODO: is there a way to get this?
        return []

    def after_parsing(self):
        # TODO: board_number not handled

        self.interface = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.interface.setblocking(0)

        try:
            self.interface.connect_ex((self.parsed.host_address, int(self.parsed.port)))
        except Exception as e:
            raise Exception("could not create socket: %s" % e)

        self.attrs[constants.VI_ATTR_TCPIP_ADDR] = self.parsed.host_address
        self.attrs[constants.VI_ATTR_TCPIP_PORT] = self.parsed.port
        self.attrs[constants.VI_ATTR_INTF_NUM] = self.parsed.board

        for name in ('TERMCHAR', 'TERMCHAR_EN', 'SUPPRESS_END_EN'):
            attribute = getattr(constants, 'VI_ATTR_' + name)
            self.attrs[attribute] = attributes.AttributesByID[attribute].default
        # to use default as ni visa driver (NI-VISA 15.0)
        self.attrs[getattr(constants, 'VI_ATTR_SUPPRESS_END_EN')] = True

    def close(self):
        self.interface.close()
        self.interface = None

    def read(self, count):
        """Reads data from device or interface synchronously.

        Corresponds to viRead function of the VISA library.

        :param count: Number of bytes to be read.
        :return: data read, return value of the library call.
        :rtype: bytes, VISAStatus
        """
        if count < self.max_recv_size:
            chunk_length = count
        else:
            chunk_length = self.max_recv_size

        term_char, _ = self.get_attribute(constants.VI_ATTR_TERMCHAR)
        term_byte = common.int_to_byte(term_char) if term_char else b''
        term_char_en, _ = self.get_attribute(constants.VI_ATTR_TERMCHAR_EN)
        timeout, _ = self.get_attribute(constants.VI_ATTR_TMO_VALUE)
        timeout /= 1000.0
        suppress_end_en, _ = self.get_attribute(constants.VI_ATTR_SUPPRESS_END_EN)

        read_fun = self.interface.recv

        out = bytearray()
        out.extend(self._pending_buffer)

        # On Windows, select is not interrupted by KeyboardInterrupt, to avoid blocking
        # for very long time, we use a decreasing timeout in select
        # minimum select timeout to avoid too short select interval
        # (minimum is in interval 1 - 100ms based on timeout)
        min_select_timeout = max(min(timeout/100.0, 0.1), 0.001)
        # initial 'select_timout' is half of timeout or max 2 secs (max blocking time).
        # min is from 'min_select_timeout'
        select_timout = max(min(timeout/2.0, 2.0), min_select_timeout)
        # time, when loop shall finish
        finish_time = time.time() + timeout
        while True:

            # check, if we have any data received (from pending buffer or further reading)
            if term_char_en and term_byte in out:
                term_byte_index = out.index(term_byte) + 1
                self._pending_buffer = out[term_byte_index:]
                return bytes(out[:term_byte_index]), constants.StatusCode.success_termination_character_read

            if len(out) >= count:
                self._pending_buffer = out[count:]
                return bytes(out[:count]), constants.StatusCode.success_max_count_read

            # use select to wait for read ready, max `select_timout` seconds, min is 'min_select_timeout' seconds
            r, w, x = select.select([self.interface], [], [], select_timout)

            read_data = b''
            if self.interface in r:
                read_data = read_fun(chunk_length)
                out.extend(read_data)

            if not read_data:
                # can't read chunk or timeout
                if out and not suppress_end_en:
                    # we have some data without termchar but no further data expected
                    self._pending_buffer = out[count:]
                    return bytes(out[:count]), constants.StatusCode.success
    
                if time.time() >= finish_time:
                    # reached timeout
                    self._pending_buffer = out[count:]
                    return bytes(out[:count]), constants.StatusCode.error_timeout

                # `select_timout` decreased to 50% of previous but to be min_select_timeout as minimum
                select_timout = max(select_timout/2.0, min_select_timeout)

    def write(self, data):
        """Writes data to device or interface synchronously.

        Corresponds to viWrite function of the VISA library.

        :param data: data to be written.
        :type data: str
        :return: Number of bytes actually transferred, return value of the library call.
        :rtype: int, VISAStatus
        """

        chunk_size = 4096

        num = sz = len(data)

        offset = 0

        while num > 0:

            block = data[offset:min(offset + chunk_size, sz)]

            try:
                # use select to wait for write ready
                select.select([], [self.interface], [])
                size = self.interface.send(block)
            except socket.timeout as e:
                return offset, StatusCode.error_io

            if size < len(block):
                return offset, StatusCode.error_io

            offset += size
            num -= size

        return offset, SUCCESS

    def _get_attribute(self, attribute):
        """Get the value for a given VISA attribute for this session.

        Use to implement custom logic for attributes.

        :param attribute: Resource attribute for which the state query is made
        :return: The state of the queried attribute for a specified resource, return value of the library call.
        :rtype: (unicode | str | list | int, VISAStatus)
        """

        if attribute == constants.VI_ATTR_TCPIP_HOSTNAME:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_TCPIP_KEEPALIVE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_TCPIP_NODELAY:
            raise NotImplementedError

        raise UnknownAttribute(attribute)

    def _set_attribute(self, attribute, attribute_state):
        """Sets the state of an attribute.

        Corresponds to viSetAttribute function of the VISA library.

        :param attribute: Attribute for which the state is to be modified. (Attributes.*)
        :param attribute_state: The state of the attribute to be set for the specified object.
        :return: return value of the library call.
        :rtype: VISAStatus
        """

        raise UnknownAttribute(attribute)
