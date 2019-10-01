# -*- coding: utf-8 -*-
"""
    pyvisa-py.gpib
    ~~~~~~~~~~~~~~

    GPIB Session implementation using linux-gpib or gpib-ctypes.


    :copyright: 2015-2019 by PyVISA-py Authors, see AUTHORS for more details.
    :license: MIT, see LICENSE for more details.
"""

from __future__ import division, unicode_literals, print_function, absolute_import
from bisect import bisect
import ctypes  # Used for ibln not ideal

from pyvisa import constants, logger, attributes

from .sessions import Session, UnknownAttribute

try:
    GPIB_CTYPES = True
    from gpib_ctypes import gpib
    from gpib_ctypes.Gpib import Gpib
    from gpib_ctypes.gpib.gpib import _lib as gpib_lib

    # Add some extra binding not available by default
    extra_funcs = [
        ("ibcac", [ctypes.c_int, ctypes.c_int], ctypes.c_int),
        ("ibgts", [ctypes.c_int, ctypes.c_int], ctypes.c_int),
        ("ibln", [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                  ctypes.POINTER(ctypes.c_short)], ctypes.c_int),
        ("ibpct", [ctypes.c_int], ctypes.c_int),
    ]
    for name, argtypes, restype in extra_funcs:
        libfunction = gpib_lib[name]
        libfunction.argtypes = argtypes
        libfunction.restype = restype

except ImportError as e:
    GPIB_CTYPES = False
    try:
        import gpib
        from Gpib import Gpib, GpibError
    except ImportError as e:
        Session.register_unavailable(constants.InterfaceType.gpib, 'INSTR',
                                     'Please install linux-gpib (Linux) or '
                                     'gpib-ctypes (Windows, Linux) to use '
                                     'this resource type. Note that installing'
                                     ' gpib-ctypes will give you access to a '
                                     'broader range of funcionality.\n%s' % e)
        raise

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


def _find_boards():
    """Find GPIB board addresses.
    """
    for board in range(16):
      try:
        yield board, gpib.ask(board, 1)
      except gpib.GpibError as e:
        logger.debug("GPIB board %i error in _find_boards(): %s", board,repr(e))


def _find_listeners():
    """Find GPIB listeners.
    """
    for board, boardpad in _find_boards():
      for i in range(31):
        try:
          if boardpad != i and gpib.listener(board, i):
            yield board, i
        except gpib.GpibError as e:
            logger.debug("GPIB board %i addr %i error in _find_listeners(): %s", board, i, repr(e))


def _analyse_lines_value(value, line):
    """Determine the state of a GPIB line based on the iblines byte.

    """
    if line == constants.VI_ATTR_GPIB_REN_STATE:
        # REN bit valid = 0x10, REN bit value = 0x100
        validity_mask = 0x10
        value_mask = 0x100
    elif line == constants.VI_ATTR_GPIB_ATN_STATE:
        # ATN bit valid = 0x40, ATN bit value = 0x4000
        validity_mask = 0x40
        value_mask = 0x4000
    elif line == constants.VI_ATTR_GPIB_NDAC_STATE:
        # NDAC bit valid = 0x2, NDAC bit value = 0x200
        validity_mask = 0x2
        value_mask = 0x200
    elif line == constants.VI_ATTR_GPIB_SRQ_STATE:
        # SRQ bit valid = 0x20, SRQ bit value = 0x2000
        validity_mask = 0x20
        value_mask = 0x2000

    if not value & validity_mask:
        return constants.VI_STATE_UNKNOWN, StatusCode.success
    else:
        if value & value_mask:
            return constants.VI_STATE_ASSERTED, StatusCode.success
        else:
            return constants.VI_STATE_UNASSERTED, StatusCode.success

StatusCode = constants.StatusCode

# linux-gpib timeout constants, in seconds. See GPIBSession._set_timeout.
TIMETABLE = (0, 10e-6, 30e-6, 100e-6, 300e-6, 1e-3, 3e-3, 10e-3, 30e-3, 100e-3, 300e-3, 1.0, 3.0,
             10.0, 30.0, 100.0, 300.0, 1000.0)


def convert_gpib_error(error, status, operation):
    """Convert a GPIB error to a VISA StatusCode.

    :param error: Error to use to determine the proper status code.
    :type error: gpib.GpibError
    :param status: Status byte of the GPIB library.
    :type status: int
    :param operation: Name of the operation that caused an exception. Used in logging.
    :type operation: str
    :return: Status code matching the GPIB error.
    :rtype: constants.StatusCode

    """
    # First check the imeout condition in the status byte
    if status & 0x4000:
        return constants.StatusCode.error_timeout
    # All other cases are hard errors.
    # In particular linux-gpib simply gives a string we could parse but that
    # feels brittle. As a consequence we only try to be smart when using
    # gpib-ctypes. However in both cases we log the exception at debug level.
    else:
        logger.debug('Failed to %s.', exc_info=error)
        if not GPIB_CTYPES:
            return constants.StatusCode.error_system_error
        if error.code == 1:
            return constants.StatusCode.error_not_cic
        elif error.code == 2:
            return constants.StatusCode.error_no_listeners
        elif error.code == 4:
            return constants.StatusCode.error_invalid_mode
        elif error.code == 11:
            return constants.StatusCode.error_nonsupported_operation
        elif error.code == 1:
            return constants.StatusCode.error_not_cic
        elif error.code == 21:
            return constants.StatusCode.error_resource_locked
        else:
            return constants.StatusCode.error_system_error


def convert_gpib_status(status):
    if status & 0x4000:
        return constants.StatusCode.error_timeout
    elif status & 0x8000:
      return constants.StatusCode.error_system_error
    else:
      return constants.StatusCode.success


class _GPIBCommon(object):
    """Common base class for GPIB sessions.

    Both INSTR and INTFC resources share the following attributes:
    - VI_ATTR_INTF_TYPE
    - VI_ATTR_TMO_VALUE
    - VI_ATTR_INTF_INST_NAME
    - VI_ATTR_INTF_NUM
    - VI_ATTR_DMA_ALLOW_EN
    - VI_ATTR_SEND_END_EN
    - VI_ATTR_TERMCHAR
    - VI_ATTR_TERM_CHAR_EN
    - VI_ATTR_RD_BUF_OPER_MODE
    - VI_ATTR_WR_BUF_OPER_MODE
    - VI_ATTR_FILE_APPEND_EN
    - VI_ATTR_GPIB_PRIMARY_ADDR
    - VI_ATTR_GPIB_SECONDARY_ADDR
    - VI_ATTR_GPIB_REN_STATE

    """
    @classmethod
    def get_low_level_info(cls):
        try:
            ver = gpib.version()
        except AttributeError:
            ver = '< 4.0'

        return 'via Linux GPIB (%s)' % ver

    def after_parsing(self):
        minor = int(self.parsed.board)
        sad = 0
        timeout = 13
        send_eoi = 1
        eos_mode = 0
        self.interface = None
        if self.parsed.resource_class == 'INSTR':
            pad = int(self.parsed.primary_address)
            # Used to talk to a specific resource
            self.interface = Gpib(name=minor, pad=pad, sad=sad,
                                  timeout=timeout, send_eoi=send_eoi,
                                  eos_mode=eos_mode)
        # Bus wide operation
        self.controller = Gpib(name=minor)

        # Force timeout setting to interface
        self.set_attribute(constants.VI_ATTR_TMO_VALUE,
                           attributes.AttributesByID[constants.VI_ATTR_TMO_VALUE].default)

        for name in ('TERMCHAR', 'TERMCHAR_EN'):
            attribute = getattr(constants, 'VI_ATTR_' + name)
            self.attrs[attribute] =\
                attributes.AttributesByID[attribute].default

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
        return super(_GPIBCommon, self)._get_timeout(attribute)

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
        status = super(_GPIBCommon, self)._set_timeout(attribute, value)
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
        if self.interface:
            self.interface.close()
        self.controller.close()

    def read(self, count):
        """Reads data from device or interface synchronously.

        Corresponds to viRead function of the VISA library.

        :param count: Number of bytes to be read.
        :return: data read, return value of the library call.
        :rtype: bytes, constants.StatusCode
        """
        # INTFC don't have an interface so use the controller
        ifc = self.interface or self.controller

        # END 0x2000
        checker = lambda current: ifc.ibsta() & 0x2000

        reader = lambda: ifc.read(count)

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

        # INTFC don't have an interface so use the controller
        ifc = self.interface or self.controller

        try:
            ifc.write(data)
            count = ifc.ibcnt() # number of bytes transmitted

            return count, StatusCode.success
        except gpib.GpibError as e:
            return 0, convert_gpib_error(e, ifc.ibsta(), 'write')

    def gpib_control_ren(self, mode):
        """Controls the state of the GPIB Remote Enable (REN) interface line, and optionally the remote/local
        state of the device.

        Corresponds to viGpibControlREN function of the VISA library.

        :param session: Unique logical identifier to a session.
        :param mode: Specifies the state of the REN line and optionally the device remote/local state.
                     (Constants.VI_GPIB_REN*)
        :return: return value of the library call.
        :rtype: :class:`pyvisa.constants.StatusCode`
        """
        if self.parsed.interface_type == 'INTFC':
            if mode not in (constants.VI_GPIB_REN_ASSERT,
                            constants.VI_GPIB_REN_DEASSERT,
                            constants.VI_GPIB_REN_ASSERT_LLO):
                return constants.StatusCode.error_nonsupported_operation

        # INTFC don't have an interface so use the controller
        ifc = self.interface or self.controller
        try:
            if mode == constants.VI_GPIB_REN_DEASSERT_GTL:
                # Send GTL command byte (cf linux-gpib documentation)
                ifc.command(chr(1))
            if mode in (constants.VI_GPIB_REN_DEASSERT,
                        constants.VI_GPIB_REN_DEASSERT_GTL):
                self.controller.remote_enable(0)

            if mode == constants.VI_GPIB_REN_ASSERT_LLO:
                # LLO
                ifc.command(b'0x11')
            elif mode == constants.VI_GPIB_REN_ADDRESS_GTL:
                # GTL
                ifc.command(b'0x1')
            elif mode == constants.VI_GPIB_REN_ASSERT_ADDRESS_LLO:
                pass
            elif mode in (constants.VI_GPIB_REN_ASSERT,
                          constants.VI_GPIB_REN_ASSERT_ADDRESS):
                ifc.remote_enable(1)
                if mode == constants.VI_GPIB_REN_ASSERT_ADDRESS:
                    # 0 for the secondary address means don't use it
                    found_listener = ctypes.c_short()
                    gpib_lib.ibln(self.parsed.board,
                              self.parsed.primary_address,
                              self.parsed.secondary_address,
                              ctypes.byref(found_listener))
        except GpibError as e:
            return convert_gpib_error(e,
                                      self.interface.ibsta(),
                                      'perform control REN')

        return constants.StatusCode.success

    def _get_attribute(self, attribute):
        """Get the value for a given VISA attribute for this session.

        Use to implement custom logic for attributes.

        :param attribute: Resource attribute for which the state query is made
        :return: The state of the queried attribute for a specified resource, return value of the library call.
        :rtype: (unicode | str | list | int, VISAStatus)
        """
        # TODO implement the following attributes
        # - VI_ATTR_INTF_INST_NAME RO
        # - VI_ATTR_DMA_ALLOW_EN RW
        # - VI_ATTR_RD_BUF_OPER_MODE RW
        # - VI_ATTR_WR_BUF_OPER_MODE RW
        # - VI_ATTR_FILE_APPEND_EN RW

        # INTFC don't have an interface so use the controller
        ifc = self.interface or self.controller

        if attribute == constants.VI_ATTR_GPIB_PRIMARY_ADDR:
            # IbaPAD 0x1
            return ifc.ask(1), StatusCode.success

        elif attribute == constants.VI_ATTR_GPIB_SECONDARY_ADDR:
            # IbaSAD 0x2
            # Remove 0x60 because National Instruments.
            sad = ifc.ask(2)
            if ifc.ask(2):
                return ifc.ask(2) - 96, StatusCode.success
            else:
                return constants.VI_NO_SEC_ADDR, StatusCode.success

        elif attribute == constants.VI_ATTR_GPIB_REN_STATE:
            try:
                lines = self.controller.lines()
                return _analyse_lines_value(lines, attribute)
            except AttributeError:
                # some versions of linux-gpib do not expose Gpib.lines()
                return constants.VI_STATE_UNKNOWN, StatusCode.success

        elif attribute == constants.VI_ATTR_SEND_END_EN:
            # Do not use IbaEndBitIsNormal 0x1a which relates to EOI on read()
            # not write(). see issue #196
            # IbcEOT 0x4
            if ifc.ask(4):
                return constants.VI_TRUE, StatusCode.success
            else:
                return constants.VI_FALSE, StatusCode.success

        elif attribute == constants.VI_ATTR_INTF_NUM:
            # IbaBNA 0x200
            return ifc.ask(512), StatusCode.success

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
        # TODO implement the following attributes
        # - VI_ATTR_DMA_ALLOW_EN RW
        # - VI_ATTR_RD_BUF_OPER_MODE RW
        # - VI_ATTR_WR_BUF_OPER_MODE RW
        # - VI_ATTR_FILE_APPEND_EN RW

        # INTFC don't have an interface so use the controller
        ifc = self.interface or self.controller

        if attribute == constants.VI_ATTR_GPIB_READDR_EN:
            # IbcREADDR 0x6
            # Setting has no effect in linux-gpib.
            if isinstance(attribute_state, int):
                ifc.config(6, attribute_state)
                return StatusCode.success
            else:
                return StatusCode.error_nonsupported_attribute_state

        elif attribute == constants.VI_ATTR_GPIB_PRIMARY_ADDR:
            # IbcPAD 0x1
            if isinstance(attribute_state, int) and 0 <= attribute_state <= 30:
                ifc.config(1, attribute_state)
                return StatusCode.success
            else:
                return StatusCode.error_nonsupported_attribute_state

        elif attribute == constants.VI_ATTR_GPIB_SECONDARY_ADDR:
            # IbcSAD 0x2
            # Add 0x60 because National Instruments.
            if isinstance(attribute_state, int) and 0 <= attribute_state <= 30:
                if ifc.ask(2):
                    ifc.config(2, attribute_state + 96)
                    return StatusCode.success
                else:
                    return StatusCode.error_nonsupported_attribute
            else:
                return StatusCode.error_nonsupported_attribute_state

        elif attribute == constants.VI_ATTR_GPIB_UNADDR_EN:
            # IbcUnAddr 0x1b
            try:
                ifc.config(27, attribute_state)
                return StatusCode.success
            except gpib.GpibError:
                return StatusCode.error_nonsupported_attribute_state

        elif attribute == constants.VI_ATTR_SEND_END_EN:
            # Do not use IbaEndBitIsNormal 0x1a which relates to EOI on read()
            # not write(). see issue #196
            # IbcEOT 0x4
            if isinstance(attribute_state, int):
                ifc.config(4, attribute_state)
                return StatusCode.success
            else:
                return StatusCode.error_nonsupported_attribute_state

        raise UnknownAttribute(attribute)


# TODO: Check secondary addresses.
@Session.register(constants.InterfaceType.gpib, 'INSTR')
class GPIBSession(_GPIBCommon, Session):
    """A GPIB Session that uses linux-gpib to do the low level communication.
    """

    @staticmethod
    def list_resources():
        return ['GPIB%d::%d::INSTR' % (board, pad) for board, pad in _find_listeners()]

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
        except gpib.GpibError as e:
            return convert_gpib_error(e, self.interface.ibsta(), 'clear')

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
        except gpib.GpibError as e:
            return convert_gpib_error(e,
                                      self.interface.ibsta(),
                                      'assert trigger')

    def read_stb(self):
        try:
            return self.interface.serial_poll(), StatusCode.success
        except gpib.GpibError as e:
            return 0, convert_gpib_error(e, self.interface.ibsta(), 'read STB')

    def _get_attribute(self, attribute):
        """Get the value for a given VISA attribute for this session.

        Use to implement custom logic for attributes. GPIB::INSTR have the
        following specific attributes:

        - VI_ATTR_TRIG_ID
        - VI_ATTR_IO_PROT
        - VI_ATTR_SUPPRESS_END_EN
        - VI_ATTR_GPIB_READDR_EN
        - VI_ATTR_GPIB_UNADDR_EN

        :param attribute: Resource attribute for which the state query is made
        :return: The state of the queried attribute for a specified resource, return value of the library call.
        :rtype: (unicode | str | list | int, VISAStatus)
        """
        # TODO implement the following attributes
        # - VI_ATTR_TRIG_ID RW or RO see specs
        # - VI_ATTR_IO_PROT RW
        # - VI_ATTR_SUPPRESS_END_EN RW
        ifc = self.interface

        if attribute == constants.VI_ATTR_GPIB_READDR_EN:
            # IbaREADDR 0x6
            # Setting has no effect in linux-gpib.
            return ifc.ask(6), StatusCode.success

        elif attribute == constants.VI_ATTR_GPIB_UNADDR_EN:
            # IbaUnAddr 0x1b
            if ifc.ask(27):
                return constants.VI_TRUE, StatusCode.success
            else:
                return constants.VI_FALSE, StatusCode.success

        return super(GPIBSession, self)._get_attribute(attribute)

    def _set_attribute(self, attribute, attribute_state):
        """Sets the state of an attribute.

        Corresponds to viSetAttribute function of the VISA library.

        :param attribute: Attribute for which the state is to be modified. (Attributes.*)
        :param attribute_state: The state of the attribute to be set for the specified object.
        :return: return value of the library call.
        :rtype: VISAStatus
        """
        # TODO implement the following attributes
        # - VI_ATTR_TRIG_ID RW or RO see specs
        # - VI_ATTR_IO_PROT RW
        # - VI_ATTR_SUPPRESS_END_EN RW
        ifc = self.interface

        if attribute == constants.VI_ATTR_GPIB_READDR_EN:
            # IbcREADDR 0x6
            # Setting has no effect in linux-gpib.
            if isinstance(attribute_state, int):
                ifc.config(6, attribute_state)
                return StatusCode.success
            else:
                return StatusCode.error_nonsupported_attribute_state

        elif attribute == constants.VI_ATTR_GPIB_UNADDR_EN:
            # IbcUnAddr 0x1b
            try:
                ifc.config(27, attribute_state)
                return StatusCode.success
            except gpib.GpibError:
                return StatusCode.error_nonsupported_attribute_state

        return super(GPIBSession, self)._set_attribute(attribute,
                                                       attribute_state)


@Session.register(constants.InterfaceType.gpib, 'INTFC')
class GPIBInterface(_GPIBCommon, Session):
    """A GPIB Interface that uses linux-gpib to do the low level communication.
    """

    @staticmethod
    def list_resources():
        return ['GPIB%d::INTFC' % board for board, pad in _find_boards()]

    def gpib_command(self, command_bytes):
        """Write GPIB command byte on the bus.

        Corresponds to viGpibCommand function of the VISA library.
        See: https://linux-gpib.sourceforge.io/doc_html/gpib-protocol.html#REFERENCE-COMMAND-BYTES

        :param command_bytes: command bytes to send
        :type command_bytes: bytes
        :return: Number of written bytes, return value of the library call.
        :rtype: int, :class:`pyvisa.constants.StatusCode`
        """
        try:
            return self.controller.command(command_bytes), StatusCode.success
        except gpib.GpibError as e:
            return convert_gpib_error(e, self.controller.ibsta(), 'gpib command')

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
        except gpib.GpibError as e:
            return convert_gpib_error(e, self.controller.ibsta(), 'send IFC')

    def gpib_control_atn(self, mode):
        """Specifies the state of the ATN line and the local active controller state.

        Corresponds to viGpibControlATN function of the VISA library.

        :param session: Unique logical identifier to a session.
        :param mode: Specifies the state of the ATN line and optionally the local active controller state.
                     (Constants.VI_GPIB_ATN*)
        :return: return value of the library call.
        :rtype: :class:`pyvisa.constants.StatusCode`
        """
        logger.debug('GPIB.control atn')
        if mode == constants.VI_GPIB_ATN_ASSERT:
            status = gpib_lib.ibcac(self.controller.id, 0)
        elif mode == constants.VI_GPIB_ATN_DEASSERT:
            status = gpib_lib.ibgts(self.controller.id, 0)
        elif mode == constants.VI_GPIB_ATN_ASSERT_IMMEDIATE:
            # Asynchronous assertion (the name is counter intuitive)
            status = gpib_lib.ibcac(self.controller.id, 1)
        elif mode == constants.VI_GPIB_ATN_DEASSERT_HANDSHAKE:
            status = gpib_lib.ibgts(self.controller.id, 1)
        else:
            return constants.StatusCode.error_invalid_mode
        return convert_gpib_status(status)

    def gpib_pass_control(self, primary_address, secondary_address):
        """Tell the GPIB device at the specified address to become controller in charge (CIC).

        Corresponds to viGpibPassControl function of the VISA library.

        :param session: Unique logical identifier to a session.
        :param primary_address: Primary address of the GPIB device to which you want to pass control.
        :param secondary_address: Secondary address of the targeted GPIB device.
                                  If the targeted device does not have a secondary address,
                                  this parameter should contain the value Constants.VI_NO_SEC_ADDR.
        :return: return value of the library call.
        :rtype: :class:`pyvisa.constants.StatusCode`
        """
        # ibpct need to get the device id matching the primary and secondary address
        logger.debug('GPIB.pass control')
        try:
            did = gpib.dev(self.parsed.board, primary_address, secondary_address)
        except gpib.GpibError:
            logger.exception('Failed to get id for %s, %d',
                             primary_address, secondary_address)
            return StatusCode.error_resource_not_found

        status = gpib_lib.ibpct(did)
        return convert_gpib_status(status)


    def _get_attribute(self, attribute):
        """Get the value for a given VISA attribute for this session.

        Use to implement custom logic for attributes. GPIB::INTFC have the
        following specific attributes:

        - VI_ATTR_DEV_STATUS_BYTE
        - VI_ATTR_GPIB_ATN_STATE
        - VI_ATTR_GPIB_NDAC_STATE
        - VI_ATTR_GPIB_SRQ_STATE
        - VI_ATTR_GPIB_CIC_STATE
        - VI_ATTR_GPIB_SYS_CNTRL_STATE
        - VI_ATTR_GPIB_HS488_CBL_LEN
        - VI_ATTR_GPIB_ADDR_STATE

        :param attribute: Resource attribute for which the state query is made
        :return: The state of the queried attribute for a specified resource, return value of the library call.
        :rtype: (unicode | str | list | int, VISAStatus)
        """
        # TODO implement the following attributes
        # - VI_ATTR_DEV_STATUS_BYTE RW
        # - VI_ATTR_GPIB_SYS_CNTRL_STATE RW
        # - VI_ATTR_GPIB_HS488_CBL_LEN RO
        # - VI_ATTR_GPIB_ADDR_STATE RO
        ifc = self.controller

        if attribute == constants.VI_ATTR_GPIB_CIC_STATE:
            # ibsta CIC = 0x0020
            if ifc.ibsta() & 0x0020:
                return constants.VI_TRUE, StatusCode.success
            else:
                return constants.VI_FALSE, StatusCode.success

        elif attribute in (constants.VI_ATTR_GPIB_ATN_STATE,
                           constants.VI_ATTR_GPIB_NDAC_STATE,
                           constants.VI_ATTR_GPIB_SRQ_STATE):
            try:
                lines = ifc.lines()
                return _analyse_lines_value(lines, attribute)
            except AttributeError:
                # some versions of linux-gpib do not expose Gpib.lines()
                return constants.VI_STATE_UNKNOWN, StatusCode.success

        return super(GPIBSession, self)._get_attribute(attribute)

    def _set_attribute(self, attribute, attribute_state):
        """Sets the state of an attribute.

        Corresponds to viSetAttribute function of the VISA library.

        :param attribute: Attribute for which the state is to be modified. (Attributes.*)
        :param attribute_state: The state of the attribute to be set for the specified object.
        :return: return value of the library call.
        :rtype: VISAStatus
        """
        # TODO implement the following attributes
        # - VI_ATTR_GPIB_SYS_CNTRL_STATE
        # - VI_ATTR_DEV_STATUS_BYTE

        # INTFC don't have an interface so use the controller
        ifc = self.controller

        return super(GPIBSession, self)._set_attribute(attribute, attribute_state)