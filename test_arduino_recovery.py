"""OFF-boundary recovery tests. All serial/MFC communication is simulated."""

import queue
import threading
import time
import unittest
from unittest.mock import Mock, patch

from bronkhorst_gui import ArduinoCommand, ArduinoNMRService, ExperimentPoint, ExperimentRunner
from experiment_cycle import ArduinoValveController, CycleTiming, ArduinoRecoveryPolicy
from test_arduino_protocol import FakeMFCService
import test_manual_controls as manual_controls


ALL_OFF = {"switched": False, "relay_mask": 3}


class RecoveryArduino:
    def __init__(self):
        self.ready = True
        self.calls = []
        self.off_failures = {}
        self.checkpoint_failure = None
        self.spectrum_failure = None
        self.on_failure = None
        self.recover_failure = None
        self.on_recover = None
        self.attempts_per_recovery = 1

    def submit(self, name, payload, timeout_s):
        self.calls.append((name, payload))
        if name == "valves":
            failure = self.on_failure if payload["switched"] else self.off_failures.get(payload["point"])
            if failure:
                self.ready = False
                raise failure
            return {"switched": payload["switched"], "relay_mask": payload["relay_mask"]}
        if name == "trigger":
            if self.spectrum_failure and payload["run_id"].endswith("-s2"):
                raise self.spectrum_failure
            return {"run_id": payload["run_id"], "point": payload["point"]}
        if name == "off_checkpoint":
            if self.checkpoint_failure:
                failure, self.checkpoint_failure = self.checkpoint_failure, None
                self.ready = False
                raise failure
            return dict(ALL_OFF)
        if name == "recover_off":
            if self.recover_failure:
                raise self.recover_failure
            if self.on_recover:
                self.on_recover()
            self.ready = True
            return {**ALL_OFF, "attempts_used": self.attempts_per_recovery}
        raise AssertionError(name)


class RecoveryRunnerTests(unittest.TestCase):
    def setup_runner(self, enabled=True, count=2):
        events, mfc, board = queue.Queue(), FakeMFCService(), RecoveryArduino()
        plan = [ExperimentPoint(i, i * 5, i * 5, 0, 0) for i in range(1, count + 1)]
        runner = ExperimentRunner(mfc, plan, "A", None, events, arduino=board,
                                  cycle=CycleTiming(0, 0, 0, 0),
                                  valves=ArduinoValveController(board, events),
                                  recover_after_off=enabled)
        return runner, board, mfc, events

    def completed(self, events):
        return [value.number for kind, value in events.queue if kind == "point_completed"]

    def test_lost_off_ack_zeroes_before_reconnect_and_advances_without_duplicate_ttl(self):
        runner, board, mfc, events = self.setup_runner()
        board.off_failures[1] = ConnectionError("USB disconnected on final OFF")
        board.on_recover = lambda: self.assertEqual(mfc.commands[-1], ("zero", {"A": 5}))
        delays = runner._wait_delay = Mock()
        runner.run()
        self.assertEqual(self.completed(events), [1, 2])
        self.assertEqual([name for name, _ in mfc.commands], ["set", "zero", "set", "zero"])
        self.assertEqual(sum(name == "recover_off" for name, _ in board.calls), 1)
        self.assertEqual(sum(name == "trigger" for name, _ in board.calls), 4)
        self.assertEqual([call.args[1] for call in delays.call_args_list], ["t1", "t2", "t3", "t4"] * 2)
        self.assertFalse(runner.valves.is_switched)
        self.assertNotIn("runner_error", [kind for kind, _ in events.queue])

    def test_delayed_failure_after_off_ack_is_also_recoverable(self):
        runner, board, mfc, events = self.setup_runner()
        board.checkpoint_failure = OSError("read failed after OFF ACK")
        runner.run()
        self.assertEqual(self.completed(events), [1, 2])
        self.assertTrue(runner.recovery_used)
        self.assertEqual(mfc.commands[1][0], "zero")

    def test_last_point_recovery_finishes_without_reapplying_gas(self):
        runner, board, mfc, events = self.setup_runner(count=1)
        board.off_failures[1] = TimeoutError("OFF ACK lost")
        runner.run()
        self.assertEqual(self.completed(events), [1])
        self.assertEqual(sum(name == "set" for name, _ in mfc.commands), 1)
        self.assertIn("finished", [kind for kind, _ in events.queue])

    def test_disabled_option_preserves_stop_on_fault_and_adds_no_checkpoint(self):
        runner, board, mfc, events = self.setup_runner(enabled=False)
        board.off_failures[1] = ConnectionError("lost")
        runner.run()
        self.assertEqual(self.completed(events), [])
        self.assertFalse(any(name in ("recover_off", "off_checkpoint") for name, _ in board.calls))
        self.assertEqual(mfc.commands[-1][0], "zero")

    def test_next_cycle_gets_full_budget_after_previous_cycle_recovery(self):
        runner, board, mfc, events = self.setup_runner(count=3)
        runner.recovery_policy = ArduinoRecoveryPolicy(attempts=1)
        board.off_failures = {1: ConnectionError("lost"), 2: ConnectionError("lost again")}
        runner.run()
        self.assertEqual(self.completed(events), [1, 2, 3])
        requests = [payload for name, payload in board.calls if name == "recover_off"]
        self.assertEqual([p["max_attempts"] for p in requests], [1, 1])
        self.assertEqual([p["attempt_offset"] for p in requests], [0, 0])
        self.assertEqual([p["point"] for p in requests], [1, 2])
        self.assertEqual(sum(name == "set" for name, _ in mfc.commands), 3)
        self.assertNotIn("runner_error", [kind for kind, _ in events.queue])

    def test_no_recovery_during_spectrum_on_t2_or_t3(self):
        for stage in ("spectrum", "ON", "t2", "t3"):
            with self.subTest(stage=stage):
                runner, board, mfc, events = self.setup_runner()
                if stage == "spectrum":
                    board.spectrum_failure = ConnectionError("lost in spectrum")
                elif stage == "ON":
                    board.on_failure = ConnectionError("lost on ON")
                else:
                    def wait(_point, name, _seconds):
                        if name == stage:
                            raise ConnectionError("lost during delay")
                    runner._wait_delay = wait
                runner.run()
                self.assertEqual(self.completed(events), [])
                self.assertFalse(any(name == "recover_off" for name, _ in board.calls))
                self.assertEqual(sum(name == "set" for name, _ in mfc.commands), 1)
                self.assertEqual(mfc.commands[-1][0], "zero")

    def test_protocol_error_is_not_treated_as_reconnectable_disconnect(self):
        runner, board, _, events = self.setup_runner()
        board.off_failures[1] = RuntimeError("wrong relay ACK")
        runner.run()
        self.assertFalse(any(name == "recover_off" for name, _ in board.calls))
        self.assertEqual(self.completed(events), [])

    def test_recovery_failure_and_stop_never_advance(self):
        for failure in (TimeoutError("20 s elapsed"), InterruptedError("cancelled")):
            with self.subTest(failure=failure):
                runner, board, mfc, events = self.setup_runner()
                board.off_failures[1] = ConnectionError("lost")
                board.recover_failure = failure
                runner.run()
                self.assertEqual(self.completed(events), [])
                self.assertEqual(sum(name == "set" for name, _ in mfc.commands), 1)
                self.assertEqual(mfc.commands[-1][0], "zero")

    def test_cancel_even_after_recovery_ack_prevents_next_point(self):
        runner, board, mfc, events = self.setup_runner()
        board.off_failures[1] = ConnectionError("lost")
        board.on_recover = runner.cancel
        runner.run()
        self.assertEqual(self.completed(events), [])
        self.assertEqual(sum(name == "set" for name, _ in mfc.commands), 1)

    def test_failed_mfc_zero_prevents_reconnection(self):
        runner, board, mfc, events = self.setup_runner()
        board.off_failures[1] = ConnectionError("lost")
        original = mfc.submit
        def submit(name, payload):
            original(name, payload)
            if name == "zero":
                raise OSError("MFC disconnected")
        mfc.submit = submit
        runner.run()
        self.assertFalse(any(name == "recover_off" for name, _ in board.calls))
        self.assertEqual(self.completed(events), [])

    def test_t1_reset_restores_flow_and_restarts_full_delay_before_first_ttl(self):
        runner, board, mfc, events = self.setup_runner()
        original = runner._wait_delay
        calls = []
        def wait(point, name, seconds):
            calls.append((point.number, name, seconds))
            if len(calls) == 1:
                board.ready = False
            return original(point, name, seconds)
        runner._wait_delay = wait
        board.on_recover = lambda: self.assertFalse(any(name == "trigger" for name, _ in board.calls))
        runner.run()
        self.assertEqual(self.completed(events), [1, 2])
        self.assertEqual([name for name, _ in mfc.commands], ["set", "zero", "set", "set", "zero"])
        self.assertEqual([name for number, name, _ in calls if number == 1], ["t1", "t1", "t2", "t3", "t4"])
        self.assertEqual(sum(name == "trigger" for name, _ in board.calls), 4)
        self.assertEqual(sum(name == "recover_off" for name, _ in board.calls), 1)
        self.assertEqual(runner.recovery_attempts_used, 0)  # Point 2 has a fresh budget.

    def test_t4_reset_does_not_reenergize_or_repeat_spectra_and_marks_warning(self):
        runner, board, mfc, events = self.setup_runner()
        original = runner._wait_delay
        def wait(point, name, seconds):
            if point.number == 1 and name == "t4":
                board.ready = False
            return original(point, name, seconds)
        runner._wait_delay = wait
        runner.run()
        self.assertEqual(self.completed(events), [2])
        self.assertEqual([point.number for kind, point in events.queue if kind == "point_completed_warning"], [1])
        self.assertEqual(runner.t4_interrupted_points, {1})
        self.assertEqual(sum(name == "trigger" for name, _ in board.calls), 4)
        self.assertEqual(sum(name == "valves" and payload["switched"] for name, payload in board.calls), 2)
        self.assertEqual([name for name, _ in mfc.commands], ["set", "zero", "set", "zero"])
        self.assertTrue(any("t4 прервана" in text for kind, text in events.queue if kind == "finished"))

    def test_t1_t4_recovery_disabled_still_stops(self):
        for stage in ("t1", "t4"):
            runner, board, mfc, events = self.setup_runner(enabled=False)
            original = runner._wait_delay
            def wait(point, name, seconds):
                if name == stage:
                    board.ready = False
                return original(point, name, seconds)
            runner._wait_delay = wait
            runner.run()
            with self.subTest(stage=stage):
                self.assertFalse(any(name == "recover_off" for name, _ in board.calls))
                self.assertEqual(self.completed(events), [])
                self.assertEqual(mfc.commands[-1][0], "zero")

    def test_repeated_resets_share_cycle_budget_but_next_cycle_starts_fresh(self):
        runner, board, mfc, events = self.setup_runner(count=3)
        runner.recovery_policy = ArduinoRecoveryPolicy(3, 0.2)
        board.attempts_per_recovery = 2
        board.off_failures[1] = ConnectionError("OFF lost")
        original = runner._wait_delay
        def wait(point, name, seconds):
            if point.number == 2 and name == "t1":
                board.ready = False
                board.attempts_per_recovery = 1
            return original(point, name, seconds)
        runner._wait_delay = wait
        runner.run()
        requests = [payload for name, payload in board.calls if name == "recover_off"]
        self.assertEqual([p["max_attempts"] for p in requests], [3, 3, 2, 1])
        self.assertEqual([p["attempt_offset"] for p in requests], [0, 0, 1, 2])
        self.assertEqual([p["point"] for p in requests], [1, 2, 2, 2])
        self.assertEqual([p["retry_interval_s"] for p in requests], [0.2] * 4)
        self.assertEqual(runner.recovery_attempts_used, 3)
        self.assertEqual(sum(name == "set" for name, _ in mfc.commands), 5)
        self.assertTrue(any("Исчерпаны" in text for kind, text in events.queue if kind == "runner_error"))

    def test_cancellation_on_t1_recovery_never_restores_flow(self):
        runner, board, mfc, events = self.setup_runner()
        board.ready = False
        board.on_recover = runner.cancel
        runner.run()
        self.assertEqual(sum(name == "set" for name, _ in mfc.commands), 1)
        self.assertEqual(self.completed(events), [])


class RecoveryServiceTests(unittest.TestCase):
    def service(self):
        return ArduinoNMRService({"port": "COM17", "baudrate": 115200,
                                  "startup_timeout_s": 8, "spectrum_timeout_s": 1}, queue.Queue())

    def mark_ready(self, service):
        service.ready = service.relay_capable = service.relay_selection_capable = True

    def test_retries_original_port_and_requires_ready_then_two_all_off_acks(self):
        service = self.service()
        def connect(settings, **_kwargs):
            self.assertEqual(settings["port"], "COM17")
            self.assertEqual(cancel.wait.call_count, service._connect.call_count)
            if service._connect.call_count == 1:
                raise PermissionError("USB not ready")
            self.mark_ready(service)
        service._connect = Mock(side_effect=connect)
        service._set_valves = Mock(return_value=dict(ALL_OFF))
        service._off_checkpoint = Mock(return_value=dict(ALL_OFF))
        cancel = Mock(is_set=Mock(return_value=False))
        payload = {"cancel_event": cancel, "deadline": time.monotonic() + 20, "retry_interval_s": 2.5}
        self.assertEqual(service._recover_off(payload), {**ALL_OFF, "attempts_used": 2})
        self.assertEqual(service._connect.call_count, 2)
        self.assertIs(service._set_valves.call_args.args[0]["switched"], False)
        self.assertEqual(service._set_valves.call_args.args[0]["relay_mask"], 3)
        service._off_checkpoint.assert_called_once_with(payload)
        self.assertEqual([call.args for call in cancel.wait.call_args_list], [(2.5,), (2.5,)])

    def test_actual_recovery_handshake_never_sends_on_or_trigger(self):
        service = self.service()
        port = Mock(is_open=True)
        port.readline.side_effect = [b"BOOT NMR_RELAYS D4_D5_OFF\n",
                                    b"CAPABILITIES NMR RELAYS_D4_D5\n",
                                    b"CAPABILITY RELAY_MASK\n", b"READY\n",
                                    b"VALVES_ACK token OFF 3\n", b"VALVES_ACK token OFF 3\n"]
        with patch("bronkhorst_gui.serial.Serial", return_value=port) as open_port, \
                patch("bronkhorst_gui.uuid4", return_value=Mock(hex="token")), \
                patch("bronkhorst_gui.ARDUINO_OFF_SETTLE_S", 0):
            result = service._recover_off({"cancel_event": threading.Event(), "deadline": time.monotonic() + 20})
        self.assertEqual(result, {**ALL_OFF, "attempts_used": 1})
        self.assertEqual(open_port.call_args.kwargs["port"], "COM17")
        commands = [call.args[0] for call in port.write.call_args_list]
        self.assertEqual(sum(line == b"VALVES token OFF 3\n" for line in commands), 2)
        self.assertFalse(any(b" ON" in line or b"TRIGGER" in line for line in commands))

    def test_cancel_while_waiting_ready_closes_port(self):
        service, cancel = self.service(), threading.Event()
        port = Mock(is_open=True)
        def read():
            cancel.set()
            return b""
        port.readline.side_effect = read
        with patch("bronkhorst_gui.serial.Serial", return_value=port), self.assertRaises(InterruptedError):
            service._recover_off({"cancel_event": cancel, "deadline": time.monotonic() + 20})
        port.close.assert_called_once()
        self.assertFalse(service.ready)
        self.assertIsNone(service.port)
        port.write.assert_not_called()

    def test_expired_or_cancelled_queued_recovery_never_opens_port(self):
        for cancelled in (False, True):
            service, cancel = self.service(), threading.Event()
            if cancelled:
                cancel.set()
            service._connect = Mock()
            with self.subTest(cancelled=cancelled), self.assertRaises((TimeoutError, InterruptedError)):
                service._recover_off({"cancel_event": cancel,
                                      "deadline": time.monotonic() + (20 if cancelled else -1)})
            service._connect.assert_not_called()

    def test_retry_loop_stops_at_global_deadline_and_closes_port(self):
        service = self.service()
        service._connect = Mock(side_effect=PermissionError("port absent"))
        cancel = Mock(is_set=Mock(return_value=False))
        with patch("bronkhorst_gui.time.monotonic", side_effect=[0, 0, 0, 21]), self.assertRaises(TimeoutError):
            service._recover_off({"cancel_event": cancel, "deadline": 20})
        service._connect.assert_called_once()
        self.assertFalse(service.ready)
        self.assertIsNone(service.port)

    def test_configured_count_and_interval_and_no_wait_after_last_attempt(self):
        service = self.service()
        service._connect = Mock(side_effect=PermissionError("USB absent"))
        cancel = Mock(is_set=Mock(return_value=False))
        with self.assertRaisesRegex(TimeoutError, "выполнено 4 из 4"):
            service._recover_off({"cancel_event": cancel, "deadline": time.monotonic() + 60,
                                  "max_attempts": 4, "retry_interval_s": 0.7})
        self.assertEqual(service._connect.call_count, 4)
        self.assertEqual([call.args for call in cancel.wait.call_args_list], [(0.7,)] * 4)
        logs = [text for kind, text in service.events.queue if kind == "arduino_log"]
        self.assertEqual(sum("Пробую подключиться к Arduino" in text for text in logs), 4)

    def test_stop_during_initial_pause_prevents_first_connection(self):
        service, cancel = self.service(), threading.Event()
        service._connect = Mock(side_effect=PermissionError("USB absent"))
        with patch.object(cancel, "wait", side_effect=lambda _seconds: cancel.set()), self.assertRaises(InterruptedError):
            service._recover_off({"cancel_event": cancel, "deadline": time.monotonic() + 60,
                                  "max_attempts": 4, "retry_interval_s": 0.1})
        service._connect.assert_not_called()

    def test_stop_between_attempts_prevents_next_connection(self):
        service, cancel = self.service(), threading.Event()
        service._connect = Mock(side_effect=PermissionError("USB absent"))
        waits = []
        def wait(seconds):
            waits.append(seconds)
            if len(waits) == 2:
                cancel.set()
        with patch.object(cancel, "wait", side_effect=wait), self.assertRaises(InterruptedError):
            service._recover_off({"cancel_event": cancel, "deadline": time.monotonic() + 60,
                                  "max_attempts": 3, "retry_interval_s": 0.5})
        service._connect.assert_called_once()
        self.assertEqual(waits, [0.5, 0.5])

    def test_first_connection_really_waits_for_configured_cooldown(self):
        service = self.service()
        started = time.monotonic()
        def connect(*_args, **_kwargs):
            self.assertGreaterEqual(time.monotonic() - started, 0.09)
            self.mark_ready(service)
        service._connect = Mock(side_effect=connect)
        service._set_valves = Mock(return_value=dict(ALL_OFF))
        service._off_checkpoint = Mock(return_value=dict(ALL_OFF))
        service._recover_off({"cancel_event": threading.Event(), "deadline": started + 3,
                              "max_attempts": 1, "retry_interval_s": 0.1})
        service._connect.assert_called_once()

    def test_missing_capabilities_or_wrong_ack_does_not_retry(self):
        for wrong_ack in (False, True):
            service = self.service()
            def connect(*_args, **_kwargs):
                service.ready = True
                service.relay_capable = service.relay_selection_capable = wrong_ack
            service._connect = Mock(side_effect=connect)
            service._set_valves = Mock(side_effect=RuntimeError("wrong ACK"))
            with self.subTest(wrong_ack=wrong_ack), self.assertRaises(RuntimeError):
                service._recover_off({"cancel_event": threading.Event(), "deadline": time.monotonic() + 20})
            service._connect.assert_called_once()
            self.assertFalse(service.ready)

    def test_settle_window_catches_board_reset_before_accepting_checkpoint(self):
        service = self.service()
        self.mark_ready(service)
        service._keepalive = Mock()
        service._read_line = Mock(return_value="BOOT NMR_RELAYS D4_D5_OFF")
        service._set_valves = Mock()
        with self.assertRaises(ConnectionError):
            service._off_checkpoint({"cancel_event": threading.Event()})
        service._set_valves.assert_not_called()

    def test_checkpoint_requires_fresh_off_ack_after_settle(self):
        service = self.service()
        self.mark_ready(service)
        service._keepalive = Mock()
        service._read_line = Mock(return_value=None)
        service._set_valves = Mock(return_value=dict(ALL_OFF))
        ticks = iter(i * 0.1 for i in range(100))
        with patch("bronkhorst_gui.time.monotonic", side_effect=lambda: next(ticks)):
            self.assertEqual(service._off_checkpoint({}), ALL_OFF)
        self.assertGreater(service._read_line.call_count, 0)
        self.assertEqual(service._set_valves.call_args.args[0]["relay_mask"], 3)

    def test_shutdown_still_allows_off_without_recovery_cancel_event(self):
        service = self.service()
        self.mark_ready(service)
        service.stopping.set()
        service._write_line = Mock()
        service._keepalive = Mock()
        service._read_line = Mock(return_value="VALVES_ACK token OFF 3")
        with patch("bronkhorst_gui.uuid4", return_value=Mock(hex="token")):
            self.assertEqual(service._set_valves(dict(ALL_OFF)), ALL_OFF)

    def test_recovery_error_invalidates_ready_and_logs_details(self):
        service = self.service()
        self.mark_ready(service)
        service._recover_off = Mock(side_effect=TimeoutError("recovery expired"))
        service._write_line = Mock()
        command = ArduinoCommand("recover_off", {})
        service._handle(command)
        self.assertIsInstance(command.error, TimeoutError)
        self.assertFalse(service.ready)
        self.assertTrue(any("recovery expired" in text for kind, text in service.events.queue if kind == "arduino_log"))


class RecoveryDesignTests(unittest.TestCase):
    setUp = manual_controls.ManualControlsTests.setUp
    tearDown = manual_controls.ManualControlsTests.tearDown

    def test_option_defaults_off_is_locked_outside_valve_mode_and_saves(self):
        self.assertFalse(self.app.recover_after_off_var.get())
        self.assertEqual(str(self.app.recovery_checkbutton.cget("state")), "disabled")
        self.app.operation_mode.set("automatic-arduino-valves")
        self.app._operation_mode_changed()
        self.assertEqual(str(self.app.recovery_checkbutton.cget("state")), "normal")
        self.app.recover_after_off_var.set(True)
        self.app.recovery_attempts_var.set("7")
        self.app.recovery_interval_var.set("0,5")
        self.app.store.save_experiment = Mock()
        self.app._save_experiment_design()
        self.assertIs(self.app.store.save_experiment.call_args.args[0]["recover_after_off"], True)
        self.assertEqual(self.app.store.save_experiment.call_args.args[0]["recovery_attempts"], 7)
        self.assertEqual(self.app.store.save_experiment.call_args.args[0]["recovery_interval_s"], 0.5)
        self.assertIn("Восстановление Arduino", self.app.design_summary_var.get())
        self.app.runner = Mock(is_alive=Mock(return_value=True), valves=Mock())
        self.app._update_operation_mode_controls()
        self.assertEqual(str(self.app.recovery_checkbutton.cget("state")), "disabled")
        self.assertTrue(all(str(entry.cget("state")) == "disabled" for entry in self.app.recovery_setting_entries))

    def test_recovery_status_updates_console_and_does_not_complete_point(self):
        self.app.events.put(("recovery_status", (1, "Восстановление Arduino…")))
        self.app._consume_events()
        self.assertIn("Восстановление Arduino", self.app.progress_var.get())
        self.assertTrue(any("RECOVERY" in line for line in self.app._connection_log_lines))

    def test_start_passes_option_only_to_valve_mode(self):
        self.app._update_devices([manual_controls.device()])
        self.app.control_enabled.set(True)
        self.app.recover_after_off_var.set(True)
        self.app.arduino.ready = self.app.arduino.relay_selection_capable = True
        self.app.arduino.settings.update({"port": "COM17", "baudrate": 115200})
        self.app._arduino_settings_from_ui = Mock(return_value={
            "port": "COM17", "baudrate": 115200, "spectrum_timeout_s": 180})
        self.app._validate_plan = Mock(return_value=[ExperimentPoint(1, 5, 5, 0, 0)])
        self.app._experiment_devices = Mock(return_value=(manual_controls.device(), None))
        for mode, expected in (("automatic-arduino-valves", True), ("automatic-arduino", False)):
            self.app.runner = None
            self.app.operation_mode.set(mode)
            with self.subTest(mode=mode), patch("bronkhorst_gui.messagebox.askyesno", return_value=True), \
                    patch("bronkhorst_gui.ExperimentRunner") as runner:
                self.app._start_experiment()
                self.assertIs(runner.call_args.kwargs["recover_after_off"], expected)
                self.assertEqual(runner.call_args.kwargs["recovery_policy"], ArduinoRecoveryPolicy() if expected else None)

    def test_interrupted_t4_is_visible_in_table_and_progress_count(self):
        self.app._update_devices([manual_controls.device()])
        self.app.operation_mode.set("automatic-arduino-valves")
        self.app.plan_vars["start"].set("5")
        self.app.plan_vars["stop"].set("5")
        point = self.app._validate_plan()[0]
        self.app.events.put(("point_completed_warning", point))
        self.app._consume_events()
        self.assertEqual(self.app.schedule_tree.set("point-1", "status"), "Завершена, t4 прервана")
        self.assertIn("t4 прервана: 1", self.app.completed_points_var.get())


class RecoveryPolicyTests(unittest.TestCase):
    def test_invalid_attempt_count_or_interval_rejected(self):
        for count in (0, -1, 101, True, 2.5):
            with self.subTest(count=count), self.assertRaises(ValueError):
                ArduinoRecoveryPolicy(count, 1)
        for interval in (0, -1, 61, float("nan"), float("inf")):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                ArduinoRecoveryPolicy(3, interval)


class SimulatedPort:
    """Thread-safe serial stand-in that can lose USB during the final OFF."""
    def __init__(self, fail_off=False):
        self.is_open, self.fail_off, self.broken, self.was_on = True, fail_off, False, False
        self.received = queue.Queue()
        self.writes = []
        for line in ("BOOT NMR_RELAYS D4_D5_OFF", "CAPABILITIES NMR RELAYS_D4_D5",
                     "CAPABILITY RELAY_MASK", "READY"):
            self.received.put((line + "\n").encode())

    def readline(self):
        if self.broken or not self.is_open:
            raise PermissionError("GetOverlappedResult failed: simulated USB loss")
        try:
            return self.received.get(timeout=0.01)
        except queue.Empty:
            return b""

    def write(self, raw):
        if self.broken or not self.is_open:
            raise PermissionError("simulated disconnected USB")
        line = raw.decode().strip()
        self.writes.append(line)
        fields = line.split()
        if fields[0] == "VALVES":
            if fields[2] == "ON":
                self.was_on = True
            elif self.fail_off and self.was_on:
                self.fail_off, self.broken = False, True
                return len(raw)
            self.received.put(("VALVES_ACK " + " ".join(fields[1:]) + "\n").encode())
        elif fields[0] == "TRIGGER":
            for reply in ("TRIGGERED", "NMR_DONE"):
                self.received.put((reply + " " + " ".join(fields[1:]) + "\n").encode())
        return len(raw)

    def flush(self):
        pass

    def close(self):
        self.is_open = False


class RecoveryWorkerIntegrationTests(unittest.TestCase):
    def test_real_worker_recovers_resets_during_t1_and_t4(self):
        for reset_stage in ("t1", "t4"):
            with self.subTest(stage=reset_stage):
                events, mfc = queue.Queue(), FakeMFCService()
                board = ArduinoNMRService({"port": "COM17", "startup_timeout_s": 1,
                                           "spectrum_timeout_s": 1}, events)
                first_port, restored_port = SimulatedPort(), SimulatedPort()
                reset_sent = False
                original_submit, original_put = mfc.submit, events.put
                def reset():
                    nonlocal reset_sent
                    if not reset_sent:
                        reset_sent = True
                        first_port.broken = True
                def submit(name, payload):
                    original_submit(name, payload)
                    if reset_stage == "t1" and name == "set":
                        reset()
                def put(item):
                    original_put(item)
                    if reset_stage == "t4" and item[0] == "spectrum_confirmed" and item[1][1] == 2:
                        reset()
                mfc.submit, events.put = submit, put
                runner = ExperimentRunner(mfc, [ExperimentPoint(1, 5, 5, 0, 0)], "A", None,
                                          events, arduino=board, cycle=CycleTiming(0.15, 0, 0, 0.15),
                                          valves=ArduinoValveController(board, events),
                                          recover_after_off=True, recovery_policy=ArduinoRecoveryPolicy(3, 0.1))
                with patch("bronkhorst_gui.serial.Serial", side_effect=[first_port, restored_port]), \
                        patch("bronkhorst_gui.ARDUINO_OFF_SETTLE_S", 0.01):
                    try:
                        board.start()
                        board.submit("connect", {"port": "COM17"})
                        runner.start()
                        runner.join(timeout=4)
                        self.assertFalse(runner.is_alive())
                        self.assertNotIn("runner_error", [kind for kind, _ in events.queue])
                        self.assertEqual(runner.recovery_attempts_used, 1)
                        self.assertEqual(sum(line.startswith("TRIGGER ") for port in (first_port, restored_port) for line in port.writes), 2)
                        self.assertEqual(runner.t4_interrupted_points, {1} if reset_stage == "t4" else set())
                        self.assertEqual(sum(name == "set" for name, _ in mfc.commands), 2 if reset_stage == "t1" else 1)
                    finally:
                        runner.cancel()
                        board.shutdown()
                        if runner.ident is not None:
                            runner.join(timeout=2)
                        board.join(timeout=2)

    def test_real_worker_and_runner_recover_between_two_points_without_hardware(self):
        events, mfc = queue.Queue(), FakeMFCService()
        board = ArduinoNMRService({"port": "COM17", "startup_timeout_s": 1,
                                   "spectrum_timeout_s": 1}, events)
        first_port, restored_port = SimulatedPort(fail_off=True), SimulatedPort()
        plan = [ExperimentPoint(1, 5, 5, 0, 0), ExperimentPoint(2, 10, 10, 0, 0)]
        runner = ExperimentRunner(mfc, plan, "A", None, events, arduino=board,
                                  cycle=CycleTiming(0, 0, 0, 0),
                                  valves=ArduinoValveController(board, events), recover_after_off=True)
        with patch("bronkhorst_gui.serial.Serial", side_effect=[first_port, restored_port]) as open_port, \
                patch("bronkhorst_gui.ARDUINO_OFF_SETTLE_S", 0.02):
            try:
                board.start()
                board.submit("connect", {"port": "COM17"})
                runner.start()
                runner.join(timeout=4)
                self.assertFalse(runner.is_alive())
                self.assertEqual([value.number for kind, value in events.queue if kind == "point_completed"], [1, 2])
                self.assertNotIn("runner_error", [kind for kind, _ in events.queue])
                self.assertEqual([name for name, _ in mfc.commands], ["set", "zero", "set", "zero"])
                self.assertEqual(open_port.call_count, 2)
                self.assertTrue(all(call.kwargs["port"] == "COM17" for call in open_port.call_args_list))
                self.assertEqual(sum(line.startswith("TRIGGER ") for port in (first_port, restored_port) for line in port.writes), 4)
                restored_commands = [line.split() for line in restored_port.writes if line.startswith("VALVES ") or line.startswith("TRIGGER ")]
                self.assertEqual([fields[2:] for fields in restored_commands[:2]], [["OFF", "3"], ["OFF", "3"]])
            finally:
                runner.cancel()
                board.shutdown()
                if runner.ident is not None:
                    runner.join(timeout=2)
                board.join(timeout=2)
        self.assertFalse(board.is_alive())


if __name__ == "__main__":
    unittest.main()
