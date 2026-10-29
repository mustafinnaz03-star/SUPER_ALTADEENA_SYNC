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
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, replace
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any
from uuid import uuid4

# The project is distributed with a local virtual environment.  VS Code can
# otherwise run a standalone file with its bundled Python (rather than the
# interpreter selected for this workspace), which would not see ProPar.
# Prefer the project packages whenever that environment is present; on an
# installed deployment without .venv this loop has no effect.
_PROJECT_ROOT = Path(__file__).resolve().parent
_PROJECT_VENV = _PROJECT_ROOT / ".venv"
_VENV_PACKAGE_DIRS = [_PROJECT_VENV / "Lib" / "site-packages"]
if (_PROJECT_VENV / "lib").is_dir():
    _VENV_PACKAGE_DIRS.extend(sorted((_PROJECT_VENV / "lib").glob("python*/site-packages")))
for _package_dir in _VENV_PACKAGE_DIRS:
    if _package_dir.is_dir() and str(_package_dir) not in sys.path:
        sys.path.insert(0, str(_package_dir))

import propar
import serial
from serial.tools import list_ports

from bronkhorst_ui import ROLE_LABELS, MODE_LABELS, build_interface, configure_style, refresh_experiment_presentation
from experiment_cycle import AUTOMATIC_MODES, CYCLE_DEFAULTS, OPERATION_LABELS, RELAY_SELECTION_LABELS, CycleTiming, ArduinoValveController, ArduinoRecoveryPolicy


# FlowDDE parameter numbers, used by the EL-FLOW RS232/FLOW-BUS (ProPar) interface.
FMEASURE, FSETPOINT, CAPACITY, CAPACITY_UNIT = 205, 206, 21, 129
MODEL, SERIAL_NUMBER = 91, 92
FLUIDSET_INDEX, FLUID_NAME = 24, 25
INLET_PRESSURE, OUTLET_PRESSURE, FLUID_TEMPERATURE = 178, 179, 181
REFERENCE_PRESSURE = 246
ROLES = ("Unassigned", "H2", "Alkene", "Other")
MFC_MODES = ("Read flow", "Set flow")
OPERATION_MODES = tuple(OPERATION_LABELS)
ARDUINO_READY = "READY"
ARDUINO_TRIGGERED = "TRIGGERED"
ARDUINO_NMR_DONE = "NMR_DONE"
ARDUINO_OFF_SETTLE_S = 1.0
MAX_PLAN_POINTS = 10000


def is_console_detail(message: str) -> bool:
    """Hide known protocol chatter only; preserve unknown messages and faults."""
    body = message.split("  ", 1)[-1].strip()
    lower = body.casefold()
    if any(marker in lower for marker in (
        "error", "fail", "timeout", "ошиб", "не удалось", "не подтвержд",
        "предупрежд", "внимание", "boot", "reset_cause",
    )):
        return False
    if body.startswith(("ARDUINO: > ", "ARDUINO: < ", "COMMAND ", "OK ", "CONNECT requested ")):
        return True
    if body.startswith("UI:"):
        return body.startswith(("UI: connect button pressed ", "UI: disconnect button pressed ",
                                "UI: Arduino connect requested ", "UI: connect_port completed ",
                                "UI: disconnect_port completed "))
    if body.startswith(("ARDUINO: Opening ", "ARDUINO: Реле:")):
        return True
    if body.startswith("ARDUINO: Точка ") and (
        "подтвердила отправку TTL" in body or "получен ответный TTL" in body
    ):
        # The runner already reports each numbered spectrum and confirmation.
        return True
    return "identification reply" in lower

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


@dataclass
class ArduinoCommand:
    name: str
    payload: dict[str, Any] | None = None
    done: threading.Event | None = None
    error: Exception | None = None
    result: Any = None


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
        raise ValueError("Параметры плана должны быть конечными числами")
    if start < 0 or stop < start or step <= 0 or ratio <= 0 or dwell_s < 0:
        raise ValueError("Начальный поток: от 0 до конечного; шаг и соотношение — больше нуля; t1 — не меньше нуля")
    steps = (stop - start) / step
    if not math.isfinite(steps) or steps > MAX_PLAN_POINTS - 1 + 1e-9:
        raise ValueError(f"План не должен содержать больше {MAX_PLAN_POINTS} точек")
    count = int(math.floor(steps + 1e-9)) + 1
    totals = [round(start + index * step, 9) for index in range(count)]
    if not math.isclose(totals[-1], stop, abs_tol=1e-7):
        raise ValueError("Разница конечного и начального потоков должна делиться на шаг")
    return [
        ExperimentPoint(i + 1, total, total * ratio / (1 + ratio), total / (1 + ratio), dwell_s)
        for i, total in enumerate(totals)
    ]


def build_single_mfc_plan(start: float, stop: float, step: float, dwell_s: float) -> list[ExperimentPoint]:
    """Build an inclusive ramp for one MFC; every point is its full setpoint."""
    if not all(math.isfinite(value) for value in (start, stop, step, dwell_s)):
        raise ValueError("Параметры плана должны быть конечными числами")
    if start < 0 or stop < start or step <= 0 or dwell_s < 0:
        raise ValueError("Начальный поток: от 0 до конечного; шаг — больше нуля; t1 — не меньше нуля")
    steps = (stop - start) / step
    if not math.isfinite(steps) or steps > MAX_PLAN_POINTS - 1 + 1e-9:
        raise ValueError(f"План не должен содержать больше {MAX_PLAN_POINTS} точек")
    count = int(math.floor(steps + 1e-9)) + 1
    totals = [round(start + index * step, 9) for index in range(count)]
    if not math.isclose(totals[-1], stop, abs_tol=1e-7):
        raise ValueError("Разница конечного и начального потоков должна делиться на шаг")
    return [ExperimentPoint(i + 1, total, total, 0.0, dwell_s) for i, total in enumerate(totals)]


def build_custom_plan(flows: str, ratio: float, dwell_s: float, single_mfc: bool = False) -> list[ExperimentPoint]:
    """Preserve the entered order and duplicates; commas are decimal marks."""
    tokens = flows.replace(";", " ").split()
    if not tokens:
        raise ValueError("Введите список потоков, например: 100 110 110 110 120")
    if len(tokens) > MAX_PLAN_POINTS:
        raise ValueError(f"План не должен содержать больше {MAX_PLAN_POINTS} точек")
    if not math.isfinite(dwell_s) or dwell_s < 0:
        raise ValueError("t1 должна быть конечным числом не меньше нуля")
    if not single_mfc and (not math.isfinite(ratio) or ratio <= 0):
        raise ValueError("Соотношение H₂ : алкен должно быть конечным числом больше нуля")
    plan = []
    for number, token in enumerate(tokens, 1):
        try:
            total = float(token.replace(",", "."))
        except ValueError as exc:
            raise ValueError(f"Точка {number}: неверный поток «{token}». Разделяйте числа пробелами или ;") from exc
        if not math.isfinite(total) or total < 0:
            raise ValueError(f"Точка {number}: поток должен быть конечным числом не меньше нуля")
        h2 = total if single_mfc else total * (ratio / (1 + ratio))
        alkene = 0.0 if single_mfc else total / (1 + ratio)
        plan.append(ExperimentPoint(number, total, h2, alkene, dwell_s))
    return plan


def repeat_plan_points(plan: list[ExperimentPoint], repeats: int = 1) -> list[ExperimentPoint]:
    """Repeat EACH point consecutively, assigning unique acquisition numbers."""
    if type(repeats) is not int or not 1 <= repeats <= MAX_PLAN_POINTS:
        raise ValueError(f"Повтор каждой точки: целое число от 1 до {MAX_PLAN_POINTS}")
    if not plan:
        raise ValueError("План должен содержать хотя бы одну точку")
    if len(plan) * repeats > MAX_PLAN_POINTS:
        raise ValueError(f"С учётом повторов допускается не больше {MAX_PLAN_POINTS} точек")
    return [replace(point, number=index * repeats + repetition + 1)
            for index, point in enumerate(plan) for repetition in range(repeats)]


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
        raw_arduino = self.raw.get("arduino", {})
        self.arduino = {
            "port": str(raw_arduino.get("port", "")),
            "baudrate": int(raw_arduino.get("baudrate", 115200)),
            "spectrum_timeout_s": float(raw_arduino.get("spectrum_timeout_s", 180.0)),
            "startup_timeout_s": float(raw_arduino.get("startup_timeout_s", 8.0)),
        }
        if not 1200 <= self.arduino["baudrate"] <= 2_000_000:
            raise ValueError("Invalid Arduino baudrate")
        if not 1 <= self.arduino["spectrum_timeout_s"] <= 86_400 or not 1 <= self.arduino["startup_timeout_s"] <= 60:
            raise ValueError("Invalid Arduino timeout")
        raw_profiles = self.raw.get("fluid_profiles", {})
        self.experiment = dict(self.raw.get("experiment", {}))
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

    def save_arduino(self, settings: dict[str, Any]) -> None:
        self.arduino = dict(settings)
        self.raw["arduino"] = self.arduino
        self.path.write_text(json.dumps(self.raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def save_experiment(self, settings: dict[str, Any]) -> None:
        self.experiment = dict(settings)
        self.raw["experiment"] = self.experiment
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


class ArduinoNMRService(threading.Thread):
    """Owns the Arduino COM port and implements a correlated TTL handshake.

    Protocol deliberately uses printable newline-delimited messages so it can
    be inspected in Arduino Serial Monitor:
      PC -> Arduino:  TRIGGER <run-id> <point>
      Arduino -> PC:  TRIGGERED <run-id> <point>
      Arduino -> PC:  NMR_DONE <run-id> <point>
    A point is accepted only after the matching TRIGGERED and NMR_DONE lines.
    """

    def __init__(self, settings: dict[str, Any], events: queue.Queue[tuple[str, Any]]):
        super().__init__(daemon=True)
        self.settings = dict(settings)
        self.events = events
        self.commands: queue.Queue[ArduinoCommand] = queue.Queue()
        self.stopping = threading.Event()
        self.port: serial.Serial | None = None
        self.ready = False
        self.relay_capable = False
        self.relay_selection_capable = False
        self.last_keepalive = 0.0

    def _log(self, message: str) -> None:
        self.events.put(("arduino_log", f"{time.strftime('%H:%M:%S')}  ARDUINO: {message}"))

    def _status(self, message: str) -> None:
        self.events.put(("arduino_status", message))

    def _close_port(self) -> None:
        self.ready = False
        self.relay_capable = False
        self.relay_selection_capable = False
        if self.port is not None:
            try:
                self.port.close()
            except Exception:
                pass
        self.port = None

    def _read_line(self) -> str | None:
        if self.port is None:
            return None
        raw = self.port.readline()
        if not raw:
            return None
        return raw.decode("utf-8", errors="replace").strip()

    def _write_line(self, line: str) -> None:
        if self.port is None or not self.port.is_open:
            raise ConnectionError("Arduino COM port is not open")
        self.port.write((line + "\n").encode("ascii"))
        self.port.flush()
        self._log(f"> {line}")

    def _check_cancelled(self, cancel_event=None) -> None:
        if self.stopping.is_set() or (cancel_event is not None and cancel_event.is_set()):
            raise InterruptedError("Восстановление/проверка Arduino остановлена оператором")

    def _connect(self, settings: dict[str, Any], cancel_event=None, deadline: float | None = None) -> None:
        port = str(settings.get("port", "")).strip()
        if not port:
            raise ValueError("Choose an Arduino COM port")
        baudrate = int(settings.get("baudrate", 115200))
        startup_timeout = float(settings.get("startup_timeout_s", 8.0))
        self._check_cancelled(cancel_event)
        self._close_port()
        self._status(f"Подключение Arduino: {port}, {baudrate} бод…")
        self._log(f"Opening {port} at {baudrate} baud.")
        self.port = serial.Serial(port=port, baudrate=baudrate, timeout=0.1, write_timeout=1)
        self.settings = {**self.settings, **settings, "port": port, "baudrate": baudrate}
        startup_deadline = time.monotonic() + startup_timeout
        if deadline is not None:
            startup_deadline = min(startup_deadline, deadline)
        while time.monotonic() < startup_deadline:
            self._check_cancelled(cancel_event)
            line = self._read_line()
            if line is None:
                continue
            self._log(f"< {line}")
            if line == "CAPABILITIES NMR RELAYS_D4_D5":
                self.relay_capable = True
            if line == "CAPABILITY RELAY_MASK":
                self.relay_selection_capable = True
            if line == ARDUINO_READY:
                self.ready = True
                self._status(f"Arduino готова · {port}")
                return
        self._close_port()
        raise TimeoutError(f"Arduino did not send '{ARDUINO_READY}' within {startup_timeout:g} s")

    def _off_checkpoint(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Catch delayed USB/reset faults after OFF, then require a fresh ACK.

        This command never energizes a relay and never issues a TTL trigger.
        Recovery is only requested at t1, t4 or the final OFF boundary.
        """
        cancel_event = payload.get("cancel_event")
        deadline = payload.get("deadline", float("inf"))
        settle_until = time.monotonic() + ARDUINO_OFF_SETTLE_S
        while time.monotonic() < settle_until:
            self._check_cancelled(cancel_event)
            if time.monotonic() >= deadline:
                raise TimeoutError("Истекло время восстановления Arduino")
            if not self.ready:
                raise ConnectionError("Arduino потеряла готовность после OFF")
            self._keepalive()
            line = self._read_line()
            if line:
                self._log(f"< {line}")
                self._check_board_fault(line)
        self._check_cancelled(cancel_event)
        result = self._set_valves({"switched": False, "relay_mask": 3,
                                   "cancel_event": cancel_event, "deadline": deadline})
        self._check_cancelled(cancel_event)
        return result

    def _recover_off(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Bounded reconnect to the existing port, ending with confirmed all-OFF."""
        cancel_event = payload.get("cancel_event")
        # An absolute deadline also prevents a delayed queued command from
        # reconnecting after its caller has already timed out.
        deadline = payload["deadline"]
        policy = ArduinoRecoveryPolicy(payload.get("max_attempts", 3), payload.get("retry_interval_s", 1.0))
        offset = payload.get("attempt_offset", 0)
        total_limit = offset + policy.attempts
        cycle_label = f"цикл {payload['point']}" if "point" in payload else "текущий цикл"
        settings = dict(self.settings)
        attempt = 0
        self._close_port()
        try:
            while attempt < policy.attempts and time.monotonic() < deadline:
                self._check_cancelled(cancel_event)
                # Let Windows/USB finish re-enumeration before the first open,
                # not just between failed opens. Never shorten this cooldown.
                if deadline - time.monotonic() < policy.interval_s:
                    raise TimeoutError("Недостаточно времени для полной паузы перед подключением Arduino")
                self._log(f"Жду {policy.interval_s:g} с перед подключением к Arduino ({cycle_label}, попытка {offset + attempt + 1}/{total_limit}).")
                wait_event = self.stopping if cancel_event is None else cancel_event
                wait_event.wait(policy.interval_s)
                self._check_cancelled(cancel_event)
                if time.monotonic() >= deadline:
                    raise TimeoutError("Истекло время ожидания подключения Arduino")
                attempt += 1
                self._log(f"Пробую подключиться к Arduino: {settings.get('port')}, {cycle_label}, попытка {offset + attempt}/{total_limit}.")
                try:
                    self._connect(settings, cancel_event=cancel_event, deadline=deadline)
                    if not self.relay_capable or not self.relay_selection_capable:
                        raise RuntimeError("После переподключения нет поддержки реле RELAY_MASK; продолжение запрещено")
                    self._set_valves({"switched": False, "relay_mask": 3,
                                      "cancel_event": cancel_event, "deadline": deadline})
                    result = self._off_checkpoint(payload)
                    self._check_cancelled(cancel_event)
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Истекло время восстановления Arduino")
                    self._log("Восстановление завершено: READY и OFF обоих реле подтверждены.")
                    return {**result, "attempts_used": attempt}
                except InterruptedError:
                    raise
                except (OSError, TimeoutError) as exc:
                    self._log(f"Попытка восстановления не удалась: {exc}")
                    self._close_port()
            self._check_cancelled(cancel_event)
            raise TimeoutError(f"Не удалось подключиться к Arduino: выполнено {attempt} из {policy.attempts} оставшихся попыток; эксперимент остановлен")
        except Exception:
            self._close_port()
            raise

    def _keepalive(self) -> None:
        if self.ready and self.relay_capable and time.monotonic() - self.last_keepalive >= 1:
            # Do not flood the GUI log with routine watchdog traffic.
            if self.port is None or not self.port.is_open:
                raise ConnectionError("Arduino COM port is not open")
            self.port.write(b"KEEPALIVE\n")
            self.last_keepalive = time.monotonic()

    def _check_board_fault(self, line: str) -> None:
        if line == "READY" or line.startswith(("BOOT", "FAILSAFE")):
            self.ready = False
            self.events.put(("valve_status", (0, None, None)))
            raise ConnectionError("Arduino перезапустилась или потеряла связь. Подключите её заново; реле переведены прошивкой в OFF.")

    def _set_valves(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.ready or not self.relay_capable:
            raise ConnectionError("Нужна готовая Arduino с новой прошивкой TTL + реле D4/D5")
        switched = bool(payload["switched"])
        relay_mask = payload.get("relay_mask", 3)
        if type(relay_mask) is not int or relay_mask not in (1, 2, 3):
            raise ValueError("Недопустимый выбор реле")
        if not self.relay_selection_capable and (switched or relay_mask != 3):
            raise ConnectionError("Обновите прошивку Arduino: требуется поддержка выбора реле (RELAY_MASK)")
        token = uuid4().hex[:16]
        state = "ON" if switched else "OFF"
        selection = f" {relay_mask}" if self.relay_selection_capable else ""
        cancel_event = payload.get("cancel_event")
        if cancel_event is not None:
            self._check_cancelled(cancel_event)
        deadline = min(time.monotonic() + 3, payload.get("deadline", float("inf")))
        if time.monotonic() >= deadline:
            raise TimeoutError("Истекло время команды OFF/восстановления Arduino")
        self._write_line(f"VALVES {token} {state}{selection}")
        while time.monotonic() < deadline:
            if cancel_event is not None:
                self._check_cancelled(cancel_event)
            self._keepalive()
            line = self._read_line()
            if not line:
                continue
            self._log(f"< {line}")
            self._check_board_fault(line)
            fields = line.split()
            if len(fields) >= 3 and fields[1] == token:
                if fields[0] == "ERROR":
                    raise RuntimeError(f"Arduino: {line}")
                if fields[0] == "VALVES_ACK":
                    expected = [state, str(relay_mask)] if self.relay_selection_capable else [state]
                    if fields[2:] != expected:
                        raise RuntimeError("Arduino подтвердила другое состояние реле")
                    self._log(f"Реле: {state}, выбор={relay_mask} подтверждено Arduino.")
                    return {"switched": switched, "relay_mask": relay_mask}
        # Never enqueue a second command behind an unacknowledged ON command.
        # Worker-side timeout is shorter than the submit() timeout.
        raise TimeoutError("Нет подтверждения переключения реле D4/D5")

    def _trigger_and_wait(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.ready:
            raise ConnectionError("Arduino is not READY; connect it before starting automatic-arduino")
        run_id = str(payload["run_id"])
        point = int(payload["point"])
        cancel_event = payload.get("cancel_event")
        timeout_s = float(payload.get("timeout_s", self.settings["spectrum_timeout_s"]))
        token = f"{run_id} {point}"
        self._write_line(f"TRIGGER {token}")
        self._status(f"Точка {point}: команда запуска отправлена. Ожидание подтверждения Arduino.")
        acknowledged = False
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self._keepalive()
            if cancel_event is not None and cancel_event.is_set():
                try:
                    self._write_line(f"ABORT {token}")
                except Exception:
                    pass
                raise InterruptedError("Stopped while waiting for NMR acquisition TTL")
            line = self._read_line()
            if line is None:
                continue
            self._log(f"< {line}")
            self._check_board_fault(line)
            fields = line.split()
            if len(fields) < 3:
                continue
            kind, received_run, received_point = fields[:3]
            if received_run != run_id or received_point != str(point):
                self._log(f"Ignoring stale or foreign Arduino message for {received_run} {received_point}.")
                continue
            if kind == ARDUINO_TRIGGERED:
                acknowledged = True
                self._log(f"Точка {point}: Arduino подтвердила отправку TTL на ЯМР (D8).")
                self._status(f"Точка {point}: TTL отправлен. Ожидание ответа ЯМР, до {timeout_s:g} с.")
            elif kind == ARDUINO_NMR_DONE:
                if not acknowledged:
                    raise RuntimeError("Received NMR_DONE before matching TRIGGERED acknowledgement")
                self._log(f"Точка {point}: получен ответный TTL от ЯМР (D2). Запись спектра подтверждена.")
                self._status(f"Точка {point}: получен ответ ЯМР, спектр записан.")
                return {"run_id": run_id, "point": point}
            elif kind == "ERROR":
                raise RuntimeError(f"Arduino reported ERROR for point {point}")
        try:
            self._write_line(f"ABORT {token}")
        except Exception:
            pass
        raise TimeoutError(f"Point {point}: no matching NMR_DONE TTL within {timeout_s:g} s")

    def _handle(self, command: ArduinoCommand) -> None:
        try:
            if command.name == "connect":
                self._connect(command.payload or {})
            elif command.name == "trigger":
                command.result = self._trigger_and_wait(command.payload or {})
            elif command.name == "valves":
                command.result = self._set_valves(command.payload or {})
            elif command.name == "off_checkpoint":
                command.result = self._off_checkpoint(command.payload or {})
            elif command.name == "recover_off":
                command.result = self._recover_off(command.payload or {})
            elif command.name == "disconnect":
                if self.ready and self.relay_capable:
                    self._set_valves({"switched": False})
                self._close_port()
                self._status("Arduino отключена.")
            elif command.name == "stop":
                self.stopping.set()
            else:
                raise ValueError(f"Unknown Arduino command {command.name}")
        except Exception as exc:
            command.error = exc
            self._status(f"Ошибка Arduino: {exc}")
            self._log(f"Ошибка команды {command.name}: {exc}")
            if command.name in ("valves", "off_checkpoint", "recover_off"):
                # Stop renewing the host lease after an unconfirmed relay
                # command. Try OFF once; a broken link times out on the board.
                try:
                    self._write_line(f"VALVES {uuid4().hex[:16]} OFF")
                except Exception:
                    pass
                self.ready = False
                self.events.put(("valve_status", (0, None, None)))
        finally:
            if command.done:
                command.done.set()

    def submit(self, name: str, payload: dict[str, Any] | None = None, timeout_s: float = 12) -> Any:
        done = threading.Event()
        command = ArduinoCommand(name, payload, done)
        self.commands.put(command)
        if not done.wait(timeout_s):
            raise TimeoutError(f"Timed out waiting for Arduino {name}")
        if command.error:
            raise command.error
        return command.result

    def run(self) -> None:
        try:
            while not self.stopping.is_set():
                try:
                    self._handle(self.commands.get(timeout=0.05))
                except queue.Empty:
                    if self.ready and self.port is not None:
                        try:
                            self._keepalive()
                            line = self._read_line()
                            if line:
                                self._log(f"< {line}")
                                self._check_board_fault(line)
                        except Exception as exc:
                            self.ready = False
                            self.events.put(("valve_status", (0, None, None)))
                            self._status(f"Ошибка связи Arduino: {exc}")
                            self._log(f"Ошибка связи Arduino: {exc}")
        finally:
            if self.ready and self.relay_capable:
                try:
                    self._set_valves({"switched": False})
                except Exception as exc:
                    self._log(f"Не удалось подтвердить OFF при отключении: {exc}")
            self._close_port()

    def shutdown(self) -> None:
        self.commands.put(ArduinoCommand("stop"))


class ExperimentRunner(threading.Thread):
    def __init__(self, service: SerialService, plan: list[ExperimentPoint], h2_serial: str, alkene_serial: str | None, events: queue.Queue[tuple[str, Any]], arduino: ArduinoNMRService | None = None, arduino_timeout_s: float = 180.0, cycle: CycleTiming | None = None, valves: ArduinoValveController | None = None, recover_after_off: bool = False, recovery_policy: ArduinoRecoveryPolicy | None = None, relay_initial_on: bool = False):
        super().__init__(daemon=True)
        if type(relay_initial_on) is not bool:
            raise ValueError("Начальное состояние реле должно быть ON или OFF")
        self.service, self.plan, self.h2_serial, self.alkene_serial, self.events = service, plan, h2_serial, alkene_serial, events
        self.arduino, self.arduino_timeout_s = arduino, arduino_timeout_s
        self.cycle, self.valves = cycle, valves
        self.relay_initial_on = relay_initial_on
        self.recover_after_off = recover_after_off
        self.recovery_used = False
        self.recovery_policy = recovery_policy or ArduinoRecoveryPolicy()
        self.recovery_attempts_used = 0
        self.t4_interrupted_points: set[int] = set()
        self.cancelled = threading.Event()
        self.run_id = f"run-{uuid4().hex[:16]}"

    def cancel(self) -> None:
        self.cancelled.set()

    def _wait_delay(self, point: ExperimentPoint, name: str, seconds: float) -> None:
        self.events.put(("cycle_stage", (point.number, name, seconds)))
        started = time.monotonic()
        while True:
            if self.cancelled.is_set():
                raise InterruptedError("Stopped by operator")
            if self.arduino is not None and not getattr(self.arduino, "ready", True):
                raise ConnectionError("Arduino отключилась во время выдержки")
            elapsed = min(time.monotonic() - started, seconds)
            self.events.put(("cycle_progress", (point.number, name, elapsed, seconds)))
            if elapsed >= seconds:
                return
            self.cancelled.wait(min(0.2, seconds - elapsed))

    def _record_spectrum(self, point: ExperimentPoint, spectrum: int) -> None:
        if self.cancelled.is_set():
            raise InterruptedError("Stopped by operator")
        self.events.put(("nmr_trigger_wait", (point, spectrum)))
        # Each acquisition has a different protocol token. A late spectrum-1
        # response cannot acknowledge spectrum 2; the existing sketch is compatible.
        self.arduino.submit(
            "trigger",
            {"run_id": f"{self.run_id}-s{spectrum}", "point": point.number,
             "cancel_event": self.cancelled, "timeout_s": self.arduino_timeout_s},
            timeout_s=self.arduino_timeout_s + 10,
        )
        if self.cancelled.is_set():
            raise InterruptedError("Stopped by operator")
        self.events.put(("spectrum_confirmed", (point, spectrum)))

    def _recover_boundary(self, point: ExperimentPoint, controlled: dict[str, float], stage: str, exc: Exception) -> None:
        if not self.recover_after_off or self.arduino is None or self.valves is None:
            raise exc
        if self.cancelled.is_set():
            raise InterruptedError("Stopped by operator") from exc
        self.events.put(("recovery_status", (point.number, f"Сбой на {stage}: {exc}. Отправка нулевого потока…")))
        # Zero is mandatory even if this cycle's attempt budget is exhausted.
        self.service.submit("zero", controlled)
        remaining = self.recovery_policy.attempts - self.recovery_attempts_used
        if remaining <= 0:
            raise ConnectionError(f"Исчерпаны все попытки подключения к Arduino в цикле {point.number} ({self.recovery_policy.attempts}); эксперимент остановлен") from exc
        if self.cancelled.is_set():
            raise InterruptedError("Stopped by operator")
        policy = ArduinoRecoveryPolicy(remaining, self.recovery_policy.interval_s)
        budget_s = policy.budget_s(float(getattr(self.arduino, "settings", {}).get("startup_timeout_s", 8)))
        self.events.put(("recovery_status", (point.number, f"Нулевая уставка принята MFC. Пауза перед подключением: {policy.interval_s:g} с; осталось попыток в цикле: {remaining}.")))
        result = self.arduino.submit("recover_off", {
            "cancel_event": self.cancelled,
            "deadline": time.monotonic() + budget_s,
            "max_attempts": remaining,
            "retry_interval_s": policy.interval_s,
            "attempt_offset": self.recovery_attempts_used,
            "point": point.number,
        }, timeout_s=budget_s + 2)
        if self.cancelled.is_set():
            raise InterruptedError("Stopped by operator")
        if result.get("switched") is not False or result.get("relay_mask") != 3 or not self.arduino.ready:
            raise ConnectionError("Восстановление не подтвердило готовность Arduino и OFF обоих реле")
        used = result.get("attempts_used")
        if type(used) is not int or not 1 <= used <= remaining:
            raise RuntimeError("Некорректный счётчик попыток восстановления Arduino")
        self.recovery_attempts_used += used
        self.recovery_used = True
        self.valves.is_switched = False
        self.events.put(("valve_status", (point.number, False, 3)))
        self.events.put(("recovery_status", (point.number, f"Arduino подключена, оба реле OFF. Использовано попыток в цикле: {self.recovery_attempts_used}/{self.recovery_policy.attempts}.")))

    def _wait_initial_delay(self, point: ExperimentPoint, seconds: float, controlled: dict[str, float]) -> None:
        while True:
            try:
                self._wait_delay(point, "t1", seconds)
                return
            except InterruptedError:
                raise
            except (OSError, TimeoutError) as exc:
                self._recover_boundary(point, controlled, "t1", exc)
                if self.cancelled.is_set():
                    raise InterruptedError("Stopped by operator")
                if self.valves is not None and self.valves.is_switched is not self.relay_initial_on:
                    # Re-establish the designed first-spectrum state BEFORE gas
                    # is reapplied. A failed ON aborts; it is never auto-retried.
                    self.valves.set_switched(self.relay_initial_on, point.number)
                if self.cancelled.is_set():
                    raise InterruptedError("Stopped by operator")
                self.service.submit("set", controlled)
                self.events.put(("recovery_status", (point.number, "Поток задан заново. Выдержка t1 начинается с нуля; TTL ещё не отправлялся.")))

    def _wait_final_delay(self, point: ExperimentPoint, seconds: float, controlled: dict[str, float]) -> bool:
        try:
            self._wait_delay(point, "t4", seconds)
            return False
        except InterruptedError:
            raise
        except (OSError, TimeoutError) as exc:
            self._recover_boundary(point, controlled, "t4", exc)
            # A reset has interrupted the designed relay hold. Never silently
            # treat it as a normal t4 or re-energize a valve after both spectra.
            self.t4_interrupted_points.add(point.number)
            self.events.put(("recovery_status", (point.number, "ВНИМАНИЕ: t4 прервана. Оба спектра уже приняты; реле остаются OFF. Переход к следующей точке без повторной записи спектров.")))
            return True

    def _finish_valve_point(self, point: ExperimentPoint, controlled: dict[str, float], next_point: bool = False) -> None:
        """Called ONLY after both NMR acknowledgements and the full t4 delay."""
        if self.cancelled.is_set():
            raise InterruptedError("Stopped by operator")
        if self.relay_initial_on and next_point:
            # Only a fully completed t4 permits normal ON for the next point.
            # Keep this outside OFF recovery: a failed ON must stop the run.
            self.valves.set_switched(True, point.number)
            return
        try:
            self.valves.set_switched(False, point.number)
            if self.recover_after_off:
                self.events.put(("recovery_status", (point.number, "Проверка связи после OFF (1 с)…")))
                result = self.arduino.submit("off_checkpoint", {"cancel_event": self.cancelled}, timeout_s=5)
                if result != {"switched": False, "relay_mask": 3}:
                    raise RuntimeError("Нет подтверждения OFF обоих реле на границе точки")
        except InterruptedError:
            raise
        except (OSError, TimeoutError) as exc:
            self._recover_boundary(point, controlled, "завершающем OFF", exc)

    def run(self) -> None:
        controlled = {self.h2_serial: 0.0}
        if self.alkene_serial is not None:
            controlled[self.alkene_serial] = 0.0
        completed = False
        try:
            if self.valves is not None and self.arduino is None:
                raise ValueError("Valve mode requires the NMR/Arduino handshake")
            if self.cancelled.is_set():
                raise InterruptedError("Stopped by operator")
            if self.valves is not None:
                self.valves.set_switched(self.relay_initial_on and bool(self.plan), 0)
            for point_index, point in enumerate(self.plan):
                if self.cancelled.is_set():
                    raise InterruptedError("Stopped by operator")
                # One cycle is one plan point (t1, both spectra, t2–t4, boundary).
                # Repeated faults within the same t1 must share this budget.
                self.recovery_attempts_used = 0
                if self.valves is not None and self.valves.is_switched is not self.relay_initial_on:
                    # A recovered/interrupted t4 leaves both outputs OFF.
                    # Apply the first state explicitly at the next point.
                    self.valves.set_switched(self.relay_initial_on, point.number)
                if self.cancelled.is_set():
                    raise InterruptedError("Stopped by operator")
                controlled = {self.h2_serial: point.h2_flow}
                if self.alkene_serial is not None:
                    controlled[self.alkene_serial] = point.alkene_flow
                self.service.submit("set", controlled)
                started = time.monotonic()
                self.events.put(("stage", (point, started)))
                timing = self.cycle or CycleTiming(t1=point.dwell_s)
                self._wait_initial_delay(point, timing.t1, controlled)
                if self.arduino is not None:
                    self._record_spectrum(point, 1)
                    self._wait_delay(point, "t2", timing.t2)
                    if self.valves is not None:
                        if self.cancelled.is_set():
                            raise InterruptedError("Stopped by operator")
                        self.valves.set_switched(not self.relay_initial_on, point.number)
                    self._wait_delay(point, "t3", timing.t3)
                    self._record_spectrum(point, 2)
                    t4_interrupted = self._wait_final_delay(point, timing.t4, controlled)
                    if self.valves is not None and not t4_interrupted:
                        self._finish_valve_point(point, controlled, next_point=point_index + 1 < len(self.plan))
                if self.cancelled.is_set():
                    raise InterruptedError("Stopped by operator")
                event = "point_completed_warning" if point.number in self.t4_interrupted_points else "point_completed"
                self.events.put((event, point))
            completed = True
        except Exception as exc:
            self.events.put(("runner_error", str(exc)))
        finally:
            if self.valves is not None and self.valves.is_switched is not False:
                try:
                    self.valves.set_switched(False, 0)
                except Exception as exc:
                    self.events.put(("runner_error", f"Не удалось подтвердить OFF реле: {exc}"))
            try:
                self.service.submit("zero", controlled)
                if completed:
                    warning = (" Внимание: t4 прервана в точках " + ", ".join(map(str, sorted(self.t4_interrupted_points))) + ".") if self.t4_interrupted_points else ""
                    self.events.put(("finished", "План завершён. Приборы приняли команду нулевого потока." + warning))
            except Exception as exc:
                self.events.put(("runner_error", f"Не удалось задать нулевой поток: {exc}"))


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

    def set_data(self, plan: list[ExperimentPoint] | None, h2_serial: str | None, alkene_serial: str | None, devices: dict[str, DeviceInfo], current_point: int | None, extra_delay_s: float = 0.0) -> None:
        self.extra_delay_s = extra_delay_s
        self.plan = list(plan or [])
        self.h2_serial, self.alkene_serial = h2_serial, alkene_serial
        self.devices, self.current_point = dict(devices), current_point
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        width, height = max(self.winfo_width(), 420), max(self.winfo_height(), 220)
        if not self.plan:
            self.create_text(width / 2, height / 2, text="Проверьте план эксперимента, чтобы увидеть график.", fill="#4b5563", font=("Segoe UI", 11))
            return
        left, right, top, bottom = 68, 24, 62, 70
        chart_width, chart_height = width - left - right, height - top - bottom
        maximum = max(max(point.h2_flow, point.alkene_flow) for point in self.plan)
        maximum = max(1.0, maximum * 1.12)
        primary = self.devices.get(self.h2_serial or "")
        unit = primary.unit if primary is not None else "единицы прибора"
        self.create_text(left, 12, text=f"Поток, {unit}", anchor="w", fill="#334155", font=("Segoe UI", 9))
        self.create_line(left, top, left, top + chart_height, fill="#64748b")
        self.create_line(left, top + chart_height, left + chart_width, top + chart_height, fill="#64748b")
        for index in range(5):
            value = maximum * index / 4
            y = top + chart_height - chart_height * index / 4
            self.create_line(left, y, left + chart_width, y, fill="#e5e7eb")
            self.create_text(left - 7, y, text=f"{value:g}", anchor="e", fill="#475569", font=("Segoe UI", 8))
        count = len(self.plan)
        durations = [point.dwell_s + getattr(self, "extra_delay_s", 0.0) for point in self.plan]
        zero_delays = not any(durations)
        if zero_delays:
            durations = [1.0] * len(self.plan)
        total_time = sum(durations)
        x_for = lambda elapsed: left + chart_width * (elapsed / max(total_time, 1.0))
        y_for = lambda value: top + chart_height - chart_height * value / maximum
        for index in range(5):
            elapsed = total_time * index / 4
            x = x_for(elapsed)
            self.create_line(x, top + chart_height, x, top + chart_height + 4, fill="#64748b")
            self.create_text(x, top + chart_height + 16, text=f"{elapsed:g}", fill="#475569", font=("Segoe UI", 8))
        axis_label = "Номер точки (все задержки равны нулю)" if zero_delays else "Сумма задержек t1–t4, с · время ЯМР не включено"
        self.create_text(left + chart_width / 2, height - 10, text=axis_label, fill="#334155", font=("Segoe UI", 9))
        primary = self.devices.get(self.h2_serial or "")
        primary_label = "Поток MFC" if self.alkene_serial is None else "Поток H₂"
        if self.alkene_serial is None and primary is not None:
            primary_label = f"{primary.serial_number} · заданный поток"
        series: list[tuple[str, str, list[float]]] = [(primary_label, "#2563eb", [point.h2_flow for point in self.plan])]
        if self.alkene_serial is not None:
            series.append(("Поток алкена", "#d97706", [point.alkene_flow for point in self.plan]))
        for series_index, (label, color, values) in enumerate(series):
            coordinates: list[float] = []
            elapsed = 0.0
            for duration, value in zip(durations, values):
                coordinates.extend((x_for(elapsed), y_for(value), x_for(elapsed + duration), y_for(value)))
                elapsed += duration
            self.create_line(*coordinates, fill=color, width=2)
            legend_x = left + series_index * 145
            self.create_line(legend_x, 38, legend_x + 20, 38, fill=color, width=2)
            self.create_text(legend_x + 25, 38, text=label, anchor="w", fill="#334155", font=("Segoe UI", 9))
        if self.current_point is not None and 1 <= self.current_point <= count:
            elapsed = sum(durations[:self.current_point - 1])
            x = x_for(elapsed)
            self.create_line(x, top, x, top + chart_height, fill="#16a34a", dash=(3, 3))
            self.create_text(x + 4, top + 8, text=f"Точка {self.current_point}", anchor="w", fill="#166534", font=("Segoe UI", 9))
        readings: list[str] = []
        for serial_number, device in sorted(self.devices.items()):
            measured = "—" if device.measured_flow is None else f"{device.measured_flow:.4g} {device.unit}"
            readings.append(f"{serial_number}: измерено {measured}")
        if readings:
            self.create_text(left, height - 28, text="   |   ".join(readings), anchor="w", fill="#0f766e", font=("Segoe UI", 9))


class ManualFlowCard(ttk.LabelFrame):
    """One persistent editor per serial number; polling never replaces typed input."""

    def __init__(self, master: tk.Misc, serial_number: str, set_flow: Any, zero_flow: Any):
        super().__init__(master, text=f"MFC {serial_number}", padding=8)
        self.flow_var = tk.StringVar(self, value="0")
        self.gas_var = tk.StringVar(self)
        self.unit_var = tk.StringVar(self)
        self.measured_var = tk.StringVar(self)
        self.setpoint_var = tk.StringVar(self)
        self.command_var = tk.StringVar(self, value="Поток из этого окна ещё не задавался")
        ttk.Label(self, textvariable=self.gas_var).pack(anchor="w")
        ttk.Label(self, textvariable=self.measured_var, font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(6, 3))
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
        self.gas_var.set(f"Газ: {device.fluid_name}   ·   {mode_label}")
        self.unit_var.set(device.unit)
        measured = "нет данных" if device.measured_flow is None else f"{device.measured_flow:.6g} {device.unit}"
        setpoint = "нет данных" if device.setpoint_flow is None else f"{device.setpoint_flow:.6g} {device.unit}"
        self.measured_var.set(f"Текущий поток: {measured}")
        self.setpoint_var.set(f"Заданный поток (ответ прибора): {setpoint}")

    def set_enabled(self, enabled: bool) -> None:
        for widget in (self.entry, self.set_button, self.zero_button):
            widget.configure(state="normal" if enabled else "disabled")


class GasLineApp(tk.Tk):
    def __init__(self, store: ConfigStore):
        super().__init__()
        self.title("Bronkhorst — управление потоком")
        self.minsize(780, 540)
        self.geometry(f"{min(1180, self.winfo_screenwidth() - 60)}x{min(820, self.winfo_screenheight() - 100)}")
        configure_style(self)
        self.store, self.events = store, queue.Queue()
        self.devices: dict[str, DeviceInfo] = {}
        self.roles = dict(store.roles)
        self.device_modes = dict(store.device_modes)
        self.service = SerialService(store.serial, store.discovery, self.events)
        default_arduino = {"port": "", "baudrate": 115200, "spectrum_timeout_s": 180.0, "startup_timeout_s": 8.0}
        self.arduino_settings = {**default_arduino, **getattr(store, "arduino", {})}
        self.arduino = ArduinoNMRService(self.arduino_settings, self.events)
        self.runner: ExperimentRunner | None = None
        self.control_enabled = tk.BooleanVar(value=False)
        self.operation_mode = tk.StringVar(value="automatic-arduino")
        self.operation_mode_hint = tk.StringVar()
        self.available_port_var = tk.StringVar(value="")
        self.arduino_port_var = tk.StringVar(value=str(self.arduino_settings["port"]))
        self.arduino_baudrate_var = tk.StringVar(value=str(self.arduino_settings["baudrate"]))
        self.arduino_timeout_var = tk.StringVar(value=str(self.arduino_settings["spectrum_timeout_s"]))
        self.arduino_status_var = tk.StringVar(value="Arduino не подключена.")
        self.arduino_connecting = False
        self.relay_test_pending = False
        self.relay_test_cancel: threading.Event | None = None
        self.relay_test_mode = self.operation_mode.get()
        self.relay_test_status_var = tk.StringVar(value="Проверка реле: подключите Arduino. Расходомер не требуется.")
        self.manual_valve_pending = False
        self.manual_valve_cancel: threading.Event | None = None
        self.manual_valve_requires_off = False
        self.manual_valve_status_var = tk.StringVar(value="Подключите Arduino. Состояние реле не подтверждено.")
        self.manual_cards: dict[str, ManualFlowCard] = {}
        self.role_device_var, self.role_var = tk.StringVar(value="Нет приборов"), tk.StringVar(value="Unassigned")
        self.device_mode_var = tk.StringVar(value="Read flow")
        self.fluidset_var = tk.StringVar(value="")
        self.expected_gas_var = tk.StringVar(value="")
        self.inlet_pressure_var = tk.StringVar(value="")
        self.outlet_pressure_var = tk.StringVar(value="")
        self.fluid_temperature_var = tk.StringVar(value="")
        self.reference_pressure_var = tk.StringVar(value="")
        self.fluid_conditions_var = tk.StringVar(value="Выберите подключённый прибор в таблице.")
        self._conditions_snapshot: tuple[Any, ...] | None = None
        saved_design = getattr(store, "experiment", {})
        self.recover_after_off_var = tk.BooleanVar(value=saved_design.get("recover_after_off") is True)
        self.recovery_attempts_var = tk.StringVar(value=str(saved_design.get("recovery_attempts", 3)))
        self.recovery_interval_var = tk.StringVar(value=str(saved_design.get("recovery_interval_s", 1.0)))
        saved_relay_mask = str(saved_design.get("relay_mask", 1))
        self.relay_selection_var = tk.StringVar(value=saved_relay_mask if saved_relay_mask in RELAY_SELECTION_LABELS else "1")
        self.relay_initial_on_var = tk.BooleanVar(value=saved_design.get("relay_initial_on") is True)
        self.relay_cycle_hint_var = tk.StringVar()
        self.relay_wiring_var = tk.StringVar()
        self.plan_vars = {key: tk.StringVar(value=str(saved_design.get(key, value))) for key, value in {"start": 10, "stop": 150, "step": 10, "ratio": 4}.items()}
        saved_plan_mode = saved_design.get("plan_mode", "range")
        self.plan_mode_var = tk.StringVar(value=saved_plan_mode if saved_plan_mode in ("range", "custom") else "range")
        self.custom_flows_var = tk.StringVar(value=str(saved_design.get("custom_flows", "")))
        self.point_repeats_var = tk.StringVar(value=str(saved_design.get("point_repeats", 1)))
        self.cycle_vars = {key: tk.StringVar(value=str(saved_design.get(key, value))) for key, value in CYCLE_DEFAULTS.items()}
        # The existing gas-plan model calls the initial stabilization time dwell.
        self.plan_vars["dwell"] = self.cycle_vars["t1"]
        self.design_summary_var = tk.StringVar()
        self.design_overview_var = tk.StringVar()
        self.design_status_var = tk.StringVar(value="Настройте потоки и задержки, затем сохраните дизайн.")
        self.valve_status_var = tk.StringVar(value="Клапан: не используется в этом режиме.")
        self.active_cycle_stage: str | None = None
        self.status_var = tk.StringVar(value="Выберите COM-порт и подключите расходомер.")
        self.port_warning_var = tk.StringVar(value="")
        self.plan_var = tk.StringVar(value="Задайте диапазон потока, шаг и время на каждой точке.")
        self.progress_var = tk.StringVar(value="Эксперимент не запущен.")
        self.active_plan: list[ExperimentPoint] = []
        self.plan_h2_serial: str | None = None
        self.plan_alkene_serial: str | None = None
        self.current_plan_point: int | None = None
        self.graph_window: tk.Toplevel | None = None
        self.flow_plot: ExperimentPlot | None = None
        self.manual_setpoints: dict[str, float] = {}
        self.manual_commands_pending: set[str] = set()
        self._connection_log_lines: list[str] = []
        self.short_logs_var = tk.BooleanVar(value=False)
        self.console_mode_var = tk.StringVar(value="Журнал · все сообщения")
        self._build()
        self.short_logs_var.trace_add("write", self._refresh_console)
        for variable in {**self.plan_vars, **self.cycle_vars}.values():
            if variable is not self.plan_vars["dwell"]:
                variable.trace_add("write", self._design_changed)
        self.cycle_vars["t1"].trace_add("write", self._design_changed)
        self.relay_selection_var.trace_add("write", self._relay_selection_changed)
        self.relay_initial_on_var.trace_add("write", self._relay_sequence_changed)
        self.plan_mode_var.trace_add("write", self._plan_source_changed)
        self.custom_flows_var.trace_add("write", self._design_changed)
        self.point_repeats_var.trace_add("write", self._design_changed)
        self.recover_after_off_var.trace_add("write", self._design_changed)
        self.recovery_attempts_var.trace_add("write", self._design_changed)
        self.recovery_interval_var.trace_add("write", self._design_changed)
        self._update_design_summary()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.service.start()
        self.arduino.start()
        self._refresh_available_ports()
        self.after(100, self._consume_events)

    def _build(self) -> None:
        build_interface(self)

    def _open_design_window(self) -> None:
        self._update_operation_mode_controls()
        self.design_window.deiconify()
        self.design_window.lift()
        self.cycle_diagram.redraw()

    def _focus_cycle_delay(self, name: str) -> None:
        entry = self.cycle_entries[name]
        entry.focus_set()
        entry.selection_range(0, "end")

    def _current_cycle(self) -> CycleTiming:
        try:
            values = {key: float(var.get().replace(",", ".")) for key, var in self.cycle_vars.items()}
        except ValueError as exc:
            raise ValueError("Задержки t1–t4 должны быть числами (в секундах)") from exc
        return CycleTiming(**values)

    def _selected_relay_mask(self) -> int:
        value = self.relay_selection_var.get()
        if value not in RELAY_SELECTION_LABELS:
            raise ValueError("Выберите одно реле (D4 или D5) либо два реле")
        return int(value)

    def _current_recovery_policy(self) -> ArduinoRecoveryPolicy:
        try:
            return ArduinoRecoveryPolicy(int(self.recovery_attempts_var.get()),
                                         float(self.recovery_interval_var.get().replace(",", ".")))
        except (TypeError, ValueError) as exc:
            raise ValueError("Попытки Arduino: целое число 1–100; интервал: 0,1–60 с") from exc

    def _relay_selection_changed(self, *_args) -> None:
        self.valve_status_var.set("Выбор реле изменён; состояние ещё не подтверждено.")
        self._design_changed()
        self._update_operation_mode_controls()
        self.cycle_diagram.redraw()

    def _relay_sequence_changed(self, *_args) -> None:
        self._design_changed()
        self._update_operation_mode_controls()

    def _plan_source_changed(self, *_args) -> None:
        self._design_changed()
        self._update_operation_mode_controls()

    def _current_point_repeats(self) -> int:
        try:
            repeats = int(self.point_repeats_var.get().strip())
        except ValueError as exc:
            raise ValueError(f"Повтор каждой точки: целое число от 1 до {MAX_PLAN_POINTS}") from exc
        if not 1 <= repeats <= MAX_PLAN_POINTS:
            raise ValueError(f"Повтор каждой точки: целое число от 1 до {MAX_PLAN_POINTS}")
        return repeats

    def _relay_cycle_description(self) -> str:
        if self.relay_initial_on_var.get():
            return "Сначала ON; после t2 → OFF; после t4 → ON только для следующей точки. Конец опыта / Стоп / ошибка → OFF."
        return "Сначала OFF; после t2 → ON; после t4 → OFF. Конец опыта / Стоп / ошибка → OFF."

    def _request_relay_test(self, pulse: bool = True) -> None:
        if self.manual_valve_pending or (pulse and self.manual_valve_requires_off):
            if not pulse:
                self._request_manual_valve(False)
            else:
                self.relay_test_status_var.set("Сначала завершите ручную команду и выключите реле.")
            return
        if self.runner and self.runner.is_alive():
            self.relay_test_status_var.set("Проверка недоступна: эксперимент выполняется.")
            return
        if self.relay_test_pending:
            if not pulse and self.relay_test_cancel is not None:
                self.relay_test_cancel.set()
                self.relay_test_status_var.set("Запрошено выключение всех реле; ожидание ответа Arduino…")
            return
        if self.arduino_connecting or not self.arduino.ready:
            self.relay_test_status_var.set("Сначала подключите Arduino и дождитесь READY.")
            return
        if not self.arduino.relay_capable or (pulse and not self.arduino.relay_selection_capable):
            self.relay_test_status_var.set("Обновите прошивку Arduino: нужна поддержка реле и выбора каналов RELAY_MASK.")
            return
        mask = self._selected_relay_mask() if pulse else 3
        self.relay_test_pending = True
        self.relay_test_mode = self.operation_mode.get()
        cancel_event = threading.Event()
        self.relay_test_cancel = cancel_event
        self.relay_test_status_var.set("Проверка: OFF → ON на 1 с → OFF…" if pulse else "Выключение всех реле…")
        self._append_connection_log(f"{time.strftime('%H:%M:%S')}  RELAY TEST: {'pulse 1 s' if pulse else 'all OFF'}, mask={mask}.")
        self._update_operation_mode_controls()
        threading.Thread(target=self._run_relay_test, args=(mask, pulse, cancel_event), daemon=True).start()

    def _run_relay_test(self, mask: int, pulse: bool, cancel_event: threading.Event) -> None:
        controller = ArduinoValveController(self.arduino, self.events, mask)
        error = None
        try:
            if pulse:
                controller.set_switched(False, 0)
                if not cancel_event.is_set():
                    controller.set_switched(True, 0)
                    cancel_event.wait(1.0)
        except Exception as exc:
            error = str(exc)
        finally:
            try:
                # Always request OFF, including after ON failed or was cancelled.
                controller.set_switched(False, 0)
            except Exception as exc:
                detail = f"Не подтверждено выключение реле: {exc}"
                error = f"{error}; {detail}" if error else detail
            self.events.put(("relay_test_finished", (pulse, cancel_event.is_set(), error)))

    def _request_manual_valve(self, switched: bool) -> None:
        if self.operation_mode.get() != "manual" or (self.runner and self.runner.is_alive()):
            self.manual_valve_status_var.set("Управление клапаном доступно только в ручном режиме без эксперимента.")
            return
        if self.manual_valve_pending:
            if not switched and self.manual_valve_cancel is not None:
                self.manual_valve_cancel.set()
                self.manual_valve_status_var.set("Запрошен OFF после текущей команды; ожидание ответа Arduino…")
            return
        if self.relay_test_pending:
            if not switched:
                self._request_relay_test(False)
            return
        if self.arduino_connecting or not self.arduino.ready:
            self.manual_valve_status_var.set("Сначала подключите Arduino и дождитесь READY.")
            return
        if not self.arduino.relay_capable or (switched and not self.arduino.relay_selection_capable):
            self.manual_valve_status_var.set("Нужна основная прошивка Arduino TTL + реле с поддержкой RELAY_MASK.")
            return
        if switched and self.manual_valve_requires_off:
            self.manual_valve_status_var.set("Сначала отправьте OFF и дождитесь подтверждения.")
            return
        mask = self._selected_relay_mask() if switched else 3
        self.manual_valve_pending = True
        # Only a confirmed OFF clears this interlock, not reconnect/READY.
        self.manual_valve_requires_off = True
        cancel = threading.Event()
        self.manual_valve_cancel = cancel
        self.manual_valve_status_var.set("Отправка ON выбранным реле…" if switched else "Отправка OFF обоим реле…")
        self._append_connection_log(f"{time.strftime('%H:%M:%S')}  MANUAL VALVES: {'ON' if switched else 'OFF'}, mask={mask}.")
        self._update_operation_mode_controls()
        threading.Thread(target=self._run_manual_valve, args=(switched, mask, cancel), daemon=True).start()

    def _run_manual_valve(self, switched: bool, mask: int, cancel: threading.Event) -> None:
        error = None
        try:
            if switched and cancel.is_set():
                ArduinoValveController(self.arduino, self.events, 3).set_switched(False, 0)
            else:
                ArduinoValveController(self.arduino, self.events, mask).set_switched(switched, 0)
                if switched and cancel.is_set():
                    ArduinoValveController(self.arduino, self.events, 3).set_switched(False, 0)
        except Exception as exc:
            error = str(exc)
            if switched:
                try:
                    ArduinoValveController(self.arduino, self.events, 3).set_switched(False, 0)
                except Exception as cleanup_exc:
                    error += f"; OFF не подтверждён: {cleanup_exc}"
        finally:
            self.events.put(("manual_valve_finished", error))

    def _update_design_summary(self) -> None:
        self.relay_cycle_hint_var.set(self._relay_cycle_description())
        if self.plan_mode_var.get() == "custom":
            flows = " ".join(self.custom_flows_var.get().split())
            flow_summary = "Список потоков: " + (flows[:85] + "…" if len(flows) > 85 else flows or "не задан")
        else:
            flow_summary = f"Поток: {self.plan_vars['start'].get()} → {self.plan_vars['stop'].get()}, шаг {self.plan_vars['step'].get()}"
        self.design_summary_var.set(
            flow_summary + f" · повтор каждой точки ×{self.point_repeats_var.get()}\n"
            + " · ".join(f"{key} = {var.get()} с" for key, var in self.cycle_vars.items())
            + "\nДва спектра на точку; время записи добавляется к задержкам."
            + "\n" + RELAY_SELECTION_LABELS.get(self.relay_selection_var.get(), "Выберите реле")
            + "\n" + self._relay_cycle_description()
            + (f"\nВосстановление Arduino на t1/t4/OFF: {self.recovery_attempts_var.get()} попыток на цикл, пауза перед каждой {self.recovery_interval_var.get()} с."
               if self.recover_after_off_var.get() else "")
        )

    def _design_changed(self, *_args) -> None:
        self._update_design_summary()
        self.design_status_var.set("Дизайн изменён. Сохраните настройки для следующего запуска программы.")
        if not (self.runner and self.runner.is_alive()):
            self.active_plan = []
            self.current_plan_point = None
            for item in self.schedule_tree.get_children():
                self.schedule_tree.delete(item)
            self.run_progress.configure(value=0)
            self.completed_points_var.set("План изменён; требуется проверка")
            self.plan_var.set("План изменён. Нажмите «Проверить план».")
            self._redraw_experiment_view()

    def _save_experiment_design(self) -> None:
        if self.runner and self.runner.is_alive():
            return
        try:
            cycle = self._current_cycle()
            # Saving the design does not require connected devices.
            self._current_plan(single_mfc=len(self.devices) <= 1)
            settings: dict[str, Any] = {
                "plan_mode": self.plan_mode_var.get(), "custom_flows": self.custom_flows_var.get(),
                "point_repeats": self._current_point_repeats(),
            }
            # Inactive fields must not invalidate a custom plan or one-MFC run.
            # Preserve their text so switching back never silently changes it.
            for key in ("start", "stop", "step", "ratio"):
                text = self.plan_vars[key].get()
                try:
                    value = float(text.replace(",", "."))
                    settings[key] = value if math.isfinite(value) else text
                except ValueError:
                    settings[key] = text
            settings.update({key: getattr(cycle, key) for key in CYCLE_DEFAULTS})
            settings["relay_mask"] = self._selected_relay_mask()
            settings["relay_initial_on"] = self.relay_initial_on_var.get()
            settings["recover_after_off"] = self.recover_after_off_var.get()
            recovery_policy = self._current_recovery_policy()
            settings["recovery_attempts"] = recovery_policy.attempts
            settings["recovery_interval_s"] = recovery_policy.interval_s
            self.store.save_experiment(settings)
            self.design_status_var.set("Дизайн сохранён в конфигурации. Перед запуском план проверяется по подключённым MFC.")
            self.status_var.set("Настройки сохранены: " + RELAY_SELECTION_LABELS[str(settings["relay_mask"])])
        except Exception as exc:
            self.design_status_var.set("Не удалось сохранить дизайн: " + str(exc))
            self.status_var.set(self.design_status_var.get())

    def _update_available_ports(self) -> None:
        ports = sorted(
            item.device for item in list_ports.comports()
            if self.store.discovery["include_non_usb"] or item.vid is not None
        )
        self.available_port_box["values"] = ports
        if self.available_port_var.get() not in ports:
            self.available_port_var.set(ports[0] if ports else "")
        self.arduino_port_box["values"] = ports
        self.manual_arduino_port_box["values"] = ports
        if self.arduino_port_var.get() not in ports:
            self.arduino_port_var.set(ports[0] if ports else "")

    def _refresh_available_ports(self) -> None:
        self._update_available_ports()
        if self.winfo_exists():
            self.after(2000, self._refresh_available_ports)

    def _operation_mode_changed(self) -> None:
        if self.manual_valve_pending or (self.manual_valve_requires_off and self.operation_mode.get() != "manual"):
            self.operation_mode.set("manual")
            self.manual_valve_status_var.set("Перед сменой режима выключите реле и дождитесь подтверждения OFF.")
            self._update_operation_mode_controls()
            return
        if self.relay_test_pending:
            self.operation_mode.set(self.relay_test_mode)
            self._update_operation_mode_controls()
            return
        if self.runner and self.runner.is_alive():
            self.operation_mode.set("automatic-arduino-valves" if self.runner.valves is not None else "automatic-arduino")
            messagebox.showwarning("Эксперимент выполняется", "Остановите эксперимент перед переходом к ручному управлению.")
        elif self.operation_mode.get() in AUTOMATIC_MODES and (
            self.manual_commands_pending or any(flow > 0 for flow in self.manual_setpoints.values())
        ):
            self.operation_mode.set("manual")
            messagebox.showwarning(
                "Есть заданный поток",
                "Дождитесь выполнения команды и задайте ноль всем MFC перед сменой режима.",
            )
        self._update_operation_mode_controls()

    def _update_operation_mode_controls(self) -> None:
        automatic = self.operation_mode.get() in AUTOMATIC_MODES
        arduino_mode = automatic
        valves_mode = self.operation_mode.get() == "automatic-arduino-valves"
        running = bool(self.runner and self.runner.is_alive())
        busy = running or self.relay_test_pending or self.manual_valve_pending
        selection_locked = busy or self.manual_valve_requires_off
        self.relay_selection_box.configure(state="disabled" if selection_locked else "readonly")
        self.manual_relay_selection_box.configure(state="disabled" if selection_locked else "readonly")
        self.save_relay_selection_button.configure(state="disabled" if busy else "normal")
        self.relay_initial_on_checkbutton.configure(state="normal" if valves_mode and not busy else "disabled")
        if getattr(self.cycle_diagram, "relay_initial_on", False) != self.relay_initial_on_var.get():
            self.cycle_diagram.relay_initial_on = self.relay_initial_on_var.get()
            self.cycle_diagram.redraw()
        self.recovery_checkbutton.configure(state="normal" if valves_mode and not busy else "disabled")
        for entry in self.recovery_setting_entries:
            entry.configure(state="normal" if valves_mode and not busy else "disabled")
        can_test = self.arduino.ready and self.arduino.relay_selection_capable and not self.arduino_connecting and not busy and not self.manual_valve_requires_off
        self.relay_test_button.configure(state="normal" if can_test else "disabled")
        can_off = not running and (self.relay_test_pending or (
            self.arduino.ready and self.arduino.relay_capable and not self.arduino_connecting))
        self.relay_off_button.configure(state="normal" if can_off else "disabled")
        manual_ready = not automatic and not running and not self.arduino_connecting and self.arduino.ready
        can_manual_on = manual_ready and self.arduino.relay_capable and self.arduino.relay_selection_capable and not busy and not self.manual_valve_requires_off
        self.manual_valve_on_button.configure(state="normal" if can_manual_on else "disabled")
        self.manual_valve_off_button.configure(state="normal" if manual_ready and self.arduino.relay_capable else "disabled")
        self.manual_arduino_connect_button.configure(state="disabled" if busy or self.arduino_connecting or (self.manual_valve_requires_off and self.arduino.ready) else "normal")
        self.manual_arduino_disconnect_button.configure(state="disabled" if busy or self.arduino_connecting or self.manual_valve_requires_off else "normal")
        self.manual_arduino_port_box.configure(state="disabled" if busy or self.arduino_connecting else "readonly")
        relay_mask = self._selected_relay_mask()
        self.cycle_diagram.relay_label = RELAY_SELECTION_LABELS[str(relay_mask)]
        self.relay_wiring_var.set({
            1: "D4 → IN1 переключается; D5 → IN2 всегда HIGH (OFF).",
            2: "D5 → IN2 переключается; D4 → IN1 всегда HIGH (OFF).",
            3: "D4 → IN1 и D5 → IN2 переключаются вместе.",
        }[relay_mask] + " LOW = ON, HIGH = OFF.")
        if running:
            topology = "single" if self.plan_alkene_serial is None else "pair"
        elif not self.devices:
            topology = "none"
        elif self._single_mfc_automatic_setup():
            topology = "single"
        else:
            topology = "pair"
        # With no MFC connected do not suggest a fictitious H2/alkene pair.
        # Re-layout only when the mode or actual MFC topology changes: polling
        # Arduino must never make the plan table visibly jump.
        single_mfc = topology in {"none", "single"}
        layout = (automatic, valves_mode, topology)
        if getattr(self, "_mode_layout", None) != layout:
            for frame in (self.arduino_frame, self.plan_view_frame, self.manual_frame, self.manual_valve_frame):
                frame.pack_forget()
            if automatic:
                self.experiment_columns.pack(fill="both", expand=True, before=self.recent_events_frame)
                if arduino_mode:
                    self.arduino_frame.pack(fill="x", pady=(0, 10), before=self.design_summary_frame)
                self.plan_view_frame.pack(fill="x", pady=(0, 8))
                self.auto_controls.pack(fill="x", before=self.progress_label)
            else:
                self.experiment_columns.pack_forget()
                self.manual_valve_frame.pack(fill="x", pady=(0, 8), before=self.recent_events_frame)
                self.manual_frame.pack(fill="x", pady=(0, 8), before=self.recent_events_frame)
                self.auto_controls.pack_forget()
            self._mode_layout = layout
            self.cycle_diagram.valves_enabled = valves_mode
            self.cycle_diagram.redraw()
            self.valve_status_var.set("Реле D4/D5: состояние ещё не подтверждено." if valves_mode else "Реле: не используются в этом режиме.")
        for widget in (self.plan_field_labels["ratio"], self.plan_field_entries["ratio"]):
            widget.grid_remove() if single_mfc else widget.grid()
        plan_title = {
            "none": "План подачи газа",
            "single": "План подачи газа · один MFC",
            "pair": "План подачи газа · смесь H₂ / алкен",
        }[topology]
        self.plan_frame.configure(text=plan_title)
        custom = self.plan_mode_var.get() == "custom"
        self.plan_mode_box.configure(state="disabled" if running else "readonly")
        for key in ("start", "stop", "step"):
            for widget in (self.plan_field_labels[key], self.plan_field_entries[key]):
                widget.grid_remove() if custom else widget.grid()
        for widget in (self.custom_flows_label, self.custom_flows_entry, self.custom_flows_note):
            widget.grid() if custom else widget.grid_remove()
        for widget in self.plan_input_widgets:
            enabled = not running and not (single_mfc and widget is self.plan_field_entries["ratio"])
            if custom and widget in tuple(self.plan_field_entries[key] for key in ("start", "stop", "step")):
                enabled = False
            if widget is self.custom_flows_entry and not custom:
                enabled = False
            widget.configure(state="normal" if enabled else "disabled")
        self.validate_plan_button.configure(state="normal" if bool(self.devices) and not running else "disabled")
        self.save_design_button.configure(state="disabled" if running else "normal")
        self.start_experiment_button.configure(state="normal" if automatic and self.devices and not busy and not self.manual_valve_requires_off else "disabled")
        self.stop_experiment_button.configure(state="normal" if automatic and running else "disabled")
        for button in self.mode_buttons:
            button.configure(state="disabled" if busy else "normal")
        for serial_number, card in self.manual_cards.items():
            card.set_enabled(not automatic and self.device_modes.get(serial_number, "Read flow") == "Set flow"
                             and serial_number not in self.manual_commands_pending)
        units = sorted({device.unit for device in self.devices.values()})
        self.plan_unit_var.set("Поток: " + " / ".join(units) if units else "Единицы потока появятся после подключения MFC.")
        if automatic:
            base = "Один MFC — весь заданный поток." if single_mfc else "Смесь H₂ / алкен в заданном соотношении."
            if topology == "none":
                base = "Для опыта сначала подключите хотя бы один MFC на вкладке «Подключение»."
            detail = "Два спектра на точку · подтверждение TTL."
            if valves_mode:
                detail += " Реле переключаются по дизайну."
            self.operation_mode_hint.set(base + " " + detail)
        elif self.devices:
            self.operation_mode_hint.set("Введите поток у нужного прибора и нажмите «Установить». «Ноль» прекращает подачу через этот MFC.")
        else:
            self.operation_mode_hint.set("Реле доступны через Arduino без MFC. Для ручной подачи потока подключите расходомер на вкладке «Подключение».")
        refresh_experiment_presentation(self, automatic=automatic, valves_mode=valves_mode)

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

    def _console_line_visible(self, message: str) -> bool:
        return not self.short_logs_var.get() or not is_console_detail(message)

    def _visible_console_lines(self) -> list[str]:
        return [message for message in self._connection_log_lines if self._console_line_visible(message)]

    def _insert_console_line(self, message: str) -> None:
        tag = "error" if any(word in message.lower() for word in ("error", "failed", "ошибк")) else "ttl" if "TTL" in message or "NMR_DONE" in message else ""
        self.connection_log.insert("end", message + "\n", (tag,) if tag else ())

    def _update_recent_console(self) -> None:
        recent = []
        for message in reversed(self._connection_log_lines):
            if self._console_line_visible(message):
                recent.append(message)
                if len(recent) == 3:
                    break
        self.recent_log.configure(state="normal")
        self.recent_log.delete("1.0", "end")
        self.recent_log.insert("end", "\n".join(reversed(recent)))
        self.recent_log.configure(state="disabled")

    def _refresh_console(self, *_args) -> None:
        # Switching the view never deletes the full diagnostic history.
        at_bottom = self.connection_log.yview()[1] >= 0.99
        position = self.connection_log.yview()[0]
        self.connection_log.configure(state="normal")
        self.connection_log.delete("1.0", "end")
        for message in self._visible_console_lines():
            self._insert_console_line(message)
        self.connection_log.configure(state="disabled")
        if at_bottom:
            self.connection_log.see("end")
        else:
            self.connection_log.yview_moveto(position)
        self.console_mode_var.set("Журнал · важные события" if self.short_logs_var.get() else "Журнал · все сообщения")
        self._update_recent_console()

    def _append_connection_log(self, message: str) -> None:
        self._connection_log_lines.append(message)
        excess = max(0, len(self._connection_log_lines) - 2000)
        removed = self._connection_log_lines[:excess]
        if excess:
            del self._connection_log_lines[:excess]
        removed_rows = sum(line.count("\n") + 1 for line in removed if self._console_line_visible(line))
        at_bottom = self.connection_log.yview()[1] >= 0.99
        self.connection_log.configure(state="normal")
        if removed_rows:
            self.connection_log.delete("1.0", f"{removed_rows + 1}.0")
        if self._console_line_visible(message):
            self._insert_console_line(message)
        if at_bottom:
            self.connection_log.see("end")
        self.connection_log.configure(state="disabled")
        self._update_recent_console()

    def _copy_console(self) -> None:
        self.clipboard_clear()
        self.clipboard_append("\n".join(self._visible_console_lines()))
        self.status_var.set(("Короткий" if self.short_logs_var.get() else "Полный") + " журнал скопирован в буфер обмена.")

    def _clear_console(self) -> None:
        self._connection_log_lines.clear()
        self.connection_log.configure(state="normal")
        self.connection_log.delete("1.0", "end")
        self.connection_log.configure(state="disabled")
        self.recent_log.configure(state="normal")
        self.recent_log.delete("1.0", "end")
        self.recent_log.configure(state="disabled")

    def _select_connection_device(self, _event=None) -> None:
        selected = self.tree.selection()
        if selected and selected[0] in self.devices:
            self.role_device_var.set(selected[0])
            self._load_role()

    def _connect_selected_port(self) -> None:
        port = self.available_port_var.get()
        if not port:
            messagebox.showwarning("Нет COM-порта", "Выберите COM-порт расходомера.")
            return
        self._append_connection_log(f"{time.strftime('%H:%M:%S')}  UI: connect button pressed for {port}.")
        threading.Thread(target=self._run_connection_command, args=("connect_port", port), daemon=True).start()

    def _disconnect_selected_port(self) -> None:
        port = self.available_port_var.get()
        if not port:
            messagebox.showwarning("Нет COM-порта", "Выберите COM-порт.")
            return
        self._append_connection_log(f"{time.strftime('%H:%M:%S')}  UI: disconnect button pressed for {port}.")
        threading.Thread(target=self._run_connection_command, args=("disconnect_port", port), daemon=True).start()

    def _arduino_settings_from_ui(self) -> dict[str, Any]:
        port = self.arduino_port_var.get().strip()
        if not port:
            raise ValueError("Выберите COM-порт Arduino")
        try:
            baudrate = int(self.arduino_baudrate_var.get().strip())
            timeout_s = float(self.arduino_timeout_var.get().replace(",", "."))
        except ValueError as exc:
            raise ValueError("Скорость и время ожидания ЯМР должны быть числами") from exc
        if baudrate < 1200 or baudrate > 2_000_000:
            raise ValueError("Скорость Arduino должна быть от 1200 до 2000000 бод")
        if timeout_s <= 0:
            raise ValueError("Время ожидания ответа ЯМР должно быть больше нуля")
        return {
            "port": port,
            "baudrate": baudrate,
            "spectrum_timeout_s": timeout_s,
            "startup_timeout_s": float(self.arduino_settings.get("startup_timeout_s", 8.0)),
        }

    def _connect_arduino(self) -> None:
        if self.manual_valve_pending or (self.manual_valve_requires_off and self.arduino.ready):
            self.manual_valve_status_var.set("Сначала выключите реле и дождитесь завершения ручной команды.")
            return
        if self.relay_test_pending:
            self.relay_test_status_var.set("Дождитесь завершения проверки реле перед переподключением Arduino.")
            return
        if self.runner and self.runner.is_alive():
            messagebox.showwarning("Эксперимент выполняется", "Остановите эксперимент перед сменой подключения Arduino.")
            return
        if self.arduino_connecting:
            return
        try:
            settings = self._arduino_settings_from_ui()
        except ValueError as exc:
            messagebox.showwarning("Настройки Arduino", str(exc))
            return
        self.arduino_connecting = True
        self.arduino_connect_button.configure(state="disabled")
        self.arduino_status_var.set(f"Подключение к {settings['port']}; ожидание готовности Arduino…")
        self._append_connection_log(f"{time.strftime('%H:%M:%S')}  UI: Arduino connect requested for {settings['port']}.")
        threading.Thread(target=self._run_arduino_connect, args=(settings,), daemon=True).start()

    def _run_arduino_connect(self, settings: dict[str, Any]) -> None:
        try:
            self.arduino.submit("connect", settings, timeout_s=settings["startup_timeout_s"] + 3)
            self.events.put(("arduino_connected", settings))
        except Exception as exc:
            self.events.put(("arduino_connection_error", str(exc)))

    def _disconnect_arduino(self) -> None:
        if self.manual_valve_pending or (self.manual_valve_requires_off and self.arduino.ready):
            self.manual_valve_status_var.set("Сначала выключите реле и дождитесь подтверждения OFF.")
            return
        if self.relay_test_pending:
            self.relay_test_status_var.set("Сначала завершите проверку кнопкой «Выключить все».")
            return
        if self.runner and self.runner.is_alive():
            messagebox.showwarning("Эксперимент выполняется", "Остановите эксперимент перед отключением Arduino.")
            return
        threading.Thread(target=self._run_arduino_disconnect, daemon=True).start()

    def _run_arduino_disconnect(self) -> None:
        try:
            self.arduino.submit("disconnect", timeout_s=4)
            self.events.put(("arduino_disconnected", None))
        except Exception as exc:
            self.events.put(("arduino_connection_error", str(exc)))

    def _run_connection_command(self, action: str, port: str) -> None:
        try:
            self.service.submit(action, {"port": port}, timeout_s=12)
            self.events.put(("connection_log", f"{time.strftime('%H:%M:%S')}  UI: {action} completed for {port}."))
            self.events.put(("connection_status", f"{port}: подключён." if action == "connect_port" else f"{port}: отключён."))
        except Exception as exc:
            message = f"{action} failed for {port}: {exc}"
            self.events.put(("connection_log", f"{time.strftime('%H:%M:%S')}  UI: {message}"))
            self.events.put(("connection_error", message))

    def _update_devices(self, devices: list[DeviceInfo]) -> None:
        previous_serials = set(self.devices)
        self.devices = {device.serial_number: device for device in devices}
        mismatches: list[str] = []
        for device in devices:
            profile = self.store.fluid_profiles.get(device.serial_number, {}).get(str(device.fluidset_index), {})
            expected = str(profile.get("expected_gas", "")).strip()
            if expected and expected.casefold() != device.fluid_name.casefold():
                mismatches.append(f"{device.serial_number}: ожидался газ '{expected}', прибор сообщает '{device.fluid_name}'.")
        unseen = set(self.tree.get_children())
        for serial_number, device in self.devices.items():
            capacity = "unknown" if device.capacity is None else f"{device.capacity:.6g} {device.unit}"
            measured = "—" if device.measured_flow is None else f"{device.measured_flow:.6g} {device.unit}"
            values = (
                serial_number,
                ROLE_LABELS.get(self.roles.get(serial_number, "Unassigned"), "Не назначена"),
                MODE_LABELS.get(self.device_modes.get(serial_number, "Read flow"), "Чтение потока"),
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
        for item in self.live_tree.get_children():
            if item not in self.devices:
                self.live_tree.delete(item)
        for serial_number, device in self.devices.items():
            def reading(value):
                return "—" if value is None else f"{value:.6g} {device.unit}"
            values = (f"{serial_number} · {device.port}", device.fluid_name,
                      reading(device.setpoint_flow), reading(device.measured_flow))
            if self.live_tree.exists(serial_number):
                self.live_tree.item(serial_number, values=values)
            else:
                self.live_tree.insert("", "end", iid=serial_number, values=values)
        self.live_timestamp_var.set(f"Последнее обновление: {time.strftime('%H:%M:%S')}" if devices else "Нет данных от приборов")
        self._update_calibration_table()
        serials = sorted(self.devices)
        self.device_count_var.set(f"MFC подключено: {len(serials)}" if serials else "Нет подключённых MFC")
        if previous_serials != set(serials):
            self.status_var.set("Приборы подключены. Выберите режим MFC и перейдите к эксперименту." if serials else "Нет подключённых приборов. Выберите COM-порт.")
        if serials:
            self.devices_empty_label.pack_forget()
        elif not self.devices_empty_label.winfo_manager():
            self.devices_empty_label.pack(anchor="w", pady=(8, 0))
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
            self.role_device_var.set("Нет приборов")
            self.role_var.set("Unassigned")
            self.device_mode_var.set("Read flow")
            self.device_detail_var.set("")
            self.fluid_conditions_var.set("Выберите подключённый прибор в таблице.")
        selected = self.devices.get(self.role_device_var.get())
        if selected is not None:
            snapshot = self._condition_snapshot(selected)
            if snapshot != self._conditions_snapshot:
                self._load_role()
        if mismatches:
            self.port_warning_var.set("Несоответствие газа — проверьте профиль: " + " ".join(mismatches))
        elif self.port_warning_var.get().startswith("Несоответствие газа"):
            self.port_warning_var.set("")
        self._update_operation_mode_controls()
        self._redraw_experiment_view()
        # Keep action feedback visible rather than overwriting it every poll.

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
            self.device_detail_var.set("")
            self.fluid_conditions_var.set("Выберите подключённый прибор в таблице.")
            self.fluidset_var.set("")
            self.expected_gas_var.set("")
            self.inlet_pressure_var.set("")
            self.outlet_pressure_var.set("")
            self.fluid_temperature_var.set("")
            self.reference_pressure_var.set("")
            self._conditions_snapshot = None
            return
        self._conditions_snapshot = self._condition_snapshot(device)
        self.device_detail_var.set(f"{device.model} · {device.port} · газ в приборе: {device.fluid_name}")
        fluid_set = "unknown" if device.fluidset_index is None else str(device.fluidset_index)
        self.fluid_conditions_var.set(f"{serial_number} · профиль {fluid_set} · газ {device.fluid_name}")
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
            raise ValueError("Введите числовые значения: давление в бар(a), температура в °C.") from exc
        if not all(math.isfinite(value) for value in values.values()) or values["inlet"] <= 0 or values["outlet"] <= 0 or values["reference"] <= 0:
            raise ValueError("Давление должно быть конечным положительным числом в бар(a).")
        return values

    def _apply_fluid_conditions(self) -> None:
        if not self.control_enabled.get():
            messagebox.showwarning("Запись отключена", "Включите «Разрешить запись параметров в прибор» на этой вкладке.")
            return
        serial_number = self.role_device_var.get()
        device = self.devices.get(serial_number)
        if device is None:
            messagebox.showwarning("Нет прибора", "Выберите подключённый прибор.")
            return
        try:
            values = self._parse_fluid_conditions()
        except ValueError as exc:
            messagebox.showwarning("Неверные условия газа", str(exc))
            return
        if not messagebox.askyesno(
            "Сохранить условия газа",
            f"Сохранить условия для профиля {device.fluidset_index} ({device.fluid_name}) на приборе {serial_number}?\n\n"
            "Изменяются сохранённые условия газовой модели.",
        ):
            return
        try:
            self.service.submit("set_fluid_conditions", {serial_number: values})
            self.store.save_fluid_profile(serial_number, device.fluidset_index or 0, {"expected_gas": self.expected_gas_var.get().strip(), **values})
            self.status_var.set(f"{serial_number}: условия сохранены. Ожидается обновление показаний.")
        except Exception as exc:
            messagebox.showerror("Не удалось сохранить условия", str(exc))

    def _activate_fluidset(self) -> None:
        if not self.control_enabled.get():
            messagebox.showwarning("Запись отключена", "Включите «Разрешить запись параметров в прибор» на этой вкладке.")
            return
        serial_number = self.role_device_var.get()
        device = self.devices.get(serial_number)
        if device is None:
            messagebox.showwarning("Нет прибора", "Выберите подключённый прибор.")
            return
        try:
            fluidset = int(self.fluidset_var.get())
            if not 0 <= fluidset <= 7:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Неверный номер профиля", "Введите целый номер профиля от 0 до 7.")
            return
        expected = self.expected_gas_var.get().strip()
        if not expected:
            messagebox.showwarning("Укажите газ", "Перед переключением профиля укажите ожидаемый газ.")
            return
        if not messagebox.askyesno(
            "Активировать профиль",
            f"Активировать профиль {fluidset} на {serial_number}?\nОжидаемый газ: {expected}\n\n"
            "Газовая модель и диапазон могут измениться. После переключения проверьте газ, считанный из прибора.",
        ):
            return
        try:
            self.service.submit("activate_fluidset", {serial_number: fluidset})
            self.store.save_fluid_profile(serial_number, fluidset, {"expected_gas": expected})
            self.status_var.set(f"Профиль {fluidset} активирован. Ожидается подтверждение газа от прибора…")
        except Exception as exc:
            messagebox.showerror("Не удалось активировать профиль", str(exc))

    def _save_role(self) -> None:
        serial_number, role = self.role_device_var.get(), self.role_var.get()
        if serial_number not in self.devices:
            messagebox.showwarning("Нет прибора", "Выберите подключённый прибор.")
            return
        self.roles[serial_number] = role
        self.store.save_roles(self.roles)
        self._update_devices(list(self.devices.values()))
        self.status_var.set(f"Роль {serial_number} сохранена: {ROLE_LABELS.get(role, role)}.")

    def _save_device_mode(self) -> None:
        serial_number, mode = self.role_device_var.get(), self.device_mode_var.get()
        if serial_number not in self.devices:
            messagebox.showwarning("Нет прибора", "Выберите подключённый прибор.")
            return
        if mode not in MFC_MODES:
            messagebox.showwarning("Неверный режим MFC", "Выберите чтение или управление потоком.")
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
            raise ValueError("Назначьте одному MFC роль «Водород», другому — «Алкен».")
        if h2[0].unit != alkene[0].unit:
            raise ValueError("Единицы потока у приборов различаются. Для смеси нужны одинаковые единицы.")
        if h2[0].capacity is None or alkene[0].capacity is None:
            raise ValueError("Не удалось прочитать диапазоны обоих MFC. Подача заблокирована.")
        for device in (h2[0], alkene[0]):
            self._validate_automatic_device(device)
        return h2[0], alkene[0]

    def _validate_automatic_device(self, device: DeviceInfo) -> None:
        if device.capacity is None:
            raise ValueError(f"Не удалось прочитать диапазон {device.serial_number}. Подача заблокирована.")
        if self.device_modes.get(device.serial_number, "Read flow") != "Set flow":
            raise ValueError(f"{device.serial_number}: выберите «Управление потоком» на вкладке «Подключение».")
        profile = self.store.fluid_profiles.get(device.serial_number, {}).get(str(device.fluidset_index), {})
        expected = str(profile.get("expected_gas", "")).strip()
        if expected and expected.casefold() != device.fluid_name.casefold():
            raise ValueError(
                f"{device.serial_number}: ожидался газ '{expected}', прибор сообщает '{device.fluid_name}'."
            )

    def _experiment_devices(self) -> tuple[DeviceInfo, DeviceInfo | None]:
        """Return one controlled MFC or the normal H2/alkene pair."""
        if len(self.devices) == 1:
            device = next(iter(self.devices.values()))
            self._validate_automatic_device(device)
            return device, None
        if not self.devices:
            raise ValueError("Подключите один MFC либо два MFC с ролями «Водород» и «Алкен».")
        h2, alkene = self._role_devices()
        return h2, alkene

    def _single_mfc_automatic_setup(self) -> bool:
        return len(self.devices) == 1

    def _redraw_experiment_view(self) -> None:
        if self.flow_plot is None or not self.flow_plot.winfo_exists():
            return
        try:
            timing = self._current_cycle()
            extra_delay_s = timing.t2 + timing.t3 + timing.t4
        except ValueError:
            extra_delay_s = 0.0
        self.flow_plot.set_data(
            self.active_plan,
            self.plan_h2_serial,
            self.plan_alkene_serial,
            self.devices,
            self.current_plan_point,
            extra_delay_s,
        )

    def _open_graph_window(self) -> None:
        if self.graph_window is None or not self.graph_window.winfo_exists():
            self.graph_window = tk.Toplevel(self)
            self.graph_window.title("График подачи газа")
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

    def _show_plan(self, plan: list[ExperimentPoint], h2: DeviceInfo, alkene: DeviceInfo | None) -> None:
        self.active_plan = list(plan)
        self.plan_h2_serial = h2.serial_number
        self.plan_alkene_serial = None if alkene is None else alkene.serial_number
        self.current_plan_point = None
        if alkene is None:
            self.schedule_tree["displaycolumns"] = ("point", "total", "dwell", "status")
            self.schedule_tree.heading("h2", text=f"MFC {h2.serial_number}")
        else:
            self.schedule_tree["displaycolumns"] = ("point", "total", "h2", "alkene", "dwell", "status")
            self.schedule_tree.heading("h2", text="Поток H₂")
        for item in self.schedule_tree.get_children():
            self.schedule_tree.delete(item)
        for point in plan:
            self.schedule_tree.insert(
                "", "end", iid=f"point-{point.number}",
                values=(point.number, f"{point.total_flow:g}", f"{point.h2_flow:g}", f"{point.alkene_flow:g}", f"{point.dwell_s:g}", "В очереди"),
            )
        self.run_progress.configure(value=0)
        self.completed_points_var.set(f"Завершено: 0 из {len(plan)}")
        self._redraw_experiment_view()

    def _point_status(self, number: int, status: str) -> None:
        item = f"point-{number}"
        if self.schedule_tree.exists(item):
            self.schedule_tree.set(item, "status", status)
        rows = self.schedule_tree.get_children()
        statuses = [self.schedule_tree.set(row, "status") for row in rows]
        done = sum(status.startswith("Завершена") for status in statuses)
        warnings = sum(status == "Завершена, t4 прервана" for status in statuses)
        self.run_progress.configure(value=100 * done / len(rows) if rows else 0)
        self.completed_points_var.set(f"Завершено: {done} из {len(rows)}" + (f"; t4 прервана: {warnings}" if warnings else ""))

    def _manual_flow_request(self, serial_number: str, flow: float) -> None:
        if self.operation_mode.get() != "manual":
            messagebox.showinfo("Автоматический режим", "Выберите «Ручное управление», чтобы задать поток отдельному прибору.")
            return
        if self.runner and self.runner.is_alive():
            messagebox.showwarning("Эксперимент выполняется", "Остановите эксперимент перед ручным управлением.")
            return
        if serial_number in self.manual_commands_pending:
            messagebox.showwarning("Команда выполняется", "Дождитесь выполнения предыдущей команды.")
            return
        device = self.devices.get(serial_number)
        if device is None:
            messagebox.showwarning("Нет прибора", "Сначала подключите MFC.")
            return
        if self.device_modes.get(serial_number, "Read flow") != "Set flow":
            messagebox.showwarning("Режим чтения", f"{serial_number}: выберите «Управление потоком» на вкладке «Подключение».")
            return
        if not math.isfinite(flow) or flow < 0:
            messagebox.showwarning("Неверный поток", "Введите конечное число, не меньше нуля.")
            return
        if device.capacity is None or flow > device.capacity + 1e-7:
            messagebox.showwarning("Превышен диапазон", f"Диапазон {serial_number}: 0–{device.capacity:g} {device.unit}" if device.capacity is not None else "Диапазон прибора неизвестен.")
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
            messagebox.showwarning("Неверный поток", "Введите число для заданного потока.")
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

    def _current_plan(self, single_mfc: bool) -> list[ExperimentPoint]:
        mode = self.plan_mode_var.get()
        if mode not in ("range", "custom"):
            raise ValueError("Выберите диапазон с шагом или список потоков")
        repeats = self._current_point_repeats()
        try:
            required = ["dwell"] + ([] if single_mfc else ["ratio"])
            if mode == "range":
                required += ["start", "stop", "step"]
            values = {key: float(self.plan_vars[key].get().replace(",", ".")) for key in required}
        except ValueError as exc:
            raise ValueError("Параметры плана должны быть числами") from exc
        if mode == "custom":
            plan = build_custom_plan(self.custom_flows_var.get(), values.get("ratio", 1.0), values["dwell"], single_mfc)
        elif single_mfc:
            plan = build_single_mfc_plan(values["start"], values["stop"], values["step"], values["dwell"])
        else:
            plan = build_plan(values["start"], values["stop"], values["step"], values["ratio"], values["dwell"])
        return repeat_plan_points(plan, repeats)

    def _validate_plan(self) -> list[ExperimentPoint] | None:
        try:
            cycle = self._current_cycle()
            h2, alkene = self._experiment_devices()
            plan = self._current_plan(alkene is None)
            for point in plan:
                if alkene is None and point.total_flow > h2.capacity + 1e-7:
                    raise ValueError(f"Точка {point.number}: поток превышает диапазон: {point.total_flow:.6g}/{h2.capacity:.6g} {h2.unit}")
                if alkene is not None and (point.h2_flow > h2.capacity + 1e-7 or point.alkene_flow > alkene.capacity + 1e-7):
                    raise ValueError(f"Точка {point.number}: поток превышает диапазон: H2 {point.h2_flow:.6g}/{h2.capacity:.6g}, alkene {point.alkene_flow:.6g}/{alkene.capacity:.6g} {h2.unit}")
            seconds = len(plan) * cycle.delay_s
            self._show_plan(plan, h2, alkene)
            self.plan_var.set(f"{len(plan)} точек · {len(plan) * 2} спектров · {seconds:g} с задержек + время ЯМР\nДиапазон потока проверен.")
            return plan
        except Exception as exc:
            self.plan_var.set("Ошибка плана: " + str(exc))
            return None

    def _toggle_control(self) -> None:
        if self.control_enabled.get() and not messagebox.askyesno("Разрешить подачу газа", "Запись уставок управляет подачей газа. Линия проверена на герметичность, защита давления и запорные клапаны готовы?"):
            self.control_enabled.set(False)

    def _start_experiment(self) -> None:
        if self.manual_valve_pending or self.manual_valve_requires_off:
            self.manual_valve_status_var.set("Перед запуском эксперимента выключите реле и дождитесь подтверждения OFF.")
            return
        if self.relay_test_pending:
            self.relay_test_status_var.set("Дождитесь завершения проверки и подтверждения OFF перед запуском.")
            return
        if self.operation_mode.get() not in AUTOMATIC_MODES:
            messagebox.showinfo("Ручной режим", "Выберите «Авто + ЯМР» или «Авто + ЯМР + клапаны».")
            return
        if not self.control_enabled.get():
            messagebox.showwarning("Запись отключена", "Включите «Разрешить подачу газа» рядом с кнопкой запуска.")
            return
        if self.runner and self.runner.is_alive():
            messagebox.showwarning("Эксперимент уже запущен", "Сначала остановите текущий эксперимент.")
            return
        plan = self._validate_plan()
        if plan is None:
            return
        h2, alkene = self._experiment_devices()
        arduino_mode = self.operation_mode.get() in AUTOMATIC_MODES
        valves_mode = self.operation_mode.get() == "automatic-arduino-valves"
        cycle = self._current_cycle()
        recovery_policy = None
        if valves_mode and self.recover_after_off_var.get():
            try:
                recovery_policy = self._current_recovery_policy()
            except ValueError as exc:
                messagebox.showwarning("Восстановление Arduino", str(exc))
                return
        timeout_s = 180.0
        if arduino_mode:
            if not self.arduino.ready:
                messagebox.showwarning("Arduino не готова", "Подключите Arduino и дождитесь сообщения о готовности.")
                return
            try:
                arduino_settings = self._arduino_settings_from_ui()
            except ValueError as exc:
                messagebox.showwarning("Настройки Arduino", str(exc))
                return
            if (
                arduino_settings["port"] != self.arduino.settings.get("port")
                or arduino_settings["baudrate"] != self.arduino.settings.get("baudrate")
            ):
                messagebox.showwarning("Настройки Arduino changed", "После изменения порта или скорости подключите Arduino заново.")
                return
            timeout_s = arduino_settings["spectrum_timeout_s"]
        extra = "\nВ каждой точке записываются два спектра, каждый с ответным TTL от ЯМР."
        source = "список потоков" if self.plan_mode_var.get() == "custom" else "диапазон с шагом"
        extra += f"\nПлан: {source}; повтор каждой точки ×{self.point_repeats_var.get()}; всего {len(plan) * 2} спектров."
        extra += f"\nt1={cycle.t1:g}, t2={cycle.t2:g}, t3={cycle.t3:g}, t4={cycle.t4:g} с."
        if valves_mode:
            if not self.arduino.relay_selection_capable:
                messagebox.showwarning("Обновите прошивку Arduino", "Загрузите arduino_nmr_ttl.ino с выбором одного/двух реле (RELAY_MASK) и подключите Arduino заново.")
                return
            extra += "\n" + RELAY_SELECTION_LABELS[str(self._selected_relay_mask())] + ": " + self._relay_cycle_description()
            if self.recover_after_off_var.get():
                extra += f"\nАвтовосстановление на t1/t4/OFF: {recovery_policy.attempts} попыток на каждый цикл, пауза {recovery_policy.interval_s:g} с перед каждой, включая первую. t1 начинается заново с начальным состоянием реле; прерванная t4 помечается предупреждением, до следующей точки реле остаются OFF."
        if alkene is None:
            prompt = (
                f"Запустить {len(plan)} точек на MFC {h2.serial_number}?\n"
                f"Прибор получит весь заданный поток. При остановке и завершении ему будет отправлен ноль.{extra}"
            )
        else:
            prompt = (
                f"Запустить {len(plan)} точек при H₂:алкен = {self.plan_vars['ratio'].get()}?\n"
                f"При остановке и завершении обоим приборам будет отправлен ноль.{extra}"
            )
        if not messagebox.askyesno("Запуск эксперимента", prompt):
            return
        self.runner = ExperimentRunner(
            self.service, plan, h2.serial_number, None if alkene is None else alkene.serial_number, self.events,
            arduino=self.arduino if arduino_mode else None,
            arduino_timeout_s=timeout_s,
            cycle=cycle,
            valves=ArduinoValveController(self.arduino, self.events, self._selected_relay_mask()) if valves_mode else None,
            recover_after_off=valves_mode and self.recover_after_off_var.get(),
            recovery_policy=recovery_policy,
            relay_initial_on=valves_mode and self.relay_initial_on_var.get(),
        )
        self.runner.start()
        self.progress_var.set("Запуск эксперимента…")
        self._update_operation_mode_controls()

    def _stop_experiment(self) -> None:
        if self.operation_mode.get() not in AUTOMATIC_MODES:
            messagebox.showinfo("Ручной режим", "Нажмите «Ноль» в карточке нужного MFC.")
            return
        if not self.runner or not self.runner.is_alive():
            messagebox.showinfo("Нет активного эксперимента", "Эксперимент сейчас не выполняется.")
            return
        target = "управляемому MFC" if self.plan_alkene_serial is None else "MFC водорода и алкена"
        if messagebox.askyesno("Остановить эксперимент", f"Остановить эксперимент и отправить нулевой поток {target}?"):
            self.runner.cancel()
            self.progress_var.set("Остановка; отправка нулевого потока…")

    def _consume_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "devices":
                    self._update_devices(payload)
                elif kind == "stage":
                    point, _ = payload
                    self._point_status(point.number, "Выдержка")
                    if self.plan_alkene_serial is None:
                        self._append_connection_log(f"{time.strftime('%H:%M:%S')}  POINT {point.number}: MFC={point.total_flow:g}, dwell={point.dwell_s:g} s")
                    else:
                        self._append_connection_log(f"{time.strftime('%H:%M:%S')}  POINT {point.number}: H2={point.h2_flow:g}, alkene={point.alkene_flow:g}, dwell={point.dwell_s:g} s")
                    self.current_plan_point = point.number
                    self.schedule_tree.selection_set(f"point-{point.number}")
                    self.schedule_tree.see(f"point-{point.number}")
                    self._redraw_experiment_view()
                    if self.plan_alkene_serial is None:
                        self.progress_var.set(f"Точка {point.number}: поток {point.total_flow:g}. Выдержка {point.dwell_s:g} с.")
                    else:
                        self.progress_var.set(f"Точка {point.number}: всего {point.total_flow:g}; H₂ {point.h2_flow:g}; алкен {point.alkene_flow:g}. Выдержка {point.dwell_s:g} с.")
                elif kind == "progress":
                    number, elapsed, dwell = payload
                    self.progress_var.set(f"Точка {number}: выдержка {elapsed:.0f} / {dwell:.0f} с")
                elif kind == "cycle_stage":
                    number, name, seconds = payload
                    self.active_cycle_stage = name
                    self.cycle_diagram.active_stage = name
                    self.cycle_diagram.redraw()
                    self._point_status(number, f"Задержка {name}")
                    self.progress_var.set(f"Точка {number}: {name}, {seconds:g} с")
                    self._append_connection_log(f"{time.strftime('%H:%M:%S')}  POINT {number}: {name} = {seconds:g} s")
                elif kind == "cycle_progress":
                    number, name, elapsed, seconds = payload
                    self.progress_var.set(f"Точка {number}: {name}, {elapsed:.1f} / {seconds:g} с")
                elif kind == "nmr_trigger_wait":
                    point, spectrum = payload
                    self.cycle_diagram.active_stage = f"nmr{spectrum}"
                    self.cycle_diagram.redraw()
                    self._point_status(point.number, f"ЯМР {spectrum}/2: ожидание TTL")
                    self.progress_var.set(f"Точка {point.number}: спектр {spectrum}/2. Ожидание запуска и ответа ЯМР.")
                    self._append_connection_log(f"{time.strftime('%H:%M:%S')}  POINT {point.number}: spectrum {spectrum}/2, requesting Arduino/NMR TTL handshake.")
                elif kind == "spectrum_confirmed":
                    point, spectrum = payload
                    self._point_status(point.number, f"Спектр {spectrum}/2 принят")
                    self.progress_var.set(f"Точка {point.number}: спектр {spectrum}/2 подтверждён ответным TTL.")
                    self._append_connection_log(f"{time.strftime('%H:%M:%S')}  POINT {point.number}: spectrum {spectrum}/2 confirmed by returning NMR TTL.")
                elif kind == "valve_status":
                    number, switched, relay_mask = payload
                    if switched is None:
                        self.valve_status_var.set("Реле D4/D5: состояние не подтверждено.")
                    else:
                        if not switched:
                            self.manual_valve_requires_off = False
                        elif self.operation_mode.get() == "manual":
                            self.manual_valve_requires_off = True
                        r1 = "ON" if switched and relay_mask & 1 else "OFF"
                        r2 = "ON" if switched and relay_mask & 2 else "OFF"
                        state = f"D4/IN1={r1}, D5/IN2={r2}"
                        self.valve_status_var.set(f"Arduino подтвердила реле {state}.")
                        if self.relay_test_pending:
                            self.relay_test_status_var.set(f"Проверка: Arduino подтвердила {state}.")
                        self._append_connection_log(f"{time.strftime('%H:%M:%S')}  RELAYS · точка {number}: {state}; подтверждены выходы, датчиков положения клапанов нет.")
                    self.manual_valve_status_var.set(self.valve_status_var.get())
                elif kind == "manual_valve_finished":
                    self.manual_valve_pending = False
                    cancel = self.manual_valve_cancel
                    self.manual_valve_cancel = None
                    if payload:
                        self.manual_valve_status_var.set("Ошибка управления реле: " + payload)
                    elif cancel is not None and cancel.is_set() and self.manual_valve_requires_off:
                        # OFF may be requested just after the worker checked the
                        # event: do not lose that final UI cancellation race.
                        self._request_manual_valve(False)
                    self._append_connection_log(f"{time.strftime('%H:%M:%S')}  MANUAL VALVES: {self.manual_valve_status_var.get()}")
                elif kind == "relay_test_finished":
                    pulse, cancelled, error = payload
                    self.relay_test_pending = False
                    self.relay_test_cancel = None
                    if error:
                        text = "Ошибка проверки реле: " + error
                    elif pulse:
                        text = ("Проверка прервана" if cancelled else "Проверка завершена") + ": все реле OFF подтверждено. Проверьте щелчок/контакты."
                    else:
                        text = "Arduino подтвердила: все реле OFF (D4/D5 HIGH)."
                    self.relay_test_status_var.set(text)
                    self._append_connection_log(f"{time.strftime('%H:%M:%S')}  {text}")
                elif kind in ("point_completed", "point_completed_warning"):
                    warning = kind == "point_completed_warning"
                    self._point_status(payload.number, "Завершена, t4 прервана" if warning else "Завершена")
                    self.progress_var.set(f"Точка {payload.number}: " + ("оба спектра приняты, t4 прервана." if warning else "цикл завершён.") + " Переход к следующей точке.")
                elif kind == "recovery_status":
                    number, message = payload
                    self._point_status(number, "Контроль OFF / восстановление")
                    self.progress_var.set(f"Точка {number}: {message}")
                    self._append_connection_log(f"{time.strftime('%H:%M:%S')}  RECOVERY · точка {number}: {message}")
                elif kind == "finished":
                    self._append_connection_log(f"{time.strftime('%H:%M:%S')}  {payload}")
                    self.progress_var.set(payload)
                    self.current_plan_point = None
                    self.cycle_diagram.active_stage = None
                    self.cycle_diagram.redraw()
                    self._redraw_experiment_view()
                elif kind == "runner_error":
                    if self.current_plan_point is not None:
                        self._point_status(self.current_plan_point, "Прервана")
                    self._append_connection_log(f"{time.strftime('%H:%M:%S')}  Ошибка эксперимента: {payload}")
                    self.progress_var.set("Ошибка эксперимента: " + payload)
                    self.control_enabled.set(False)
                    self.cycle_diagram.active_stage = None
                    self.cycle_diagram.redraw()
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
                    self.status_var.set("Не удалось подключиться. Подробности на вкладке «Консоль».")
                elif kind == "arduino_log":
                    self._append_connection_log(payload)
                elif kind == "arduino_status":
                    self.arduino_status_var.set(payload)
                elif kind == "arduino_connected":
                    self.arduino_connecting = False
                    self.arduino_connect_button.configure(state="normal")
                    self.arduino_settings = dict(payload)
                    if hasattr(self.store, "save_arduino"):
                        self.store.save_arduino(self.arduino_settings)
                    self.relay_test_status_var.set(
                        "Arduino готова. «Проверить реле» включит выбранные каналы на 1 с и выключит их."
                        if self.arduino.relay_selection_capable else
                        "Для проверки реле обновите прошивку Arduino до версии с RELAY_MASK.")
                    self.manual_valve_status_var.set("Arduino готова. Выберите реле и нажмите ON или OFF; положение клапана не измеряется.")
                    self._append_connection_log(f"{time.strftime('%H:%M:%S')}  UI: Arduino connected on {payload['port']}.")
                elif kind == "arduino_disconnected":
                    self.arduino_connecting = False
                    self.arduino_connect_button.configure(state="normal")
                    self.arduino_status_var.set("Arduino не подключена.")
                    self.relay_test_status_var.set("Проверка реле: Arduino отключена.")
                    self.manual_valve_status_var.set("Arduino отключена. Текущее состояние реле не подтверждено.")
                    self._append_connection_log(f"{time.strftime('%H:%M:%S')}  UI: Arduino disconnected.")
                elif kind == "arduino_connection_error":
                    self.arduino_connecting = False
                    self.arduino_connect_button.configure(state="normal")
                    self.arduino_status_var.set("Ошибка подключения Arduino: " + payload)
                    self.manual_valve_status_var.set("Ошибка подключения Arduino; состояние реле не подтверждено.")
                    self._append_connection_log(f"{time.strftime('%H:%M:%S')}  ARDUINO CONNECTION ERROR: {payload}")
                elif kind == "manual_setpoint":
                    serial_number, flow = payload
                    self.manual_commands_pending.discard(serial_number)
                    self.manual_setpoints[serial_number] = flow
                    device = self.devices.get(serial_number)
                    unit = "device unit" if device is None else device.unit
                    self.progress_var.set(f"{serial_number}: прибор принял поток {flow:g} {unit}.")
                    card = self.manual_cards.get(serial_number)
                    if card is not None:
                        card.command_var.set(f"Последняя команда принята: {flow:g} {unit}")
                    self._append_connection_log(f"{time.strftime('%H:%M:%S')}  MANUAL {serial_number}: accepted setpoint {flow:g} {unit}")
                    self._update_operation_mode_controls()
                elif kind == "manual_setpoint_error":
                    serial_number, error = payload
                    self.manual_commands_pending.discard(serial_number)
                    self.progress_var.set(f"Ошибка задания потока {serial_number}: {error}")
                    card = self.manual_cards.get(serial_number)
                    if card is not None:
                        card.command_var.set(f"Ошибка команды: {error}")
                    self._append_connection_log(f"{time.strftime('%H:%M:%S')}  MANUAL ERROR {serial_number}: {error}")
                    self.control_enabled.set(False)
                    self._update_operation_mode_controls()
        except queue.Empty:
            pass
        if self.winfo_exists():
            self._update_operation_mode_controls()
            self.after(100, self._consume_events)

    def _close(self) -> None:
        if self.manual_valve_pending:
            if self.manual_valve_cancel is not None:
                self.manual_valve_cancel.set()
            self.manual_valve_status_var.set("Ожидание OFF перед закрытием…")
            if not getattr(self, "_close_pending", False):
                self._close_pending = True
                self.after(100, self._retry_close)
            return
        if self.relay_test_pending:
            if self.relay_test_cancel is not None:
                self.relay_test_cancel.set()
            self.relay_test_status_var.set("Выключение реле перед закрытием…")
            if not getattr(self, "_close_pending", False):
                self._close_pending = True
                self.after(100, self._retry_close)
            return
        if self.runner and self.runner.is_alive():
            self.runner.cancel()
            # Keep the two COM workers alive until the runner has sent OFF
            # and zeroed MFCs. Closing them first would discard cleanup commands.
            self.progress_var.set("Остановка перед закрытием: OFF реле и нулевой поток…")
            if not getattr(self, "_close_pending", False):
                self._close_pending = True
                self.after(100, self._retry_close)
            return
        self.service.shutdown()
        self.arduino.shutdown()
        self._shutdown_started = time.monotonic()
        self._wait_for_workers()

    def _retry_close(self) -> None:
        self._close_pending = False
        self._close()

    def _wait_for_workers(self) -> None:
        # Arduino's worker confirms OFF before it releases the port. The
        # firmware timeout also covers a serial link lost during this wait.
        if (self.service.is_alive() or self.arduino.is_alive()) and time.monotonic() - self._shutdown_started < 8:
            self.after(100, self._wait_for_workers)
        else:
            self.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    # Resolve the bundled configuration beside this script rather than from
    # the shell's current directory. This keeps the normal VS Code/PowerShell
    # launch command working from the workspace root.
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().with_name("config.json"))
    args = parser.parse_args()
    try:
        store = ConfigStore(args.config)
    except Exception as exc:
        raise SystemExit(f"Invalid configuration: {exc}") from exc
    GasLineApp(store).mainloop()


if __name__ == "__main__":
    main()
