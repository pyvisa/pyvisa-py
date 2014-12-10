# -*- coding: utf-8 -*-
"""
    pyvisa-sim.common
    ~~~~~~~~~~~~~~~~~

    Common code.

    :copyright: 2014 by PyVISA-sim Authors, see AUTHORS for more details.
    :license: MIT, see LICENSE for more details.
"""

from __future__ import division, unicode_literals, print_function, absolute_import

import sys

from pyvisa import constants


class MockInterface(object):

    def __init__(self, resource_name):
       self.resource_name = resource_name


class NamedObject(object):
    """A class to construct named sentinels.
    """

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return '<%s>' % self.name

    __str__ = __repr__


if sys.version >= '3':
    def iter_bytes(data, mask=None, send_end=False):

        if send_end and mask is None:
            raise ValueError('send_end requires a valid mask.')

        if mask is None:
            for d in data[:]:
                yield bytes([d])

        else:
            for d in data[:-1]:
                yield bytes([d & ~mask])

            if send_end:
                yield bytes([data[-1] | ~mask])
            else:
                yield bytes([data[-1] & ~mask])

    int_to_byte = lambda val: bytes([val])
    last_int = lambda val: val[-1]
else:
    def iter_bytes(data, mask=None, send_end=False):

        if send_end and mask is None:
            raise ValueError('send_end requires a valid mask.')

        if mask is None:
            for d in data[:]:
                yield d
        else:
            for d in data[:-1]:
                yield chr(ord(d) & ~mask)

            if send_end:
                yield chr(ord(data[-1]) | ~mask)
            else:
                yield chr(ord(data[-1]) & ~mask)

    int_to_byte = chr
    last_int = lambda val: ord(val[-1])


class InvalidResourceName(ValueError):
    pass


_INTERFACE_TYPES = {'ASRL': constants.InterfaceType.asrl,
                    'GPIB': constants.InterfaceType.gpib,
                    'PXI': constants.InterfaceType.pxi,
                    'TCPIP': constants.InterfaceType.tcpip,
                    'USB': constants.InterfaceType.usb,
                    'VXI': constants.InterfaceType.vxi}

_RESOURCE_CLASSES = ('INSTR', 'INTFC', 'BACKPLANE', 'MEMACC', 'SOCKET', 'RAW', 'SERVANT')


#: (str, str) -> (str, *str) -> {}
_SUBPARSER = {}

def register_subparser(interface_type, resource_class):
    """Register a subparser for a given interface type and resource class.

    :type interface_type: str
    :type resource_class: str
    :return: a decorator
    """
    def deco(func):
        _SUBPARSER[(interface_type, resource_class)] = func
        return func

    return deco


def call_subparser(interface_type_part, resource_class, *parts):
    """Call a subparser based on the interface_type and resource_class.

    :type interface_type_part: str
    :type resource_class: str
    :return: dict mapping resource part to value.
    :rtype: dict

    :raises ValueError: if the interface is unknown.
    """
    for interface_type, const in _INTERFACE_TYPES.items():
        if not interface_type_part.upper().startswith(interface_type):
            continue

        first_part = interface_type_part.lstrip(interface_type)
        out = _SUBPARSER[(interface_type, resource_class)](first_part, *parts)
        out.update(interface_type=const, resource_class=resource_class)
        out['canonical_resource_name'] = out['canonical_resource_name'] % out
        return out

    raise ValueError('Unknown interface type %s' % interface_type_part)


def parse_resource_name(resource_name):
    """Parse a resource name and return a dict mapping resource part to value.

    :type resource_name: str
    :rtype: dict

    :raises InvalidResourceName: if the resource name is invalid.
    """
    # TODO Remote VISA

    parts = resource_name.strip().split('::')
    interface_type, parts = parts[0], parts[1:]

    if len(parts) == 0:
        resource_class = 'INSTR'
    elif parts[-1] in _RESOURCE_CLASSES:
        parts, resource_class = parts[:-1], parts[-1]
    else:
        resource_class = 'INSTR'

    try:
        out = call_subparser(interface_type, resource_class, *parts)
        out['resource_name'] = resource_name
        return out
    except KeyError:
        raise InvalidResourceName('Invalid resource name: %s\n'
                                  'Could find subparser for %s and %s' % (resource_name, interface_type, resource_class))
    except InvalidResourceName as e:
        raise InvalidResourceName('Invalid resource name: %s\n'
                                  'The syntax is %s' % (resource_name, str(e)))


@register_subparser('GPIB', 'INSTR')
def _gpib_instr(board, *parts):
    """GPIB Instrument subparser.

    Format:
        GPIB[board]::primary address[::secondary address][::INSTR]

    :raises InvalidResourceName: if the resource name is invalid.
    """

    if not board:
        board = '0'

    if len(parts) == 2:
        primary_address, secondary_address = parts
    elif len(parts) == 1:
        primary_address, secondary_address = parts[0], constants.VI_NO_SEC_ADDR
    else:
        raise InvalidResourceName('GPIB[board]::primary address[::secondary address][::INSTR]')

    return dict(board=board,
                primary_address=primary_address,
                secondary_address=secondary_address,
                canonical_resource_name='GPIB%(board)s::%(primary_address)s::%(secondary_address)s::INSTR')


@register_subparser('GPIB', 'INTFC')
def _gpib_intfc(board, *parts):
    """GPIB Interface subparser.

    Format:
        GPIB[board]::INTFC

    :raises InvalidResourceName: if the resource name is invalid.
    """

    if not board:
        board = '0'

    if len(parts) != 0:
        raise InvalidResourceName('GPIB[board]::INTFC')

    return dict(board=board,
                canonical_resource_name='GPIB%(board)s::INTFC')


@register_subparser('ASRL', 'INSTR')
def _asrl_instr(board, *parts):
    """ASRL Instrument subparser.

    Format:
        ASRLboard[::INSTR]

    :raises InvalidResourceName: if the resource name is invalid.
    """

    if not board:
        raise ValueError('ASRL INSTR requires a board.')

    if len(parts) != 0:
        raise InvalidResourceName('ASRLboard[::INSTR]')

    return dict(board=board,
                canonical_resource_name='ASRL%(board)s::INSTR')


@register_subparser('TCPIP', 'INSTR')
def _tcpip_instr(board, *parts):
    """TCPIP Instrument subparser.

    Format:
        TCPIP[board]::host address[::LAN device name][::INSTR]

    :raises InvalidResourceName: if the resource name is invalid.
    """

    if not board:
        board = '0'

    if len(parts) == 2:
        host_address, lan_device_name = parts
    elif len(parts) == 1:
        host_address, lan_device_name = parts[0], 'inst0'
    else:
        raise InvalidResourceName('TCPIP[board]::host address[::LAN device name][::INSTR]')

    return dict(board=board,
                host_address=host_address,
                lan_device_name=lan_device_name,
                canonical_resource_name='TCPIP%(board)s::%(host_address)s::%(lan_device_name)s::INSTR')


@register_subparser('TCPIP', 'SOCKET')
def _tcpip_socket(board, *parts):
    """TCPIP Socket subparser.

    Format:
        TCPIP[board]::host address::port::SOCKET

    :raises InvalidResourceName: if the resource name is invalid.
    """

    if not board:
        board = '0'

    if len(parts) == 2:
        host_address, port = parts
    elif len(parts) == 1:
        host_address, port = parts[0], 'inst0'
    else:
        raise InvalidResourceName('TCPIP[board]::host address::port::SOCKET')

    return dict(board=board,
                host_address=host_address,
                port=port,
                canonical_resource_name='TCPIP%(board)s::%(host_address)s::%(port)s::SOCKET')


@register_subparser('USB', 'INSTR')
def _usb_instr(board, *parts):
    """USB Instrument subparser.

    Format:
        USB[board]::manufacturer ID::model code::serial number[::USB interface number][::INSTR]

    :raises InvalidResourceName: if the resource name is invalid.
    """

    if not board:
        board = '0'

    if len(parts) == 4:
        manufacturer_id, model_code, serial_number, usb_interface_number = parts
    elif len(parts) == 3:
        manufacturer_id, model_code, serial_number = parts
        usb_interface_number = '0'
    else:
        raise InvalidResourceName('USB[board]::manufacturer ID::model code::serial number[::USB interface number][::INSTR]')

    return dict(board=board,
                manufacturer_id=manufacturer_id,
                model_code=model_code,
                serial_number=serial_number,
                usb_interface_number=usb_interface_number,
                canonical_resource_name='USB%(board)s::%(manufacturer_id)s::%(model_code)s::%(serial_number)s::%(usb_interface_number)s::INSTR')


@register_subparser('USB', 'RAW')
def _usb_raw(board, *parts):
    """USB Raw subparser.

    Format:
        USB[board]::manufacturer ID::model code::serial number[::USB interface number]::RAW

    :raises InvalidResourceName: if the resource name is invalid.
    """

    if not board:
        board = '0'

    if len(parts) == 4:
        manufacturer_id, model_code, serial_number, usb_interface_number = parts
    elif len(parts) == 3:
        manufacturer_id, model_code, serial_number = parts
        usb_interface_number = '0'
    else:
        raise InvalidResourceName('USB[board]::manufacturer ID::model code::serial number[::USB interface number][::INSTR]')

    return dict(board=board,
                manufacturer_id=manufacturer_id,
                model_code=model_code,
                serial_number=serial_number,
                usb_interface_number=usb_interface_number,
                canonical_resource_name='USB%(board)s::%(manufacturer_id)s::%(model_code)s::%(serial_number)s::%(usb_interface_number)s::RAW')


def to_canonical_name(resource_name):
    """Parse a resource name and return the canonical version.

    :type resource_name: str
    :rtype: str
    """
    return parse_resource_name(resource_name)['canonical_resource_name']
