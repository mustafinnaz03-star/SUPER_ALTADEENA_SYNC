"""Arbitrary flow order, consecutive repetitions, validation and offline GUI."""
import json
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import Mock

from bronkhorst_gui import (
    ConfigStore, ExperimentRunner, MAX_PLAN_POINTS, build_custom_plan, build_plan,
    build_single_mfc_plan, repeat_plan_points,
)
from experiment_cycle import CycleTiming, ArduinoValveController
from test_arduino_protocol import ConfirmingArduino, FakeMFCService
import test_manual_controls


class CustomPlanTests(unittest.TestCase):
    def test_custom_order_and_duplicates_are_not_sorted_or_merged(self):
        plan = build_custom_plan("100 110 110 110 120 50 0", 4, 2, single_mfc=True)
        self.assertEqual([p.total_flow for p in plan], [100, 110, 110, 110, 120, 50, 0])
        self.assertEqual([p.h2_flow for p in plan], [p.total_flow for p in plan])
        self.assertTrue(all(p.alkene_flow == 0 for p in plan))
        self.assertEqual([p.number for p in plan], list(range(1, 8)))

    def test_whitespace_semicolon_decimal_comma_dot_and_exponents(self):
        plan = build_custom_plan(" 10,5; 20.25\n 3e1\t0 ", 4, 2)
        self.assertEqual([p.total_flow for p in plan], [10.5, 20.25, 30, 0])
        for point in plan:
            self.assertAlmostEqual(point.h2_flow + point.alkene_flow, point.total_flow)
            self.assertAlmostEqual(point.h2_flow, 4 * point.alkene_flow)
            self.assertEqual(point.dwell_s, 2)

    def test_bad_custom_values_are_rejected_with_point_number(self):
        for bad in ("-1", "nan", "inf", "1e999", "hello", "10,20,30"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "Точка 2"):
                build_custom_plan("10 " + bad, 4, 1)
        for empty in ("", "   ", "; ;"):
            with self.subTest(empty=empty), self.assertRaises(ValueError):
                build_custom_plan(empty, 4, 1)

    def test_invalid_dwell_and_pair_ratio_rejected_but_single_ignores_ratio(self):
        for bad in (-1, float("nan"), float("inf")):
            with self.subTest(dwell=bad), self.assertRaises(ValueError):
                build_custom_plan("10", 4, bad)
        for bad in (0, -1, float("nan"), float("inf")):
            with self.subTest(ratio=bad), self.assertRaises(ValueError):
                build_custom_plan("10", bad, 1)
        self.assertEqual(build_custom_plan("10", float("nan"), 1, True)[0].h2_flow, 10)

    def test_each_point_repeats_consecutively_and_keeps_distinct_numbers(self):
        base = build_custom_plan("5 10 5", 4, 2)
        repeated = repeat_plan_points(base, 2)
        self.assertEqual([p.total_flow for p in repeated], [5, 5, 10, 10, 5, 5])
        self.assertEqual([p.number for p in repeated], list(range(1, 7)))
        self.assertEqual([p.number for p in base], [1, 2, 3])
        self.assertEqual(repeated[2].h2_flow, base[1].h2_flow)

    def test_one_value_can_run_thirty_complete_points(self):
        plan = repeat_plan_points(build_custom_plan("10", 4, 0, True), 30)
        mfc, board, events = FakeMFCService(), ConfirmingArduino(), queue.Queue()
        runner = ExperimentRunner(mfc, plan, "A", None, events, arduino=board,
                                  cycle=CycleTiming(0, 0, 0, 0),
                                  valves=ArduinoValveController(board, events))
        runner.run()
        triggers = [payload for name, payload, _ in board.calls if name == "trigger"]
        self.assertEqual(len(triggers), 60)
        self.assertEqual(len({(p["run_id"], p["point"]) for p in triggers}), 60)
        self.assertEqual([p["point"] for p in triggers], [i for i in range(1, 31) for _ in range(2)])
        self.assertEqual([name for name, _ in mfc.commands], ["set"] * 30 + ["zero"])
        self.assertTrue(all(payload == {"A": 10} for name, payload in mfc.commands if name == "set"))
        self.assertEqual(sum(kind == "point_completed" for kind, _ in events.queue), 30)
        self.assertFalse(runner.valves.is_switched)

    def test_invalid_repetitions_and_excessively_large_plans_are_rejected(self):
        plan = build_custom_plan("10 20", 4, 1)
        for bad in (0, -1, 1.5, True, "2", MAX_PLAN_POINTS + 1):
            with self.subTest(repeats=bad), self.assertRaises(ValueError):
                repeat_plan_points(plan, bad)
        with self.assertRaises(ValueError):
            repeat_plan_points(plan, MAX_PLAN_POINTS)
        with self.assertRaises(ValueError):
            repeat_plan_points([], 1)
        with self.assertRaises(ValueError):
            build_custom_plan("1 " * (MAX_PLAN_POINTS + 1), 4, 0)
        for factory in (lambda: build_plan(0, 1e300, 1e-300, 4, 0),
                        lambda: build_single_mfc_plan(0, MAX_PLAN_POINTS, 1, 0)):
            with self.assertRaises(ValueError):
                factory()

    def test_range_repeats_keep_existing_endpoint_rules(self):
        plan = repeat_plan_points(build_single_mfc_plan(5, 15, 5, 2), 2)
        self.assertEqual([p.total_flow for p in plan], [5, 5, 10, 10, 15, 15])
        with self.assertRaises(ValueError):
            build_single_mfc_plan(5, 16, 5, 2)


class CustomPlanDesignTests(unittest.TestCase):
    setUp = test_manual_controls.ManualControlsTests.setUp
    tearDown = test_manual_controls.ManualControlsTests.tearDown

    def custom(self, text="10 20 20 5", repeats="1"):
        self.app.plan_mode_var.set("custom")
        self.app.custom_flows_var.set(text)
        self.app.point_repeats_var.set(repeats)

    def test_source_switch_shows_relevant_fields_and_keeps_edits(self):
        self.assertEqual(self.app.plan_mode_var.get(), "range")
        self.assertEqual(self.app.point_repeats_var.get(), "1")
        self.assertEqual(self.app.custom_flows_entry.winfo_manager(), "")
        self.app.plan_vars["start"].set("7")
        self.custom()
        self.assertEqual(self.app.custom_flows_entry.winfo_manager(), "grid")
        self.assertEqual(self.app.plan_field_entries["start"].winfo_manager(), "")
        self.app.plan_mode_var.set("range")
        self.assertEqual(self.app.plan_vars["start"].get(), "7")
        self.assertEqual(self.app.custom_flows_var.get(), "10 20 20 5")
        self.assertEqual(self.app.plan_field_entries["start"].winfo_manager(), "grid")

    def test_preview_preserves_order_repetitions_and_counts_two_spectra_per_point(self):
        self.app._update_devices([test_manual_controls.device()])
        self.custom(repeats="2")
        plan = self.app._validate_plan()
        self.assertEqual([p.total_flow for p in plan], [10, 10, 20, 20, 20, 20, 5, 5])
        self.assertEqual(len(self.app.schedule_tree.get_children()), 8)
        self.assertIn("16 спектров", self.app.plan_var.get())
        self.assertIn("×2", self.app.design_summary_var.get())
        self.app.custom_flows_var.set("30")
        self.assertEqual(self.app.active_plan, [])
        self.assertFalse(self.app.schedule_tree.get_children())

    def test_custom_ignores_inactive_range_and_single_mfc_ratio(self):
        self.custom("10 20", "3")
        for var in self.app.plan_vars.values():
            if var is not self.app.plan_vars["dwell"]:
                var.set("not used")
        self.assertEqual(len(self.app._current_plan(True)), 6)
        self.app.store.save_experiment = Mock()
        self.app._save_experiment_design()
        self.app.store.save_experiment.assert_called_once()
        saved = self.app.store.save_experiment.call_args.args[0]
        self.assertEqual(saved["plan_mode"], "custom")
        self.assertEqual(saved["custom_flows"], "10 20")
        self.assertEqual(saved["point_repeats"], 3)
        self.app.plan_mode_var.set("range")
        with self.assertRaises(ValueError):
            self.app._current_plan(True)

    def test_custom_pair_splits_total_flow_by_ratio(self):
        self.app._update_devices([test_manual_controls.device(), test_manual_controls.device("MFC-B", "COM_TEST_B")])
        self.app.roles = {"MFC-A": "H2", "MFC-B": "Alkene"}
        self.custom("50 25 50")
        self.app.plan_vars["ratio"].set("4")
        plan = self.app._validate_plan()
        self.assertEqual([p.h2_flow for p in plan], [40, 20, 40])
        self.assertEqual([p.alkene_flow for p in plan], [10, 5, 10])

    def test_capacity_checks_every_point_not_just_last_for_single_and_pair(self):
        for paired, flows in ((False, "10 101 5"), (True, "10 501 5")):
            with self.subTest(paired=paired):
                devices = [test_manual_controls.device()]
                if paired:
                    devices.append(test_manual_controls.device("MFC-B", "COM_TEST_B"))
                self.app._update_devices(devices)
                self.app.roles = {"MFC-A": "H2", "MFC-B": "Alkene"}
                self.custom(flows)
                self.assertIsNone(self.app._validate_plan())
                self.assertIn("Точка 2", self.app.plan_var.get())
                self.assertIn("превышает диапазон", self.app.plan_var.get())
        self.app.service.submit.assert_not_called()

    def test_invalid_repeat_and_empty_list_block_plan_and_save(self):
        self.custom("")
        self.app.store.save_experiment = Mock()
        for text, repeats in (("", "1"), ("10", "0"), ("10", "2.5"), ("10", "nan")):
            with self.subTest(text=text, repeats=repeats):
                self.custom(text, repeats)
                with self.assertRaises(ValueError):
                    self.app._current_plan(True)
                self.app._save_experiment_design()
        self.app.store.save_experiment.assert_not_called()

    def test_running_experiment_locks_source_list_and_repetition(self):
        self.custom()
        self.app.runner = Mock(is_alive=Mock(return_value=True), valves=None)
        self.app._update_operation_mode_controls()
        for widget in (self.app.plan_mode_box, self.app.custom_flows_entry, self.app.point_repeats_entry):
            self.assertEqual(str(widget.cget("state")), "disabled")

    def test_alkene_capacity_is_checked_even_when_hydrogen_fits(self):
        self.app._update_devices([test_manual_controls.device(), test_manual_controls.device("MFC-B", "COM_TEST_B")])
        self.app.roles = {"MFC-A": "H2", "MFC-B": "Alkene"}
        self.custom("10 200 5")
        self.app.plan_vars["ratio"].set("0.25")
        self.assertIsNone(self.app._validate_plan())
        self.assertIn("Точка 2", self.app.plan_var.get())
        self.assertIn("alkene 160/100", self.app.plan_var.get())

    def test_custom_fields_fit_small_designer(self):
        self.custom()
        self.app._open_design_window()
        self.app.design_window.geometry("680x500")
        self.app.update()
        right = self.app.design_window.winfo_rootx() + self.app.design_window.winfo_width()
        for widget in (self.app.plan_mode_box, self.app.custom_flows_entry, self.app.point_repeats_entry):
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), right)

    def test_custom_settings_round_trip_without_changing_connections(self):
        self.custom("10,5 20 20 10", "30")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps({"arduino": {"port": "COM9"}, "roles": {"A": "H2"}}), encoding="utf-8")
            store = ConfigStore(path)
            self.app.store.save_experiment = store.save_experiment
            self.app._save_experiment_design()
            restored = ConfigStore(path)
            self.assertEqual(restored.experiment["plan_mode"], "custom")
            self.assertEqual(restored.experiment["custom_flows"], "10,5 20 20 10")
            self.assertEqual(restored.experiment["point_repeats"], 30)
            self.assertEqual(restored.arduino["port"], "COM9")
            self.assertEqual(restored.roles, {"A": "H2"})

    def test_plot_accepts_nonmonotonic_and_repeated_points_without_collapsing_them(self):
        self.app._update_devices([test_manual_controls.device()])
        self.custom("10 20 20 5", "2")
        self.app._validate_plan()
        self.app._open_graph_window()
        self.app.update_idletasks()
        self.assertEqual(len(self.app.flow_plot.plan), 8)
        self.assertEqual([p.total_flow for p in self.app.flow_plot.plan], [10, 10, 20, 20, 20, 20, 5, 5])
        self.app._close_graph_window()


if __name__ == "__main__":
    unittest.main()
