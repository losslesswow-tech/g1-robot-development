"""Right-hand commissioning tests. No real hardware is used."""
import copy
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import step_server as step


class FakeDriver:
    def __init__(self):
        self.targets = [0, 0, 0, 0, 0, 1000]
        self.actual = [0, 6, 0, 1, 15, 987]
        self.writes = []
        self.error = None

    def read_targets(self, side):
        return self.targets[:]

    def read(self, side):
        return {'actual': self.actual[:], 'force_raw': [0]*6}

    def move(self, side, values):
        self.writes.append((side, values[:]))
        if self.error:
            raise self.error
        self.actual = [a+v-t for a, v, t in zip(self.actual, values, self.targets)]
        self.targets = values[:]
        return self.actual[:]


class RightTrialTests(unittest.TestCase):
    def setUp(self):
        self.plan = step.load_plan(ROOT/'single-step-right-fingers.json')
        self.mapping = step.load_mapping(ROOT/'calibration.example.json')
        self.driver = FakeDriver()
        self.bench = step.StepWorkbench(self.mapping, self.driver, self.plan)

    def arm(self):
        prepared = self.bench.arm('R', 'right-offline-test')
        return dict(client='right-offline-test', lease=prepared['lease'],
                    side='R', seq=1, values=prepared['target'])

    def test_prepare_has_no_write_or_false_calibration(self):
        payload = self.arm()
        self.assertEqual(payload['values'], [0, 0, 0, 0, 0, 1000])
        self.assertEqual(self.driver.writes, [])
        self.assertFalse(self.mapping['hands']['R']['verified'])
        self.assertFalse(self.mapping['hands']['R']['model_axes_verified'])

    def test_each_of_six_channels_can_move_alone_and_hold_exact_targets(self):
        for index in range(6):
            with self.subTest(index=index):
                self.setUp()
                payload = self.arm()
                payload['values'][index] += 50 if index < 5 else -50
                result = self.bench.pose(payload)
                self.assertEqual(self.driver.writes, [('R', payload['values'])])
                self.assertFalse(result['reached'])  # Original criterion, thumb offsets retained.
                self.assertEqual(result['errors'][4:], [15, 13])
                self.assertFalse(result['session_active'])
                with self.assertRaises(ValueError):
                    self.bench.pose(dict(payload, seq=2))

    def test_feedback_never_replaces_passive_targets_on_next_prepare(self):
        payload = self.arm()
        payload['values'][3] = 40
        self.bench.pose(payload)
        second = self.arm()
        self.assertEqual(second['values'], [0, 0, 0, 40, 0, 1000])
        second['values'][0] = 20
        self.bench.pose(second)
        self.assertEqual(self.driver.writes[-1], ('R', [20, 0, 0, 40, 0, 1000]))

    def test_no_change_multiple_channels_wrong_side_and_outside_window_rejected(self):
        payload = self.arm()
        for values, side in [([0, 0, 0, 0, 0, 1000], 'R'),
                             ([50, 50, 0, 0, 0, 1000], 'R'),
                             ([0, 0, 0, 51, 0, 1000], 'R'),
                             ([0, 0, 0, 0, 0, 949], 'R'),
                             ([50, 0, 0, 0, 0, 1000], 'L')]:
            with self.assertRaises(ValueError):
                self.bench.pose(dict(payload, values=values, side=side))
        self.assertEqual(self.driver.writes, [])

    def test_external_target_change_or_feedback_drift_blocks_write(self):
        payload = self.arm()
        payload['values'][3] = 50
        self.driver.targets[0] = 1
        with self.assertRaises(ValueError):
            self.bench.pose(payload)
        self.assertEqual(self.driver.writes, [])
        self.driver.targets[0] = 0
        self.driver.actual[4] = 23
        with self.assertRaises(ValueError):
            self.arm()

    def test_timeout_never_retries(self):
        payload = self.arm()
        payload['values'][3] = 50
        self.driver.error = subprocess.TimeoutExpired('fake', 22)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.bench.pose(payload)
        self.assertIsNone(self.bench.lease)
        self.assertEqual(len(self.driver.writes), 1)

    def test_both_hand_scopes_preserved_and_wrong_hand_payload_rejected(self):
        left = step.load_plan(ROOT/'single-step-left-index.json')
        bench = step.StepWorkbench(self.mapping, self.driver, self.plan, extra_plans=[left])
        self.assertEqual(set(bench.config()['single_step_scopes']), {'L', 'R'})
        session = bench.arm('R', 'right-offline-test')
        with self.assertRaises(ValueError):
            bench.pose(dict(client='right-offline-test', lease=session['lease'],
                            side='L', seq=1, values=[2, 4, 44, 150, 1, 978]))
        self.assertEqual(self.driver.writes, [])


if __name__ == '__main__':
    unittest.main()
