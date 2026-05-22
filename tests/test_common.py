from typing import List, Optional

import pytest
from pyvisa_py import common


@pytest.mark.parametrize(
    "bits, want",
    [
        (0, 0b0),
        (1, 0b1),
        (5, 0b0001_1111),
        (7, 0b0111_1111),
        (8, 0b1111_1111),
        (11, 0b0111_1111_1111),
    ],
)
def test_create_bitmask(bits, want):
    got = common._create_bitmask(bits)
    assert got == want


@pytest.mark.parametrize(
    "data, data_bits, send_end, want",
    [
        (b"\x01", None, False, b"\x01"),
        (b"hello world!", None, False, b"hello world!"),
        (b"\x03", 2, None, b"\x03"),
        (b"\x04", 2, None, b"\x00"),
        (b"\xff", 5, None, b"\x1f"),
        (b"\xfe", 7, None, b"\x7e"),
        (b"\xfe", 8, None, b"\xfe"),
        (b"\xff", 9, None, b"\xff"),
        (b"\x04", 2, False, b"\x00"),
        (b"\x04", 3, False, b"\x00"),
        (b"\x05", 3, False, b"\x01"),
        (b"\xff", 7, False, b"\x3f"),
        (b"\xff", 8, False, b"\x7f"),
        (b"\x04", 2, True, b"\x02"),
        (b"\x04", 3, True, b"\x04"),
        (b"\x01", 3, True, b"\x05"),
        (b"\x9f", 7, True, b"\x5f"),
        (b"\x9f", 8, True, b"\x9f"),
        (b"\xff", 9, None, b"\xff"),
        (b"\xff", 9, False, b"\x7f"),
        (b"\xff", 9, True, b"\xff"),
        (b"\x6d\x5e\x25\x25", 4, None, b"\r\x0e\x05\x05"),
        (b"\x6d\x5e\x25\x25", 4, False, b"\x05\x06\x05\x05"),
        (b"\x6d\x5e\x25\x25", 4, True, b"\x05\x06\x05\x0d"),
        (b"a\xb1", 6, None, b"\x21\x31"),
        (b"a\xb1", 6, False, b"\x01\x11"),
        (b"a\xb1", 6, True, b"\x011"),
    ],
)
def test_iter_bytes(
    data: bytes, data_bits: Optional[int], send_end: bool, want: List[bytes]
) -> None:
    got = b"".join(common.iter_bytes(data, data_bits=data_bits, send_end=send_end))
    assert got == want


def test_iter_bytes_with_send_end_requires_data_bits() -> None:
    with pytest.raises(ValueError):
        list(common.iter_bytes(b"", data_bits=None, send_end=True))


def test_iter_bytes_raises_on_bad_data_bits() -> None:
    with pytest.raises(ValueError):
        list(common.iter_bytes(b"", data_bits=0, send_end=None))
