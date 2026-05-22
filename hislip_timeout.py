import pyvisa

rm = pyvisa.ResourceManager("@py")
# r = rm.open_resource("TCPIP::127.0.0.1::hislip0::INSTR")
# print(r.query("*IDN?"))

rm.open_resource("TCPIP::192.168.0.125::hislip99::INSTR")
