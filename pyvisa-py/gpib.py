# -*- coding: utf-8 -*-
"""
    pyvisa-py.gpib
    ~~~~~~~~~~~~~~

    GPIB Session implementation using linux-gpib.


    :copyright: 2015 by PyVISA-py Authors, see AUTHORS for more details.
    :license: MIT, see LICENSE for more details.
"""

from __future__ import division, unicode_literals, print_function, absolute_import
from bisect import bisect

from pyvisa import constants, logger, attributes

from .sessions import Session, UnknownAttribute

try:
    import gpib
    from Gpib import Gpib
except ImportError as e:
    Session.register_unavailable(constants.InterfaceType.gpib, 'INSTR',
                                 'Please install linux-gpib to use this resource type.\n%s' % e)
    raise


def _find_listeners():
    """Find GPIB listeners.
    """
    for i in range(31):
        try:
            if gpib.listener(BOARD, i) and gpib.ask(BOARD, 1) != i:
                yield i
        except gpib.GpibError as e:
            logger.debug("GPIB error in _find_listeners(): %s", repr(e))


StatusCode = constants.StatusCode

# linux-gpib timeout constants, in seconds. See GPIBSession._set_timeout.
TIMETABLE = (0, 10e-6, 30e-6, 100e-6, 300e-6, 1e-3, 3e-3, 10e-3, 30e-3, 100e-3, 300e-3, 1.0, 3.0,
             10.0, 30.0, 100.0, 300.0, 1000.0)

# TODO: Check board indices other than 0.
BOARD = 0
# TODO: Check secondary addresses.
@Session.register(constants.InterfaceType.gpib, 'INSTR')
class GPIBSession(Session):
    """A GPIB Session that uses linux-gpib to do the low level communication.
    """

    @staticmethod
    def list_resources():
        return ['GPIB0::%d::INSTR' % pad for pad in _find_listeners()]

    @classmethod
    def get_low_level_info(cls):
        try:
            ver = gpib.version()
        except AttributeError:
            ver = '< 4.0'

        return 'via Linux GPIB (%s)' % ver

    def after_parsing(self):
        minor = int(self.parsed.board)
        pad = int(self.parsed.primary_address)
        sad = 0
        timeout = 13
        send_eoi = 1
        eos_mode = 0
        self.interface = Gpib(name=minor, pad=pad, sad=sad, timeout=timeout, send_eoi=send_eoi, eos_mode=eos_mode)
        self.controller = Gpib(name=minor) # this is the bus controller device
        self.handle = self.interface.id
        # force timeout setting to interface
        self.set_attribute(constants.VI_ATTR_TMO_VALUE, attributes.AttributesByID[constants.VI_ATTR_TMO_VALUE].default)

    def _get_timeout(self, attribute):
        if self.interface:
            # 0x3 is the hexadecimal reference to the IbaTMO (timeout) configuration
            # option in linux-gpib.
            gpib_timeout = self.interface.ask(3)
            if gpib_timeout and gpib_timeout < len(TIMETABLE):
                self.timeout = TIMETABLE[gpib_timeout]
            else:
                # value is 0 or out of range -> infinite
                self.timeout = None
        return super(GPIBSession, self)._get_timeout(attribute)

    def _set_timeout(self, attribute, value):
        """
        linux-gpib only supports 18 discrete timeout values. If a timeout
        value other than these is requested, it will be rounded up to the closest
        available value. Values greater than the largest available timout value
        will instead be rounded down. The available timeout values are:
        0   Never timeout.
        1   10 microseconds
        2   30 microseconds
        3   100 microseconds
        4   300 microseconds
        5   1 millisecond
        6   3 milliseconds
        7   10 milliseconds
        8   30 milliseconds
        9   100 milliseconds
        10  300 milliseconds
        11  1 second
        12  3 seconds
        13  10 seconds
        14  30 seconds
        15  100 seconds
        16  300 seconds
        17  1000 seconds
        """
        status = super(GPIBSession, self)._set_timeout(attribute, value)
        if self.interface:
            if self.timeout is None:
                gpib_timeout = 0
            else:
                # round up only values that are higher by 0.1% than discrete values
                gpib_timeout = min(bisect(TIMETABLE, 0.999 * self.timeout), 17)
                self.timeout = TIMETABLE[gpib_timeout]
            self.interface.timeout(gpib_timeout)
        return status

    def close(self):
        gpib.close(self.handle)

    def read(self, count):
        """Reads data from device or interface synchronously.

        Corresponds to viRead function of the VISA library.

        :param count: Number of bytes to be read.
        :return: data read, return value of the library call.
        :rtype: bytes, constants.StatusCode
        """

        # 0x2000 = 8192 = END
        checker = lambda current: self.interface.ibsta() & 8192

        reader = lambda: self.interface.read(count)

        return self._read(reader, count, checker, False, None, False, gpib.GpibError)

    def write(self, data):
        """Writes data to device or interface synchronously.

        Corresponds to viWrite function of the VISA library.

        :param data: data to be written.
        :type data: bytes
        :return: Number of bytes actually transferred, return value of the library call.
        :rtype: int, VISAStatus
        """

        logger.debug('GPIB.write %r' % data)

        try:
            self.interface.write(data)
            count = self.interface.ibcnt() # number of bytes transmitted

            return count, StatusCode.success

        except gpib.GpibError:
            # 0x4000 = 16384 = TIMO
            if self.interface.ibsta() & 16384:
                return 0, StatusCode.error_timeout
            else:
                return 0, StatusCode.error_system_error

    def clear(self):
        """Clears a device.

        Corresponds to viClear function of the VISA library.

        :param session: Unique logical identifier to a session.
        :return: return value of the library call.
        :rtype: :class:`pyvisa.constants.StatusCode`
        """

        logger.debug('GPIB.device clear')

        try:
            self.interface.clear()
            return 0, StatusCode.success
        except Exception:
            return 0, StatusCode.error_system_error

    def gpib_command(self, command_byte):
        """Write GPIB command byte on the bus.

        Corresponds to viGpibCommand function of the VISA library.
        See: https://linux-gpib.sourceforge.io/doc_html/gpib-protocol.html#REFERENCE-COMMAND-BYTES

        :param command_byte: command byte to send
        :type command_byte: int, must be [0 255]
        :return: return value of the library call
        :rtype: :class:`pyvisa.constants.StatusCode`
        """

        if 0 <= command_byte <= 255:
            data = chr(command_byte)
        else:
            return StatusCode.error_nonsupported_operation

        try:
            self.controller.command(data)
            return StatusCode.success

        except gpib.GpibError:
            return StatusCode.error_system_error

    def trigger(self, protocol):
        """Asserts hardware trigger.
        Only supports protocol = constants.VI_TRIG_PROT_DEFAULT

        :return: return value of the library call.
        :rtype: :class:`pyvisa.constants.StatusCode`
        """

        logger.debug('GPIB.device assert hardware trigger')

        try:
            if protocol == constants.VI_TRIG_PROT_DEFAULT:
                self.interface.trigger()
                return StatusCode.success
            else:
                return StatusCode.error_nonsupported_operation
        except gpib.GpibError:
            return StatusCode.error_system_error

    def gpib_send_ifc(self):
        """Pulse the interface clear line (IFC) for at least 100 microseconds.

        Corresponds to viGpibSendIFC function of the VISA library.

        :param session: Unique logical identifier to a session.
        :return: return value of the library call.
        :rtype: :class:`pyvisa.constants.StatusCode`
        """

        logger.debug('GPIB.interface clear')

        try:
            self.controller.interface_clear()
            return 0, StatusCode.success
        except:
            return 0, StatusCode.error_system_error

    def _get_attribute(self, attribute):
        """Get the value for a given VISA attribute for this session.

        Use to implement custom logic for attributes.

        :param attribute: Resource attribute for which the state query is made
        :return: The state of the queried attribute for a specified resource, return value of the library call.
        :rtype: (unicode | str | list | int, VISAStatus)
        """

        if attribute == constants.VI_ATTR_GPIB_READDR_EN:
            # IbaREADDR 0x6
            # Setting has no effect in linux-gpib.
            return self.interface.ask(6), StatusCode.success

        elif attribute == constants.VI_ATTR_GPIB_PRIMARY_ADDR:
            # IbaPAD 0x1
            return self.interface.ask(1), StatusCode.success

        elif attribute == constants.VI_ATTR_GPIB_SECONDARY_ADDR:
            # IbaSAD 0x2
            # Remove 0x60 because National Instruments.
            sad = self.interface.ask(2)
            if self.interface.ask(2):
                return self.interface.ask(2) - 96, StatusCode.success
            else:
                return constants.VI_NO_SEC_ADDR, StatusCode.success

        elif attribute == constants.VI_ATTR_GPIB_REN_STATE:
            # I have no idea how to implement this.
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_UNADDR_EN:
            # IbaUnAddr 0x1b
            if self.interface.ask(27):
                return constants.VI_TRUE, StatusCode.success
            else:
                return constants.VI_FALSE, StatusCode.success

        elif attribute == constants.VI_ATTR_SEND_END_EN:
            # IbaEndBitIsNormal 0x1a
            if self.interface.ask(26):
                return constants.VI_TRUE, StatusCode.success
            else:
                return constants.VI_FALSE, StatusCode.success

        elif attribute == constants.VI_ATTR_INTF_NUM:
            # IbaBNA 0x200
            return self.interface.ask(512), StatusCode.success

        elif attribute == constants.VI_ATTR_INTF_TYPE:
            return constants.InterfaceType.gpib, StatusCode.success

        raise UnknownAttribute(attribute)

    def _set_attribute(self, attribute, attribute_state):
        """Sets the state of an attribute.

        Corresponds to viSetAttribute function of the VISA library.

        :param attribute: Attribute for which the state is to be modified. (Attributes.*)
        :param attribute_state: The state of the attribute to be set for the specified object.
        :return: return value of the library call.
        :rtype: VISAStatus
        """

        if attribute == constants.VI_ATTR_GPIB_READDR_EN:
            # IbcREADDR 0x6
            # Setting has no effect in linux-gpib.
            if isinstance(attribute_state, int):
                self.interface.config(6, attribute_state)
                return StatusCode.success
            else:
                return StatusCode.error_nonsupported_attribute_state

        elif attribute == constants.VI_ATTR_GPIB_PRIMARY_ADDR:
            # IbcPAD 0x1
            if isinstance(attribute_state, int) and 0 <= attribute_state <= 30:
                self.interface.config(1, attribute_state)
                return StatusCode.success
            else:
                return StatusCode.error_nonsupported_attribute_state

        elif attribute == constants.VI_ATTR_GPIB_SECONDARY_ADDR:
            # IbcSAD 0x2
            # Add 0x60 because National Instruments.
            if isinstance(attribute_state, int) and 0 <= attribute_state <= 30:
                if self.interface.ask(2):
                    self.interface.config(2, attribute_state + 96)
                    return StatusCode.success
                else:
                    return StatusCode.error_nonsupported_attribute
            else:
                return StatusCode.error_nonsupported_attribute_state

        elif attribute == constants.VI_ATTR_GPIB_UNADDR_EN:
            # IbcUnAddr 0x1b
            try:
                self.interface.config(27, attribute_state)
                return StatusCode.success
            except gpib.GpibError:
                return StatusCode.error_nonsupported_attribute_state

        elif attribute == constants.VI_ATTR_SEND_END_EN:
            # IbcEndBitIsNormal 0x1a
            if isinstance(attribute_state, int):
                self.interface.config(26, attribute_state)
                return StatusCode.success
            else:
                return StatusCode.error_nonsupported_attribute_state

        raise UnknownAttribute(attribute)

    def read_stb(self):
        return self.interface.serial_poll()
