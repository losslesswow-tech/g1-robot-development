import unittest
import threading
import io
import urllib.error
from unittest.mock import patch
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from task_action import TaskHand, TARGET, validate_hand_steps
from task_action import post
from navigation_patch import preserve_task_result


class FakeAPI:
    def __init__(self):
        self.calls=[];self.targets=[7,9,11,200,15,1000];self.actual=self.targets[:]
        self.reached=True;self.error=None
    def __call__(self,path,data):
        self.calls.append((path,data.copy()))
        if path=='/api/task/arm':return {'lease':'test-lease','target':self.targets[:],'actual':self.actual[:]}
        if path=='/api/task/pose':
            if self.error:raise self.error
            return {'target':data['values'],'actual':data['values'],'reached':self.reached,'errors':[0,0,0,0,0,13] if not self.reached else [0]*6}
        return {'ok':True}


class TaskActionTests(unittest.TestCase):
    def test_service_guard_error_is_preserved_without_retry(self):
        message='反馈偏离本次参考姿态，未发送动作；请核查实物'
        import json
        error=urllib.error.HTTPError('http://test/api/arm',400,'Bad Request',{},
            io.BytesIO(json.dumps({'error':message}).encode()))
        with patch('task_action.Path.read_text',side_effect=[
                '{"management_host":"192.0.2.1","control_port":8769}', 'test-private-token']), \
                patch('task_action.urllib.request.urlopen',side_effect=error) as request:
            with self.assertRaisesRegex(RuntimeError,message):post('/api/task/arm',{})
            self.assertEqual(request.call_count,1)
    def test_idle_tick_keeps_failure_visible(self):
        source='''def tick(self):
        if not self.task_buf:
            self.task_state_str = "无任务"
            return
'''
        scope={};exec(preserve_task_result(source),scope)
        self.task_buf=[];self.task_state_str='收食指失败：反馈偏离'
        scope['tick'](self)
        self.assertEqual(self.task_state_str,'收食指失败：反馈偏离')
    def setUp(self):
        self.api=FakeAPI();self.hand=TaskHand(self.api);self.hand.start()
    def run_step(self):
        self.hand.step(0,0,TARGET)
        self.hand.jobs[(0,0)]['thread'].join(3)
        return self.hand.step(0,0,TARGET)
    def test_one_pose_uses_current_targets_and_only_changes_right_index(self):
        self.assertEqual(self.run_step()['state'],'done')
        poses=[v for p,v in self.api.calls if p=='/api/task/pose']
        self.assertEqual(len(poses),1)
        self.assertEqual(poses[0]['values'],[7,9,11,1000,15,1000])
        self.assertEqual(poses[0]['side'],'R')
        for _ in range(10):self.hand.step(0,0,TARGET)
        self.assertEqual(len([p for p,_ in self.api.calls if p=='/api/task/pose']),1)
    def test_unreached_task_fails_without_retry(self):
        self.api.reached=False
        self.assertEqual(self.run_step()['state'],'failed')
        self.assertEqual(len([p for p,_ in self.api.calls if p=='/api/task/pose']),1)
    def test_timeout_has_uncertain_outcome_and_no_retry(self):
        self.api.error=TimeoutError('timeout')
        self.assertEqual(self.run_step()['state'],'failed')
        self.assertEqual(len([p for p,_ in self.api.calls if p=='/api/task/pose']),1)
    def test_already_reached_has_no_write(self):
        self.api.targets[3]=1000;self.api.actual=self.api.targets[:]
        self.assertEqual(self.run_step()['state'],'done')
        self.assertNotIn('/api/task/pose',[p for p,_ in self.api.calls])
    def test_cancel_before_dispatch_never_arms(self):
        self.hand.cancel();self.hand.step(0,0,TARGET)
        self.assertEqual(self.api.calls,[])
    def test_pause_during_prepare_cancels_without_pose_and_never_replays(self):
        original=self.api
        def transport(path,data):
            result=original(path,data)
            if path=='/api/task/arm':self.hand.pause()
            return result
        self.hand.transport=transport
        self.hand.step(0,0,TARGET);self.hand.jobs[(0,0)]['thread'].join(3)
        self.hand.resume()
        self.assertEqual(self.hand.step(0,0,TARGET)['state'],'failed')
        self.assertNotIn('/api/task/pose',[p for p,_ in self.api.calls])
    def test_validation_rejects_other_hand_target_or_error_recovery(self):
        for data in ({'subTasks':[{'action':'U','target':{'handAction':'left_index_extend'}}]},
                     {'defaults':{'errorSteps':[{'action':'U','target':TARGET}]}}):
            with self.assertRaises(ValueError):validate_hand_steps(data)
        validate_hand_steps({'subTasks':[{'action':'U','target':TARGET}]})
    def test_constructing_and_starting_are_offline(self):
        self.assertEqual(self.api.calls,[])
    def test_retract1000_to0_preserves_other_five_targets_and_sends_once(self):
        self.api.targets[3]=1000;self.api.actual=self.api.targets[:]
        target={'handAction':'right_index_retract'}
        self.hand.step(0,0,target);self.hand.jobs[(0,0)]['thread'].join(3)
        self.assertEqual(self.hand.step(0,0,target)['state'],'done')
        poses=[v for p,v in self.api.calls if p=='/api/task/pose']
        self.assertEqual(len(poses),1)
        self.assertEqual(poses[0]['values'],[7,9,11,0,15,1000])
        self.assertEqual(poses[0]['side'],'R')
    def test_retract_already0_never_writes(self):
        self.api.targets[3]=0;self.api.actual=self.api.targets[:]
        target={'handAction':'right_index_retract'}
        self.hand.step(0,0,target);self.hand.jobs[(0,0)]['thread'].join(3)
        self.assertEqual(self.hand.step(0,0,target)['state'],'done')
        self.assertNotIn('/api/task/pose',[p for p,_ in self.api.calls])
    def test_extend_retract_step_mismatch_and_mutating_submitted_action_rejected(self):
        validate_hand_steps({'subTasks':[{'action':'V','target':{'handAction':'right_index_retract'}}]})
        for action,target in [('U',{'handAction':'right_index_retract'}),('V',TARGET)]:
            with self.assertRaises(ValueError):validate_hand_steps({'subTasks':[{'action':action,'target':target}]})
        self.run_step()
        with self.assertRaises(ValueError):self.hand.step(0,0,{'handAction':'right_index_retract'})
