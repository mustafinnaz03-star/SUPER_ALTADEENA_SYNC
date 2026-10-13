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
        service.port = Mock(is_open=True)
        service._write_line = Mock()
        return service

    def test_only_matching_ack_accepts_output_state(self):
        service = self.service()
        service._read_line = Mock(side_effect=["VALVES_ACK old OFF", "VALVES_ACK token ON"])
        with patch("bronkhorst_gui.uuid4", return_value=Mock(hex="token")):
            self.assertEqual(service._set_valves({"switched": True}), {"switched": True})
        service._write_line.assert_called_once_with("VALVES token ON")

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
                self.assertNotIn(("valve_status", (1, True)), events)

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
