"""The interface shared by the real serial link and the simulator (pure; no I/O)."""

from __future__ import annotations

import contextlib
import queue
from dataclasses import dataclass
from typing import Literal, Protocol, TypeVar

from opencoord.core.types import DeviceConfig, ModelInfo, Sweep
from opencoord.device import protocol
from opencoord.device.models import Capabilities

_T = TypeVar("_T")


def put_drop_oldest(q: queue.Queue[_T], item: _T) -> None:
    """Put ``item`` without blocking, dropping the oldest items while the queue is full."""
    while True:
        try:
            q.put_nowait(item)
            return
        except queue.Full:
            with contextlib.suppress(queue.Empty):
                q.get_nowait()


def check_sweep_points(points: int, caps: Capabilities) -> None:
    """Raise ``ValueError`` unless the device can be set to ``points`` points per sweep."""
    protocol.set_sweep_points(points)  # raises for counts the protocol cannot encode
    if points > caps.sweep_points_max:
        raise ValueError(f"{caps.name} supports at most {caps.sweep_points_max} sweep points")


@dataclass(frozen=True)
class LinkEvent:
    """Connection state change; ``message`` is meant to be shown to the user."""

    kind: Literal["connected", "disconnected", "error"]
    message: str


class Link(Protocol):
    """What the scanner and the UI need from a device connection.

    Contract (all implementations):

    - ``model``, ``config`` and ``capabilities`` are ``None`` until the device has reported them.
      ``config`` is replaced (never mutated) and changes **only when the device confirms**, which is
      asynchronous: callers must not assume ``config`` changed right after ``set_span`` returns.
    - ``open()`` blocks until the device has reported model and config (or a timeout, default 5 s),
      then emits a ``connected`` event. On failure it raises ``ConnectionError`` with the
      user-facing message and also emits an ``error`` event with the same message. After an
      automatic reconnect the link emits ``connected`` again (and ``disconnected`` on losing the
      device). Opening an already open link is a no-op.
    - ``set_span`` silently clamps to the capabilities (and to at least one step of span). It raises
      ``ValueError`` if ``start_hz >= stop_hz`` and ``RuntimeError`` if the link is not open. It
      also resumes sweeping after ``hold()``.
    - ``set_sweep_points(n)`` changes the number of points per sweep, keeping start and span; it is
      confirmed asynchronously like ``set_span`` (``config.sweep_points`` changes when the device
      confirms). It raises ``ValueError`` for a count the device cannot take (not encodable, or
      above ``capabilities.sweep_points_max``) and ``RuntimeError`` if the link is not open. On
      the real device more points also lower ``capabilities.max_span_hz`` (342.37 MHz at 512).
    - ``hold()`` pauses sweeping; ``switch_module(main)`` selects the main or expansion module.
    - Links that cannot retune (e.g. a replay of a recording) may raise ``NotImplementedError``
      from ``set_span``, ``hold`` and ``switch_module``.
    - ``sweeps`` and ``events`` are bounded queues that drop the oldest item when full. They are
      not cleared on ``close()``; consumers drain them.
    - A sweep may predate the latest ``set_span``. Every ``Sweep`` carries its own axis, so
      consumers must compare ``sweep.start_hz``/``stop_hz`` and the point count with what they
      asked for and discard mismatches.
    """

    sweeps: queue.Queue[Sweep]
    events: queue.Queue[LinkEvent]

    @property
    def model(self) -> ModelInfo | None: ...

    @property
    def config(self) -> DeviceConfig | None: ...

    @property
    def capabilities(self) -> Capabilities | None: ...

    @property
    def is_open(self) -> bool: ...

    def open(self) -> None: ...

    def close(self) -> None: ...

    def set_span(self, start_hz: int, stop_hz: int) -> None: ...

    def set_sweep_points(self, points: int) -> None: ...

    def hold(self) -> None: ...

    def switch_module(self, main: bool) -> None: ...
