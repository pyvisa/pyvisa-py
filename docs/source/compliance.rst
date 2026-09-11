Compatibility and specific features of PyVISA-Py
================================================

Supported VISA resources
------------------------

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
TCPIP INSTR        TCPIP[board]::host address[::LAN device name][::INSTR]
TCPIP SOCKET       TCPIP[board]::host address[::port]::SOCKET
-----------------  ----------------------------------------------------------------------------------------
VICP INSTR         VICP::host address[::INSTR]
-----------------  ----------------------------------------------------------------------------------------
USB INSTR          USB[board]::manufacturer ID::model code::serial number[::USB interface number][::INSTR]
USB RAW            USB[board]::manufacturer ID::model code::serial number[::USB interface number]::RAW
-----------------  ----------------------------------------------------------------------------------------
PRLGX ASRL         PRLGX-ASRL[board]::serial device::INTFC
PRLGX TCPIP        PRLGX-TCPIP[board]::host address[::port]::INTFC
=================  ========================================================================================

Notes:

* TCPIP INSTR: 
    * Supports both VXI-11 and HiSLIP protocols, as determined by `LAN device name` (``hislip...`` for HiSLIP, anything else goes to VXI-11), with VXI-11 as the default.
    * For HiSLIP, the port number can be specified the standard way by appending it to the `LAN device name`, separated by a comma. Example: ``hislip0,4880``
    * For VXI-11, the port number is normally provided by a port mapper, but can also be provided explicitly by appending it to the `host address`, separated by a comma. Example: ``192.168.1.101,1024``. Not all VISA backends support this feature.
* PRLGX / Prologix:
    * This is PyVISA-Py specific. See the Prologix section for details.
* All TCP-IP based resources (except PRLGX-TCPIP):
    * Board is ignored, and presumed to be 0 (the default network interface).


Prologix
--------

Controlling GPIB resources via a prologix adapter is possible. Historically, you can do this 
via a `ASRL INSTR` or `TCPIP SOCKET` resource. In that case, be sure to set read- and write termination, 
and to use the correct prologix  ``++`` commands mixed in with the SCPI commands.

This is still the most versatile and portable method. It is however complicated to set up, as it requires 
special care when working with multiple instruments, the code is not easily transposable to other setups.

There is an easier method available: using the PyVISA-Py specific `PRLGX ASRL` or `PRLGX TCPIP` resource, and the `GPIB` resources.

This will allow you to treat the gateway and the connected instruments as separate resources, and makes it easier to interact with the instruments directly. 

How to use
^^^^^^^^^^

This goes in 2 steps:

1. Open the gateway resource using the appropriate Prologix-specific resource string.
2. Open the connected instrument as a GPIB resource.

Example::

    import pyvisa

    # force the PyVISA-Py backend, in case you have installed others.
    rm = pyvisa.ResourceManager("@py")

    # Open the gateway.
    # It will then propose itself as a native GPIB interface.
    # On TCP-IP, port 1234 is the default. This example makes it explicit.
    # You can also use serial, like so: "PRLGX-ASRL::/dev/cu.usbserialXXX::INTFC"
    prlgx = rm.open_resource("PRLGX-TCPIP::192.168.1.110::1234::INTFC")
    
    # Open the various connected instruments via the GPIB interface just created.
    inst1 = rm.open_resource("GPIB::1::INSTR")  # instrument at address 1
    inst2 = rm.open_resource("GPIB::2::INSTR")  # etc
    inst18 = rm.open_resource("GPIB::18::INSTR")

    # and now you can talk to the instruments (one at a time please, there is no locking mechanism):
    print(inst1.query("*IDN?"))
    print(inst2.query("*IDN?"))
    print(inst18.query("*ID?"))

Apart from the standard read/write functions, the ``read_stb``, 
``assert_trigger``, and ``clear`` methods are also supported on the instruments.

When using attributes with prologix: few attributes are supported, and they must 
go through the INTFC resource. 
The device-specific attributes are generally ignored or not accessible.

GPIB secondary addresses are supported.

Attention with read operations
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

If you use this method, be aware that it tries to be intelligent, and emits the ``++read eoi`` by itself
when you do a ``read`` after a ``write`` command. 
It will also emit that sequence between the write and read of a ``query``.
You can however no longer emit ``++`` commands yourself.
That means that you may be limited if you want to do ``read_raw`` or other standalone read methods. 
Best is to only use ``query`` and ``write`` methods.

Cohabitation with other adapters or GPIB interfaces
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Multiple GPIB interfaces and Prologix interfaces can coexist, 
just make sure that you specify a board on the prologix interface, 
and specify the same board for the connected instruments.
    
Functions
---------

Most functions are supported. See the FAQ page and the pyvisa documentation for more details.

Not or partially supported features
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The following features are not or not fully supported:

* Shared locks and nested locks are not supported.
* Asynchronous read/write operations are not supported.
* Secured/Encrypted (SSL/TLS) and Authenticated connections are not supported.
* Asynchronous termination (``terminate()``) is only supported for HiSLIP.
* LXI service discovery is not supported.

``VXI-11 device_docmd()``
^^^^^^^^^^^^^^^^^^^^^^^^^

If you have a VXI-11.2 (VXI-11 to GPIB) gateway, you may want to use the VXI-11 ``device_docmd()`` 
command to deal with legacy devices or to perform low-level GPIB operations manually. 

Unfortunately, there is no official provision in the VISA standards to do that.

PyVISA-Py does provide a way to execute this command via a low level method::

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
            If command_bytes is empty, of the wrong type, or longer than 128 bytes.
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


Note that this is PyVISA-Py specific, and that some gateways may not support this feature.

You can also use the same mechanism to execute other commands via `interface.device_docmd()`. 
See the documentation of VXI-11 and potentially the documentation of your gateway for details. 
Some gateways even support parallel polling via this.

As mentioned in the code, only use this on VXI-11, on the SICL address (typically ``gpib0``).

Attributes
----------

This document assesses the VPP-4.3 attributes applicable to PyVISA-Py's implemented resource types.
Resource classes not listed in a section cannot use that attribute under VPP-4.3.

This chapter does not cover attributes for the PRLGX interface and device resources, as they are very limited.

``VI_ATTR_4882_COMPLIANT``
^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** USB INSTR
| **Access:** RO
| **Coverage:** Missing

``VI_ATTR_ASRL_AVAIL_NUM``
^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** ASRL INSTR
| **Access:** RO
| **Coverage:** Full

``VI_ATTR_ASRL_BAUD``
^^^^^^^^^^^^^^^^^^^^^
| **Used by:** ASRL INSTR
| **Access:** R/W
| **Coverage:** Full

``VI_ATTR_ASRL_CTS_STATE``
^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** ASRL INSTR
| **Access:** RO
| **Coverage:** Full

``VI_ATTR_ASRL_DATA_BITS``
^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** ASRL INSTR
| **Access:** R/W
| **Coverage:** Full

``VI_ATTR_ASRL_DCD_STATE``
^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** ASRL INSTR
| **Access:** RO
| **Coverage:** Missing

``VI_ATTR_ASRL_DSR_STATE``
^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** ASRL INSTR
| **Access:** RO
| **Coverage:** Full

``VI_ATTR_ASRL_DTR_STATE``
^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** ASRL INSTR
| **Access:** R/W
| **Coverage:** Missing

``VI_ATTR_ASRL_END_IN``
^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** ASRL INSTR
| **Access:** R/W
| **Coverage:** Full

``VI_ATTR_ASRL_END_OUT``
^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** ASRL INSTR
| **Access:** R/W
| **Coverage:** Full

``VI_ATTR_ASRL_FLOW_CNTRL``
^^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** ASRL INSTR
| **Access:** R/W
| **Coverage:** Full

``VI_ATTR_ASRL_PARITY``
^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** ASRL INSTR
| **Access:** R/W
| **Coverage:** Full

``VI_ATTR_ASRL_REPLACE_CHAR``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** ASRL INSTR
| **Access:** R/W
| **Coverage:** Missing

``VI_ATTR_ASRL_RI_STATE``
^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** ASRL INSTR
| **Access:** RO
| **Coverage:** Missing

``VI_ATTR_ASRL_RTS_STATE``
^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** ASRL INSTR
| **Access:** R/W
| **Coverage:** Missing

``VI_ATTR_ASRL_STOP_BITS``
^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** ASRL INSTR
| **Access:** R/W
| **Coverage:** Full

``VI_ATTR_ASRL_XOFF_CHAR``
^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** ASRL INSTR
| **Access:** R/W
| **Coverage:** Missing

``VI_ATTR_ASRL_XON_CHAR``
^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** ASRL INSTR
| **Access:** R/W
| **Coverage:** Missing

``VI_ATTR_DEV_STATUS_BYTE``
^^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** GPIB INTFC
| **Access:** R/W
| **Coverage:** Missing

``VI_ATTR_DMA_ALLOW_EN``
^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** all resources except USB RAW
| **Access:** R/W
| **Coverage:** Partial; fixed to False

``VI_ATTR_FILE_APPEND_EN``
^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** all resources except USB RAW
| **Access:** R/W
| **Coverage:** Partial; fixed to False

``VI_ATTR_GPIB_ADDR_STATE``
^^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** GPIB INTFC
| **Access:** RO
| **Coverage:** Missing

``VI_ATTR_GPIB_ATN_STATE``
^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** GPIB INTFC
| **Access:** RO
| **Coverage:** Full

``VI_ATTR_GPIB_CIC_STATE``
^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** GPIB INTFC
| **Access:** RO
| **Coverage:** Full

``VI_ATTR_GPIB_HS488_CBL_LEN``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** GPIB INTFC
| **Access:** R/W
| **Coverage:** Missing

``VI_ATTR_GPIB_NDAC_STATE``
^^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** GPIB INTFC
| **Access:** RO
| **Coverage:** Full

``VI_ATTR_GPIB_PRIMARY_ADDR``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** GPIB
| **Access:** RO for INSTR, R/W for INTFC
| **Coverage:** Full

``VI_ATTR_GPIB_READDR_EN``
^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** GPIB INSTR
| **Access:** R/W
| **Coverage:** Full

``VI_ATTR_GPIB_REN_STATE``
^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** GPIB
| **Access:** RO
| **Coverage:** Full

``VI_ATTR_GPIB_SECONDARY_ADDR``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** GPIB
| **Access:** RO for INSTR, R/W for INTFC
| **Coverage:** Full

``VI_ATTR_GPIB_SRQ_STATE``
^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** GPIB INTFC
| **Access:** RO
| **Coverage:** Full

``VI_ATTR_GPIB_SYS_CNTRL_STATE``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** GPIB INTFC
| **Access:** R/W
| **Coverage:** Missing

``VI_ATTR_GPIB_UNADDR_EN``
^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** GPIB INSTR
| **Access:** R/W
| **Coverage:** Full

``VI_ATTR_INTF_INST_NAME``
^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** all resources except USB RAW
| **Access:** RO
| **Coverage:** Full

``VI_ATTR_INTF_NUM``
^^^^^^^^^^^^^^^^^^^^
| **Used by:** all resources except USB RAW
| **Access:** RO
| **Coverage:** Full

For some resources, this relies on the board that can be defined in the connection string. 
Support for board selection for network connections is not implemented yet.

``VI_ATTR_INTF_TYPE``
^^^^^^^^^^^^^^^^^^^^^
| **Used by:** all resources except USB RAW
| **Access:** RO
| **Coverage:** Full

``VI_ATTR_IO_PROT``
^^^^^^^^^^^^^^^^^^^
| **Used by:** GPIB INSTR, ASRL INSTR, TCPIP SOCKET, USB INSTR
| **Access:** R/W
| **Coverage:**
|  - GPIB, USB: Partial; only supports the normal protocol.
|  - ASRL, SOCKET: Full

Proposition for future change: Could probably easily be added for GPIB.

``VI_ATTR_MANF_ID``
^^^^^^^^^^^^^^^^^^^
| **Used by:** USB INSTR
| **Access:** RO
| **Coverage:** Full

``VI_ATTR_MANF_NAME``
^^^^^^^^^^^^^^^^^^^^^
| **Used by:** USB INSTR
| **Access:** RO
| **Coverage:** Missing

``VI_ATTR_MAX_QUEUE_LENGTH``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** all resources
| **Access:** R/W (becomes RO after the first ``viEnableEvent()``)
| **Coverage:** Partial; fixed to a default value.

``VI_ATTR_MODEL_CODE``
^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** USB INSTR
| **Access:** RO
| **Coverage:** Full

``VI_ATTR_MODEL_NAME``
^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** USB INSTR
| **Access:** RO
| **Coverage:** Missing

``VI_ATTR_RD_BUF_OPER_MODE``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** all resources except USB RAW
| **Access:** R/W
| **Coverage:** Partial; fixed to VI_FLUSH_DISABLE.

``VI_ATTR_RD_BUF_SIZE``
^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** all resources except USB RAW
| **Access:** RO
| **Coverage:** Missing

Proposition for future change: together with ``VI_ATTR_WR_BUF_SIZE``. Not sure how to do that, as we don't have ``viSetBuf()``

``VI_ATTR_RM_SESSION``
^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** all resources
| **Access:** RO
| **Coverage:** Full

``VI_ATTR_RSRC_CLASS``
^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** all resources
| **Access:** RO
| **Coverage:** Full

``VI_ATTR_RSRC_IMPL_VERSION``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** all resources
| **Access:** RO
| **Coverage:** Missing

``VI_ATTR_RSRC_LOCK_STATE``
^^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** all resources
| **Access:** RO
| **Coverage:** Full

However, the only locking that is supported is exclusive locking, on HiSLIP and VXI-11. 
Lock sharing or lock nesting is not supported.

``VI_ATTR_RSRC_MANF_ID``
^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** all resources
| **Access:** RO
| **Coverage:** Full. Uses 0 as a placeholder for the VXI manufacturer ID.

``VI_ATTR_RSRC_MANF_NAME``
^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** all resources
| **Access:** RO
| **Coverage:** Full ("PyVISA-Py")

``VI_ATTR_RSRC_NAME``
^^^^^^^^^^^^^^^^^^^^^
| **Used by:** all resources
| **Access:** RO
| **Coverage:** Full

``VI_ATTR_RSRC_SPEC_VERSION``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** all resources
| **Access:** RO
| **Coverage:** Full

``VI_ATTR_SEND_END_EN``
^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** all resources except USB RAW
| **Access:** R/W
| **Coverage:**
|  - GPIB INSTR, GPIB INTFC: Full
|  - ASRL, VXI-11, HiSLIP, USB, SOCKET: Partial; The partial implementations expose or read the setting but do not consistently apply it to the underlying transport's end-of-message behavior.

Proposition for future change: to be tackled together with ``VI_ATTR_SUPPRESS_END_EN``

``VI_ATTR_SUPPRESS_END_EN``
^^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** GPIB INSTR, ASRL INSTR, TCPIP INSTR (VXI-11 and HiSLIP), SOCKET, USB INSTR
| **Access:** R/W
| **Coverage:**
|  - ASRL, VXI-11, SOCKET, USB: Full
|  - HiSLIP: Partial; HiSLIP stores the value but its receive implementation does not honor it.
|  - GPIB: Missing

Proposition for future change: to be tackled together with ``VI_ATTR_SEND_END_EN``

``VI_ATTR_TCPIP_ADDR``
^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** TCPIP INSTR (VXI-11 and HiSLIP), SOCKET
| **Access:** RO
| **Coverage:** Full

``VI_ATTR_TCPIP_DEVICE_NAME``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** TCPIP INSTR (VXI-11 and HiSLIP)
| **Access:** RO
| **Coverage:** Full

``VI_ATTR_TCPIP_HISLIP_MAX_MESSAGE_KB``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** TCPIP INSTR (HiSLIP)
| **Access:** R/W
| **Coverage:** Full

``VI_ATTR_TCPIP_HISLIP_OVERLAP_EN``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** TCPIP INSTR (HiSLIP)
| **Access:** R/W
| **Coverage:** Partial; The value is stored but changes do not issue the required HiSLIP device clear or switch the protocol's overlap mode.

Proposition for future change: not easy to do. Default value is supposed to be "Preference returned by device."
For now: Fake RW (force to VI_FALSE, unsupported-state otherwise)

``VI_ATTR_TCPIP_HISLIP_VERSION``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** TCPIP INSTR (HiSLIP)
| **Access:** RO
| **Coverage:** Full

``VI_ATTR_TCPIP_HOSTNAME``
^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** TCPIP INSTR (VXI-11 and HiSLIP), SOCKET
| **Access:** RO
| **Coverage:** Full

``VI_ATTR_TCPIP_IS_HISLIP``
^^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** TCPIP INSTR (VXI-11 and HiSLIP)
| **Access:** RO
| **Coverage:** Full

``VI_ATTR_TCPIP_KEEPALIVE``
^^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** TCPIP INSTR (HiSLIP), SOCKET
| **Access:** R/W
| **Coverage:** Full

``VI_ATTR_TCPIP_NODELAY``
^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** TCPIP INSTR (HiSLIP), SOCKET
| **Access:** R/W
| **Coverage:**
|  - SOCKET: Full
|  - HiSLIP: Partial; HiSLIP reports a stored value but does not set the TCP socket's ``TCP_NODELAY`` option.

Proposition for future change: do like SOCKET

``VI_ATTR_TCPIP_PORT``
^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** TCPIP INSTR (HiSLIP), SOCKET
| **Access:** RO
| **Coverage:** Full

``VI_ATTR_TERMCHAR``
^^^^^^^^^^^^^^^^^^^^
| **Used by:** all resources except USB RAW
| **Access:** R/W
| **Coverage:**
|  - HiSLIP: Partial; HiSLIP stores the value but does not use it to terminate reads.
|  - All others: Full

Proposition for future change: to be tackled together with ``VI_ATTR_TERMCHAR_EN``

``VI_ATTR_TERMCHAR_EN``
^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** all resources except USB RAW
| **Access:** R/W
| **Coverage:**
|  - HiSLIP: Partial; HiSLIP stores the value but does not use it to terminate reads.
|  - All others: Full

Proposition for future change: to be tackled together with ``VI_ATTR_TERMCHAR``

``VI_ATTR_TMO_VALUE``
^^^^^^^^^^^^^^^^^^^^^
| **Used by:** all resources
| **Access:** R/W
| **Coverage:** Full

``VI_ATTR_TRIG_ID``
^^^^^^^^^^^^^^^^^^^
| **Used by:** GPIB INSTR, ASRL INSTR, TCPIP INSTR (VXI-11 and HiSLIP), USB INSTR
| **Access:** R/W while trigger sensing is disabled, RO while it is enabled
| **Coverage:** Missing

Although officially mandatory on all resources, it only seems to be relevant for VXI,
which is not supported by PyVISA-Py.
NI-VISA and R&S VISA do not support it in VXI-11 nor HiSLIP.
NI-VISA claims it is fixed to VI_TRIG_SW for GPIB, Serial, TCPIP.

``VI_ATTR_USB_INTFC_NUM``
^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** USB INSTR
| **Access:** RO
| **Coverage:** Full

``VI_ATTR_USB_MAX_INTR_SIZE``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** USB INSTR
| **Access:** R/W while USB interrupt sensing is disabled, RO while it is enabled
| **Coverage:** Missing

``VI_ATTR_USB_PROTOCOL``
^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** USB INSTR
| **Access:** RO
| **Coverage:** Missing

``VI_ATTR_USB_SERIAL_NUM``
^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** USB INSTR
| **Access:** RO
| **Coverage:** Full

``VI_ATTR_USER_DATA`` / ``VI_ATTR_USER_DATA_32`` / ``VI_ATTR_USER_DATA_64``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** all resources
| **Access:** RW
| **Coverage:** Full

``VI_ATTR_WR_BUF_OPER_MODE``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** all resources except USB RAW
| **Access:** R/W
| **Coverage:** Partial; fixed to VI_FLUSH_WHEN_FULL

``VI_ATTR_WR_BUF_SIZE``
^^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** all resources except USB RAW
| **Access:** RO
| **Coverage:** Missing

Proposition for future change: together with ``VI_ATTR_RD_BUF_SIZE``. Not sure how to do that, as we don't have ``viSetBuf()``

``VI_KTATTR_LOCKWAIT``
^^^^^^^^^^^^^^^^^^^^^^
| **Used by:** VXI-11 INSTR
| **Access:** RW
| **Coverage:** Full

This is a PyVISA-Py and Keysight specific attribute.
