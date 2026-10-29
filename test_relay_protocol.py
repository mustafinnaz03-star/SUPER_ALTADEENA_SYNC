"""Relay acknowledgements and fault cleanup; no real serial ports."""
import queue
import unittest
from unittest.mock import Mock, patch

from bronkhorst_gui import ArduinoCommand, ArduinoNMRService, ExperimentPoint, ExperimentRunner
from experiment_cycle import ArduinoValveController, CycleTiming
from test_arduino_protocol import ConfirmingArduino, FakeMFCService
import test_manual_controls


class RelayProtocolTests(unittest.TestCase):
    def service(self):
        service = ArduinoNMRService({"spectrum_timeout_s": 1}, queue.Queue())
        service.ready = True
        service.relay_capable = True
        service.relay_selection_capable = True
        service.port = Mock(is_open=True)
        service._write_line = Mock()
        return service

    def test_only_matching_ack_accepts_output_state(self):
        service = self.service()
        service._read_line = Mock(side_effect=["VALVES_ACK old OFF 1", "VALVES_ACK token ON 1"])
        with patch("bronkhorst_gui.uuid4", return_value=Mock(hex="token")):
            self.assertEqual(service._set_valves({"switched": True, "relay_mask": 1}), {"switched": True, "relay_mask": 1})
        service._write_line.assert_called_once_with("VALVES token ON 1")

    def test_missing_or_wrong_ack_never_publishes_confirmed_on(self):
        for response in ("VALVES_ACK token OFF", "ERROR token ON BUSY", "BOOT NMR_RELAYS D4_D5_OFF"):
            with self.subTest(response=response):
                service = self.service()
                service._read_line = Mock(return_value=response)
                controller = ArduinoValveController(service, service.events)
                service.submit = lambda _name, payload, timeout_s: service._set_valves(payload)
                with patch("bronkhorst_gui.uuid4", return_value=Mock(hex="token")), self.assertRaises(Exception):
                    controller.set_switched(True, 1)
                self.assertIsNone(controller.is_switched)
                events = list(service.events.queue)
                self.assertNotIn(("valve_status", (1, True, 1)), events)

    def test_each_selection_is_sent_and_confirmed(self):
        for mask in (1, 2, 3):
            for switched in (False, True):
                with self.subTest(mask=mask, switched=switched):
                    service = self.service()
                    state = "ON" if switched else "OFF"
                    service._read_line = Mock(return_value=f"VALVES_ACK token {state} {mask}")
                    controller = ArduinoValveController(service, service.events, relay_mask=mask)
                    service.submit = lambda _name, payload, timeout_s: service._set_valves(payload)
                    with patch("bronkhorst_gui.uuid4", return_value=Mock(hex="token")):
                        controller.set_switched(switched, 1)
                    service._write_line.assert_called_once_with(f"VALVES token {state} {mask}")
                    self.assertIs(controller.is_switched, switched)
                    self.assertIn(("valve_status", (1, switched, mask)), list(service.events.queue))

    def test_wrong_mask_ack_does_not_accept_a_different_relay(self):
        service = self.service()
        service._read_line = Mock(return_value="VALVES_ACK token ON 3")
        with patch("bronkhorst_gui.uuid4", return_value=Mock(hex="token")), self.assertRaises(RuntimeError):
            service._set_valves({"switched": True, "relay_mask": 1})

    def test_legacy_two_relay_firmware_only_allows_all_off(self):
        service = self.service()
        service.relay_selection_capable = False
        for mask in (1, 2, 3):
            with self.subTest(mask=mask), self.assertRaises(ConnectionError):
                service._set_valves({"switched": True, "relay_mask": mask})
        service._write_line.assert_not_called()
        service._read_line = Mock(return_value="VALVES_ACK token OFF")
        with patch("bronkhorst_gui.uuid4", return_value=Mock(hex="token")):
            result = service._set_valves({"switched": False})
        self.assertEqual(result, {"switched": False, "relay_mask": 3})
        service._write_line.assert_called_once_with("VALVES token OFF")

    def test_old_ttl_firmware_cannot_drive_relays(self):
        service = self.service()
        service.relay_capable = False
        with self.assertRaises(ConnectionError):
            service._set_valves({"switched": True})
        service._write_line.assert_not_called()

    def test_ack_timeout_sends_off_and_stops_renewing_lease(self):
        service = self.service()
        service._read_line = Mock(return_value=None)
        command = ArduinoCommand("valves", {"switched": True})
        with patch("bronkhorst_gui.time.monotonic", side_effect=[0, 4]):
            service._handle(command)
        self.assertIsInstance(command.error, TimeoutError)
        self.assertFalse(service.ready)
        self.assertTrue(service._write_line.call_args.args[0].endswith(" OFF"))

    def test_keepalive_works_during_nmr_wait(self):
        service = self.service()
        service._read_line = Mock(side_effect=["TRIGGERED run 1", "NMR_DONE run 1"])
        service._trigger_and_wait({"run_id": "run", "point": 1})
        service.port.write.assert_any_call(b"KEEPALIVE\n")

    def test_reset_and_host_timeout_interrupt_nmr(self):
        for line in ("BOOT NMR_RELAYS D4_D5_OFF", "READY", "FAILSAFE HOST_TIMEOUT RELAYS_OFF"):
            service = self.service()
            service._read_line = Mock(return_value=line)
            with self.subTest(line=line), self.assertRaises(ConnectionError):
                service._trigger_and_wait({"run_id": "run", "point": 1})
            self.assertFalse(service.ready)

    def test_failed_cleanup_never_skips_mfc_zero(self):
        arduino, mfc, events = ConfirmingArduino(), FakeMFCService(), queue.Queue()
        original = arduino.submit
        def submit(name, payload, timeout_s):
            if name == "trigger" and payload["run_id"].endswith("-s2"):
                raise TimeoutError("spectrum 2 failed")
            if name == "valves" and not payload["switched"] and any(
                    call[0] == "valves" and call[1]["switched"] for call in arduino.calls):
                raise ConnectionError("OFF acknowledgement lost")
            return original(name, payload, timeout_s)
        arduino.submit = submit
        runner = ExperimentRunner(mfc, [ExperimentPoint(1, 5, 5, 0, 0)], "A", None, events,
                                  arduino=arduino, cycle=CycleTiming(0, 0, 0, 0),
                                  valves=ArduinoValveController(arduino, events))
        runner.run()
        self.assertEqual(mfc.commands[-1][0], "zero")
        self.assertIsNone(runner.valves.is_switched)
        kinds = [kind for kind, _ in events.queue]
        self.assertNotIn("finished", kinds)
        self.assertNotIn("point_completed", kinds)


class RelayShutdownTests(unittest.TestCase):
    setUp = test_manual_controls.ManualControlsTests.setUp
    tearDown = test_manual_controls.ManualControlsTests.tearDown

    def prepare_relay_test(self):
        self.app.arduino.ready = True
        self.app.arduino.relay_capable = True
        self.app.arduino.relay_selection_capable = True
        self.app.arduino.submit = Mock(side_effect=lambda _name, payload, timeout_s: {
            "switched": payload["switched"], "relay_mask": payload["relay_mask"]})
        self.app._update_operation_mode_controls()

    def test_gui_pulse_works_without_mfc_and_always_returns_all_off(self):
        self.prepare_relay_test()
        self.assertFalse(self.app.devices)
        self.assertEqual(str(self.app.relay_test_button.cget("state")), "normal")
        self.app.relay_selection_var.set("2")
        self.app.relay_test_pending = True
        cancel = Mock(is_set=Mock(return_value=False))
        self.app._run_relay_test(2, True, cancel)
        cancel.wait.assert_called_once_with(1.0)
        calls = self.app.arduino.submit.call_args_list
        self.assertEqual([(call.args[1]["switched"], call.args[1]["relay_mask"]) for call in calls],
                         [(False, 2), (True, 2), (False, 2)])
        self.app._consume_events()
        self.assertFalse(self.app.relay_test_pending)
        self.assertIn("все реле OFF подтверждено", self.app.relay_test_status_var.get())
        self.assertIn("D4/IN1=OFF, D5/IN2=OFF", self.app.valve_status_var.get())

    def test_on_failure_still_requests_off_and_reports_error(self):
        self.prepare_relay_test()
        def submit(_name, payload, timeout_s):
            if payload["switched"]:
                raise TimeoutError("lost ON acknowledgement")
            return {"switched": False, "relay_mask": payload["relay_mask"]}
        self.app.arduino.submit.side_effect = submit
        self.app.relay_test_pending = True
        self.app._run_relay_test(1, True, Mock(is_set=Mock(return_value=False)))
        self.app._consume_events()
        self.assertEqual([call.args[1]["switched"] for call in self.app.arduino.submit.call_args_list],
                         [False, True, False])
        self.assertFalse(self.app.relay_test_pending)
        self.assertIn("Ошибка проверки реле", self.app.relay_test_status_var.get())

    def test_pending_test_blocks_start_and_can_be_cancelled(self):
        self.prepare_relay_test()
        with patch("bronkhorst_gui.threading.Thread") as thread:
            self.app._request_relay_test()
        thread.return_value.start.assert_called_once()
        self.assertTrue(self.app.relay_test_pending)
        self.assertEqual(str(self.app.relay_selection_box.cget("state")), "disabled")
        self.assertEqual(str(self.app.start_experiment_button.cget("state")), "disabled")
        self.app._start_experiment()
        self.assertIsNone(self.app.runner)
        self.app._request_relay_test(False)
        self.assertTrue(self.app.relay_test_cancel.is_set())
        # Cancel before ON: no output may be energized, and OFF is confirmed.
        self.app._run_relay_test(1, True, self.app.relay_test_cancel)
        self.app._consume_events()
        self.assertTrue(all(not call.args[1]["switched"] for call in self.app.arduino.submit.call_args_list))
        self.assertFalse(self.app.relay_test_pending)

    def test_running_experiment_and_old_firmware_reject_manual_test(self):
        self.prepare_relay_test()
        self.app.runner = Mock(is_alive=Mock(return_value=True))
        self.app._request_relay_test()
        self.app._request_relay_test(False)
        self.app.arduino.submit.assert_not_called()
        self.app.runner = None
        self.app.arduino.relay_selection_capable = False
        self.app._request_relay_test()
        self.app.arduino.submit.assert_not_called()
        self.assertIn("Обновите прошивку", self.app.relay_test_status_var.get())

    def test_window_close_waits_for_manual_test_off(self):
        self.prepare_relay_test()
        self.app.relay_test_pending = True
        self.app.relay_test_cancel = Mock()
        self.app.service.shutdown = Mock()
        self.app.arduino.shutdown = Mock()
        self.app._close()
        self.app.relay_test_cancel.set.assert_called_once()
        self.app.service.shutdown.assert_not_called()
        self.app.arduino.shutdown.assert_not_called()
        self.app.relay_test_pending = False

    def test_selection_defaults_to_d4_and_is_saved_and_locked_during_run(self):
        self.assertEqual(self.app._selected_relay_mask(), 1)
        self.app.operation_mode.set("automatic-arduino-valves")
        self.app._operation_mode_changed()
        self.assertEqual(str(self.app.relay_selection_box.cget("state")), "readonly")
        self.app.relay_selection_var.set("2")
        self.assertIn("D4 → IN1 всегда HIGH", self.app.relay_wiring_var.get())
        self.app.store.save_experiment = Mock()
        self.app._save_experiment_design()
        self.assertEqual(self.app.store.save_experiment.call_args.args[0]["relay_mask"], 2)
        self.app.runner = Mock(is_alive=Mock(return_value=True), valves=Mock(relay_mask=2))
        self.app._update_operation_mode_controls()
        self.assertEqual(str(self.app.relay_selection_box.cget("state")), "disabled")
        self.assertEqual(str(self.app.save_relay_selection_button.cget("state")), "disabled")

    def test_status_reports_selected_relay_not_assumed_valve_positions(self):
        self.app.events.put(("valve_status", (1, True, 1)))
        self.app._consume_events()
        self.assertIn("D4/IN1=ON, D5/IN2=OFF", self.app.valve_status_var.get())
        self.app.events.put(("valve_status", (1, True, 2)))
        self.app._consume_events()
        self.assertIn("D4/IN1=OFF, D5/IN2=ON", self.app.valve_status_var.get())

    def test_window_close_keeps_com_workers_until_runner_cleanup_finishes(self):
        runner = Mock(is_alive=Mock(return_value=True))
        self.app.runner = runner
        self.app.service.shutdown = Mock()
        self.app.arduino.shutdown = Mock()
        self.app._close()
        runner.cancel.assert_called_once()
        self.app.service.shutdown.assert_not_called()
        self.app.arduino.shutdown.assert_not_called()


if __name__ == "__main__":
    unittest.main()
