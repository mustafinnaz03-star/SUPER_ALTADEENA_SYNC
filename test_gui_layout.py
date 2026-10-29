"""Compact layout checks only: all device workers are mocked."""
import unittest

import test_manual_controls


class WorkbenchLayoutTests(unittest.TestCase):
    setUp = test_manual_controls.ManualControlsTests.setUp
    tearDown = test_manual_controls.ManualControlsTests.tearDown

    def mode(self, value):
        self.app.operation_mode.set(value)
        self.app._operation_mode_changed()
        self.app.update_idletasks()

    def test_relay_setup_is_inside_arduino_card_and_only_shown_in_valve_mode(self):
        self.assertIs(self.app.relay_setup_frame.master, self.app.arduino_frame)
        self.mode("automatic-arduino")
        self.assertEqual(self.app.relay_setup_frame.winfo_manager(), "")
        self.mode("automatic-arduino-valves")
        self.assertEqual(self.app.relay_setup_frame.winfo_manager(), "pack")
        self.app.relay_selection_var.set("2")
        self.mode("automatic-arduino")
        self.mode("automatic-arduino-valves")
        self.assertEqual(self.app._selected_relay_mask(), 2)
        self.app.service.submit.assert_not_called()

    def test_advanced_connection_fields_collapse_without_changing_values(self):
        self.mode("automatic-arduino")
        self.assertEqual(self.app.arduino_details_frame.winfo_manager(), "")
        self.app.arduino_baudrate_var.set("115200")
        self.app.arduino_timeout_var.set("240")
        self.app.arduino_details_checkbutton.invoke()
        self.assertEqual(self.app.arduino_details_frame.winfo_manager(), "pack")
        self.app.arduino_details_checkbutton.invoke()
        self.assertEqual(self.app.arduino_details_frame.winfo_manager(), "")
        self.assertEqual(self.app.arduino_timeout_var.get(), "240")
        self.assertEqual(self.app.arduino_baudrate_var.get(), "115200")

    def test_sidebar_stays_compact_when_main_window_grows(self):
        self.mode("automatic-arduino")
        self.app.notebook.select(2)
        self.app.deiconify()
        widths = []
        for size in ("980x780", "1700x900"):
            self.app.geometry(size)
            self.app.update()
            widths.append((self.app.experiment_sidebar.winfo_width(), self.app.experiment_workspace.winfo_width(),
                           self.app.winfo_width()))
        self.assertLessEqual(abs(widths[1][0] - widths[0][0]), 10)
        # Windows may clamp the requested 1700 px to the current work area.
        # The workspace should receive the ACTUAL width increase, not an
        # assumed 720 px increase that depends on monitor size/DPI settings.
        window_growth = widths[1][2] - widths[0][2]
        workspace_growth = widths[1][1] - widths[0][1]
        self.assertGreaterEqual(window_growth, 0)
        self.assertAlmostEqual(workspace_growth, window_growth, delta=25)

    def test_empty_table_hints_disappear_after_devices_and_validation(self):
        self.mode("automatic-arduino")
        self.assertEqual(self.app.live_empty_label.winfo_manager(), "place")
        self.assertEqual(self.app.schedule_empty_label.winfo_manager(), "place")
        self.assertEqual(tuple(self.app.schedule_tree.cget("displaycolumns")), ("point", "total", "dwell", "status"))
        self.app._update_devices([test_manual_controls.device()])
        self.app.plan_vars["stop"].set("50")
        plan = self.app._validate_plan()
        self.assertIsNotNone(plan)
        self.app._update_operation_mode_controls()
        self.assertEqual(self.app.live_empty_label.winfo_manager(), "")
        self.assertEqual(self.app.schedule_empty_label.winfo_manager(), "")
        self.assertIn(f"{len(plan)} точек", self.app.design_overview_var.get())
        self.app.plan_vars["step"].set("5")
        self.app._update_operation_mode_controls()
        self.assertEqual(self.app.schedule_empty_label.winfo_manager(), "place")

    def test_small_window_keeps_run_actions_in_view_and_sidebar_design_button(self):
        self.mode("automatic-arduino-valves")
        self.app.geometry("780x540")
        self.app.deiconify()
        self.app.notebook.select(2)
        self.app.update()
        right = self.app.winfo_rootx() + self.app.winfo_width()
        bottom = self.app.winfo_rooty() + self.app.winfo_height()
        for button in (self.app.start_experiment_button, self.app.stop_experiment_button):
            self.assertTrue(button.winfo_viewable())
            self.assertLessEqual(button.winfo_rootx() + button.winfo_width(), right)
            self.assertLessEqual(button.winfo_rooty() + button.winfo_height(), bottom)
        self.assertIs(self.app.open_design_button.master, self.app.design_summary_frame)


if __name__ == "__main__":
    unittest.main()
