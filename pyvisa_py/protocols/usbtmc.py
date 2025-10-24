# -*- coding: utf-8 -*-
"""Implements Session to control USBTMC instruments

Loosely based on PyUSBTMC:python module to handle USB-TMC(Test and
Measurement class) devices. by Noboru Yamamot, Accl. Lab, KEK, JAPAN

This file is an offspring of the Lantz Project.

:copyright: 2014-2025 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

import enum
import math
import struct
import time
import warnings
from collections import namedtuple
from typing import Tuple

import usb

from pyvisa.constants import StatusCode, TriggerProtocol

from ..common import LOGGER
from .usbutil import find_devices, find_endpoint, find_interfaces, usb_find_desc


class MsgID(enum.IntEnum):
    """From USB-TMC table2"""

    dev_dep_msg_out = 1
    request_dev_dep_msg_in = 2
    dev_dep_msg_in = 2
    vendor_specific_out = 126
    request_vendor_specific_in = 127
    vendor_specific_in = 127

    # USB488
    trigger = 128


class Request(enum.IntEnum):
    initiate_abort_bulk_out = 1
    check_abort_bulk_out_status = 2
    initiate_abort_bulk_in = 3
    check_abort_bulk_in_status = 4
    initiate_clear = 5
    check_clear_status = 6
    get_capabilities = 7
    indicator_pulse = 64

    # USB488
    read_status_byte = 128
    ren_control = 160
    go_to_local = 161
    local_lockout = 162


class UsbTmcStatus(enum.IntEnum):
    success = 1
    pending = 2
    failed = 0x80
    transfer_not_in_progress = 0x81
    split_not_in_progress = 0x82
    split_in_progress = 0x83


UsbTmcInterfaceCapabilities = namedtuple(
    "UsbTmcInterfaceCapabilities", "indicator_pulse talk_only listen_only"
)
Usb488InterfaceCapabilities = namedtuple(
    "Usb488InterfaceCapabilities", "usb488 ren_control trigger"
)


def find_tmc_devices(
    vendor=None, product=None, serial_number=None, custom_match=None, **kwargs
):
    """Find connected USBTMC devices. See usbutil.find_devices for more info."""

    def is_usbtmc(dev):
        if custom_match and not custom_match(dev):
            return False
        return bool(find_interfaces(dev, bInterfaceClass=0xFE, bInterfaceSubClass=3))

    return find_devices(vendor, product, serial_number, is_usbtmc, **kwargs)


class BTag:
    """A transfer identifier

    The Host should increment the bTag
    by 1 each time it sends a new Bulk-OUT Header.

    """

    def __init__(self, first: int, last: int):
        self._first = first
        self._last = last
        self._current: int | None = None

    def next(self) -> int:
        self._current = self._first if self._current is None else self._current + 1
        if self._current > self._last:
            self._current = self._first
        return self._current


class BulkOutMessage:
    """The Host uses the Bulk-OUT endpoint to send USBTMC command messages to
    the device.

    """

    @staticmethod
    def build_array(btag, eom, chunk):
        size = len(chunk)
        return (
            struct.pack("BBBx", MsgID.dev_dep_msg_out, btag, ~btag & 0xFF)
            + struct.pack("<LBxxx", size, eom)
            + chunk
            + b"\0" * ((4 - size) % 4)
        )


class BulkInMessage(
    namedtuple(
        "BulkInMessage",
        "msgid btag btaginverse transfer_size transfer_attributes data",
    )
):
    """The Host uses the Bulk-IN endpoint to read USBTMC response messages from
    the device.

    The Host must first send a USBTMC command message that expects a response
    before attempting to read a USBTMC response message.

    """

    @classmethod
    def from_bytes(cls, data):
        msgid, btag, btaginverse = struct.unpack_from("BBBx", data)
        if msgid != MsgID.dev_dep_msg_in:
            warnings.warn(
                "Unexpected MsgID format. Consider updating the device's firmware. "
                "See https://github.com/pyvisa/pyvisa-py/issues/20"
                f"Expected message id was {MsgID.dev_dep_msg_in}, got {msgid}."
            )
            return BulkInMessage.from_quirky(data)

        transfer_size, transfer_attributes = struct.unpack_from("<LBxxx", data, 4)

        # Truncate data to the specified length (discard padding).
        data = data[12 : 12 + transfer_size]
        return cls(msgid, btag, btaginverse, transfer_size, transfer_attributes, data)

    @classmethod
    def from_quirky(cls, data):
        """Constructs a correct response for quirky devices."""
        msgid, btag, btaginverse = struct.unpack_from("BBBx", data)
        data = data.rstrip(b"\x00")
        # check whether it contains a ';' and if throw away the first 12 bytes
        if b";" in data:
            transfer_size, transfer_attributes = struct.unpack_from("<LBxxx", data, 4)
            data = data[12:]
        else:
            transfer_size = 0
            transfer_attributes = 1
        return cls(msgid, btag, btaginverse, transfer_size, transfer_attributes, data)

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

        return struct.pack(
            "BBBx", MsgID.request_dev_dep_msg_in, btag, ~btag & 0xFF
        ) + struct.pack("<LBBxx", transfer_size, transfer_attributes, term_char)


class TriggerMessage:
    """The Host uses the Bulk-OUT endpoint to send USBTMC trigger message to the device."""

    @staticmethod
    def build_array(btag):
        # According to USBTMC-USB488 1.0 section 3.2.1.1, table 2:
        return struct.pack("BBBx", MsgID.trigger, btag, ~btag & 0xFF) + bytes(8)


class USBRaw:
    """Base class for drivers that communicate with instruments
    via usb port using pyUSB
    """

    #: Configuration number to be used. If None, the default will be used.
    CONFIGURATION: int | None = None

    #: Interface index it be used
    INTERFACE = (0, 0)

    #: Receive and Send endpoints to be used. If None the first IN (or OUT)
    #: BULK endpoint will be used.
    ENDPOINTS = (None, None)

    find_devices = staticmethod(find_devices)

    def __init__(
        self,
        vendor=None,
        product=None,
        serial_number=None,
        device_filters=None,
        timeout=None,
        **kwargs,
    ):
        super().__init__()

        # Timeout expressed in ms as an integer and limited to 2**32-1
        # If left to None pyusb will use its default value
        self.timeout = timeout

        device_filters = device_filters or {}
        devices = list(
            self.find_devices(vendor, product, serial_number, None, **device_filters)
        )

        if not devices:
            raise ValueError("No device found.")
        elif len(devices) > 1:
            desc = "\n".join(str(dev) for dev in devices)
            raise ValueError(
                f"{len(devices)} devices found:\n{desc}\nPlease narrow the search"
                " criteria"
            )

        self.usb_dev = devices[0]

        try:
            if self.usb_dev.is_kernel_driver_active(0):
                self.usb_dev.detach_kernel_driver(0)
        except (usb.core.USBError, NotImplementedError):
            pass

        try:
            cfg = self.usb_dev.get_active_configuration()
        except usb.core.USBError:
            cfg = None

        if cfg is None:
            try:
                self.usb_dev.set_configuration()
                cfg = self.usb_dev.get_active_configuration()
            except usb.core.USBError as e:
                raise Exception("failed to set configuration\n %s" % e)

        intf = cfg[(0, 0)]

        # Check if the interface exposes multiple alternative setting and
        # set one only if there is more than one.
        if (
            len(
                tuple(
                    usb.util.find_descriptor(
                        cfg, find_all=True, bInterfaceNumber=intf.bInterfaceNumber
                    )
                )
            )
            > 1
        ):
            try:
                self.usb_dev.set_interface_altsetting()
            except usb.core.USBError:
                pass

        self.usb_intf = self._find_interface(self.usb_dev, self.INTERFACE)

        self.usb_recv_ep, self.usb_send_ep = self._find_endpoints(
            self.usb_intf, self.ENDPOINTS
        )

    def _find_interface(self, dev, setting):
        return self.usb_dev.get_active_configuration()[self.INTERFACE]

    def _find_endpoints(self, interface, setting):
        recv, send = setting
        if recv is None:
            recv = find_endpoint(interface, usb.ENDPOINT_IN, usb.ENDPOINT_TYPE_BULK)
        else:
            recv = usb_find_desc(interface, bEndpointAddress=recv)

        if send is None:
            send = find_endpoint(interface, usb.ENDPOINT_OUT, usb.ENDPOINT_TYPE_BULK)
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

        data = self.usb_recv_ep.read(size, self.timeout).tobytes()

        return data

    def close(self):
        return usb.util.dispose_resources(self.usb_dev)


class USBTMC(USBRaw):
    find_devices = staticmethod(find_tmc_devices)

    def __init__(self, vendor=None, product=None, serial_number=None, **kwargs):
        super().__init__(vendor, product, serial_number, **kwargs)
        self.usb_intr_in = find_endpoint(
            self.usb_intf, usb.ENDPOINT_IN, usb.ENDPOINT_TYPE_INTERRUPT
        )

        time.sleep(0.01)

        self._capabilities = self._get_capabilities()

        self._btag = BTag(1, 255)
        self._read_stb_btag = BTag(2, 127)

        if not (self.usb_recv_ep and self.usb_send_ep):
            msg = "TMC device must have both Bulk-In and Bulk-out endpoints."
            raise ValueError(msg)

        self._enable_remote_control()

    def _enable_remote_control(self):
        if not self._capabilities["usb488"].ren_control:
            return

        self.usb_dev.ctrl_transfer(
            usb.util.build_request_type(
                usb.util.CTRL_IN,
                usb.util.CTRL_TYPE_CLASS,
                usb.util.CTRL_RECIPIENT_INTERFACE,
            ),
            Request.ren_control,
            1,
            self.usb_intf.index,
            1,
            timeout=self.timeout,
        )

    def _get_capabilities(self):
        capabilities = self.usb_dev.ctrl_transfer(
            usb.util.build_request_type(
                usb.util.CTRL_IN,
                usb.util.CTRL_TYPE_CLASS,
                usb.util.CTRL_RECIPIENT_INTERFACE,
            ),
            Request.get_capabilities,
            0x0000,
            self.usb_intf.index,
            0x0018,
            timeout=self.timeout,
        )

        # bit #2: 1 - The USBTMC interface accepts the
        #             INDICATOR_PULSE request.
        #         0 - The USBTMC interface does not accept the
        #             INDICATOR_PULSE request. The device, when
        #             an INDICATOR_PULSE request is received,
        #             must treat this command as a non-defined
        #             command and return a STALL handshake
        #             packet.
        # bit #1: 1 - The USBTMC interface is talk-only.
        #         0 - The USBTMC interface is not talk-only.
        # bit #0: 1 - The USBTMC interface is listen-only.
        #         0 - The USBTMC interface is not listen-only.
        usbtmc_capabilities = UsbTmcInterfaceCapabilities(
            indicator_pulse=bool(capabilities[4] & (1 << 2)),
            talk_only=bool(capabilities[4] & (1 << 1)),
            listen_only=bool(capabilities[4] & (1 << 0)),
        )

        # bit #2: The interface is a 488.2 USB488 interface.
        # bit #1: The interface accepts REN_CONTROL, GO_TO_LOCAL,
        #         and LOCAL_LOCKOUT requests.
        # bit #0: The interface accepts the MsgID = TRIGGER
        #         USBTMC command message and forwards
        #         TRIGGER requests to the Function Layer.
        usb488_capabilities = Usb488InterfaceCapabilities(
            usb488=bool(capabilities[14] & (1 << 2)),
            ren_control=bool(capabilities[14] & (1 << 1)),
            trigger=bool(capabilities[14] & (1 << 0)),
        )

        LOGGER.debug(usbtmc_capabilities)
        LOGGER.debug(usb488_capabilities)

        return {"usb488": usb488_capabilities, "usbtmc": usbtmc_capabilities}

    def _find_interface(self, dev, setting):
        interfaces = find_interfaces(dev, bInterfaceClass=0xFE, bInterfaceSubClass=3)
        if not interfaces:
            raise ValueError("USB TMC interface not found.")
        elif len(interfaces) > 1:
            pass

        return interfaces[0]

    def _abort_bulk_in(self, btag):
        """Request that the device abort a pending Bulk-IN operation."""

        abort_timeout_ms = 5000

        # Send INITIATE_ABORT_BULK_IN.
        # According to USBTMC 1.00 4.2.1.4:
        #   wValue = bTag value of transfer to be aborted
        #   wIndex = Bulk-IN endpoint
        #   wLength = 0x0002 (length of device response)
        data = self.usb_dev.ctrl_transfer(
            usb.util.build_request_type(
                usb.util.CTRL_IN,
                usb.util.CTRL_TYPE_CLASS,
                usb.util.CTRL_RECIPIENT_ENDPOINT,
            ),
            Request.initiate_abort_bulk_in,
            btag,
            self.usb_recv_ep.bEndpointAddress,
            0x0002,
            timeout=abort_timeout_ms,
        )

        if data[0] != UsbTmcStatus.success:
            # Abort Bulk-IN failed. Ignore it.
            return

        # Read remaining data from Bulk-IN endpoint.
        self.usb_recv_ep.read(self.usb_recv_ep.wMaxPacketSize, abort_timeout_ms)

        # Send CHECK_ABORT_BULK_IN_STATUS until it completes.
        # According to USBTMC 1.00 4.2.1.5:
        #   wValue = 0x0000
        #   wIndex = Bulk-IN endpoint
        #   wLength = 0x0008 (length of device response)
        for retry in range(100):
            data = self.usb_dev.ctrl_transfer(
                usb.util.build_request_type(
                    usb.util.CTRL_IN,
                    usb.util.CTRL_TYPE_CLASS,
                    usb.util.CTRL_RECIPIENT_ENDPOINT,
                ),
                Request.check_abort_bulk_in_status,
                0x0000,
                self.usb_recv_ep.bEndpointAddress,
                0x0008,
                timeout=abort_timeout_ms,
            )
            if data[0] != UsbTmcStatus.pending:
                break
            time.sleep(0.05)

    def write(self, data):
        """Send raw bytes to the instrument.

        :param data: bytes to be sent to the instrument
        :type data: bytes
        """

        begin, end, size = 0, 0, len(data)
        bytes_sent = 0

        raw_write = super().write

        # Send all data via one or more Bulk-OUT transfers.
        # Set the EOM flag on the last transfer only.
        # Send at least one transfer (possibly empty).
        while (end == 0) or (end < size):
            begin, end = end, begin + self.usb_send_ep.wMaxPacketSize

            btag = self._btag.next()

            eom = end >= size
            chunk = BulkOutMessage.build_array(btag, eom, data[begin:end])

            bytes_sent += raw_write(chunk)

        return size

    def read(self, size):
        usbtmc_header_size = 12
        eom = False

        raw_read = super().read
        raw_write = super().write

        received_message = bytearray()

        while not eom:
            received_transfer = bytearray()
            btag = self._btag.next()

            req = BulkInMessage.build_array(btag, size, None)
            raw_write(req)

            try:
                # make sure the data request is in multitudes of wMaxPacketSize.
                # + 1 * wMaxPacketSize for message sizes that equals wMaxPacketSize == size + usbtmc_header_size.
                # This to be able to retrieve a short package to end communication
                # (see USB 2.0 Section 5.8.3 and USBTMC Section 3.3)
                chunk_size = (
                    math.floor(
                        (size + usbtmc_header_size) / self.usb_recv_ep.wMaxPacketSize
                    )
                    + 1
                ) * self.usb_recv_ep.wMaxPacketSize
                resp = raw_read(chunk_size)

                response = BulkInMessage.from_bytes(resp)
                received_transfer.extend(response.data)

                # Detect EOM only when device sends all expected bytes.
                if len(received_transfer) >= response.transfer_size:
                    eom = response.transfer_attributes & 1
                if not eom and len(received_transfer) >= size:
                    # Read asking for 'size' bytes from the device.
                    # This may be less then the device wants to send back in a message
                    # Therefore the request does not mean that we must receive a EOM.
                    # Multiple `transfers` will be required to retrieve the remaining bytes.
                    eom = True
                else:
                    while (
                        (len(resp) % self.usb_recv_ep.wMaxPacketSize) == 0
                        or len(received_transfer) < response.transfer_size
                    ) and not eom:
                        # USBTMC Section 3.3 specifies that the first usb packet
                        # must contain the header. the remaining packets do not need
                        # the header the message is finished when a "short packet"
                        # is sent (one whose length is less than wMaxPacketSize)
                        # wMaxPacketSize may be incorrectly reported by certain drivers.
                        # Therefore, continue reading until the transfer_size is reached.
                        chunk_size = (
                            math.floor(
                                (size - len(received_transfer))
                                / self.usb_recv_ep.wMaxPacketSize
                            )
                            + 1
                        ) * self.usb_recv_ep.wMaxPacketSize
                        resp = raw_read(chunk_size)
                        received_transfer.extend(resp)
                    if len(received_transfer) >= response.transfer_size:
                        eom = response.transfer_attributes & 1
                    if not eom and len(received_transfer) >= size:
                        eom = True
                # Truncate data to the specified length (discard padding)
                # USBTMC header (12 bytes) has already truncated
                received_message.extend(received_transfer[: response.transfer_size])
            except (usb.core.USBError, ValueError):
                # Abort failed Bulk-IN operation.
                self._abort_bulk_in(btag)
                raise

        return bytes(received_message)

    def read_stb(self) -> Tuple[int, StatusCode]:
        """Reads a status byte of the service request.

        Returns
        -------
        int
            Service request status byte
        StatusCode
            Return value of the library call.

        """
        if not self._capabilities["usb488"].usb488:
            return 0, StatusCode.error_nonsupported_operation

        btag = self._read_stb_btag.next()
        # According to USBTMC-USB488 1.0 section 4.3.1:
        #   wValue = bTag value of transfer
        #   wIndex = Bulk-IN endpoint
        #   wLength = 0x0003 (length of device response, USBTMC-USB488 1.0 section 4.3.1.1 and 4.3.1.2)
        data = self.usb_dev.ctrl_transfer(
            usb.util.build_request_type(
                usb.util.CTRL_IN,
                usb.util.CTRL_TYPE_CLASS,
                usb.util.CTRL_RECIPIENT_INTERFACE,
            ),
            Request.read_status_byte,
            btag,
            self.usb_intf.index,
            0x0003,
            timeout=self.timeout,
        )

        if data[0] != UsbTmcStatus.success:
            raise ValueError("status nok")

        if data[1] != btag:
            raise ValueError("Read status byte btag mismatch", "read_stb")

        if self.usb_intr_in is None:
            return data[2], StatusCode.success

        # Read response from interrupt channel
        data = self.usb_intr_in.read(2, self.timeout)

        # USBTMC-USB488 1.0 section 3.4.2, bNotify1 field
        if data[0] & 0x80 != 0x80 or data[0] & 0x7F != btag:
            raise ValueError(
                "Read status byte interrupt-IN bNotify1 mismatch", "read_stb"
            )

        return data[1], StatusCode.success

    def assert_trigger(self, protocol: TriggerProtocol) -> StatusCode:
        """Assert software or hardware trigger.

        Corresponds to viAssertTrigger function of the VISA library.

        Parameters
        ----------
        protocol : constants.TriggerProtocol
            Trigger protocol to use during assertion.

        Returns
        -------
        StatusCode
            Return value of the library call.

        """
        if (
            not self._capabilities["usb488"].trigger
            or protocol != TriggerProtocol.default
        ):
            return StatusCode.error_nonsupported_operation

        raw_write = super().write

        btag = self._btag.next()
        msg = TriggerMessage.build_array(btag)
        raw_write(msg)

        return StatusCode.success
