"""The interface shared by the real serial link and the simulator (pure; no I/O)."""

from __future__ import annotations

import queue
from dataclasses import dataclass
from typing import Literal, Protocol

from opencoord.core.types import DeviceConfig, ModelInfo, Sweep
from opencoord.device.models import Capabilities


@dataclass(frozen=True)
class LinkEvent:
    """Connection state change; ``message`` is meant to be shown to the user."""

    kind: Literal["connected", "disconnected", "error"]
    message: str


class Link(Protocol):
    """What the scanner and the UI need from a device connection.

    ``model``, ``config`` and ``capabilities`` are ``None`` until the device has reported them.
    ``config`` is replaced (never mutated) each time the device echoes a new ``#C2-F``.
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

    def hold(self) -> None: ...

    def switch_module(self, main: bool) -> None: ...
