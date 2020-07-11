# -*- coding: utf-8 -*-
"""Common code.

:copyright: 2014-2020 by PyVISA-sim Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""
import logging

from pyvisa import logger

logger = logging.LoggerAdapter(logger, {"backend": "py"})  # type: ignore


class NamedObject(object):
    """A class to construct named sentinels."""

    #: Name used to identify the sentinel
    name: str

    def __init__(self, name) -> None:
        self.name = name

    def __repr__(self) -> str:
        return "<%s>" % self.name

    __str__ = __repr__


int_to_byte = lambda val: val.to_bytes(1, "big")
