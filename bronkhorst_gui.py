"""Automatic Bronkhorst EL-FLOW Prestige discovery and experiment runner.

Discovery only uses ProPar reads. Setpoints are written only after the operator
enables control and starts a validated plan.
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
ROLES = ("Unassigned", "H2", "Alkene", "Other")

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
    payload: dict[str, float] | None = None
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
            "probe_timeout_s": float(raw_discovery.get("probe_timeout_s", 0.12)),
            "ports": [str(v) for v in raw_discovery.get("ports", [])],
            "include_non_usb": bool(raw_discovery.get("include_non_usb", False)),
            "slave_ids": sorted(set(slave_ids)),
        }
        if not 0.5 <= self.discovery["scan_interval_s"] <= 120 or not 0.02 <= self.discovery["probe_timeout_s"] <= 5:
            raise ValueError("Invalid discovery timing")
        self.roles = {str(serial): str(role) for serial, role in self.raw.get("roles", {}).items() if str(role) in ROLES}

    def save_roles(self, roles: dict[str, str]) -> None:
        self.raw["roles"] = roles
        self.path.write_text(json.dumps(self.raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self.roles = dict(roles)


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
        return DeviceInfo(serial_number, model or "EL-FLOW Prestige", port, 0x80, capacity, unit, measured)

    def _candidate_ports(self) -> list[str]:
        if self.discovery["ports"]:
            return self.discovery["ports"]
        return [item.device for item in list_ports.comports() if self.discovery["include_non_usb"] or item.vid is not None]

    def _scan(self) -> None:
        found: dict[str, DeviceInfo] = {}
        ports = self._candidate_ports()
        now = time.monotonic()
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
                continue
            if port in self.reported_port_errors:
                del self.reported_port_errors[port]
                self.events.put(("port_recovered", f"{port} is available again."))
            try:
                device = self._probe(client, port)
                found[device.serial_number] = device
            except Exception:
                continue
        for port, client in list(self.clients.items()):
            if port not in ports:
                client.master.stop()
                del self.clients[port]
        self.devices = found
        self.events.put(("devices", list(found.values())))

    def _poll(self) -> None:
        refreshed: dict[str, DeviceInfo] = {}
        for serial_number, device in self.devices.items():
            try:
                flow = float(self._read(self._client(device.port, self.serial["timeout_s"]), FMEASURE, "poll"))
                refreshed[serial_number] = replace(device, measured_flow=flow)
            except Exception:
                refreshed[serial_number] = replace(device, measured_flow=None)
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

    def _handle(self, command: ServiceCommand) -> None:
        try:
            if command.name == "set":
                self._apply(command.payload or {})
            elif command.name == "zero":
                self._apply({serial: 0.0 for serial in (command.payload or {})})
            elif command.name == "stop":
                self.stopping.set()
            else:
                raise ValueError(f"Unknown command {command.name}")
        except Exception as exc:
            command.error = exc
        finally:
            if command.done:
                command.done.set()

    def submit(self, name: str, payload: dict[str, float] | None, timeout_s: float = 5) -> None:
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
            for client in self.clients.values():
                client.master.stop()

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


class GasLineApp(tk.Tk):
    def __init__(self, store: ConfigStore):
        super().__init__()
        self.title("Bronkhorst gas line — auto discovery and experiments")
        self.minsize(920, 560)
        self.store, self.events = store, queue.Queue()
        self.devices: dict[str, DeviceInfo] = {}
        self.roles = dict(store.roles)
        self.service = SerialService(store.serial, store.discovery, self.events)
        self.runner: ExperimentRunner | None = None
        self.control_enabled = tk.BooleanVar(value=False)
        self.role_device_var, self.role_var = tk.StringVar(value="No device detected"), tk.StringVar(value="Unassigned")
        self.plan_vars = {key: tk.StringVar(value=value) for key, value in {"start": "10", "stop": "150", "step": "10", "ratio": "4", "dwell": "60"}.items()}
        self.status_var = tk.StringVar(value="Waiting for USB Bronkhorst devices…")
        self.port_warning_var = tk.StringVar(value="")
        self.plan_var = tk.StringVar(value="Enter plan values; capacities are checked before start.")
        self.progress_var = tk.StringVar(value="No experiment is running.")
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.service.start()
        self.after(100, self._consume_events)

    def _build(self) -> None:
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Hardware shutdown valves and leak checks are mandatory. Discovery sends reads only.", foreground="#9b1c1c").pack(anchor="w", pady=(0, 8))
        devices_frame = ttk.LabelFrame(body, text="Automatically detected Bronkhorst devices", padding=8)
        devices_frame.pack(fill="x")
        columns = ("serial", "role", "port", "address", "model", "capacity", "measured")
        self.tree = ttk.Treeview(devices_frame, columns=columns, show="headings", height=6)
        for column, label in zip(columns, ("Serial number", "Gas role", "COM", "Protocol", "Model", "Capacity", "Measured")):
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
        plan_frame = ttk.LabelFrame(body, text="Experiment: fixed H₂:alkene ratio, total-flow ramp", padding=8)
        plan_frame.pack(fill="x", pady=(12, 0))
        fields = (("Total from", "start"), ("to", "stop"), ("step", "step"), ("H₂ : alkene", "ratio"), ("Dwell / point, s", "dwell"))
        for i, (label, key) in enumerate(fields):
            ttk.Label(plan_frame, text=label).grid(row=0, column=i * 2, sticky="w", padx=(0, 4))
            ttk.Entry(plan_frame, textvariable=self.plan_vars[key], width=9).grid(row=0, column=i * 2 + 1, sticky="w", padx=(0, 12))
        ttk.Button(plan_frame, text="Validate plan", command=self._validate_plan).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(plan_frame, textvariable=self.plan_var, wraplength=850).grid(row=1, column=2, columnspan=8, sticky="w", pady=(8, 0))
        controls = ttk.Frame(body)
        controls.pack(fill="x", pady=12)
        ttk.Checkbutton(controls, text="Enable writing setpoints", variable=self.control_enabled, command=self._toggle_control).pack(side="left")
        ttk.Button(controls, text="Start experiment", command=self._start_experiment).pack(side="left", padx=8)
        ttk.Button(controls, text="STOP and set H₂/alkene to zero", command=self._stop_experiment).pack(side="left")
        ttk.Label(body, textvariable=self.progress_var).pack(anchor="w")
        ttk.Label(body, textvariable=self.port_warning_var, foreground="#9b1c1c", wraplength=880).pack(anchor="w", pady=(6, 0))
        ttk.Label(body, textvariable=self.status_var, wraplength=880).pack(anchor="w", pady=(8, 0))

    def _update_devices(self, devices: list[DeviceInfo]) -> None:
        self.devices = {device.serial_number: device for device in devices}
        unseen = set(self.tree.get_children())
        for serial_number, device in self.devices.items():
            capacity = "unknown" if device.capacity is None else f"{device.capacity:.6g} {device.unit}"
            measured = "—" if device.measured_flow is None else f"{device.measured_flow:.6g} {device.unit}"
            values = (serial_number, self.roles.get(serial_number, "Unassigned"), device.port, "ProPar", device.model, capacity, measured)
            if serial_number in unseen:
                self.tree.item(serial_number, values=values)
                unseen.remove(serial_number)
            else:
                self.tree.insert("", "end", iid=serial_number, values=values)
        for serial_number in unseen:
            self.tree.delete(serial_number)
        serials = sorted(self.devices)
        self.role_device_box["values"] = serials
        if serials:
            self.role_device_box.configure(state="readonly")
            if self.role_device_var.get() not in serials:
                self.role_device_var.set(serials[0])
                self._load_role()
        else:
            self.role_device_box.configure(state="disabled")
            self.role_device_var.set("No device detected")
            self.role_var.set("Unassigned")
        self.status_var.set(f"Connected Bronkhorst devices: {len(serials)}. USB ports rescan every {self.store.discovery['scan_interval_s']:g} s.")

    def _load_role(self, _event: object | None = None) -> None:
        self.role_var.set(self.roles.get(self.role_device_var.get(), "Unassigned"))

    def _save_role(self) -> None:
        serial_number, role = self.role_device_var.get(), self.role_var.get()
        if serial_number not in self.devices:
            messagebox.showwarning("No device", "Select a connected device first.")
            return
        self.roles[serial_number] = role
        self.store.save_roles(self.roles)
        self._update_devices(list(self.devices.values()))
        self.status_var.set(f"Saved role for {serial_number}: {role}.")

    def _role_devices(self) -> tuple[DeviceInfo, DeviceInfo]:
        h2 = [self.devices[serial] for serial, role in self.roles.items() if role == "H2" and serial in self.devices]
        alkene = [self.devices[serial] for serial, role in self.roles.items() if role == "Alkene" and serial in self.devices]
        if len(h2) != 1 or len(alkene) != 1:
            raise ValueError("Assign exactly one connected device to H2 and one to Alkene.")
        if h2[0].unit != alkene[0].unit:
            raise ValueError("H2 and alkene units differ; use matching standard-volume units.")
        if h2[0].capacity is None or alkene[0].capacity is None:
            raise ValueError("Could not read both capacities; writing is blocked.")
        return h2[0], alkene[0]

    def _current_plan(self) -> list[ExperimentPoint]:
        try:
            values = {key: float(variable.get().replace(",", ".")) for key, variable in self.plan_vars.items()}
        except ValueError as exc:
            raise ValueError("Plan values must be numbers") from exc
        return build_plan(values["start"], values["stop"], values["step"], values["ratio"], values["dwell"])

    def _validate_plan(self) -> list[ExperimentPoint] | None:
        try:
            plan = self._current_plan()
            h2, alkene = self._role_devices()
            last = plan[-1]
            if last.h2_flow > h2.capacity + 1e-7 or last.alkene_flow > alkene.capacity + 1e-7:
                raise ValueError(f"Final point exceeds capacity: H2 {last.h2_flow:.6g}/{h2.capacity:.6g}, alkene {last.alkene_flow:.6g}/{alkene.capacity:.6g} {h2.unit}")
            seconds = sum(point.dwell_s for point in plan)
            self.plan_var.set(f"{len(plan)} points, {plan[0].total_flow:g}–{plan[-1].total_flow:g} {h2.unit}; {seconds / 60:g} min. Capacities are safe.")
            return plan
        except Exception as exc:
            self.plan_var.set("Plan error: " + str(exc))
            return None

    def _toggle_control(self) -> None:
        if self.control_enabled.get() and not messagebox.askyesno("Enable writes", "Writing setpoints controls gas flow. Are shutdown valves, pressure protection and leak checks active?"):
            self.control_enabled.set(False)

    def _start_experiment(self) -> None:
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
                    self.progress_var.set(f"Point {point.number}: total {point.total_flow:g}; H2 {point.h2_flow:g}; alkene {point.alkene_flow:g}. Holding {point.dwell_s:g} s.")
                elif kind == "progress":
                    number, elapsed, dwell = payload
                    self.progress_var.set(f"Point {number}: stabilization {elapsed:.0f}/{dwell:.0f} s")
                elif kind == "finished":
                    self.progress_var.set(payload)
                elif kind == "runner_error":
                    self.progress_var.set("EXPERIMENT ERROR: " + payload)
                    self.control_enabled.set(False)
                elif kind == "port_warning":
                    self.port_warning_var.set(payload)
                elif kind == "port_recovered":
                    self.port_warning_var.set(payload)
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
