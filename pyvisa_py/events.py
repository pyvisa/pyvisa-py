# -*- coding: utf-8 -*-
"""Event handling primitives for pyvisa-py.

This module provides the thread-safe building blocks used by the VISA event
subsystem: event contexts, queues, handler registries, and per-session state.

"""

import collections
import random
import threading
import time
import warnings
from dataclasses import dataclass, field
from typing import Any, Callable

from pyvisa import constants

import enum


class EventMechanism(enum.Flag):
    """Internal Flag enum mirroring VISA event-delivery mechanisms.

    ``ALL = OxFFFF`` is an *atypical* sentinel: it does **not**
    bitwise-compose from the auto-generated flags (``QUEUE | HANDLER |
    SUSPEND == 7``), but it is accepted from ``constants.EventMechanism.all``
    and is canonicalised to the three real flags on store so that bitwise
    ``~`` works correctly.
    """

    NONE = 0
    QUEUE = 1  # VI_QUEUE   (1)
    HANDLER = 2  # VI_HNDLR   (2)
    SUSPEND = 4  # VI_SUSPEND_HNDLR (4)
    ALL = 0xFFFF  # VI_ALL_MECH (0xFFFF)


from .common import LOGGER


@dataclass(frozen=True, slots=True)
class EventContext:
    """Immutable description of a single VISA event occurrence."""

    event_type: constants.EventType
    status_byte: int = 0
    timestamp: float = field(default_factory=time.time)
    context_id: int = field(default_factory=lambda: random.getrandbits(32))


class EventQueue:
    """Thread-safe FIFO queue for :class:`EventContext` objects."""

    def __init__(self) -> None:
        self._deque: collections.deque[EventContext] = collections.deque()
        self._cond = threading.Condition()

    def put(self, ctx: EventContext) -> None:
        """Add an event context to the queue (non-blocking)."""
        with self._cond:
            self._deque.append(ctx)
            self._cond.notify_all()

    def get(self, timeout_ms: int | None) -> EventContext | None:
        """Retrieve an event context.

        Parameters
        ----------
        timeout_ms :
            ``None`` blocks forever, ``0`` returns immediately if empty,
            and a positive value blocks up to that many milliseconds.

        Returns
        -------
        EventContext or None
            The retrieved context, or ``None`` if the queue was empty.

        """
        if timeout_ms is None:
            with self._cond:
                while not self._deque:
                    self._cond.wait()
                return self._deque.popleft()
        if timeout_ms == 0:
            with self._cond:
                if self._deque:
                    return self._deque.popleft()
                return None
        deadline = time.time() + timeout_ms / 1000.0
        with self._cond:
            while not self._deque:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
            return self._deque.popleft()

    def get_matching(
        self,
        event_type: constants.EventType | None,
        timeout_ms: int | None,
    ) -> EventContext | None:
        """Retrieve the first event matching *event_type*.

        If *event_type* is ``None``, matches any event.
        ``timeout_ms`` semantics are the same as :meth:`get`.
        """
        if timeout_ms is None:
            with self._cond:
                while True:
                    for idx, ctx in enumerate(self._deque):
                        if event_type is None or ctx.event_type == event_type:
                            del self._deque[idx]
                            return ctx
                    self._cond.wait()
        if timeout_ms == 0:
            with self._cond:
                for idx, ctx in enumerate(self._deque):
                    if event_type is None or ctx.event_type == event_type:
                        del self._deque[idx]
                        return ctx
            return None
        deadline = time.time() + timeout_ms / 1000.0
        with self._cond:
            while True:
                for idx, ctx in enumerate(self._deque):
                    if event_type is None or ctx.event_type == event_type:
                        del self._deque[idx]
                        return ctx
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def discard_all(self, event_type: constants.EventType | None = None) -> None:
        """Remove items from the queue.

        If *event_type* is ``None``, the entire queue is cleared.
        Otherwise only contexts whose ``event_type`` matches are removed.

        """
        with self._cond:
            if event_type is None:
                self._deque.clear()
            else:
                kept = [ctx for ctx in self._deque if ctx.event_type != event_type]
                self._deque.clear()
                self._deque.extend(kept)


# HandlerCallback: callable invoked when a VISA event fires.
#   Arg 0 (Any):                  session handle (vi)
#   Arg 1 (constants.EventType):  the event type that fired
#   Arg 2 (int):                  event context id
#   Arg 3 (Any):                  user-supplied handle passed at install_handler time
HandlerCallback = Callable[[Any, constants.EventType, int, Any], None]


class HandlerRegistry:
    """Thread-safe registry of user-installed event handlers."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # event_type -> list of (handler, user_handle)
        self._handlers: collections.defaultdict[
            constants.EventType, list[tuple[HandlerCallback, Any]]
        ] = collections.defaultdict(list)

    def install(
        self,
        event_type: constants.EventType,
        handler: HandlerCallback,
        user_handle: Any,
    ) -> None:
        """Register a handler for the given event type."""
        with self._lock:
            self._handlers[event_type].append((handler, user_handle))

    def uninstall(
        self,
        event_type: constants.EventType,
        handler: HandlerCallback,
        user_handle: Any = None,
    ) -> bool:
        """Remove a previously installed handler.

        If *user_handle* is ``None``, the first entry matching *handler*
        identity is removed regardless of its user handle.

        Returns ``True`` if a handler was removed, ``False`` otherwise.

        """
        with self._lock:
            entries = self._handlers.get(event_type, [])
            for idx, (h, uh) in enumerate(entries):
                if h is handler and (user_handle is None or uh == user_handle):
                    entries.pop(idx)
                    return True
            return False

    def fire(
        self,
        event_type: constants.EventType,
        session: Any,
        context_id: int,
    ) -> None:
        """Invoke all handlers registered for *event_type*.

        Each handler is called as ``handler(session, event_type, context_id,
        user_handle)`` where *user_handle* is the value supplied at
        installation.  Exceptions raised by a handler are warned via
        ``warnings.warn`` and do not prevent subsequent handlers from running.

        """
        with self._lock:
            handlers = list(self._handlers.get(event_type, []))

        for handler, user_handle in handlers:
            try:
                handler(session, event_type, context_id, user_handle)
            except Exception as exc:
                warnings.warn(
                    f"Event handler {handler!r} raised an exception: {exc!r}",
                    stacklevel=2,
                )


class EventState:
    """Per-session container for event enablement, queuing, and handlers."""

    def __init__(self) -> None:
        # {event_type: EventMechanism}
        self._lock = threading.RLock()
        self.enabled: dict[constants.EventType, EventMechanism] = {}
        self.queue = EventQueue()
        self.registry = HandlerRegistry()
        self.monitor_thread: threading.Thread | None = None
        self.stop_flag: threading.Event = threading.Event()

    def enable(
        self,
        event_type: constants.EventType,
        mechanism: constants.EventMechanism,
    ) -> None:
        """Enable delivery of *event_type* via *mechanism*."""
        m = EventMechanism(int(mechanism))
        with self._lock:
            if m is EventMechanism.ALL:
                self.enabled[event_type] = (
                    EventMechanism.QUEUE
                    | EventMechanism.HANDLER
                    | EventMechanism.SUSPEND
                )
            else:
                self.enabled[event_type] = (
                    self.enabled.get(event_type, EventMechanism.NONE) | m
                )

    def disable(
        self,
        event_type: constants.EventType,
        mechanism: constants.EventMechanism,
    ) -> None:
        """Disable delivery of *event_type* via *mechanism*."""
        m = EventMechanism(int(mechanism))
        with self._lock:
            if event_type not in self.enabled:
                return
            if m is EventMechanism.ALL:
                del self.enabled[event_type]
            else:
                new = self.enabled[event_type] & ~m
                if new is EventMechanism.NONE:
                    del self.enabled[event_type]
                else:
                    self.enabled[event_type] = new

    def is_queue_enabled(self, event_type: constants.EventType) -> bool:
        """Return whether queue delivery is enabled for *event_type*."""
        with self._lock:
            return bool(
                self.enabled.get(event_type, EventMechanism.NONE) & EventMechanism.QUEUE
            )

    def is_handler_enabled(self, event_type: constants.EventType) -> bool:
        """Return whether handler (callback) delivery is enabled for *event_type*."""
        with self._lock:
            return (
                self.enabled.get(event_type, EventMechanism.NONE)
                & EventMechanism.HANDLER
            ) is not EventMechanism.NONE

    def get_delivery_mechanisms(
        self, event_type: constants.EventType
    ) -> tuple[bool, bool]:
        """Return (queue_enabled, handler_enabled) for *event_type*.

        The check is performed atomically under the state lock.
        """
        with self._lock:
            mech = self.enabled.get(event_type, EventMechanism.NONE)
            return (
                bool(mech & EventMechanism.QUEUE),
                bool(mech & EventMechanism.HANDLER),
            )

    def any_enabled(self) -> bool:
        """Return ``True`` if any event type has any mechanism enabled."""
        with self._lock:
            return any(m is not EventMechanism.NONE for m in self.enabled.values())

    def should_monitor(self) -> bool:
        """Convenience alias for :meth:`any_enabled`."""
        return self.any_enabled()
