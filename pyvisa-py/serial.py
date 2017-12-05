# -*- coding: utf-8 -*-
"""
    pyvisa-py.serial
    ~~~~~~~~~~~~~~~~

    Serial Session implementation using PySerial.


    :copyright: 2014 by PyVISA-py Authors, see AUTHORS for more details.
    :license: MIT, see LICENSE for more details.
"""

from __future__ import division, unicode_literals, print_function, absolute_import

from pyvisa import constants, attributes, logger

from .sessions import Session, UnknownAttribute
from . import common

try:
    import serial
    from serial.tools.list_ports import comports
except ImportError as e:
    Session.register_unavailable(constants.InterfaceType.asrl, 'INSTR',
                                 'Please install PySerial (>=3.0) to use this resource type.\n%s' % e)
    raise


def to_state(boolean_input):
    """Convert a boolean input into a LineState value
    """
    if boolean_input:
        return constants.LineState.asserted
    return constants.LineState.unasserted


StatusCode = constants.StatusCode
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
            ver = serial.VERSION
        except AttributeError:
            ver = 'N/A'

        return 'via PySerial (%s)' % ver

    def after_parsing(self):
        if 'mock' in self.parsed:
            cls = self.parsed.mock
        else:
            cls = serial.Serial

        self.interface = cls(port=self.parsed.board, timeout=self.timeout, write_timeout=self.timeout)

        for name in ('ASRL_END_IN', 'ASRL_END_OUT', 'SEND_END_EN', 'TERMCHAR',
                    'TERMCHAR_EN', 'SUPPRESS_END_EN'):
            attribute = getattr(constants, 'VI_ATTR_' + name)
            self.attrs[attribute] = attributes.AttributesByID[attribute].default

    def _get_timeout(self, attribute):
        if self.interface:
            self.timeout = self.interface.timeout
        return super(SerialSession, self)._get_timeout(attribute)

    def _set_timeout(self, attribute, value):
        status = super(SerialSession, self)._set_timeout(attribute, value)
        if self.interface:
            self.interface.timeout = self.timeout
            self.interface.write_timeout = self.timeout
        return status

    def close(self):
        self.interface.close()

    def read(self, count):
        """Reads data from device or interface synchronously.

        Corresponds to viRead function of the VISA library.

        :param count: Number of bytes to be read.
        :return: data read, return value of the library call.
        :rtype: bytes, constants.StatusCode
        """

        end_in, _ = self.get_attribute(constants.VI_ATTR_ASRL_END_IN)
        suppress_end_en, _ = self.get_attribute(constants.VI_ATTR_SUPPRESS_END_EN)

        reader = lambda: self.interface.read(1)

        if end_in == SerialTermination.none:
            checker = lambda current: False

        elif end_in == SerialTermination.last_bit:
            mask = 2 ** self.interface.bytesize
            checker = lambda current: bool(common.last_int(current) & mask)

        elif end_in == SerialTermination.termination_char:
            end_char, _ = self.get_attribute(constants.VI_ATTR_TERMCHAR)

            checker = lambda current: common.last_int(current) == end_char

        else:
            raise ValueError('Unknown value for VI_ATTR_ASRL_END_IN: %s' % end_in)

        return self._read(reader, count, checker, suppress_end_en, None, False,
                          serial.SerialTimeoutException)

    def write(self, data):
        """Writes data to device or interface synchronously.

        Corresponds to viWrite function of the VISA library.

        :param data: data to be written.
        :type data: bytes
        :return: Number of bytes actually transferred, return value of the library call.
        :rtype: int, VISAStatus
        """
        logger.debug('Serial.write %r' % data)
        # TODO: How to deal with VI_ATTR_TERMCHAR_EN
        end_out, _ = self.get_attribute(constants.VI_ATTR_ASRL_END_OUT)
        send_end, _ = self.get_attribute(constants.VI_ATTR_SEND_END_EN)

        try:
            # We need to wrap data in common.iter_bytes to Provide Python 2 and 3 compatibility

            if end_out in (SerialTermination.none, SerialTermination.termination_break):
                data = common.iter_bytes(data)

            elif end_out == SerialTermination.last_bit:
                last_bit, _ = self.get_attribute(constants.VI_ATTR_ASRL_DATA_BITS)
                mask = 1 << (last_bit - 1)
                data = common.iter_bytes(data, mask, send_end)

            elif end_out == SerialTermination.termination_char:
                term_char, _ = self.get_attribute(constants.VI_ATTR_TERMCHAR)
                data = common.iter_bytes(data + common.int_to_byte(term_char))

            else:
                raise ValueError('Unknown value for VI_ATTR_ASRL_END_OUT: %s' % end_out)

            count = 0
            for d in data:
                count += self.interface.write(d)

            if end_out == SerialTermination.termination_break:
                logger.debug('Serial.sendBreak')
                self.interface.sendBreak()

            return count, StatusCode.success

        except serial.SerialTimeoutException:
            return 0, StatusCode.error_timeout

    def _get_attribute(self, attribute):
        """Get the value for a given VISA attribute for this session.

        Use to implement custom logic for attributes.

        :param attribute: Resource attribute for which the state query is made
        :return: The state of the queried attribute for a specified resource, return value of the library call.
        :rtype: (unicode | str | list | int, VISAStatus)
        """

        if attribute == constants.VI_ATTR_ASRL_ALLOW_TRANSMIT:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_AVAIL_NUM:
            return self.interface.inWaiting(), StatusCode.success

        elif attribute == constants.VI_ATTR_ASRL_BAUD:
            return self.interface.baudrate, StatusCode.success

        elif attribute == constants.VI_ATTR_ASRL_BREAK_LEN:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_BREAK_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_CONNECTED:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_CTS_STATE:
            return to_state(self.interface.getCTS()), StatusCode.success

        elif attribute == constants.VI_ATTR_ASRL_DATA_BITS:
            return self.interface.bytesize, StatusCode.success

        elif attribute == constants.VI_ATTR_ASRL_DCD_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_DISCARD_NULL:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_DSR_STATE:
            return to_state(self.interface.getDSR()), StatusCode.success

        elif attribute == constants.VI_ATTR_ASRL_DTR_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_FLOW_CNTRL:
            return (self.interface.xonxoff * constants.VI_ASRL_FLOW_XON_XOFF |
                    self.interface.rtscts * constants.VI_ASRL_FLOW_RTS_CTS |
                    self.interface.dsrdtr * constants.VI_ASRL_FLOW_DTR_DSR), StatusCode.success

        elif attribute == constants.VI_ATTR_ASRL_PARITY:
            parity = self.interface.parity
            if parity == serial.PARITY_NONE:
                return constants.Parity.none, StatusCode.success
            elif parity == serial.PARITY_EVEN:
                return constants.Parity.even, StatusCode.success
            elif parity == serial.PARITY_ODD:
                return constants.Parity.odd, StatusCode.success
            elif parity == serial.PARITY_MARK:
                return constants.Parity.mark, StatusCode.success
            elif parity == serial.PARITY_SPACE:
                return constants.Parity.space, StatusCode.success

            raise Exception('Unknown parity value: %r' % parity)

        elif attribute == constants.VI_ATTR_ASRL_RI_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_RTS_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_STOP_BITS:
            bits = self.interface.stopbits
            if bits == serial.STOPBITS_ONE:
                return constants.StopBits.one, StatusCode.success
            elif bits == serial.STOPBITS_ONE_POINT_FIVE:
                return constants.StopBits.one_and_a_half, StatusCode.success
            elif bits == serial.STOPBITS_TWO:
                return constants.StopBits.two, StatusCode.success

            raise Exception('Unknown bits value: %r' % bits)

        elif attribute == constants.VI_ATTR_ASRL_XOFF_CHAR:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_INTF_TYPE:
            return constants.InterfaceType.asrl, StatusCode.success

        raise UnknownAttribute(attribute)

    def _set_attribute(self, attribute, attribute_state):
        """Sets the state of an attribute.

        Corresponds to viSetAttribute function of the VISA library.

        :param attribute: Attribute for which the state is to be modified. (Attributes.*)
        :param attribute_state: The state of the attribute to be set for the specified object.
        :return: return value of the library call.
        :rtype: VISAStatus
        """

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
            return StatusCode.success

        elif attribute == constants.VI_ATTR_ASRL_DCD_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_DISCARD_NULL:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_DSR_STATE:
            return to_state(self.interface.getDSR())

        elif attribute == constants.VI_ATTR_ASRL_DTR_STATE:
            raise NotImplementedError

        elif attribute == constants.VI_ATTR_ASRL_FLOW_CNTRL:
            if not isinstance(attribute_state, int):
                return StatusCode.error_nonsupported_attribute_state

            if not 0 < attribute_state < 8:
                return StatusCode.error_nonsupported_attribute_state

            try:
                self.interface.xonxoff = attribute_state & constants.VI_ASRL_FLOW_XON_XOFF
                self.interface.rtscts = attribute_state & constants.VI_ASRL_FLOW_RTS_CTS
                self.interface.dsrdtr = attribute_state & constants.VI_ASRL_FLOW_DTR_DSR
                return StatusCode.success
            except:
                return StatusCode.error_nonsupported_attribute_state

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

        raise UnknownAttribute(attribute)
