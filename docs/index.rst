:orphan:


PyVISA-py: Pure Python backend for PyVISA
=========================================

.. image:: _static/logo-full.jpg
   :alt: PyVISA


PyVISA-py is a backend for PyVISA_. It implements most of the methods
for Message Based communication (Serial/USB/GPIB/Ethernet) using Python
and some well developed, easy to deploy and cross platform libraries.

You can select the PyVISA-py backend using **@py** when instantiating the
visa Resource Manager:

    >>> import visa
    >>> rm = visa.ResourceManager('@py')
    >>> rm.list_resources()
    ('USB0::0x1AB1::0x0588::DS1K00005888::INSTR')
    >>> inst = rm.open_resource('USB0::0x1AB1::0x0588::DS1K00005888::INSTR')
    >>> print(inst.query("*IDN?"))


That's all! Except for **@py**, the code is exactly what you would write to
using the NI-VISA backend for PyVISA.


Installation
============

Just run the following command in your console:

    pip install pyvisa-py


You can report a problem or ask for features in the `issue tracker`_.
Or get the code in GitHub_.


FAQ
===


Which libraries are used by PyVISA-py?
--------------------------------------

It depends on the interface type. For **ASRL** and **USB** we use PySerial_ and PyUSB_
respectively. For **TCPIP** we use the :py:mod:`socket` module in the Python Standard Library.
**GPIB** resources are not currently supported but they are in the plan using `linux-gpib`_.


If I only need **TCPIP**, do I need to install PySerial and PyUSB?
------------------------------------------------------------------

No. Libraries are loaded on demand.


How do I know if PyVISA-py is properly installed?
-------------------------------------------------

Using the pyvisa information tool. Run in your console::

  python -m visa info

You will get info about PyVISA, the installed backends and their options.


Which resource types are supported?
-----------------------------------

Now:

- ASRL INSTR
- USB INSTR
- TCPIP INSTR
- USB RAW
- TCPIP SOCKET
- GPIB INSTR



Are all VISA attributes and methods implemented?
------------------------------------------------

No. We have implemented those attributes and methods that are most commonly needed.
We would like to reach feature parity. If there is something that you need, let us know.


Why are you developing this?
----------------------------

The `National Instruments's VISA`_ is a proprietary library that only works on certain systems.
We wanted to provide a compatible alternative.


Why not using LibreVISA?
------------------------

LibreVISA_ is still young. However, you can already use it with the NI backend as it
has the same API. We think that PyVISA-py is easier to hack and we can quickly
reach feature parity with NI-VISA for message-based instruments.


Why putting PyVISA in the middle?
---------------------------------

Because it allows you to change the backend easily without changing your application.
In other projects we implemented classes to call USBTMC devices without PyVISA.
But this leads to code duplication or an adapter class in your code.
By using PyVISA as a frontend to many backends, we abstract these things
from higher level applications.



.. _PySerial: http://pyserial.sourceforge.net/
.. _PyVISA: http://pyvisa.readthedocs.org/
.. _PyUSB: http://walac.github.io/pyusb/
.. _PyPI: https://pypi.python.org/pypi/PyVISA-py
.. _GitHub: https://github.com/hgrecco/pyvisa-py
.. _`National Instruments's VISA`: http://ni.com/visa/
.. _`LibreVISA`: http://www.librevisa.org/
.. _`issue tracker`: https://github.com/hgrecco/pyvisa-py/issues
.. _`linux-gpib`: http://linux-gpib.sourceforge.net/

