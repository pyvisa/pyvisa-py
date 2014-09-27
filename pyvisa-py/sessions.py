# -*- coding: utf-8 -*-
"""
    pyvisa-py.session
    ~~~~~~~~~~~~~~~~~

    Base Session class.


    :copyright: 2014 by PyVISA-py Authors, see AUTHORS for more details.
    :license: MIT, see LICENSE for more details.
"""

from __future__ import division, unicode_literals, print_function, absolute_import

import abc

from pyvisa import logger, constants, attributes, compat

from . import common


class Session(compat.with_metaclass(abc.ABCMeta)):
    """A base class for Session objects.

    Just makes sure that common methods are defined and information is stored.

    :param resource_manager_session: The session handle of the parent Resource Manager
    :param resource_name: The resource name.
    :param parsed: the parsed resource name (optional).
    """

    def _get_timeout(self): pass
    def _set_timeout(self, value): pass
    timeout = abc.abstractproperty(_get_timeout, _set_timeout)

    @abc.abstractmethod
    def _get_attribute(self, attribute):
        pass

    @abc.abstractmethod
    def _set_attribute(self, attribute, attribute_state):
        pass

    # TODO: We also need a few others. Add the minimal List
    #: Maps (Interface Type, Resource Class) to Python class encapsulating that resource.
    #: dict[(Interface Type, Resource Class) , Session]
    _session_classes = dict()

    #: Session handler for the resource manager.
    session_type = None

    @classmethod
    def get_session_class(cls, interface_type, resource_class):
        """Return the session class for a given interface type and resource class.

        :type interface_type: constants.InterfaceType
        :type resource_class: str
        :return: Session
        """
        try:
            return cls._session_classes[(interface_type, resource_class)]
        except KeyError:
            raise ValueError('No class registered for %s, %s' % (interface_type, resource_class))

    @classmethod
    def register(cls, interface_type, resource_class):
        """Register a session class for a given interface type and resource class.

        :type interface_type: constants.InterfaceType
        :type resource_class: str
        """
        def _internal(python_class):
            if (interface_type, resource_class) in cls._session_classes:
                logger.warning('%s is already registered in the ResourceManager. '
                               'Overwriting with %s' % ((interface_type, resource_class), python_class))

            cls._session_classes[(interface_type, resource_class)] = python_class
            return python_class
        return _internal

    def __init__(self, resource_manager_session, resource_name, parsed=None):
        if parsed is None:
            parsed = common.parse_resource_name(resource_name)
        self.parsed = parsed

        #: Used as a place holder for the object doing the lowlevel communication.
        self.interface = None
        self.attrs = {constants.VI_ATTR_RM_SESSION: resource_manager_session,
                      constants.VI_ATTR_RSRC_NAME: parsed['canonical_resource_name'],
                      constants.VI_ATTR_RSRC_CLASS: parsed['resource_class'],
                      constants.VI_ATTR_INTF_TYPE: parsed['interface_type']}
        self.after_parsing()

    def after_parsing(self):
        pass

    def get_attribute(self, attribute):
        try:
            attr = attributes.AttributesByID[attribute]
        except KeyError:
            return 0, constants.StatusCode.error_nonsupported_attribute

        if not attr.in_resource(self.session_type):
            return 0, constants.StatusCode.error_nonsupported_attribute

        # First try to answer those attributes that are common to all session types,
        # and if not one of those, dispatch to the session object.

        if attribute in self.attrs:
            return self.attrs[attribute], constants.StatusCode.success

        elif attribute == constants.VI_ATTR_TMO_VALUE:
            return self.timeout, constants.StatusCode.success

        if not attr.read:
            raise Exception('Do not now how to handle write only attributes.')

        return self._get_attribute(attribute), constants.StatusCode.success

    def set_attribute(self, attribute, attribute_state):
        try:
            attr = attributes.AttributesByID[attribute]
        except KeyError:
            return constants.StatusCode.error_nonsupported_attribute

        if not attr.in_resource(self.session_type):
            return constants.StatusCode.error_nonsupported_attribute

        if not attr.write:
            return constants.StatusCode.error_attribute_read_only

        try:
            self._set_attribute(attribute, attribute_state)
        except ValueError:
            return constants.StatusCode.error_nonsupported_attribute_state

        return constants.StatusCode.success



