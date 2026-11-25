# Why

This directory holds any reference documentation that is needed for this project.

The [HiSLIP](https://www.ivifoundation.org/downloads/Protocol%20Specifications/IVI-6.1_HiSLIP-2.0-2020-04-23.pdf) and [VISA](https://www.ivifoundation.org/downloads/VISA/vpp43_2024-01-04.pdf) reference documentation can be found by clicking on the provided links, which will bring you to their official websites. However, if you want, you can safely 
download them to this directory, as they are mentioned in `.gitignore`.

# About VXI-11

Although HiSLIP is growing in use, VXI-11 is still the most used instrument communication protocol.

The VXI-11 reference documents were published in 1995, and were historically 
published via `vxibus.org`. That website has stopped functioning in 2026.  
Several copies of the reference documents exist on the internet (wayback machine, eevblog, personal copies), 
but no other website has officially taken over the role of hosting them.

Therefore, the VXI-11 standards documents are copied here.

If this poses problems, please tell us so and we'll remove the documents.

- VXI-11 (Base Protocol): Defines the overall network instrument protocol over TCP/IP using ONC-RPC (DEVICE_CORE, DEVICE_ASYNC, and DEVICE_INTR), including connection handling, data channels, interrupts, and concurrent access.
- VXI-11.1 (VXIbus / Slot 0 Controllers): Targets VXIbus chassis and VXIbus devices/Slot 0 controllers over a network. It is rarely used or practically unused in modern setups.
- VXI-11.2 (Ethernet-to-GPIB Gateways): Defines how a network gateway controls a GPIB (IEEE-488) bus and performs bus-specific functions for connected GPIB instruments (such as LAN-to-GPIB controllers).
- VXI-11.3 (Ethernet Instruments / IEEE-488.2): Defines instrument-specific functions—including data transfer to and from a device—for instruments with native Ethernet interfaces (such as IEEE-488.2 or LXI instruments).
