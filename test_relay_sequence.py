"""Reversed experiment relay timing; serial/MFC hardware is mocked."""
import queue
import unittest
from unittest.mock import Mock, patch

from bronkhorst_gui import ExperimentPoint, ExperimentRunner
from experiment_cycle import ArduinoValveController, CycleTiming
from test_arduino_protocol import FakeMFCService
from test_arduino_recovery import RecoveryArduino
import test_manual_controls


class RelaySequenceTests(unittest.TestCase):
    def runner(self, count=1, recover=False):
        events, mfc, board = queue.Queue(), FakeMFCService(), RecoveryArduino()
        plan = [ExperimentPoint(i, i * 5, i * 5, 0, 0) for i in range(1, count + 1)]
        runner = ExperimentRunner(mfc, plan, "A", None, events, arduino=board,
                                  cycle=CycleTiming(0, 0, 0, 0),
                                  valves=ArduinoValveController(board, events),
                                  relay_initial_on=True, recover_after_off=recover)
        return runner, board, mfc, events

    def test_reverse_holds_on_for_first_spectrum_and_off_for_second(self):
        runner, board, mfc, events = self.runner(count=2)
        stages = []
        runner._wait_delay = lambda point, name, seconds: stages.append((point.number, name, runner.valves.is_switched))
        runner.run()
        self.assertEqual(stages, [(i, name, state) for i in (1, 2)
                                 for name, state in (("t1", True), ("t2", True), ("t3", False), ("t4", False))])
        self.assertEqual([payload["switched"] for name, payload in board.calls if name == "valves"],
                         [True, False, True, False, False])
        self.assertEqual(sum(name == "trigger" for name, _ in board.calls), 4)
        self.assertEqual([name for name, _ in mfc.commands], ["set", "set", "zero"])
        self.assertFalse(runner.valves.is_switched)
        self.assertNotIn("runner_error", [kind for kind, _ in events.queue])

    def test_single_last_point_never_reenergizes_after_second_spectrum(self):
        runner, board, _, _ = self.runner()
        runner.run()
        self.assertEqual([payload["switched"] for name, payload in board.calls if name == "valves"],
                         [True, False, False])

    def test_stop_at_any_delay_always_ends_off_and_zero(self):
        for target in ("t1", "t2", "t3", "t4"):
            with self.subTest(stage=target):
                runner, board, mfc, events = self.runner(count=2)
                def wait(point, name, seconds):
                    if name == target:
                        runner.cancel()
                        raise InterruptedError("Stopped")
                runner._wait_delay = wait
                runner.run()
                self.assertFalse(runner.valves.is_switched)
                self.assertEqual(mfc.commands[-1][0], "zero")
                self.assertNotIn("point_completed", [kind for kind, _ in events.queue])
                self.assertEqual([name for name, _ in mfc.commands], ["set", "zero"])

    def test_cancel_before_start_never_sends_on(self):
        runner, board, mfc, _ = self.runner()
        runner.cancel()
        runner.run()
        self.assertFalse(any(name == "valves" and payload["switched"] for name, payload in board.calls))
        self.assertEqual([name for name, _ in mfc.commands], ["zero"])

    def test_failed_initial_on_aborts_before_flow_or_ttl_and_cleans_off(self):
        runner, board, mfc, events = self.runner(recover=True)
        board.on_failure = OSError("ON failure")
        runner.run()
        self.assertFalse(runner.valves.is_switched)
        self.assertEqual([name for name, _ in mfc.commands], ["zero"])
        self.assertFalse(any(name in ("trigger", "recover_off") for name, _ in board.calls))
        self.assertIn("runner_error", [kind for kind, _ in events.queue])

    def test_mid_cycle_off_failure_does_not_retry_or_repeat_first_spectrum(self):
        runner, board, mfc, _ = self.runner(recover=True)
        board.off_failures[1] = OSError("mid-cycle OFF failure")
        runner.run()
        self.assertEqual(sum(name == "trigger" for name, _ in board.calls), 1)
        self.assertFalse(any(name == "recover_off" for name, _ in board.calls))
        self.assertEqual(mfc.commands[-1][0], "zero")

    def test_t1_recovery_restores_on_before_reapplying_flow(self):
        runner, board, mfc, events = self.runner(recover=True)
        states = []
        original_set = mfc.submit
        def submit(name, payload):
            if name == "set":
                states.append(runner.valves.is_switched)
            return original_set(name, payload)
        mfc.submit = submit
        lost = False
        def wait(point, name, seconds):
            nonlocal lost
            if name == "t1" and not lost:
                lost = True
                board.ready = False
                raise OSError("lost during t1")
        runner._wait_delay = wait
        runner.run()
        self.assertEqual(states, [True, True])
        self.assertEqual(sum(name == "recover_off" for name, _ in board.calls), 1)
        self.assertEqual(sum(name == "trigger" for name, _ in board.calls), 2)
        self.assertNotIn("runner_error", [kind for kind, _ in events.queue])

    def test_stop_while_restoring_on_after_t1_recovery_never_reapplies_flow(self):
        runner, board, mfc, _ = self.runner(recover=True)
        original = board.submit
        def submit(name, payload, timeout_s):
            result = original(name, payload, timeout_s)
            if name == "valves" and payload["switched"] and payload["point"] == 1:
                runner.cancel()
            return result
        board.submit = submit
        def wait(point, name, seconds):
            board.ready = False
            raise OSError("lost during t1")
        runner._wait_delay = wait
        runner.run()
        self.assertEqual([name for name, _ in mfc.commands], ["set", "zero", "zero"])
        self.assertFalse(any(name == "trigger" for name, _ in board.calls))
        self.assertFalse(runner.valves.is_switched)

    def test_t4_recovery_keeps_off_until_the_next_point_without_duplicate_ttl(self):
        runner, board, _, events = self.runner(count=2, recover=True)
        def wait(point, name, seconds):
            if point.number == 1 and name == "t4":
                board.ready = False
                raise OSError("lost during t4")
        runner._wait_delay = wait
        runner.run()
        index = next(i for i, call in enumerate(board.calls) if call[0] == "recover_off")
        ons_after_recovery = [payload["point"] for name, payload in board.calls[index + 1:]
                              if name == "valves" and payload["switched"]]
        self.assertEqual(ons_after_recovery, [2])
        self.assertEqual(sum(name == "trigger" for name, _ in board.calls), 4)
        self.assertIn(1, runner.t4_interrupted_points)
        self.assertFalse(runner.valves.is_switched)
        self.assertNotIn("runner_error", [kind for kind, _ in events.queue])

    def test_failed_boundary_on_is_not_treated_as_recoverable_off(self):
        runner, board, mfc, events = self.runner(count=2, recover=True)
        original = board.submit
        def submit(name, payload, timeout_s):
            if name == "valves" and payload["switched"] and payload["point"] == 1:
                raise OSError("next-point ON failed")
            return original(name, payload, timeout_s)
        board.submit = submit
        runner.run()
        self.assertFalse(any(name == "recover_off" for name, _ in board.calls))
        self.assertEqual(sum(name == "trigger" for name, _ in board.calls), 2)
        self.assertEqual(mfc.commands[-1][0], "zero")
        self.assertFalse(runner.valves.is_switched)
        self.assertIn("runner_error", [kind for kind, _ in events.queue])


class RelaySequenceDesignTests(unittest.TestCase):
    setUp = test_manual_controls.ManualControlsTests.setUp
    tearDown = test_manual_controls.ManualControlsTests.tearDown

    def test_reverse_choice_updates_diagram_and_persists_without_hardware(self):
        self.assertFalse(self.app.relay_initial_on_var.get())
        self.app.operation_mode.set("automatic-arduino-valves")
        self.app._operation_mode_changed()
        self.app.relay_initial_on_checkbutton.invoke()
        self.assertTrue(self.app.relay_initial_on_var.get())
        self.assertTrue(self.app.cycle_diagram.relay_initial_on)
        self.assertIn("Сначала ON", self.app.relay_cycle_hint_var.get())
        self.assertIn("после t2 → OFF", self.app.design_summary_var.get())
        texts = [self.app.cycle_diagram.itemcget(item, "text")
                 for item in self.app.cycle_diagram.find_all()
                 if self.app.cycle_diagram.type(item) == "text"]
        self.assertIn("ON к след. точке", texts)
        self.app.store.save_experiment = Mock()
        self.app._save_experiment_design()
        self.assertIs(self.app.store.save_experiment.call_args.args[0]["relay_initial_on"], True)
        self.app.service.submit.assert_not_called()

    def test_running_cycle_locks_sequence_choice(self):
        self.app.operation_mode.set("automatic-arduino-valves")
        self.app.runner = Mock(is_alive=Mock(return_value=True), valves=Mock())
        self.app._update_operation_mode_controls()
        self.assertEqual(str(self.app.relay_initial_on_checkbutton.cget("state")), "disabled")

    def test_start_passes_reversed_order_only_to_valve_mode_and_explains_it(self):
        self.app._update_devices([test_manual_controls.device()])
        self.app.control_enabled.set(True)
        self.app.relay_initial_on_var.set(True)
        self.app.arduino.ready = self.app.arduino.relay_selection_capable = True
        self.app.arduino.settings.update({"port": "COM17", "baudrate": 115200})
        self.app._arduino_settings_from_ui = Mock(return_value={
            "port": "COM17", "baudrate": 115200, "spectrum_timeout_s": 180})
        self.app._validate_plan = Mock(return_value=[ExperimentPoint(1, 5, 5, 0, 0)])
        self.app._experiment_devices = Mock(return_value=(test_manual_controls.device(), None))
        for mode, expected in (("automatic-arduino-valves", True), ("automatic-arduino", False)):
            self.app.runner = None
            self.app.operation_mode.set(mode)
            with self.subTest(mode=mode), patch("bronkhorst_gui.messagebox.askyesno", return_value=True) as confirm, \
                    patch("bronkhorst_gui.ExperimentRunner") as runner:
                self.app._start_experiment()
                self.assertIs(runner.call_args.kwargs["relay_initial_on"], expected)
                if expected:
                    self.assertIn("Сначала ON", confirm.call_args.args[1])
                    self.assertIn("после t2 → OFF", confirm.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
