"""Offline scope/target tests; every driver in this file is fake."""
import copy
from pathlib import Path
import subprocess
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import step_server as step


class FakeDriver:
    def __init__(self):
        self.actual = [0, 4, 43, 200, 1, 963]
        self.targets = [2, 4, 44, 200, 1, 978]
        self.writes = []
        self.error = None

    def read(self, side):
        return {'actual': self.actual[:], 'force_raw': [0]*6}

    def read_targets(self, side):
        return self.targets[:]

    def move(self, side, values):
        self.writes.append((side, values[:]))
        if self.error:
            raise self.error
        self.targets = values[:]
        self.actual[3] = values[3]
        return self.actual[:]


class SingleStepTests(unittest.TestCase):
    def setUp(self):
        self.mapping = step.load_mapping(ROOT/'calibration.example.json')
        self.plan = step.load_plan(ROOT/'single-step-left-index.json')
        self.original = FakeDriver()
        self.now = 100
        self.bench = step.StepWorkbench(self.mapping, self.original, self.plan,
                                        clock=lambda:self.now)

    def arm(self):
        session = self.bench.arm('L', 'offline-test-client')
        return {'client': 'offline-test-client', 'lease': session['lease'],
                'side': 'L', 'seq': 1, 'values': [2, 4, 44, 150, 1, 978]}

    def test_raw_trial_does_not_fabricate_angle_calibration(self):
        self.assertEqual(self.bench.config()['mode'], 'single-step')
        self.assertFalse(self.mapping['hands']['L']['verified'])
        self.assertFalse(self.mapping['hands']['L']['model_axes_verified'])

    def test_prepare_reads_commanded_values_and_never_writes(self):
        session = self.bench.arm('L', 'offline-test-client')
        self.assertEqual(session['target'][5], 978)
        self.assertEqual(session['actual'][5], 963)
        self.assertEqual(self.original.writes, [])

    def test_right_hand_and_other_channels_are_rejected(self):
        with self.assertRaises(ValueError):
            self.bench.arm('R', 'offline-test-client')
        payload = self.arm()
        for index in (0, 1, 2, 4, 5):
            values = payload['values'][:]
            values[index] += 1
            with self.assertRaises(ValueError):
                self.bench.pose(dict(payload, values=values))
        self.assertEqual(self.original.writes, [])

    def test_out_of_range_is_rejected_without_clipping(self):
        payload = self.arm()
        for value in (81, 201):
            values = payload['values'][:]
            values[3] = value
            with self.assertRaises(ValueError):
                self.bench.pose(dict(payload, values=values))
        self.assertEqual(self.original.writes, [])

    def test_one_exact_command_keeps_device_target_not_feedback(self):
        payload = self.arm()
        result = self.bench.pose(payload)
        self.assertEqual(self.original.writes, [('L', [2, 4, 44, 150, 1, 978])])
        self.assertFalse(result['reached'])
        self.assertEqual(result['errors'][5], 15)
        self.assertFalse(result['session_active'])
        self.assertIsNone(self.bench.lease)
        with self.assertRaises(ValueError):
            self.bench.pose(dict(payload, seq=2))
        self.assertEqual(len(self.original.writes), 1)

    def test_new_preparation_still_holds_978_after_feedback_963(self):
        payload = self.arm()
        self.bench.pose(payload)
        session = self.bench.arm('L', 'offline-test-client')
        self.assertEqual(session['target'], [2, 4, 44, 150, 1, 978])
        self.assertEqual(session['actual'][5], 963)

    def test_changed_external_target_rejected_before_motion(self):
        payload = self.arm()
        self.original.targets[5] = 963
        with self.assertRaises(ValueError):
            self.bench.pose(payload)
        self.assertEqual(self.original.writes, [])
        self.assertIsNone(self.bench.lease)

    def test_passive_drift_rejected_before_prepare_or_motion(self):
        self.original.actual[5] = 955
        with self.assertRaises(ValueError):
            self.arm()
        self.original.actual[5] = 963
        payload = self.arm()
        self.original.actual[0] = 8
        with self.assertRaises(ValueError):
            self.bench.pose(payload)
        self.assertEqual(self.original.writes, [])

    def test_timeout_is_not_retried_and_consumes_session(self):
        payload = self.arm()
        self.original.error = subprocess.TimeoutExpired('fake', 22)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.bench.pose(payload)
        self.assertIsNone(self.bench.lease)
        self.assertEqual(len(self.original.writes), 1)

    def test_expired_and_wrong_owner_cannot_send(self):
        payload = self.arm()
        with self.assertRaises(ValueError):
            self.bench.pose(dict(payload, client='other-client'))
        self.now += 7
        with self.assertRaises(ValueError):
            self.bench.pose(payload)
        self.assertEqual(self.original.writes, [])

    def test_even_full_arrival_still_closes_one_shot_session(self):
        payload = self.arm()
        self.original.move = lambda side, values: values[:]
        result = self.bench.pose(payload)
        self.assertTrue(result['reached'])
        self.assertFalse(result['session_active'])
        self.assertIsNone(self.bench.lease)

    def test_concurrent_second_submission_cannot_queue(self):
        payload = self.arm()
        started, finish = threading.Event(), threading.Event()
        original_move = self.original.move
        def delayed(side, values):
            started.set()
            finish.wait(2)
            return original_move(side, values)
        self.original.move = delayed
        thread = threading.Thread(target=lambda:self.bench.pose(payload))
        thread.start()
        self.assertTrue(started.wait(1))
        with self.assertRaises(ValueError):
            self.bench.pose(dict(payload, seq=2))
        finish.set()
        thread.join(2)
        self.assertEqual(len(self.original.writes), 1)


if __name__ == '__main__':
    unittest.main()
