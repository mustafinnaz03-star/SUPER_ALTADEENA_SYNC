"""Manual held relay control: no physical serial ports or real workers."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_manual_controls


def inline_thread(*, target, args, **_kwargs):
    return SimpleNamespace(start=lambda: target(*args))


class ManualValveTests(unittest.TestCase):
    setUp = test_manual_controls.ManualControlsTests.setUp
    tearDown = test_manual_controls.ManualControlsTests.tearDown

    def prepare(self):
        self.app.arduino.ready = True
        self.app.arduino.relay_capable = True
        self.app.arduino.relay_selection_capable = True
        self.app.arduino.submit = Mock(side_effect=lambda _name, payload, timeout_s: {
            "switched": payload["switched"], "relay_mask": payload["relay_mask"]})
        self.app._update_operation_mode_controls()

    def command(self, on):
        with patch("bronkhorst_gui.threading.Thread", side_effect=inline_thread):
            self.app._request_manual_valve(on)
            self.app._consume_events()

    def test_manual_panel_and_connection_visible_without_mfc(self):
        self.prepare()
        self.assertFalse(self.app.devices)
        self.assertEqual(self.app.manual_valve_frame.winfo_manager(), "pack")
        self.assertEqual(self.app.manual_arduino_port_box.cget("textvariable"), str(self.app.arduino_port_var))
        self.assertEqual(str(self.app.manual_valve_on_button.cget("state")), "normal")
        self.assertEqual(self.app.experiment_columns.winfo_manager(), "")
        self.app.operation_mode.set("automatic-arduino")
        self.app._operation_mode_changed()
        self.assertEqual(self.app.manual_valve_frame.winfo_manager(), "")

    def test_selected_on_is_held_and_all_off_is_explicit_no_ttl_or_mfc(self):
        self.prepare()
        for mask in (1, 2, 3):
            with self.subTest(mask=mask):
                self.app.relay_selection_var.set(str(mask))
                self.app.arduino.submit.reset_mock()
                self.command(True)
                self.assertEqual(self.app.arduino.submit.call_count, 1)
                self.app.arduino.submit.assert_called_with("valves", {
                    "switched": True, "point": 0, "relay_mask": mask}, timeout_s=5)
                self.assertTrue(self.app.manual_valve_requires_off)
                self.assertFalse(self.app.manual_valve_pending)
                self.assertIn("Arduino подтвердила", self.app.manual_valve_status_var.get())
                self.assertEqual(str(self.app.manual_relay_selection_box.cget("state")), "disabled")
                self.command(False)
                self.app.arduino.submit.assert_called_with("valves", {
                    "switched": False, "point": 0, "relay_mask": 3}, timeout_s=5)
                self.assertFalse(self.app.manual_valve_requires_off)
                self.assertIn("D4/IN1=OFF, D5/IN2=OFF", self.app.manual_valve_status_var.get())
        self.app.service.submit.assert_not_called()
        self.assertTrue(all(call.args[0] == "valves" for call in self.app.arduino.submit.call_args_list))

    def test_on_and_pending_commands_block_mode_change_and_experiment(self):
        self.prepare()
        self.command(True)
        self.app.operation_mode.set("automatic-arduino")
        self.app._operation_mode_changed()
        self.assertEqual(self.app.operation_mode.get(), "manual")
        self.app._start_experiment()
        self.assertIsNone(self.app.runner)
        self.app._request_relay_test()
        self.assertIn("Сначала", self.app.relay_test_status_var.get())
        self.command(False)
        self.app.operation_mode.set("automatic-arduino")
        self.app._operation_mode_changed()
        self.assertEqual(self.app.operation_mode.get(), "automatic-arduino")

    def test_disconnect_and_reconnect_cannot_interrupt_held_on(self):
        self.prepare()
        self.command(True)
        self.app.arduino.submit.reset_mock()
        with patch("bronkhorst_gui.threading.Thread") as thread:
            self.app._disconnect_arduino()
            self.app._connect_arduino()
        thread.assert_not_called()
        self.app.arduino.submit.assert_not_called()

    def test_wrong_mode_busy_old_firmware_and_disconnected_reject_on(self):
        self.prepare()
        self.app.operation_mode.set("automatic-arduino")
        self.app._request_manual_valve(True)
        self.app.operation_mode.set("manual")
        self.app.runner = Mock(is_alive=Mock(return_value=True))
        self.app._request_manual_valve(True)
        self.app.runner = None
        self.app.arduino.ready = False
        self.app._request_manual_valve(True)
        self.app.arduino.ready = True
        self.app.arduino.relay_selection_capable = False
        self.app._request_manual_valve(True)
        self.app.arduino.submit.assert_not_called()
        self.assertIn("прошивка", self.app.manual_valve_status_var.get())
        self.command(False)  # Legacy relay firmware may still accept both OFF.
        self.assertFalse(self.app.manual_valve_requires_off)

    def test_pending_on_can_be_cancelled_before_switch_and_locks_other_commands(self):
        self.prepare()
        with patch("bronkhorst_gui.threading.Thread"):
            self.app._request_manual_valve(True)
        self.assertTrue(self.app.manual_valve_pending)
        self.assertEqual(str(self.app.manual_valve_on_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.manual_valve_off_button.cget("state")), "normal")
        self.app._request_manual_valve(False)
        self.assertTrue(self.app.manual_valve_cancel.is_set())
        self.app._run_manual_valve(True, 1, self.app.manual_valve_cancel)
        self.app._consume_events()
        self.assertFalse(self.app.manual_valve_pending)
        self.assertFalse(self.app.manual_valve_requires_off)
        self.assertTrue(all(not call.args[1]["switched"] for call in self.app.arduino.submit.call_args_list))

    def test_failed_on_attempts_off_and_keeps_error_visible(self):
        self.prepare()
        def submit(_name, payload, timeout_s):
            if payload["switched"]:
                raise TimeoutError("ON acknowledgement lost")
            return {"switched": False, "relay_mask": payload["relay_mask"]}
        self.app.arduino.submit.side_effect = submit
        self.command(True)
        self.assertEqual([call.args[1]["switched"] for call in self.app.arduino.submit.call_args_list], [True, False])
        self.assertFalse(self.app.manual_valve_requires_off)
        self.assertIn("Ошибка управления реле", self.app.manual_valve_status_var.get())

    def test_failed_off_and_connection_loss_do_not_claim_off_or_clear_interlock(self):
        self.prepare()
        self.command(True)
        self.app.arduino.submit.side_effect = TimeoutError("OFF acknowledgement lost")
        self.command(False)
        self.assertTrue(self.app.manual_valve_requires_off)
        self.assertIn("Ошибка", self.app.manual_valve_status_var.get())
        self.app.arduino.ready = False
        self.app.events.put(("valve_status", (0, None, None)))
        self.app._consume_events()
        self.assertIn("не подтверждено", self.app.manual_valve_status_var.get())
        self.assertTrue(self.app.manual_valve_requires_off)
        self.assertEqual(str(self.app.manual_valve_on_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.manual_arduino_connect_button.cget("state")), "normal")
        # Reconnected READY is not an OFF acknowledgement and never reissues ON.
        self.app.arduino.ready = True
        self.app.events.put(("arduino_connected", {"port": "COM_TEST"}))
        self.app._consume_events()
        self.assertTrue(self.app.manual_valve_requires_off)
        self.assertEqual(str(self.app.manual_valve_on_button.cget("state")), "disabled")

    def test_close_during_pending_on_requests_off_and_waits(self):
        self.prepare()
        with patch("bronkhorst_gui.threading.Thread"):
            self.app._request_manual_valve(True)
        self.app.service.shutdown = Mock()
        self.app.arduino.shutdown = Mock()
        self.app._close()
        self.assertTrue(self.app.manual_valve_cancel.is_set())
        self.app.service.shutdown.assert_not_called()
        self.app.arduino.shutdown.assert_not_called()
        self.app._run_manual_valve(True, 1, self.app.manual_valve_cancel)
        self.app._consume_events()
        self.assertFalse(self.app.manual_valve_pending)
        self.assertFalse(self.app.manual_valve_requires_off)

    def test_off_requested_after_worker_return_is_not_lost(self):
        self.prepare()
        with patch("bronkhorst_gui.threading.Thread", side_effect=inline_thread):
            self.app._request_manual_valve(True)
            # Worker finished, but its queued result has not reached the UI yet.
            self.app._request_manual_valve(False)
            self.app._consume_events()
        self.assertEqual([call.args[1]["switched"] for call in self.app.arduino.submit.call_args_list], [True, False])
        self.assertFalse(self.app.manual_valve_requires_off)
        self.assertFalse(self.app.manual_valve_pending)


if __name__ == "__main__":
    unittest.main()
