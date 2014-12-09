# -*- coding: utf-8 -*-


class MockSession(object):

    buffer = None

    #: Dict Mapping questions to answers
    #: Dict[bytes, bytes]
    messages = None

    def __init__(self, messages):
        for key, values in messages.items:
            assert isinstance(key, bytes)
            assert isinstance(values, bytes)

    def write(self, data):
        assert isinstance(data, bytes)
        return 1

    def read(self, n):
        assert self.buffer is not None
        current, self.buffer = self.buffer[:n], self.buffer[n:]
        if not self.buffer:
            self.buffer = None
        return current
