# -*- coding: utf-8 -*-
"""
    pyvisa-py.serial
    ~~~~~~~~~~~~~~~~

    GPIB Session implementation using linux-gpib.


    :copyright: 2015 by PyVISA-py Authors, see AUTHORS for more details.
    :license: MIT, see LICENSE for more details.
"""

from __future__ import division, unicode_literals, print_function, absolute_import
from bisect import bisect
from re import findall

from pyvisa import constants, attributes, logger

from .sessions import Session, UnknownAttribute
from . import common

try:
    import gpib
    from Gpib import Gpib
except ImportError as e:
    Session.register_unavailable(constants.InterfaceType.gpib, 'INSTR',
                                 'Please install linux-gpib to use this resource type.\n%s' % e)
    def gpib(*args, **kwargs):
        raise ValueError('Please install linux-gpib to use this resource type')

    Gpib = gpib
    
    raise


StatusCode = constants.StatusCode
SUCCESS = StatusCode.success
# linux-gpib timeout constants, in milliseconds. See self.timeout.
TIMETABLE = (0, 1e-2, 3e-2, 1e-1, 3e-1, 1e0, 3e0, 1e1, 3e1, 1e2, 3e2, 1e3, 3e3,
             1e4, 3e4, 1e5, 3e5, 1e6)


# TODO: Check board indices other than 0.
# TODO: Check secondary addresses.
@Session.register(constants.InterfaceType.gpib, 'INSTR')
class GPIBSession(Session):
    """A GPIB Session that uses linux-gpib to do the low level communication.
    """

    @staticmethod
    def list_resources():
        def find_listeners():
            for i in range(1, 31):
                if gpib.listener(0, i):
                    yield i

        return ['GPIB0::%d::INSTR' % pad for pad in find_listeners()]

    def after_parsing(self):
        minor = self.parsed['board']
        pad = self.parsed['primary_address']
        handle = gpib.dev(int(minor), int(pad))
        self.interface = Gpib(handle)

    @property
    def timeout(self):
        # 0x3 is the hexadecimal reference to the IbaTMO (timeout) configuration
        # option in linux-gpib.
        return TIMETABLE[self.interface.ask(3)]

    @timeout.setter
    def timeout(self, value):
        """linux-gpib only supports 18 discrete timeout values. If a timeout
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

        :param value: Requested timeout value in milliseconds
        """
        self.interface.timeout(bisect(TIMETABLE, value))

    def close(self):
        # Closes a Gpib object. The Gpib object has no native close() function,
        # so we need to use the lower level gpib.close() instead. gpib.close()
        # takes the device handle which was used to initialize the Gpib object as
        # its argument, so we use a regular expression to extract that device
        # handle from the Gpib object.
        handle = int(findall(r'\d+', repr(self.interface))[0])
        gpib.close(handle)

    def read(self, count):
        """Reads data from device or interface synchronously.

        Corresponds to viRead function of the VISA library.

        :param count: Number of bytes to be read.
        :return: data read, return value of the library call.
        :rtype: bytes, constants.StatusCode
        """

        # 0x2000 = 8192 = END
        checker = lambda current: self.interface.ibsta() & 8192

        reader = lambda: self.interface.read(1)

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

    # TODO: Implement some useful attributes.
    def _get_attribute(self, attribute):
        """Get the value for a given VISA attribute for this session.

        Use to implement custom logic for attributes.

        :param attribute: Resource attribute for which the state query is made
        :return: The state of the queried attribute for a specified resource, return value of the library call.
        :rtype: (unicode | str | list | int, VISAStatus)
        """

        if attribute == constants.VI_ATTR_GPIB_READDR_EN:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_ATN_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_ADDR_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_CIC_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_NDAC_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_SRQ_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_SYS_CNTRL_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_HS488_CBL_LEN:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_PRIMARY_ADDR:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_SECONDARY_ADDR:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_REN_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_UNADDR_EN:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_RECV_CIC_STATE:
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

        if attribute == constants.VI_ATTR_GPIB_READDR_EN:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_ATN_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_ADDR_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_CIC_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_NDAC_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_SRQ_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_SYS_CNTRL_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_HS488_CBL_LEN:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_PRIMARY_ADDR:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_SECONDARY_ADDR:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_REN_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_UNADDR_EN:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_RECV_CIC_STATE:
            raise NotImplementedError

        raise UnknownAttribute(attribute)

