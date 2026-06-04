# -*- coding: utf-8 -*-
"""Central dispatch for GPIB INSTR resources across pyvisa-py backends.

pyvisa-py can serve ``GPIB<n>::...::INSTR`` resources through several
mutually exclusive backends — native linux-gpib / gpib-ctypes, a Prologix
controller, or an NI GPIB-ENET/100 bridge. The session registry holds a
single class per ``(InterfaceType.gpib, "INSTR")`` slot.

This module owns the ``(gpib, "INSTR")`` slot once and resolves the
concrete session class at open time by consulting an ordered list of
backend resolvers. :func:`register_builtin_backends` imports each in-tree
backend defensively and registers a resolver for it; a backend whose
import fails is simply absent, so the remaining backends keep working
regardless of import order. The module has no hard dependency on any GPIB
library, so it can own the slot even when ``gpib.py`` fails to import.

:copyright: 2026 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

from typing import Callable, List, Optional, Tuple, Type, cast

from pyvisa import constants, rname
from pyvisa.constants import StatusCode
from pyvisa.rname import GPIBInstr
from pyvisa.typing import VISARMSession

from .common import LOGGER
from .sessions import OpenError, Session, UnavailableSession

#: A resolver inspects a parsed resource name and returns the session class
#: that should handle it, or ``None`` if the resource is not served by this
#: backend (e.g. the board number is not registered to it). Resources reach
#: this dispatcher only via the ``(gpib, "INSTR")`` slot, so the parsed name
#: is always a :class:`~pyvisa.rname.GPIBInstr`.
GPIBInstrResolver = Callable[[GPIBInstr], Optional[Type[Session]]]

#: Registered backends as ``(priority, label, resolver)``. Lower priority
#: values are consulted first; :func:`register_backend` keeps the list
#: sorted. The sort is stable, so insertion order breaks ties — import
#: order therefore only affects equal-priority backends, never the
#: correctness of the fallback chain.
_GPIB_INSTR_BACKENDS: List[Tuple[int, str, GPIBInstrResolver]] = []


def register_backend(
    resolver: GPIBInstrResolver, priority: int, label: str = ""
) -> None:
    """Register a backend resolver for ``GPIB::INSTR`` dispatch.

    Parameters
    ----------
    resolver : GPIBInstrResolver
        Callable returning the session class for a parsed resource, or
        ``None`` if this backend does not serve it.
    priority : int
        Lower values are consulted first. A catch-all fallback (e.g. the
        native driver, which claims any board) should use a high value so
        it never shadows a more specific backend.
    label : str
        Optional human-readable name, used only for debugging.

    """
    _GPIB_INSTR_BACKENDS.append((priority, label, resolver))
    _GPIB_INSTR_BACKENDS.sort(key=lambda item: item[0])


@Session.register(constants.InterfaceType.gpib, "INSTR")
class GPIBInstrDispatch(Session):
    """Resolve a ``GPIB::INSTR`` resource to the right backend session.

    The class is a dispatch shim: its :meth:`__new__` returns an instance
    of a *different* class (the backend session), so ``__init__`` is never
    run on the dispatcher itself.

    """

    def __new__(  # type: ignore[misc]
        cls,
        resource_manager_session: VISARMSession,
        resource_name: str,
        parsed: Optional[rname.ResourceName] = None,
        open_timeout: Optional[int] = None,
    ) -> Session:
        if parsed is None:
            parsed = rname.parse_resource_name(resource_name)
        # Registered only for (gpib, INSTR), so the parsed name is a GPIBInstr.
        gpib_parsed = cast(GPIBInstr, parsed)

        for _priority, _label, resolve in _GPIB_INSTR_BACKENDS:
            newcls = resolve(gpib_parsed)
            if newcls is not None:
                return newcls(
                    resource_manager_session, resource_name, parsed, open_timeout
                )

        raise OpenError(StatusCode.error_resource_not_found)

    @staticmethod
    def list_resources() -> List[str]:
        """List native GPIB::INSTR resources found on local boards.

        Only the native linux-gpib / gpib-ctypes listeners are enumerated:
        Prologix has no listing support, and NI GPIB-ENET/100 instruments
        are discovered at the INTFC level. Returns an empty list when no
        native GPIB library is installed.

        """
        try:
            from .gpib import _find_listeners
        except Exception:
            return []
        return [
            "GPIB%d::%d::INSTR" % (board, pad)
            if sad == 0
            else "GPIB%d::%d::%d::INSTR" % (board, pad, sad - 0x60)
            for board, pad, sad in _find_listeners()
        ]


# --- Built-in backend wiring ------------------------------------------------
# Priorities: lower values are consulted first. A backend bound to specific
# boards (Prologix, NI-ENET/100 bridge) must out-rank the native driver,
# which is the catch-all fallback and therefore comes last.
_PRIORITY_BRIDGE = 10
_PRIORITY_PROLOGIX = 20
_PRIORITY_NATIVE = 100

#: Guards :func:`register_builtin_backends` so repeated calls (e.g. one per
#: ResourceManager) do not append duplicate resolvers.
_builtins_registered = False


def _board_resolver(boards: dict, session_cls: Type[Session]) -> GPIBInstrResolver:
    """Build a resolver that claims a resource only for registered boards.

    The ``boards`` mapping is captured by reference and queried at dispatch
    time, so boards registered after wiring (the normal case — INTFC
    sessions populate it on open) are still seen.

    """

    def resolve(parsed: GPIBInstr) -> Optional[Type[Session]]:
        return session_cls if parsed.board in boards else None

    return resolve


def _make_native_unavailable(exc: Exception) -> Type[Session]:
    """Return an unavailable session explaining the missing GPIB library.

    ``gpib.py`` raises on import when neither linux-gpib nor gpib-ctypes is
    present. Routing unmatched boards here preserves the previous
    behaviour: a clear, actionable error at open time instead of a generic
    "resource not found".

    """

    class _NativeGPIBUnavailable(UnavailableSession):
        session_issue = (
            "Please install linux-gpib (Linux) or gpib-ctypes (Windows, Linux) "
            "to use native GPIB::INSTR resources.\n%s" % exc
        )

    return _NativeGPIBUnavailable


def register_builtin_backends() -> None:
    """Wire pyvisa-py's in-tree GPIB::INSTR backends into the dispatcher.

    Each backend is imported defensively: a backend whose import fails is
    simply skipped (native gpib is additionally represented by an
    unavailable session so its install hint survives). The call is
    idempotent.

    """
    global _builtins_registered
    if _builtins_registered:
        return

    # NI GPIB-ENET/100 bridge: claims boards bound to a bridge INTFC. Highest
    # precedence so a board registered to a bridge is never shadowed.
    try:
        from . import nienet100
    except Exception as e:
        LOGGER.debug("NI-ENET/100 GPIB::INSTR backend not registered: %s", e)
    else:
        register_backend(
            _board_resolver(
                nienet100._NIEnet100IntfcSession.boards,
                nienet100.NIEnet100InstrSession,
            ),
            priority=_PRIORITY_BRIDGE,
            label="ni-enet100",
        )

    # Prologix controller: claims boards bound to a Prologix interface.
    try:
        from . import prologix
    except Exception as e:  # pragma: no cover - prologix import is robust
        LOGGER.debug("Prologix GPIB::INSTR backend not registered: %s", e)
    else:
        register_backend(
            _board_resolver(
                prologix._PrologixIntfcSession.boards, prologix.PrologixInstrSession
            ),
            priority=_PRIORITY_PROLOGIX,
            label="prologix",
        )

    # Native linux-gpib / gpib-ctypes: catch-all for any board not claimed
    # above.
    try:
        from .gpib import GPIBSession
    except Exception as e:
        native_cls: Type[Session] = _make_native_unavailable(e)
    else:
        native_cls = GPIBSession
    register_backend(lambda parsed: native_cls, priority=_PRIORITY_NATIVE, label="gpib")

    _builtins_registered = True
