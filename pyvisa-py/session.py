# -*- coding: utf-8 -*-
"""
    pyvisa-py.session
    ~~~~~~~~~~~~~~~~~

    Base Session class.

    This file is part of PyVISA-py

    :copyright: 2014 by PyVISA-py Authors, see AUTHORS for more details.
    :license: MIT, see LICENSE for more details.
"""

from __future__ import division, unicode_literals, print_function, absolute_import


import abc


class Session(object):
    """A base class for Session objects.

    Just makes sure that common methods are defined and information is stored.
    """
    __metaclass__ = abc.ABCMeta

    def __init__(self, resource_name, resource_class):
        self.resource_name = resource_name
        self.resource_class = resource_class

    def _get_timeout(self): pass
    def _set_timeout(self, value): pass
    timeout = abc.abstractproperty(_get_timeout, _set_timeout)

    # TODO: We also need a few others. Add the minimal List
