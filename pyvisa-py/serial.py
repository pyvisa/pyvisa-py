# -*- coding: utf-8 -*-
"""
    pyvisa-py.serial
    ~~~~~~~~~~~~~~~~

    Serial Session implementation using PySerial.

    This file is part of PyVISA-py

    :copyright: 2014 by PyVISA-py Authors, see AUTHORS for more details.
    :license: MIT, see LICENSE for more details.
"""

import re

import serial

from pyvisa import constants

from .session import Session

def to_state(boolean_input):
    """Convert a boolean input into a LineState value
    """
    if boolean_input:
        return constants.LineState.asserted
    return constants.LineState.unasserted



#: Regular expression to match Serial resource names.
#: Defintion:  ASRLboard[::INSTR]
_RESOURCE_RE = re.compile('^ASRL(?P<board>[^\s:]+)'
                          '(::INSTR)?', re.I
                         )

from serial.tools.list_ports import comports

StatusCode = constants.StatusCode
SUCCESS = StatusCode.success
SerialTermination = constants.SerialTermination


class UnsupportedAttrError(ValueError):
    pass


class SerialSession(Session):
    """A serial Session that uses PySerial to do the low level communication.
    """

    @staticmethod
    def list_resources():
        return ['ASRL%s::INSTR' % port[0] for port in comports()]

    @staticmethod
    def parse_resource_name(resource_name):
        r = _RESOURCE_RE.match(resource_name)
        if r is None:
            raise ValueError('Is not a valid Serial Session resource name %s' % resource_name)
        return dict(board=r.group('board'),
                    interface_type=constants.InterfaceType.asrl,
                    resource_class='INSTR',
                    resource_name=resource_name)

    def __init__(self, resource_name):
        parsed = self.parse_resource_name(resource_name)
        super(SerialSession, self).__init__(parsed['resource_name'], parsed['resource_class'])
        port = parsed['board']
        self.internal = serial.Serial(port=port, timeout=2000, writeTimeout=2000)

        self.attrs = {constants.VI_ATTR_ASRL_END_IN: SerialTermination.termination_char}

    def _get_timeout(self):
        return self.internal.timeout

    def _set_timeout(self, value):
        self.internal.timeout = value
        self.internal.writeTimeout = value

    def close(self):
        self.internal.close()

    def read(self, count):
        """Reads data from device or interface synchronously.

        Corresponds to viRead function of the VISA library.

        :param session: Unique logical identifier to a session.
        :param count: Number of bytes to be read.
        :return: data read, return value of the library call.
        :rtype: bytes, VISAStatus
        """

        # TODO: Deal with end of line an other stuff
        end_in = self.attrs[constants.VI_ATTR_ASRL_END_IN]

        if end_in == SerialTermination.none:
            ret = self.internal.read(count)
            if len(ret) == count:
                return ret, StatusCode.success_max_count_read
            else:
                return ret, StatusCode.error_timeout

        elif end_in == SerialTermination.last_bit:
            ret = b''
            mask = 2 ** self.internal.bytesize
            while:
                ret += self.internal.read(1)
                if ret[-1] & mask:
                    # TODO: What is the correct success code??
                    return ret, SUCCESS
                #TODO: Should we stop here as well?
                if len(ret) == count:
                    return ret, StatusCode.success_max_count_read
                else:
                    return ret, StatusCode.error_timeout

        elif end_in == SerialTermination.term_char:
            ret = b''
            term_char = self.attrs[constants.VI_ASRL_END_TERMCHAR]
            while:
                ret += self.internal.read(1)
                if ret[-1] == term_char:
                    # TODO: What is the correct success code??
                    return ret, StatusCode.termination_char
                #TODO: Should we stop here as well?
                if len(ret) == count:
                    return ret, StatusCode.success_max_count_read
                else:
                    return ret, StatusCode.error_timeout

        else:
            raise ValueError('Unknown value for VI_ATTR_ASRL_END_IN: %d' % end_in)

    def write(self, data):
        """Writes data to device or interface synchronously.

        Corresponds to viWrite function of the VISA library.

        :param session: Unique logical identifier to a session.
        :param data: data to be written.
        :type data: str
        :return: Number of bytes actually transferred, return value of the library call.
        :rtype: int, VISAStatus
        """

        # TODO: How to deal with VI_ATTR_TERMCHAR_EN
        end_out = self.attrs[constants.VI_ATTR_ASRL_END_OUT]
        send_end = self.attrs[constants.VI_ATTR_SEND_END_EN]
        try:
            if end_out == SerialTermination.none:
                pass

            elif end_out == SerialTermination.last_bit:
                #: TODO unset last bit in data[:-1] and set last data[-1]

                data = data
            elif end_out == SerialTermination.termination_char:
                data = data + self.attrs[constants.VI_ASRL_END_TERMCHAR]

            count = self.internal.write(data)

            if end_out == SerialTermination.termination_break:
                #TODO: SEND BREAK
                pass

            return count, SUCCESS
        except SerialTimeoutException:
            return 0, StatusCode.error_timeout

    def get_attribute(self, attribute):
        """Retrieves the state of an attribute.

        Corresponds to viGetAttribute function of the VISA library.

        :param session: Unique logical identifier to a session, event, or find list.
        :param attribute: Resource attribute for which the state query is made (see Attributes.*)
        :return: The state of the queried attribute for a specified resource, return value of the library call.
        :rtype: unicode (Py2) or str (Py3), list or other type, VISAStatus
        """
        try:
            return self._get_attribute(attribute), SUCCESS
        except UnsupportedAttrError:
            return None, StatusCode.error_nonsupported_attribute
        except NotImplementedError:
            raise e

    def _get_attribute(self, attribute):

        if attribute == constants.VI_ATTR_ASRL_ALLOW_TRANSMIT:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_AVAIL_NUM:
            return self.internal.inWaiting()

        elif attribute == constants.VI_ATTR_ASRL_BAUD:
            return self.internal.baudrate

        elif attribute == constants.VI_ATTR_ASRL_BREAK_LEN:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_BREAK_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_CONNECTED:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_CTS_STATE:
            return to_state(self.internal.getCTS())

        elif attribute == constants.VI_ATTR_ASRL_DATA_BITS:
            return self.internal.bytesize

        elif attribute == constants.VI_ATTR_ASRL_DCD_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_DISCARD_NULL:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_DSR_STATE:
            return to_state(self.internal.getDSR())

        elif attribute == constants.VI_ATTR_ASRL_DTR_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_END_IN:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_END_OUT:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_FLOW_CNTRL:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_PARITY:
            parity = self.internal.parity
            if parity == serial.PARITY_NONE:
                return constants.Parity.none
            elif parity == serial.PARITY_EVEN:
                return constants.Parity.even
            elif parity == serial.PARITY_ODD:
                return constants.Parity.odd
            elif parity == serial.PARITY_MARK:
                return constants.Parity.mark
            elif parity == serial.PARITY_SPACE:
                return constants.Parity.space

            raise Exception('Unknown parity value: %r' % parity)

        elif attribute == constants.VI_ATTR_ASRL_RI_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_RTS_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_STOP_BITS:
            bits = self.internal.stopbits
            if bits == serial.STOPBITS_ONE:
                return constants.StopBits.one
            elif bits == serial.STOPBITS_ONE_POINT_FIVE:
                return constants.StopBits.one_and_a_half
            elif bits == serial.STOPBITS_TWO:
                return constants.StopBits.two

            raise Exception('Unknown bits value: %r' % bits)

        elif attribute == constants.VI_ATTR_ASRL_XOFF_CHAR:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_INTF_TYPE:
            return constants.InterfaceType.asrl

        elif attribute == constants.VI_ATTR_SEND_END_EN:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_SUPPRESS_END_EN:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_TERMCHAR:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_TERMCHAR_EN:
            raise NotImplementedError

        raise ValueError

    def set_attribute(self, attribute, attribute_state):
        """Sets the state of an attribute.

        Corresponds to viSetAttribute function of the VISA library.

        :param session: Unique logical identifier to a session.
        :param attribute: Attribute for which the state is to be modified. (Attributes.*)
        :param attribute_state: The state of the attribute to be set for the specified object.
        :return: return value of the library call.
        :rtype: VISAStatus
        """
        try:
            return SUCCESS
        except UnsupportedAttrError:
            return StatusCode.error_nonsupported_attribute
        except ValueError:
            return StatusCode.error_nonsupported_attribute_state
        except NotImplementedError:
            raise e

    def _set_attribute(self, attribute, attribute_state):
        """Sets the state of an attribute.

        Corresponds to viSetAttribute function of the VISA library.

        :param session: Unique logical identifier to a session.
        :param attribute: Attribute for which the state is to be modified. (Attributes.*)
        :param attribute_state: The state of the attribute to be set for the specified object.
        :return: return value of the library call.
        :rtype: VISAStatus
        """

        if attribute == constants.VI_ATTR_ASRL_ALLOW_TRANSMIT:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_BAUD:
            self.internal.baudrate = attribute_state
            return Success

        elif attribute == constants.VI_ATTR_ASRL_BREAK_LEN:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_BREAK_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_CONNECTED:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_DATA_BITS:
            self.internal.bytesize = attribute_state

        elif attribute == constants.VI_ATTR_ASRL_DCD_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_DISCARD_NULL:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_DSR_STATE:
            return to_state(self.internal.getDSR())

        elif attribute == constants.VI_ATTR_ASRL_DTR_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_END_IN:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_END_OUT:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_FLOW_CNTRL:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_PARITY:
            if attribute_state == constants.Parity.none:
                self.internal.parity = serial.PARITY_NONE
                return StatusCode.success

            elif attribute_state == constants.Parity.even:
                self.internal.parity = serial.PARITY_EVEN
                return StatusCode.success

            elif attribute_state == constants.Parity.odd:
                self.internal.parity = serial.PARITY_ODD
                return StatusCode.success

            elif attribute_state == serial.PARITY_MARK:
                self.internal.parity = serial.PARITY_MARK
                return StatusCode.success

            elif attribute_state == constants.Parity.space:
                self.internal.parity = serial.PARITY_SPACE
                return StatusCode.success

            return StatusCode.error_nonsupported_attribute_state

        elif attribute == constants.VI_ATTR_ASRL_RI_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_RTS_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_STOP_BITS:
            bits = self.internal.stopbits
            if bits == serial.STOPBITS_ONE:
                return constants.StopBits.one
            elif bits == serial.STOPBITS_ONE_POINT_FIVE:
                return constants.StopBits.one_and_a_half
            elif bits == serial.STOPBITS_TWO:
                return constants.StopBits.two

            raise Exception('Unknown bits value: %r' % bits)

        elif attribute == constants.VI_ATTR_ASRL_XOFF_CHAR:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_SEND_END_EN:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_SUPPRESS_END_EN:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_TERMCHAR:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_TERMCHAR_EN:
            raise NotImplementedError

        raise UnsupportedAttrError
