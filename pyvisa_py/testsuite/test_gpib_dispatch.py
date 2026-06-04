"""Tests for the central GPIB::INSTR dispatch helper.

These exercise the routing logic in isolation: which backend session class
a parsed resource resolves to, in what order backends are consulted, and
the no-match behaviour. No real GPIB hardware or backend session is
involved — backends are registered as plain resolver callables returning
sentinel classes.

:copyright: 2026 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

import pytest

from pyvisa_py import gpib_dispatch
from pyvisa_py.sessions import OpenError


@pytest.fixture
def clean_registry():
    """Isolate the module-global backend list around each test.

    The dispatcher keeps its resolvers in a module-level list; save it,
    clear it for the test, and restore it afterwards so tests do not leak
    into one another or into real registrations.

    """
    saved = list(gpib_dispatch._GPIB_INSTR_BACKENDS)
    gpib_dispatch._GPIB_INSTR_BACKENDS.clear()
    try:
        yield gpib_dispatch._GPIB_INSTR_BACKENDS
    finally:
        gpib_dispatch._GPIB_INSTR_BACKENDS[:] = saved


class _Recorder:
    """Sentinel backend session that records its constructor arguments."""

    def __init__(self, rm, name, parsed, open_timeout):
        self.args = (rm, name, parsed, open_timeout)


def test_first_matching_backend_wins(clean_registry):
    class A(_Recorder):
        pass

    class B(_Recorder):
        pass

    gpib_dispatch.register_backend(lambda p: None, priority=0, label="none")
    gpib_dispatch.register_backend(lambda p: A, priority=10, label="a")
    gpib_dispatch.register_backend(lambda p: B, priority=20, label="b")

    obj = gpib_dispatch.GPIBInstrDispatch(
        object(), "GPIB0::1::INSTR", parsed=object(), open_timeout=None
    )
    assert isinstance(obj, A)


def test_no_backend_matches_raises_openerror(clean_registry):
    gpib_dispatch.register_backend(lambda p: None, priority=0)

    with pytest.raises(OpenError):
        gpib_dispatch.GPIBInstrDispatch(object(), "GPIB0::1::INSTR", parsed=object())


def test_empty_registry_raises_openerror(clean_registry):
    with pytest.raises(OpenError):
        gpib_dispatch.GPIBInstrDispatch(object(), "GPIB0::1::INSTR", parsed=object())


def test_priority_then_insertion_order(clean_registry):
    order = []

    def probe(tag):
        def resolve(parsed):
            order.append(tag)
            return None

        return resolve

    # Register out of order and with a tie at priority 10; the dispatcher
    # must consult them by ascending priority, breaking the tie by
    # insertion order (first-registered first).
    gpib_dispatch.register_backend(probe("p10-first"), priority=10)
    gpib_dispatch.register_backend(probe("p0"), priority=0)
    gpib_dispatch.register_backend(probe("p10-second"), priority=10)

    with pytest.raises(OpenError):
        gpib_dispatch.GPIBInstrDispatch(object(), "x", parsed=object())

    assert order == ["p0", "p10-first", "p10-second"]


def test_constructor_args_forwarded_unchanged(clean_registry):
    captured = {}

    class Fake:
        def __init__(self, rm, name, parsed, open_timeout):
            captured.update(
                rm=rm, name=name, parsed=parsed, open_timeout=open_timeout
            )

    gpib_dispatch.register_backend(lambda p: Fake, priority=0)

    rm, name, parsed, timeout = object(), "GPIB0::5::INSTR", object(), 7
    gpib_dispatch.GPIBInstrDispatch(rm, name, parsed=parsed, open_timeout=timeout)

    assert captured == {
        "rm": rm,
        "name": name,
        "parsed": parsed,
        "open_timeout": timeout,
    }


def test_resource_name_parsed_when_not_supplied(clean_registry):
    seen = {}

    def resolve(parsed):
        seen["parsed"] = parsed
        return None

    gpib_dispatch.register_backend(resolve, priority=0)

    with pytest.raises(OpenError):
        gpib_dispatch.GPIBInstrDispatch(object(), "GPIB0::5::INSTR")

    # The dispatcher parsed the string into a resource object for us.
    assert seen["parsed"] is not None
    assert str(seen["parsed"].board) == "0"
