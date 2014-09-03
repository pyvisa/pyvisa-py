# -*- coding: utf-8 -*-
"""
    pyvisa-py
    ~~~~~~~~~

    Pure Python backend for PyVISA.

    This file is part of PyVISA-py

    :copyright: 2014 by PyVISA-py Authors, see AUTHORS for more details.
    :license: MIT, see LICENSE for more details.
"""

from __future__ import division, unicode_literals, print_function, absolute_import

from .highlevel import PyVisaLibrary

WRAPPER_CLASS = PyVisaLibrary

