# -*- coding: utf-8 -*-
"""TCPIP Session implementation using Python Standard library.


:copyright: 2014-2024 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

from __future__ import annotations

import ipaddress
import random
import select
import socket
import threading
import time
import warnings
from typing import Any, Dict, Final, List, Optional, Tuple, Type

from pyvisa import attributes, constants, errors, rname
from pyvisa.constants import BufferOperation, ResourceAttribute, StatusCode
from pyvisa.typing import VISAJobID

from .common import LOGGER, connect_timeout, int_to_byte, set_keepalive
from .events import EventContext
from .protocols import hislip, rpc, vxi11
from .sessions import OpenError, Session, UnknownAttribute, VISARMSession

# Let psutil be optional dependency
try:
    import psutil  # type: ignore
except ImportError:
    psutil = None  # type: ignore

# Let zeroconf be optional dependency
try:
    import zeroconf  # type: ignore
except ImportError:
    zeroconf = None  # type: ignore

# Let pyvicp be an optional dependency
try:
    import pyvicp  # type: ignore
except ImportError:
    pyvicp = None  # type: ignore


# Conversion between VXI11 error codes and VISA status
# TODO this is so far a best guess, in particular 6 and 29 are likely wrong
VXI11_ERRORS_TO_VISA = {
    0: StatusCode.success,  # no_error
    1: StatusCode.error_invalid_format,  # syntax_error
    3: StatusCode.error_connection_lost,  # device_not_accessible
    4: StatusCode.error_invalid_access_key,  # invalid_link_identifier
    5: StatusCode.error_invalid_parameter,  # parameter_error
    6: StatusCode.error_handler_not_installed,  # channel_not_established
    8: StatusCode.error_nonsupported_operation,  # operation_not_supported
    9: StatusCode.error_allocation,  # out_of_resources
    11: StatusCode.error_resource_locked,  # device_locked_by_another_link
    12: StatusCode.error_session_not_locked,  # no_lock_held_by_this_link
    15: StatusCode.error_timeout,  # io_timeout
    17: StatusCode.error_io,  # io_error
    23: StatusCode.error_abort,  # abort
    29: StatusCode.error_window_already_mapped,  # channel_already_established
}


def vxi11_error_to_visa(error_code: int) -> StatusCode:
    """Translate a VXI-11 return code into a VISA status code.

    Unknown VXI-11 error codes are translated to ``VI_ERROR_IO`` because
    VISA has no standard status code for them.
    """
    return VXI11_ERRORS_TO_VISA.get(int(error_code), StatusCode.error_io)


def calculate_lock_timeout_from_open_timeout(open_timeout: Optional[int]) -> int:
    # lock_timeout can be 0 for immediate
    lock_timeout = open_timeout
    if lock_timeout is None:
        lock_timeout = 10000  # default lock timeout in ms. This shouldn't happen
    if lock_timeout == constants.VI_TMO_INFINITE:
        lock_timeout = 2**32 - 1  # This is dangerous, but hey, the caller wanted it.
    if lock_timeout == constants.VI_TMO_IMMEDIATE:
        lock_timeout = 0  # This is NOP, but makes the code more readable
    return lock_timeout


@Session.register(constants.InterfaceType.tcpip, "INSTR")
class TCPIPInstrSession(Session):
    """A class to dispatch to VXI11 or HiSLIP, based on the protocol."""

    def __new__(
        cls,
        resource_manager_session: VISARMSession,
        resource_name: str,
        parsed=None,
        access_mode: constants.AccessModes = constants.AccessModes.no_lock,
        open_timeout: Optional[int] = None,
    ):
        newcls: Type

        if parsed is None:
            parsed = rname.parse_resource_name(resource_name)

        if parsed.lan_device_name.lower().startswith("hislip"):
            newcls = TCPIPInstrHiSLIP

        else:
            newcls = TCPIPInstrVxi11

        return newcls(
            resource_manager_session, resource_name, parsed, access_mode, open_timeout
        )

    @staticmethod
    def list_resources(wait_time=1.0) -> List[str]:
        return TCPIPInstrVxi11.list_resources() + TCPIPInstrHiSLIP.list_resources()

    @classmethod
    def get_low_level_info(cls) -> str:
        vxi11 = "ok" if psutil is not None else "partial (psutil not installed)"
        hislip = "ok" if zeroconf is not None else "disabled (zeroconf not installed)"
        return (
            "\n         Resource discovery:"
            f"\n         - VXI-11: {vxi11}"
            f"\n         - hislip: {hislip}"
        )


class TCPIPInstrHiSLIP(Session):
    """A TCPIP Session built on socket standard library using HiSLIP protocol."""

    # we don't decorate this class with Session.register() because we don't
    # want it to be registered in the _session_classes array, but we still
    # need to define session_type to make the set_attribute machinery work.
    session_type = (constants.InterfaceType.tcpip, "INSTR")

    _supported_event_types = {constants.EventType.service_request}

    REMOTELOCALOPCODE: Final[dict[constants.RENLineOperation, str]] = {
        constants.RENLineOperation.address_gtl: "justGTL",
        constants.RENLineOperation.asrt: "enableRemote",
        constants.RENLineOperation.asrt_address: "enableAndGotoRemote",
        constants.RENLineOperation.asrt_address_llo: "enableAndGTRLLO",
        constants.RENLineOperation.asrt_llo: "enableAndLockoutLocal",
        constants.RENLineOperation.deassert: "disableRemote",
        constants.RENLineOperation.deassert_gtl: "disableAndGTL",
    }

    LOCKRESPONSE_TO_STATUSCODE: Final[dict[str, constants.StatusCode]] = {
        hislip.LOCKRESPONSE[0]: constants.StatusCode.error_resource_locked,
        hislip.LOCKRESPONSE[1]: constants.StatusCode.success,
        hislip.LOCKRESPONSE[2]: constants.StatusCode.success,
        hislip.LOCKRESPONSE[3]: constants.StatusCode.error_nonsupported_operation,
    }

    # Override parsed to take into account the fact that this class is only used
    # for a specific kind of resource
    parsed: rname.TCPIPInstr

    default_tcpip_port = 4880

    @staticmethod
    def list_resources(wait_time=1.0) -> List[str]:
        resources = []
        try:
            for host, props in get_services(
                "_hislip._tcp.local.", wait_time=wait_time
            ).items():
                port = TCPIPInstrHiSLIP.default_tcpip_port
                if "port" in props:
                    port = props["port"]
                resources.append(f"TCPIP::{host}::hislip0,{port}::INSTR")
        except NotImplementedError:
            warnings.warn(
                "TCPIP::hislip resource discovery requires the zeroconf package "
                "to be installed... try 'pip install zeroconf'",
                UserWarning,
            )
        return sorted(resources)

    def after_parsing(self) -> None:
        # TODO: board_number not handled

        parsed = self.parsed

        if "," in parsed.lan_device_name:
            sub_address, port_str = parsed.lan_device_name.split(",")
            port = int(port_str)
        else:
            sub_address = parsed.lan_device_name
            port = self.default_tcpip_port

        try:
            self._async_interrupted_message_id: Optional[int] = None
            self.interface = hislip.Instrument(
                parsed.host_address,
                # ``open_timeout`` is already in milliseconds, which is what
                # hislip.Instrument expects.
                open_timeout=self.open_timeout,
                timeout=self.timeout,
                port=port,
                sub_address=sub_address,
                event_callback=self._handle_async_service_request,
                interrupt_callback=self._handle_async_interrupted,
            )
        except Exception as e:
            LOGGER.exception(
                f"Failed to open HiSLIP connection to {parsed.host_address} "
                f"on port {port} with lan device name {sub_address}"
            )
            raise OpenError() from e

        # initialize the constant attributes
        self.attrs[ResourceAttribute.dma_allow_enabled] = constants.VI_FALSE
        self.attrs[ResourceAttribute.file_append_enabled] = constants.VI_FALSE
        self.attrs[ResourceAttribute.interface_instrument_name] = "TCPIP0 (HiSLIP)"
        self.attrs[ResourceAttribute.interface_number] = 0
        self.attrs[ResourceAttribute.io_prot] = constants.VI_PROT_NORMAL
        self.attrs[ResourceAttribute.read_buffer_operation_mode] = (
            constants.VI_FLUSH_DISABLE
        )
        # do NOT set the resource lock state manually; it will be managed by the remote locking mechanism.
        # self.attrs[ResourceAttribute.resource_lock_state] = constants.VI_NO_LOCK
        self.attrs[ResourceAttribute.send_end_enabled] = constants.VI_TRUE
        self.attrs[ResourceAttribute.suppress_end_enabled] = constants.VI_FALSE
        self.attrs[ResourceAttribute.tcpip_address] = parsed.host_address
        self.attrs[ResourceAttribute.tcpip_device_name] = parsed.lan_device_name
        self.attrs[ResourceAttribute.tcpip_hislip_overlap_enable] = constants.VI_FALSE
        self.attrs[ResourceAttribute.tcpip_hislip_version] = 0x0010_0000
        self.attrs[ResourceAttribute.tcpip_hostname] = parsed.host_address
        self.attrs[ResourceAttribute.tcpip_is_hislip] = constants.VI_TRUE
        self.attrs[ResourceAttribute.tcpip_nodelay] = constants.VI_TRUE
        self.attrs[ResourceAttribute.tcpip_port] = port
        self.attrs[ResourceAttribute.termchar] = ord("\n")
        self.attrs[ResourceAttribute.termchar_enabled] = constants.VI_FALSE
        self.attrs[ResourceAttribute.write_buffer_operation_mode] = (
            constants.VI_FLUSH_WHEN_FULL
        )

        # configure the variable attributes
        self.attrs[ResourceAttribute.tcpip_hislip_max_message_kb] = (
            self.get_max_message_kb,
            self.set_max_message_kb,
        )
        self.attrs[ResourceAttribute.tcpip_keepalive] = (
            self.get_keepalive,
            self.set_keepalive,
        )

        # and now handle the lock
        if self.access_mode & constants.AccessModes.exclusive_lock:
            # It wouldn't be too hard to implement shared locking, since
            # the interface likely supports it, but for now we only handle exclusive locks.

            # about the timeout: Found nothing in the spec that says how to handle this.
            # Do what is done by VXI-11
            lock_timeout = calculate_lock_timeout_from_open_timeout(self.open_timeout)
            _k, rv = self.lock(constants.Lock.exclusive, lock_timeout, "")
            if rv != StatusCode.success:
                self.close()  # the operation was open_with_lock. If the lock fails, the open should also be reversed.
                raise RuntimeError("Failed to acquire exclusive lock")

    def _handle_async_service_request(self, status_byte: int) -> None:
        ctx = EventContext(
            event_type=constants.EventType.service_request,
            context_id=status_byte,
        )
        self._fire_event(constants.EventType.service_request, ctx)

    def _handle_async_interrupted(self, message_id: int) -> None:
        self._async_interrupted_message_id = message_id

        # TODO: additional attributes (someday)
        # self.attrs[ResourceAttribute.manufacturer_id] = 16711
        # self.attrs[ResourceAttribute.max_queue_length] = 50
        # self.attrs[ResourceAttribute.read_buffer_size] = 4096
        # self.attrs[ResourceAttribute.resource_impl_version] = 0x0050_0c01
        # self.attrs[ResourceAttribute.resource_manufacturer_id] = 4015
        # self.attrs[ResourceAttribute.resource_manufacturer_name] = 'Rohde & Schwarz GmbH'
        # self.attrs[ResourceAttribute.resource_spec_version] = 0x0050_0800
        # self.attrs[ResourceAttribute.user_data] = 0
        # self.attrs[ResourceAttribute.write_buffer_size] = 4096

    def get_max_message_kb(
        self, attribute: ResourceAttribute
    ) -> Tuple[int, StatusCode]:
        """Get the maximum HiSLIP message size in kilobytes."""
        max_msg_size_kb = round(self.interface.max_msg_size / 1024)
        return max_msg_size_kb, StatusCode.success

    def set_max_message_kb(
        self, attribute: ResourceAttribute, size_kb: int
    ) -> StatusCode:
        """Set the maximum HiSLIP message size in kilobytes."""
        if size_kb < 1:
            raise ValueError("size must be >= 1 kilobyte")

        if size_kb > 0xFFFF_FFFF:
            raise ValueError("size exceeds the range in the VISA spec")

        self.interface.max_msg_size = round(size_kb * 1024)
        return StatusCode.success

    def get_keepalive(self, attribute: ResourceAttribute) -> Tuple[bool, StatusCode]:
        """Is TCP keepalive enabled for the resource."""
        return self.interface.keepalive, StatusCode.success

    def set_keepalive(
        self, attribute: ResourceAttribute, keepalive: bool
    ) -> StatusCode:
        """Turns TCP keepalive on/off for this connection."""
        self.interface.keepalive = keepalive
        return StatusCode.success

    def close(self) -> StatusCode:
        self.interface.close()
        self.interface = None
        return StatusCode.success

    def _set_timeout(self, attribute: ResourceAttribute, value: int) -> StatusCode:
        status = super()._set_timeout(attribute, value)
        if hasattr(self.interface, "timeout"):
            self.interface.timeout = 1e-3 * value

        return status

    def read(self, count: int) -> Tuple[bytes, StatusCode]:
        """Reads data from device or interface synchronously.

        Corresponds to viRead function of the VISA library.

         Parameters
        -----------
        count : int
            Number of bytes to be read.

        Returns
        -------
        bytes
            Data read from the device
        StatusCode
            Return value of the library call.

        """
        try:
            data = self.interface.receive(count)
            status = (
                StatusCode.success_termination_character_read
                if self.interface._rmt
                else StatusCode.success_max_count_read
                if len(data) >= count
                else StatusCode.success
            )

        except hislip.HiSLIPInterruptedError:
            # terminate() was called from another thread.  Reset the HiSLIP
            # protocol state so the session is ready for further I/O.
            self.interface.complete_terminate()
            data, status = b"", StatusCode.error_abort

        except socket.timeout:
            data, status = b"", StatusCode.error_timeout

        return data, status

    def write(self, data: bytes) -> Tuple[int, StatusCode]:
        """Writes data to device or interface synchronously.

        Corresponds to viWrite function of the VISA library.

        Parameters
        ----------
        data : bytes
            Data to be written.

        Returns
        -------
        int
            Number of bytes actually transferred
        StatusCode
            Return value of the library call.

        """
        self.interface.send(data)

        return len(data), StatusCode.success

    def gpib_control_ren(self, mode: constants.RENLineOperation) -> StatusCode:
        """Controls the state of the GPIB Remote Enable (REN) interface line.

        Optionally the remote/local state of the device is also controlled.

        Corresponds to viGpibControlREN function of the VISA library.

        Parameters
        ----------
        mode : constants.RENLineOperation
            Specifies the state of the REN line and optionally the device
            remote/local state.

        Returns
        -------
        StatusCode
            Return value of the library call.

        """
        try:
            method = self.REMOTELOCALOPCODE[mode]
        except KeyError:
            # unknown value?
            return StatusCode.error_nonsupported_operation

        self.interface.async_remote_local_control(method)

        return StatusCode.success

    def clear(self) -> StatusCode:
        """Clears a device.

        Corresponds to viClear function of the VISA library.

        """
        self.interface.device_clear()

        return StatusCode.success

    def assert_trigger(self, protocol: constants.TriggerProtocol):
        """Asserts software or hardware trigger.

        Corresponds to viAssertTrigger function of the VISA library.

        Parameters
        ----------
        protocol : constants.TriggerProtocol
            Trigger protocol to use during assertion. Only default is supported.

        Returns
        -------
        StatusCode
            Return value of the library call.

        """
        if protocol != constants.TriggerProtocol.default:
            return StatusCode.error_nonsupported_operation

        # Contrary to serial and socket, io_prot is not used, because VPP-4.3 does not explicitly demands it.

        self.interface.trigger()
        return StatusCode.success

    def read_stb(self) -> Tuple[int, StatusCode]:
        """Reads a status byte of the service request.

        Corresponds to viReadSTB function of the VISA library.

        Returns
        -------
        int
            Service request status byte
        StatusCode
            Return value of the library call.

        """

        # According to IVI-6.1 Rev.2 status query corresponds to viReadSTB.
        stb = self.interface.async_status_query()
        errorcode = StatusCode.success

        return stb, errorcode

    def lock(
        self,
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
        # For now, we only handle exclusive locks.
        # It could be possible to handle shared locks in the future
        if lock_type != constants.Lock.exclusive:
            return "", StatusCode.error_nonsupported_operation

        # requested_key is ignored for exclusive locks.
        # Note to future: requested_key None is invalid: make it an empty string
        rv = self.interface.async_lock_request(timeout, "")
        # rv is from LOCKRESPONSE

        return "", self.LOCKRESPONSE_TO_STATUSCODE.get(
            rv, StatusCode.error_nonsupported_operation
        )

    def unlock(self) -> constants.StatusCode:
        """Relinquish a lock for the specified resource.

        Corresponds to viUnlock function of the VISA library.

        Returns
        -------
        StatusCode
            Return value of the library call.

        """
        rv = self.interface.async_lock_release("")
        # rv is from LOCKRESPONSE
        return self.LOCKRESPONSE_TO_STATUSCODE.get(
            rv, StatusCode.error_nonsupported_operation
        )

    def terminate(self, job_id: VISAJobID | None = None) -> StatusCode:
        """Cancel a pending I/O operation on this session.

        Corresponds to viTerminate function of the VISA library.

        Thread-safe: may be called from any thread while another thread is
        blocked in read().  The blocked read() will return with
        StatusCode.error_abort and automatically reset the HiSLIP protocol
        state so the session is ready for further I/O.

        .. note::

            This implementation goes beyond what mainstream VISA libraries
            (e.g. Keysight IO Libraries) provide for synchronous HiSLIP reads.
            Keysight's ``viTerminate()`` returns ``VI_SUCCESS`` but does not
            actually cancel a blocked synchronous ``viRead()``; the read
            continues until the normal timeout expires.  This implementation
            truly cancels the blocked read via a cancel pipe and performs a
            HiSLIP device clear to re-sync the protocol.  Code relying on
            this behavior may not be portable to other VISA backends.

        Parameters
        ----------
        job_id : VISAJobID | None, optional
            Specifies an operation identifier.  If None, aborts all calls
            on this session.

        Returns
        -------
        StatusCode
            Return value of the library call.

        """
        self.interface.terminate()
        return StatusCode.success

    def _get_attribute(self, attribute: ResourceAttribute) -> Tuple[Any, StatusCode]:
        """Get the value for a given VISA attribute for this session.

        Use to implement custom logic for attributes.

        Parameters
        ----------
        attribute : ResourceAttribute
            Attribute for which the state query is made

        Returns
        -------
        Any
            State of the queried attribute for a specified resource
        StatusCode
            Return value of the library call.

        """
        # get the lock state from the instrument itself:
        # RULE 3.6.6
        # IF a session uses HiSLIP, THEN a VISA implementation SHALL return the HiSLIP remote lock state
        # for VI_ATTR_RSRC_LOCK_STATE.
        if attribute == constants.VI_ATTR_RSRC_LOCK_STATE:
            exclusive_lock = self.interface.async_lock_info()
            # exclusive_lock: 0: No exclusive lock granted
            #                 1: Exclusive lock granted
            # we do not do shared locks (yet)
            if exclusive_lock not in (0, 1):
                raise ValueError(f"Unexpected exclusive_lock value: {exclusive_lock}")
            else:
                if exclusive_lock == 0:
                    return constants.VI_NO_LOCK, StatusCode.success
                else:
                    return constants.VI_EXCLUSIVE_LOCK, StatusCode.success
        raise UnknownAttribute(attribute)

    def _set_attribute(
        self, attribute: ResourceAttribute, attribute_state: Any
    ) -> StatusCode:
        """Sets the state of an attribute.

        Corresponds to viSetAttribute function of the VISA library.

        Parameters
        ----------
        attribute : constants.ResourceAttribute
            Attribute for which the state is to be modified. (Attributes.*)
        attribute_state : Any
            The state of the attribute to be set for the specified object.

        Returns
        -------
        StatusCode
            Return value of the library call.

        """
        raise UnknownAttribute(attribute)


class Vxi11CoreClient(vxi11.CoreClient):
    """
    make a connection using vxi11 protocol, optionally allowing the port number
    to be specified.  although in general the port number must be obtained by
    querying the portmapper, in practice any given instrument typically always
    uses the same port number.  this allows you to open that port on a firewall
    or set up an ssh tunnel to that port.

    """

    def __init__(
        self,
        host: str,
        port: Optional[int],
        access_mode: constants.AccessModes = constants.AccessModes.no_lock,
        open_timeout: Optional[int] = None,
    ) -> None:
        self._lock = threading.Lock()
        self.packer = vxi11.Vxi11Packer()
        self.unpacker = vxi11.Vxi11Unpacker(b"")
        prog, vers = vxi11.DEVICE_CORE_PROG, vxi11.DEVICE_CORE_VERS

        if port is None:
            rpc.TCPClient.__init__(self, host, prog, vers, open_timeout)
        else:
            # bypass the portmapper lookup and use the specified port instead
            rpc.RawTCPClient.__init__(self, host, prog, vers, port, open_timeout)

    def make_call(self, proc, args, pack_func, unpack_func):
        with self._lock:
            return super().make_call(proc, args, pack_func, unpack_func)


class TCPIPInstrVxi11(Session):
    """A TCPIP Session built on socket standard library using VXI-11 protocol."""

    # we don't decorate this class with Session.register() because we don't
    # want it to be registered in the _session_classes array, but we still
    # need to define session_type to make the set_attribute machinery work.
    session_type = (constants.InterfaceType.tcpip, "INSTR")

    _supported_event_types = {constants.EventType.service_request}

    #: Maximum size of a chunk of data in bytes.
    max_recv_size: int

    #: Unique ID of the client used to authenticate messages.
    client_id: int

    #: ID of the link used for VXI-11 communication
    link: int

    # Override parsed to take into account the fact that this class is only used
    # for a specific kind of resource
    parsed: rname.TCPIPInstr

    # Setting if keepalive has been activated
    keepalive: bool

    @staticmethod
    def list_resources() -> List[str]:
        broadcast_addr = []
        if psutil is not None:
            # Get broadcast address for each interface
            for interface, snics in psutil.net_if_addrs().items():
                for snic in snics:
                    if snic.family is socket.AF_INET and snic.netmask is not None:
                        addr = snic.address
                        mask = snic.netmask
                        network = ipaddress.IPv4Network(addr + "/" + mask, strict=False)
                        broadcast_addr.append(str(network.broadcast_address))
        else:
            # If psutil unavailable fallback to default interface
            broadcast_addr.append("255.255.255.255")
            warnings.warn(
                "TCPIP:instr resource discovery is limited to the default interface."
                "Install psutil: pip install psutil if you want to scan all interfaces.",
                UserWarning,
            )

        pmap_list = [rpc.BroadcastUDPPortMapperClient(ip) for ip in broadcast_addr]
        for pmap in list(pmap_list):
            pmap.set_timeout(0)
            try:
                pmap.send_port(
                    (vxi11.DEVICE_CORE_PROG, vxi11.DEVICE_CORE_VERS, rpc.IPPROTO_TCP, 0)
                )
            except rpc.RPCError:
                pmap_list.remove(pmap)

        # Timeout for responses
        time.sleep(1)

        all_res = []
        for pmap in pmap_list:
            try:
                resp = pmap.recv_port(
                    (vxi11.DEVICE_CORE_PROG, vxi11.DEVICE_CORE_VERS, rpc.IPPROTO_TCP, 0)
                )
            except rpc.RPCError:
                pass
            else:
                res = [r[1][0] for r in resp if r[0] > 0]
                res = sorted(
                    res, key=lambda ip: tuple(int(part) for part in ip.split("."))
                )
                # TODO: Detect GPIB over TCPIP
                res = ["TCPIP::{}::INSTR".format(host) for host in res]
                all_res.extend(res)

        return all_res

    def after_parsing(self) -> None:
        # TODO: board_number not handled

        host_address = self.parsed.host_address
        if "," in host_address:
            host_address, port_str = host_address.split(",")
            port = int(port_str)
        else:
            port = None
        try:
            self.interface = Vxi11CoreClient(
                host_address, port, self.access_mode, self.open_timeout
            )
        except rpc.RPCError:
            LOGGER.exception(
                f"Failed to open VX11 connection to {host_address} on port {port}"
            )
            raise OpenError()

        self.client_id = random.getrandbits(31)
        self.keepalive = False
        self._srq_server: vxi11.SrqInterruptTCPServer | None = None
        self._srq_lifecycle_lock = threading.Lock()

        # RULE B.6.6:
        # The operation of create_link SHALL ignore locks if lockDevice is false.
        # RULE B.6.7:
        # If lockDevice is true and the lock is not freed after at least lock_timeout milliseconds, create_link
        # SHALL terminate without creating a link and return with error set to 11, device locked by another link.

        # However, some devices, even from the big brands, once they are locked, will respect nothing of the above.
        # If there is already a lock, expect some devices to act as if lockDevice is True,
        # to use arbitrarily longer timeouts, and expect a reply of "timeout" instead of "device locked by another link".
        # Comparable liberties are likely to be taken on device_lock().

        if self.access_mode & constants.AccessModes.exclusive_lock:
            lock_device = 1
            lock_timeout = calculate_lock_timeout_from_open_timeout(self.open_timeout)
        else:
            lock_device = 0
            lock_timeout = 0  # time is not used now.

        error, link, _abort_port, max_recv_size = self.interface.create_link(
            self.client_id, lock_device, lock_timeout, self.parsed.lan_device_name
        )

        if error:
            raise Exception("error creating link: %d" % error)

        self.link = link
        self.max_recv_size = min(max_recv_size, 2**30)  # 1GB

        self.attrs[ResourceAttribute.tcpip_is_hislip] = False
        self.attrs[ResourceAttribute.tcpip_address] = self.parsed.host_address
        self.attrs[ResourceAttribute.tcpip_hostname] = ""
        self.attrs[ResourceAttribute.tcpip_device_name] = self.parsed.lan_device_name
        for name in ("SEND_END_EN", "TERMCHAR", "TERMCHAR_EN", "SUPPRESS_END_EN"):
            attribute = getattr(constants, "VI_ATTR_" + name)
            self.attrs[attribute] = attributes.AttributesByID[attribute].default

        if lock_device:
            self.attrs[ResourceAttribute.resource_lock_state] = (
                constants.VI_EXCLUSIVE_LOCK
            )
        else:
            self.attrs[ResourceAttribute.resource_lock_state] = constants.VI_NO_LOCK

        # add the Keysight and PyVISA-Py specific lock wait attribute, which is a boolean that
        # controls whether to wait for the lock or not
        self.attrs[ResourceAttribute.lockwait] = constants.VI_FALSE  # type: ignore[attr-defined]

    def close(self) -> StatusCode:
        self._stop_event_monitor()
        try:
            self.interface.destroy_link(self.link)
        except (errors.VisaIOError, socket.error, rpc.RPCError) as e:
            LOGGER.error("Error closing VISA link: {}".format(e))

        self.interface.close()
        self.interface = None

        return StatusCode.success

    def _start_event_monitor(self) -> StatusCode:
        """Start the VXI-11 interrupt server and enable events on the device."""
        with self._srq_lifecycle_lock:
            with self._event_state._lock:
                if (
                    self._event_state.monitor_thread is not None
                    and self._event_state.monitor_thread.is_alive()
                ):
                    return StatusCode.success
                if not self._event_state.any_enabled():
                    return StatusCode.success

            self._event_state.stop_flag.clear()

            server = vxi11.SrqInterruptTCPServer(
                "",
                vxi11.DEVICE_INTR_PROG,
                vxi11.DEVICE_INTR_VERS,
                0,
                self,
            )
            port = server.sock.getsockname()[1]

            local_ip_str = self.interface.sock.getsockname()[0]
            host_addr = int(ipaddress.IPv4Address(local_ip_str))

            error = self.interface.create_intr_chan(
                host_addr,
                port,
                vxi11.DEVICE_INTR_PROG,
                vxi11.DEVICE_INTR_VERS,
                vxi11.DEVICE_TCP,
            )
            if error:
                LOGGER.error("create_intr_chan failed with error %d", error)
                try:
                    server.sock.close()
                except Exception:
                    pass
                return vxi11_error_to_visa(error)

            error = self.interface.device_enable_srq(self.link, True, b"srq")
            if error:
                LOGGER.error("device_enable_srq failed with error %d", error)
                try:
                    self.interface.destroy_intr_chan()
                except Exception:
                    pass
                try:
                    server.sock.close()
                except Exception:
                    pass
                return vxi11_error_to_visa(error)

            with self._event_state._lock:
                if (
                    self._event_state.monitor_thread is not None
                    and self._event_state.monitor_thread.is_alive()
                ):
                    try:
                        server.sock.close()
                    except Exception:
                        pass
                    return StatusCode.success
                thread = threading.Thread(target=server.loop, daemon=True)
                self._event_state.monitor_thread = thread
                self._srq_server = server
            thread.start()
            return StatusCode.success

    def _stop_event_monitor(self) -> None:
        """Disable events and stop the interrupt server thread."""
        if self._srq_server is None:
            return
        with self._srq_lifecycle_lock:
            self._event_state.stop_flag.set()
            try:
                # even in disable, you must provide a body, so we use the same body as in enable
                self.interface.device_enable_srq(self.link, False, b"srq")
            except Exception:
                LOGGER.exception("Error disabling VXI-11 SRQ")
            try:
                self.interface.destroy_intr_chan()
            except Exception:
                LOGGER.exception("Error destroying VXI-11 interrupt channel")
            with self._event_state._lock:
                thread = self._event_state.monitor_thread
                self._event_state.monitor_thread = None
                server = self._srq_server
                self._srq_server = None

        if thread is not None:
            thread.join(timeout=1.0)
        if server is not None:
            try:
                server.stop()
            except Exception:
                pass
            try:
                server.sock.close()
            except Exception:
                pass

    def _adapt_flags_and_lock_timeout(self, flags: int) -> Tuple[int, int]:
        # Do as Keysight does it:

        lock_timeout = constants.VI_TMO_IMMEDIATE
        if self.attrs[ResourceAttribute.lockwait] == constants.VI_TRUE:  # type: ignore[attr-defined]
            # Get the timeout as cleaned up by the upper layers
            lock_timeout = self._io_timeout  # is in ms
            flags |= vxi11.OP_FLAG_WAIT_BLOCK
        return flags, lock_timeout

    def _read_status_from_reason(
        self, reason: int, suppress_end_en: bool, termchar_en: bool
    ) -> StatusCode:
        """Completion status for a read, from the reason the device gave.

        VPP-4.3 RULE 6.1.1 gives the END indicator priority: it is answered
        regardless of whether the termination character was also read, so it
        is tested first. RULE 6.1.4 withholds VI_SUCCESS while
        VI_ATTR_SUPPRESS_END_EN is set, and RULE 6.1.5 withholds
        VI_SUCCESS_TERM_CHAR while VI_ATTR_TERMCHAR_EN is clear.
        """
        if not suppress_end_en and (reason & vxi11.RX_END):
            return StatusCode.success
        if termchar_en and (reason & vxi11.RX_CHR):
            return StatusCode.success_termination_character_read
        return StatusCode.success_max_count_read

    def read(self, count: int) -> Tuple[bytes, StatusCode]:
        """Reads data from device or interface synchronously.

        Corresponds to viRead function of the VISA library.

        Parameters
        ----------
        count : int
            Number of bytes to be read.

        Returns
        -------
        bytes
            Data read
        StatusCode
            Return value of the library call.

        """
        if count < 0:
            return b"", StatusCode.error_invalid_parameter
        if count == 0:
            return b"", StatusCode.success_max_count_read

        chunk_length = min(count, self.max_recv_size)

        termchar_en, _ = self.get_attribute(ResourceAttribute.termchar_enabled)
        if termchar_en:
            term_char, _ = self.get_attribute(ResourceAttribute.termchar)
            flags = vxi11.OP_FLAG_TERMCHAR_SET
        else:
            term_char = flags = 0

        flags, lock_timeout = self._adapt_flags_and_lock_timeout(flags)

        suppress_end_en, _ = self.get_attribute(ResourceAttribute.suppress_end_enabled)

        read_data = bytearray()
        reason = 0
        # RX_CHR always stops the read. RX_END only stops when suppress_end_en
        # is disabled.
        stop_reason = vxi11.RX_CHR
        if not suppress_end_en:
            stop_reason |= vxi11.RX_END
        read_fun = self.interface.device_read
        status = StatusCode.success

        # Get the timeout as cleaned up by the upper layers
        timeout = self._io_timeout

        # See if a timeout was really set.
        # if self.timeout is None, the given timeout was VI_TMO_INFINITE
        finite_timeout = self.timeout is not None

        start_time = time.time()
        while reason & stop_reason == 0:
            # Decrease timeout so that the total timeout does not get larger
            # than the specified timeout.

            # Calculate timeout for current chunk.
            # Limit the minimum timeout to 10ms, because 0 is undefined
            # according to VXI-11 spec (done also by Visas)
            if finite_timeout:
                elapsed_ms = int((time.time() - start_time) * 1000)
                remaining_timeout = timeout - elapsed_ms
                if remaining_timeout <= 0:
                    return bytes(read_data), StatusCode.error_timeout
            else:
                remaining_timeout = constants.VI_TMO_INFINITE  # This is 2**32 - 1
            chunk_timeout = max(10, remaining_timeout)
            error, reason, data = read_fun(
                self.link,
                chunk_length,
                chunk_timeout,
                lock_timeout,
                flags,
                term_char,
            )

            if error:
                return bytes(read_data), vxi11_error_to_visa(error)

            read_data.extend(data)
            count -= len(data)

            if count <= 0:
                status = self._read_status_from_reason(
                    reason, suppress_end_en, termchar_en
                )
                break

            chunk_length = min(count, chunk_length)

        if count > 0 and (reason & stop_reason):
            status = self._read_status_from_reason(reason, suppress_end_en, termchar_en)

        return bytes(read_data), status

    def write(self, data: bytes) -> Tuple[int, StatusCode]:
        """Writes data to device or interface synchronously.

        Corresponds to viWrite function of the VISA library.

        Parameters
        ----------
        data : bytes
            Data to be written.

        Returns
        -------
        int
            Number of bytes actually transferred
        StatusCode
            Return value of the library call.

        """
        try:
            flags = 0
            num = len(data)
            offset = 0

            flags, lock_timeout = self._adapt_flags_and_lock_timeout(flags)

            while num > 0:
                if num <= self.max_recv_size:
                    flags |= vxi11.OP_FLAG_END

                block = data[offset : offset + self.max_recv_size]

                error, size = self.interface.device_write(
                    self.link, self._io_timeout, lock_timeout, flags, block
                )

                if error or size < len(block):
                    return offset, vxi11_error_to_visa(error)

                offset += size
                num -= size

            return offset, StatusCode.success

        except vxi11.Vxi11Error:
            return 0, StatusCode.error_timeout

    def gpib_control_ren(self, mode: constants.RENLineOperation) -> StatusCode:
        """Controls the state of the GPIB Remote Enable (REN) interface line.

        Optionally the remote/local state of the device is also controlled.

        Corresponds to viGpibControlREN function of the VISA library.

        Parameters
        ----------
        mode : constants.RENLineOperation
            Specifies the state of the REN line and optionally the device
            remote/local state.

        Returns
        -------
        StatusCode
            Return value of the library call.

        """
        flags, lock_timeout = self._adapt_flags_and_lock_timeout(0)
        if mode in (
            constants.RENLineOperation.asrt_address,
            constants.RENLineOperation.asrt_address_llo,
        ):
            error = self.interface.device_remote(
                self.link, flags, lock_timeout, self._io_timeout
            )
            return vxi11_error_to_visa(error)
        elif mode in (
            constants.RENLineOperation.address_gtl,
            constants.RENLineOperation.deassert_gtl,
        ):
            error = self.interface.device_local(
                self.link, flags, lock_timeout, self._io_timeout
            )
            return vxi11_error_to_visa(error)
        else:
            return constants.StatusCode.error_nonsupported_operation

    def _get_attribute(self, attribute: ResourceAttribute) -> Tuple[Any, StatusCode]:
        """Get the value for a given VISA attribute for this session.

        Use to implement custom logic for attributes.

        Parameters
        ----------
        attribute :
            Resource attribute for which the state query is made

        Returns
        -------
        Any
            The state of the queried attribute for a specified resource
        StatusCode
            Return value of the library call.

        """
        # This is an abuse of the VISA standard
        if attribute == constants.VI_ATTR_TCPIP_KEEPALIVE:
            return self.keepalive, StatusCode.success

        if attribute == constants.VI_KTATTR_LOCKWAIT:  # type: ignore[attr-defined]
            return self.attrs[ResourceAttribute.lockwait], StatusCode.success  # type: ignore[attr-defined]

        raise UnknownAttribute(attribute)

    def _set_attribute(
        self, attribute: ResourceAttribute, attribute_state: Any
    ) -> StatusCode:
        """Sets the state of an attribute.

        Corresponds to viSetAttribute function of the VISA library.

        Parameters
        ----------
        attribute : ResourceAttribute
             Attribute for which the state is to be modified.
        attribute_state : Any
            The state of the attribute to be set for the specified object.

        Returns
        -------
        StatusCode
            Return value of the library call.

        """

        # In case of an environment with idle socket garbage collection (like docker)
        # sockets need to be kept alive. Set pyvisa.constants.ResourceAttribute.tcpip_keepalive to enable
        # keepalive packets even for VXI11 protocol. To read more on this issue
        # https://tech.xing.com/a-reason-for-unexplained-connection-timeouts-on-kubernetes-docker-abd041cf7e02
        if attribute == constants.VI_ATTR_TCPIP_KEEPALIVE:
            if attribute_state is True:
                set_keepalive(self.interface.sock, True)
                self.keepalive = True
            elif attribute_state is False:
                set_keepalive(self.interface.sock, False)
                self.keepalive = False
            else:
                return StatusCode.error_nonsupported_format
            return StatusCode.success

        if attribute == constants.VI_KTATTR_LOCKWAIT:  # type: ignore[attr-defined]
            return StatusCode.success

        raise UnknownAttribute(attribute)

    def assert_trigger(self, protocol: constants.TriggerProtocol):
        """Asserts software or hardware trigger.

        Corresponds to viAssertTrigger function of the VISA library.

        Parameters
        ----------
        protocol : constants.TriggerProtocol
            Trigger protocol to use during assertion. Only default is supported.

        Returns
        -------
        StatusCode
            Return value of the library call.

        """
        if protocol != constants.TriggerProtocol.default:
            return StatusCode.error_nonsupported_operation

        # Contrary to serial and socket, io_prot is not used, because VPP-4.3 does not explicitly demands it.

        flags = 0
        flags, lock_timeout = self._adapt_flags_and_lock_timeout(flags)

        error = self.interface.device_trigger(
            self.link, flags, lock_timeout, self._io_timeout
        )

        return vxi11_error_to_visa(error)

    def clear(self) -> StatusCode:
        """Clears a device.

        Corresponds to viClear function of the VISA library.

        """
        flags = 0
        flags, lock_timeout = self._adapt_flags_and_lock_timeout(flags)

        error = self.interface.device_clear(
            self.link, flags, lock_timeout, self._io_timeout
        )

        return vxi11_error_to_visa(error)

    def read_stb(self) -> Tuple[int, StatusCode]:
        """Reads a status byte of the service request.

        Corresponds to viReadSTB function of the VISA library.

        Returns
        -------
        int
            Service request status byte
        StatusCode
            Return value of the library call.

        """
        flags = 0
        flags, lock_timeout = self._adapt_flags_and_lock_timeout(flags)

        error, stb = self.interface.device_read_stb(
            self.link, flags, lock_timeout, self._io_timeout
        )

        return stb, vxi11_error_to_visa(error)

    def lock(
        self,
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
        # TODO: shared lock is not implemented
        if lock_type == constants.Lock.shared:
            return "", StatusCode.error_nonsupported_operation

        # The only remaining lock type is exclusive lock

        # RULE B.6.74:
        # If some other link has the lock, device_lock SHALL examine the waitlock
        # flag in flags. If the flag is set, device_lock SHALL block until the
        # lock is free. If the flag is not set, device_lock SHALL terminate with
        # error set to 11, device locked by another link.

        flags = 0
        # The waitlock flag is the only flag used here
        if timeout != constants.VI_TMO_IMMEDIATE:
            flags = vxi11.OP_FLAG_WAIT_BLOCK

        error = self.interface.device_lock(self.link, flags, timeout)
        error = vxi11_error_to_visa(error)
        if error == StatusCode.success:
            self.attrs[ResourceAttribute.resource_lock_state] = (
                constants.VI_EXCLUSIVE_LOCK
            )

        return "", error

    def unlock(self) -> constants.StatusCode:
        """Relinquish a lock for the specified resource.

        Corresponds to viUnlock function of the VISA library.

        Returns
        -------
        StatusCode
            Return value of the library call.

        """
        error = self.interface.device_unlock(self.link)
        error = vxi11_error_to_visa(error)
        if error == StatusCode.success:
            self.attrs[ResourceAttribute.resource_lock_state] = constants.VI_NO_LOCK

        return error

    def _set_timeout(self, attribute: ResourceAttribute, value: int) -> StatusCode:
        """Sets timeout calculated value from python way to VI_ way"""
        # value is in milliseconds,
        # and can be VI_TMO_INFINITE (2**32 - 1) or VI_TMO_IMMEDIATE (0)
        self._io_timeout = value
        # self.timeout comes from the superclass, is in seconds, and can be None (infinite) or 0 (immediate)
        if value == constants.VI_TMO_INFINITE:
            self.timeout = None
        elif value == constants.VI_TMO_IMMEDIATE:
            self.timeout = 0
        else:
            self.timeout = value / 1000.0
        return StatusCode.success


class TCPIPInstrVicp(Session):
    """VICP Session that uses pyvicp to do the low level communication."""

    # Override parsed to take into account the fact that this class is only used
    # for a specific kind of resource
    parsed: rname.VICPInstr

    @staticmethod
    def list_resources(wait_time=1.0) -> List[str]:
        resources = []
        try:
            services = get_services("_lxi._tcp.local.", wait_time=wait_time)
        except NotImplementedError:
            warnings.warn(
                "VICP resources discovery requires the zeroconf package to be "
                "installed... try 'pip install zeroconf'",
                UserWarning,
            )
            return []

        for host, properties in services.items():
            if properties["Manufacturer"].lower().startswith("lecroy"):
                resources.append(f"VICP::{host}::INSTR")

        return sorted(resources)

    def after_parsing(self) -> None:
        # TODO: board_number not handled
        if pyvicp is None:
            raise NotImplementedError(
                "VICP requires the pyvicp package to be installed... "
                "try 'pip install pyvicp'"
            )

        host_address = self.parsed.host_address
        if "," in host_address:
            host_address, port_str = host_address.split(",")
            port = int(port_str)
        else:
            port = 1861

        try:
            self.interface = pyvicp.Client(
                self.parsed.host_address, port, timeout=self.timeout
            )
        except OSError as e:
            LOGGER.exception(
                f"Failed to open VICP connection to {self.parsed.host_address} "
                f"on port {port}"
            )
            raise OpenError() from e

        # initialize the constant attributes
        for name in ("SEND_END_EN", "TERMCHAR", "TERMCHAR_EN"):
            attribute = getattr(constants, "VI_ATTR_" + name)
            self.attrs[attribute] = attributes.AttributesByID[attribute].default

        self.attrs[ResourceAttribute.dma_allow_enabled] = constants.VI_FALSE
        self.attrs[ResourceAttribute.file_append_enabled] = constants.VI_FALSE
        self.attrs[ResourceAttribute.interface_instrument_name] = "TCPIP0 (VICP)"
        self.attrs[ResourceAttribute.interface_number] = 0
        self.attrs[ResourceAttribute.io_prot] = constants.VI_PROT_NORMAL
        self.attrs[ResourceAttribute.read_buffer_operation_mode] = (
            constants.VI_FLUSH_DISABLE
        )
        self.attrs[ResourceAttribute.resource_lock_state] = constants.VI_NO_LOCK
        self.attrs[ResourceAttribute.suppress_end_enabled] = constants.VI_FALSE
        self.attrs[ResourceAttribute.tcpip_address] = self.parsed.host_address
        self.attrs[ResourceAttribute.tcpip_hostname] = self.parsed.host_address
        self.attrs[ResourceAttribute.tcpip_is_hislip] = constants.VI_FALSE
        self.attrs[ResourceAttribute.tcpip_nodelay] = constants.VI_TRUE
        self.attrs[ResourceAttribute.tcpip_port] = port
        self.attrs[ResourceAttribute.write_buffer_operation_mode] = (
            constants.VI_FLUSH_WHEN_FULL
        )

        # configure the variable attributes
        self.attrs[ResourceAttribute.tcpip_keepalive] = (
            self.get_keepalive,
            self.set_keepalive,
        )

        # TODO: additional attributes (someday)
        # self.attrs[ResourceAttribute.manufacturer_id] = 16711
        # self.attrs[ResourceAttribute.max_queue_length] = 50
        # self.attrs[ResourceAttribute.read_buffer_size] = 4096
        # self.attrs[ResourceAttribute.resource_impl_version] = 0x0050_0c01
        # self.attrs[ResourceAttribute.resource_manufacturer_id] = 4015
        # self.attrs[ResourceAttribute.resource_manufacturer_name] = 'Rohde & Schwarz GmbH'
        # self.attrs[ResourceAttribute.resource_spec_version] = 0x0050_0800
        # self.attrs[ResourceAttribute.user_data] = 0
        # self.attrs[ResourceAttribute.write_buffer_size] = 4096

    def get_keepalive(self, attribute: ResourceAttribute) -> Tuple[bool, StatusCode]:
        """Is TCP keepalive enabled for the resource."""
        return self.interface.keepalive, StatusCode.success

    def set_keepalive(
        self, attribute: ResourceAttribute, keepalive: bool
    ) -> StatusCode:
        """Turns TCP keepalive on/off for this connection."""
        self.interface.keepalive = keepalive
        return StatusCode.success

    def close(self) -> StatusCode:
        self.interface.close()
        self.interface = None
        return StatusCode.success

    def read(self, count: int) -> Tuple[bytes, StatusCode]:
        """Reads data from device or interface synchronously.

        Corresponds to viRead function of the VISA library.

        Parameters
        -----------
        count : int
            Number of bytes to be read.

        Returns
        -------
        bytes
            Data read from the device
        StatusCode
            Return value of the library call.

        """
        try:
            data = self.interface.receive(count)
        except socket.timeout:
            return b"", StatusCode.error_timeout

        if len(data) >= count:
            return data, StatusCode.success_max_count_read
        else:
            return data, StatusCode.success_termination_character_read

    def write(self, data: bytes) -> Tuple[int, StatusCode]:
        """Writes data to device or interface synchronously.

        Corresponds to viWrite function of the VISA library.

        Parameters
        ----------
        data : bytes
            Data to be written.

        Returns
        -------
        int
            Number of bytes actually transferred
        StatusCode
            Return value of the library call.

        """
        self.interface.send(data)

        return len(data), StatusCode.success

    def clear(self) -> StatusCode:
        """Clears a device.

        Corresponds to viClear function of the VISA library.

        """
        self.interface.device_clear()

        return StatusCode.success

    def _set_timeout(self, attribute: ResourceAttribute, value: int) -> StatusCode:
        status = super()._set_timeout(attribute, value)
        if hasattr(self.interface, "timeout"):
            self.interface.timeout = 1e-3 * value

        return status

    def _get_attribute(self, attribute: ResourceAttribute) -> Tuple[Any, StatusCode]:
        """Get the value for a given VISA attribute for this session.

        Use to implement custom logic for attributes.

        Parameters
        ----------
        attribute : ResourceAttribute
            Attribute for which the state query is made

        Returns
        -------
        Any
            State of the queried attribute for a specified resource
        StatusCode
            Return value of the library call.

        """
        raise UnknownAttribute(attribute)

    def _set_attribute(
        self, attribute: ResourceAttribute, attribute_state: Any
    ) -> StatusCode:
        """Sets the state of an attribute.

        Corresponds to viSetAttribute function of the VISA library.

        Parameters
        ----------
        attribute : constants.ResourceAttribute
            Attribute for which the state is to be modified. (Attributes.*)
        attribute_state : Any
            The state of the attribute to be set for the specified object.

        Returns
        -------
        StatusCode
            Return value of the library call.

        """
        raise UnknownAttribute(attribute)


if pyvicp is not None:
    Session.register(constants.InterfaceType.vicp, "INSTR")(TCPIPInstrVicp)
else:
    Session.register_unavailable(
        constants.InterfaceType.vicp,
        "INSTR",
        "Please install PyVICP to use this resource type.",
    )


@Session.register(constants.InterfaceType.tcpip, "SOCKET")
class TCPIPSocketSession(Session):
    """A TCPIP Session that uses the network standard library to do the low
    level communication.

    """

    # Details about implementation:
    # On Windows, select is not interrupted by KeyboardInterrupt, to avoid
    # blocking for very long time, we use a decreasing timeout in select.
    # A minimum select timeout which prevents using too short select interval
    # is also calculated and select timeout is not lower that that minimum
    # timeout. The absolute minimum is 1 ms as a consequence.
    # This is valid for connect and read operations

    #: Maximum size of a chunk of data in bytes.
    max_recv_size: int

    # Override parsed to take into account the fact that this class is only used
    # for a specific kind of resource
    parsed: rname.TCPIPSocket

    default_tcpip_port = 5025

    @staticmethod
    def list_resources(wait_time=1.0) -> List[str]:
        resources = []

        try:
            for host, props in get_services(
                "_scpi-raw._tcp.local.", wait_time=wait_time
            ).items():
                port = TCPIPSocketSession.default_tcpip_port
                if "port" in props:
                    port = props["port"]
                resources.append(f"TCPIP::{host}::{port}::SOCKET")
        except NotImplementedError:
            warnings.warn(
                "TCPIP::SOCKET resource discovery requires the zeroconf package "
                "to be installed... try 'pip install zeroconf'",
                UserWarning,
            )
        return sorted(resources)

    def after_parsing(self) -> None:
        # TODO: board_number not handled

        ret_status = self._connect()
        if ret_status != StatusCode.success:
            self.close()
            raise Exception("could not connect: {0}".format(str(ret_status)))

        self.max_recv_size = 4096
        # This buffer is used to store the bytes that appeared after
        # termination char
        self._pending_buffer = bytearray()

        self.attrs[ResourceAttribute.tcpip_address] = self.parsed.host_address
        self.attrs[ResourceAttribute.tcpip_port] = self.parsed.port
        self.attrs[ResourceAttribute.interface_number] = self.parsed.board
        self.attrs[ResourceAttribute.tcpip_nodelay] = (
            self._get_tcpip_nodelay,
            self._set_attribute,
        )
        self.attrs[ResourceAttribute.tcpip_hostname] = ""
        self.attrs[ResourceAttribute.tcpip_keepalive] = (
            self._get_tcpip_keepalive,
            self._set_tcpip_keepalive,
        )
        # to use default as ni visa driver (NI-VISA 15.0)
        self.attrs[ResourceAttribute.suppress_end_enabled] = True
        self.attrs[ResourceAttribute.io_prot] = constants.VI_PROT_NORMAL

        for name in ("TERMCHAR", "TERMCHAR_EN"):
            attribute = getattr(constants, "VI_ATTR_" + name)
            self.attrs[attribute] = attributes.AttributesByID[attribute].default

    def _connect(self) -> StatusCode:
        timeout = connect_timeout(self.open_timeout)
        try:
            self.interface = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.interface.setblocking(False)
            self.interface.connect_ex((self.parsed.host_address, int(self.parsed.port)))
        except Exception as e:
            raise Exception("could not connect: {0}".format(str(e)))
        finally:
            self.interface.setblocking(True)

        # minimum is in interval 100 - 500ms based on timeout
        min_select_timeout = max(min(timeout / 10.0, 0.5), 0.1)
        # initial 'select_timout' is half of timeout or max 2 secs
        # (max blocking time). min is from 'min_select_timeout'
        select_timout = max(min(timeout / 2.0, 2.0), min_select_timeout)
        # time, when loop shall finish
        finish_time = time.time() + timeout
        while True:
            # use select to wait for socket ready, max `select_timout` seconds
            r, w, _x = select.select(
                [self.interface], [self.interface], [], select_timout
            )
            if self.interface in r or self.interface in w:
                return StatusCode.success

            if time.time() >= finish_time:
                # reached timeout
                return StatusCode.error_timeout

            # `select_timout` decreased to 50% of previous or
            # min_select_timeout
            select_timout = max(select_timout / 2.0, min_select_timeout)

    def close(self) -> StatusCode:
        self.interface.close()
        self.interface = None
        return StatusCode.success

    def read(self, count: int) -> Tuple[bytes, StatusCode]:
        """Reads data from device or interface synchronously.

        Corresponds to viRead function of the VISA library.

         Parameters
        -----------
        count : int
            Number of bytes to be read.

        Returns
        -------
        bytes
            Data read from the device
        StatusCode
            Return value of the library call.

        """
        if count < self.max_recv_size:
            chunk_length = count
        else:
            chunk_length = self.max_recv_size

        term_char, _ = self.get_attribute(ResourceAttribute.termchar)
        term_byte = int_to_byte(term_char) if term_char is not None else b""
        term_char_en, _ = self.get_attribute(ResourceAttribute.termchar_enabled)
        suppress_end_en, _ = self.get_attribute(ResourceAttribute.suppress_end_enabled)

        read_fun = self.interface.recv

        # minimum is in interval 1 - 100ms based on timeout, 1sec if no timeout
        # defined
        min_select_timeout = (
            1 if self.timeout is None else max(min(self.timeout / 100.0, 0.1), 0.001)
        )
        # initial 'select_timout' is half of timeout or max 2 secs
        # (max blocking time). min is from 'min_select_timeout'
        select_timout = (
            2.0
            if self.timeout is None
            else max(min(self.timeout / 2.0, 2.0), min_select_timeout)
        )
        # time, when loop shall finish, None means never ending story if no
        # data arrives
        finish_time = None if self.timeout is None else (time.time() + self.timeout)
        while True:
            # check, if we have any data received (from pending buffer or
            # further reading)
            if term_char_en and term_byte in self._pending_buffer:
                term_byte_index = self._pending_buffer.index(term_byte) + 1
                if term_byte_index > count:
                    term_byte_index = count
                    status = StatusCode.success_max_count_read
                else:
                    status = StatusCode.success_termination_character_read
                out = bytes(self._pending_buffer[:term_byte_index])
                self._pending_buffer = self._pending_buffer[term_byte_index:]
                return out, status

            if len(self._pending_buffer) >= count:
                out = bytes(self._pending_buffer[:count])
                self._pending_buffer = self._pending_buffer[count:]
                return out, StatusCode.success_max_count_read

            # use select to wait for read ready, max `select_timout` seconds
            r, _w, _x = select.select([self.interface], [], [], select_timout)

            read_data = b""
            if self.interface in r:
                read_data = read_fun(chunk_length)
                self._pending_buffer.extend(read_data)

            if not read_data:
                # can't read chunk or timeout
                if self._pending_buffer and not suppress_end_en:
                    # we have some data without termchar but no further data
                    # expected
                    out = bytes(self._pending_buffer[:count])
                    self._pending_buffer = self._pending_buffer[count:]
                    return out, StatusCode.success

                if finish_time and time.time() >= finish_time:
                    # reached timeout
                    out = bytes(self._pending_buffer[:count])
                    self._pending_buffer = self._pending_buffer[count:]
                    return out, StatusCode.error_timeout

                # `select_timout` decreased to 50% of previous or
                # min_select_timeout
                select_timout = max(select_timout / 2.0, min_select_timeout)

    def write(self, data: bytes) -> Tuple[int, StatusCode]:
        """Writes data to device or interface synchronously.

        Corresponds to viWrite function of the VISA library.

        Parameters
        ----------
        data : bytes
            Data to be written.

        Returns
        -------
        int
            Number of bytes actually transferred
        StatusCode
            Return value of the library call.

        """
        chunk_size = 4096

        num = sz = len(data)

        offset = 0

        while num > 0:
            block = data[offset : min(offset + chunk_size, sz)]

            try:
                # use select to wait for write ready
                select.select([], [self.interface], [])
                size = self.interface.send(block)
            except socket.timeout:
                return offset, StatusCode.error_io

            if size < len(block):
                return offset, StatusCode.error_io

            offset += size
            num -= size

        return offset, StatusCode.success

    def _clear_buff(self) -> None:
        # used by flush and clear operations
        self._pending_buffer.clear()
        while True:
            r, _w, _x = select.select([self.interface], [], [], 0.1)
            if not r:
                break
            r[0].recv(4096)

    def clear(self) -> StatusCode:
        """Clears a device.

        Corresponds to viClear function of the VISA library.

        """
        self._clear_buff()

        # Send the *CLS command to clear the device if using the 488.2 STRS protocol
        # VPP-4.3 Permission 6.14
        if self.attrs[ResourceAttribute.io_prot] == constants.VI_PROT_4882_STRS:
            self.write(b"*CLS\n")

        return StatusCode.success

    def flush(self, mask: BufferOperation) -> StatusCode:
        """Flush the specified buffers.
        Corresponds to viFlush function of the VISA library.
        Parameters
        ----------
        mask : constants.BufferOperation
            Specifies the action to be taken with flushing the buffer.
            The values can be combined using the | operator. However multiple
            operations on a single buffer cannot be combined.
        Returns
        -------
        constants.StatusCode
            Return value of the library call.
        """
        if mask & BufferOperation.discard_read_buffer:
            self._clear_buff()
        if (
            mask & BufferOperation.discard_read_buffer_no_io
            or mask & BufferOperation.discard_receive_buffer
            or mask & BufferOperation.discard_receive_buffer2
        ):
            self._pending_buffer.clear()
        if (
            mask & BufferOperation.flush_write_buffer
            or mask & BufferOperation.flush_transmit_buffer
            or mask & BufferOperation.discard_write_buffer
            or mask & BufferOperation.discard_transmit_buffer
        ):
            pass

        return StatusCode.success

    def read_stb(self) -> Tuple[int, StatusCode]:
        """Reads a status byte of the service request.

        Corresponds to viReadSTB function of the VISA library.

        Returns
        -------
        int
            Service request status byte
        StatusCode
            Return value of the library call.

        """
        if self.attrs[ResourceAttribute.io_prot] != constants.VI_PROT_4882_STRS:
            return 0, StatusCode.error_invalid_setup

        # Read the status byte from the device
        _n, status = self.write(b"*STB?\n")
        if status != StatusCode.success:
            return 0, status
        stbs, status = self.read(100)
        if status != StatusCode.success:
            return 0, status
        try:
            stb = int(stbs)
            return stb, StatusCode.success
        except ValueError:
            return 0, StatusCode.error_nonsupported_operation

    def assert_trigger(self, protocol: constants.TriggerProtocol) -> StatusCode:
        """Asserts hardware trigger.

        Parameters
        ----------
        protocol : constants.TriggerProtocol
            Triggering protocol to use.
            Only supports constants.TriggerProtocol.default

        Returns
        -------
        StatusCode
            Return value of the library call.

        """
        if protocol != constants.TriggerProtocol.default:
            return StatusCode.error_nonsupported_operation
        if self.attrs[ResourceAttribute.io_prot] != constants.VI_PROT_4882_STRS:
            return StatusCode.error_invalid_setup

        _n, status = self.write(b"*TRG\n")
        return status

    def _get_tcpip_nodelay(
        self, attribute: ResourceAttribute
    ) -> Tuple[constants.VisaBoolean, StatusCode]:
        if self.interface:
            value = self.interface.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
            return (
                constants.VisaBoolean.true
                if value == 1
                else constants.VisaBoolean.false,
                StatusCode.success,
            )
        return constants.VisaBoolean.false, StatusCode.error_nonsupported_attribute

    def _set_tcpip_nodelay(
        self, attribute: ResourceAttribute, attribute_state: bool
    ) -> StatusCode:
        if self.interface:
            self.interface.setsockopt(
                socket.IPPROTO_TCP, socket.TCP_NODELAY, 1 if attribute_state else 0
            )
            return StatusCode.success
        return StatusCode.error_nonsupported_attribute

    def _get_tcpip_keepalive(
        self, attribute: ResourceAttribute
    ) -> Tuple[constants.VisaBoolean, StatusCode]:
        if self.interface:
            value = self.interface.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
            return (
                constants.VisaBoolean.true
                if value == 1
                else constants.VisaBoolean.false,
                StatusCode.success,
            )
        return constants.VisaBoolean.false, StatusCode.error_nonsupported_attribute

    def _set_tcpip_keepalive(
        self, attribute: ResourceAttribute, attribute_state: bool
    ) -> StatusCode:
        if self.interface:
            set_keepalive(self.interface, bool(attribute_state))
            return StatusCode.success
        return StatusCode.error_nonsupported_attribute

    def _get_attribute(self, attribute: ResourceAttribute) -> Tuple[Any, StatusCode]:
        """Get the value for a given VISA attribute for this session.

        Use to implement custom logic for attributes.

        Parameters
        ----------
        attribute : ResourceAttribute
            Attribute for which the state query is made

        Returns
        -------
        Any
            State of the queried attribute for a specified resource
        StatusCode
            Return value of the library call.

        """
        raise UnknownAttribute(attribute)

    def _set_attribute(
        self, attribute: ResourceAttribute, attribute_state: Any
    ) -> StatusCode:
        """Sets the state of an attribute.

        Corresponds to viSetAttribute function of the VISA library.

        Parameters
        ----------
        attribute : constants.ResourceAttribute
            Attribute for which the state is to be modified. (Attributes.*)
        attribute_state : Any
            The state of the attribute to be set for the specified object.

        Returns
        -------
        StatusCode
            Return value of the library call.

        """
        raise UnknownAttribute(attribute)


def get_services(service_type: str, wait_time: float = 0.1) -> Dict[str, dict]:
    if zeroconf is None:
        raise NotImplementedError(
            "Service discovery requires the zeroconf package to be installed... "
            "try 'pip install zeroconf'"
        )

    class MyListener(zeroconf.ServiceListener):
        def __init__(self, *args, **kwargs):
            self.services = {}
            super().__init__(*args, **kwargs)

        def remove_service(self, zc: zeroconf.Zeroconf, type_: str, name: str) -> None:
            del self.services[name]

        def add_service(self, zc: zeroconf.Zeroconf, type_: str, name: str) -> None:
            info = zc.get_service_info(type_, name)
            if info is None:
                return
            properties = {}
            properties["port"] = info.port
            for key, val in info.properties.items():
                if key == b"txtvers":
                    continue
                properties[key.decode()] = val.decode()
            ipaddr = ipaddress.ip_address(info.addresses[0])
            self.services[str(ipaddr)] = properties

        update_service = add_service

    zero_conf = zeroconf.Zeroconf()
    listener = MyListener()
    browser = zeroconf.ServiceBrowser(zero_conf, service_type, listener, delay=0)
    time.sleep(wait_time)
    browser.cancel()
    zero_conf.close()
    return listener.services
