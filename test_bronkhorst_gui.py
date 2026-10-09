import unittest

from bronkhorst_gui import build_plan, build_single_mfc_plan, float_to_registers, registers_to_float, registers_to_text


class FlowProgramTests(unittest.TestCase):
    def test_requested_ratio_ramp(self):
        plan = build_plan(start=10, stop=150, step=10, ratio=4, dwell_s=60)
        self.assertEqual(len(plan), 15)
        self.assertEqual(plan[0].total_flow, 10)
        self.assertEqual(plan[-1].total_flow, 150)
        self.assertAlmostEqual(plan[0].h2_flow, 8)
        self.assertAlmostEqual(plan[0].alkene_flow, 2)
        self.assertAlmostEqual(plan[-1].h2_flow / plan[-1].alkene_flow, 4)
        self.assertEqual(sum(point.dwell_s for point in plan), 900)

    def test_rejects_non_aligned_endpoint(self):
        with self.assertRaises(ValueError):
            build_plan(start=10, stop=25, step=10, ratio=4, dwell_s=60)

    def test_single_mfc_ramp_assigns_full_total_to_one_channel(self):
        plan = build_single_mfc_plan(start=5, stop=20, step=5, dwell_s=2)
        self.assertEqual([point.total_flow for point in plan], [5, 10, 15, 20])
        self.assertEqual([point.h2_flow for point in plan], [5, 10, 15, 20])
        self.assertEqual([point.alkene_flow for point in plan], [0, 0, 0, 0])

    def test_float_register_round_trip(self):
        value = 12.345
        self.assertAlmostEqual(registers_to_float(float_to_registers(value)), value, places=5)

    def test_register_text_decode(self):
        self.assertEqual(registers_to_text([0x4D31, 0x3233, 0x0000]), "M123")


if __name__ == "__main__":
    unittest.main()
