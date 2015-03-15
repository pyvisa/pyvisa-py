# -*- coding: utf-8 -*-
"""
    pyvisa-py.serial
    ~~~~~~~~~~~~~~~~

    GPIB Session implementation using linux-gpib.


    :copyright: 2015 by PyVISA-py Authors, see AUTHORS for more details.
    :license: MIT, see LICENSE for more details.
"""

from __future__ import division, unicode_literals, print_function, absolute_import

from pyvisa import constants, attributes, logger

from .sessions import Session, UnknownAttribute
from . import common

try:
    import gpib
    from Gpib import Gpib
except ImportError:
    raise ValueError('Please install linux-gpib to use this resource type')


StatusCode = constants.StatusCode
SUCCESS = StatusCode.success


# TODO: Check board indices other than 0.
# TODO: Check secondary addresses.
@Session.register(constants.InterfaceType.gpib, 'INSTR')
class GPIBSession(Session):
    """A GPIB Session that uses linux-gpib to do the low level communication.
    """

    @staticmethod
    def list_resources():
        def find_listeners():
            for i in range(1,31):
                if gpib.listener(0, i):
                    yield i

        return ['GPIB0::%d::INSTR' % listener for listener in find_listeners()]

    def after_parsing(self):
        # TODO: Make common.parse_resource_name return integers for GPIB sessions.
        handle = gpib.dev(int(self.parsed['board']), int(self.parsed['primary_address']))
        self.interface = Gpib(handle)

    @property
    def timeout(self):
        # 0x3 is the hexadecimal reference to the IbaTMO (timeout) configuration
        # option in linux-gpib.
        return self.interface.ask(3)

    @timeout.setter
    def timeout(self, value):
        self.interface.timeout(value)

    def close(self):
        self.interface.close()

    def read(self, count):
        """Reads data from device or interface synchronously.

        Corresponds to viRead function of the VISA library.

        :param count: Number of bytes to be read.
        :return: data read, return value of the library call.
        :rtype: bytes, constants.StatusCode
        """

        # TODO: This is mostly broken for some reason. Figure out why and fix it.
        # TODO: We probably want some kind of termination character logic here.
        reader = lambda: self.interface.read(1)

        checker = lambda current: False

        return self._read(reader, count, checker, False, None, False, gpib.GpibError)

    def write(self, data):
        """Writes data to device or interface synchronously.

        Corresponds to viWrite function of the VISA library.

        :param data: data to be written.
        :type data: bytes
        :return: Number of bytes actually transferred, return value of the library call.
        :rtype: int, VISAStatus
        """

        logger.debug('Serial.write %r' % data)

        try:
            # TODO: We may want some termination character logic here too.
            self.interface.write(data)

            return SUCCESS

        # TODO: linux-gpib only has a single generic exception, GpibError, so how
        #       can we tell what went wrong?
        except gpib.GpibError:
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
            return NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_ADDR_STATE:
            return NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_CIC_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_NDAC_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_SRQ_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_SYS_CNTRL_STATE:
            return NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_HS488_CBL_LEN:
            return NotImplementedError

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
            return NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_ADDR_STATE:
            return NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_CIC_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_NDAC_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_SRQ_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_SYS_CNTRL_STATE:
            return NotImplementedError

        elif attribute == constants.VI_ATTR_GPIB_HS488_CBL_LEN:
            return NotImplementedError

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
