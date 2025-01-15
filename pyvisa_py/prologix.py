"""
Implements the interface and instrument classes for Prologix-style devices.
"""

import select
import socket
import sys
from typing import Any, List, Optional, Tuple, Union

from pyvisa import attributes, constants, errors, logger, rname
from pyvisa.constants import BufferOperation, ResourceAttribute, StatusCode

from .serial import SerialSession, comports, serial as pyserial
from .sessions import Session, UnknownAttribute, VISARMSession
from .tcpip import TCPIPSocketSession

# dictionary lookup for Prologix controllers that have been opened
BOARDS = {}

IS_WIN = sys.platform == "win32"


class _PrologixIntfcSession(Session):  # pylint: disable=W0223
    """
    This is the common class for both
    PRLGX-TCPIP<n>::INTFC resources and
    PRLGX-ASRL<n>::INTFC resources.
    """

    # Override parsed to take into account the fact that this
    # class is only used for specific kinds of resources
    parsed: Union[rname.TCPIPSocket, rname.PrlgxASRLIntfc]
    plus_plus_read: bool = True
    rd_ahead: bytes = b""

    def __init__(
        self,
        resource_manager_session: VISARMSession,
        resource_name: str,
        parsed: Optional[rname.ResourceName] = None,
        open_timeout: Optional[int] = None,
    ) -> None:
        super().__init__(resource_manager_session, resource_name, parsed, open_timeout)

        self.set_attribute(ResourceAttribute.termchar, ord("\n"))
        self.set_attribute(ResourceAttribute.termchar_enabled, True)

        # Set mode as CONTROLLER
        self.write_oob(b"++mode 1\n")

        # Turn off read-after-write to avoid "Query Unterminated" errors
        self.write_oob(b"++auto 0\n")

        # Read timeout is 50ms (from Willow Garage, Inc code)
        self.write_oob(b"++read_tmo_ms 50\n")

        # Do not append CR or LF to GPIB data
        self.write_oob(b"++eos 3\n")

        # Assert EOI with last byte to indicate end of data
        self.write_oob(b"++eoi 1\n")

        # additional setup found in code from Willow Garage, Inc
        self.write_oob(b"++eot_enable 1\n")
        self.write_oob(b"++eot_char 0\n")

        BOARDS[self.parsed.board] = self
        self._gpib_addr = ""

    def close(self) -> StatusCode:
        BOARDS.pop(self.parsed.board)
        return super().close()  # type: ignore[safe-super]

    @property
    def gpib_addr(self) -> str:
        """
        gpib_addr is the currently addressed gpib instrument
        """
        return self._gpib_addr

    @gpib_addr.setter
    def gpib_addr(self, addr: str) -> None:
        if self._gpib_addr != addr:
            self.write_oob(f"++addr {addr}\n".encode())
            self._gpib_addr = addr

    def write_oob(self, data: bytes) -> Tuple[int, StatusCode]:
        """out-of-band write (for sending "++" commands)"""
        if self.interface is None:
            raise errors.InvalidSession()

        return super().write(data)

    def read(self, count: int) -> Tuple[bytes, StatusCode]:
        if self.interface is None:
            raise errors.InvalidSession()

        if self.plus_plus_read:
            self.plus_plus_read = False
            self.write_oob(b"++read eoi\n")

        return super().read(count)

    def assert_trigger(self, protocol: constants.TriggerProtocol) -> StatusCode:
        """Asserts hardware trigger.

        Implemented by instr sessions, not intfc sessions.
        """
        if self.interface is None:
            raise errors.InvalidSession()

        raise NotImplementedError


@Session.register(constants.InterfaceType.prlgx_tcpip, "INTFC")
class PrologixTCPIPIntfcSession(_PrologixIntfcSession, TCPIPSocketSession):
    """
    This class is instantiated for PRLGX-TCPIP<n>::INTFC resources.
    """

    # Override parsed to take into account the fact that this class is only
    # used for specific kinds of resources
    parsed: rname.TCPIPSocket

    def write(self, data: bytes) -> Tuple[int, StatusCode]:
        """Writes data to device or interface synchronously.

        Corresponds to viWrite function of the VISA library.

        Parameters
        ----------
        data : bytes
            Data to be written.

        Returns
        -------
        int
            Number of bytes actually transferred
        StatusCode
            Return value of the library call.

        """
        if self.interface is None:
            raise errors.InvalidSession()

        try:
            # use select to wait for write ready
            rd, _wr, _ = select.select([self.interface], [self.interface], [])
            if rd:
                self.clear()
        except socket.timeout:
            return 0, StatusCode.error_io

        self._pending_buffer.clear()
        self.plus_plus_read = True
        return super().write(data)


@Session.register(constants.InterfaceType.prlgx_asrl, "INTFC")
class PrologixASRLIntfcSession(_PrologixIntfcSession, SerialSession):
    """
    This class is instantiated for PRLGX-ASRL<n>::INTFC resources.
    """

    # Override parsed to take into account the fact that this class is only
    # used for specific kinds of resources
    parsed: rname.PrlgxASRLIntfc  # type: ignore[assignment]

    @staticmethod
    def list_resources() -> List[str]:
        return [
            f"PRLGX-ASRL::{port[0][3:] if IS_WIN else port[0]}::INTFC"
            for port in comports()
        ]

    def after_parsing(self) -> None:
        self.interface = pyserial.serial_for_url(
            ("COM" if IS_WIN else "") + self.parsed.serial_device,
            timeout=self.timeout,
            write_timeout=self.timeout,
            baudrate=115200,
        )
        # self.interface.write_termination = "\n"
        # self.write_termination = "\n"

        for name in (
            "ASRL_END_IN",
            "ASRL_END_OUT",
            "SEND_END_EN",
            "TERMCHAR",
            "TERMCHAR_EN",
            "SUPPRESS_END_EN",
        ):
            attribute = getattr(constants, "VI_ATTR_" + name)
            self.attrs[attribute] = attributes.AttributesByID[attribute].default

    def write(self, data: bytes) -> Tuple[int, StatusCode]:
        """Writes data to device or interface synchronously.

        Corresponds to viWrite function of the VISA library.

        Parameters
        ----------
        data : bytes
            Data to be written.

        Returns
        -------
        int
            Number of bytes actually transferred
        StatusCode
            Return value of the library call.

        """
        if self.interface is None:
            raise errors.InvalidSession()

        if self.interface.inWaiting() > 0:
            self.interface.flushInput()
        self.plus_plus_read = True
        return super().write(data)


class PrologixInstrSession(Session):
    """
    This class is instantiated for GPIB<n>::INSTR resources, but only when
    the corresponding PRLGX-xxx<n>::INTFC resource has been instantiated.
    """

    # we don't decorate this class with Session.register() because we don't
    # want it to be registered in the _session_classes array, but we still
    # need to define session_type to make the set_attribute machinery work.
    session_type = (constants.InterfaceType.gpib, "INSTR")

    # Override parsed to take into account the fact that this
    # class is only used for a specific kind of resource
    parsed: rname.GPIBInstr

    @staticmethod
    def list_resources() -> List[str]:
        # TODO: is there a way to get this?
        return []

    def after_parsing(self) -> None:
        self.interface = BOARDS[self.parsed.board]
        self.gpib_addr = self.parsed.primary_address
        if self.parsed.secondary_address:
            # Secondary address of the device to connect to
            # Reference for the GPIB secondary address
            # https://www.mathworks.com/help/instrument/secondaryaddress.html
            # NOTE: a secondary address of 0 is not the same as no secondary address.
            self.gpib_addr += " " + self.parsed.secondary_address

    def close(self) -> StatusCode:
        if self.interface is None or self.interface.interface is None:
            raise errors.InvalidSession()

        self.interface = None
        return StatusCode.success

    def read(self, count: int) -> Tuple[bytes, StatusCode]:
        if self.interface is None or self.interface.interface is None:
            raise errors.InvalidSession()

        self.interface.gpib_addr = self.gpib_addr

        return self.interface.read(count)

    def write(self, data: bytes) -> Tuple[int, StatusCode]:
        if self.interface is None or self.interface.interface is None:
            raise errors.InvalidSession()

        self.interface.gpib_addr = self.gpib_addr

        # if the calling function has appended a newline to the data,
        # we don't want it to be escaped.  remove it from the data
        # and stash it away so we can append it after all the escapes
        # have been added in.
        if data[-2:] == b"\r\n":
            last_byte = b"\r\n"
            data = data[:-2]
        elif data[-2:] == b"\n\r":
            last_byte = b"\n\r"
            data = data[:-2]
        elif data[-1] == ord("\n"):
            last_byte = b"\n"
            data = data[:-1]
        else:
            last_byte = b""

        # escape the "special" characters
        data = data.replace(b"\033", b"\033\033")
        data = data.replace(b"\n", b"\033\n")
        data = data.replace(b"\r", b"\033\r")
        data = data.replace(b"+", b"\033+")

        return self.interface.write(data + last_byte)

    def flush(self, mask: BufferOperation) -> StatusCode:
        if self.interface is None or self.interface.interface is None:
            raise errors.InvalidSession()

        return self.interface.flush(mask)

    def clear(self) -> StatusCode:
        """Clears a device.

        Corresponds to viClear function of the VISA library.

        Returns
        -------
        StatusCode
            Return value of the library call.

        """
        logger.debug("GPIB.device clear")
        if self.interface is None or self.interface.interface is None:
            raise errors.InvalidSession()

        self.interface.gpib_addr = self.gpib_addr
        _, status_code = self.interface.write_oob(b"++clr\n")
        return status_code

    def assert_trigger(self, protocol: constants.TriggerProtocol) -> StatusCode:
        """Asserts hardware trigger.

        Parameters
        ----------
        protocol : constants.TriggerProtocol
            Triggering protocol to use.
            Only supports constants.TriggerProtocol.default

        Returns
        -------
        StatusCode
            Return value of the library call.

        """
        logger.debug("GPIB.device assert hardware trigger")

        if self.interface is None or self.interface.interface is None:
            raise errors.InvalidSession()

        self.interface.gpib_addr = self.gpib_addr
        _, status_code = self.interface.write_oob(b"++trg\n")
        return status_code

    def read_stb(self) -> Tuple[int, StatusCode]:
        """Read the device status byte."""
        if self.interface is None or self.interface.interface is None:
            raise errors.InvalidSession()

        self.interface.gpib_addr = self.gpib_addr
        self.interface.write_oob(b"++spoll\n")
        data, status_code = self.interface.read(32)
        return (int(data), status_code)

    def _get_attribute(self, attribute: ResourceAttribute) -> Tuple[Any, StatusCode]:
        """Get the value for a given VISA attribute for this session.

        Use to implement custom logic for attributes.

        Parameters
        ----------
        attribute : ResourceAttribute
            Attribute for which the state query is made

        Returns
        -------
        Any
            State of the queried attribute for a specified resource
        StatusCode
            Return value of the library call.

        """
        raise UnknownAttribute(attribute)

    def _set_attribute(
        self, attribute: ResourceAttribute, attribute_state: Any
    ) -> StatusCode:
        """Sets the state of an attribute.

        Corresponds to viSetAttribute function of the VISA library.

        Parameters
        ----------
        attribute : constants.ResourceAttribute
            Attribute for which the state is to be modified. (Attributes.*)
        attribute_state : Any
            The state of the attribute to be set for the specified object.

        Returns
        -------
        StatusCode
            Return value of the library call.

        """
        raise UnknownAttribute(attribute)
