"""Authorized raw window extension; all movements here use a fake driver."""
import unittest
from test_right_trial import ROOT, FakeDriver, step


class RightRangeTests(unittest.TestCase):
    def setUp(self):
        self.plan = step.load_plan(ROOT/'single-step-right-range500.json')
        self.driver = FakeDriver()
        self.driver.targets = self.plan['hold_targets'][:]
        self.driver.actual = self.plan['reference_actual'][:]
        self.bench = step.StepWorkbench(step.load_mapping(ROOT/'calibration.example.json'),
                                         self.driver, self.plan)

    def arm(self):
        session = self.bench.arm('R', 'range500-offline')
        return dict(client='range500-offline', lease=session['lease'], side='R',
                    seq=1, values=session['target'])

    def test_each_first_five_accepts_literal500_without_hidden50_limit(self):
        for index in range(5):
            with self.subTest(index=index):
                self.setUp()
                payload = self.arm()
                payload['values'][index] = 500
                self.bench.pose(payload)
                self.assertEqual(self.driver.writes, [('R', payload['values'])])
                self.assertEqual(self.driver.targets[5], 1000)
                self.assertIsNone(self.bench.lease)

    def test_zero_endpoint_preserves_other_saved_targets(self):
        payload = self.arm()
        payload['values'][0] = 0
        self.bench.pose(payload)
        self.assertEqual(self.driver.targets, [0, 0, 0, 0, 0, 1000])

    def test_501_multiple_changes_and_rotation500_rejected(self):
        payload = self.arm()
        for changes in ({0:501}, {0:500,1:500}, {5:500}, {5:949}):
            values = payload['values'][:]
            for i, value in changes.items():
                values[i] = value
            with self.assertRaises(ValueError):
                self.bench.pose(dict(payload, values=values))
        self.assertEqual(self.driver.writes, [])

    def test_prepare_retains_current_pinky_target_without_motion(self):
        payload = self.arm()
        self.assertEqual(payload['values'], [50, 0, 0, 0, 0, 1000])
        self.assertEqual(self.driver.writes, [])
        self.assertFalse(self.bench.mapping['hands']['R']['verified'])


if __name__ == '__main__':
    unittest.main()
