# -*- coding: utf-8 -*-
"""
    pyvisa-sim.common
    ~~~~~~~~~~~~~~~~~

    Common code.

    :copyright: 2014 by PyVISA-sim Authors, see AUTHORS for more details.
    :license: MIT, see LICENSE for more details.
"""

from __future__ import (division, unicode_literals, print_function,
                        absolute_import)

import logging
import sys

from pyvisa import logger

logger = logging.LoggerAdapter(logger, {'backend': 'py'})


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
