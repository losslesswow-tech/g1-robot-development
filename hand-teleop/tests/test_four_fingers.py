"""Combination scope and literal target preservation, using a fake driver only."""
import unittest
from test_right_trial import ROOT, FakeDriver, step


class FourFingerTests(unittest.TestCase):
    def setUp(self):
        self.plan = step.load_plan(ROOT/'single-step-right-four-fingers.json')
        self.driver = FakeDriver()
        self.driver.targets = self.plan['hold_targets'][:]
        self.driver.actual = self.plan['reference_actual'][:]
        self.bench = step.StepWorkbench(step.load_mapping(ROOT/'calibration.example.json'),
                                        self.driver, self.plan)

    def arm(self):
        session = self.bench.arm('R', 'four-fingers-offline')
        return dict(client='four-fingers-offline', lease=session['lease'], side='R',
                    seq=1, values=session['target'])

    def test_two_three_or_four_fingers_share_one_exact_command(self):
        for count in (2, 3, 4):
            with self.subTest(count=count):
                self.setUp()
                payload = self.arm()
                payload['values'][:count] = [100, 200, 300, 500][:count]
                result = self.bench.pose(payload)
                self.assertEqual(self.driver.writes, [('R', payload['values'])])
                self.assertEqual(self.driver.targets[4:], [0, 1000])
                self.assertFalse(result['reached'])  # Existing thumb offset still counts.
                self.assertEqual(result['errors'][5], 13)
                self.assertIsNone(self.bench.lease)
                with self.assertRaises(ValueError):
                    self.bench.pose(dict(payload, seq=2))
                self.assertEqual(len(self.driver.writes), 1)

    def test_thumb_changes_cannot_be_mixed_with_fingers_or_each_other(self):
        payload = self.arm()
        for edits in ({0:100,4:100}, {2:300,5:950}, {4:100,5:950}):
            values = payload['values'][:]
            for i, v in edits.items(): values[i] = v
            with self.assertRaises(ValueError):
                self.bench.pose(dict(payload, values=values))
        self.assertEqual(self.driver.writes, [])

    def test_thumb_bend_or_rotation_can_still_be_sent_individually(self):
        for index, value in ((4, 200), (5, 950)):
            self.setUp()
            payload = self.arm()
            payload['values'][index] = value
            self.bench.pose(payload)
            self.assertEqual(self.driver.writes, [('R', payload['values'])])

    def test_any_out_of_range_finger_rejects_whole_command(self):
        payload = self.arm()
        payload['values'][:4] = [100, 200, 501, 300]
        with self.assertRaises(ValueError): self.bench.pose(payload)
        self.assertEqual(self.driver.writes, [])

    def test_prepare_never_writes_and_next_prepare_keeps_command_not_feedback(self):
        payload = self.arm()
        self.assertEqual(self.driver.writes, [])
        self.assertFalse(self.bench.mapping['hands']['R']['verified'])
        payload['values'][:4] = [100, 200, 300, 400]
        self.bench.pose(payload)
        next_payload = self.arm()
        self.assertEqual(next_payload['values'], [100, 200, 300, 400, 0, 1000])


if __name__ == '__main__': unittest.main()
