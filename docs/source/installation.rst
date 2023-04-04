.. _installation:


Installation
============

Pyvisa-py is available on PyPI_ and can be easily installed using pip:

    pip install pyvisa-py


Pyvisa-py runs on Python 3.6+.

If you do not install any extra library pyvisa-py will only be able to access
tcpip resources. The following sections will describe what extra libraries you
need to install and how to configure them to use other resources.


Ethernet resources: TCPIP INSTR/SOCKET
--------------------------------------

Pyvisa-py relies on :py:mod:`socket` module in the Python Standard Library to
interact with the instrument which you do not need to install any extra library
to access those resources.

To discover VXI-11 devices on all network interfaces, please install
`psutil`_. Otherwise, discovery will only occur on the default network
interface.

Discovery of both HiSLIP  and VICP devices relies on `mDNS`_, which is a protocol for
service discovery in a local area network.  To enable resource
discovery for HiSLIP and VICP, you should install `zeroconf`_.

The TCP/IP VICP protocol (proprietary to Teledyne LeCroy) depends on
the `pyvicp`_ package.  You should install this package if you need to
use VICP.


Serial resources: ASRL INSTR
----------------------------

To access serial resources, you should install PySerial_. Version 3.0 or newer
is required. No special configuration is required.


GPIB resources: GPIB INSTR
--------------------------

On all platforms, using **GPIB** resources requires to install a gpib driver.
On Windows, it is install as part of NI-VISA or Keysight VISA for example. On
MacOSX, you should install the NI-488 library from National instrument. On
Linux, you can use a commercial driver (NI) or the `linux-gpib`_ project.

On Linux, `linux-gpib`_ comes with Python bindings so you do not have to
install any extra library.
On all systems with GPIB device drivers, GPIB support is available through
`gpib-ctypes`_.

You should not have to perform any special configuration after the install.


USB resources: USB INSTR/RAW
----------------------------

For **USB** resources, you need to install PyUSB_. PyUSB_ relies on USB driver
library such as libusb 0.1, libusb 1.0, libusbx, libusb-win32 and OpenUSB
that you should also install. Please refer to PyUSB_ documentation for more
details.

On Unix system, one may have to modify udev rules to allow non-root access to
the device you are trying to connect to. The following tutorial describes how
to do it http://ask.xmodulo.com/change-usb-device-permission-linux.html.

On Windows, you may have to uninstall the USBTMC-specific driver installed by
Windows and re-install a generic driver. Please check `libusb's guide`_ for more
details, but installing a ``WinUSB`` driver with Zadig_ should be a good start.

Note that on Windows, devices that are already open cannot be detected and will
not be returned by ``ResourceManager.list_resources``.


How do I know if PyVISA-py is properly installed?
-------------------------------------------------

Using the pyvisa information tool. Run in your console::

  pyvisa-info

You will get info about PyVISA, the installed backends and their options.


Using the development version
-----------------------------

You can install the latest development version (at your own risk) directly
form GitHub_::

    $ pip install -U git+https://github.com/pyvisa/pyvisa-py.git


.. _PySerial: https://pythonhosted.org/pyserial/
.. _PyVISA: http://pyvisa.readthedocs.org/
.. _PyUSB: https://github.com/pyusb/pyusb
.. _PyPI: https://pypi.python.org/pypi/PyVISA-py
.. _GitHub: https://github.com/pyvisa/pyvisa-py
.. _`National Instruments's VISA`: http://ni.com/visa/
.. _`LibreVISA`: http://www.librevisa.org/
.. _`issue tracker`: https://github.com/pyvisa/pyvisa-py/issues
.. _`linux-gpib`: http://linux-gpib.sourceforge.net/
.. _`gpib-ctypes`: https://pypi.org/project/gpib-ctypes/
.. _`psutil`: https://pypi.org/project/psutil/
.. _`mDNS`: https://en.wikipedia.org/wiki/Multicast_DNS
.. _`zeroconf`: https://pypi.org/project/zeroconf/
.. _`pyvicp`: https://pypi.org/project/pyvicp/
.. _`libusb's guide`: https://github.com/libusb/libusb/wiki/Windows#user-content-How_to_use_libusb_on_Windows
.. _`Zadig`: https://zadig.akeo.ie/
