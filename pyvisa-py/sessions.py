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
import time

from pyvisa import logger, constants, attributes, compat

from . import common


class UnknownAttribute(Exception):

    def __init__(self, attribute):
        self.attribute = attribute

    def __str__(self):
        attr = self.attribute
        if isinstance(attr, int):
            try:
                name = attributes.AttributesByID[attr].visa_name
            except KeyError:
                name = 'Name not found'

            return 'Unknown attribute %s (%s - %s)' % (attr, hex(attr), name)

        return 'Unknown attribute %s' % attr

    __repr__ = __str__


class Session(compat.with_metaclass(abc.ABCMeta)):
    """A base class for Session objects.

    Just makes sure that common methods are defined and information is stored.

    :param resource_manager_session: The session handle of the parent Resource Manager
    :param resource_name: The resource name.
    :param parsed: the parsed resource name (optional).
                   If not provided, the resource_name will be parsed.
    """

    @abc.abstractmethod
    def _get_attribute(self, attribute):
        """Get the value for a given VISA attribute for this session.

        Use to implement custom logic for attributes.

        :param attribute: Resource attribute for which the state query is made
        :return: The state of the queried attribute for a specified resource, return value of the library call.
        :rtype: unicode (Py2) or str (Py3), list or other type, VISAStatus
        """

    @abc.abstractmethod
    def _set_attribute(self, attribute, attribute_state):
        """Set the attribute_state value for a given VISA attribute for this session.

        Use to implement custom logic for attributes.

        :param attribute: Resource attribute for which the state query is made.
        :param attribute_state: value.
        :return: The return value of the library call.
        :rtype: VISAStatus
        """

    @abc.abstractmethod
    def close(self):
        """Close the session. Use it to do final clean ups.
        """

    #: Maps (Interface Type, Resource Class) to Python class encapsulating that resource.
    #: dict[(Interface Type, Resource Class) , Session]
    _session_classes = dict()

    #: Session type as (Interface Type, Resource Class)
    session_type = None

    @classmethod
    def get_low_level_info(cls):
        return ''

    @classmethod
    def iter_valid_session_classes(cls):
        """Yield (Interface Type, Resource Class), Session class pair for
        valid sessions classes.
        """

        for key, val in cls._session_classes.items():
            if issubclass(val, Session):
                yield key, val

    @classmethod
    def iter_session_classes_issues(cls):
        """Yield (Interface Type, Resource Class), Issues class pair for
        invalid sessions classes (i.e. those with import errors).
        """
        for key, val in cls._session_classes.items():
            try:
                yield key, getattr(val, 'session_issue')
            except AttributeError:
                pass

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

            python_class.session_type = (interface_type, resource_class)
            cls._session_classes[(interface_type, resource_class)] = python_class
            return python_class
        return _internal

    @classmethod
    def register_unavailable(cls, interface_type, resource_class, msg):
        """Register an unavailable session class for a given interface type and resource class.
        raising a ValueError if called.

        :type interface_type: constants.InterfaceType
        :type resource_class: str
        """
        def _internal(*args, **kwargs):
            raise ValueError(msg)

        _internal.session_issue = msg

        if (interface_type, resource_class) in cls._session_classes:
            logger.warning('%s is already registered in the ResourceManager. '
                           'Overwriting with unavailable %s' % ((interface_type, resource_class), msg))

        cls._session_classes[(interface_type, resource_class)] = _internal

    def __init__(self, resource_manager_session, resource_name, parsed=None):
        if isinstance(resource_name, common.MockInterface):
            parsed = common.parse_resource_name(resource_name.resource_name)
            parsed['mock'] = resource_name

        elif parsed is None:
            parsed = common.parse_resource_name(resource_name)

        self.parsed = parsed

        #: Used as a place holder for the object doing the lowlevel communication.
        self.interface = None

        #: Used for attributes not handled by the underlying interface.
        #: Values are get or set automatically by get_attribute and set_attribute
        #: Add your own by overriding after_parsing.
        self.attrs = {constants.VI_ATTR_RM_SESSION: resource_manager_session,
                      constants.VI_ATTR_RSRC_NAME: parsed['canonical_resource_name'],
                      constants.VI_ATTR_RSRC_CLASS: parsed['resource_class'],
                      constants.VI_ATTR_INTF_TYPE: parsed['interface_type']}
        self.after_parsing()

    def after_parsing(self):
        """Override this method to provide custom initialization code, to be
        called after the resourcename is properly parsed
        """

    def get_attribute(self, attribute):
        """Get the value for a given VISA attribute for this session.

        Does a few checks before and calls before dispatching to `_get_attribute`.

        :param attribute: Resource attribute for which the state query is made
        :return: The state of the queried attribute for a specified resource, return value of the library call.
        :rtype: unicode (Py2) or str (Py3), list or other type, VISAStatus
        """

        # Check if the attribute value is defined.
        try:
            attr = attributes.AttributesByID[attribute]
        except KeyError:
            return 0, constants.StatusCode.error_nonsupported_attribute

        # Check if the attribute is defined for this session type.
        if not attr.in_resource(self.session_type):
            return 0, constants.StatusCode.error_nonsupported_attribute

        # Check if reading the attribute is allowed.
        if not attr.read:
            raise Exception('Do not now how to handle write only attributes.')

        # First try to answer those attributes that are common to all session types
        # or user defined becasue they are not defined by the interface.
        if attribute in self.attrs:
            return self.attrs[attribute], constants.StatusCode.success

        elif attribute == constants.VI_ATTR_TMO_VALUE:
            return self.timeout, constants.StatusCode.success

        # Dispatch to `_get_attribute`, which must be implemented by subclasses.

        try:
            return self._get_attribute(attribute), constants.StatusCode.success
        except UnknownAttribute as e:
            logger.exception(str(e))
            return constants.StatusCode.error_nonsupported_attribute

    def set_attribute(self, attribute, attribute_state):
        """Set the attribute_state value for a given VISA attribute for this session.

        Does a few checks before and calls before dispatching to `_gst_attribute`.

        :param attribute: Resource attribute for which the state query is made.
        :param attribute_state: value.
        :return: The return value of the library call.
        :rtype: VISAStatus
        """

        # Check if the attribute value is defined.
        try:
            attr = attributes.AttributesByID[attribute]
        except KeyError:
            return constants.StatusCode.error_nonsupported_attribute

        # Check if the attribute is defined for this session type.
        if not attr.in_resource(self.session_type):
            return constants.StatusCode.error_nonsupported_attribute

        # Check if writing the attribute is allowed.
        if not attr.write:
            return constants.StatusCode.error_attribute_read_only

        # First try to answer those attributes that are common to all session types
        # or user defined because they are not defined by the interface.
        if attribute in self.attrs:
            self.attrs[attribute] = attribute_state
            return constants.StatusCode.success

        elif attribute == constants.VI_ATTR_TMO_VALUE:
            try:
                self.timeout = attribute_state
            except:
                return constants.StatusCode.error_nonsupported_attribute_state

            return constants.StatusCode.success

         # Dispatch to `_set_attribute`, which must be implemented by subclasses.

        try:
            return self._set_attribute(attribute, attribute_state)
        except ValueError:
            return constants.StatusCode.error_nonsupported_attribute_state
        except NotImplementedError:
            e = UnknownAttribute(attribute)
            logger.exception(str(e))
            return constants.StatusCode.error_nonsupported_attribute
        except UnknownAttribute as e:
            logger.exception(str(e))
            return constants.StatusCode.error_nonsupported_attribute

    def _read(self, reader, count, end_indicator_checker, suppress_end_en,
              termination_char, termination_char_en, timeout_exception):
        """Reads data from device or interface synchronously.

        Corresponds to viRead function of the VISA library.

        :param reader: Function to read a single byte.
        :type reader: () -> byte
        :param count: Number of bytes to be read.
        :type count: int
        :param end_indicator_checker: Function to check if the byte.
        :type end_indicator_checker: (byte) -> boolean
        :param suppress_end_en: suppress end.
        :type suppress_end_en: bool
        :param termination_char: Number of bytes to be read.
        :param termination_char_en: termination char enabled.
        :type termination_char_en: boolean
        :param: timeout_exception: Exception to capture time out for the given interface.
        :type: Exception
        :return: data read, return value of the library call.
        :rtype: bytes, constants.StatusCode
        """

        timeout = self.get_attribute(constants.VI_ATTR_TMO_VALUE)[0] / 1000.

        start = time.time()
        out = b''
        while True:
            try:
                current = reader()
            except timeout_exception:
                return out, constants.StatusCode.error_timeout

            if current:
                out += current
                end_indicator_received = end_indicator_checker(current)
                if end_indicator_received and not suppress_end_en:
                    # RULE 6.1.1
                    return out, constants.StatusCode.success

                elif not end_indicator_received and current == termination_char and termination_char_en:
                    # RULE 6.1.2
                    return out, constants.StatusCode.success_termination_character_read

                elif not end_indicator_received and current != termination_char and len(out) == count:
                    # RULE 6.1.3
                    return out, constants.StatusCode.success_max_count_read

            if time.time() - start > timeout:
                return out, constants.StatusCode.error_timeout
