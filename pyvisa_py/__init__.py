# -*- coding: utf-8 -*-
"""Pure Python backend for PyVISA.


:copyright: 2014-2020 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

import sys

if sys.version_info >= (3, 8):
    from importlib.metadata import PackageNotFoundError, version
else:
    from importlib_metadata import PackageNotFoundError, version  # type: ignore

__version__ = "unknown"
try:
    __version__ = version(__name__)
except PackageNotFoundError:
    # package is not installed
    pass

# noqa: we need to import so that __init_subclass__() is executed once
from . import attributes  # noqa: F401
from .highlevel import PyVisaLibrary

WRAPPER_CLASS = PyVisaLibrary
