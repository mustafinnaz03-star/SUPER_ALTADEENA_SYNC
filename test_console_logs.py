"""Console-only filtering; no serial port or clipboard is used."""

import unittest
from unittest.mock import Mock

from bronkhorst_gui import is_console_detail
import test_manual_controls as manual_controls


class ConsoleClassificationTests(unittest.TestCase):
    def test_protocol_chatter_and_duplicate_confirmations_are_details(self):
        for message in (
            "ARDUINO: > TRIGGER run-s1 1", "ARDUINO: < TRIGGERED run-s1 1",
            "ARDUINO: < NMR_DONE run-s1 1", "ARDUINO: < VALVES_ACK token OFF 3",
            "ARDUINO: < CAPABILITY RELAY_MASK", "ARDUINO: Opening COM13 at 115200 baud.",
            "ARDUINO: Реле: OFF, выбор=3 подтверждено Arduino.",
            "ARDUINO: Точка 1: Arduino подтвердила отправку TTL на ЯМР (D8).",
            "ARDUINO: Точка 1: получен ответный TTL от ЯМР (D2). Запись спектра подтверждена.",
            "COMMAND set: {'A': 10}", "OK set", "CONNECT requested for COM9",
            "UI: connect button pressed for COM9", "COM9: identification reply — serial A",
        ):
            with self.subTest(message=message):
                self.assertTrue(is_console_detail("12:00:00  " + message))

    def test_important_progress_faults_and_unknown_messages_are_kept(self):
        for message in (
            "POINT 1: t1 = 5 s", "POINT 1: spectrum 2/2 confirmed by returning NMR TTL.",
            "RELAYS · точка 1: D4/IN1=OFF, D5/IN2=OFF",
            "RECOVERY · точка 2: Нулевая уставка принята MFC.",
            "ARDUINO: Пробую подключиться к Arduino: COM13, цикл 2, попытка 1/3.",
            "ARDUINO: Жду 2 с перед подключением к Arduino",
            "ARDUINO: < BOOT NMR_RELAYS D4_D5_OFF", "ARDUINO: < RESET_CAUSE MCUSR=0x0 UNKNOWN_BOOTLOADER",
            "ARDUINO: < FAILSAFE HOST_TIMEOUT RELAYS_OFF", "ARDUINO: < ERROR token ON BUSY",
            "UI: Arduino connected on COM13.", "UI: Arduino disconnected.",
            "UI: Не удалось подключиться", "UI: Новый важный тип сообщения",
            "COMMAND ошибка", "Ошибка эксперимента: timeout", "План завершён.",
            "ВНИМАНИЕ: t4 прервана", "Не подтверждён OFF", "Test event 1",
        ):
            with self.subTest(message=message):
                self.assertFalse(is_console_detail("12:00:00  " + message))


class ConsoleFilterTests(unittest.TestCase):
    setUp = manual_controls.ManualControlsTests.setUp
    tearDown = manual_controls.ManualControlsTests.tearDown

    def text(self):
        return self.app.connection_log.get("1.0", "end-1c")

    def append_sample(self):
        messages = ["12:00:00  POINT 1: t1 = 5 s",
                    "12:00:01  ARDUINO: > TRIGGER run-s1 1",
                    "12:00:02  POINT 1: spectrum 1/2 confirmed by returning NMR TTL.",
                    "12:00:03  ARDUINO: < ERROR token ON BUSY"]
        for message in messages:
            self.app._append_connection_log(message)
        return messages

    def test_toggle_refilters_existing_history_without_losing_full_messages(self):
        messages = self.append_sample()
        self.assertFalse(self.app.short_logs_var.get())
        self.assertEqual(self.text(), "\n".join(messages) + "\n")
        self.app.short_logs_checkbutton.invoke()
        self.assertNotIn(" > TRIGGER", self.text())
        self.assertIn("spectrum 1/2 confirmed", self.text())
        self.assertIn("ERROR token ON BUSY", self.text())
        self.assertEqual(self.app._connection_log_lines, messages)
        self.assertTrue(self.app.connection_log.tag_ranges("error"))
        self.app.short_logs_checkbutton.invoke()
        self.assertEqual(self.text(), "\n".join(messages) + "\n")

    def test_new_logs_and_recent_events_follow_the_filter(self):
        self.app.short_logs_var.set(True)
        self.append_sample()
        self.app._append_connection_log("12:00:04  OK set")
        self.app._append_connection_log("12:00:05  RECOVERY · Жду 2 с")
        self.assertNotIn("OK set", self.text())
        recent = self.app.recent_log.get("1.0", "end")
        self.assertNotIn("OK set", recent)
        self.assertIn("Жду 2 с", recent)
        self.assertIn("ERROR token", recent)

    def test_copy_uses_visible_mode_and_full_mode_restores_diagnostics(self):
        messages = self.append_sample()
        self.app.clipboard_clear = Mock()
        self.app.clipboard_append = Mock()
        self.app.short_logs_var.set(True)
        self.app._copy_console()
        copied = self.app.clipboard_append.call_args.args[0]
        self.assertNotIn(" > TRIGGER", copied)
        self.assertIn("ERROR token", copied)
        self.app.short_logs_var.set(False)
        self.app._copy_console()
        self.app.clipboard_append.assert_called_with("\n".join(messages))

    def test_clear_removes_hidden_history_too(self):
        self.append_sample()
        self.app.short_logs_var.set(True)
        self.app._clear_console()
        self.app.short_logs_var.set(False)
        self.assertEqual(self.app._connection_log_lines, [])
        self.assertEqual(self.text(), "")
        self.assertEqual(self.app.recent_log.get("1.0", "end").strip(), "")

    def test_bounded_history_removes_only_corresponding_visible_rows(self):
        self.app._connection_log_lines = ["12:00:00  OK old",
                                           "12:00:01  Important old\nsecond line"] + ["12:00:02  OK set"] * 1998
        self.app.short_logs_var.set(True)
        self.app._append_connection_log("12:00:03  OK new")
        self.assertIn("Important old", self.text())
        self.app._append_connection_log("12:00:04  Important new")
        self.assertEqual(self.text(), "12:00:04  Important new\n")
        self.assertEqual(len(self.app._connection_log_lines), 2000)
        self.app.short_logs_var.set(False)
        self.assertEqual(self.text(), "\n".join(self.app._connection_log_lines) + "\n")


if __name__ == "__main__":
    unittest.main()
