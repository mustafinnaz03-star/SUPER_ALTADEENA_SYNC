"""Offline checks for the Arduino/NMR handshake; no COM port is opened."""

import queue
import unittest
from unittest.mock import Mock

from bronkhorst_gui import ArduinoNMRService, ExperimentPoint, ExperimentRunner


class FakeMFCService:
    def __init__(self):
        self.commands = []

    def submit(self, name, payload):
        self.commands.append((name, payload))


class ConfirmingArduino:
    def __init__(self):
        self.calls = []

    def submit(self, name, payload, timeout_s):
        self.calls.append((name, payload, timeout_s))
        if name == "valves":
            return {"switched": payload["switched"]}
        return {"run_id": payload["run_id"], "point": payload["point"]}


class TimingOutArduino:
    def submit(self, name, payload, timeout_s):
        raise TimeoutError("no matching NMR_DONE TTL")


class ArduinoExperimentTests(unittest.TestCase):
    def test_console_reports_ttl_only_after_matching_acknowledgements(self):
        events = queue.Queue()
        service = ArduinoNMRService({"spectrum_timeout_s": 1}, events)
        service.ready = True
        service._write_line = Mock()
        service._read_line = Mock(side_effect=["TRIGGERED old-run 1", "TRIGGERED run-test 1", "NMR_DONE run-test 1"])
        service._trigger_and_wait({"run_id": "run-test", "point": 1})
        log = []
        while not events.empty():
            kind, value = events.get_nowait()
            if kind == "arduino_log":
                log.append(value)
        sent = [line for line in log if "подтвердила отправку TTL" in line]
        received = [line for line in log if "получен ответный TTL" in line]
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(received), 1)
        self.assertLess(log.index(sent[0]), log.index(received[0]))

    def test_next_point_waits_for_confirmed_spectrum(self):
        service, arduino, events = FakeMFCService(), ConfirmingArduino(), queue.Queue()
        plan = [
            ExperimentPoint(1, 10, 8, 2, 0.001),
            ExperimentPoint(2, 20, 16, 4, 0.001),
        ]
        runner = ExperimentRunner(service, plan, "H2", "ALKENE", events, arduino=arduino, arduino_timeout_s=3)
        runner.run()

        event_kinds = []
        while not events.empty():
            event_kinds.append(events.get_nowait()[0])
        self.assertEqual([call[1]["point"] for call in arduino.calls], [1, 1, 2, 2])
        self.assertNotEqual(arduino.calls[0][1]["run_id"], arduino.calls[1][1]["run_id"])
        self.assertEqual(event_kinds.count("nmr_trigger_wait"), 4)
        self.assertEqual(event_kinds.count("spectrum_confirmed"), 4)
        self.assertEqual(event_kinds.count("point_completed"), 2)
        self.assertNotIn("runner_error", event_kinds)
        self.assertEqual(service.commands[-1][0], "zero")
        self.assertEqual(set(service.commands[-1][1]), {"H2", "ALKENE"})

    def test_timeout_reports_error_and_never_accepts_a_spectrum(self):
        service, events = FakeMFCService(), queue.Queue()
        plan = [ExperimentPoint(1, 10, 8, 2, 0.001)]
        runner = ExperimentRunner(service, plan, "H2", "ALKENE", events, arduino=TimingOutArduino(), arduino_timeout_s=3)
        runner.run()

        received = []
        while not events.empty():
            received.append(events.get_nowait())
        self.assertIn("nmr_trigger_wait", [kind for kind, _ in received])
        self.assertNotIn("spectrum_confirmed", [kind for kind, _ in received])
        errors = [payload for kind, payload in received if kind == "runner_error"]
        self.assertEqual(errors, ["no matching NMR_DONE TTL"])
        self.assertEqual(service.commands[-1][0], "zero")
        self.assertEqual(set(service.commands[-1][1]), {"H2", "ALKENE"})

    def test_single_mfc_runner_writes_and_zeroes_only_one_device(self):
        service, events = FakeMFCService(), queue.Queue()
        plan = [ExperimentPoint(1, 10, 10, 0, 0.001)]
        runner = ExperimentRunner(service, plan, "MFC-ONLY", None, events)
        runner.run()

        self.assertEqual(service.commands[0], ("set", {"MFC-ONLY": 10}))
        self.assertEqual(service.commands[-1], ("zero", {"MFC-ONLY": 10}))
        received = []
        while not events.empty():
            received.append(events.get_nowait())
        self.assertNotIn("runner_error", [kind for kind, _ in received])


if __name__ == "__main__":
    unittest.main()
