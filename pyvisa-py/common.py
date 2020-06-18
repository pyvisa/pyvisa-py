# -*- coding: utf-8 -*-
"""Common code.

:copyright: 2014-2020 by PyVISA-sim Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""
import logging
from typing import Optional, SupportsBytes

from pyvisa import logger

logger = logging.LoggerAdapter(logger, {"backend": "py"})


class MockInterface(object):

    #: Name of the resource used to create this interface
    resource_name: str

    def __init__(self, resource_name) -> None:
        self.resource_name = resource_name


class NamedObject(object):
    """A class to construct named sentinels."""

    #: Name used to identify the sentinel
    name: str

    def __init__(self, name) -> None:
        self.name = name

    def __repr__(self) -> str:
        return "<%s>" % self.name

    __str__ = __repr__


# XXX can probably be removed
def iter_bytes(data: SupportsBytes, mask: Optional[int] = None, send_end: bool = False):
    if send_end and mask is None:
        raise ValueError("send_end requires a valid mask.")

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
