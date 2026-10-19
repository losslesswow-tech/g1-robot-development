import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from action_flow import ActionFlow, DEFAULT_TARGET, validate_target
from console_worker import MockBridge, resources


class Clock:
    value = 0.
    def __call__(self):
        return self.value
    def advance(self, seconds=.02):
        self.value += seconds


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.events = []
        self.flow = ActionFlow(self.clock, lambda event, **data: self.events.append((event, data)))
        self.defaults, self.policy, unused = resources({}, True)
        self.bridge = MockBridge(self.defaults)

    def start(self, stop='released', target=None):
        self.flow.start(self.bridge, self.policy, self.defaults, target or DEFAULT_TARGET, stop)

    def step(self, n=1, heartbeat=True):
        for unused in range(n):
            if heartbeat:
                self.flow.heartbeat()
            self.flow.tick()
            self.clock.advance()

    def until(self, predicate, limit=1000):
        for unused in range(limit):
            if predicate():
                return
            self.step()
        self.fail('flow did not reach expected state')

    def test_single_shot_has_500_samples_and_returns_zero_without_replay(self):
        self.assertEqual(self.bridge.sent, 0)
        self.start()
        self.until(lambda: not self.flow.active)
        self.assertEqual(self.flow.policy_samples, 500)
        self.assertEqual([data['node'] for event, data in self.events if event == 'node'],
                         ['prepared', 'raised', 'pressed', 'withdrawn', 'lowered'])
        self.assertEqual(self.flow.native_finished['sent'], 863)
        self.assertEqual(self.flow.native_finished['final_weight'], 0)
        self.assertEqual(self.flow.status()['telemetry']['weight'], 0)
        count = self.bridge.sent
        self.step(20)
        self.assertEqual(self.bridge.sent, count)

    def test_each_node_holds_then_advances_in_order(self):
        self.start('prepared')
        for node, expected_steps in [('prepared', 0), ('raised', 112), ('pressed', 182), ('withdrawn', 500), ('lowered', 500)]:
            self.until(lambda: self.flow.phase == 'holding')
            self.assertEqual(self.flow.node, node)
            self.assertEqual(self.flow.policy_step, expected_steps)
            self.step(30)
            self.assertEqual(self.flow.policy_step, expected_steps)
            next_node = dict(prepared='raised', raised='pressed', pressed='withdrawn', withdrawn='lowered', lowered='released')[node]
            self.flow.command('next', next_node)
        self.assertFalse(self.flow.active)
        self.assertEqual(self.flow.native_finished['final_weight'], 0)

    def test_pause_freezes_phase_and_resume_completes_exact_policy_sample_count(self):
        self.start()
        self.until(lambda: self.flow.policy_step >= 20)
        self.flow.command('pause')
        step, action = self.flow.policy_step, list(self.flow.previous_action)
        self.step(100)
        self.assertEqual(self.flow.policy_step, step)
        self.assertEqual(self.flow.previous_action, action)
        self.flow.command('resume')
        self.until(lambda: not self.flow.active)
        self.assertEqual(self.flow.policy_samples, 500)

    def test_cannot_skip_nodes_or_return_during_weight_acquisition(self):
        self.start('prepared')
        with self.assertRaisesRegex(ValueError, 'use_release'):
            self.flow.command('return')
        self.until(lambda: self.flow.phase == 'holding')
        with self.assertRaisesRegex(ValueError, 'node_order'):
            self.flow.command('next', 'pressed')
        self.assertEqual(self.flow.node, 'prepared')

    def test_heartbeat_loss_releases_without_policy_replay(self):
        self.start('raised')
        self.until(lambda: self.flow.phase == 'holding')
        step = self.flow.policy_step
        self.step(160, heartbeat=False)
        self.assertFalse(self.flow.active)
        self.assertEqual(self.flow.error, 'page_heartbeat_timeout')
        self.assertEqual(self.flow.policy_step, step)
        self.assertEqual(self.flow.native_finished['final_weight'], 0)

    def test_node_hold_timeout_releases_despite_healthy_heartbeat(self):
        self.start('prepared')
        self.until(lambda: self.flow.phase == 'holding')
        self.step(1002)
        self.assertFalse(self.flow.active)
        self.assertEqual(self.flow.error, 'node_hold_timeout')

    def test_target_is_validated_copied_and_frozen(self):
        value = [.47, -.2, .4]
        self.start(target=value)
        value[0] = 1
        self.assertEqual(self.flow.target, [.47, -.2, .4])
        for invalid in ([], [True, -.23, .31], [.5, -.23, math.nan], [.55, -.23, .31], [.5, -.23, .51], ['.5', -.23, .31]):
            with self.assertRaises(ValueError):
                validate_target(invalid)

    def test_explicit_return_from_paused_stage_releases_at_default(self):
        self.start('raised')
        self.until(lambda: self.flow.phase == 'holding')
        self.flow.command('return')
        self.until(lambda: not self.flow.active)
        self.assertEqual(self.bridge.q[12:29], self.defaults[12:29])
        self.assertEqual(self.flow.native_finished['final_weight'], 0)


if __name__ == '__main__':
    unittest.main()
