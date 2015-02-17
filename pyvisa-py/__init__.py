# -*- coding: utf-8 -*-
"""
    pyvisa-py
    ~~~~~~~~~

    Pure Python backend for PyVISA.


    :copyright: 2014 by PyVISA-py Authors, see AUTHORS for more details.
    :license: MIT, see LICENSE for more details.
"""

from __future__ import division, unicode_literals, print_function, absolute_import


import pkg_resources

__version__ = "unknown"
try:                # pragma: no cover
    __version__ = pkg_resources.get_distribution('pyvisa-py').version
except:             # pragma: no cover
    pass    # we seem to have a local copy without any repository control or installed without setuptools
            # so the reported version will be __unknown__


from .highlevel import PyVisaLibrary

WRAPPER_CLASS = PyVisaLibrary

