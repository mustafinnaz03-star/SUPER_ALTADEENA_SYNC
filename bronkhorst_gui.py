"""Automatic Bronkhorst EL-FLOW Prestige discovery and experiment runner.

Discovery only uses ProPar reads. With control enabled, the operator can start
a validated plan or send manual setpoints from individual MFC cards.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import queue
import struct
import threading
import time
import tkinter as tk
from dataclasses import dataclass, replace
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

import propar
from serial.tools import list_ports


# FlowDDE parameter numbers, used by the EL-FLOW RS232/FLOW-BUS (ProPar) interface.
FMEASURE, FSETPOINT, CAPACITY, CAPACITY_UNIT = 205, 206, 21, 129
MODEL, SERIAL_NUMBER = 91, 92
FLUIDSET_INDEX, FLUID_NAME = 24, 25
INLET_PRESSURE, OUTLET_PRESSURE, FLUID_TEMPERATURE = 178, 179, 181
REFERENCE_PRESSURE = 246
ROLES = ("Unassigned", "H2", "Alkene", "Other")
MFC_MODES = ("Read flow", "Set flow")

# A busy COM port is an expected state during discovery (for example when
# FlowSuite is open). It is reported in the GUI rather than printed repeatedly.
logging.getLogger("pymodbus").setLevel(logging.CRITICAL)


@dataclass(frozen=True)
class DeviceInfo:
    serial_number: str
    model: str
    port: str
    slave_id: int
    capacity: float | None
    unit: str
    measured_flow: float | None = None
    fluid_name: str = "Unknown"
    fluidset_index: int | None = None
    inlet_pressure_bar_a: float | None = None
    outlet_pressure_bar_a: float | None = None
    fluid_temperature_c: float | None = None
    reference_pressure_bar_a: float | None = None
    setpoint_flow: float | None = None


@dataclass(frozen=True)
class ExperimentPoint:
    number: int
    total_flow: float
    h2_flow: float
    alkene_flow: float
    dwell_s: float


@dataclass
class ServiceCommand:
    name: str
    payload: dict[str, Any] | None = None
    done: threading.Event | None = None
    error: Exception | None = None


def registers_to_float(registers: list[int]) -> float:
    if len(registers) != 2:
        raise ValueError("Float requires two registers")
    return struct.unpack(">f", b"".join(register.to_bytes(2, "big") for register in registers))[0]


def float_to_registers(value: float) -> list[int]:
    if not math.isfinite(value) or value < 0:
        raise ValueError("Setpoint must be a finite non-negative number")
    raw = struct.pack(">f", value)
    return [int.from_bytes(raw[:2], "big"), int.from_bytes(raw[2:], "big")]


def registers_to_text(registers: list[int]) -> str:
    raw = b"".join(register.to_bytes(2, "big") for register in registers)
    return raw.decode("ascii", errors="ignore").replace("\x00", " ").strip()


def build_plan(start: float, stop: float, step: float, ratio: float, dwell_s: float) -> list[ExperimentPoint]:
    """Build an inclusive total-flow ramp at a constant H2:alkene ratio."""
    values = (start, stop, step, ratio, dwell_s)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("All plan values must be finite")
    if start < 0 or stop < start or step <= 0 or ratio <= 0 or dwell_s <= 0:
        raise ValueError("Require 0 ≤ start ≤ stop and positive step, ratio and dwell time")
    count = int(math.floor((stop - start) / step + 1e-9)) + 1
    totals = [round(start + index * step, 9) for index in range(count)]
    if not math.isclose(totals[-1], stop, abs_tol=1e-7):
        raise ValueError("(stop − start) must be divisible by step")
    return [
        ExperimentPoint(i + 1, total, total * ratio / (1 + ratio), total / (1 + ratio), dwell_s)
        for i, total in enumerate(totals)
    ]


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        self.raw = json.loads(path.read_text(encoding="utf-8"))
        defaults = self.raw.get("serial_defaults", {})
        self.serial = {
            "baudrate": int(defaults.get("baudrate", 38400)), "bytesize": int(defaults.get("bytesize", 8)),
            "parity": str(defaults.get("parity", "N")), "stopbits": int(defaults.get("stopbits", 1)),
            "timeout_s": float(defaults.get("timeout_s", 0.4)),
        }
        raw_discovery = self.raw.get("discovery", {})
        slave_ids = raw_discovery.get("slave_ids", list(range(1, 9)))
        if not isinstance(slave_ids, list) or not slave_ids or any(not isinstance(v, int) or not 1 <= v <= 247 for v in slave_ids):
            raise ValueError("discovery.slave_ids must be values 1..247")
        self.discovery = {
            "scan_interval_s": float(raw_discovery.get("scan_interval_s", 2.0)),
            # USB hubs and two simultaneously starting EL-FLOW instruments can
            # answer more slowly than a single direct interface.  Do not use a
            # sub-400-ms discovery timeout for ProPar.
            "probe_timeout_s": max(0.4, float(raw_discovery.get("probe_timeout_s", 0.5))),
            "ports": [str(v) for v in raw_discovery.get("ports", [])],
            "include_non_usb": bool(raw_discovery.get("include_non_usb", False)),
            "slave_ids": sorted(set(slave_ids)),
        }
        if not 0.5 <= self.discovery["scan_interval_s"] <= 120 or not 0.02 <= self.discovery["probe_timeout_s"] <= 5:
            raise ValueError("Invalid discovery timing")
        self.roles = {str(serial): str(role) for serial, role in self.raw.get("roles", {}).items() if str(role) in ROLES}
        self.device_modes = {
            str(serial): str(mode)
            for serial, mode in self.raw.get("device_modes", {}).items()
            if str(mode) in MFC_MODES
        }
        raw_profiles = self.raw.get("fluid_profiles", {})
        self.fluid_profiles: dict[str, dict[str, dict[str, Any]]] = {
            str(serial): {str(slot): dict(profile) for slot, profile in slots.items() if isinstance(profile, dict)}
            for serial, slots in raw_profiles.items() if isinstance(slots, dict)
        }

    def save_roles(self, roles: dict[str, str]) -> None:
        self.raw["roles"] = roles
        self.path.write_text(json.dumps(self.raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self.roles = dict(roles)

    def save_device_modes(self, device_modes: dict[str, str]) -> None:
        self.raw["device_modes"] = device_modes
        self.path.write_text(json.dumps(self.raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self.device_modes = dict(device_modes)

    def save_fluid_profile(self, serial_number: str, fluidset_index: int, profile: dict[str, Any]) -> None:
        self.fluid_profiles.setdefault(serial_number, {})[str(fluidset_index)] = dict(profile)
        self.raw["fluid_profiles"] = self.fluid_profiles
        self.path.write_text(json.dumps(self.raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


class SerialService(threading.Thread):
    """The only thread that opens COM ports and talks ProPar to EL-FLOW units."""

    def __init__(self, serial: dict[str, Any], discovery: dict[str, Any], events: queue.Queue[tuple[str, Any]]):
        super().__init__(daemon=True)
        self.serial, self.discovery, self.events = serial, discovery, events
        self.commands: queue.Queue[ServiceCommand] = queue.Queue()
        self.stopping = threading.Event()
        self.clients: dict[str, propar.instrument] = {}
        self.devices: dict[str, DeviceInfo] = {}
        self.blocked_ports_until: dict[str, float] = {}
        self.reported_port_errors: dict[str, str] = {}
        self.port_failures: dict[str, int] = {}
        self.missing_port_scans: dict[str, int] = {}
        # Ports are deliberately opt-in.  Listing USB COM devices is harmless;
        # opening every one of them is not, especially with two adapters on a hub.
        self.connected_ports: set[str] = set()

    def _log(self, message: str) -> None:
        self.events.put(("connection_log", f"{time.strftime('%H:%M:%S')}  {message}"))

    def _discard_client(self, port: str) -> None:
        """Close a failed client and clear ProPar's module-global port cache."""
        client = self.clients.pop(port, None)
        if client is not None:
            try:
                client.master.stop()
            except Exception:
                pass
        # propar.instrument reuses this global cache. Leaving a stopped master
        # here makes a reconnected USB adapter look permanently unavailable.
        propar._PROPAR_MASTERS.pop(port, None)

    def _client(self, port: str, timeout: float) -> propar.instrument:
        client = self.clients.get(port)
        if client is None:
            # EL-FLOW Prestige uses address 0x80 for a directly connected
            # local RS232 instrument; ProPar handles the serial framing.
            client = propar.instrument(port, address=0x80, baudrate=self.serial["baudrate"])
            self.clients[port] = client
        client.master.response_timeout = timeout
        return client

    @staticmethod
    def _read(client: propar.instrument, parameter: int, action: str) -> Any:
        value = client.readParameter(parameter)
        if value is None:
            raise IOError(f"{action}: no ProPar response")
        return value

    def _probe(self, client: propar.instrument, port: str) -> DeviceInfo:
        serial_number = str(self._read(client, SERIAL_NUMBER, "serial number")).strip()
        if not serial_number:
            raise ValueError("not a recognizable Bronkhorst serial number")
        model = str(self._read(client, MODEL, "model")).strip()
        measured = float(self._read(client, FMEASURE, "flow"))
        try:
            capacity = float(self._read(client, CAPACITY, "capacity"))
            unit = str(self._read(client, CAPACITY_UNIT, "capacity unit")).strip() or "device unit"
            if not math.isfinite(capacity) or capacity <= 0:
                capacity = None
        except Exception:
            capacity, unit = None, "device unit"
        # These are stored parameters of the active fluid set, not live pressure
        # readings.  They are nevertheless essential for interpreting a volume
        # flow in the unit selected on the instrument.
        try:
            fluid_name = str(self._read(client, FLUID_NAME, "fluid name")).strip() or "Unknown"
            fluidset_index = int(self._read(client, FLUIDSET_INDEX, "fluid-set index"))
            inlet = float(self._read(client, INLET_PRESSURE, "inlet pressure"))
            outlet = float(self._read(client, OUTLET_PRESSURE, "outlet pressure"))
            temperature = float(self._read(client, FLUID_TEMPERATURE, "fluid temperature"))
            reference = float(self._read(client, REFERENCE_PRESSURE, "reference pressure"))
        except Exception:
            fluid_name, fluidset_index = "Unknown", None
            inlet = outlet = temperature = reference = None
        return DeviceInfo(
            serial_number, model or "EL-FLOW Prestige", port, 0x80, capacity, unit, measured,
            fluid_name, fluidset_index, inlet, outlet, temperature, reference,
            setpoint_flow=self._optional_flow(client, FSETPOINT),
        )

    def _optional_flow(self, client: propar.instrument, parameter: int) -> float | None:
        try:
            value = float(self._read(client, parameter, "flow readback"))
            return value if math.isfinite(value) else None
        except Exception:
            return None

    def _candidate_ports(self) -> list[str]:
        return sorted(self.connected_ports)

    def _connect_port(self, port: str) -> None:
        available = {item.device for item in list_ports.comports()}
        if port not in available:
            raise ConnectionError(f"{port} is no longer present in Windows")
        self._log(f"CONNECT requested for {port}; opening ProPar at {self.serial['baudrate']} baud.")
        self.blocked_ports_until.pop(port, None)
        self.port_failures.pop(port, None)
        self._discard_client(port)
        self.connected_ports.add(port)
        self._scan()
        devices = [device for device in self.devices.values() if device.port == port]
        if not devices:
            self.connected_ports.discard(port)
            self._discard_client(port)
            raise ConnectionError(f"{port}: EL-FLOW did not answer the ProPar identification read")
        device = devices[0]
        self._log(f"CONNECTED {port}: serial {device.serial_number}; {device.model}; gas {device.fluid_name}.")

    def _disconnect_port(self, port: str) -> None:
        self.connected_ports.discard(port)
        self._discard_client(port)
        self.port_failures.pop(port, None)
        for serial_number, device in list(self.devices.items()):
            if device.port == port:
                self.devices.pop(serial_number, None)
        self.events.put(("devices", list(self.devices.values())))
        self._log(f"DISCONNECTED {port} by operator.")

    def _scan(self) -> None:
        # Keep a previously identified MFC through a few transient misses.
        # USB hubs can briefly reorder/reset FTDI ports while another adapter
        # is plugged in; dropping the row on the first miss is misleading.
        found: dict[str, DeviceInfo] = dict(self.devices)
        ports = self._candidate_ports()
        now = time.monotonic()
        active_ports = set(ports)
        for serial_number, device in list(found.items()):
            if device.port not in active_ports:
                misses = self.missing_port_scans.get(serial_number, 0) + 1
                self.missing_port_scans[serial_number] = misses
                if misses >= 3:
                    found.pop(serial_number, None)
            else:
                self.missing_port_scans.pop(serial_number, None)
        for port in ports:
            if now < self.blocked_ports_until.get(port, 0):
                continue
            try:
                client = self._client(port, self.discovery["probe_timeout_s"])
            except (PermissionError, OSError, ConnectionError) as exc:
                # Do not hammer a port owned by FlowSuite/a serial monitor or a
                # second program instance. Try again after a short cooldown.
                message = f"{port} is busy or access is denied: close FlowSuite/serial monitor or another copy of this program. Retrying in 10 s."
                self.blocked_ports_until[port] = now + 10
                if self.reported_port_errors.get(port) != message:
                    self.reported_port_errors[port] = message
                    self.events.put(("port_warning", message))
                    self._log(f"{port}: open failed — {exc}")
                continue
            if port in self.reported_port_errors:
                del self.reported_port_errors[port]
                self.events.put(("port_recovered", f"{port} is available again."))
            try:
                device = self._probe(client, port)
                # A verified response on this port replaces any stale device
                # record that referenced the same COM name.
                for serial_number, old_device in list(found.items()):
                    if old_device.port == port and serial_number != device.serial_number:
                        found.pop(serial_number, None)
                found[device.serial_number] = device
                self.port_failures.pop(port, None)
                if device.serial_number not in self.devices:
                    self._log(f"{port}: identification reply — serial {device.serial_number}, gas {device.fluid_name}.")
            except Exception as exc:
                failures = self.port_failures.get(port, 0) + 1
                self.port_failures[port] = failures
                if failures == 1:
                    self._log(f"{port}: ProPar identification failed — {type(exc).__name__}: {exc}")
                for serial_number, old_device in list(found.items()):
                    if old_device.port == port:
                        found[serial_number] = replace(old_device, measured_flow=None, setpoint_flow=None)
                # Recreate the serial object only after repeated failures.
                if failures >= 3:
                    self._log(f"{port}: {failures} identification failures; recreating the ProPar session.")
                    self._discard_client(port)
                continue
        for port in list(self.clients):
            if port not in ports:
                self._discard_client(port)
        self.devices = found
        self.events.put(("devices", list(found.values())))

    def _poll(self) -> None:
        refreshed: dict[str, DeviceInfo] = {}
        for serial_number, device in self.devices.items():
            try:
                client = self._client(device.port, self.serial["timeout_s"])
                flow = self._optional_flow(client, FMEASURE)
                setpoint = self._optional_flow(client, FSETPOINT)
                refreshed[serial_number] = replace(device, measured_flow=flow, setpoint_flow=setpoint)
            except Exception:
                refreshed[serial_number] = replace(device, measured_flow=None, setpoint_flow=None)
        self.devices = refreshed
        self.events.put(("devices", list(refreshed.values())))

    def _apply(self, setpoints: dict[str, float]) -> None:
        for serial_number, value in setpoints.items():
            device = self.devices.get(serial_number)
            if device is None:
                raise ConnectionError(f"Device {serial_number} is not connected")
            if device.capacity is None:
                raise ValueError(f"Capacity for {serial_number} could not be read; writes are blocked")
            if value > device.capacity + 1e-7:
                raise ValueError(f"{serial_number}: {value:g} exceeds {device.capacity:g} {device.unit}")
            if not self._client(device.port, self.serial["timeout_s"]).writeParameter(FSETPOINT, value):
                raise IOError(f"write setpoint to {serial_number} was not acknowledged")

    def _apply_fluid_conditions(self, conditions: dict[str, dict[str, float]]) -> None:
        """Write explicit conditions to the active fluid set, never sensor calibration constants."""
        for serial_number, values in conditions.items():
            device = self.devices.get(serial_number)
            if device is None:
                raise ConnectionError(f"Device {serial_number} is not connected")
            inlet, outlet = float(values["inlet"]), float(values["outlet"])
            temperature, reference = float(values["temperature"]), float(values["reference"])
            if not all(math.isfinite(value) for value in (inlet, outlet, temperature, reference)):
                raise ValueError("Fluid conditions must be finite values")
            if inlet <= 0 or outlet <= 0 or reference <= 0:
                raise ValueError("Pressures must be positive values in bar(a)")
            client = self._client(device.port, self.serial["timeout_s"])
            for parameter, value, label in (
                (INLET_PRESSURE, inlet, "inlet pressure"),
                (OUTLET_PRESSURE, outlet, "outlet pressure"),
                (FLUID_TEMPERATURE, temperature, "fluid temperature"),
                (REFERENCE_PRESSURE, reference, "volume reference pressure"),
            ):
                if not client.writeParameter(parameter, value):
                    raise IOError(f"{label} update for {serial_number} was not acknowledged")

    def _activate_fluidset(self, fluidsets: dict[str, int]) -> None:
        for serial_number, fluidset in fluidsets.items():
            device = self.devices.get(serial_number)
            if device is None:
                raise ConnectionError(f"Device {serial_number} is not connected")
            if not isinstance(fluidset, int) or not 0 <= fluidset <= 7:
                raise ValueError("Fluid-set slot must be an integer from 0 to 7")
            if not self._client(device.port, self.serial["timeout_s"]).writeParameter(FLUIDSET_INDEX, fluidset):
                raise IOError(f"fluid-set activation for {serial_number} was not acknowledged")

    def _handle(self, command: ServiceCommand) -> None:
        self._log(f"COMMAND {command.name}: {command.payload or {}}")
        try:
            if command.name == "set":
                self._apply(command.payload or {})
            elif command.name == "zero":
                self._apply({serial: 0.0 for serial in (command.payload or {})})
            elif command.name == "set_fluid_conditions":
                self._apply_fluid_conditions(command.payload or {})
            elif command.name == "activate_fluidset":
                self._activate_fluidset(command.payload or {})
            elif command.name == "connect_port":
                self._connect_port(str((command.payload or {}).get("port", "")))
            elif command.name == "disconnect_port":
                self._disconnect_port(str((command.payload or {}).get("port", "")))
            elif command.name == "stop":
                self.stopping.set()
            else:
                raise ValueError(f"Unknown command {command.name}")
            self._log(f"OK {command.name}")
        except Exception as exc:
            command.error = exc
            self._log(f"ERROR {command.name}: {exc}")
        finally:
            if command.done:
                command.done.set()

    def submit(self, name: str, payload: dict[str, Any] | None, timeout_s: float = 5) -> None:
        done = threading.Event()
        command = ServiceCommand(name, payload, done)
        self.commands.put(command)
        if not done.wait(timeout_s):
            raise TimeoutError(f"Timed out waiting for {name}")
        if command.error:
            raise command.error

    def run(self) -> None:
        next_scan = next_poll = 0.0
        try:
            while not self.stopping.is_set():
                now = time.monotonic()
                if now >= next_scan:
                    self._scan()
                    next_scan = now + self.discovery["scan_interval_s"]
                if now >= next_poll:
                    self._poll()
                    next_poll = now + 0.5
                try:
                    self._handle(self.commands.get(timeout=0.05))
                except queue.Empty:
                    pass
        finally:
            for port in list(self.clients):
                self._discard_client(port)

    def shutdown(self) -> None:
        self.commands.put(ServiceCommand("stop"))


class ExperimentRunner(threading.Thread):
    def __init__(self, service: SerialService, plan: list[ExperimentPoint], h2_serial: str, alkene_serial: str, events: queue.Queue[tuple[str, Any]]):
        super().__init__(daemon=True)
        self.service, self.plan, self.h2_serial, self.alkene_serial, self.events = service, plan, h2_serial, alkene_serial, events
        self.cancelled = threading.Event()

    def cancel(self) -> None:
        self.cancelled.set()

    def run(self) -> None:
        controlled = {self.h2_serial: 0.0, self.alkene_serial: 0.0}
        try:
            for point in self.plan:
                if self.cancelled.is_set():
                    raise InterruptedError("Stopped by operator")
                controlled = {self.h2_serial: point.h2_flow, self.alkene_serial: point.alkene_flow}
                self.service.submit("set", controlled)
                started = time.monotonic()
                self.events.put(("stage", (point, started)))
                while not self.cancelled.wait(0.2):
                    elapsed = time.monotonic() - started
                    self.events.put(("progress", (point.number, min(elapsed, point.dwell_s), point.dwell_s)))
                    if elapsed >= point.dwell_s:
                        break
                if self.cancelled.is_set():
                    raise InterruptedError("Stopped by operator")
            self.events.put(("finished", "Plan complete; setting both controlled channels to zero."))
        except Exception as exc:
            self.events.put(("runner_error", str(exc)))
        finally:
            try:
                self.service.submit("zero", controlled)
            except Exception as exc:
                self.events.put(("runner_error", f"Could not set channels to zero: {exc}"))


class ExperimentPlot(tk.Canvas):
    """Dependency-free step plot of the planned channels and live MFC readings."""

    def __init__(self, master: tk.Misc, height: int = 520):
        super().__init__(master, height=height, background="#ffffff", highlightthickness=1, highlightbackground="#b8c2cc")
        self.plan: list[ExperimentPoint] = []
        self.h2_serial: str | None = None
        self.alkene_serial: str | None = None
        self.devices: dict[str, DeviceInfo] = {}
        self.current_point: int | None = None
        self.bind("<Configure>", lambda _event: self.redraw())

    def set_data(self, plan: list[ExperimentPoint] | None, h2_serial: str | None, alkene_serial: str | None, devices: dict[str, DeviceInfo], current_point: int | None) -> None:
        self.plan = list(plan or [])
        self.h2_serial, self.alkene_serial = h2_serial, alkene_serial
        self.devices, self.current_point = dict(devices), current_point
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        width, height = max(self.winfo_width(), 420), max(self.winfo_height(), 220)
        if not self.plan:
            self.create_text(width / 2, height / 2, text="Validate an experiment plan to display setpoint profiles.", fill="#4b5563", font=("Segoe UI", 11))
            return
        left, right, top, bottom = 68, 24, 26, 48
        chart_width, chart_height = width - left - right, height - top - bottom
        maximum = max(max(point.h2_flow, point.alkene_flow) for point in self.plan)
        maximum = max(1.0, maximum * 1.12)
        self.create_text(left, 12, text="Flow, device unit/min", anchor="w", fill="#334155", font=("Segoe UI", 9))
        self.create_line(left, top, left, top + chart_height, fill="#64748b")
        self.create_line(left, top + chart_height, left + chart_width, top + chart_height, fill="#64748b")
        for index in range(5):
            value = maximum * index / 4
            y = top + chart_height - chart_height * index / 4
            self.create_line(left, y, left + chart_width, y, fill="#e5e7eb")
            self.create_text(left - 7, y, text=f"{value:g}", anchor="e", fill="#475569", font=("Segoe UI", 8))
        count = len(self.plan)
        total_time = sum(point.dwell_s for point in self.plan)
        x_for = lambda elapsed: left + chart_width * (elapsed / max(total_time, 1.0))
        y_for = lambda value: top + chart_height - chart_height * value / maximum
        for index in range(5):
            elapsed = total_time * index / 4
            x = x_for(elapsed)
            self.create_line(x, top + chart_height, x, top + chart_height + 4, fill="#64748b")
            self.create_text(x, top + chart_height + 16, text=f"{elapsed:g}", fill="#475569", font=("Segoe UI", 8))
        self.create_text(left + chart_width / 2, height - 10, text="Time, s", fill="#334155", font=("Segoe UI", 9))
        series = (("H₂ setpoint", "#2563eb", [point.h2_flow for point in self.plan]), ("Alkene setpoint", "#d97706", [point.alkene_flow for point in self.plan]))
        for series_index, (label, color, values) in enumerate(series):
            coordinates: list[float] = []
            elapsed = 0.0
            for point, value in zip(self.plan, values):
                coordinates.extend((x_for(elapsed), y_for(value), x_for(elapsed + point.dwell_s), y_for(value)))
                elapsed += point.dwell_s
            self.create_line(*coordinates, fill=color, width=2)
            legend_x = left + series_index * 145
            self.create_line(legend_x, 20, legend_x + 20, 20, fill=color, width=2)
            self.create_text(legend_x + 25, 20, text=label, anchor="w", fill="#334155", font=("Segoe UI", 9))
        if self.current_point is not None and 1 <= self.current_point <= count:
            elapsed = sum(point.dwell_s for point in self.plan[:self.current_point - 1])
            x = x_for(elapsed)
            self.create_line(x, top, x, top + chart_height, fill="#16a34a", dash=(3, 3))
            self.create_text(x + 4, top + 8, text=f"Current: {self.current_point}", anchor="w", fill="#166534", font=("Segoe UI", 9))
        readings: list[str] = []
        for serial_number, device in sorted(self.devices.items()):
            measured = "—" if device.measured_flow is None else f"{device.measured_flow:.4g} {device.unit}"
            readings.append(f"{serial_number}: measured {measured}")
        if readings:
            self.create_text(left, height - 28, text="   |   ".join(readings), anchor="w", fill="#0f766e", font=("Segoe UI", 9))


class ManualFlowCard(ttk.LabelFrame):
    """One persistent editor per serial number; polling never replaces typed input."""

    def __init__(self, master: tk.Misc, serial_number: str, set_flow: Any, zero_flow: Any):
        super().__init__(master, text=f"MFC {serial_number}", padding=12)
        self.flow_var = tk.StringVar(self, value="0")
        self.gas_var = tk.StringVar(self)
        self.unit_var = tk.StringVar(self)
        self.measured_var = tk.StringVar(self)
        self.setpoint_var = tk.StringVar(self)
        self.command_var = tk.StringVar(self, value="Команды из этого окна ещё не отправлялись")
        ttk.Label(self, textvariable=self.gas_var).pack(anchor="w")
        ttk.Label(self, textvariable=self.measured_var, font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(8, 4))
        ttk.Label(self, textvariable=self.setpoint_var).pack(anchor="w")
        row = ttk.Frame(self)
        row.pack(fill="x", pady=(10, 0))
        ttk.Label(row, text="Задать поток:").pack(side="left")
        self.entry = ttk.Entry(row, textvariable=self.flow_var, width=12)
        self.entry.pack(side="left", padx=8)
        self.entry.bind("<Return>", lambda _event: set_flow(serial_number))
        ttk.Label(row, textvariable=self.unit_var).pack(side="left", padx=(0, 12))
        self.set_button = ttk.Button(row, text="Установить", command=lambda: set_flow(serial_number))
        self.set_button.pack(side="left")
        self.zero_button = ttk.Button(row, text="Ноль", command=lambda: zero_flow(serial_number))
        self.zero_button.pack(side="left", padx=8)
        ttk.Label(self, textvariable=self.command_var, wraplength=720).pack(anchor="w", pady=(8, 0))

    def update_device(self, device: DeviceInfo, role: str, mode: str) -> None:
        self.configure(text=f"MFC {device.serial_number} · {device.port}")
        mode_label = "чтение потока" if mode == "Read flow" else "выставление потока"
        self.gas_var.set(f"Газ в приборе: {device.fluid_name}   |   Назначенная роль: {role}   |   Режим: {mode_label}")
        self.unit_var.set(device.unit)
        measured = "нет данных" if device.measured_flow is None else f"{device.measured_flow:.6g} {device.unit}"
        setpoint = "нет данных" if device.setpoint_flow is None else f"{device.setpoint_flow:.6g} {device.unit}"
        self.measured_var.set(f"Текущий поток: {measured}")
        self.setpoint_var.set(f"Уставка, прочитанная из прибора: {setpoint}")

    def set_enabled(self, enabled: bool) -> None:
        for widget in (self.entry, self.set_button, self.zero_button):
            widget.configure(state="normal" if enabled else "disabled")


class GasLineApp(tk.Tk):
    def __init__(self, store: ConfigStore):
        super().__init__()
        self.title("Bronkhorst gas line — auto discovery and experiments")
        self.minsize(920, 840)
        self.store, self.events = store, queue.Queue()
        self.devices: dict[str, DeviceInfo] = {}
        self.roles = dict(store.roles)
        self.device_modes = dict(store.device_modes)
        self.service = SerialService(store.serial, store.discovery, self.events)
        self.runner: ExperimentRunner | None = None
        self.control_enabled = tk.BooleanVar(value=False)
        self.operation_mode = tk.StringVar(value="automatic")
        self.operation_mode_hint = tk.StringVar()
        self.available_port_var = tk.StringVar(value="")
        self.manual_cards: dict[str, ManualFlowCard] = {}
        self.role_device_var, self.role_var = tk.StringVar(value="No device detected"), tk.StringVar(value="Unassigned")
        self.device_mode_var = tk.StringVar(value="Read flow")
        self.fluidset_var = tk.StringVar(value="")
        self.expected_gas_var = tk.StringVar(value="")
        self.inlet_pressure_var = tk.StringVar(value="")
        self.outlet_pressure_var = tk.StringVar(value="")
        self.fluid_temperature_var = tk.StringVar(value="")
        self.reference_pressure_var = tk.StringVar(value="")
        self.fluid_conditions_var = tk.StringVar(value="Select a detected device to inspect its active gas calibration.")
        self._conditions_snapshot: tuple[Any, ...] | None = None
        self.plan_vars = {key: tk.StringVar(value=value) for key, value in {"start": "10", "stop": "150", "step": "10", "ratio": "4", "dwell": "60"}.items()}
        self.status_var = tk.StringVar(value="Waiting for USB Bronkhorst devices…")
        self.port_warning_var = tk.StringVar(value="")
        self.plan_var = tk.StringVar(value="Enter plan values; capacities are checked before start.")
        self.progress_var = tk.StringVar(value="No experiment is running.")
        self.active_plan: list[ExperimentPoint] = []
        self.plan_h2_serial: str | None = None
        self.plan_alkene_serial: str | None = None
        self.current_plan_point: int | None = None
        self.graph_window: tk.Toplevel | None = None
        self.flow_plot: ExperimentPlot | None = None
        self.manual_setpoints: dict[str, float] = {}
        self.manual_commands_pending: set[str] = set()
        self._connection_log_lines: list[str] = []
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.service.start()
        self._refresh_available_ports()
        self.after(100, self._consume_events)

    def _build(self) -> None:
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Hardware shutdown valves and leak checks are mandatory. Discovery sends reads only.", foreground="#9b1c1c").pack(anchor="w", pady=(0, 8))
        self.notebook = ttk.Notebook(body)
        self.notebook.pack(fill="both", expand=True)
        connection_tab = ttk.Frame(self.notebook, padding=10)
        calibration_tab = ttk.Frame(self.notebook, padding=10)
        experiment_tab = ttk.Frame(self.notebook, padding=10)
        console_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(connection_tab, text="Подключение")
        self.notebook.add(calibration_tab, text="Параметры / калибровка")
        self.notebook.add(experiment_tab, text="Эксперимент")
        self.notebook.add(console_tab, text="Консоль")

        mode_frame = ttk.LabelFrame(experiment_tab, text="Operation mode", padding=8)
        mode_frame.pack(fill="x", pady=(12, 0))
        ttk.Radiobutton(
            mode_frame,
            text="Automatic — follow the validated H₂:alkene plan",
            variable=self.operation_mode,
            value="automatic",
            command=self._operation_mode_changed,
        ).pack(anchor="w")
        ttk.Radiobutton(
            mode_frame,
            text="Manual — individual flow controls for each connected MFC",
            variable=self.operation_mode,
            value="manual",
            command=self._operation_mode_changed,
        ).pack(anchor="w", pady=(4, 0))
        ttk.Label(mode_frame, textvariable=self.operation_mode_hint, foreground="#4b5563", wraplength=850).pack(anchor="w", pady=(6, 0))

        connect_frame = ttk.LabelFrame(connection_tab, text="Manual COM connection", padding=8)
        connect_frame.pack(fill="x", pady=(0, 10))
        ttk.Label(connect_frame, text="Available USB COM:").pack(side="left")
        self.available_port_box = ttk.Combobox(connect_frame, textvariable=self.available_port_var, state="readonly", width=14)
        self.available_port_box.pack(side="left", padx=(6, 8))
        ttk.Button(connect_frame, text="Refresh list", command=self._update_available_ports).pack(side="left")
        ttk.Button(connect_frame, text="Connect selected", command=self._connect_selected_port).pack(side="left", padx=8)
        ttk.Button(connect_frame, text="Disconnect selected", command=self._disconnect_selected_port).pack(side="left")

        devices_frame = ttk.LabelFrame(connection_tab, text="Automatically detected Bronkhorst devices", padding=8)
        devices_frame.pack(fill="x")
        columns = ("serial", "role", "mode", "port", "address", "model", "capacity", "measured")
        self.tree = ttk.Treeview(devices_frame, columns=columns, show="headings", height=6)
        for column, label in zip(columns, ("Serial number", "Gas role", "MFC mode", "COM", "Protocol", "Model", "Capacity", "Measured")):
            self.tree.heading(column, text=label)
            self.tree.column(column, width=125 if column in {"serial", "model"} else 95, stretch=True)
        self.tree.pack(fill="x")
        role_row = ttk.Frame(devices_frame)
        role_row.pack(fill="x", pady=(8, 0))
        ttk.Label(role_row, text="Detected device serial (auto-read):").pack(side="left")
        self.role_device_box = ttk.Combobox(role_row, textvariable=self.role_device_var, state="disabled", width=22)
        self.role_device_box.pack(side="left", padx=(6, 12))
        self.role_device_box.bind("<<ComboboxSelected>>", self._load_role)
        ttk.Label(role_row, text="Connected gas:").pack(side="left")
        ttk.Combobox(role_row, textvariable=self.role_var, values=ROLES, state="readonly", width=12).pack(side="left", padx=6)
        ttk.Button(role_row, text="Save gas role", command=self._save_role).pack(side="left")
        ttk.Label(role_row, text="MFC mode:").pack(side="left", padx=(18, 0))
        self.device_mode_box = ttk.Combobox(role_row, textvariable=self.device_mode_var, values=MFC_MODES, state="disabled", width=15)
        self.device_mode_box.pack(side="left", padx=6)
        ttk.Button(role_row, text="Save MFC mode", command=self._save_device_mode).pack(side="left")
        ttk.Label(connection_tab, text="Role 'Other' is for carrier/purge/monitoring gas: it remains connected and visible, but is excluded from the H₂:alkene experiment plan.", foreground="#4b5563", wraplength=860).pack(anchor="w", pady=(8, 0))
        ttk.Label(connection_tab, text="The list refresh does not open ports. Connect only the COM ports used by MFCs; a device is retained through three short hub misses.", foreground="#4b5563", wraplength=860).pack(anchor="w", pady=(12, 0))
        ttk.Label(connection_tab, text="Журнал подключения и команд доступен на вкладке «Консоль».").pack(anchor="w", pady=(12, 0))
        log_frame = ttk.LabelFrame(console_tab, text="Журнал событий", padding=6)
        log_frame.pack(fill="both", expand=True, pady=(8, 0))
        self.connection_log = tk.Text(log_frame, height=9, wrap="word", state="disabled", background="#111827", foreground="#d1fae5", insertbackground="#d1fae5", font=("Consolas", 9))
        log_scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.connection_log.yview)
        log_scrollbar.pack(side="right", fill="y")
        self.connection_log.configure(yscrollcommand=log_scrollbar.set)
        self.connection_log.pack(side="left", fill="both", expand=True)

        calibration_devices_frame = ttk.LabelFrame(calibration_tab, text="Calibration overview: all connected channels", padding=8)
        calibration_devices_frame.pack(fill="x")
        calibration_columns = ("serial", "port", "gas", "fluidset", "inlet", "outlet", "temperature", "reference", "capacity")
        self.calibration_tree = ttk.Treeview(calibration_devices_frame, columns=calibration_columns, show="headings", height=5)
        calibration_labels = ("Serial number", "COM", "Active gas", "Fluid set", "Inlet, bar(a)", "Outlet, bar(a)", "Temp, °C", "Reference, bar(a)", "Capacity")
        for column, label in zip(calibration_columns, calibration_labels):
            self.calibration_tree.heading(column, text=label)
            self.calibration_tree.column(column, width=105 if column not in {"serial", "gas"} else 135, stretch=True)
        self.calibration_tree.pack(fill="x")
        self.calibration_tree.bind("<<TreeviewSelect>>", self._select_calibration_device)
        ttk.Label(calibration_devices_frame, text="Click a row to select that MFC for the detailed fields below. All values are read from the device's active fluid set.", foreground="#4b5563").pack(anchor="w", pady=(6, 0))

        conditions_frame = ttk.LabelFrame(calibration_tab, text="Selected MFC: fluid-set parameters", padding=8)
        conditions_frame.pack(fill="x", pady=(12, 0))
        ttk.Label(conditions_frame, textvariable=self.fluid_conditions_var, wraplength=850).grid(row=0, column=0, columnspan=5, sticky="w")
        ttk.Label(conditions_frame, text="Fluid-set slot (0–7):").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Spinbox(conditions_frame, from_=0, to=7, textvariable=self.fluidset_var, width=5).grid(row=1, column=1, sticky="w", padx=(6, 12), pady=(8, 0))
        ttk.Label(conditions_frame, text="Expected gas:").grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Entry(conditions_frame, textvariable=self.expected_gas_var, width=16).grid(row=1, column=3, sticky="w", padx=(6, 12), pady=(8, 0))
        ttk.Button(conditions_frame, text="Activate selected fluid set", command=self._activate_fluidset).grid(row=1, column=4, sticky="w", pady=(8, 0))
        condition_fields = (("Inlet, bar(a)", self.inlet_pressure_var), ("Outlet, bar(a)", self.outlet_pressure_var), ("Fluid temp, °C", self.fluid_temperature_var), ("Volume reference, bar(a)", self.reference_pressure_var))
        for column, (label, variable) in enumerate(condition_fields):
            ttk.Label(conditions_frame, text=label).grid(row=2, column=column * 2, sticky="w", pady=(8, 0))
            ttk.Entry(conditions_frame, textvariable=variable, width=9).grid(row=2, column=column * 2 + 1, sticky="w", padx=(6, 12), pady=(8, 0))
        ttk.Button(conditions_frame, text="Save active fluid-set conditions", command=self._apply_fluid_conditions).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(conditions_frame, text="Stored fluid-set conditions, not live pressure/temperature measurements.", foreground="#6b4f00").grid(row=3, column=2, columnspan=6, sticky="w", pady=(8, 0))
        ttk.Label(calibration_tab, text="Choose only a fluid-set slot that was configured for the physically connected gas. Values below are stored fluid-model conditions, not live pressure or temperature sensor readings.", wraplength=860, foreground="#4b5563").pack(anchor="w", pady=(12, 0))

        self.plan_frame = ttk.LabelFrame(experiment_tab, text="Experiment: fixed H₂:alkene ratio, total-flow ramp", padding=8)
        self.plan_frame.pack(fill="x", pady=(12, 0))
        fields = (("Total from", "start"), ("to", "stop"), ("step", "step"), ("H₂ : alkene", "ratio"), ("Dwell / point, s", "dwell"))
        self.plan_input_widgets: list[ttk.Entry] = []
        for i, (label, key) in enumerate(fields):
            ttk.Label(self.plan_frame, text=label).grid(row=0, column=i * 2, sticky="w", padx=(0, 4))
            entry = ttk.Entry(self.plan_frame, textvariable=self.plan_vars[key], width=9)
            entry.grid(row=0, column=i * 2 + 1, sticky="w", padx=(0, 12))
            self.plan_input_widgets.append(entry)
        self.validate_plan_button = ttk.Button(self.plan_frame, text="Validate plan", command=self._validate_plan)
        self.validate_plan_button.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(self.plan_frame, textvariable=self.plan_var, wraplength=850).grid(row=1, column=2, columnspan=8, sticky="w", pady=(8, 0))

        self.plan_view_frame = ttk.LabelFrame(experiment_tab, text="Experiment schedule", padding=8)
        self.plan_view_frame.pack(fill="both", expand=True, pady=(12, 0))
        schedule_actions = ttk.Frame(self.plan_view_frame)
        schedule_actions.pack(fill="x", pady=(0, 8))
        ttk.Button(schedule_actions, text="Open graph in a separate window", command=self._open_graph_window).pack(side="left")
        ttk.Label(schedule_actions, text="The graph shows a stepped setpoint held for the selected dwell time.", foreground="#4b5563").pack(side="left", padx=12)
        schedule_body = ttk.Frame(self.plan_view_frame)
        schedule_body.pack(fill="both", expand=True)
        schedule_columns = ("point", "total", "h2", "alkene", "dwell")
        self.schedule_tree = ttk.Treeview(schedule_body, columns=schedule_columns, show="headings", height=12)
        for column, label in zip(schedule_columns, ("Point", "Total", "H₂ setpoint", "Alkene setpoint", "Dwell, s")):
            self.schedule_tree.heading(column, text=label)
            self.schedule_tree.column(column, width=125, stretch=True)
        schedule_scrollbar = ttk.Scrollbar(schedule_body, orient="vertical", command=self.schedule_tree.yview)
        schedule_scrollbar.pack(side="right", fill="y")
        self.schedule_tree.configure(yscrollcommand=schedule_scrollbar.set)
        self.schedule_tree.pack(side="left", fill="both", expand=True)

        self.manual_frame = ttk.LabelFrame(experiment_tab, text="Manual flow control", padding=8)
        self.manual_frame.pack(fill="both", expand=True, pady=(12, 0))
        self.manual_canvas = tk.Canvas(self.manual_frame, highlightthickness=0, height=260)
        manual_scrollbar = ttk.Scrollbar(self.manual_frame, orient="vertical", command=self.manual_canvas.yview)
        manual_scrollbar.pack(side="right", fill="y")
        self.manual_canvas.configure(yscrollcommand=manual_scrollbar.set)
        self.manual_canvas.pack(side="left", fill="both", expand=True)
        self.manual_cards_frame = ttk.Frame(self.manual_canvas)
        cards_window = self.manual_canvas.create_window((0, 0), window=self.manual_cards_frame, anchor="nw")
        self.manual_canvas.bind("<Configure>", lambda event: self.manual_canvas.itemconfigure(cards_window, width=event.width))
        self.manual_cards_frame.bind("<Configure>", lambda _event: self.manual_canvas.configure(scrollregion=self.manual_canvas.bbox("all")))
        self.manual_empty_label = ttk.Label(self.manual_cards_frame, text="Нет подключённых MFC. Подключите прибор на вкладке «Подключение».", padding=12)
        self.manual_empty_label.pack(anchor="w")
        self.auto_controls = ttk.Frame(experiment_tab)
        self.auto_controls.pack(fill="x", pady=8, before=self.plan_view_frame)
        self.enable_writing_check = ttk.Checkbutton(
            self.auto_controls,
            text="Enable writing setpoints",
            variable=self.control_enabled,
            command=self._toggle_control,
        )
        self.enable_writing_check.pack(side="left")
        self.start_experiment_button = ttk.Button(self.auto_controls, text="Start experiment", command=self._start_experiment)
        self.start_experiment_button.pack(side="left", padx=8)
        self.stop_experiment_button = ttk.Button(self.auto_controls, text="STOP and set H₂/alkene to zero", command=self._stop_experiment)
        self.stop_experiment_button.pack(side="left")
        self.progress_label = ttk.Label(experiment_tab, textvariable=self.progress_var)
        self.progress_label.pack(anchor="w")
        ttk.Label(body, textvariable=self.port_warning_var, foreground="#9b1c1c", wraplength=880).pack(anchor="w", pady=(8, 0))
        ttk.Label(body, textvariable=self.status_var, wraplength=880).pack(anchor="w", pady=(4, 0))
        self._update_operation_mode_controls()

    def _update_available_ports(self) -> None:
        ports = sorted(
            item.device for item in list_ports.comports()
            if self.store.discovery["include_non_usb"] or item.vid is not None
        )
        self.available_port_box["values"] = ports
        if self.available_port_var.get() not in ports:
            self.available_port_var.set(ports[0] if ports else "")

    def _refresh_available_ports(self) -> None:
        self._update_available_ports()
        if self.winfo_exists():
            self.after(2000, self._refresh_available_ports)

    def _operation_mode_changed(self) -> None:
        if self.operation_mode.get() == "manual" and self.runner and self.runner.is_alive():
            self.operation_mode.set("automatic")
            messagebox.showwarning("Experiment running", "Stop the automatic experiment before switching to manual control.")
        elif self.operation_mode.get() == "automatic" and (
            self.manual_commands_pending or any(flow > 0 for flow in self.manual_setpoints.values())
        ):
            self.operation_mode.set("manual")
            messagebox.showwarning(
                "Manual flow active",
                "Wait for the manual command to finish and set every manually controlled MFC to zero before switching modes.",
            )
        self._update_operation_mode_controls()

    def _update_operation_mode_controls(self) -> None:
        automatic = self.operation_mode.get() == "automatic"
        if automatic:
            self.manual_frame.pack_forget()
            if not self.start_experiment_button.winfo_manager():
                self.start_experiment_button.pack(side="left", padx=8)
            if not self.stop_experiment_button.winfo_manager():
                self.stop_experiment_button.pack(side="left")
            if not self.enable_writing_check.winfo_manager():
                self.enable_writing_check.pack(side="left", before=self.start_experiment_button)
            if not self.plan_frame.winfo_manager():
                self.plan_frame.pack(fill="x", pady=(12, 0), before=self.progress_label)
            if not self.plan_view_frame.winfo_manager():
                self.plan_view_frame.pack(fill="both", expand=True, pady=(12, 0), before=self.progress_label)
            self.auto_controls.pack_forget()
            self.auto_controls.pack(fill="x", pady=8, before=self.plan_view_frame)
        else:
            self.plan_frame.pack_forget()
            self.plan_view_frame.pack_forget()
            self.auto_controls.pack_forget()
            self.manual_frame.pack_forget()
            self.manual_frame.pack(fill="both", expand=True, pady=(12, 0), before=self.progress_label)
        for widget in self.plan_input_widgets:
            widget.configure(state="normal" if automatic else "disabled")
        for widget in (self.validate_plan_button, self.start_experiment_button, self.stop_experiment_button):
            widget.configure(state="normal" if automatic else "disabled")

        for serial_number, card in self.manual_cards.items():
            card.set_enabled(
                not automatic
                and self.device_modes.get(serial_number, "Read flow") == "Set flow"
                and serial_number not in self.manual_commands_pending
            )
        if automatic:
            self.operation_mode_hint.set("The controller will apply every validated point in sequence. Manual setpoints are disabled.")
        elif self.devices:
            self.operation_mode_hint.set("Задавайте поток в карточке нужного MFC. Кнопки «Установить» и «Ноль» отправляют команду сразу.")
        else:
            self.operation_mode_hint.set("Сначала подключите масс-флоу на вкладке «Подключение».")

    def _update_manual_cards(self) -> None:
        for serial_number in set(self.manual_cards) - self.devices.keys():
            self.manual_cards.pop(serial_number).destroy()
        for serial_number in sorted(self.devices):
            if serial_number not in self.manual_cards:
                self.manual_cards[serial_number] = ManualFlowCard(
                    self.manual_cards_frame, serial_number, self._set_manual_flow, self._zero_manual_flow,
                )
            card = self.manual_cards[serial_number]
            card.pack(fill="x", pady=(0, 10))
            card.update_device(
                self.devices[serial_number],
                self.roles.get(serial_number, "Unassigned"),
                self.device_modes.get(serial_number, "Read flow"),
            )
        if self.manual_cards:
            self.manual_empty_label.pack_forget()
        elif not self.manual_empty_label.winfo_manager():
            self.manual_empty_label.pack(anchor="w")

    def _append_connection_log(self, message: str) -> None:
        self._connection_log_lines.append(message)
        self._connection_log_lines = self._connection_log_lines[-2000:]
        at_bottom = self.connection_log.yview()[1] >= 0.99
        self.connection_log.configure(state="normal")
        self.connection_log.delete("1.0", "end")
        self.connection_log.insert("end", "\n".join(self._connection_log_lines) + "\n")
        if at_bottom:
            self.connection_log.see("end")
        self.connection_log.configure(state="disabled")

    def _connect_selected_port(self) -> None:
        port = self.available_port_var.get()
        if not port:
            messagebox.showwarning("No COM port", "Select an available USB COM port first.")
            return
        self._append_connection_log(f"{time.strftime('%H:%M:%S')}  UI: connect button pressed for {port}.")
        threading.Thread(target=self._run_connection_command, args=("connect_port", port), daemon=True).start()

    def _disconnect_selected_port(self) -> None:
        port = self.available_port_var.get()
        if not port:
            messagebox.showwarning("No COM port", "Select a COM port first.")
            return
        self._append_connection_log(f"{time.strftime('%H:%M:%S')}  UI: disconnect button pressed for {port}.")
        threading.Thread(target=self._run_connection_command, args=("disconnect_port", port), daemon=True).start()

    def _run_connection_command(self, action: str, port: str) -> None:
        try:
            self.service.submit(action, {"port": port}, timeout_s=12)
            self.events.put(("connection_log", f"{time.strftime('%H:%M:%S')}  UI: {action} completed for {port}."))
            self.events.put(("connection_status", f"Connected {port}."))
        except Exception as exc:
            message = f"{action} failed for {port}: {exc}"
            self.events.put(("connection_log", f"{time.strftime('%H:%M:%S')}  UI: {message}"))
            self.events.put(("connection_error", message))

    def _update_devices(self, devices: list[DeviceInfo]) -> None:
        self.devices = {device.serial_number: device for device in devices}
        mismatches: list[str] = []
        for device in devices:
            profile = self.store.fluid_profiles.get(device.serial_number, {}).get(str(device.fluidset_index), {})
            expected = str(profile.get("expected_gas", "")).strip()
            if expected and expected.casefold() != device.fluid_name.casefold():
                mismatches.append(f"{device.serial_number}: expected gas '{expected}', device reports '{device.fluid_name}'.")
        unseen = set(self.tree.get_children())
        for serial_number, device in self.devices.items():
            capacity = "unknown" if device.capacity is None else f"{device.capacity:.6g} {device.unit}"
            measured = "—" if device.measured_flow is None else f"{device.measured_flow:.6g} {device.unit}"
            values = (
                serial_number,
                self.roles.get(serial_number, "Unassigned"),
                self.device_modes.get(serial_number, "Read flow"),
                device.port,
                "ProPar",
                device.model,
                capacity,
                measured,
            )
            if serial_number in unseen:
                self.tree.item(serial_number, values=values)
                unseen.remove(serial_number)
            else:
                self.tree.insert("", "end", iid=serial_number, values=values)
        for serial_number in unseen:
            self.tree.delete(serial_number)
        self._update_calibration_table()
        serials = sorted(self.devices)
        self.role_device_box["values"] = serials
        self._update_manual_cards()
        if serials:
            self.role_device_box.configure(state="readonly")
            self.device_mode_box.configure(state="readonly")
            if self.role_device_var.get() not in serials:
                self.role_device_var.set(serials[0])
                self._load_role()
        else:
            self.role_device_box.configure(state="disabled")
            self.device_mode_box.configure(state="disabled")
            self.role_device_var.set("No device detected")
            self.role_var.set("Unassigned")
            self.device_mode_var.set("Read flow")
        selected = self.devices.get(self.role_device_var.get())
        if selected is not None:
            snapshot = self._condition_snapshot(selected)
            if snapshot != self._conditions_snapshot:
                self._load_role()
        if mismatches:
            self.port_warning_var.set("GAS PROFILE MISMATCH — do not start flow: " + " ".join(mismatches))
        self._update_operation_mode_controls()
        self._redraw_experiment_view()
        self.status_var.set(f"Connected Bronkhorst devices: {len(serials)}. Available USB COM list refreshes every 2 s; only ports selected with Connect are opened.")

    def _update_calibration_table(self) -> None:
        unseen = set(self.calibration_tree.get_children())
        for serial_number, device in self.devices.items():
            values = (
                serial_number,
                device.port,
                device.fluid_name,
                "—" if device.fluidset_index is None else str(device.fluidset_index),
                "—" if device.inlet_pressure_bar_a is None else f"{device.inlet_pressure_bar_a:g}",
                "—" if device.outlet_pressure_bar_a is None else f"{device.outlet_pressure_bar_a:g}",
                "—" if device.fluid_temperature_c is None else f"{device.fluid_temperature_c:g}",
                "—" if device.reference_pressure_bar_a is None else f"{device.reference_pressure_bar_a:g}",
                "—" if device.capacity is None else f"{device.capacity:.6g} {device.unit}",
            )
            if serial_number in unseen:
                self.calibration_tree.item(serial_number, values=values)
                unseen.remove(serial_number)
            else:
                self.calibration_tree.insert("", "end", iid=serial_number, values=values)
        for serial_number in unseen:
            self.calibration_tree.delete(serial_number)

    def _select_calibration_device(self, _event: object | None = None) -> None:
        selected = self.calibration_tree.selection()
        if not selected:
            return
        serial_number = selected[0]
        if serial_number in self.devices:
            self.role_device_var.set(serial_number)
            self._load_role()

    @staticmethod
    def _condition_snapshot(device: DeviceInfo) -> tuple[Any, ...]:
        return (
            device.serial_number, device.fluid_name, device.fluidset_index,
            device.inlet_pressure_bar_a, device.outlet_pressure_bar_a,
            device.fluid_temperature_c, device.reference_pressure_bar_a,
        )

    def _load_role(self, _event: object | None = None) -> None:
        serial_number = self.role_device_var.get()
        self.role_var.set(self.roles.get(serial_number, "Unassigned"))
        self.device_mode_var.set(self.device_modes.get(serial_number, "Read flow"))
        device = self.devices.get(serial_number)
        if device is None:
            self.fluid_conditions_var.set("Select a detected device to inspect its active gas calibration.")
            self.fluidset_var.set("")
            self.expected_gas_var.set("")
            self.inlet_pressure_var.set("")
            self.outlet_pressure_var.set("")
            self.fluid_temperature_var.set("")
            self.reference_pressure_var.set("")
            self._conditions_snapshot = None
            return
        self._conditions_snapshot = self._condition_snapshot(device)
        fluid_set = "unknown" if device.fluidset_index is None else str(device.fluidset_index)
        inlet = "unknown" if device.inlet_pressure_bar_a is None else f"{device.inlet_pressure_bar_a:g} bar(a)"
        outlet = "unknown" if device.outlet_pressure_bar_a is None else f"{device.outlet_pressure_bar_a:g} bar(a)"
        temperature = "unknown" if device.fluid_temperature_c is None else f"{device.fluid_temperature_c:g} °C"
        reference = "unknown" if device.reference_pressure_bar_a is None else f"{device.reference_pressure_bar_a:g} bar(a)"
        self.fluid_conditions_var.set(
            f"Active fluid set {fluid_set}: {device.fluid_name}. Stored inlet {inlet}; outlet {outlet}; "
            f"fluid temperature {temperature}; volume reference {reference}."
        )
        self.fluidset_var.set("" if device.fluidset_index is None else str(device.fluidset_index))
        saved_profile = self.store.fluid_profiles.get(serial_number, {}).get(str(device.fluidset_index), {})
        self.expected_gas_var.set(str(saved_profile.get("expected_gas", device.fluid_name)))
        self.inlet_pressure_var.set("" if device.inlet_pressure_bar_a is None else f"{device.inlet_pressure_bar_a:g}")
        self.outlet_pressure_var.set("" if device.outlet_pressure_bar_a is None else f"{device.outlet_pressure_bar_a:g}")
        self.fluid_temperature_var.set("" if device.fluid_temperature_c is None else f"{device.fluid_temperature_c:g}")
        self.reference_pressure_var.set("" if device.reference_pressure_bar_a is None else f"{device.reference_pressure_bar_a:g}")

    def _parse_fluid_conditions(self) -> dict[str, float]:
        try:
            values = {
                "inlet": float(self.inlet_pressure_var.get().replace(",", ".")),
                "outlet": float(self.outlet_pressure_var.get().replace(",", ".")),
                "temperature": float(self.fluid_temperature_var.get().replace(",", ".")),
                "reference": float(self.reference_pressure_var.get().replace(",", ".")),
            }
        except ValueError as exc:
            raise ValueError("Enter numeric fluid-set conditions: pressures in bar(a), temperature in °C.") from exc
        if not all(math.isfinite(value) for value in values.values()) or values["inlet"] <= 0 or values["outlet"] <= 0 or values["reference"] <= 0:
            raise ValueError("Pressures must be finite positive values in bar(a).")
        return values

    def _apply_fluid_conditions(self) -> None:
        if not self.control_enabled.get():
            messagebox.showwarning("Writes disabled", "Enable writing setpoints before updating stored fluid-set conditions.")
            return
        serial_number = self.role_device_var.get()
        device = self.devices.get(serial_number)
        if device is None:
            messagebox.showwarning("No device", "Select a connected device first.")
            return
        try:
            values = self._parse_fluid_conditions()
        except ValueError as exc:
            messagebox.showwarning("Invalid conditions", str(exc))
            return
        if not messagebox.askyesno(
            "Confirm fluid-set conditions",
            f"Store the displayed conditions for active fluid set {device.fluidset_index} ({device.fluid_name}) on {serial_number}?\n\n"
            "These are gas-model conditions, not sensor calibration constants or live measurements.",
        ):
            return
        try:
            self.service.submit("set_fluid_conditions", {serial_number: values})
            self.store.save_fluid_profile(serial_number, device.fluidset_index or 0, {"expected_gas": self.expected_gas_var.get().strip(), **values})
            self.status_var.set(f"Fluid-set condition command acknowledged for {serial_number}; values will refresh shortly.")
        except Exception as exc:
            messagebox.showerror("Condition update failed", str(exc))

    def _activate_fluidset(self) -> None:
        if not self.control_enabled.get():
            messagebox.showwarning("Writes disabled", "Enable writing setpoints before activating a fluid set.")
            return
        serial_number = self.role_device_var.get()
        device = self.devices.get(serial_number)
        if device is None:
            messagebox.showwarning("No device", "Select a connected device first.")
            return
        try:
            fluidset = int(self.fluidset_var.get())
            if not 0 <= fluidset <= 7:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Invalid fluid-set slot", "Choose an integer slot from 0 to 7.")
            return
        expected = self.expected_gas_var.get().strip()
        if not expected:
            messagebox.showwarning("Expected gas required", "Enter the expected gas label before switching a fluid set.")
            return
        if not messagebox.askyesno(
            "Activate fluid set",
            f"Activate fluid-set slot {fluidset} on {serial_number}?\nExpected gas: {expected}\n\n"
            "The active gas conversion model and range may change. Verify the gas label read back from the device before flowing gas.",
        ):
            return
        try:
            self.service.submit("activate_fluidset", {serial_number: fluidset})
            self.store.save_fluid_profile(serial_number, fluidset, {"expected_gas": expected})
            self.status_var.set(f"Fluid-set {fluidset} activation acknowledged. Waiting to read back and verify the device gas label…")
        except Exception as exc:
            messagebox.showerror("Fluid-set activation failed", str(exc))

    def _save_role(self) -> None:
        serial_number, role = self.role_device_var.get(), self.role_var.get()
        if serial_number not in self.devices:
            messagebox.showwarning("No device", "Select a connected device first.")
            return
        self.roles[serial_number] = role
        self.store.save_roles(self.roles)
        self._update_devices(list(self.devices.values()))
        self.status_var.set(f"Saved role for {serial_number}: {role}.")

    def _save_device_mode(self) -> None:
        serial_number, mode = self.role_device_var.get(), self.device_mode_var.get()
        if serial_number not in self.devices:
            messagebox.showwarning("No device", "Select a connected device first.")
            return
        if mode not in MFC_MODES:
            messagebox.showwarning("Invalid MFC mode", "Choose Read flow or Set flow.")
            return
        self.device_modes[serial_number] = mode
        self.store.save_device_modes(self.device_modes)
        self._update_devices(list(self.devices.values()))
        label = "чтение потока" if mode == "Read flow" else "выставление потока"
        self.status_var.set(f"Режим {serial_number}: {label}.")

    def _role_devices(self) -> tuple[DeviceInfo, DeviceInfo]:
        h2 = [self.devices[serial] for serial, role in self.roles.items() if role == "H2" and serial in self.devices]
        alkene = [self.devices[serial] for serial, role in self.roles.items() if role == "Alkene" and serial in self.devices]
        if len(h2) != 1 or len(alkene) != 1:
            raise ValueError("Assign exactly one connected device to H2 and one to Alkene.")
        if h2[0].unit != alkene[0].unit:
            raise ValueError("H2 and alkene units differ; use matching standard-volume units.")
        if h2[0].capacity is None or alkene[0].capacity is None:
            raise ValueError("Could not read both capacities; writing is blocked.")
        for device in (h2[0], alkene[0]):
            if self.device_modes.get(device.serial_number, "Read flow") != "Set flow":
                raise ValueError(f"{device.serial_number} is in Read flow mode; choose Set flow before starting the plan.")
            profile = self.store.fluid_profiles.get(device.serial_number, {}).get(str(device.fluidset_index), {})
            expected = str(profile.get("expected_gas", "")).strip()
            if expected and expected.casefold() != device.fluid_name.casefold():
                raise ValueError(
                    f"Gas profile mismatch for {device.serial_number}: expected '{expected}', device reports '{device.fluid_name}'."
                )
        return h2[0], alkene[0]

    def _redraw_experiment_view(self) -> None:
        if self.flow_plot is None or not self.flow_plot.winfo_exists():
            return
        self.flow_plot.set_data(
            self.active_plan,
            self.plan_h2_serial,
            self.plan_alkene_serial,
            self.devices,
            self.current_plan_point,
        )

    def _open_graph_window(self) -> None:
        if self.graph_window is None or not self.graph_window.winfo_exists():
            self.graph_window = tk.Toplevel(self)
            self.graph_window.title("Experiment flow graph")
            self.graph_window.geometry("1100x680")
            self.graph_window.minsize(720, 440)
            self.graph_window.protocol("WM_DELETE_WINDOW", self._close_graph_window)
            self.flow_plot = ExperimentPlot(self.graph_window)
            self.flow_plot.pack(fill="both", expand=True, padx=12, pady=12)
        self.graph_window.deiconify()
        self.graph_window.lift()
        self._redraw_experiment_view()

    def _close_graph_window(self) -> None:
        if self.graph_window is not None and self.graph_window.winfo_exists():
            self.graph_window.destroy()
        self.graph_window = None
        self.flow_plot = None

    def _show_plan(self, plan: list[ExperimentPoint], h2: DeviceInfo, alkene: DeviceInfo) -> None:
        self.active_plan = list(plan)
        self.plan_h2_serial, self.plan_alkene_serial = h2.serial_number, alkene.serial_number
        self.current_plan_point = None
        for item in self.schedule_tree.get_children():
            self.schedule_tree.delete(item)
        for point in plan:
            self.schedule_tree.insert(
                "", "end", iid=f"point-{point.number}",
                values=(point.number, f"{point.total_flow:g}", f"{point.h2_flow:g}", f"{point.alkene_flow:g}", f"{point.dwell_s:g}"),
            )
        self._redraw_experiment_view()

    def _manual_flow_request(self, serial_number: str, flow: float) -> None:
        if self.operation_mode.get() != "manual":
            messagebox.showinfo("Automatic mode", "Select Manual mode before sending a setpoint to an individual MFC.")
            return
        if self.runner and self.runner.is_alive():
            messagebox.showwarning("Experiment running", "Stop the scheduled experiment before using manual flow control.")
            return
        if serial_number in self.manual_commands_pending:
            messagebox.showwarning("Command in progress", "Wait for the previous manual setpoint command to finish.")
            return
        device = self.devices.get(serial_number)
        if device is None:
            messagebox.showwarning("No device", "Connect and select an MFC first.")
            return
        if self.device_modes.get(serial_number, "Read flow") != "Set flow":
            messagebox.showwarning("Read flow mode", f"{serial_number} is configured for flow reading. Choose Set flow before sending a setpoint.")
            return
        if not math.isfinite(flow) or flow < 0:
            messagebox.showwarning("Invalid flow", "Enter a finite non-negative setpoint.")
            return
        if device.capacity is None or flow > device.capacity + 1e-7:
            messagebox.showwarning("Out of range", f"Allowed range for {serial_number}: 0–{device.capacity:g} {device.unit}" if device.capacity is not None else "The device capacity is unknown.")
            return
        self.manual_commands_pending.add(serial_number)
        self.manual_cards[serial_number].command_var.set(f"Отправка уставки: {flow:g} {device.unit}…")
        self._update_operation_mode_controls()
        threading.Thread(target=self._run_manual_setpoint, args=(serial_number, flow), daemon=True).start()

    def _set_manual_flow(self, serial_number: str) -> None:
        card = self.manual_cards.get(serial_number)
        if card is None:
            return
        try:
            flow = float(card.flow_var.get().replace(",", "."))
        except ValueError:
            messagebox.showwarning("Invalid flow", "Enter a numeric setpoint.")
            return
        self._manual_flow_request(serial_number, flow)

    def _zero_manual_flow(self, serial_number: str) -> None:
        card = self.manual_cards.get(serial_number)
        if card is not None:
            card.flow_var.set("0")
            self._manual_flow_request(serial_number, 0.0)

    def _run_manual_setpoint(self, serial_number: str, flow: float) -> None:
        try:
            self.service.submit("set", {serial_number: flow})
            self.events.put(("manual_setpoint", (serial_number, flow)))
        except Exception as exc:
            self.events.put(("manual_setpoint_error", (serial_number, str(exc))))

    def _current_plan(self) -> list[ExperimentPoint]:
        try:
            values = {key: float(variable.get().replace(",", ".")) for key, variable in self.plan_vars.items()}
        except ValueError as exc:
            raise ValueError("Plan values must be numbers") from exc
        return build_plan(values["start"], values["stop"], values["step"], values["ratio"], values["dwell"])

    def _validate_plan(self) -> list[ExperimentPoint] | None:
        if self.operation_mode.get() != "automatic":
            self.plan_var.set("Select Automatic mode to validate a scheduled plan.")
            return None
        try:
            plan = self._current_plan()
            h2, alkene = self._role_devices()
            last = plan[-1]
            if last.h2_flow > h2.capacity + 1e-7 or last.alkene_flow > alkene.capacity + 1e-7:
                raise ValueError(f"Final point exceeds capacity: H2 {last.h2_flow:.6g}/{h2.capacity:.6g}, alkene {last.alkene_flow:.6g}/{alkene.capacity:.6g} {h2.unit}")
            seconds = sum(point.dwell_s for point in plan)
            self._show_plan(plan, h2, alkene)
            self.plan_var.set(f"{len(plan)} points, {plan[0].total_flow:g}–{plan[-1].total_flow:g} {h2.unit}; {seconds / 60:g} min. Capacities are safe.")
            return plan
        except Exception as exc:
            self.plan_var.set("Plan error: " + str(exc))
            return None

    def _toggle_control(self) -> None:
        if self.control_enabled.get() and not messagebox.askyesno("Enable writes", "Writing setpoints controls gas flow. Are shutdown valves, pressure protection and leak checks active?"):
            self.control_enabled.set(False)

    def _start_experiment(self) -> None:
        if self.operation_mode.get() != "automatic":
            messagebox.showinfo("Manual mode", "Switch to Automatic mode before starting the scheduled experiment.")
            return
        if not self.control_enabled.get():
            messagebox.showwarning("Writes disabled", "Enable writing setpoints before starting.")
            return
        if self.runner and self.runner.is_alive():
            messagebox.showwarning("Already running", "Stop the active experiment first.")
            return
        plan = self._validate_plan()
        if plan is None:
            return
        h2, alkene = self._role_devices()
        if not messagebox.askyesno("Start experiment", f"Start {len(plan)} points at H2:alkene = {self.plan_vars['ratio'].get()}?\nBoth controlled channels are zeroed when stopped or complete."):
            return
        self.runner = ExperimentRunner(self.service, plan, h2.serial_number, alkene.serial_number, self.events)
        self.runner.start()
        self.progress_var.set("Starting experiment…")

    def _stop_experiment(self) -> None:
        if self.operation_mode.get() != "automatic":
            messagebox.showinfo("Manual mode", "Use 'Set selected channel to zero' to stop the manually selected MFC.")
            return
        if not self.runner or not self.runner.is_alive():
            messagebox.showinfo("No active experiment", "No scheduled experiment is running.")
            return
        if messagebox.askyesno("Stop experiment", "Stop and command zero flow to the H2 and alkene channels?"):
            self.runner.cancel()
            self.progress_var.set("Stopping; requesting zero setpoints…")

    def _consume_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "devices":
                    self._update_devices(payload)
                elif kind == "stage":
                    point, _ = payload
                    self._append_connection_log(f"{time.strftime('%H:%M:%S')}  POINT {point.number}: H2={point.h2_flow:g}, alkene={point.alkene_flow:g}, dwell={point.dwell_s:g} s")
                    self.current_plan_point = point.number
                    self.schedule_tree.selection_set(f"point-{point.number}")
                    self.schedule_tree.see(f"point-{point.number}")
                    self._redraw_experiment_view()
                    self.progress_var.set(f"Point {point.number}: total {point.total_flow:g}; H2 {point.h2_flow:g}; alkene {point.alkene_flow:g}. Holding {point.dwell_s:g} s.")
                elif kind == "progress":
                    number, elapsed, dwell = payload
                    self.progress_var.set(f"Point {number}: stabilization {elapsed:.0f}/{dwell:.0f} s")
                elif kind == "finished":
                    self._append_connection_log(f"{time.strftime('%H:%M:%S')}  {payload}")
                    self.progress_var.set(payload)
                    self.current_plan_point = None
                    self._redraw_experiment_view()
                elif kind == "runner_error":
                    self._append_connection_log(f"{time.strftime('%H:%M:%S')}  EXPERIMENT ERROR: {payload}")
                    self.progress_var.set("EXPERIMENT ERROR: " + payload)
                    self.control_enabled.set(False)
                elif kind == "port_warning":
                    self.port_warning_var.set(payload)
                elif kind == "port_recovered":
                    self.port_warning_var.set(payload)
                elif kind == "connection_log":
                    self._append_connection_log(payload)
                elif kind == "connection_status":
                    self.port_warning_var.set("")
                    self.status_var.set(payload)
                elif kind == "connection_error":
                    self.port_warning_var.set(payload)
                    self.status_var.set("Connection failed; see the Console tab for details.")
                elif kind == "manual_setpoint":
                    serial_number, flow = payload
                    self.manual_commands_pending.discard(serial_number)
                    self.manual_setpoints[serial_number] = flow
                    device = self.devices.get(serial_number)
                    unit = "device unit" if device is None else device.unit
                    self.progress_var.set(f"Manual setpoint acknowledged: {serial_number} = {flow:g} {unit}.")
                    card = self.manual_cards.get(serial_number)
                    if card is not None:
                        card.command_var.set(f"Последняя команда принята: {flow:g} {unit}")
                    self._append_connection_log(f"{time.strftime('%H:%M:%S')}  MANUAL {serial_number}: accepted setpoint {flow:g} {unit}")
                    self._update_operation_mode_controls()
                elif kind == "manual_setpoint_error":
                    serial_number, error = payload
                    self.manual_commands_pending.discard(serial_number)
                    self.progress_var.set(f"MANUAL SETPOINT ERROR {serial_number}: {error}")
                    card = self.manual_cards.get(serial_number)
                    if card is not None:
                        card.command_var.set(f"Ошибка команды: {error}")
                    self._append_connection_log(f"{time.strftime('%H:%M:%S')}  MANUAL ERROR {serial_number}: {error}")
                    self.control_enabled.set(False)
                    self._update_operation_mode_controls()
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._consume_events)

    def _close(self) -> None:
        if self.runner and self.runner.is_alive():
            self.runner.cancel()
        self.service.shutdown()
        self.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    args = parser.parse_args()
    try:
        store = ConfigStore(args.config)
    except Exception as exc:
        raise SystemExit(f"Invalid configuration: {exc}") from exc
    GasLineApp(store).mainloop()


if __name__ == "__main__":
    main()
