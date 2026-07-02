# -*- coding: utf-8 -*-

# Derived from gpib_ctypes/constants.py

from enum import IntEnum


class timeout(IntEnum):
    TNONE = 0  # infinite
    T10us = 1  # 10 usec
    T30us = 2  # 30 usec
    T100us = 3  # 100 usec
    T300us = 4  # 300 usec
    T1ms = 5  # 1 msec
    T3ms = 6  # 3 msec
    T10ms = 7  # 10 msec
    T30ms = 8  # 30 msec
    T100ms = 9  # 100 msec
    T300ms = 10  # 300 msec
    T1s = 11  # 1 sec
    T3s = 12  # 3 sec
    T10s = 13  # 10 sec
    T30s = 14  # 30 sec
    T100s = 15  # 100 sec
    T300s = 16  # 300 sec
    T1000s = 17  # 1000 sec


class config(IntEnum):
    IbcPAD = 0x1
    IbcSAD = 0x2
    IbcTMO = 0x3
    IbcEOT = 0x4
    IbcPPC = 0x5  # board only
    IbcREADDR = 0x6  # device only
    IbcAUTOPOLL = 0x7  # board only
    IbcCICPROT = 0x8  # board only
    IbcIRQ = 0x9  # board only
    IbcSC = 0xA  # board only
    IbcSRE = 0xB  # board only
    IbcEOSrd = 0xC
    IbcEOSwrt = 0xD
    IbcEOScmp = 0xE
    IbcEOSchar = 0xF
    IbcPP2 = 0x10  # board only
    IbcTIMING = 0x11  # board only
    IbcDMA = 0x12  # board only
    IbcReadAdjust = 0x13
    IbcWriteAdjust = 0x14
    IbcEventQueue = 0x15  # board only
    IbcSPollBit = 0x16  # board only
    IbcSpollBit = 0x16  # board only
    IbcSendLLO = 0x17  # board only
    IbcSPollTime = 0x18  # device only
    IbcPPollTime = 0x19  # board only
    IbcEndBitIsNormal = 0x1A
    IbcUnAddr = 0x1B  # device only
    IbcHSCableLength = 0x1F  # board only
    IbcIst = 0x20  # board only
    IbcRsv = 0x21  # board only
    IbcBNA = 0x200  # device only


class ask(IntEnum):
    IbaPAD = 0x1
    IbaSAD = 0x2
    IbaTMO = 0x3
    IbaEOT = 0x4
    IbaPPC = 0x5  # board only
    IbaREADDR = 0x6  # device only
    IbaAUTOPOLL = 0x7  # board only
    IbaCICPROT = 0x8  # board only
    IbaIRQ = 0x9  # board only
    IbaSC = 0xA  # board only
    IbaSRE = 0xB  # board only
    IbaEOSrd = 0xC
    IbaEOSwrt = 0xD
    IbaEOScmp = 0xE
    IbaEOSchar = 0xF
    IbaPP2 = 0x10  # board only
    IbaTIMING = 0x11  # board only
    IbaDMA = 0x12  # board only
    IbaReadAdjust = 0x13
    IbaWriteAdjust = 0x14
    IbaEventQueue = 0x15  # board only
    IbaSPollBit = 0x16  # board only
    IbaSpollBit = 0x16  # board only
    IbaSendLLO = 0x17  # board only
    IbaSPollTime = 0x18  # device only
    IbaPPollTime = 0x19  # board only
    IbaEndBitIsNormal = 0x1A
    IbaUnAddr = 0x1B  # device only
    IbaHSCableLength = 0x1F  # board only
    IbaIst = 0x20  # board only
    IbaRsv = 0x21  # board only
    IbaBNA = 0x200  # device only
    Iba7BitEOS = 0x1000  # board only, linux-gpib only


class status(IntEnum):
    DCAS = 0x0001  # device clear state
    DTAS = 0x0002  # device trigger state
    LACS = 0x0004  # interface is Listener
    TACS = 0x0008  # interface is Talker
    ATN = 0x0010  # attention
    CIC = 0x0020  # Controller-in-Charge
    REM = 0x0040  # remote state
    LOK = 0x0080  # lockout state
    CMPL = 0x0100  # IO completed
    EVENT = 0x0200  # DCAS, DTAS, or IFC occurred
    SPOLL = 0x0400  # board serial-polled by busmaster
    RQS = 0x0800  # device requesting service
    SRQI = 0x1000  # SRQ is asserted
    END = 0x2000  # EOI or EOS
    TIMO = 0x4000  # timeout
    ERR = 0x8000  # error


class lines(IntEnum):
    ValidDAV = 0x1  # the BusDAV bit is valid
    ValidNDAC = 0x2  # the BusNDAC bit is valid
    ValidNRFD = 0x4  # the BusNRFD bit is valid
    ValidIFC = 0x8  # the BusIFC bit is valid
    ValidREN = 0x10  # the BusREN bit is valid
    ValidSRQ = 0x20  # the BusSRQ bit is valid
    ValidATN = 0x40  # the BusATN bit is valid
    ValidEOI = 0x80  # the BusEOI bit is valid
    BusDAV = 0x100  # DAV line is asserted
    BusNDAC = 0x200  # NDAC line is asserted
    BusNRFD = 0x400  # NRFD line is asserted
    BusIFC = 0x800  # IFC line is asserted
    BusREN = 0x1000  # REN line is asserted
    BusSRQ = 0x2000  # SRQ line is asserted
    BusATN = 0x4000  # ATN line is asserted
    BusEOI = 0x8000  # EOI line is asserted


class sad(IntEnum):
    NO_SAD = 0
    ALL_SAD = -1
    FIRST_SAD = 0x60
    LAST_SAD = 0x7E


class stb(IntEnum):
    IbStbRQS = 0x40
    IbStbESB = 0x20
    IbStbMAV = 0x10


class command(IntEnum):
    IcGTL = 1  # go to local
    IcLLO = 3  # local lock out


class error(IntEnum):
    EDVR = 0  # system error
    ECIC = 1  # not CIC
    ENOL = 2  # no listener
    EADR = 3  # CIC and not addressed before IO
    EARG = 4  # bad argument to function call
    ESAC = 5  # not SAC
    EABO = 6  # IO aborted
    ENEB = 7  # GPIB board offline
    EDMA = 8  # DMA hardware error
    EOIP = 10  # previous IO still in progress
    ECAP = 11  # not capable
    EFSO = 12  # file system operation error
    EBUS = 14  # bus error
    ESTB = 15  # lost serial poll bytes
    ESRQ = 16  # SRQ stuck on
    ETAB = 20  # table overflow
    ELCK = 21  # interface locked
    EARM = 22  # failed to rearm
    EHDL = 23  # invalid handle
    EWIP = 26  # previous wait still in progress
    ERST = 27  # event notification cancelled due to reset
    EPWR = 28  # interface lost power
