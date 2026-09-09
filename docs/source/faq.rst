.. _faq:


FAQ
===


Are all VISA attributes and methods implemented?
------------------------------------------------

No. We have implemented those attributes and methods that are most commonly
needed. We would like to reach feature parity. If there is something that you
need, let us know.


How do I cancel a pending I/O operation (viTerminate)?
------------------------------------------------------

For HiSLIP sessions (``TCPIP::host::hislip0::INSTR``), ``viTerminate()`` is
supported.  This allows one thread to cancel a blocking read that is running
in another thread, without destroying the session.

The blocked read will return with ``VI_ERROR_ABORT``.  The HiSLIP protocol
state is automatically reset (via a device clear) so the session is ready for
further I/O immediately::

    >>> import pyvisa
    >>> rm = pyvisa.ResourceManager('@py')
    >>> inst = rm.open_resource('TCPIP::192.168.1.100::hislip0::INSTR')
    >>> 
    >>> # From another thread, to cancel a blocked read:
    >>> inst.visalib.terminate(inst.session, None, None)
    >>> 
    >>> # The blocked read returns VI_ERROR_ABORT.
    >>> # The session is ready for further I/O — no manual viClear() needed.

``viTerminate()`` is not yet supported for VXI-11, USBTMC, or serial sessions.

.. note::

    **Portability:** This implementation goes beyond what mainstream VISA
    libraries provide for synchronous operations.  For example, Keysight IO
    Libraries' ``viTerminate()`` returns ``VI_SUCCESS`` but does not actually
    cancel a blocked synchronous ``viRead()`` — the read continues until the
    normal timeout expires.  The VISA specification defines ``viTerminate()``
    primarily for asynchronous operations (``viReadAsync`` / ``viWriteAsync``),
    and its behavior on synchronous calls is implementation-defined.  Code
    that relies on ``viTerminate()`` cancelling a synchronous read may not
    be portable to other VISA backends.


Why are you developing this?
----------------------------

The IVI compliant VISA implementations available (`National Instruments NI-VISA`_ ,
`Keysight IO Libraries`_, `Tektronix TekVISA`_, etc) are proprietary libraries that only work on
certain systems. We wanted to provide a compatible alternative.


Are GBIP secondary addresses supported?
---------------------------------------

GPIB secondary addresses are supported in NI-VISA fashion, meaning that the
secondary address is not 96 to 126 as transmitted on the bus, but 0 to 30.

For expample, ``GPIB0::9::1::INSTR`` is the address of the first VXI module
controlled by a GPIB VXI command module set to primary address ``9``, while
the command module itself is found at ``GPIB0::9::0::INSTR``, which is distinct
from a pure primary address like ``GPIB0::9::INSTR``.

``ResourceManager.list_resources()`` can discover both primary and secondary 
addressable ``GPIB::...::INSTR`` resources. As a result, it can be slow,
as it now needs to check 992 addresses per GPIB controller instead of just 31.

For every primary address where no listener is detected, all
secondary addresses are checked for listeners as well to find, for example,
VXI modules controlled by an HP E1406A.

For primary addresses where a listener is detected, no secondary addresses are
checked as most devices simply ignore secondary addressing.

If you have an instrument that reacts to the primary address and has different
functionality on some secondary addresses, please leave a bug report.

If you use a VXI-11.2 (VXI-11 to GPIB) gateway, you can use constructions like 
``TCPIP::host::gpib0,9,1::INSTR``, where ``gpib0`` is the 'GPIB SICL Interface Name' 
configured on the gateway, ``9`` is the primary address of the instrument, 
and ``1`` is the secondary address of that instrument.
``ResourceManager.list_resources()`` will however not try to discover any resources
behind a VXI-11.2 gateway, as the SICL Interface Name is not automatically known.


Can PyVISA-py be used from a VM?
--------------------------------

Because PyVISA-py access hardware resources such as USB ports, running from a
VM can cause issues like unexpected timeouts because the VM does not
receive the response. You should consult your VM manual to determine
if you are able to setup the VM in such a way that it works.  See
https://github.com/pyvisa/pyvisa-py/issues/243 for the kind of issue
it can cause.


Can PyVISA-py be used from a Docker container?
----------------------------------------------
As the Windows variant of Docker can forward neither USB ports nor GPIB
interfaces, the obvious choice would be to connect via TCP/IP. The problem of a
Docker container is that idle connections are disconnected by the VPN garbage
collection. For this reason it is reasonable to enable keepalive packets.
The VISA attribute ``VI_ATTR_TCPIP_KEEPALIVE`` has been modified to work
for all TCP/IP instruments. Enabling this option can be done with:

    >>> inst.set_visa_attribute(pyvisa.constants.ResourceAttribute.tcpip_keepalive, True)

where ``inst`` is an active TCP/IP visa session.
(see https://tech.xing.com/a-reason-for-unexplained-connection-timeouts-on-kubernetes-docker-abd041cf7e02
if you want to read more about connection dropping in docker containers)


Why not using LibreVISA?
------------------------

LibreVISA_ is unmaintained at this point (latest release is from 2013).
However, you can use it with the IVI backend as it has the same API.
We think that PyVISA-py is easier to hack and we can quickly reach feature parity
with other IVI-VISA implementation for message-based instruments.


Why putting PyVISA in the middle?
---------------------------------

Because it allows you to change the backend easily without changing your application.
In other projects, we implemented classes to call USBTMC devices without PyVISA.
But this leads to code duplication or an adapter class in your code.
By using PyVISA as a frontend to many backends, we abstract these things
from higher level applications.


What does ``open_timeout`` control?
-----------------------------------

For the TCP transports (``TCPIP::INSTR``, both VXI-11 and HiSLIP, and
``TCPIP::SOCKET``), ``open_timeout`` bounds how long PyVISA-py will spend
establishing the connection::

    >>> import pyvisa
    >>> rm = pyvisa.ResourceManager('@py')
    >>> # allow 10 s to reach an instrument across a slow link
    >>> inst = rm.open_resource('TCPIP::192.168.1.100::INSTR', open_timeout=10000)

If you do not specify ``open_timeout``, the connection attempt is given 2000 ms.  An
``open_timeout`` of ``0`` selects that same default rather than meaning "give up
immediately", since ``ResourceManager.open_resource`` passes ``0`` whenever you
omit the argument.

.. note::

    **Portability:** what ``open_timeout`` does is seemingly inconsistent
    across VISA implementations.  It is the ``viOpen`` timeout parameter, which
    VPP-4.3 section 4.3.3 defines as the time to wait for a lock.  VPP-4.3
    PERMISSION 4.3.2 also allows using it for the open itself, which is what
    PyVISA-py does, but an implementation is free not to.  An ``open_timeout``
    chosen for a slow link may therefore have no effect elsewhere.

    The 2000 ms comes from VPP-4.3 RECOMMENDATION 4.3.3:

        If the value of the timeout parameter to viOpen is 0 and a VISA
        implementation uses the timeout when opening the resource, the
        implementation should behave as if the timeout parameter is the VISA
        default timeout value of 2000 milliseconds.


Locking
-------

**PyVISA-Py only supports exclusive locking on VXI-11 and HiSLIP. Shared locks and nested locking are not supported.** 

Serial and TCPIP::SOCKET instruments do not support locking.

With exclusive locking, only one session can be used at a time on an instrument.  
If another session has a lock, another client will not be able to communicate with
the instrument. Either ``open_resource`` will fail, either ``read`` / ``write`` / ``query`` /... 
operations will fail. 
The related error codes in that case are:
``VI_ERROR_RSRC_LOCKED`` (as it should), or ``VI_ERROR_TMO`` or ``VI_ERROR_RSRC_BUSY`` or ``VI_ERROR_IO``.

There are two ways of using exclusive locking on an instrument session via pyvisa:

- lock on open
- lock after open

"Lock on open" is done via the ``access_mode`` argument to ``ResourceManager.open_resource``.  The
default is ``pyvisa.constants.AccessModes.no_lock``, which does not request a lock.

    >>> import pyvisa
    >>> rm = pyvisa.ResourceManager('@py')
    >>> # allow 10 s to reach an instrument across a slow link, 
    >>> # and 10 s to acquire a lock on the instrument
    >>> inst = rm.open_resource('TCPIP::192.168.1.100::INSTR', open_timeout=10000, 
    >>>                         access_mode=pyvisa.constants.AccessModes.exclusive_lock)

The connection will be established with the same timeout handling as mentioned above,
but will then try to open a link with a lock timeout also governed by ``open_timeout``.
That lock request will succeed if the instrument grants it within this period.
An ``open_timeout`` of ``0`` or ``VI_TMO_IMMEDIATE`` there means: "give up immediately", 
while None means "10 seconds", and ``VI_TMO_INFINITE`` means "wait indefinitely".

If you want better control over the different timeout settings, use "lock after open":

    >>> import pyvisa
    >>> rm = pyvisa.ResourceManager('@py')
    >>> # allow 3 s to reach the instrument
    >>> inst = rm.open_resource('TCPIP::192.168.1.100::INSTR', open_timeout=3000)
    >>> # and then try to acquire a lock on the instrument with a 10 s timeout
    >>> inst.lock_excl(timeout=10000)

If you have not locked the instrument, and want to control the behaviour of your program
in case another program or session has locked it, you must choose one of the following methods:

- Request a lock via ``inst.lock_excl()``. This is the most portable. See above.
- Only for VXI-11: Configure the lock timeout via the Keysight and PyVISA-Py specific attribute ``VI_KTATTR_LOCKWAIT`` (0x0FFF002B)

    When using ``VI_KTATTR_LOCKWAIT`` on an instrument that is locked by another session:

    - If ``0``, operations will fail immediately. 
    - If ``1``, operations will wait for ``inst.timeout`` for the lock to be removed before failing. 

    The default value of ``VI_KTATTR_LOCKWAIT`` is ``FALSE/0`` (do not wait).

    In theory ``VI_KTATTR_LOCKWAIT = False`` should behave the same as 
    ``inst.timeout = 0`` + ``VI_KTATTR_LOCKWAIT = True`` when applied to an operation on an instrument 
    that already has a lock: they should reply immediately with ``VI_ERROR_RSRC_LOCKED``.
    However, in practice this is not always the case, and some instruments take quite some liberties with it.    

    ``VI_KTATTR_LOCKWAIT`` may not be visible in the ``pyvisa/constants.py`` file,
    but it is set by PyVISA-Py. Just use as follows:

    >>> import pyvisa
    >>> import pyvisa.constants
    >>> rm = pyvisa.ResourceManager('@py')
    >>> inst = rm.open_resource('TCPIP::192.168.1.100::INSTR')
    >>>
    >>> # Set lockwait to True (VI_TRUE = 1)
    >>> inst.set_visa_attribute(pyvisa.constants.VI_KTATTR_LOCKWAIT, 1)  # type: ignore[attr-defined]
    >>> # Read back the attribute value
    >>> lockwait_val = inst.get_visa_attribute(pyvisa.constants.VI_KTATTR_LOCKWAIT) # type: ignore[attr-defined]
    >>> print("Lockwait state:", lockwait_val)
    >>>
    >>> # Do your operations


    Note that ``open_resource()`` and ``lock_excl()`` use their own timeout values, and do not use ``VI_KTATTR_LOCKWAIT``.

    ``session.lock_timeout``, from previous PyVISA-Py versions, has been removed, and replaced by the 
    use of ``VI_KTATTR_LOCKWAIT``, as it is easier, more predictable and more portable.

The ``VI_ATTR_RSRC_LOCK_STATE`` attribute is fully supported on HiSLIP: it reflects the current lock state of the resource accurately.
On VXI-11, it represents the lock state of the session, so it may not accurately reflect the lock state of the underlying resource itself.


Event handling is not affected by locking. 


.. note::

    **Portability:** Use of `lock_excl()` is the most portable, robust, and predictable way to handle locking.
    
    Know that some devices (even recent ones from the big brands), and all of the VISA 
    backends, use a certain amount of liberties with regards to the standards. 
    Do not expect respect of the following:

    - the prescribed return codes (example: you may see "I/O Timeout" instead of "Resource already locked"),
    - the length of the timeouts (timeouts may be significantly longer or shorter than specified)
    - the sequencing: some devices, once already locked, will allow `open_resource`
      to succeed (as they should per VXI-11 spec RULE B.6.6), but others don't.
    
    Worse, some instruments do not clean up locks after the connection is closed. 
    That is not only a violation of the expected behavior according to the standards, 
    but can also lead to unexpected locking issues in subsequent connections, requiring an instrument reboot.

    About the lock timeout handling: Keysight VISA and PyVISA-py both support the lock timeout 
    attribute ``VI_KTATTR_LOCKWAIT``. NI-VISA and R&S VISA have no known means of controlling 
    the lock timeout, and mostly use the I/O timeout and/or internal timing for lock timeout handling.

    If you are debugging locking issues, note that NI-VISA supports
    the lock-on-open method, but underneath uses the lock-after-open method, and, 
    after having established a lock, handles the locking internally without addressing
    the instrument.

Remote/Local control
--------------------

Setting an instrument to Remote or Local is possible via VXI-11, HiSLIP and GPIB.

In PyVISA, this is done through ``inst.control_ren(pyvisa.constants.RENLineOperation.{op})``
where valid ``{op}`` values are:

================  ===========  =========================================
RENLineOperation  VXI-11       HiSLIP
================  ===========  =========================================
address_gtl       goto local   goto local, no change to remote enable
asrt              error        enable remote
asrt_address      goto remote  enable remote, goto remote
asrt_address_llo  goto remote  enable remote, goto remote, local lockout
asrt_llo          error        enable remote, local lockout
deassert          error        disable remote
deassert_gtl      goto local   disable remote, goto local
================  ===========  =========================================

This is fully conform to what VPP-4.3 Rule 6.5.6 and Observations 6.5.1 + 6.5.2 say,
and what NI-VISA does, so this should be fully portable.

GPIB has functionality comparable to HiSLIP, but may behave differently than NI-VISA, 
depending on the type of interface.

Triggers
--------

The trigger functionality in PyVISA-Py is almost feature complete. 
Triggers are supported on GPIB, VXI-11 and HiSLIP resources, and on the 
USB and USBTMC resources that support it. They are also supported on 
Serial devices and TCP/IP sockets, via ``VI_ATTR_IO_PROT``. See the `Attributes` 
section below for more details.

In addition, for Prologix resources: ``inst.assert_trigger()`` will send ``++trg\n``

Attributes
----------

VI_ATTR_IO_PROT (``ResourceAttribute.io_prot``)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Is supported on Serial and TCPIP socket resources (when set to ``VI_PROT_4882_STRS``).

For GPIB resources, this attribute is not (yet) supported.


.. _PySerial: https://pythonhosted.org/pyserial/
.. _PyVISA: http://pyvisa.readthedocs.org/
.. _PyUSB: https://github.com/pyusb/pyusb
.. _PyPI: https://pypi.python.org/pypi/PyVISA-py
.. _GitHub: https://github.com/pyvisa/pyvisa-py
.. _`National Instruments NI-VISA`: http://ni.com/visa/
.. _`LibreVISA`: http://www.librevisa.org/
.. _`issue tracker`: https://github.com/pyvisa/pyvisa-py/issues
.. _`linux-gpib`: http://linux-gpib.sourceforge.net/
.. _`gpib-ctypes`: https://pypi.org/project/gpib-ctypes/
.. _`Tektronix TekVISA`: https://www.tek.com/en/support/software/driver/tekvisa-connectivity-software-v420
.. _`Keysight IO Libraries`: https://www.keysight.com/us/en/lib/software-detail/computer-software/io-libraries-suite-downloads-2175637.html
