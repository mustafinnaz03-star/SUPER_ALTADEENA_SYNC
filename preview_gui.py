"""Offline layout preview. NEVER starts serial workers or saves real config."""
import argparse
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bronkhorst_gui import ArduinoNMRService, DeviceInfo, GasLineApp, OPERATION_MODES, SerialService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=OPERATION_MODES, default="automatic-arduino")
    parser.add_argument("--size", default="1180x820")
    parser.add_argument("--demo", action="store_true", help="Show synthetic MFC readings and a validated plan")
    args = parser.parse_args()
    store = SimpleNamespace(
        serial={"timeout_s": 0.4}, discovery={"include_non_usb": False}, roles={},
        device_modes={}, fluid_profiles={}, experiment={},
        save_experiment=Mock(), save_arduino=Mock(), save_role=Mock(), save_device_mode=Mock(),
    )
    with patch.object(SerialService, "start"), patch.object(ArduinoNMRService, "start"), \
            patch.object(GasLineApp, "_refresh_available_ports"):
        app = GasLineApp(store)
    app.title("ОФЛАЙН · Bronkhorst — предпросмотр интерфейса")
    app.service.submit = Mock()
    app.arduino.submit = Mock(side_effect=lambda name, payload=None, **kwargs: {
        "switched": (payload or {}).get("switched", False),
        "relay_mask": (payload or {}).get("relay_mask", 3),
    })
    app.arduino_port_box["values"] = ("DEMO",)
    app.manual_arduino_port_box["values"] = ("DEMO",)
    app.arduino_port_var.set("DEMO")
    app.operation_mode.set(args.mode)
    if args.demo:
        devices = [DeviceInfo("DEMO-01", "Synthetic MFC", "DEMO1", 128, 500, "ml/min", 80.12, "H2", setpoint_flow=80),
                   DeviceInfo("DEMO-02", "Synthetic MFC", "DEMO2", 128, 100, "ml/min", 19.97, "Alkene", setpoint_flow=20)]
        app.roles = {"DEMO-01": "H2", "DEMO-02": "Alkene"}
        app.device_modes = {device.serial_number: "Set flow" for device in devices}
        app._update_devices(devices)
        app.plan_mode_var.set("custom")
        app.custom_flows_var.set("100 110 110 110 120")
        app.arduino.ready = app.arduino.relay_capable = app.arduino.relay_selection_capable = True
        app.arduino_status_var.set("Демонстрация · реальные порты не открываются")
        app._validate_plan()
    app._operation_mode_changed()
    app.notebook.select(2)
    app.geometry(args.size)
    app.bind("<F6>", lambda _event: app.geometry("780x540"))
    app.bind("<F7>", lambda _event: app.geometry("1180x820"))
    app.bind("<F8>", lambda _event: app.geometry("1700x900"))
    app.mainloop()


if __name__ == "__main__":
    main()
