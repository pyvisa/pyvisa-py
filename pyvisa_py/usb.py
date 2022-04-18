# -*- coding: utf-8 -*-
"""Serial Session implementation using PyUSB.


:copyright: 2014-2020 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""
import errno
from typing import Any, List, Tuple, Type, Union

from pyvisa import attributes, constants
from pyvisa.constants import ResourceAttribute, StatusCode
from pyvisa.rname import USBInstr, USBRaw

from .common import logger
from .sessions import Session, UnknownAttribute

try:
    import usb

    from .protocols import usbraw, usbtmc, usbutil
except ImportError as e:
    msg = "Please install PyUSB to use this resource type.\n%s"
    Session.register_unavailable(constants.InterfaceType.usb, "INSTR", msg % e)
    Session.register_unavailable(constants.InterfaceType.usb, "RAW", msg % e)
    raise

try:
    _ = usb.core.find()
except Exception as e:
    msg = (
        "PyUSB does not seem to be properly installed.\n"
        "Please refer to PyUSB documentation and \n"
        "install a suitable backend like \n"
        "libusb 0.1, libusb 1.0, libusbx, \n"
        "libusb-win32 or OpenUSB.\n%s" % e
    )
    Session.register_unavailable(constants.InterfaceType.usb, "INSTR", msg)
    Session.register_unavailable(constants.InterfaceType.usb, "RAW", msg)
    raise


class USBTimeoutException(Exception):
    """Exception used internally to indicate USB timeout."""

    pass


class USBSession(Session):
    """Base class for drivers working with usb devices via usb port using pyUSB."""

    # Override parsed to take into account the fact that this class is only used
    # for a specific kind of resource
    parsed: Union[USBInstr, USBRaw]

    #: Class to use when instantiating the interface
    _intf_cls: Union[Type[usbraw.USBRawDevice], Type[usbtmc.USBTMC]]

    @staticmethod
    def list_resources() -> List[str]:
        """Return list of resources for this type of USB device."""
        raise NotImplementedError

    @classmethod
    def get_low_level_info(cls) -> str:
        try:
            ver = usb.__version__
        except AttributeError:
            ver = "N/A"

        try:
            # noinspection PyProtectedMember
            backend = usb.core.find()._ctx.backend.__class__.__module__.split(".")[-1]
        except Exception:
            backend = "N/A"

        return "via PyUSB (%s). Backend: %s" % (ver, backend)

    def after_parsing(self) -> None:
        self.interface = self._intf_cls(
            int(self.parsed.manufacturer_id, 0),
            int(self.parsed.model_code, 0),
            self.parsed.serial_number,
        )

        for name in ("SEND_END_EN", "SUPPRESS_END_EN", "TERMCHAR", "TERMCHAR_EN"):
            attribute = getattr(constants, "VI_ATTR_" + name)
            self.attrs[attribute] = attributes.AttributesByID[attribute].default

        # Force setting the timeout to get the proper value
        attribute = constants.VI_ATTR_TMO_VALUE
        self.set_attribute(attribute, attributes.AttributesByID[attribute].default)

    def _get_timeout(self, attribute: ResourceAttribute) -> Tuple[int, StatusCode]:
        if self.interface:
            if self.interface.timeout == 2**32 - 1:
                self.timeout = None
            else:
                self.timeout = self.interface.timeout / 1000
        return super(USBSession, self)._get_timeout(attribute)

    def _set_timeout(self, attribute: ResourceAttribute, value: int) -> StatusCode:
        status = super(USBSession, self)._set_timeout(attribute, value)
        timeout = int(self.timeout * 1000) if self.timeout else 2**32 - 1
        timeout = min(timeout, 2**32 - 1)
        if self.interface:
            self.interface.timeout = timeout
        return status

    def read(self, count: int) -> Tuple[bytes, StatusCode]:
        """Reads data from device or interface synchronously.

        Corresponds to viRead function of the VISA library.

        Parameters
        -----------
        count : int
            Number of bytes to be read.

        Returns
        -------
        bytes
            Data read from the device
        StatusCode
            Return value of the library call.

        """

        def _usb_reader():
            """Data reader identifying usb timeout exception."""
            try:
                return self.interface.read(count)
            except usb.USBError as exc:
                if exc.errno in (errno.ETIMEDOUT, -errno.ETIMEDOUT):
                    raise USBTimeoutException()
                raise

        supress_end_en, _ = self.get_attribute(ResourceAttribute.suppress_end_enabled)

        if supress_end_en:
            raise ValueError(
                "VI_ATTR_SUPPRESS_END_EN == True is currently unsupported by pyvisa-py"
            )

        term_char, _ = self.get_attribute(ResourceAttribute.termchar)
        term_char_en, _ = self.get_attribute(ResourceAttribute.termchar_enabled)

        return self._read(
            _usb_reader,
            count,
            lambda current: True,  # USB always returns a complete message
            supress_end_en,
            term_char,
            term_char_en,
            USBTimeoutException,
        )

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
        send_end, _ = self.get_attribute(ResourceAttribute.send_end_enabled)

        count = self.interface.write(data)

        return count, StatusCode.success

    def close(self):
        self.interface.close()
        return StatusCode.success

    def _get_attribute(
        self, attribute: constants.ResourceAttribute
    ) -> Tuple[Any, StatusCode]:
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
        self, attribute: constants.ResourceAttribute, attribute_state: Any
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


@Session.register(constants.InterfaceType.usb, "INSTR")
class USBInstrSession(USBSession):
    """Class for USBTMC devices."""

    # Override parsed to take into account the fact that this class is only used
    # for a specific kind of resource
    parsed: USBInstr

    #: Class to use when instantiating the interface
    _intf_cls = usbtmc.USBTMC

    @staticmethod
    def list_resources() -> List[str]:
        out = []
        fmt = (
            "USB%(board)s::%(manufacturer_id)s::%(model_code)s::"
            "%(serial_number)s::%(usb_interface_number)s::INSTR"
        )
        for dev in usbtmc.find_tmc_devices():
            intfc = usbutil.find_interfaces(
                dev, bInterfaceClass=0xFE, bInterfaceSubClass=3
            )
            try:
                intfc = intfc[0].index
            except (IndexError, AttributeError):
                intfc = 0

            try:
                serial = dev.serial_number
            except (NotImplementedError, ValueError):
                msg = (
                    "Found a device whose serial number cannot be read."
                    " The partial VISA resource name is: " + fmt
                )
                logger.warning(
                    msg,
                    dict(
                        board=0,
                        manufacturer_id=dev.idVendor,
                        model_code=dev.idProduct,
                        serial_number="???",
                        usb_interface_number=intfc,
                    ),
                )
                continue

            out.append(
                fmt
                % dict(
                    board=0,
                    manufacturer_id=dev.idVendor,
                    model_code=dev.idProduct,
                    serial_number=serial,
                    usb_interface_number=intfc,
                )
            )
        return out


@Session.register(constants.InterfaceType.usb, "RAW")
class USBRawSession(USBSession):
    """Class for RAW devices."""

    # Override parsed to take into account the fact that this class is only used
    # for a specific kind of resource
    parsed: USBRaw

    #: Class to use when instantiating the interface
    _intf_cls = usbraw.USBRawDevice

    @staticmethod
    def list_resources() -> List[str]:
        out = []
        fmt = (
            "USB%(board)s::%(manufacturer_id)s::%(model_code)s::"
            "%(serial_number)s::%(usb_interface_number)s::RAW"
        )
        for dev in usbraw.find_raw_devices():
            intfc = usbutil.find_interfaces(dev, bInterfaceClass=0xFF)
            try:
                intfc = intfc[0].index
            except (IndexError, AttributeError):
                intfc = 0

            try:
                serial = dev.serial_number
            except (NotImplementedError, ValueError):
                msg = (
                    "Found a device whose serial number cannot be read."
                    " The partial VISA resource name is: " + fmt
                )
                logger.warning(
                    msg,
                    dict(
                        board=0,
                        manufacturer_id=dev.idVendor,
                        model_code=dev.idProduct,
                        serial_number="???",
                        usb_interface_number=intfc,
                    ),
                )
                continue

            out.append(
                fmt
                % dict(
                    board=0,
                    manufacturer_id=dev.idVendor,
                    model_code=dev.idProduct,
                    serial_number=serial,
                    usb_interface_number=intfc,
                )
            )
        return out
