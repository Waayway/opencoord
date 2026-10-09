"""The app's logic without Dear PyGui: owns the link, traces, scanner and settings.

The views call the intent methods (``connect``, ``start``, ``set_range`` ...) and render
``controller.state`` (:class:`opencoord.ui.state.AppState`); the frame loop calls ``tick()`` once
per frame. Everything here runs on the UI thread except ``Link.open()`` / ``Link.close()``, which
block (up to 5 s) and therefore run on short-lived worker threads that report back through a
queue drained by ``tick()``.

Two acquisition modes:

* **Live**: the device is tuned to the chosen range (clamped by the device) and every sweep is
  folded into the ``TraceSet`` and the waterfall.
* **Scan**: a ``SegmentedScanner`` covers the range at the chosen resolution; its partial stitched
  trace is shown while it runs, and the finished scan is folded into the ``TraceSet`` (so repeated
  scans accumulate max hold) and the waterfall. While a scan runs it owns ``link.sweeps``.

When idle the device is put on hold and queued sweeps are discarded.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from opencoord.core import presets
from opencoord.core.presets import RangePreset
from opencoord.core.settings import WATERFALL_DEPTH_MAX, WATERFALL_DEPTH_MIN, AppSettings
from opencoord.core.types import DeviceConfig, Sweep
from opencoord.device.link import SerialPort, find_ports
from opencoord.device.link_api import Link
from opencoord.device.scanner import Resolution, SegmentedScanner
from opencoord.ui.state import AppState, Mode, WaterfallHistory

log = logging.getLogger(__name__)

LinkFactory = Callable[[str | None], Link]
PortLister = Callable[[], list[SerialPort]]

_MHZ = 1_000_000
#: Accept the current config if the device has not confirmed a retune within this time.
LIVE_CONFIRM_TIMEOUT_S = 3.0
_RATE_WINDOW_S = 1.0


@dataclass(frozen=True)
class _Opened:
    link: Link


@dataclass(frozen=True)
class _OpenFailed:
    link: Link
    message: str


@dataclass(frozen=True)
class _Closed:
    link: Link


_Result = _Opened | _OpenFailed | _Closed
#: How long ``shutdown()`` waits for background closes still running.
_SHUTDOWN_JOIN_S = 3.0
_DISCONNECTING = "Disconnecting..."


def _mhz(hz: float) -> str:
    return f"{hz / _MHZ:.3f}"


class Controller:
    def __init__(
        self,
        link_factory: LinkFactory,
        *,
        settings: AppSettings | None = None,
        port_lister: PortLister = find_ports,
        simulator: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        s = settings or AppSettings()
        self._settings = s
        self._factory = link_factory
        self._port_lister = port_lister
        self._clock = clock
        self._link: Link | None = None
        self._connecting: Link | None = None
        #: Links that may still hold their port: being closed, or abandoned while opening (closed
        #: as soon as their open() returns). No new connect starts until this is empty, because
        #: serial ports are opened exclusively.
        self._closing: set[Link] = set()
        self._close_threads: list[threading.Thread] = []
        self._shutting_down = threading.Event()
        self._results: queue.Queue[_Result] = queue.Queue()
        self._scanner: SegmentedScanner | None = None
        self._live_awaiting: DeviceConfig | None = None
        self._live_requested_at = 0.0
        #: Port connected to in this session (``None`` until then, and for the simulator).
        self._connected_port: str | None = None
        self._rate_count = 0
        self._rate_since = clock()

        depth = min(max(s.waterfall_depth, WATERFALL_DEPTH_MIN), WATERFALL_DEPTH_MAX)
        st = AppState(simulator=simulator, waterfall=WaterfallHistory(depth))
        st.auto_connect = s.auto_connect
        st.mode = "scan" if s.mode == "scan" else "live"
        st.resolution = Resolution(s.resolution)
        st.start_hz, st.stop_hz = s.start_hz, s.stop_hz
        preset = presets.find(s.preset, None) if s.preset else None
        if preset is not None:
            st.preset = preset.name
            st.start_hz, st.stop_hz = preset.start_hz, preset.stop_hz
        st.view_range_hz = (st.start_hz, st.stop_hz)
        self.state = st

    # --- helpers ------------------------------------------------------------------------------

    def _changed(self) -> None:
        self.state.ui_version += 1

    def _say(self, message: str) -> None:
        self.state.message = message
        self._changed()

    def available_presets(self) -> list[RangePreset]:
        return presets.available(self.state.device_range_hz)

    def current_settings(self, base: AppSettings) -> AppSettings:
        """``base`` updated with what the user chose in this session (window size excluded)."""
        st = self.state
        return replace(
            base,
            last_port=self._connected_port or base.last_port,
            auto_connect=st.auto_connect,
            preset=st.preset,
            resolution=st.resolution.value,
            start_hz=st.start_hz,
            stop_hz=st.stop_hz,
            waterfall_depth=st.waterfall.depth,
            mode=st.mode,
        )

    # --- connection intents -------------------------------------------------------------------

    def startup(self) -> None:
        """List ports; connect the simulator, or the last port if auto-connect is on."""
        self.refresh_ports()
        if self.state.simulator:
            self.connect()
        elif self.state.auto_connect and self._settings.last_port:
            self.connect(self._settings.last_port)

    def refresh_ports(self) -> None:
        try:
            self.state.ports = self._port_lister()
        except Exception as exc:  # pyserial raises assorted OS errors here
            log.warning("could not list serial ports", exc_info=True)
            self.state.ports = []
            self.state.error = f"Could not list serial ports: {exc}"
        self._changed()

    def connect(self, port: str | None = None) -> None:
        """Open a link to ``port`` (``None`` = auto-detect) on a worker thread."""
        st = self.state
        if self._closing:
            self._say("Waiting for the previous connection to close...")
            return
        if st.connection != "disconnected":
            return
        link = self._factory(port)
        self._connecting = link
        st.connection = "connecting"
        st.port = port
        st.error = None
        target = "the simulator" if st.simulator else port or "an RF Explorer (auto-detect)"
        self._say(f"Connecting to {target}...")
        threading.Thread(
            target=self._open_worker, args=(link,), name="link-open", daemon=True
        ).start()

    def _open_worker(self, link: Link) -> None:
        try:
            link.open()
        except Exception as exc:  # ConnectionError carries the user-facing message
            self._results.put(_OpenFailed(link, str(exc) or type(exc).__name__))
            return
        if self._shutting_down.is_set():  # the app quit while we were opening
            _close_quietly(link)
            return
        self._results.put(_Opened(link))

    def _close_later(self, link: Link) -> None:
        """Close ``link`` on a worker thread; ``tick()`` sees a ``_Closed`` when it is done."""
        self._closing.add(link)

        def close() -> None:
            _close_quietly(link)
            self._results.put(_Closed(link))

        thread = threading.Thread(target=close, name="link-close", daemon=True)
        self._close_threads = [t for t in self._close_threads if t.is_alive()] + [thread]
        thread.start()

    def disconnect(self) -> None:
        st = self.state
        link, self._link = self._link, None
        if self._connecting is not None:  # still opening: closed as soon as open() returns
            self._closing.add(self._connecting)
            self._connecting = None
        if link is not None:
            self._close_later(link)
        self._scanner = None
        self._live_awaiting = None
        st.running = st.stopping = False
        st.scan_partial = st.scan_progress = None
        st.model = st.capabilities = st.config = None
        st.connection = "disconnecting" if self._closing else "disconnected"
        st.sweeps_per_s = 0.0
        st.trace_version += 1
        self._update_estimate()
        self._say(_DISCONNECTING if self._closing else "Disconnected")

    def set_auto_connect(self, on: bool) -> None:
        self.state.auto_connect = on
        self._changed()

    # --- acquisition intents ------------------------------------------------------------------

    def set_mode(self, mode: Mode) -> None:
        if mode == self.state.mode:
            return
        if self.state.running:
            self.stop()
        self.state.mode = mode
        self._update_view_range()
        self._changed()

    def toggle(self) -> None:
        if self.state.busy:
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        st = self.state
        link = self._link
        if link is None or st.connection != "connected":
            self._say("Connect a device first")
            return
        if st.busy:
            return
        if st.mode == "live":
            self._start_live(link)
        else:
            self._start_scan(link)

    def stop(self) -> None:
        st = self.state
        if not st.running:
            return
        st.running = False
        if self._scanner is not None:
            self._scanner.cancel()
            st.stopping = True
            self._say("Stopping the scan...")
        else:
            self._live_awaiting = None
            self._hold()
            self._say("Stopped")

    def reset_max_hold(self) -> None:
        self.state.traces.reset()
        self.state.trace_version += 1

    def set_waterfall_depth(self, depth: int) -> None:
        depth = min(max(depth, WATERFALL_DEPTH_MIN), WATERFALL_DEPTH_MAX)
        self.state.waterfall.set_depth(depth)
        self._changed()

    # --- range intents ------------------------------------------------------------------------

    def set_range(self, start_hz: int, stop_hz: int, *, preset: str | None = None) -> None:
        st = self.state
        if stop_hz <= start_hz:
            self._say("Stop must be greater than start")
            return
        st.start_hz, st.stop_hz = int(start_hz), int(stop_hz)
        st.preset = preset
        self._update_estimate()
        if st.running and st.mode == "live" and self._link is not None:
            self._tune_live(self._link)
        elif st.running:
            self._say("The new range applies to the next scan")
        if not st.busy:
            self._update_view_range()
        self._changed()

    def set_center_span(self, center_hz: int, span_hz: int) -> None:
        half = span_hz // 2
        self.set_range(center_hz - half, center_hz - half + span_hz)

    def set_preset(self, name: str) -> None:
        preset = presets.find(name, self.state.device_range_hz)
        if preset is None:
            self._say(f"{name} is not available on this device")
            return
        self.set_range(preset.start_hz, preset.stop_hz, preset=preset.name)

    def set_resolution(self, resolution: Resolution) -> None:
        self.state.resolution = Resolution(resolution)
        self._update_estimate()
        self._changed()

    # --- frame tick ---------------------------------------------------------------------------

    def tick(self, now: float | None = None) -> None:
        """Advance everything once; call every frame. Never blocks."""
        now = self._clock() if now is None else now
        self._drain_results()
        link = self._link
        if link is None:
            return
        self._drain_events(link)
        if self._link is None:
            return
        self._sync_device(link)
        if self._scanner is not None:
            self._step_scan(link, self._scanner)
        elif self.state.running and self.state.mode == "live":
            self._step_live(link, now)
        else:
            _drain(link)
        self._update_rate(now)

    def shutdown(self) -> None:
        """Close the link synchronously (app exit).

        A connect still in progress closes its own link once ``open()`` returns; background
        closes are given a few seconds to finish.
        """
        self._shutting_down.set()
        self._connecting = None
        link, self._link = self._link, None
        if link is not None:
            _close_quietly(link)
        deadline = time.monotonic() + _SHUTDOWN_JOIN_S
        for thread in self._close_threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

    # --- tick internals -----------------------------------------------------------------------

    def _drain_results(self) -> None:
        while True:
            try:
                result = self._results.get_nowait()
            except queue.Empty:
                return
            st = self.state
            if isinstance(result, _Closed):
                self._closing.discard(result.link)
                self._closed_one()
                continue
            if result.link is not self._connecting:  # abandoned by disconnect() while opening
                if isinstance(result, _Opened):
                    self._close_later(result.link)
                else:
                    self._closing.discard(result.link)
                    self._closed_one()
                continue
            self._connecting = None
            if isinstance(result, _OpenFailed):
                st.connection = "disconnected"
                st.error = result.message
                self._say(result.message)
                continue
            link = result.link
            self._link = link
            st.connection = "connected"
            st.model, st.capabilities, st.config = link.model, link.capabilities, link.config
            if not st.simulator:
                active = getattr(link, "active_port", None)
                self._connected_port = active[0] if active else st.port
            self._hold()
            _drain(link)
            if st.preset is not None and presets.find(st.preset, st.device_range_hz) is None:
                st.preset = None  # this device cannot tune it; keep the range as a custom one
            self._update_estimate()
            caps = st.capabilities
            self._say(f"Connected: {caps.name if caps else 'device'}")

    def _closed_one(self) -> None:
        st = self.state
        if not self._closing and st.connection == "disconnecting":
            st.connection = "disconnected"
            if st.message == _DISCONNECTING:
                st.message = "Disconnected"
            self._changed()

    def _drain_events(self, link: Link) -> None:
        st = self.state
        while True:
            try:
                ev = link.events.get_nowait()
            except queue.Empty:
                break
            if ev.kind == "error":
                st.error = ev.message
            elif ev.kind == "disconnected":
                st.connection = "reconnecting"
            elif ev.kind == "connected" and st.connection == "reconnecting":
                st.connection = "connected"
                if st.running and st.mode == "live":
                    self._tune_live(link)
            self._say(ev.message)
        if not link.is_open:
            self.disconnect()
            st.error = st.error or "The device link closed"
            self._say("Device disconnected")

    def _sync_device(self, link: Link) -> None:
        st = self.state
        config, caps = link.config, link.capabilities
        if config is not st.config or caps is not st.capabilities:
            st.config, st.capabilities = config, caps
            self._changed()

    def _hold(self) -> None:
        if self._link is None:
            return
        with contextlib.suppress(NotImplementedError, RuntimeError):
            self._link.hold()

    def _start_live(self, link: Link) -> None:
        st = self.state
        self._tune_live(link)
        st.running = True
        self._say(f"Tuning to {_mhz(st.start_hz)}-{_mhz(st.stop_hz)} MHz...")

    def _tune_live(self, link: Link) -> None:
        st = self.state
        self._live_awaiting = link.config
        self._live_requested_at = self._clock()
        try:
            link.set_span(st.start_hz, st.stop_hz)
        except NotImplementedError:
            self._live_awaiting = None  # a replay: take its sweeps as they come
        except (RuntimeError, ValueError) as exc:
            self._live_awaiting = None
            st.error = str(exc)
        _drain(link)

    def _step_live(self, link: Link, now: float) -> None:
        st = self.state
        sweeps = _drain(link)
        config = link.config
        if config is None:
            return
        if self._live_awaiting is not None:
            confirmed = config is not self._live_awaiting
            if not confirmed and now - self._live_requested_at < LIVE_CONFIRM_TIMEOUT_S:
                return  # these sweeps predate the retune
            if not confirmed:
                log.warning("device did not confirm the live span; using its current span")
            self._live_awaiting = None
            st.view_range_hz = (config.start_hz, config.stop_hz)
            clamped = config.stop_hz < st.stop_hz - config.step_hz or config.start_hz > st.start_hz
            note = " (the device's max span)" if clamped else ""
            self._say(f"Live: {_mhz(config.start_hz)}-{_mhz(config.stop_hz)} MHz{note}")
        tol = max(config.step_hz, 1000)
        fresh = 0
        for sweep in sweeps:
            if (
                len(sweep.dbm) != config.sweep_points
                or abs(sweep.start_hz - config.start_hz) > tol
                or abs(sweep.stop_hz - config.stop_hz) > tol
            ):
                continue
            try:
                st.traces.update(sweep)
            except ValueError:
                log.debug("skipping a sweep with non-finite levels")
                continue
            live = st.traces.live
            assert live is not None
            st.waterfall.push(live)
            fresh += 1
        if fresh:
            live = st.traces.live
            assert live is not None
            if st.view_range_hz != (live.start_hz, live.stop_hz):
                st.view_range_hz = (live.start_hz, live.stop_hz)
                self._changed()
            self._rate_count += fresh
            st.trace_version += 1

    def _start_scan(self, link: Link) -> None:
        st = self.state
        try:
            scanner = SegmentedScanner(link, st.start_hz, st.stop_hz, st.resolution)
        except (RuntimeError, ValueError) as exc:
            self._say(f"Cannot scan: {exc}")
            return
        self._scanner = scanner
        st.running = True
        st.scan_partial = None
        st.scan_progress = None
        st.view_range_hz = scanner.range_hz
        lo, hi = scanner.range_hz
        self._say(
            f"Scanning {_mhz(lo)}-{_mhz(hi)} MHz ({len(scanner.segments)} segments, "
            f"about {scanner.estimate_seconds():.0f} s)"
        )

    def _step_scan(self, link: Link, scanner: SegmentedScanner) -> None:
        st = self.state
        try:
            progress = scanner.step()
        except (RuntimeError, ValueError) as exc:
            log.warning("scan failed", exc_info=True)
            self._end_scan(f"Scan failed: {exc}")
            return
        st.scan_progress = progress
        if progress.partial is not st.scan_partial:
            st.scan_partial = progress.partial
            st.trace_version += 1
        if not progress.done:
            return
        result = scanner.result
        if result is not None:
            try:
                st.traces.update(Sweep(result.freqs_hz, result.dbm, time.time()))
            except ValueError as exc:
                self._end_scan(f"Scan failed: {exc}")
                return
            live = st.traces.live
            assert live is not None
            st.waterfall.push(live)
            self._rate_count += 1
            st.view_range_hz = (live.start_hz, live.stop_hz)
            self._end_scan(f"Scan done: {len(result.freqs_hz)} points")
        elif progress.stalled:
            message = f"Scan stalled at segment {progress.segment_index + 1}: the device stopped"
            st.error = message
            self._end_scan(message)
        else:
            self._end_scan("Scan stopped")

    def _end_scan(self, message: str) -> None:
        st = self.state
        self._scanner = None
        st.running = st.stopping = False
        st.scan_partial = None
        st.trace_version += 1
        self._hold()
        self._say(message)

    def _update_rate(self, now: float) -> None:
        elapsed = now - self._rate_since
        if elapsed >= _RATE_WINDOW_S:
            rate = self._rate_count / elapsed
            self._rate_count = 0
            self._rate_since = now
            if rate != self.state.sweeps_per_s:
                self.state.sweeps_per_s = rate
                self._changed()

    def _update_view_range(self) -> None:
        self.state.view_range_hz = (self.state.start_hz, self.state.stop_hz)

    def _update_estimate(self) -> None:
        st = self.state
        st.scan_estimate_s = None
        link = self._link
        if link is None or st.connection != "connected":
            return
        try:
            scanner = SegmentedScanner(link, st.start_hz, st.stop_hz, st.resolution)
        except (RuntimeError, ValueError):
            return
        st.scan_estimate_s = scanner.estimate_seconds()


def _drain(link: Link) -> list[Sweep]:
    out: list[Sweep] = []
    while True:
        try:
            out.append(link.sweeps.get_nowait())
        except queue.Empty:
            return out


def _close_quietly(link: Link) -> None:
    try:
        link.close()
    except Exception:
        log.warning("error closing the link", exc_info=True)


__all__ = ["LIVE_CONFIRM_TIMEOUT_S", "Controller", "LinkFactory", "PortLister"]
