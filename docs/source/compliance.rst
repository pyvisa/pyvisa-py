Supported VISA resources
========================

The following table shows the supported VISA resource classes and the grammar for the address string. 
Optional string segments are shown in square brackets ([ ]).

=================  ========================================================================================
Interface          Syntax
=================  ========================================================================================
ASRL INSTR         ASRL[board]::INSTR
-----------------  ----------------------------------------------------------------------------------------
GPIB INSTR         GPIB[board]::primary_address[::secondary_address][::INSTR]
GPIB INTFC         GPIB[board]::INTFC
-----------------  ----------------------------------------------------------------------------------------
PRLGX ASRL         PRLGX-ASRL[board]::serial device::INTFC
PRLGX TCPIP        PRLGX-TCPIP[board]::host address[::port]::INTFC
-----------------  ----------------------------------------------------------------------------------------
TCPIP INSTR        TCPIP[board]::host address[::LAN device name][::INSTR]
TCPIP SOCKET       TCPIP[board]::host address[::port]::SOCKET
-----------------  ----------------------------------------------------------------------------------------
VICP INSTR         VICP::host address[::INSTR]
-----------------  ----------------------------------------------------------------------------------------
USB INSTR          USB[board]::manufacturer ID::model code::serial number[::USB interface number][::INSTR]
USB RAW            USB[board]::manufacturer ID::model code::serial number[::USB interface number]::RAW
=================  ========================================================================================

Notes:

* TCPIP INSTR: 
    * Supports both VXI-11 and HiSLIP protocols, as determined by `LAN device name` (``inst...`` or ``hislip...``), VXI-11 by default.
    * For HiSLIP, the port number can be specified the standard way by appending it to the `LAN device name`, separated by a comma. Example: ``hislip0,4880``
    * For VXI-11, the port number is normally provided by a port mapper, but can also be provided explicitly by appending it to the `host address`, separated by a comma. Example: ``192.168.1.101,1024``. Not all VISA backends support this feature.
* PRLGX / Prologix:
    * This is PyVISA-Py specific. See the Prologix section for details.
* All TCP-IP based resources:
    * Board is ignored, and presumed to be 0 (the default network interface).


Prologix
========

Controlling GPIB resources via a prologix adapter is possible. There are 2 main methods:

* via a `ASRL INSTR` or `TCPIP SOCKET` resource. 
  Be sure to set read- and write termination, and to use the correct prologix  ``++`` commands mixed in with the SCPI commands.
* via the PyVISA-Py specific `PRLGX ASRL` or `PRLGX TCPIP` resources, and the `GPIB` resources. 
  This will allow you to treat the gateway and the connected instruments as separate resources, and makes it easier to interact with the instruments directly. 
  
  This goes in 2 steps:

  1. Open the gateway resource using the appropriate Prologix-specific resource string.
  2. Open the connected instrument as a GPIB resource.

    Example::

        import pyvisa

        # force the PyVISA-Py backend, in case you have installed others.
        rm = pyvisa.ResourceManager("@py")

        # Open the gateway.
        # It will then propose itself as a native GPIB interface.
        # You can't also have a GPIB card in your system.
        # Port 1234 is by default. You can also specify it the standard way.
        prlgx = rm.open_resource("PRLGX-TCPIP::192.168.1.110::INTFC")

        # Open the various connected instruments via the GPIB interface just created.
        inst1 = rm.open_resource("GPIB::1::INSTR")  # instrument at address 1
        inst2 = rm.open_resource("GPIB::2::INSTR")  # etc
        inst18 = rm.open_resource("GPIB::18::INSTR")

        # and now you can talk to the instruments (one at a time please):
        print(inst1.query("*IDN?"))
        print(inst2.query("*IDN?"))
        print(inst18.query("*ID?"))

    If you use this method, be aware that it tries to be intelligent, and emits the ``++read eoi`` command by itself 
    when you do a query. But you can no longer emit that yourself. That means that you can no longer call 
    ``read_raw`` or other standalone read methods. Only use ``query`` and ``write`` methods.

Functions: VPP-4.3 Compliance
=============================

The following features are not supported:

* Shared locks and nested locks are not supported.
* Asynchronous read/write operations are not supported.
* Termination is only supported for HiSLIP.

``VXI-11 device_docmd()``
-------------------------

If you have a VXI-11.2 (VXI-11 to GPIB) gateway, you may want to use the VXI-11 ``device_docmd()`` command. 
However, there is no official provision in the VISA standards to do that.

PyVISA-Py does provide a way to use this command via a lower end method::

    import pyvisa

    # VXI-11.2 Table B.1
    VXI11_DOCMD_SEND_COMMAND = 0x020000
    SEND_COMMAND_DATASIZE = 1  # Table B.1: Send Command's datasize is 1 (byte-granular)
    SEND_COMMAND_MAX_BYTES = 128  # Table B.1: data_in.data_in_len is 0-128 for Send Command

    def vxi11_send_command(inst, command_bytes, io_timeout_ms=5000):
        """Send raw GPIB command bytes via the VXI-11.2 B.5.1 "Send Command" doCmd.

        Parameters
        ----------
        inst : pyvisa resource
            Must be opened against the *interface itself* (e.g. "TCPIP::<ip>::gpib0::INSTR"),
            not a specific device link - see RULE B.5.2 above.
        command_bytes : bytes | bytearray | list[int] | tuple[int, ...]
            0-128 raw GPIB command bytes to put on the bus with ATN asserted (IEEE 488.2, 16.2.1).
            This is where you'd put addressing/handshake bytes (UNL, UNT, MLA, MTA, secondary
            addresses, ...) if you're doing your own bus addressing by hand.
        io_timeout_ms : int
            I/O timeout for this call, in milliseconds.

        Returns
        -------
        bytes
            data_out from the server. Per RULE B.5.5 this SHALL be an exact echo of the bytes sent.

        Raises
        ------
        ValueError
            If command_bytes is empty of the wrong type or longer than 128 bytes.
        Vxi11DocmdError
            If the server returns a nonzero VXI-11 error code.
        """
        data_in = bytes(command_bytes)
        if len(data_in) > SEND_COMMAND_MAX_BYTES:
            raise ValueError(
                f"Send Command accepts at most {SEND_COMMAND_MAX_BYTES} bytes "
                f"(Table B.1), got {len(data_in)}"
            )

        session = inst.visalib.sessions[inst.session]

        error, data_out = session.interface.device_docmd(
            session.link,
            0,  # flags
            io_timeout_ms,
            1000,  # lock timeout
            VXI11_DOCMD_SEND_COMMAND,
            False,  # network_order - irrelevant here, data_in/data_out are raw byte arrays already
            SEND_COMMAND_DATASIZE,
            data_in,
        )

        if error:
            raise Exception(f"VXI-11 Send Command error: {error}")

        return data_out


    rm = pyvisa.ResourceManager("@py")
    inst = rm.open_resource("TCPIP::192.168.3.2::gpib0::INSTR")
    echoed = vxi11_send_command(inst, [0x3F, 0x5F])  # UNL, UNT
    inst.close()


Note that this is PyVISA-Py specific, and not all gateways support this command (although they should).

As mentioned in the code, only use this only on VXI-11, on the SICL address (typically ``gpib0``).

Attributes: VPP-4.3 Compliance
==============================

This document assesses the VPP-4.3 attributes applicable to PyVISA-Py's
implemented resource types.
Resource classes not listed in a section cannot use that attribute under VPP-4.3.

# TODO: add prologix to the supported types, and check prologix specific attributes.

``VI_ATTR_4882_COMPLIANT``
--------------------------
Usable by USB INSTR.

Coverage: Missing.

Proposition: later


``VI_ATTR_ASRL_AVAIL_NUM``
--------------------------
Usable by ASRL INSTR.

Coverage: Full.


``VI_ATTR_ASRL_BAUD``
---------------------
Usable by ASRL INSTR.

Coverage: Full.


``VI_ATTR_ASRL_CTS_STATE``
--------------------------
Usable by ASRL INSTR.

Coverage: Full.


``VI_ATTR_ASRL_DATA_BITS``
--------------------------
Usable by ASRL INSTR.

Coverage: Full.


``VI_ATTR_ASRL_DCD_STATE``
--------------------------
Usable by ASRL INSTR.

Coverage: Missing.

Proposition: later


``VI_ATTR_ASRL_DSR_STATE``
--------------------------
Usable by ASRL INSTR.

Coverage: Full.


``VI_ATTR_ASRL_DTR_STATE``
--------------------------
Usable by ASRL INSTR.

Coverage: Missing.

Proposition: later

``VI_ATTR_ASRL_END_IN``
-----------------------
Usable by ASRL INSTR.

Coverage: Full.


``VI_ATTR_ASRL_END_OUT``
------------------------
Usable by ASRL INSTR.

Coverage: Full.


``VI_ATTR_ASRL_FLOW_CNTRL``
---------------------------
Usable by ASRL INSTR.

Coverage: Full.


``VI_ATTR_ASRL_PARITY``
-----------------------
Usable by ASRL INSTR.

Coverage: Full.


``VI_ATTR_ASRL_REPLACE_CHAR``
-----------------------------
Usable by ASRL INSTR.

Coverage: Missing.

Proposition: later


``VI_ATTR_ASRL_RI_STATE``
-------------------------
Usable by ASRL INSTR.

Coverage: Missing.

Proposition: later


``VI_ATTR_ASRL_RTS_STATE``
--------------------------
Usable by ASRL INSTR.

Coverage: Missing.

Proposition: later


``VI_ATTR_ASRL_STOP_BITS``
--------------------------
Usable by ASRL INSTR.

Coverage: Full.


``VI_ATTR_ASRL_XOFF_CHAR``
--------------------------
Usable by ASRL INSTR.

Coverage: Missing.

Proposition: later


``VI_ATTR_ASRL_XON_CHAR``
-------------------------
Usable by ASRL INSTR.

Coverage: Missing.

Proposition: later


``VI_ATTR_DEV_STATUS_BYTE``
---------------------------
Usable by GPIB INTFC.

Coverage: Missing.

Proposition: later


``VI_ATTR_DMA_ALLOW_EN``
------------------------
Usable by all resources.

Coverage: HiSLIP Partial; all others Missing. 

HiSLIP exposes a constant disabled value instead of providing
the required writable attribute and unsupported-state response.

Proposition: add to all (`dma_allow_enabled`), in faked RW (force to False, unsupported-state otherwise).


``VI_ATTR_FILE_APPEND_EN``
--------------------------
Usable by all resources.

Coverage: HiSLIP Partial; all others Missing. 

HiSLIP exposes a constant false value, but it is not writable
and is not used by file transfer operations.

Proposition: add to all (`file_append_enabled`), in faked RW (force to False, unsupported-state otherwise).


``VI_ATTR_GPIB_ADDR_STATE``
---------------------------
Usable by GPIB INTFC.

Coverage: Missing.

Proposition: later


``VI_ATTR_GPIB_ATN_STATE``
--------------------------
Usable by GPIB INTFC.

Coverage: Full.


``VI_ATTR_GPIB_CIC_STATE``
--------------------------
Usable by GPIB INTFC.

Coverage: Full.


``VI_ATTR_GPIB_HS488_CBL_LEN``
-------------------------------
Usable by GPIB INTFC.

Coverage: Missing.

Proposition: later


``VI_ATTR_GPIB_NDAC_STATE``
---------------------------
Usable by GPIB INTFC.

Coverage: Full.


``VI_ATTR_GPIB_PRIMARY_ADDR``
-----------------------------
Usable by GPIB INSTR and GPIB INTFC.

Coverage: Full.


``VI_ATTR_GPIB_READDR_EN``
--------------------------
Usable by GPIB INSTR.

Coverage: Full.


``VI_ATTR_GPIB_REN_STATE``
--------------------------
Usable by GPIB INSTR and GPIB INTFC.

Coverage: Full.


``VI_ATTR_GPIB_SECONDARY_ADDR``
-------------------------------
Usable by GPIB INSTR and GPIB INTFC.

Coverage: Full.


``VI_ATTR_GPIB_SRQ_STATE``
--------------------------
Usable by GPIB INTFC.

Coverage: Full.


``VI_ATTR_GPIB_SYS_CNTRL_STATE``
--------------------------------
Usable by GPIB INTFC.

Coverage: Missing.

Proposition: later


``VI_ATTR_GPIB_UNADDR_EN``
--------------------------
Usable by GPIB INSTR.

Coverage: Full.


``VI_ATTR_INTF_INST_NAME``
--------------------------
Usable by all resources.

Coverage: HiSLIP Full; all others Missing.

Proposition: add to all (`interface_instrument_name`).


``VI_ATTR_INTF_NUM``
--------------------
Usable by all resources.

Coverage: GPIB INSTR, GPIB INTFC, HiSLIP, and TCPIP SOCKET Full; 
ASRL, VXI-11, and USB Missing.

Proposition: distribute everywhere except GPIB: `interface_number = self.parsed.board`.
While we're there, correct `VI_ATTR_INTF_INST_NAME` (`interface_instrument_name`): include board in it, for VXI-11 and HiSLIP.
For VXI-11 and HiSLIP, interface_number corresponds to the network interface of the client PC.
Add tests about this.


``VI_ATTR_INTF_TYPE``
---------------------
Usable by all resources.

Coverage: Full.


``VI_ATTR_IO_PROT``
-------------------
Usable by GPIB INSTR, ASRL INSTR, TCPIP SOCKET, and USB INSTR.

Coverage: 
GPIB partial: only normal protocol is supported;
ASRL and SOCKET full: VI_PROT_4882_STRS is accepted; 
USB Missing. 

Proposition: fake RW on the missing/partial interfaces, and use unsupported-state on all where needed. 
Could probably easily be added for GPIB, mark that in the code.


``VI_ATTR_MANF_ID``
-------------------
Usable by USB INSTR.

Coverage: Full.


``VI_ATTR_MANF_NAME``
---------------------
Usable by USB INSTR.

Coverage: Missing.

Proposition: later


``VI_ATTR_MAX_QUEUE_LENGTH``
----------------------------
Usable by all resources.

Coverage: Missing.

Proposition: later. This means creating a queue with configurable size 
per each session, on all types of instruments. 
See VPP-4.3 Rules 3.2.5, 3.2.6, 3.7.3, 3.7.4, 3.7.5.
NI-VISA has 50 by default.

``VI_ATTR_MODEL_CODE``
----------------------
Usable by USB INSTR.

Coverage: Full.


``VI_ATTR_MODEL_NAME``
----------------------
Usable by USB INSTR.

Coverage: Missing.

Proposition: later


``VI_ATTR_RD_BUF_OPER_MODE``
----------------------------
Usable by all resources.

Coverage: HiSLIP Partial; all others Missing. 

HiSLIP stores the default but does not provide formatted read
buffer behavior.

Proposition: add to all (`read_buffer_operation_mode`), in faked RW (force to VI_FLUSH_DISABLE, unsupported-state otherwise).


``VI_ATTR_RD_BUF_SIZE``
-----------------------
Usable by all resources.

Coverage: Missing.

Proposition: together with ``VI_ATTR_WR_BUF_SIZE``. Not sure how to do that, as we don't have ``viSetBuf()``


``VI_ATTR_RM_SESSION``
----------------------
Usable by all resources.

Coverage: Full.


``VI_ATTR_RSRC_CLASS``
----------------------
Usable by all resources.

Coverage: Full.


``VI_ATTR_RSRC_IMPL_VERSION``
-----------------------------
Usable by all resources.

Coverage: Missing.

Proposition: later


``VI_ATTR_RSRC_LOCK_STATE``
---------------------------
Usable by all resources.

Coverage: HiSLIP and VXI-11 Partial; Does not implement VISA lock sharing nor nesting.
All others Missing. 

Proposition: add to all others (`resource_lock_state`), in faked RW (force to VI_NO_LOCK, unsupported-state otherwise).

``VI_ATTR_RSRC_MANF_ID``
------------------------
Usable by all resources.

Coverage: Missing.

Proposition: PyVISA-Py does not have a VXI manufacturer ID (although it might be possible to get one). We fake IVI member ID `xx` on HiSLIP. Maybe that?
NI-Visa = 0x0FF6

``VI_ATTR_RSRC_MANF_NAME``
--------------------------
Usable by all resources.

Coverage: full.


``VI_ATTR_RSRC_NAME``
---------------------
Usable by all resources. 

Coverage: Full.


``VI_ATTR_RSRC_SPEC_VERSION``
-----------------------------
Usable by all resources. 

Coverage: Missing.

Proposition: since we're not fully compliant, this is stretching it. 
NI-VISA MacOS uses 0x0070 0000, NI-VISA's doc says it is 0x0030 0000
VPP-4.3 says it SHALL be 0x0070 0200
Maybe set to 0x0030 0000 ?

Covered by berg's test.


``VI_ATTR_SEND_END_EN``
-----------------------
Usable by all resources.

Coverage: GPIB INSTR and GPIB INTFC Full; 
ASRL, VXI-11, HiSLIP, and USB Partial; 
SOCKET Missing. 

The partial implementations expose or read the setting but do not consistently apply it to
the underlying transport's end-of-message behavior.

Proposition: to be tackled together with ``VI_ATTR_SUPPRESS_END_EN``.


``VI_ATTR_SUPPRESS_END_EN``
---------------------------
Usable by GPIB INSTR, ASRL INSTR, TCPIP INSTR (VXI-11 and HiSLIP), TCPIP
SOCKET, and USB INSTR. 

Coverage: ASRL, VXI-11, SOCKET, and USB Full; 
HiSLIP Partial; 
GPIB Missing. 

HiSLIP stores the value but its receive implementation does not honor it.

Proposition: to be tackled together with ``VI_ATTR_SEND_END_EN``.


``VI_ATTR_TCPIP_ADDR``
----------------------
Usable by TCPIP INSTR (VXI-11 and HiSLIP) and TCPIP SOCKET. 

Coverage: Full.


``VI_ATTR_TCPIP_DEVICE_NAME``
-----------------------------
Usable by TCPIP INSTR (VXI-11 and HiSLIP).

Coverage: Full.


``VI_ATTR_TCPIP_HISLIP_MAX_MESSAGE_KB``
----------------------------------------
Usable by TCPIP INSTR (HiSLIP).

Coverage: Full.


``VI_ATTR_TCPIP_HISLIP_OVERLAP_EN``
------------------------------------
Usable by TCPIP INSTR (HiSLIP).

Coverage: Partial. 

The value is stored but changes do not issue the required HiSLIP device clear or switch the
protocol's overlap mode.

Proposition: not easy to do. Default value is supposed to be "Preference returned by device." 
For now: Fake RW (force to VI_FALSE, unsupported-state otherwise).


``VI_ATTR_TCPIP_HISLIP_VERSION``
---------------------------------
Usable by TCPIP INSTR (HiSLIP).

Coverage: Full.


``VI_ATTR_TCPIP_HOSTNAME``
--------------------------
Usable by TCPIP INSTR (VXI-11 and HiSLIP) and TCPIP SOCKET. 

Coverage: Full.


``VI_ATTR_TCPIP_IS_HISLIP``
---------------------------
Usable by TCPIP INSTR (VXI-11 and HiSLIP).

Coverage: Full.


``VI_ATTR_TCPIP_KEEPALIVE``
---------------------------
Usable by TCPIP INSTR (HiSLIP) and TCPIP SOCKET.

Coverage: Full.


``VI_ATTR_TCPIP_NODELAY``
-------------------------
Usable by TCPIP INSTR (HiSLIP) and TCPIP SOCKET.

Coverage: SOCKET Full; 
HiSLIP Partial. 

HiSLIP reports a stored value but does not set the TCP socket's ``TCP_NODELAY`` option.

Proposition: do like SOCKET


``VI_ATTR_TCPIP_PORT``
----------------------
Usable by TCPIP INSTR (HiSLIP) and TCPIP SOCKET.

Coverage: Full.


``VI_ATTR_TERMCHAR``
--------------------
Usable by all resources.

Coverage: all Full except HiSLIP: Partial. 

HiSLIP stores the value but does not use it to terminate reads.

Proposition: to be tackled together with ``VI_ATTR_TERMCHAR_EN``.


``VI_ATTR_TERMCHAR_EN``
-----------------------
Usable by all resources.

Coverage: all Full except HiSLIP: Partial. 

HiSLIP stores the value but does not use it to terminate reads.

Proposition: to be tackled together with ``VI_ATTR_TERMCHAR``.


``VI_ATTR_TMO_VALUE``
---------------------
Usable by all resources.

Coverage: Full.


``VI_ATTR_TRIG_ID``
-------------------
Usable by GPIB INSTR, ASRL INSTR, TCPIP INSTR (VXI-11 and HiSLIP), and USB
INSTR.

Coverage: Missing.

Proposition: leave as is.
Although mandatory everywhere, it only seems to be relevant for VXI, 
which is not supported by PyVISA-Py.
NI-VISA and R&S VISA do not support it in VXI-11 nor HiSLIP.
NI-VISA claims it is fixed to VI_TRIG_SW for GPIB, Serial, TCPIP.

``VI_ATTR_USB_INTFC_NUM``
-------------------------
Usable by USB INSTR.

Coverage: Full.


``VI_ATTR_USB_MAX_INTR_SIZE``
------------------------------
Usable by USB INSTR.

Coverage: Missing.

Proposition: later


``VI_ATTR_USB_PROTOCOL``
------------------------
Usable by USB INSTR.

Coverage: Missing.

Proposition: later


``VI_ATTR_USB_SERIAL_NUM``
--------------------------
Usable by USB INSTR.

Coverage: Full.


``VI_ATTR_USER_DATA`` / ``VI_ATTR_USER_DATA_32`` / ``VI_ATTR_USER_DATA_64``
---------------------------------------------------------------------------
Usable by all resources.

Coverage: Missing.

Proposition: Can be implemented as a simple session-local storage attribute.


``VI_ATTR_WR_BUF_OPER_MODE``
----------------------------
Usable by all resources.

Coverage: HiSLIP Partial; 
All others Missing. HiSLIP stores the default but has no formatted write buffer.

Proposition: (write_buffer_operation_mode) fake RW (force to VI_FLUSH_WHEN_FULL, unsupported-state otherwise)
See ``VI_ATTR_RD_BUF_OPER_MODE`` for more details.


``VI_ATTR_WR_BUF_SIZE``
-----------------------
Usable by all resources.

Coverage: Missing.

Proposition: together with ``VI_ATTR_RD_BUF_SIZE``. Not sure how to do that, as we don't have ``viSetBuf()``


Attributes: PyVISA-Py Additions
===============================

``VI_KTATTR_LOCKWAIT``
----------------------
Usable by VXI-11 INSTR.

This is a PyVISA-Py and Keysight specific attribute.

Coverage: Full.
