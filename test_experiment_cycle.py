"""Offline cycle ordering, cancellation, persistence and design-window checks."""

import json
import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from bronkhorst_gui import ArduinoNMRService, ConfigStore, ExperimentPoint, ExperimentRunner
from experiment_cycle import CycleTiming, OPERATION_LABELS, ArduinoValveController
from test_arduino_protocol import ConfirmingArduino, FakeMFCService
import test_manual_controls as manual_controls
from test_manual_controls import device


def drain(events):
    result = []
    while not events.empty():
        result.append(events.get_nowait())
    return result


class CycleTests(unittest.TestCase):
    def test_invalid_timing_rejected(self):
        for value in (-1, float("nan"), float("inf")):
            for key in ("t1", "t2", "t3", "t4"):
                with self.subTest(value=value, key=key), self.assertRaises(ValueError):
                    CycleTiming(**{key: value})
        self.assertEqual(CycleTiming(0, 0, 0, 0).delay_s, 0)

    def test_complete_cycle_keeps_flow_until_two_spectra_and_valve_close(self):
        mfc, arduino, events = FakeMFCService(), ConfirmingArduino(), queue.Queue()
        plan = [ExperimentPoint(1, 10, 8, 2, 0), ExperimentPoint(2, 20, 16, 4, 0)]
        runner = ExperimentRunner(mfc, plan, "H2", "ALKENE", events, arduino=arduino,
                                  cycle=CycleTiming(0, 0, 0, 0), valves=ArduinoValveController(arduino, events))
        runner.run()
        observed = drain(events)
        sequence = []
        for kind, payload in observed:
            if kind == "cycle_stage":
                sequence.append(payload[1])
            elif kind == "nmr_trigger_wait":
                sequence.append(f"nmr{payload[1]}")
            elif kind == "valve_status" and payload[0] > 0 and payload[1] is not None:
                sequence.append("open" if payload[1] else "close")
            elif kind == "point_completed":
                sequence.append("point_done")
        expected = ["t1", "nmr1", "t2", "open", "t3", "nmr2", "t4", "close", "point_done"]
        self.assertEqual(sequence, expected * 2)
        self.assertEqual(mfc.commands, [("set", {"H2": 8, "ALKENE": 2}),
                                        ("set", {"H2": 16, "ALKENE": 4}),
                                        ("zero", {"H2": 16, "ALKENE": 4})])
        self.assertEqual(sum(call[0] == "trigger" for call in arduino.calls), 4)
        self.assertFalse(runner.valves.is_switched)
        self.assertNotIn("runner_error", [kind for kind, _ in observed])

    def test_each_delay_runs_with_its_own_duration(self):
        events = queue.Queue()
        runner = ExperimentRunner(FakeMFCService(), [ExperimentPoint(1, 10, 10, 0, 0)], "A", None,
                                  events, arduino=ConfirmingArduino(), cycle=CycleTiming(1, 2, 3, 4))
        # Avoid wall-clock sleeps here; the real interruptible wait is tested below.
        runner._wait_delay = Mock()
        runner.run()
        self.assertEqual([(call.args[1], call.args[2]) for call in runner._wait_delay.call_args_list],
                         [("t1", 1), ("t2", 2), ("t3", 3), ("t4", 4)])

    def test_stop_during_each_delay_zeroes_flow_without_advancing(self):
        for delay in ("t1", "t2", "t3", "t4"):
            with self.subTest(delay=delay):
                events, mfc, arduino = queue.Queue(), FakeMFCService(), ConfirmingArduino()
                timing = {key: 0 for key in ("t1", "t2", "t3", "t4")}
                timing[delay] = 30
                runner = ExperimentRunner(mfc, [ExperimentPoint(1, 10, 10, 0, 0), ExperimentPoint(2, 20, 20, 0, 0)],
                                          "A", None, events, arduino=arduino,
                                          cycle=CycleTiming(**timing), valves=ArduinoValveController(arduino, events))
                runner.start()
                while True:
                    kind, payload = events.get(timeout=2)
                    if kind == "cycle_stage" and payload[1] == delay:
                        break
                runner.cancel()
                runner.join(timeout=2)
                self.assertFalse(runner.is_alive())
                self.assertEqual([name for name, _ in mfc.commands], ["set", "zero"])
                self.assertFalse(runner.valves.is_switched)
                self.assertNotIn("point_completed", [kind for kind, _ in drain(events)])

    def test_second_spectrum_failure_resets_relays_and_prevents_next_point(self):
        events, mfc = queue.Queue(), FakeMFCService()
        arduino = ConfirmingArduino()
        original_submit = arduino.submit
        count = 0
        def fail_second(name, payload, timeout_s):
            nonlocal count
            if name == "trigger":
                count += 1
                if count == 2:
                    raise TimeoutError("second spectrum timeout")
            return original_submit(name, payload, timeout_s)
        arduino.submit = fail_second
        runner = ExperimentRunner(mfc, [ExperimentPoint(1, 10, 10, 0, 0), ExperimentPoint(2, 20, 20, 0, 0)],
                                  "A", None, events, arduino=arduino,
                                  cycle=CycleTiming(0, 0, 0, 0), valves=ArduinoValveController(arduino, events))
        runner.run()
        observed = drain(events)
        self.assertEqual(sum(kind == "spectrum_confirmed" for kind, _ in observed), 1)
        self.assertNotIn("point_completed", [kind for kind, _ in observed])
        self.assertEqual([name for name, _ in mfc.commands], ["set", "zero"])
        self.assertFalse(runner.valves.is_switched)
        self.assertEqual([payload[1] for kind, payload in observed if kind == "valve_status" and payload[1] is not None], [False, True, False])

    def test_stop_waiting_for_either_spectrum_aborts_handshake(self):
        for spectrum in (1, 2):
            with self.subTest(spectrum=spectrum):
                events, mfc = queue.Queue(), FakeMFCService()
                arduino = ArduinoNMRService({"spectrum_timeout_s": 1}, events)
                arduino.ready = True
                arduino._write_line = Mock()
                runner = ExperimentRunner(mfc, [ExperimentPoint(1, 10, 10, 0, 0)], "A", None,
                                          events, cycle=CycleTiming(0, 0, 0, 0),
                                          valves=ArduinoValveController(arduino, events), arduino=arduino)
                arduino.submit = lambda name, payload, timeout_s: (arduino._trigger_and_wait(payload) if name == "trigger" else {"switched": payload["switched"], "relay_mask": payload["relay_mask"]})
                responses = iter([f"TRIGGERED {runner.run_id}-s1 1", f"NMR_DONE {runner.run_id}-s1 1"] if spectrum == 2 else [])

                def read_line():
                    line = next(responses, None)
                    if line is None:
                        runner.cancel()
                    return line

                arduino._read_line = read_line
                runner.run()
                self.assertIn((f"ABORT {runner.run_id}-s{spectrum} 1",),
                              [call.args for call in arduino._write_line.call_args_list])
                self.assertEqual(mfc.commands[-1][0], "zero")
                self.assertFalse(runner.valves.is_switched)
                self.assertNotIn("point_completed", [kind for kind, _ in drain(events)])

    def test_late_first_spectrum_response_cannot_accept_second_spectrum(self):
        events = queue.Queue()
        arduino = ArduinoNMRService({"spectrum_timeout_s": 1}, events)
        arduino.ready = True
        arduino._write_line = Mock()
        arduino._read_line = Mock(side_effect=["TRIGGERED run-s2 1", "NMR_DONE run-s1 1", "NMR_DONE run-s2 1"])
        result = arduino._trigger_and_wait({"run_id": "run-s2", "point": 1})
        self.assertEqual(result["run_id"], "run-s2")
        self.assertEqual(arduino._read_line.call_count, 3)

    def test_design_persistence_preserves_connection_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps({"roles": {"A": "H2"}, "arduino": {"port": "COM9"}}), encoding="utf-8")
            store = ConfigStore(path)
            design = {"start": 5, "stop": 15, "step": 5, "ratio": 4, "t1": 1, "t2": 2, "t3": 3, "t4": 4}
            store.save_experiment(design)
            restored = ConfigStore(path)
            self.assertEqual(restored.experiment, design)
            self.assertEqual(restored.roles, {"A": "H2"})
            self.assertEqual(restored.arduino["port"], "COM9")


class DesignWindowTests(unittest.TestCase):
    setUp = manual_controls.ManualControlsTests.setUp
    tearDown = manual_controls.ManualControlsTests.tearDown

    def test_exactly_three_modes_and_separate_editor_available_without_mfc(self):
        self.assertEqual([button.cget("value") for button in self.app.mode_buttons], list(OPERATION_LABELS))
        self.app._open_design_window()
        self.app.update()
        self.assertEqual(self.app.design_window.state(), "normal")
        for key, entry in self.app.cycle_entries.items():
            self.assertEqual(str(entry.cget("state")), "normal")
        self.app.cycle_vars["t2"].set("2,5")
        self.assertEqual(self.app._current_cycle().t2, 2.5)
        self.assertIn("t2 = 2,5 с", self.app.design_summary_var.get())
        self.app.operation_mode.set("automatic-arduino-valves")
        self.app._operation_mode_changed()
        self.assertTrue(self.app.cycle_diagram.valves_enabled)
        self.assertIn("D4/D5", self.app.valve_status_var.get())

    def test_running_cycle_locks_design_and_save(self):
        self.app._update_devices([device()])
        self.app.runner = Mock(valves=None, is_alive=Mock(return_value=True))
        self.app._update_operation_mode_controls()
        for entry in (*self.app.plan_field_entries.values(), *self.app.cycle_entries.values()):
            self.assertEqual(str(entry.cget("state")), "disabled")
        self.assertEqual(str(self.app.save_design_button.cget("state")), "disabled")
        self.assertTrue(all(str(button.cget("state")) == "disabled" for button in self.app.mode_buttons))

    def test_delay_edit_invalidates_preview_and_invalid_delay_blocks_start(self):
        self.app._update_devices([device()])
        self.app.plan_vars["stop"].set("50")
        self.assertIsNotNone(self.app._validate_plan())
        self.app.cycle_vars["t4"].set("3")
        self.assertFalse(self.app.schedule_tree.get_children())
        self.app.cycle_vars["t3"].set("-1")
        self.assertIsNone(self.app._validate_plan())
        self.assertIn("не меньше нуля", self.app.plan_var.get())

    def test_save_design_does_not_need_devices(self):
        self.app.store.save_experiment = Mock()
        self.app._save_experiment_design()
        self.assertEqual(self.app.store.save_experiment.call_args.args[0]["t1"], 60)

    def test_small_designer_keeps_fields_in_view_and_diagram_scrollable(self):
        self.app._open_design_window()
        self.app.design_window.geometry("680x500")
        self.app.update()
        for entry in self.app.cycle_entries.values():
            self.assertLessEqual(entry.winfo_rootx() + entry.winfo_width(),
                                 self.app.design_window.winfo_rootx() + self.app.design_window.winfo_width())
        self.assertLess(self.app.cycle_diagram.xview()[1], 1)
        self.app.cycle_diagram.xview_moveto(1)
        self.assertEqual(self.app.cycle_diagram.xview()[1], 1)
        for key in ("t1", "t2", "t3", "t4"):
            self.assertTrue(self.app.cycle_diagram.find_withtag(f"delay-{key}"))


if __name__ == "__main__":
    unittest.main()
