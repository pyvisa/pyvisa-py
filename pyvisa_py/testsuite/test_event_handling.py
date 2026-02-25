import pyvisa


def my_handler(session, event_type, context, user_handle):
    print("Interrupt Received! SRQ Triggered.")
    # You might need to query the device here to clear the bit
    # inst.query("*ESR?")


rm = pyvisa.ResourceManager("@py")  # Forces pyvisa-py backend
inst = rm.open_resource("TCPIP::192.168.1.157::INSTR")

# 1. Register the python function
inst.install_handler(pyvisa.constants.VI_EVENT_SERVICE_REQ, my_handler)

# 2. Tell the hardware to start monitoring (Starts your thread)
inst.enable_event(pyvisa.constants.VI_EVENT_SERVICE_REQ, pyvisa.constants.VI_HNDLR)

# 3. Enable SRQ on the device side (Standard SCPI)
inst.write("*SRE 16")  # Enable Message Available bit (example)
inst.write("*CLS")

print("Waiting for interrupt...")
input("Press Enter to exit")  # Keep script alive to listen
