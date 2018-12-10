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

    # patch Gpib to avoid double closing of handles
    def _patch_Gpib():
        if not hasattr(Gpib, "close"):
            _old_del = Gpib.__del__

            def _inner(self):
                _old_del(self)
                self._own = False
            Gpib.__del__ = _inner
            Gpib.close = _inner
    _patch_Gpib()
except ImportError as e:
    Session.register_unavailable(constants.InterfaceType.gpib, 'INSTR',
                                 'Please install linux-gpib to use this resource type.\n%s' % e)
    raise


class GPIBEvent(object):
    """A simple event object that holds event attributes.
    """

    def __init__(self, attrs):
        """Initialize GPIBEvent with an attribute dictionary.

        :param attrs: Event attributes to be stored.
        :type attrs: dict or dictionary-like
        """
        self.attrs = {attribute: value for attribute, value in attrs.items()}

    def __del__(self):
        self.close()

    def get_attribute(self, attr):
        """Retrieves the state of an attribute.

        Corresponds to viGetAttribute function of the VISA library for this particular event.

        :param attribute: Event attribute for which the state query is made (see Attributes.*)
        :return: The state of the queried attribute, return value describing success.
        :rtype: unicode | str | list | int, VISAStatus
        """
        try:
            return self.attrs[attr], StatusCode.success
        except KeyError:
            return None, StatusCode.error_nonsupported_attribute

    def close(self):
        """Closes the event.

        Corresponds to viClose function of the VISA library.

        :return: return value of the library call.
        :rtype: VISAStatus
        """
        return StatusCode.success


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
        self.interface = Gpib(name=minor, pad=pad, sad=sad,
                              timeout=timeout, send_eoi=send_eoi, eos_mode=eos_mode)
        self.controller = Gpib(name=minor)  # this is the bus controller device
        # force timeout setting to interface
        self.set_attribute(constants.VI_ATTR_TMO_VALUE,
                           attributes.AttributesByID[constants.VI_ATTR_TMO_VALUE].default)

        # prepare set of allowed events
        self.valid_event_types = {constants.VI_EVENT_IO_COMPLETION,
                                  constants.VI_EVENT_SERVICE_REQ}

        self.enabled_queue_events = set()

        self.event_queue = []

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
        self.interface.close()
        self.controller.close()

    def read(self, count):
        """Reads data from device or interface synchronously.

        Corresponds to viRead function of the VISA library.

        :param count: Number of bytes to be read.
        :return: data read, return value of the library call.
        :rtype: bytes, constants.StatusCode
        """

        # 0x2000 = 8192 = END
        def checker(current): return self.interface.ibsta() & 8192

        def reader(): return self.interface.read(count)

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
            count = self.interface.ibcnt()  # number of bytes transmitted

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
            return StatusCode.success
        except gpib.GpibError:
            return StatusCode.error_system_error

    def gpib_command(self, command_byte):
        """Write GPIB command byte on the bus.

        Corresponds to viGpibCommand function of the VISA library.
        See: https://linux-gpib.sourceforge.io/doc_html/gpib-protocol.html#REFERENCE-COMMAND-BYTES

        :param command_byte: command byte to send
        :type command_byte: int, must be [0 255]
        :return: Number of written bytes, return value of the library call.
        :rtype: int, :class:`pyvisa.constants.StatusCode`
        """

        if 0 <= command_byte <= 255:
            data = chr(command_byte)
        else:
            return 0, StatusCode.error_nonsupported_operation

        try:
            return self.controller.command(data), StatusCode.success

        except gpib.GpibError:
            return 0, StatusCode.error_system_error

    def assert_trigger(self, protocol):
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
            return StatusCode.success
        except gpib.GpibError:
            return StatusCode.error_system_error

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
        try:
            return self.interface.serial_poll(), StatusCode.success
        except gpib.GpibError:
            return 0, StatusCode.error_system_error

    def disable_event(self, event_type, mechanism):
        """Disables notification of the specified event type(s) via the specified mechanism(s).

        Corresponds to viDisableEvent function of the VISA library.

        :param event_type: Logical event identifier.
        :param mechanism: Specifies event handling mechanisms to be disabled.
                        (Constants.VI_QUEUE, .VI_HNDLR, .VI_SUSPEND_HNDLR, .VI_ALL_MECH)
        :return: return value of the library call.
        :rtype: :class:`pyvisa.constants.StatusCode`
        """

        if event_type not in self.valid_event_types:
            return StatusCode.error_invalid_event

        if mechanism in (constants.VI_QUEUE, constants.VI_ALL_MECH):
            if event_type not in self.enabled_queue_events:
                return StatusCode.success_event_already_disabled

            self.enabled_queue_events.remove(event_type)
            return StatusCode.success

        return StatusCode.error_invalid_mechanism

    def discard_events(self, event_type, mechanism):
        """Discards event occurrences for specified event types and mechanisms in a session.

        Corresponds to viDiscardEvents function of the VISA library.

        :param event_type: Logical event identifier.
        :param mechanism: Specifies event handling mechanisms to be discarded.
                        (Constants.VI_QUEUE, .VI_SUSPEND_HNDLR, .VI_ALL_MECH)
        :return: return value of the library call.
        :rtype: :class:`pyvisa.constants.StatusCode`
        """
        if event_type not in self.valid_event_types:
            return StatusCode.error_invalid_event

        if mechanism in (constants.VI_QUEUE, constants.VI_ALL_MECH):
            self.event_queue = [(t, a) for t, a in self.event_queue if not (
                event_type == constants.VI_ALL_ENABLED_EVENTS or t == event_type)]
            return StatusCode.success

        return StatusCode.error_invalid_mechanism

    def enable_event(self, event_type, mechanism, context=None):
        """Enable event occurrences for specified event types and mechanisms in a session.

        Corresponds to viEnableEvent function of the VISA library.

        :param event_type: Logical event identifier.
        :param mechanism: Specifies event handling mechanisms to be enabled.
                        (Constants.VI_QUEUE, .VI_HNDLR, .VI_SUSPEND_HNDLR)
        :param context:
        :return: return value of the library call.
        :rtype: :class:`pyvisa.constants.StatusCode`
        """

        if event_type not in self.valid_event_types:
            return StatusCode.error_invalid_event

        if mechanism in (constants.VI_QUEUE, constants.VI_ALL_MECH):
            # enable GPIB autopoll
            try:
                self.controller.config(7, 1)
            except gpib.GpibError:
                return StatusCode.error_invalid_setup

            if event_type in self.enabled_queue_events:
                return StatusCode.success_event_already_enabled
            else:
                self.enabled_queue_events.add(event_type)
                return StatusCode.success

        # mechanisms which are not implemented: constants.VI_SUSPEND_HNDLR, constants.VI_ALL_MECH
        return StatusCode.error_invalid_mechanism

    def wait_on_event(self, in_event_type, timeout):
        """Waits for an occurrence of the specified event for a given session.

        Corresponds to viWaitOnEvent function of the VISA library.

        :param in_event_type: Logical identifier of the event(s) to wait for.
        :param timeout: Absolute time period in time units that the resource shall wait for a specified event to
                        occur before returning the time elapsed error. The time unit is in milliseconds.
        :return: - Logical identifier of the event actually received
                 - A handle specifying the unique occurrence of an event
                 - return value of the library call.
        :rtype: - eventtype
                - event object # TODO
                - :class:`pyvisa.constants.StatusCode`
        """

        if in_event_type not in self.valid_event_types:
            return StatusCode.error_invalid_event

        if in_event_type not in self.enabled_queue_events:
            return StatusCode.error_not_enabled

        # if the event queue is empty, wait for more events
        if not self.event_queue:
            old_timeout = self.timeout
            self.timeout = timeout

            event_mask = 0

            if in_event_type in (constants.VI_EVENT_IO_COMPLETION, constants.VI_ALL_ENABLED_EVENTS):
                event_mask |= 0x100  # CMPL

            if in_event_type in (constants.VI_EVENT_SERVICE_REQ, constants.VI_ALL_ENABLED_EVENTS):
                event_mask |= gpib.RQS

            if timeout != 0:
                event_mask |= gpib.TIMO

            self.interface.wait(event_mask)
            sta = self.interface.ibsta()

            self.timeout = old_timeout

            # TODO: set event attributes
            if 0x100 & event_mask & sta:
                evt_type = constants.VI_EVENT_IO_COMPLETION
                # TODO: implement all event attributes
                # VI_ATTR_EVENT_TYPE: VI_EVENT_IO_COMPLETION,
                # VI_ATTR_STATUS: return code of the asynchronous operation that has completed,
                # VI_ATTR_JOB_ID: job ID of the asynchronous operation that has completed,
                # VI_ATTR_BUFFER: the address of the buffer that was used in the asynchronous operation,
                # VI_ATTR_RET_COUNT/VI_ATTR_RET_COUNT_32/VI_ATTR_RET_COUNT_64: number of elements that were asynchronously transferred,
                # VI_ATTR_OPER_NAME: name of the operation generating the event
                attrs = {
                    constants.VI_ATTR_EVENT_TYPE: constants.VI_EVENT_IO_COMPLETION}
                self.event_queue.append(
                    (constants.VI_EVENT_IO_COMPLETION,
                     GPIBEvent(attrs)))

            if gpib.RQS & event_mask & sta:
                self.event_queue.append(
                    (constants.VI_EVENT_SERVICE_REQ,
                     GPIBEvent({constants.VI_ATTR_EVENT_TYPE: constants.VI_EVENT_SERVICE_REQ})))

        try:
            out_event_type, event_data = self.event_queue.pop()
            return out_event_type, event_data, StatusCode.success
        except IndexError:
            return in_event_type, None, StatusCode.error_timeout

