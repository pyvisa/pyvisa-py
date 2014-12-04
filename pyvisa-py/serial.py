# -*- coding: utf-8 -*-
"""
    pyvisa-py.serial
    ~~~~~~~~~~~~~~~~

    Serial Session implementation using PySerial.


    :copyright: 2014 by PyVISA-py Authors, see AUTHORS for more details.
    :license: MIT, see LICENSE for more details.
"""

from __future__ import division, unicode_literals, print_function, absolute_import

from pyvisa import constants, attributes

from .sessions import Session
from . import common

try:
    import serial
    from serial.tools.list_ports import comports
except ImportError as e:
    Session.register_unavailable(constants.InterfaceType.asrl, 'INSTR',
                                 'Please install PySerial to use this resource type.\n%s' % e)
    raise


def to_state(boolean_input):
    """Convert a boolean input into a LineState value
    """
    if boolean_input:
        return constants.LineState.asserted
    return constants.LineState.unasserted


StatusCode = constants.StatusCode
SUCCESS = StatusCode.success
SerialTermination = constants.SerialTermination


@Session.register(constants.InterfaceType.asrl, 'INSTR')
class SerialSession(Session):
    """A serial Session that uses PySerial to do the low level communication.
    """

    @staticmethod
    def list_resources():
        return ['ASRL%s::INSTR' % port[0] for port in comports()]

    @classmethod
    def get_low_level_info(cls):
        try:
            ver = '.'.join(serial.version)
        except AttributeError:
            ver = 'N/A'

        return 'via PySerial (%s)' % ver

    def after_parsing(self):
        self.interface = serial.Serial(port=self.parsed['board'], timeout=2000, writeTimeout=2000)

        for name in 'ASRL_END_IN,ASRL_END_OUT,SEND_END_EN,TERMCHAR,' \
                    'TERMCHAR_EN'.split(','):
            attribute = getattr(constants, 'VI_ATTR_' + name)
            self.attrs[attribute] = attributes.AttributesByID[attribute].default

    @property
    def timeout(self):
        return self.interface.timeout

    @timeout.setter
    def timeout(self, value):
        self.interface.timeout = value
        self.interface.writeTimeout = value

    def close(self):
        self.interface.close()

    def read(self, count):
        """Reads data from device or interface synchronously.

        Corresponds to viRead function of the VISA library.

        :param count: Number of bytes to be read.
        :return: data read, return value of the library call.
        :rtype: bytes, VISAStatus
        """

        end_in = self.get_attribute(constants.VI_ATTR_ASRL_END_IN)

        if end_in == SerialTermination.none:
            ret = self.interface.read(count)
            if len(ret) == count:
                return ret, StatusCode.success_max_count_read
            else:
                return ret, StatusCode.error_timeout

        elif end_in == SerialTermination.last_bit:
            ret = b''
            mask = 2 ** self.interface.bytesize
            while True:
                ret += self.interface.read(1)
                if common.last_int(ret) & mask:
                    # TODO: What is the correct success code??
                    return ret, StatusCode.success

                #TODO: Should we stop here as well?
                if len(ret) == count:
                    return ret, StatusCode.success_max_count_read
                else:
                    return ret, StatusCode.error_timeout

        elif end_in == SerialTermination.term_char:
            ret = b''
            term_char = self.get_attribute(constants.VI_ASRL_END_TERMCHAR)
            while True:
                ret += self.interface.read(1)
                if ret[-1:] == term_char:
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

        :param data: data to be written.
        :type data: str
        :return: Number of bytes actually transferred, return value of the library call.
        :rtype: int, VISAStatus
        """

        # TODO: How to deal with VI_ATTR_TERMCHAR_EN
        end_out, _ = self.get_attribute(constants.VI_ATTR_ASRL_END_OUT)
        send_end, _ = self.get_attribute(constants.VI_ATTR_SEND_END_EN)

        try:
            if end_out == SerialTermination.none:
                pass

            elif end_out == SerialTermination.last_bit:
                last_bit, _ = self.get_attribute(constants.VI_ATTR_ASRL_DATA_BITS)
                mask = 1 << (last_bit - 1)
                data = common.iter_bytes(data, mask, send_end)

            elif end_out == SerialTermination.termination_char:
                term_char, _ = self.get_attribute(constants.VI_ASRL_END_TERMCHAR)
                data = data + term_char

            count = 0
            for d in data:
                count += self.interface.write(d)

            if end_out == SerialTermination.termination_break:
                self.interface.sendBreak()

            return count, constants.StatusCode.success

        except serial.SerialTimeoutException:
            return 0, StatusCode.error_timeout

    def _get_attribute(self, attribute):

        if attribute == constants.VI_ATTR_ASRL_ALLOW_TRANSMIT:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_AVAIL_NUM:
            return self.interface.inWaiting()

        elif attribute == constants.VI_ATTR_ASRL_BAUD:
            return self.interface.baudrate

        elif attribute == constants.VI_ATTR_ASRL_BREAK_LEN:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_BREAK_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_CONNECTED:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_CTS_STATE:
            return to_state(self.interface.getCTS())

        elif attribute == constants.VI_ATTR_ASRL_DATA_BITS:
            return self.interface.bytesize

        elif attribute == constants.VI_ATTR_ASRL_DCD_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_DISCARD_NULL:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_DSR_STATE:
            return to_state(self.interface.getDSR())

        elif attribute == constants.VI_ATTR_ASRL_DTR_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_FLOW_CNTRL:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_PARITY:
            parity = self.interface.parity
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
            bits = self.interface.stopbits
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

        elif attribute == constants.VI_ATTR_SUPPRESS_END_EN:
            raise NotImplementedError

        raise Exception('Unknown attribute %s' % attribute)

    def _set_attribute(self, attribute, attribute_state):

        if attribute == constants.VI_ATTR_ASRL_ALLOW_TRANSMIT:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_BAUD:
            self.interface.baudrate = attribute_state
            return StatusCode.success

        elif attribute == constants.VI_ATTR_ASRL_BREAK_LEN:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_BREAK_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_CONNECTED:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_DATA_BITS:
            self.interface.bytesize = attribute_state

        elif attribute == constants.VI_ATTR_ASRL_DCD_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_DISCARD_NULL:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_DSR_STATE:
            return to_state(self.interface.getDSR())

        elif attribute == constants.VI_ATTR_ASRL_DTR_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_FLOW_CNTRL:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_PARITY:
            if attribute_state == constants.Parity.none:
                self.interface.parity = serial.PARITY_NONE
                return StatusCode.success

            elif attribute_state == constants.Parity.even:
                self.interface.parity = serial.PARITY_EVEN
                return StatusCode.success

            elif attribute_state == constants.Parity.odd:
                self.interface.parity = serial.PARITY_ODD
                return StatusCode.success

            elif attribute_state == serial.PARITY_MARK:
                self.interface.parity = serial.PARITY_MARK
                return StatusCode.success

            elif attribute_state == constants.Parity.space:
                self.interface.parity = serial.PARITY_SPACE
                return StatusCode.success

            return StatusCode.error_nonsupported_attribute_state

        elif attribute == constants.VI_ATTR_ASRL_RI_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_RTS_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_STOP_BITS:
            bits = self.interface.stopbits
            if bits == serial.STOPBITS_ONE:
                return constants.StopBits.one
            elif bits == serial.STOPBITS_ONE_POINT_FIVE:
                return constants.StopBits.one_and_a_half
            elif bits == serial.STOPBITS_TWO:
                return constants.StopBits.two

            raise Exception('Unknown bits value: %r' % bits)

        elif attribute == constants.VI_ATTR_ASRL_XOFF_CHAR:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_SUPPRESS_END_EN:
            raise NotImplementedError

        raise Exception('Unknown attribute %s' % attribute)
