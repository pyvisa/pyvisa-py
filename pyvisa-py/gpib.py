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

from pyvisa import constants, logger

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
SUCCESS = StatusCode.success

# linux-gpib timeout constants, in milliseconds. See self.timeout.
TIMETABLE = (0, 1e-2, 3e-2, 1e-1, 3e-1, 1e0, 3e0, 1e1, 3e1, 1e2, 3e2, 1e3, 3e3,
             1e4, 3e4, 1e5, 3e5, 1e6)


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
        minor = self.parsed.board
        pad = self.parsed.primary_address
        self.handle = gpib.dev(int(minor), int(pad))
        self.interface = Gpib(self.handle)

    @property
    def timeout(self):

        # 0x3 is the hexadecimal reference to the IbaTMO (timeout) configuration
        # option in linux-gpib.
        return TIMETABLE[self.interface.ask(3)]

    @timeout.setter
    def timeout(self, value):

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
        self.interface.timeout(bisect(TIMETABLE, value))

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

            return SUCCESS

        except gpib.GpibError:
            # 0x4000 = 16384 = TIMO
            if self.interface.ibsta() & 16384:
                return 0, StatusCode.error_timeout
            else:
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
            return self.interface.ask(6), SUCCESS

        elif attribute == constants.VI_ATTR_GPIB_PRIMARY_ADDR:
            # IbaPAD 0x1
            return self.interface.ask(1), SUCCESS

        elif attribute == constants.VI_ATTR_GPIB_SECONDARY_ADDR:
            # IbaSAD 0x2
            # Remove 0x60 because National Instruments.
            sad = self.interface.ask(2)
            if self.interface.ask(2):
                return self.interface.ask(2) - 96, SUCCESS
            else:
                return constants.VI_NO_SEC_ADDR, SUCCESS

        elif attribute == constants.VI_ATTR_GPIB_REN_STATE:
            # I have no idea how to implement this.
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_UNADDR_EN:
            # IbaUnAddr 0x1b
            if self.interface.ask(27):
                return constants.VI_TRUE, SUCCESS
            else:
                return constants.VI_FALSE, SUCCESS

        elif attribute == constants.VI_ATTR_SEND_END_EN:
            # IbaEndBitIsNormal 0x1a
            if self.interface.ask(26):
                return constants.VI_TRUE, SUCCESS
            else:
                return constants.VI_FALSE, SUCCESS

        elif attribute == constants.VI_ATTR_INTF_NUM:
            # IbaBNA 0x200
            return self.interface.ask(512), SUCCESS

        elif attribute == constants.VI_ATTR_INTF_TYPE:
            return constants.InterfaceType.gpib, SUCCESS

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
                return SUCCESS
            else:
                return StatusCode.error_nonsupported_attribute_state

        elif attribute == constants.VI_ATTR_GPIB_PRIMARY_ADDR:
            # IbcPAD 0x1
            if isinstance(attribute_state, int) and 0 <= attribute_state <= 30:
                self.interface.config(1, attribute_state)
                return SUCCESS
            else:
                return StatusCode.error_nonsupported_attribute_state

        elif attribute == constants.VI_ATTR_GPIB_SECONDARY_ADDR:
            # IbcSAD 0x2
            # Add 0x60 because National Instruments.
            if isinstance(attribute_state, int) and 0 <= attribute_state <= 30:
                if self.interface.ask(2):
                    self.interface.config(2, attribute_state + 96)
                    return SUCCESS
                else:
                    return StatusCode.error_nonsupported_attribute
            else:
                return StatusCode.error_nonsupported_attribute_state

        elif attribute == constants.VI_ATTR_GPIB_UNADDR_EN:
            # IbcUnAddr 0x1b
            try:
                self.interface.config(27, attribute_state)
                return SUCCESS
            except gpib.GpibError:
                return StatusCode.error_nonsupported_attribute_state

        elif attribute == constants.VI_ATTR_SEND_END_EN:
            # IbcEndBitIsNormal 0x1a
            if isinstance(attribute_state, int):
                self.interface.config(26, attribute_state)
                return SUCCESS
            else:
                return StatusCode.error_nonsupported_attribute_state

        raise UnknownAttribute(attribute)

