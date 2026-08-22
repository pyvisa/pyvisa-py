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

from pyvisa.constants import InterfaceType
from pyvisa_py import gpib_dispatch
from pyvisa_py.sessions import OpenError, Session


@pytest.fixture
def clean_registry():
    """Isolate the module-global backend list around each test.

    The dispatcher keeps its resolvers in a module-level list; save it,
    clear it for the test, and restore it afterwards so tests do not leak
    into one another or into real registrations.

    """
    saved = list(gpib_dispatch._GPIB_INSTR_BACKENDS)
    saved_flag = gpib_dispatch._builtins_registered
    gpib_dispatch._GPIB_INSTR_BACKENDS.clear()
    gpib_dispatch._builtins_registered = False
    try:
        yield gpib_dispatch._GPIB_INSTR_BACKENDS
    finally:
        gpib_dispatch._GPIB_INSTR_BACKENDS[:] = saved
        gpib_dispatch._builtins_registered = saved_flag


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
            captured.update(rm=rm, name=name, parsed=parsed, open_timeout=open_timeout)

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


class _Parsed:
    """Minimal stand-in for a parsed GPIB resource carrying only ``board``."""

    def __init__(self, board):
        self.board = board


def test_board_resolver_claims_only_registered_boards():
    class Sentinel:
        pass

    boards = {"1": object()}
    resolve = gpib_dispatch._board_resolver(boards, Sentinel)

    assert resolve(_Parsed("1")) is Sentinel
    assert resolve(_Parsed("2")) is None


def test_board_resolver_sees_boards_registered_after_wiring():
    class Sentinel:
        pass

    boards = {}
    resolve = gpib_dispatch._board_resolver(boards, Sentinel)

    # Board not present yet ...
    assert resolve(_Parsed("0")) is None
    # ... an INTFC session registers it later, by reference.
    boards["0"] = object()
    assert resolve(_Parsed("0")) is Sentinel


def test_native_unavailable_raises_actionable_error_on_open():
    cls = gpib_dispatch._make_native_unavailable(ImportError("no gpib lib"))

    with pytest.raises(ValueError) as info:
        cls(object(), "GPIB0::1::INSTR", _Parsed("0"), None)
    assert "gpib-ctypes" in str(info.value)


def test_register_builtin_backends_is_idempotent_and_ordered(clean_registry):
    gpib_dispatch.register_builtin_backends()
    after_first = list(clean_registry)
    gpib_dispatch.register_builtin_backends()
    after_second = list(clean_registry)

    # Idempotent: a second call adds nothing.
    assert after_first == after_second

    priorities = [priority for priority, _label, _resolve in after_first]
    labels = [label for _priority, label, _resolve in after_first]

    # Native is the catch-all and must be wired last (highest priority value).
    assert priorities == sorted(priorities)
    assert priorities[-1] == gpib_dispatch._PRIORITY_NATIVE
    assert labels[-1] == "gpib"
    # Pure-Python backends are always available and out-rank native; the
    # bridge out-ranks Prologix.
    assert labels.index("ni-enet100") < labels.index("prologix") < labels.index("gpib")
    assert dict(zip(labels, priorities))["ni-enet100"] == gpib_dispatch._PRIORITY_BRIDGE


def test_bridge_out_ranks_prologix_for_shared_board(clean_registry):
    # A board registered to *both* a bridge and Prologix must resolve to the
    # bridge, because the bridge resolver is wired with higher precedence.
    class Bridge(_Recorder):
        pass

    class Prlgx(_Recorder):
        pass

    bridge_boards = {"0": object()}
    prologix_boards = {"0": object()}
    gpib_dispatch.register_backend(
        gpib_dispatch._board_resolver(bridge_boards, Bridge),
        priority=gpib_dispatch._PRIORITY_BRIDGE,
        label="ni-enet100",
    )
    gpib_dispatch.register_backend(
        gpib_dispatch._board_resolver(prologix_boards, Prlgx),
        priority=gpib_dispatch._PRIORITY_PROLOGIX,
        label="prologix",
    )

    obj = gpib_dispatch.GPIBInstrDispatch(
        object(), "GPIB0::1::INSTR", parsed=_Parsed("0")
    )
    assert isinstance(obj, Bridge)


def test_central_dispatcher_owns_gpib_instr_slot():
    # Importing gpib_dispatch registers it as the single owner of the slot.
    assert (
        Session.get_session_class(InterfaceType.gpib, "INSTR")
        is gpib_dispatch.GPIBInstrDispatch
    )


def test_list_resources_is_callable_and_returns_list():
    # Returns native listeners when a GPIB library is present, otherwise an
    # empty list — never raises, regardless of platform.
    result = gpib_dispatch.GPIBInstrDispatch.list_resources()
    assert isinstance(result, list)
