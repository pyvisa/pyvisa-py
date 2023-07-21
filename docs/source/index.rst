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

    >>> import pyvisa
    >>> rm = pyvisa.ResourceManager('@py')
    >>> rm.list_resources()
    ('USB0::0x1AB1::0x0588::DS1K00005888::INSTR')
    >>> inst = rm.open_resource('USB0::0x1AB1::0x0588::DS1K00005888::INSTR')
    >>> print(inst.query("*IDN?"))


That's all! Except for **@py**, the code is exactly what you would write to
using the NI-VISA backend for PyVISA.

Currently Pyvisa-py support the following resources:

- TCPIP INSTR
- TCPIP SOCKET
- GPIB INSTR
- ASRL INSTR
- USB INSTR
- USB RAW

    Note:
    ASRL INSTR supports also URL Handlers like 
    
    - loop:// --> ASLRloop://::INSTR
    - socket:// --> ASRLsocket://::INSTR

    These entries will not be listed during the device discovery `rm.list_resources()`.
    For further details see https://pyserial.readthedocs.io/en/latest/url_handlers.html
    

You can report a problem or ask for features in the `issue tracker`_.
Or get the code in GitHub_.

.. toctree::
    :maxdepth: 2

    Installation <installation.rst>
    FAQ <faq.rst>

.. _PyVISA: http://pyvisa.readthedocs.org/
.. _GitHub: https://github.com/pyvisa/pyvisa-py
.. _`issue tracker`: https://github.com/pyvisa/pyvisa-py/issues

