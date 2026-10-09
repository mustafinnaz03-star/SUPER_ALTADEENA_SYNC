"""Offline checks for the Arduino/NMR handshake; no COM port is opened."""

import queue
import unittest

from bronkhorst_gui import ExperimentPoint, ExperimentRunner


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
        return {"run_id": payload["run_id"], "point": payload["point"]}


class TimingOutArduino:
    def submit(self, name, payload, timeout_s):
        raise TimeoutError("no matching NMR_DONE TTL")


class ArduinoExperimentTests(unittest.TestCase):
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
        self.assertEqual([call[1]["point"] for call in arduino.calls], [1, 2])
        self.assertEqual(event_kinds.count("nmr_trigger_wait"), 2)
        self.assertEqual(event_kinds.count("spectrum_confirmed"), 2)
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


if __name__ == "__main__":
    unittest.main()
