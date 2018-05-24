# -*- coding: utf-8 -*-
"""
    pyvisa-py.usb
    ~~~~~~~~~~~~~

    Serial Session implementation using PyUSB.


    :copyright: 2014 by PyVISA-py Authors, see AUTHORS for more details.
    :license: MIT, see LICENSE for more details.
"""

from __future__ import division, unicode_literals, print_function, absolute_import

import logging
from pyvisa import constants, attributes

from .sessions import Session, UnknownAttribute

try:
    import usb
    from .protocols import usbtmc, usbutil, usbraw
except ImportError as e:
    msg = 'Please install PyUSB to use this resource type.\n%s'
    Session.register_unavailable(constants.InterfaceType.usb,
                                 'INSTR', msg % e)
    Session.register_unavailable(constants.InterfaceType.usb,
                                 'RAW', msg % e)
    raise

try:
    _ = usb.core.find()
except Exception as e:
    msg = 'PyUSB does not seem to be properly installed.\n' \
          'Please refer to PyUSB documentation and \n' \
          'install a suitable backend like \n' \
          'libusb 0.1, libusb 1.0, libusbx, \n' \
          'libusb-win32 or OpenUSB.\n%s' % e
    Session.register_unavailable(constants.InterfaceType.usb, 'INSTR', msg)
    Session.register_unavailable(constants.InterfaceType.usb, 'RAW', msg)
    raise


StatusCode = constants.StatusCode


class USBSession(Session):
    """Base class for drivers that communicate with usb devices
    via usb port using pyUSB
    """

    @staticmethod
    def list_resources():
        """Return list of resources for this type of USB device"""
        raise NotImplementedError

    @classmethod
    def get_low_level_info(cls):
        try:
            ver = usb.__version__
        except AttributeError:
            ver = 'N/A'

        try:
            # noinspection PyProtectedMember
            backend = usb.core.find()._ctx.backend.__class__.__module__.split('.')[-1]
        except Exception:
            backend = 'N/A'

        return 'via PyUSB (%s). Backend: %s' % (ver, backend)

    def _get_timeout(self, attribute):
        if self.interface:
            if self.interface.timeout == 2**32-1:
                self.timeout = None
            else:
                self.timeout = self.interface.timeout / 1000
        return super(USBSession, self)._get_timeout(attribute)

    def _set_timeout(self, attribute, value):
        status = super(USBSession, self)._set_timeout(attribute, value)
        timeout = int(self.timeout*1000) if self.timeout else 2**32-1
        if self.interface:
            self.interface.timeout = timeout
        return status

    def read(self, count):
        """Reads data from device or interface synchronously.

        Corresponds to viRead function of the VISA library.

        :param count: Number of bytes to be read.
        :return: data read, return value of the library call.
        :rtype: (bytes, VISAStatus)
        """

        supress_end_en, _ = self.get_attribute(constants.VI_ATTR_SUPPRESS_END_EN)

        if supress_end_en:
            raise ValueError('VI_ATTR_SUPPRESS_END_EN == True is currently unsupported by pyvisa-py')

        term_char, _ = self.get_attribute(constants.VI_ATTR_TERMCHAR)
        term_char_en, _ = self.get_attribute(constants.VI_ATTR_TERMCHAR_EN)

        return self._read(lambda: self.interface.read(count),
                          count,
                          lambda current: True, # USB always returns a complete message
                          supress_end_en,
                          term_char,
                          term_char_en,
                          usb.USBError)

    def write(self, data):
        """Writes data to device or interface synchronously.

        Corresponds to viWrite function of the VISA library.

        :param data: data to be written.
        :type data: bytes
        :return: Number of bytes actually transferred, return value of the library call.
        :rtype: (int, VISAStatus)
        """

        send_end, _ = self.get_attribute(constants.VI_ATTR_SEND_END_EN)

        count = self.interface.write(data)

        return count, StatusCode.success

    def close(self):
        self.interface.close()

    def _get_attribute(self, attribute):
        """Get the value for a given VISA attribute for this session.

        Use to implement custom logic for attributes.

        :param attribute: Resource attribute for which the state query is made
        :return: The state of the queried attribute for a specified resource, return value of the library call.
        :rtype: (unicode | str | list | int, VISAStatus)
        """

        raise UnknownAttribute(attribute)

    def _set_attribute(self, attribute, attribute_state):
        """Sets the state of an attribute.

        Corresponds to viSetAttribute function of the VISA library.

        :param attribute: Attribute for which the state is to be modified. (Attributes.*)
        :param attribute_state: The state of the attribute to be set for the specified object.
        :return: return value of the library call.
        :rtype: VISAStatus
        """

        raise UnknownAttribute(attribute)


@Session.register(constants.InterfaceType.usb, 'INSTR')
class USBInstrSession(USBSession):
    """Base class for drivers that communicate with instruments
    via usb port using pyUSB
    """

    @staticmethod
    def list_resources():
        out = []
        fmt = 'USB%(board)s::%(manufacturer_id)s::%(model_code)s::' \
              '%(serial_number)s::%(usb_interface_number)s::INSTR'
        for dev in usbtmc.find_tmc_devices():
            intfc = usbutil.find_interfaces(dev, bInterfaceClass=0xfe,
                                            bInterfaceSubClass=3)
            try:
                intfc = intfc[0].index
            except (IndexError, AttributeError):
                intfc = 0

            try:
                serial = dev.serial_number
            except (NotImplementedError, ValueError):
                logger = logging.getLogger(__name__)
                msg = ('Found a device whose serial number cannot be read.'
                       ' The partial VISA resource name is: ' + fmt)
                logger.warning(msg, dict(board=0,
                                         manufacturer_id=dev.idVendor,
                                         model_code=dev.idProduct,
                                         serial_number='???',
                                         usb_interface_number=intfc))
                continue

            out.append(fmt % dict(board=0,
                                  manufacturer_id=dev.idVendor,
                                  model_code=dev.idProduct,
                                  serial_number=serial,
                                  usb_interface_number=intfc))
        return out

    def after_parsing(self):
        self.interface = usbtmc.USBTMC(int(self.parsed.manufacturer_id, 0),
                                       int(self.parsed.model_code, 0),
                                       self.parsed.serial_number)

        for name in ('SEND_END_EN', 'TERMCHAR', 'TERMCHAR_EN'):
            attribute = getattr(constants, 'VI_ATTR_' + name)
            self.attrs[attribute] = attributes.AttributesByID[attribute].default


@Session.register(constants.InterfaceType.usb, 'RAW')
class USBRawSession(USBSession):
    """Base class for drivers that communicate with usb raw devices
    via usb port using pyUSB
    """

    @staticmethod
    def list_resources():
        out = []
        fmt = 'USB%(board)s::%(manufacturer_id)s::%(model_code)s::' \
              '%(serial_number)s::%(usb_interface_number)s::RAW'
        for dev in usbraw.find_raw_devices():
            intfc = usbutil.find_interfaces(dev, bInterfaceClass=0xFF)
            try:
                intfc = intfc[0].index
            except (IndexError, AttributeError):
                intfc = 0

            try:
                serial = dev.serial_number
            except (NotImplementedError, ValueError):
                logger = logging.getLogger(__name__)
                msg = ('Found a device whose serial number cannot be read.'
                       ' The partial VISA resource name is: ' + fmt)
                logger.warning(msg, dict(board=0,
                                         manufacturer_id=dev.idVendor,
                                         model_code=dev.idProduct,
                                         serial_number='???',
                                         usb_interface_number=intfc))
                continue

            out.append(fmt % dict(board=0,
                                  manufacturer_id=dev.idVendor,
                                  model_code=dev.idProduct,
                                  serial_number=serial,
                                  usb_interface_number=intfc))
        return out

    def after_parsing(self):
        self.interface = usbraw.USBRawDevice(int(self.parsed.manufacturer_id, 0),
                                             int(self.parsed.model_code, 0),
                                             self.parsed.serial_number)

        for name in ('SEND_END_EN', 'TERMCHAR', 'TERMCHAR_EN'):
            attribute = getattr(constants, 'VI_ATTR_' + name)
            self.attrs[attribute] = attributes.AttributesByID[attribute].default
