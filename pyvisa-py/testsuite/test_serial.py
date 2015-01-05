# -*- coding: utf-8 -*-

from __future__ import division, unicode_literals, print_function, absolute_import

import importlib

try:
    import Queue as queue
except ImportError:
    import queue

from pyvisa.testsuite import BaseTestCase
from pyvisa import constants

serial = importlib.import_module('pyvisa-py.serial')
common = importlib.import_module('pyvisa-py.common')
SerialSession = serial.SerialSession
iter_bytes = common.iter_bytes


class NamedObject(object):
    """A class to construct named sentinels.
    """

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return '<%s>' % self.name

    __str__ = __repr__


class SpecialByte(NamedObject):

    def __len__(self):
        return 1


EOM4882 = SpecialByte('EOM4882')

class MockSerialInterface(common.MockInterface):

    def __init__(self, resource_name, messages):
        super(MockSerialInterface, self).__init__(resource_name)

        #: Stores the queries accepted by the device.
        #: dict[tuple[bytes], tuple[bytes])
        self._queries = {}

        for key, value in messages.items():
            assert isinstance(key, bytes)
            assert isinstance(value, bytes)
            self._queries[tuple(iter_bytes(key))] = tuple(iter_bytes(value))

        #: Buffer in which the user can read
        #: queue.Queue[bytes]
        self._output_buffer = queue.Queue()

        #: Buffer in which the user can write
        #: [bytes]
        self._input_buffer = list()

    def __call__(self, *args, **kwargs):
        return self

    def write(self, data):
        """Write data into the device input buffer.

        :param data: single element byte
        :type data: bytes
        """
        if not isinstance(data, (bytes, SpecialByte)):
            raise TypeError('data must be an instance of bytes or SpecialByte')

        if len(data) != 1:
            raise ValueError('data must have a length of 1, not %d' % len(data))

        self._input_buffer.append(data)

        # After writing to the input buffer, tries to see if the query is in the
        # list of messages it understands and reply accordingly.
        try:
            answer = self._queries[tuple(self._input_buffer)]
            for part in answer:
                self._output_buffer.put(part)

            self._input_buffer.clear()
        except KeyError:
            pass

        return len(data)

    def read(self, n):
        """Return a single byte from the output buffer
        """
        out = b''
        for _ in range(n):
            out += self._output_buffer.get_nowait()

        return out



class Test(BaseTestCase):

    def test_simple(self):

        a, b = b'*IDN?\n', b'Test\n'

        sess = SerialSession(None, MockSerialInterface('ASRL11::INSTR', {a: b}))

        sess.write(a)
        self.assertEqual(sess.read(1), (b, constants.StatusCode.success_termination_character_read))


