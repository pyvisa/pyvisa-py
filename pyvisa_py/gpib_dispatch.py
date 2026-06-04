# -*- coding: utf-8 -*-
"""Central dispatch for GPIB INSTR resources across pyvisa-py backends.

pyvisa-py can serve ``GPIB<n>::...::INSTR`` resources through several
mutually exclusive backends — native linux-gpib / gpib-ctypes, a Prologix
controller, or an NI GPIB-ENET/100 bridge. The session registry holds a
single class per ``(InterfaceType.gpib, "INSTR")`` slot.

This module owns the slot once and resolves the concrete session class at
open time by consulting an ordered list of backend resolvers. Backends
register themselves via :func:`register_backend` when they import
successfully; a backend that fails to import is simply absent from the
list, so the remaining backends keep working regardless of import order.

The dispatcher is intentionally **not** registered in this module yet —
that happens once the existing backends have been migrated onto
:func:`register_backend`, so adding this module changes no behaviour on
its own.

:copyright: 2026 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

from typing import Callable, List, Optional, Tuple, Type

from pyvisa import rname
from pyvisa.constants import StatusCode
from pyvisa.typing import VISARMSession

from .sessions import OpenError, Session

#: A resolver inspects a parsed resource name and returns the session class
#: that should handle it, or ``None`` if the resource is not served by this
#: backend (e.g. the board number is not registered to it).
GPIBInstrResolver = Callable[[rname.ResourceName], Optional[Type[Session]]]

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

        for _priority, _label, resolve in _GPIB_INSTR_BACKENDS:
            newcls = resolve(parsed)
            if newcls is not None:
                return newcls(
                    resource_manager_session, resource_name, parsed, open_timeout
                )

        raise OpenError(StatusCode.error_resource_not_found)
