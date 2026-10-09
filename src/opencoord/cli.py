"""Headless command line interface (``opencoord-cli``): info, sweep, scan and record."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

import numpy as np
import numpy.typing as npt

from opencoord import __version__
from opencoord.core.types import Sweep
from opencoord.device import link as serial_link
from opencoord.device.link import SerialLink
from opencoord.device.link_api import Link
from opencoord.device.scanner import Resolution, SegmentedScanner
from opencoord.device.simulator import SimulatedLink

EXIT_OK = 0
EXIT_DEVICE = 1
EXIT_USAGE = 2

_MHZ = 1_000_000
_CONFIRM_TIMEOUT_S = 5.0
_SWEEP_TIMEOUT_S = 10.0
_SCAN_POLL_S = 0.02
_SCAN_MIN_TIMEOUT_S = 30.0


class CliError(Exception):
    """A device/connection problem; the message is printed to the user (exit code 1)."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opencoord-cli", description=__doc__)
    parser.add_argument("--version", action="version", version=f"opencoord {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    parser.add_argument("--port", help="serial port (default: auto-detect an RF Explorer)")
    parser.add_argument("--simulator", action="store_true", help="use the built-in simulator")
    sub = parser.add_subparsers(dest="command")

    info = sub.add_parser("info", help="print model, firmware, capabilities and configuration")
    info.add_argument("--json", action="store_true", help="machine-readable output")

    sweep = sub.add_parser("sweep", help="print sweeps as MHz,dBm rows")
    sweep.add_argument("--start", type=float, required=True, metavar="MHZ")
    sweep.add_argument("--stop", type=float, required=True, metavar="MHZ")
    sweep.add_argument("--count", type=int, default=1, metavar="N", help="sweeps to take (1)")
    sweep.add_argument("--max-hold", action="store_true", help="output one max-hold table")
    sweep.add_argument("--csv", type=Path, metavar="FILE", help="write to FILE instead of stdout")

    scan = sub.add_parser("scan", help="segmented scan, printed as one stitched MHz,dBm table")
    scan.add_argument("--start", type=float, required=True, metavar="MHZ")
    scan.add_argument("--stop", type=float, required=True, metavar="MHZ")
    scan.add_argument(
        "--resolution", choices=[r.value for r in Resolution], default=Resolution.NORMAL.value
    )
    scan.add_argument("--csv", type=Path, metavar="FILE", help="write to FILE instead of stdout")

    record = sub.add_parser("record", help="record the raw device byte stream for a fixture")
    record.add_argument("--raw", type=Path, required=True, metavar="FILE")
    record.add_argument("--seconds", type=float, required=True, metavar="S")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    serial_factory: serial_link.SerialFactory = serial_link.open_serial,
    port_lister: serial_link.PortLister = serial_link.list_serial_ports,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return EXIT_USAGE
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING, format="%(name)s: %(message)s"
    )
    if args.command in ("sweep", "scan") and args.start >= args.stop:
        parser.error("--start must be less than --stop")
    if args.command == "sweep" and args.count < 1:
        parser.error("--count must be at least 1")
    if args.command == "record":
        if args.simulator:
            parser.error("record needs a real device: the simulator has no raw bytes")
        if args.seconds <= 0:
            parser.error("--seconds must be positive")

    def make_serial_link(raw_sink: Callable[[bytes], None] | None = None) -> SerialLink:
        return SerialLink(
            args.port, serial_factory=serial_factory, port_lister=port_lister, raw_sink=raw_sink
        )

    try:
        if args.command == "record":
            return _record(args, make_serial_link)
        link: Link = SimulatedLink() if args.simulator else make_serial_link()
        try:
            link.open()
            if args.command == "info":
                return _info(link, args.json)
            if args.command == "scan":
                return _scan(link, args)
            return _sweep(link, args)
        finally:
            link.close()
    except (ConnectionError, CliError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_DEVICE
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_DEVICE


# --- info ----------------------------------------------------------------------------------------


def _info_dict(link: Link) -> dict[str, Any]:
    model, config, caps = link.model, link.config, link.capabilities
    if model is None or config is None or caps is None:
        raise CliError("the device did not report its configuration")
    return {
        "model": caps.main_name,
        "expansion_model": caps.expansion_name,
        "active_module": caps.name,
        "firmware": model.firmware,
        "main_code": model.main_code,
        "expansion_code": model.expansion_code,
        "capabilities": dataclasses.asdict(caps),
        "config": {**dataclasses.asdict(config), "stop_hz": config.stop_hz},
    }


def _info(link: Link, as_json: bool) -> int:
    data = _info_dict(link)
    if as_json:
        print(json.dumps(data, indent=2))
        return EXIT_OK
    caps, cfg = data["capabilities"], data["config"]
    rbw = "n/a" if cfg["rbw_hz"] is None else f"{cfg['rbw_hz'] / 1000:g} kHz"
    print(f"Model:        {data['model']}")
    print(f"Expansion:    {data['expansion_model'] or 'none'}")
    print(f"Active:       {data['active_module']}")
    print(f"Firmware:     {data['firmware']}")
    print("Capabilities:")
    print(f"  range       {caps['min_hz'] / _MHZ:g} - {caps['max_hz'] / _MHZ:g} MHz")
    print(f"  max span    {caps['max_span_hz'] / _MHZ:g} MHz")
    print(f"  plus model  {'yes' if caps['is_plus'] else 'no'}")
    print(f"  max points  {caps['sweep_points_max']}")
    print("Configuration:")
    print(f"  span        {cfg['start_hz'] / _MHZ:.3f} - {cfg['stop_hz'] / _MHZ:.3f} MHz")
    print(f"  points      {cfg['sweep_points']} (step {cfg['step_hz']} Hz)")
    print(f"  RBW         {rbw}")
    print(f"  amplitude   {cfg['amp_bottom_dbm']:g} to {cfg['amp_top_dbm']:g} dBm")
    return EXIT_OK


# --- sweep ---------------------------------------------------------------------------------------


def _wait_for_span(link: Link, start_hz: int, stop_hz: int) -> None:
    """Wait until ``link.config`` reflects the requested span (allowing clamping and rounding)."""
    caps = link.capabilities
    assert caps is not None
    want_start, want_stop = max(start_hz, caps.min_hz), min(stop_hz, caps.max_hz)
    deadline = time.monotonic() + _CONFIRM_TIMEOUT_S
    while time.monotonic() < deadline:
        cfg = link.config
        if cfg is not None:
            tol = max(cfg.step_hz, 1000)
            if abs(cfg.start_hz - want_start) <= tol and abs(cfg.stop_hz - want_stop) <= tol:
                return
        time.sleep(0.02)
    raise CliError("the device did not confirm the requested span")


def _collect(link: Link, count: int) -> list[Sweep]:
    """The next ``count`` sweeps whose axis matches the current configuration."""
    sweeps: list[Sweep] = []
    deadline = time.monotonic() + _SWEEP_TIMEOUT_S
    while len(sweeps) < count:
        try:
            sweep = link.sweeps.get(timeout=max(deadline - time.monotonic(), 0.001))
        except Exception:
            raise CliError("no sweeps received from the device") from None
        cfg = link.config
        if (
            cfg is not None
            and len(sweep.dbm) == cfg.sweep_points
            and sweep.start_hz == cfg.start_hz
            and sweep.stop_hz == cfg.stop_hz
        ):
            sweeps.append(sweep)
            deadline = time.monotonic() + _SWEEP_TIMEOUT_S
        elif time.monotonic() > deadline:
            raise CliError("no sweeps received for the requested span")
    return sweeps


def _write_rows(out: IO[str], sweeps: list[Sweep], max_hold: bool) -> None:
    if max_hold:
        out.write("MHz,dBm\n")
        held = np.max(np.stack([s.dbm for s in sweeps]), axis=0)
        for f, d in zip(sweeps[0].freqs_hz, held, strict=True):
            out.write(f"{f / _MHZ:.6f},{d:.1f}\n")
        return
    multi = len(sweeps) > 1
    out.write("sweep,MHz,dBm\n" if multi else "MHz,dBm\n")
    for i, s in enumerate(sweeps):
        for f, d in zip(s.freqs_hz, s.dbm, strict=True):
            prefix = f"{i}," if multi else ""
            out.write(f"{prefix}{f / _MHZ:.6f},{d:.1f}\n")


def _sweep(link: Link, args: argparse.Namespace) -> int:
    original = link.config
    start_hz, stop_hz = round(args.start * _MHZ), round(args.stop * _MHZ)
    try:
        link.set_span(start_hz, stop_hz)
        _wait_for_span(link, start_hz, stop_hz)
        sweeps = _collect(link, args.count)
    finally:
        if original is not None and link.is_open:  # leave the device as we found it
            try:
                link.set_span(original.start_hz, original.stop_hz)
                _wait_for_span(link, original.start_hz, original.stop_hz)
            except Exception:  # best effort; do not mask the original error
                logging.getLogger(__name__).debug("could not restore span", exc_info=True)
    if args.csv is not None:
        with args.csv.open("w", newline="") as f:
            _write_rows(f, sweeps, args.max_hold)
    else:
        _write_rows(sys.stdout, sweeps, args.max_hold)
    return EXIT_OK


# --- scan ----------------------------------------------------------------------------------------


def _scan(link: Link, args: argparse.Namespace) -> int:
    start_hz, stop_hz = round(args.start * _MHZ), round(args.stop * _MHZ)
    scanner = SegmentedScanner(link, start_hz, stop_hz, Resolution(args.resolution))
    estimate = scanner.estimate_seconds()
    began = time.monotonic()
    deadline = began + max(3 * estimate, _SCAN_MIN_TIMEOUT_S)
    progress = scanner.step()
    while not progress.done:
        if time.monotonic() > deadline:
            scanner.cancel()
            _finish_restore(scanner)
            raise CliError(f"scan stalled at segment {progress.segment_index + 1}")
        time.sleep(_SCAN_POLL_S)
        progress = scanner.step()
    trace = scanner.result
    if progress.stalled or trace is None:
        raise CliError(f"scan stalled at segment {progress.segment_index + 1}")
    elapsed = time.monotonic() - began
    if args.csv is not None:
        with args.csv.open("w", newline="") as f:
            _write_trace(f, trace.freqs_hz, trace.dbm)
    else:
        _write_trace(sys.stdout, trace.freqs_hz, trace.dbm)
    bin_khz = float(np.median(np.diff(trace.freqs_hz))) / 1000 if len(trace.freqs_hz) > 1 else 0
    print(
        f"{args.resolution} scan {args.start:g}-{args.stop:g} MHz: {len(trace.dbm)} points, "
        f"{bin_khz:.1f} kHz bins, {progress.segment_count} segments, {elapsed:.1f} s "
        f"(estimated {estimate:.0f} s)",
        file=sys.stderr,
    )
    return EXIT_OK


def _finish_restore(scanner: SegmentedScanner) -> None:
    """Keep stepping a cancelled scan until the device has its old settings back (bounded)."""
    deadline = time.monotonic() + _CONFIRM_TIMEOUT_S
    while not scanner.step().done and time.monotonic() < deadline:
        time.sleep(_SCAN_POLL_S)


def _write_trace(
    out: IO[str], freqs_hz: npt.NDArray[np.float64], dbm: npt.NDArray[np.float32]
) -> None:
    out.write("MHz,dBm\n")
    for f, d in zip(freqs_hz, dbm, strict=True):
        out.write(f"{f / _MHZ:.6f},{d:.1f}\n")


# --- record --------------------------------------------------------------------------------------


def _record(args: argparse.Namespace, make_link: Callable[..., SerialLink]) -> int:
    raw_path: Path = args.raw
    started = datetime.now(UTC)
    total = 0
    with raw_path.open("wb") as raw:

        def sink(data: bytes) -> None:
            nonlocal total
            raw.write(data)
            total += len(data)

        link = make_link(sink)
        try:
            link.open()
            time.sleep(args.seconds)
            model, config, caps = link.model, link.config, link.capabilities
            active = link.active_port
        finally:
            link.close()
    if model is None or config is None or caps is None or active is None:
        raise CliError("the device did not report its configuration")
    sidecar = {
        "device": caps.main_name,
        "model_code": model.main_code,
        "expansion_code": model.expansion_code,
        "firmware": model.firmware,
        "port": active[0],
        "baud": active[1],
        "recorded": started.isoformat(timespec="seconds"),
        "duration_s": args.seconds,
        "bytes": total,
        "settings": {**dataclasses.asdict(config), "stop_hz": config.stop_hz},
        "opencoord_version": __version__,
    }
    json_path = raw_path.with_suffix(".json")
    json_path.write_text(json.dumps(sidecar, indent=2) + "\n")
    print(f"recorded {total} bytes to {raw_path} (sidecar {json_path})")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
