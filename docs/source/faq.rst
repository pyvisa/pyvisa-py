.. _faq:


FAQ
===


Are all VISA attributes and methods implemented?
------------------------------------------------

No. We have implemented those attributes and methods that are most commonly
needed. We would like to reach feature parity. If there is something that you
need, let us know.


Why are you developing this?
----------------------------

The IVI compliant VISA implementations available (`National Instruments NI-VISA`_ ,
`Keysight IO Libraries`_, `Tektronix TekVISA`_, etc) are proprietary libraries that only work on
certain systems. We wanted to provide a compatible alternative.


Are GBIP secondary addresses supported?
---------------------------------------

GPIB secondary addresses are supported in NI-VISA fashion, meaning that the
secondary address is not 96 to 126 as transmitted on the bus, but 0 to 30.

For expample, `GPIB0::9::1::INSTR` is the address of the first VXI module
controlled by a GPIB VXI command module set to primary address `9`, while
the command module itself is found at `GPIB0::9::0::INSTR`, which is distinct
from a pure primary address like `GPIB0::9::INSTR`.

``ResourceManager.list_resources()`` has become slower as a result,
as it now needs to check 992 addresses per GPIB controller instead of just 31.

For every primary address where no listener is detected, all
secondary addresses are checked for listeners as well to find, for example,
VXI modules controlled by an HP E1406A.

For primary addresses where a listener is detected, no secondary addresses are
checked as most devices simply ignore secondary addressing.

If you have a device that reacts to the primary address and has different
functionality on some secondary addresses, please leave a bug report.


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
The VISA attribute `VI_ATTR_TCPIP_KEEPALIVE` has been modified to work
for all TCP/IP instruments. Enabling this option can be done with:

    inst.set_visa_attribute(pyvisa.constants.ResourceAttribute.tcpip_keepalive, True)

where `inst` is an active TCP/IP visa session.
(see https://tech.xing.com/a-reason-for-unexplained-connection-timeouts-on-kubernetes-docker-abd041cf7e02
if you want to read more about connection dropping in docker containers)


Why not using LibreVISA?
------------------------

LibreVISA_ is still young and appears mostly unmaintained at this
point (latest release is from 2013).
However, you can already use it with the IVI backend as it has the same API.
We think that PyVISA-py is easier to hack and we can quickly reach feature parity
with other IVI-VISA implementation for message-based instruments.


Why putting PyVISA in the middle?
---------------------------------

Because it allows you to change the backend easily without changing your application.
In other projects, we implemented classes to call USBTMC devices without PyVISA.
But this leads to code duplication or an adapter class in your code.
By using PyVISA as a frontend to many backends, we abstract these things
from higher level applications.


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
