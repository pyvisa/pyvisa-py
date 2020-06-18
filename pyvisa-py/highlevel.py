# -*- coding: utf-8 -*-
"""Highlevel wrapper of the VISA Library.


:copyright: 2014-2020 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""
import random
from collections import OrderedDict
from typing import Any, Dict, Iterable, Iterator, Optional, Tuple, Union

from pyvisa import constants, errors, highlevel, rname
from pyvisa.constants import StatusCode
from pyvisa.typing import VISAEventContext, VISARMSession, VISASession

from . import sessions
from .common import logger


class PyVisaLibrary(highlevel.VisaLibraryBase):
    """A pure Python backend for PyVISA.

    The object is basically a dispatcher with some common functions implemented.

    When a new resource object is requested to pyvisa, the library creates a
    Session object (that knows how to perform low-level communication operations)
    associated with a session handle (a number, usually refered just as session).

    A call to a library function is handled by PyVisaLibrary if it involves a
    resource agnostic function or dispatched to the correct session object
    (obtained from the session id).

    Importantly, the user is unaware of this. PyVisaLibrary behaves for
    the user just as NIVisaLibrary.

    """

    # Try to import packages implementing lower level functionality.
    try:
        from .serial import SerialSession

        logger.debug("SerialSession was correctly imported.")
    except Exception as e:
        logger.debug("SerialSession was not imported %s." % e)

    try:
        from .usb import USBRawSession, USBSession

        logger.debug("USBSession and USBRawSession were correctly imported.")
    except Exception as e:
        logger.debug("USBSession and USBRawSession were not imported %s." % e)

    try:
        from .tcpip import TCPIPInstrSession, TCPIPSocketSession

        logger.debug("TCPIPSession was correctly imported.")
    except Exception as e:
        logger.debug("TCPIPSession was not imported %s." % e)

    try:
        from .gpib import GPIBSession

        logger.debug("GPIBSession was correctly imported.")
    except Exception as e:
        logger.debug("GPIBSession was not imported %s." % e)

    @classmethod
    def get_session_classes(cls) -> Dict[sessions.Session]:
        return sessions.Session._session_classes

    @classmethod
    def iter_session_classes_issues(
        cls,
    ) -> Iterator[Tuple[constants.InterfaceType, str], str]:
        return sessions.Session.iter_session_classes_issues()

    @staticmethod
    def get_library_paths() -> Iterable[str]:
        """List a dummy library path to allow to create the library."""
        return ("py",)

    @staticmethod
    def get_debug_info() -> Dict[str, Union[str, Dict[str, str]]]:
        """Return a list of lines with backend info."""
        from . import __version__

        d = OrderedDict()
        d["Version"] = "%s" % __version__

        for key, val in PyVisaLibrary.get_session_classes().items():
            key_name = "%s %s" % (key[0].name.upper(), key[1])
            try:
                d[key_name] = getattr(val, "session_issue").split("\n")
            except AttributeError:
                d[key_name] = "Available " + val.get_low_level_info()

        return d

    def _init(self) -> None:
        """Custom initialization code."""
        #: map session handle to session object.
        #: dict[int, session.Session]
        self.sessions = {}

    def _register(self, obj: object) -> VISASession:
        """Creates a random but unique session handle for a session object.

        Register it in the sessions dictionary and return the value.

        """
        session = None

        while session is None or session in self.sessions:
            session = random.randint(1000000, 9999999)

        self.sessions[session] = obj
        return session

    # noinspection PyShadowingBuiltins
    def open(
        self,
        session: VISARMSession,
        resource_name: str,
        access_mode: constants.AccessModes = constants.AccessModes.no_lock,
        open_timeout: Optional[int] = constants.VI_TMO_IMMEDIATE,
    ) -> Tuple[VISASession, StatusCode]:
        """Opens a session to the specified resource.

        Corresponds to viOpen function of the VISA library.

        Parameters
        ----------
        session : typing.VISARMSession
            Resource Manager session (should always be a session returned from
            open_default_resource_manager()).
        resource_name : str
            Unique symbolic name of a resource.
        access_mode : constants.AccessModes, optional
            Specifies the mode by which the resource is to be accessed.
        open_timeout : Optional[int]
            Specifies the maximum time period (in milliseconds) that this
            operation waits before returning an error.

        Returns
        -------
        typing.VISASession
            Unique logical identifier reference to a session
        StatusCode
            Return value of the library call.

        """
        try:
            open_timeout = int(open_timeout)
        except ValueError:
            raise ValueError(
                "open_timeout (%r) must be an integer (or compatible type)"
                % open_timeout
            )

        try:
            parsed = rname.parse_resource_name(resource_name)
        except rname.InvalidResourceName:
            return 0, StatusCode.error_invalid_resource_name

        cls = sessions.Session.get_session_class(
            parsed.interface_type_const, parsed.resource_class
        )

        sess = cls(session, resource_name, parsed, open_timeout)

        return self._register(sess), StatusCode.success

    def clear(self, session: VISASession) -> StatusCode:
        """Clears a device.

        Corresponds to viClear function of the VISA library.

        Parameters
        ----------
        session : typin.VISASession
            Unique logical identifier to a session.

        Returns
        -------
        StatusCode
            Return value of the library call.

        """
        try:
            sess = self.sessions[session]
        except KeyError:
            return constants.StatusCode.error_invalid_object
        return sess.clear()

    def flush(
        self, session: VISASession, mask: constants.BufferOperation
    ) -> constants.StatusCode:
        """Flush the specified buffers.

        The buffers can be associated with formatted I/O operations and/or
        serial communication.

        Corresponds to viFlush function of the VISA library.

        Parameters
        ----------
        session : VISASession
            Unique logical identifier to a session.
        mask : constants.BufferOperation
            Specifies the action to be taken with flushing the buffer.
            The values can be combined using the | operator. However multiple
            operations on a single buffer cannot be combined.

        Returns
        -------
        constants.StatusCode
            Return value of the library call.

        """
        try:
            sess = self.sessions[session]
        except KeyError:
            return constants.StatusCode.error_invalid_object
        return sess.flush(mask)

    def gpib_command(
        self, session: VISASession, command_byte: bytes
    ) -> Tuple[int, constants.StatusCode]:
        """Write GPIB command bytes on the bus.

        Corresponds to viGpibCommand function of the VISA library.

        Parameters
        ----------
        session : VISASession
            Unique logical identifier to a session.
        command_byte : bytes
            Data to write.

        Returns
        -------
        int
            Number of written bytes
        constants.StatusCode
            Return value of the library call.

        """
        try:
            return self.sessions[session].gpib_command(command_byte)
        except KeyError:
            return constants.StatusCode.error_invalid_object

    def assert_trigger(
        self, session: VISASession, protocol: constants.TriggerProtocol
    ) -> constants.StatusCode:
        """Assert software or hardware trigger.

        Corresponds to viAssertTrigger function of the VISA library.

        Parameters
        ----------
        session : VISASession
            Unique logical identifier to a session.
        protocol : constants.TriggerProtocol
            Trigger protocol to use during assertion.

        Returns
        -------
        constants.StatusCode
            Return value of the library call.

        """
        try:
            return self.sessions[session].assert_trigger(protocol)
        except KeyError:
            return constants.StatusCode.error_invalid_object

    def gpib_send_ifc(self, session: VISASession) -> constants.StatusCode:
        """Pulse the interface clear line (IFC) for at least 100 microseconds.

        Corresponds to viGpibSendIFC function of the VISA library.

        Parameters
        ----------
        session : VISASession
            Unique logical identifier to a session.

        Returns
        -------
        constants.StatusCode
            Return value of the library call.

        """
        try:
            sess = self.sessions[session]
        except KeyError:
            return constants.StatusCode.error_invalid_object
        return sess.gpib_send_ifc()

    def gpib_control_ren(
        self, session: VISASession, mode: constants.RENLineOperation
    ) -> constants.StatusCode:
        """Controls the state of the GPIB Remote Enable (REN) interface line.

        Optionally the remote/local state of the device can also be set.

        Corresponds to viGpibControlREN function of the VISA library.

        Parameters
        ----------
        session : VISASession
            Unique logical identifier to a session.
        mode : constants.RENLineOperation
            State of the REN line and optionally the device remote/local state.

        Returns
        -------
        constants.StatusCode
            Return value of the library call.

        """
        try:
            sess = self.sessions[session]
        except KeyError:
            return constants.StatusCode.error_invalid_object
        return sess.gpib_control_ren()

    def gpib_control_atn(
        self, session: VISASession, mode: constants.ATNLineOperation
    ) -> constants.StatusCode:
        """Specifies the state of the ATN line and the local active controller state.

        Corresponds to viGpibControlATN function of the VISA library.

        Parameters
        ----------
        session : VISASession
            Unique logical identifier to a session.
        mode : constants.ATNLineOperation
            State of the ATN line and optionally the local active controller state.

        Returns
        -------
        constants.StatusCode
            Return value of the library call.

        """
        try:
            sess = self.sessions[session]
        except KeyError:
            return constants.StatusCode.error_invalid_object
        return sess.gpib_control_atn()

    def gpib_pass_control(
        self, session: VISASession, primary_address: int, secondary_address: int
    ) -> constants.StatusCode:
        """Tell a GPIB device to become controller in charge (CIC).

        Corresponds to viGpibPassControl function of the VISA library.

        Parameters
        ----------
        session : VISASession
            Unique logical identifier to a session.
        primary_address : int
            Primary address of the GPIB device to which you want to pass control.
        secondary_address : int
            Secondary address of the targeted GPIB device.
            If the targeted device does not have a secondary address, this parameter
            should contain the value Constants.VI_NO_SEC_ADDR.

        Returns
        -------
        constants.StatusCode
            Return value of the library call.

        """
        try:
            sess = self.sessions[session]
        except KeyError:
            return constants.StatusCode.error_invalid_object
        return sess.gpib_pass_control()

    def read_stb(self, session: VISASession) -> Tuple[int, constants.StatusCode]:
        """Reads a status byte of the service request.

        Corresponds to viReadSTB function of the VISA library.

        Parameters
        ----------
        session : VISASession
            Unique logical identifier to a session.

        Returns
        -------
        int
            Service request status byte
        constants.StatusCode
            Return value of the library call.

        """
        try:
            sess = self.sessions[session]
        except KeyError:
            return 0, constants.StatusCode.error_invalid_object
        return sess.read_stb()

    def close(
        self, session: Union[VISASession, VISAEventContext, VISARMSession]
    ) -> constants.StatusCode:
        """Closes the specified session, event, or find list.

        Corresponds to viClose function of the VISA library.

        Parameters
        ---------
        session : Union[VISASession, VISAEventContext, VISARMSession]
            Unique logical identifier to a session, event, resource manager.

        Returns
        -------
        constants.StatusCode
            Return value of the library call.

        """
        try:
            sess = self.sessions[session]
            if sess is not self:
                sess.close()
        except KeyError:
            return StatusCode.error_invalid_object

    def open_default_resource_manager(
        self,
    ) -> Tuple[VISARMSession, constants.StatusCode]:
        """This function returns a session to the Default Resource Manager resource.

        Corresponds to viOpenDefaultRM function of the VISA library.

        Returns
        -------
        VISARMSession
            Unique logical identifier to a Default Resource Manager session
        constants.StatusCode
            Return value of the library call.

        """
        return self._register(self), StatusCode.success

    def list_resources(
        self, session: VISARMSession, query: str = "?*::INSTR"
    ) -> Tuple[str, ...]:
        """Return a tuple of all connected devices matching query.

        Parameters
        ----------
        session : VISARMSession
            Unique logical identifier to the resource manager session.
        query : str
            Regular expression used to match devices.

        Returns
        -------
        Tuple[str, ...]
            Resource names of all the connected devices matching the query.

        """
        # For each session type, ask for the list of connected resources and
        # merge them into a single list.

        resources = sum(
            [
                st.list_resources()
                for key, st in sessions.Session.iter_valid_session_classes()
            ],
            [],
        )

        resources = rname.filter(resources, query)

        return resources

    def read(
        self, session: VISASession, count: int
    ) -> Tuple[bytes, constants.StatusCode]:
        """Reads data from device or interface synchronously.

        Corresponds to viRead function of the VISA library.

        Parameters
        ----------
        session : VISASession
            Unique logical identifier to a session.
        count : int
            Number of bytes to be read.

        Returns
        -------
        bytes
            Date read
        constants.StatusCode
            Return value of the library call.

        """
        # from the session handle, dispatch to the read method of the session object.
        try:
            ret = self.sessions[session].read(count)
        except KeyError:
            return 0, StatusCode.error_invalid_object

        if ret[1] < 0:
            raise errors.VisaIOError(ret[1])

        return ret

    def write(
        self, session: VISASession, data: bytes
    ) -> Tuple[int, constants.StatusCode]:
        """Write data to device or interface synchronously.

        Corresponds to viWrite function of the VISA library.

        Parameters
        ----------
        session : VISASession
            Unique logical identifier to a session.
        data : bytes
            Data to be written.

        Returns
        -------
        int
            Number of bytes actually transferred
        constants.StatusCode
            Return value of the library call.

        """
        # from the session handle, dispatch to the write method of the session object.
        try:
            ret = self.sessions[session].write(data)
        except KeyError:
            return 0, StatusCode.error_invalid_object

        if ret[1] < 0:
            raise errors.VisaIOError(ret[1])

        return ret

    def buffer_read(
        self, session: VISASession, count: int
    ) -> Tuple[bytes, constants.StatusCode]:
        """Reads data through the use of a formatted I/O read buffer.

        The data can be read from a device or an interface.

        Corresponds to viBufRead function of the VISA library.

        Parameters
        ----------
        session : VISASession\
            Unique logical identifier to a session.
        count : int
            Number of bytes to be read.

        Returns
        -------
        bytes
            Data read
        constants.StatusCode
            Return value of the library call.

        """
        return self.read(session, count)

    def buffer_write(
        self, session: VISASession, data: bytes
    ) -> Tuple[int, constants.StatusCode]:
        """Writes data to a formatted I/O write buffer synchronously.

        Corresponds to viBufWrite function of the VISA library.

        Parameters
        ----------
        session : VISASession
            Unique logical identifier to a session.
        data : bytes
            Data to be written.

        Returns
        -------
        int
            number of written bytes
        constants.StatusCode
            return value of the library call.

        """
        return self.write(session, data)

    def get_attribute(
        self,
        session: Union[VISASession, VISAEventContext, VISARMSession],
        attribute: Union[constants.ResourceAttribute, constants.EventAttribute],
    ) -> Tuple[Any, constants.StatusCode]:
        """Retrieves the state of an attribute.

        Corresponds to viGetAttribute function of the VISA library.

        Parameters
        ----------
        session : Union[VISASession, VISAEventContext]
            Unique logical identifier to a session, event, or find list.
        attribute : Union[constants.ResourceAttribute, constants.EventAttribute]
            Resource or event attribute for which the state query is made.

        Returns
        -------
        Any
            State of the queried attribute for a specified resource
        constants.StatusCode
            Return value of the library call.

        """
        try:
            sess = self.sessions[session]
        except KeyError:
            return None, StatusCode.error_invalid_object

        return sess.get_attribute(attribute)

    def set_attribute(
        self,
        session: VISASession,
        attribute: constants.ResourceAttribute,
        attribute_state: Any,
    ) -> constants.StatusCode:
        """Set the state of an attribute.

        Corresponds to viSetAttribute function of the VISA library.

        Parameters
        ----------
        session : VISASession
            Unique logical identifier to a session.
        attribute : constants.ResourceAttribute
            Attribute for which the state is to be modified.
        attribute_state : Any
            The state of the attribute to be set for the specified object.

        Returns
        -------
        StatusCode
            Return value of the library call.

        """
        try:
            sess = self.sessions[session]
        except KeyError:
            return StatusCode.error_invalid_object

        return sess.set_attribute(attribute, attribute_state)

    def lock(
        self,
        session: VISASession,
        lock_type: constants.Lock,
        timeout: int,
        requested_key: Optional[str] = None,
    ) -> Tuple[str, constants.StatusCode]:
        """Establishes an access mode to the specified resources.

        Corresponds to viLock function of the VISA library.

        Parameters
        ----------
        session : VISASession
            Unique logical identifier to a session.
        lock_type : constants.Lock
            Specifies the type of lock requested.
        timeout : int
            Absolute time period (in milliseconds) that a resource waits to get
            unlocked by the locking session before returning an error.
        requested_key : Optional[str], optional
            Requested locking key in the case of a shared lock. For an exclusive
            lock it should be None.

        Returns
        -------
        Optional[str]
            Key that can then be passed to other sessions to share the lock, or
            None for an exclusive lock.
        StatusCode
            Return value of the library call.

        """
        try:
            sess = self.sessions[session]
        except KeyError:
            return StatusCode.error_invalid_object

        return sess.lock(lock_type, timeout, requested_key)

    def unlock(self, session: VISASession) -> constants.StatusCode:
        """Relinquish a lock for the specified resource.

        Corresponds to viUnlock function of the VISA library.

        Parameters
        ----------
        session : VISASession
            Unique logical identifier to a session.

        Returns
        -------
        StatusCode
            Return value of the library call.

        """
        try:
            sess = self.sessions[session]
        except KeyError:
            return StatusCode.error_invalid_object

        return sess.unlock()

    def disable_event(
        self,
        session: VISASession,
        event_type: constants.EventType,
        mechanism: constants.EventMechanism,
    ) -> StatusCode:
        """Disable notification for an event type(s) via the specified mechanism(s).

        Corresponds to viDisableEvent function of the VISA library.

        Parameters
        ----------
        session : VISASession
            Unique logical identifier to a session.
        event_type : constants.EventType
            Event type.
        mechanism : constants.EventMechanism
            Event handling mechanisms to be disabled.

        Returns
        -------
        StatusCode
            Return value of the library call.

        """
        pass

    def discard_events(
        self,
        session: VISASession,
        event_type: constants.EventType,
        mechanism: constants.EventMechanism,
    ) -> StatusCode:
        """Discard event occurrences for a given type and mechanisms in a session.

        Corresponds to viDiscardEvents function of the VISA library.

        Parameters
        ----------
        session : VISASession
            Unique logical identifier to a session.
        event_type : constans.EventType
            Logical event identifier.
        mechanism : constants.EventMechanism
            Specifies event handling mechanisms to be discarded.

        Returns
        -------
        StatusCode
            Return value of the library call.

        """
        pass
