"""Offline checks: serial communication is mocked, no COM ports are opened."""

import queue
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bronkhorst_gui import DeviceInfo, FMEASURE, FSETPOINT, GasLineApp, SerialService, build_plan


def device(serial="MFC-A", port="COM_TEST_A"):
    return DeviceInfo(serial, "Test MFC", port, 128, 100, "mls/min", 3.63306, "H2", setpoint_flow=0)


class TelemetryTests(unittest.TestCase):
    def test_zero_setpoint_does_not_replace_nonzero_measurement(self):
        service = SerialService({"timeout_s": 0.4}, {}, queue.Queue())
        service.devices = {"MFC-A": device()}
        client = Mock()
        client.readParameter.side_effect = lambda parameter: {FMEASURE: 3.63306, FSETPOINT: 0}[parameter]
        service._client = Mock(return_value=client)
        service._poll()
        self.assertEqual(service.devices["MFC-A"].setpoint_flow, 0)
        self.assertEqual(service.devices["MFC-A"].measured_flow, 3.63306)

    def test_failed_setpoint_read_keeps_valid_measurement(self):
        service = SerialService({"timeout_s": 0.4}, {}, queue.Queue())
        service.devices = {"MFC-A": device()}
        client = Mock()
        client.readParameter.side_effect = lambda parameter: 2.5 if parameter == FMEASURE else None
        service._client = Mock(return_value=client)
        service._poll()
        self.assertIsNone(service.devices["MFC-A"].setpoint_flow)
        self.assertEqual(service.devices["MFC-A"].measured_flow, 2.5)


class ManualControlsTests(unittest.TestCase):
    def setUp(self):
        store = SimpleNamespace(
            serial={"timeout_s": 0.4},
            discovery={},
            roles={},
            device_modes={"MFC-A": "Set flow", "MFC-B": "Set flow"},
            fluid_profiles={},
        )
        with patch.object(SerialService, "start"), patch.object(GasLineApp, "_refresh_available_ports"):
            try:
                self.app = GasLineApp(store)
            except tk.TclError as exc:
                self.skipTest(f"Tk display unavailable: {exc}")
        self.app.withdraw()
        self.app.service.submit = Mock()
        self.app.operation_mode.set("manual")
        self.app._operation_mode_changed()
        self.app.control_enabled.set(False)

    def tearDown(self):
        if hasattr(self, "app"):
            for timer in self.app.tk.call("after", "info"):
                self.app.after_cancel(timer)
            self.app._close()

    def test_cards_follow_connected_devices_and_keep_typed_input(self):
        self.assertEqual(len(self.app.manual_cards), 0)
        self.app._update_devices([device()])
        first_card = self.app.manual_cards["MFC-A"]
        first_card.flow_var.set("12,5")
        self.app._update_devices([device(), device("MFC-B", "COM_TEST_B")])
        self.assertEqual(len(self.app.manual_cards), 2)
        self.assertIs(self.app.manual_cards["MFC-A"], first_card)
        self.assertEqual(first_card.flow_var.get(), "12,5")
        self.assertIn("3.63306", first_card.measured_var.get())
        self.assertIn("0 mls/min", first_card.setpoint_var.get())
        self.assertEqual(str(first_card.entry.cget("state")), "normal")
        self.app.device_modes["MFC-A"] = "Read flow"
        self.app._update_devices([device(), device("MFC-B", "COM_TEST_B")])
        self.assertEqual(str(first_card.entry.cget("state")), "disabled")
        self.assertIn("чтение потока", first_card.gas_var.get())
        self.app.device_modes["MFC-A"] = "Set flow"
        self.app._update_devices([device(), device("MFC-B", "COM_TEST_B")])
        self.app.update_idletasks()
        self.assertEqual(self.app.plan_frame.winfo_manager(), "")
        self.assertEqual(self.app.plan_view_frame.winfo_manager(), "")
        self.assertEqual(self.app.auto_controls.winfo_manager(), "")
        self.app._update_devices([device("MFC-B", "COM_TEST_B")])
        self.assertEqual(list(self.app.manual_cards), ["MFC-B"])
        self.app._update_devices([])
        self.assertEqual(len(self.app.manual_cards), 0)
        self.assertEqual(self.app.manual_empty_label.winfo_manager(), "pack")
        self.app.operation_mode.set("automatic")
        self.app._operation_mode_changed()
        self.assertEqual(self.app.plan_frame.winfo_manager(), "pack")
        self.assertEqual(self.app.plan_view_frame.winfo_manager(), "pack")
        self.assertEqual(self.app.manual_frame.winfo_manager(), "")
        self.assertEqual(self.app.auto_controls.winfo_manager(), "pack")
        packed = self.app.auto_controls.master.pack_slaves()
        self.assertLess(packed.index(self.app.auto_controls), packed.index(self.app.plan_view_frame))

    def test_each_card_targets_its_device_without_confirmation_and_logs_result(self):
        self.app._update_devices([device(), device("MFC-B", "COM_TEST_B")])
        second_card = self.app.manual_cards["MFC-B"]
        second_card.flow_var.set("12,5")

        def inline_thread(*, target, args, **_kwargs):
            return SimpleNamespace(start=lambda: target(*args))

        with patch("bronkhorst_gui.threading.Thread", side_effect=inline_thread), patch("bronkhorst_gui.messagebox.askyesno") as confirm:
            second_card.set_button.invoke()
            self.app._consume_events()
            self.app.service.submit.assert_called_once_with("set", {"MFC-B": 12.5})
            second_card.zero_button.invoke()
            self.app._consume_events()
            self.app.service.submit.assert_called_with("set", {"MFC-B": 0.0})
            confirm.assert_not_called()
        self.assertEqual(second_card.flow_var.get(), "0")
        self.assertIn("3.63306", second_card.measured_var.get())
        self.assertFalse(self.app.manual_commands_pending)
        tabs = [self.app.notebook.tab(tab, "text") for tab in self.app.notebook.tabs()]
        self.assertEqual(tabs[-1], "Консоль")
        self.assertEqual(len(tabs), 4)
        self.assertIn("MFC-B: accepted setpoint 0", self.app.connection_log.get("1.0", "end"))

    def test_graph_opens_separately_and_draws_step_profile_over_time(self):
        self.app.active_plan = build_plan(start=10, stop=50, step=20, ratio=4, dwell_s=15)
        self.app._open_graph_window()
        self.app.update_idletasks()
        self.assertIsNotNone(self.app.graph_window)
        self.assertEqual(self.app.graph_window.title(), "Experiment flow graph")
        self.assertIsNotNone(self.app.flow_plot)
        texts = [
            self.app.flow_plot.itemcget(item, "text")
            for item in self.app.flow_plot.find_all()
            if self.app.flow_plot.type(item) == "text"
        ]
        self.assertIn("Time, s", texts)
        h2_line = next(
            item for item in self.app.flow_plot.find_all()
            if self.app.flow_plot.type(item) == "line" and self.app.flow_plot.itemcget(item, "fill") == "#2563eb"
        )
        coordinates = self.app.flow_plot.coords(h2_line)
        self.assertEqual(coordinates[2], coordinates[4])
        self.app._close_graph_window()
        self.assertIsNone(self.app.graph_window)
        self.assertIsNone(self.app.flow_plot)


if __name__ == "__main__":
    unittest.main()
