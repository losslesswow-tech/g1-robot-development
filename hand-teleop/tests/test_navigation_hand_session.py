"""Offline integration of navigation jobs and the exclusive hand session."""
import unittest
from unittest.mock import patch

from test_right_trial import ROOT, step
from test_all_channels import BothFakeDriver
from task_action import TaskHand


class Motors(BothFakeDriver):
    fail_index = False
    drift_passive = False
    overwrite_target = False

    def move(self, side, values):
        self.writes.append((side, values[:]))
        self.targets[side] = values[:]
        if not self.fail_index:
            self.actual[side][3] = min(1000, values[3]+1)
        if self.drift_passive:
            self.actual[side][0] += 20
        if self.overwrite_target:
            self.targets[side][0] += 10
        return self.actual[side][:]


class NavigationHandSessionTests(unittest.TestCase):
    def setUp(self):
        plans = [step.load_plan(ROOT/name) for name in
                 ('manual-range1000-right.json','manual-range1000-left.json')]
        self.driver = Motors(plans)
        self.driver.actual['R'][3] = 991
        self.bench = step.StepWorkbench(step.load_mapping(ROOT/'calibration.example.json'),
            self.driver, plans[0], extra_plans=plans[1:])
        self.hand = TaskHand(self.transport)
        self.hand.start()

    def transport(self, path, data):
        routes = {'/api/task/arm':lambda:self.bench.task_arm(data['action'],data['client']),
                  '/api/task/pose':lambda:self.bench.task_pose(data),
                  '/api/heartbeat':lambda:self.bench.heartbeat(data),
                  '/api/disarm':lambda:self.bench.disarm(data)}
        return routes[path]()

    def run_action(self, action='right_index_retract', index=0):
        target = {'handAction':action}
        self.hand.step(0,index,target)
        self.hand.jobs[(0,index)]['thread'].join(3)
        return self.hand.step(0,index,target)

    def prepare(self, action='right_index_retract'):
        data = self.bench.task_arm(action,'offline-nav-session')
        values = data['target'][:]
        values[3] = self.bench.TASK_ACTIONS[action]
        return dict(client='offline-nav-session',lease=data['lease'],side='R',seq=1,values=values)

    def test_target_zero_actual991_sends_one_literal_close_and_keeps_other_hand(self):
        left = self.driver.targets['L'][:]
        result = self.run_action()
        self.assertEqual(result['state'],'done')
        self.assertEqual(self.driver.writes,[('R',[0,0,0,0,0,1000])])
        self.assertEqual(self.driver.targets['L'],left)
        self.assertFalse(result['result']['repository_reached'])
        self.assertTrue(result['result']['reached'])
        self.assertFalse(self.bench.status('R')['armed'])

    def test_extend_then_retract_each_send_once_with_fresh_snapshot(self):
        self.assertEqual(self.run_action('right_index_extend')['state'],'done')
        self.assertEqual(self.run_action(index=1)['state'],'done')
        self.assertEqual([v[3] for _,v in self.driver.writes],[1000,0])

    def test_already_closed_and_static_thumb_offset_does_not_write(self):
        self.driver.actual['R'][3] = 1
        result = self.run_action()
        self.assertEqual(result['state'],'done')
        self.assertFalse(result['result']['motion_sent'])
        self.assertEqual(self.driver.writes,[])

    def test_changed_device_target_after_prepare_rejects_without_write(self):
        data = self.prepare()
        self.driver.targets['R'][3] = 100
        with self.assertRaisesRegex(ValueError,'准备后发生变化'):self.bench.task_pose(data)
        self.assertEqual(self.driver.writes,[])

    def test_changed_actual_after_prepare_rejects_without_write(self):
        data = self.prepare()
        self.driver.actual['R'][1] += 8
        with self.assertRaisesRegex(ValueError,'准备后发生变化'):self.bench.task_pose(data)
        self.assertEqual(self.driver.writes,[])

    def test_extend_after_retract_accepts_stable_thumb26_and_keeps_its_command_zero(self):
        self.driver.actual['R'][3] = 1
        self.driver.actual['R'][4] = 26
        result = self.run_action('right_index_extend')
        self.assertEqual(result['state'],'done')
        self.assertEqual(self.driver.writes,[('R',[0,0,0,1000,0,1000])])
        self.assertEqual(result['result']['device_target'],[0,0,0,1000,0,1000])

    def test_moving_during_prepare_rejects(self):
        read = self.driver.read
        count = [0]
        def moving(side):
            count[0] += 1
            if count[0] == 2:self.driver.actual[side][3] -= 20
            return read(side)
        with patch.object(self.driver,'read',side_effect=moving):
            with self.assertRaisesRegex(ValueError,'仍在变化'):self.prepare()
        self.assertEqual(self.driver.writes,[])

    def test_tampered_other_finger_or_wrong_index_endpoint_rejected(self):
        data = self.prepare()
        for index in (0,3,5):
            values = data['values'][:]
            values[index] = 500 if index != 5 else 950
            with self.assertRaisesRegex(ValueError,'仅能改变'):self.bench.task_pose(dict(data,values=values))
        self.assertEqual(self.driver.writes,[])

    def test_session_cannot_replay_or_cross_into_manual_endpoint(self):
        data = self.prepare()
        with self.assertRaisesRegex(ValueError,'会话类型'):self.bench.pose(data)
        self.bench.task_pose(data)
        with self.assertRaises(ValueError):self.bench.task_pose(data)
        self.assertEqual(len(self.driver.writes),1)

    def test_manual_reference_guard_is_unchanged(self):
        with self.assertRaisesRegex(ValueError,'反馈偏离'):self.bench.arm('R','offline-manual-session')
        self.assertEqual(self.driver.writes,[])

    def test_index_not_reached_fails_without_timer_retry(self):
        self.driver.fail_index = True
        self.assertEqual(self.run_action()['state'],'failed')
        for _ in range(20):self.hand.step(0,0,{'handAction':'right_index_retract'})
        self.assertEqual(len(self.driver.writes),1)

    def test_passive_feedback_drift_is_reported_while_command_targets_are_held(self):
        self.driver.drift_passive = True
        result = self.run_action()
        self.assertEqual(result['state'],'done')
        self.assertEqual(result['result']['passive_drift'],[20,0,0,0,0])
        self.assertTrue(result['result']['targets_unchanged'])
        self.assertEqual(len(self.driver.writes),1)

    def test_external_target_change_during_motion_fails_without_retry(self):
        self.driver.overwrite_target = True
        result = self.run_action()
        self.assertEqual(result['state'],'failed')
        self.assertFalse(result['result']['targets_unchanged'])
        self.assertEqual(len(self.driver.writes),1)

    def test_unknown_actions_have_no_lease_or_write(self):
        for action in ('left_index_retract','close_all',None):
            with self.assertRaises(ValueError):self.bench.task_arm(action,'offline-nav-session')
        self.assertIsNone(self.bench.lease)
        self.assertEqual(self.driver.writes,[])
