# -*- coding: utf-8 -*-
"""
    pyvisa-py.highlevel
    ~~~~~~~~~~~~~~~~~~~

    Highlevel wrapper of the VISA Library.


    :copyright: 2014 by PyVISA-py Authors, see AUTHORS for more details.
    :license: MIT, see LICENSE for more details.
"""

from __future__ import division, unicode_literals, print_function, absolute_import

import warnings

import random
import re

from pyvisa import constants, errors, highlevel, rname
from pyvisa.compat import integer_types, OrderedDict

from . import sessions
from .common import logger


class PyVisaLibrary(highlevel.VisaLibraryBase):
    """A pure Python backend for PyVISA.

    The object is basically a dispatcher with some common functions implemented.

    When a new resource object is requested to pyvisa, the library creates a Session object
    (that knows how to perform low-level communication operations) associated with a session handle
    (a number, usually refered just as session).

    A call to a library function is handled by PyVisaLibrary if it involves a resource agnosting
    function or dispatched to the correct session object (obtained from the session id).

    Importantly, the user is unaware of this. PyVisaLibrary behaves for the user just as NIVisaLibrary.
    """

    # Try to import packages implementing lower level functionality.
    try:
        from .serial import SerialSession
        logger.debug('SerialSession was correctly imported.')
    except ImportError as e:
        logger.debug('SerialSession was not imported %s.' % e)

    try:
        from .usb import USBSession
        logger.debug('USBSession was correctly imported.')
    except ImportError as e:
        logger.debug('USBSession was not imported %s.' % e)

    try:
        from .usb import USBRawSession
        logger.debug('USBRawSession was correctly imported.')
    except ImportError as e:
        logger.debug('USBRawSession was not imported %s.' % e)

    try:
        from .tcpip import TCPIPSession
        logger.debug('TCPIPSession was correctly imported.')
    except ImportError as e:
        logger.debug('TCPIPSession was not imported %s.' % e)

    try:
        from .gpib import GPIBSession
        logger.debug('GPIBSession was correctly imported.')
    except ImportError as e:
        logger.debug('GPIBSession was not imported %s.' % e)

    @classmethod
    def get_session_classes(cls):
        return sessions.Session._session_classes

    @classmethod
    def iter_session_classes_issues(cls):
        return sessions.Session.iter_session_classes_issues()

    @staticmethod
    def get_debug_info():
        """Return a list of lines with backend info.
        """
        from . import __version__
        d = OrderedDict()
        d['Version'] = '%s' % __version__

        for key, val in PyVisaLibrary.get_session_classes().items():
            key_name = '%s %s' % (key[0].name.upper(), key[1])
            try:
                d[key_name] = getattr(val, 'session_issue').split('\n')
            except AttributeError:
                d[key_name] = 'Available ' + val.get_low_level_info()

        return d

    def _init(self):

        #: map session handle to session object.
        #: dict[int, session.Session]
        self.sessions = {}

    def _register(self, obj):
        """Creates a random but unique session handle for a session object,
        register it in the sessions dictionary and return the value

        :param obj: a session object.
        :return: session handle
        :rtype: int
        """
        session = None

        while session is None or session in self.sessions:
            session = random.randint(1000000, 9999999)

        self.sessions[session] = obj
        return session

    def _return_handler(self, ret_value, func, arguments):
        """Check return values for errors and warnings.

        TODO: THIS IS JUST COPIED PASTED FROM NIVisaLibrary.
        Needs to be adapted.
        """

        logger.debug('%s%s -> %r',
                     func.__name__, _args_to_str(arguments), ret_value,
                     extra=self._logging_extra)

        try:
            ret_value = constants.StatusCode(ret_value)
        except ValueError:
            pass

        self._last_status = ret_value

        # The first argument of almost all registered visa functions is a session.
        # We store the error code per session
        session = None
        if func.__name__ not in ('viFindNext', ):
            try:
                session = arguments[0]
            except KeyError:
                raise Exception('Function %r does not seem to be a valid '
                                'visa function (len args %d)' % (func, len(arguments)))

            # Functions that use the first parameter to get a session value.
            if func.__name__ in ('viOpenDefaultRM', ):
                # noinspection PyProtectedMember
                session = session._obj.value

            if isinstance(session, integer_types):
                self._last_status_in_session[session] = ret_value
            else:
                # Functions that might or might have a session in the first argument.
                if func.__name__ not in ('viClose', 'viGetAttribute', 'viSetAttribute', 'viStatusDesc'):
                    raise Exception('Function %r does not seem to be a valid '
                                    'visa function (type args[0] %r)' % (func, type(session)))

        if ret_value < 0:
            raise errors.VisaIOError(ret_value)

        if ret_value in self.issue_warning_on:
            if session and ret_value not in self._ignore_warning_in_session[session]:
                warnings.warn(errors.VisaIOWarning(ret_value), stacklevel=2)

        return ret_value

    # noinspection PyShadowingBuiltins
    def open(self, session, resource_name,
             access_mode=constants.AccessModes.no_lock, open_timeout=constants.VI_TMO_IMMEDIATE):
        """Opens a session to the specified resource.

        Corresponds to viOpen function of the VISA library.

        :param session: Resource Manager session (should always be a session returned from open_default_resource_manager()).
        :param resource_name: Unique symbolic name of a resource.
        :param access_mode: Specifies the mode by which the resource is to be accessed. (constants.AccessModes)
        :param open_timeout: Specifies the maximum time period (in milliseconds) that this operation waits
                             before returning an error.
        :return: Unique logical identifier reference to a session, return value of the library call.
        :rtype: session, VISAStatus
        """

        try:
            open_timeout = int(open_timeout)
        except ValueError:
            raise ValueError('open_timeout (%r) must be an integer (or compatible type)' % open_timeout)

        try:
            parsed = rname.parse_resource_name(resource_name)
        except rname.InvalidResourceName:
            return 0, constants.StatusCode.error_invalid_resource_name

        cls = sessions.Session.get_session_class(parsed.interface_type_const, parsed.resource_class)

        sess = cls(session, resource_name, parsed)

        return self._register(sess), constants.StatusCode.success

    def close(self, session):
        """Closes the specified session, event, or find list.

        Corresponds to viClose function of the VISA library.

        :param session: Unique logical identifier to a session, event, or find list.
        :return: return value of the library call.
        :rtype: VISAStatus
        """
        try:
            sess = self.sessions[session]
            if sess is not self:
                sess.close()
        except KeyError:
            return constants.StatusCode.error_invalid_object

    def open_default_resource_manager(self):
        """This function returns a session to the Default Resource Manager resource.

        Corresponds to viOpenDefaultRM function of the VISA library.

        :return: Unique logical identifier to a Default Resource Manager session, return value of the library call.
        :rtype: session, VISAStatus
        """
        return self._register(self), constants.StatusCode.success

    def list_resources(self, session, query='?*::INSTR'):
        """Returns a tuple of all connected devices matching query.

        :param query: regular expression used to match devices.
        """

        # For each session type, ask for the list of connected resources and
        # merge them into a single list.

        resources = sum([st.list_resources()
                         for key, st in sessions.Session.iter_valid_session_classes()], [])

        query = query.replace('?*', '.*')
        matcher = re.compile(query, re.IGNORECASE)

        resources = tuple(res for res in resources if matcher.match(res))

        if resources:
            return resources

        raise errors.VisaIOError(errors.StatusCode.error_resource_not_found.value)

    def read(self, session, count):
        """Reads data from device or interface synchronously.

        Corresponds to viRead function of the VISA library.

        :param session: Unique logical identifier to a session.
        :param count: Number of bytes to be read.
        :return: data read, return value of the library call.
        :rtype: bytes, VISAStatus
        """

        # from the session handle, dispatch to the read method of the session object.
        try:
            return self.sessions[session].read(count)
        except KeyError:
            return constants.StatusCode.error_invalid_object

    def write(self, session, data):
        """Writes data to device or interface synchronously.

        Corresponds to viWrite function of the VISA library.

        :param session: Unique logical identifier to a session.
        :param data: data to be written.
        :type data: str
        :return: Number of bytes actually transferred, return value of the library call.
        :rtype: int, VISAStatus
        """

        # from the session handle, dispatch to the write method of the session object.
        try:
            return self.sessions[session].write(data)
        except KeyError:
            return constants.StatusCode.error_invalid_object

    def get_attribute(self, session, attribute):
        """Retrieves the state of an attribute.

        Corresponds to viGetAttribute function of the VISA library.

        :param session: Unique logical identifier to a session, event, or find list.
        :param attribute: Resource attribute for which the state query is made (see Attributes.*)
        :return: The state of the queried attribute for a specified resource, return value of the library call.
        :rtype: unicode | str | list | int, VISAStatus
        """
        try:
            sess = self.sessions[session]
        except KeyError:
            return None, constants.StatusCode.error_invalid_object

        return sess.get_attribute(attribute)

    def set_attribute(self, session, attribute, attribute_state):
        """Sets the state of an attribute.

        Corresponds to viSetAttribute function of the VISA library.

        :param session: Unique logical identifier to a session.
        :param attribute: Attribute for which the state is to be modified. (Attributes.*)
        :param attribute_state: The state of the attribute to be set for the specified object.
        :return: return value of the library call.
        :rtype: VISAStatus
        """

        try:
            sess = self.sessions[session]
        except KeyError:
            return constants.StatusCode.error_invalid_object

        return sess.set_attribute(attribute, attribute_state)
    
    def disable_event(self, session, event_type, mechanism):
        # TODO: implement this for GPIB finalization
        pass

    def discard_events(self, session, event_type, mechanism):
        # TODO: implement this for GPIB finalization
        pass

