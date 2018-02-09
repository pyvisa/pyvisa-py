# -*- coding: utf-8 -*-
"""
    pyvisa-py.protocols.usbtmc
    ~~~~~~~~~~~~~~~~~~~~~~~~~~

    Implements Session to control USBTMC instruments

    Loosely based on PyUSBTMC:python module to handle USB-TMC(Test and
    Measurement class)　devices.

    by Noboru Yamamot, Accl. Lab, KEK, JAPAN

    This file is an offspring of the Lantz Project.

    :copyright: 2014 by PyVISA-py Authors, see AUTHORS for more details.
    :license: MIT, see LICENSE for more details.
"""

from __future__ import (division, unicode_literals, print_function,
                        absolute_import)

import enum
from pyvisa.compat import struct
import time
from collections import namedtuple

import usb

from .usbutil import (find_devices, find_interfaces, find_endpoint,
                      usb_find_desc)

import sys

if sys.version_info < (3, 2):
    def array_to_bytes(arr):
        return arr.tostring()
else:
    def array_to_bytes(arr):
        return arr.tobytes()


class MsgID(enum.IntEnum):
    """From USB-TMC table2
    """
    dev_dep_msg_out = 1
    request_dev_dep_msg_in = 2
    dev_dep_msg_in = 2
    vendor_specific_out = 126
    request_vendor_specific_in = 127
    vendor_specific_in = 127
    trigger = 128  # for USB488


class Request(enum.IntEnum):
    initiate_abort_bulk_out = 1
    check_abort_bulk_out_status = 2
    initiate_abort_bulk_in = 3
    check_abort_bulk_in_status = 4
    initiate_clear = 5
    check_clear_status = 6
    get_capabilities = 7
    indicator_pulse = 64


def find_tmc_devices(vendor=None, product=None, serial_number=None,
                     custom_match=None, **kwargs):
    """Find connected USBTMC devices. See usbutil.find_devices for more info.

    """
    def is_usbtmc(dev):
        if custom_match and not custom_match(dev):
            return False
        return bool(find_interfaces(dev, bInterfaceClass=0xfe,
                                    bInterfaceSubClass=3))

    return find_devices(vendor, product, serial_number, is_usbtmc, **kwargs)


class BulkOutMessage(object):
    """The Host uses the Bulk-OUT endpoint to send USBTMC command messages to
    the device.

    """

    @staticmethod
    def build_array(btag, eom, chunk):
        size = len(chunk)
        return (struct.pack('BBBx', MsgID.dev_dep_msg_out, btag,
                            ~btag & 0xFF) +
                struct.pack("<LBxxx", size, eom) +
                chunk +
                b'\0' * ((4 - size) % 4))


class BulkInMessage(namedtuple('BulkInMessage', 'msgid btag btaginverse '
                               'transfer_size transfer_attributes data')):
    """The Host uses the Bulk-IN endpoint to read USBTMC response messages from
    the device.

    The Host must first send a USBTMC command message that expects a response
    before attempting to read a USBTMC response message.

    """

    @classmethod
    def from_bytes(cls, data):
        msgid, btag, btaginverse = struct.unpack_from('BBBx', data)
        assert msgid == MsgID.dev_dep_msg_in

        transfer_size, transfer_attributes = struct.unpack_from('<LBxxx', data,
                                                                4)

        data = data[12:]
        return cls(msgid, btag, btaginverse, transfer_size,
                   transfer_attributes, data)

    @staticmethod
    def build_array(btag, transfer_size, term_char=None):
        """

        :param transfer_size:
        :param btag:
        :param term_char:
        :return:
        """

        if term_char is None:
            transfer_attributes = 0
            term_char = 0
        else:
            transfer_attributes = 2

        return (struct.pack('BBBx', MsgID.request_dev_dep_msg_in, btag,
                            ~btag & 0xFF) +
                struct.pack("<LBBxx", transfer_size, transfer_attributes,
                            term_char))


class USBRaw(object):
    """Base class for drivers that communicate with instruments
    via usb port using pyUSB
    """

    #: Configuration number to be used. If None, the default will be used.
    CONFIGURATION = None

    #: Interface index it be used
    INTERFACE = (0, 0)

    #: Receive and Send endpoints to be used. If None the first IN (or OUT)
    #: BULK endpoint will be used.
    ENDPOINTS = (None, None)

    timeout = 2000

    find_devices = staticmethod(find_devices)

    def __init__(self, vendor=None, product=None, serial_number=None,
                 device_filters=None, timeout=None, **kwargs):
        super(USBRaw, self).__init__()

        self.timeout = timeout

        device_filters = device_filters or {}
        devices = list(self.find_devices(vendor, product, serial_number, None,
                                         **device_filters))

        if not devices:
            raise ValueError('No device found.')
        elif len(devices) > 1:
            desc = '\n'.join(str(dev) for dev in devices)
            raise ValueError('{} devices found:\n{}\nPlease narrow the search'
                             ' criteria'.format(len(devices), desc))

        self.usb_dev = devices[0]

        try:
            if self.usb_dev.is_kernel_driver_active(0):
                    self.usb_dev.detach_kernel_driver(0)
        except (usb.core.USBError, NotImplementedError) as e:
            pass

        try:
            self.usb_dev.set_configuration()
        except usb.core.USBError as e:
            raise Exception('failed to set configuration\n %s' % e)

        try:
            self.usb_dev.set_interface_altsetting()
        except usb.core.USBError as e:
            pass

        self.usb_intf = self._find_interface(self.usb_dev, self.INTERFACE)

        self.usb_recv_ep, self.usb_send_ep =\
            self._find_endpoints(self.usb_intf, self.ENDPOINTS)

    def _find_interface(self, dev, setting):
        return self.usb_dev.get_active_configuration()[self.INTERFACE]

    def _find_endpoints(self, interface, setting):
        recv, send = setting
        if recv is None:
            recv = find_endpoint(interface, usb.ENDPOINT_IN,
                                 usb.ENDPOINT_TYPE_BULK)
        else:
            recv = usb_find_desc(interface, bEndpointAddress=recv)

        if send is None:
            send = find_endpoint(interface, usb.ENDPOINT_OUT,
                                 usb.ENDPOINT_TYPE_BULK)
        else:
            send = usb_find_desc(interface, bEndpointAddress=send)

        return recv, send

    def write(self, data):
        """Send raw bytes to the instrument.

        :param data: bytes to be sent to the instrument
        :type data: bytes
        """

        try:
            return self.usb_send_ep.write(data)
        except usb.core.USBError as e:
            raise ValueError(str(e))

    def read(self, size):
        """Receive raw bytes to the instrument.

        :param size: number of bytes to receive
        :return: received bytes
        :return type: bytes
        """

        if size <= 0:
            size = 1

        data = array_to_bytes(self.usb_recv_ep.read(size, self.timeout))

        return data

    def close(self):
        return usb.util.dispose_resources(self.usb_dev)


class USBTMC(USBRaw):

    RECV_CHUNK = 1024 ** 2

    find_devices = staticmethod(find_tmc_devices)

    def __init__(self, vendor=None, product=None, serial_number=None,
                 **kwargs):
        super(USBTMC, self).__init__(vendor, product, serial_number, **kwargs)
        self.usb_intr_in = find_endpoint(self.usb_intf, usb.ENDPOINT_IN,
                                         usb.ENDPOINT_TYPE_INTERRUPT)

        self.usb_dev.reset()
        self.usb_dev.set_configuration()

        time.sleep(0.01)

        self._get_capabilities()

        self._btag = 0

        if not (self.usb_recv_ep and self.usb_send_ep):
            msg = "TMC device must have both Bulk-In and Bulk-out endpoints."
            raise ValueError(msg)

    def _get_capabilities(self):
        self.usb_dev.ctrl_transfer(
            usb.util.build_request_type(usb.util.CTRL_IN,
                                        usb.util.CTRL_TYPE_CLASS,
                                        usb.util.CTRL_RECIPIENT_INTERFACE),
            Request.get_capabilities,
            0x0000,
            self.usb_intf.index,
            0x0018,
            timeout=self.timeout)

    def _find_interface(self, dev, setting):
        interfaces = find_interfaces(dev, bInterfaceClass=0xFE,
                                     bInterfaceSubClass=3)
        if not interfaces:
            raise ValueError('USB TMC interface not found.')
        elif len(interfaces) > 1:
            pass

        return interfaces[0]

    def write(self, data):
        """Send raw bytes to the instrument.

        :param data: bytes to be sent to the instrument
        :type data: bytes
        """

        begin, end, size = 0, 0, len(data)
        bytes_sent = 0

        raw_write = super(USBTMC, self).write

        while not end > size:
            begin, end = end, begin + self.RECV_CHUNK

            self._btag = (self._btag % 255) + 1

            data = BulkOutMessage.build_array(self._btag, end > size,
                                              data[begin:end])

            bytes_sent += raw_write(data)

        return bytes_sent

    def read(self, size):

        recv_chunk = self.RECV_CHUNK

        eom = False

        raw_read = super(USBTMC, self).read
        raw_write = super(USBTMC, self).write

        received = bytearray()

        while not eom:
            self._btag = (self._btag % 255) + 1

            req = BulkInMessage.build_array(self._btag, recv_chunk, None)

            raw_write(req)

            resp = raw_read(recv_chunk)

            response = BulkInMessage.from_bytes(resp)

            received.extend(response.data)

            eom = response.transfer_attributes & 1

        return bytes(received)
