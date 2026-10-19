"""No-robot tests for stage isolation and the playback velocity bound."""
import math
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
import playback as app


class Clock:
    def __init__(self):self.now=100.;self.on_sleep=None
    def monotonic(self):return self.now
    def sleep(self,seconds):
        self.now+=max(.000001,seconds)
        if self.on_sleep:self.on_sleep()


class FakeBridge:
    def __init__(self,bias=0.):
        self.q=[0.]*29;self.commands=[];self.released=False
        self.bias=bias
        self.process=self;self.thread=self;self.finished=None
    def snapshot(self):
        return {'q':self.q[:],'dq':[0.]*29,'gyro':[0.]*3,'rpy':[0.]*3}
    def state(self):return dict(self.snapshot(),connected=True,sent=len(self.commands),weight=0)
    def send(self,q,weight,softness):
        self.commands.append((q[:],weight,softness));self.q[12:29]=q
        self.q[22]+=self.bias
    def write(self,line):
        if line!='RELEASE':raise AssertionError('unexpected command')
        self.released=True;self.finished={'returncode':0,'final_weight':0,'reason':'RELEASE',
                                         'sent':len(self.commands),'zero_weight_frames':10}
    def poll(self):return self.finished['returncode'] if self.finished else None
    def handback_confirmed(self):return bool(self.finished and self.finished['final_weight']==0)
    def wait(self,timeout):return 0
    def join(self,timeout):pass
    def release(self):self.write('RELEASE')
    def close(self):pass


class Tests(unittest.TestCase):
    def test_quintic_interpolation_respects_velocity_bound(self):
        start=[0.]*14;end=[math.radians(90),math.radians(-20)]+[0.]*12
        seconds=app.duration(start,end)
        for i in range(10000):
            speed=abs(app.blend((i+1)/10000)-app.blend(i/10000))*abs(end[0])/(seconds/10000)
            self.assertLessEqual(speed,math.radians(20)+1e-7)
        self.assertEqual(app.blend(0),0);self.assertEqual(app.blend(1),1)

    def test_twenty_degree_peak_halves_long_segment_duration(self):
        start=[0.]*14;end=[math.radians(90)]+[0.]*13
        self.assertAlmostEqual(app.duration(start,end),app.duration(start,end,rate_deg_s=10)/2)

    def test_position_at_two_degrees_is_accepted(self):
        state={'q':[0.]*29,'dq':[0.]*29};state['q'][22]=math.radians(2)
        self.assertTrue(app.arrival_metrics(state,[0.]*14)['position_ok'])

    def fixture(self):
        r=app.Replay.__new__(app.Replay)
        r.lock=threading.RLock();r.phase='readonly';r.error='';r.completed=0;r.requested=0
        r.started=None;r.ended=None;r.stopped=threading.Event();r.bridge=FakeBridge()
        r.points=[{'q_arm':[math.radians(20)]+[0.]*13},{'q_arm':[math.radians(-80)]+[0.]*13}]
        r.target=None;r.segment_duration=0;r.segment_elapsed=0.;r.motion_elapsed=0.
        r.holding_started=None;r.directory=Path('fake');r.events=[];r.arrival=None
        r.service=None;r.restore_result=None
        r.mode='staged'
        def log(event,**values):
            r.events.append((event,values))
            if event=='point_reached':r.stopped.set()
        r.log=log
        return r

    def test_no_onsite_confirmation_produces_no_commands(self):
        r=self.fixture()
        with self.assertRaisesRegex(RuntimeError,'onsite'):r.next(False)
        self.assertEqual(r.bridge.commands,[])

    def test_one_click_runs_one_point_then_stops_without_next_point(self):
        r=self.fixture();clock=Clock()
        clock.on_sleep=lambda:setattr(r,'last_heartbeat',clock.now)
        with patch.object(app,'time',clock),patch.object(app.threading.Thread,'start',lambda self:None):
            r.next(True)
            with self.assertRaisesRegex(RuntimeError,'not_ready'):r.next(True)
            r.run()
        self.assertEqual(r.completed,1)
        self.assertEqual(r.bridge.commands[0][1],0)
        self.assertTrue(r.bridge.released)
        self.assertEqual(r.phase,'ended')
        self.assertTrue(all(-1e-8<=q[3]<=math.radians(20)+1e-8 for q,w,s in r.bridge.commands))
        self.assertEqual(sum(event=='stage_requested' for event,_ in r.events),1)
        self.assertEqual(sum(event=='point_reached' for event,_ in r.events),1)

    def test_lost_browser_heartbeat_releases_without_reaching_next_point(self):
        r=self.fixture();clock=Clock()
        with patch.object(app,'time',clock),patch.object(app.threading.Thread,'start',lambda self:None):
            r.next(True);r.run()
        self.assertEqual(r.error,'browser_heartbeat_lost')
        self.assertEqual(r.completed,0)
        self.assertTrue(r.bridge.released)
        self.assertEqual(r.phase,'ended_with_error')

    def test_recorded_8_4_degree_offset_fails_restored_two_degree_tolerance(self):
        state={'q':[0.]*29,'dq':[0.]*29}
        state['q'][22]=math.radians(8.4);state['dq'][22]=.02
        metrics=app.arrival_metrics(state,[0.]*14)
        self.assertFalse(metrics['position_ok']);self.assertTrue(metrics['speed_ok'])
        self.assertEqual(metrics['joint_index'],22)
        self.assertEqual(metrics['joint'],'右肩前后摆')

    def test_stable_8_4_degree_offset_times_out_and_does_not_complete_stage(self):
        r=self.fixture();r.bridge=FakeBridge(math.radians(8.4));clock=Clock()
        clock.on_sleep=lambda:setattr(r,'last_heartbeat',clock.now)
        with patch.object(app,'time',clock),patch.object(app.threading.Thread,'start',lambda self:None):
            r.next(True);r.run()
        self.assertEqual(r.completed,0)
        self.assertEqual(r.error,'target_not_reached_no_next_stage')
        self.assertAlmostEqual(r.arrival['max_error_deg'],8.4)
        self.assertEqual(sum(event=='stage_requested' for event,_ in r.events),1)

    def test_position_outside_two_degrees_is_not_accepted(self):
        state={'q':[0.]*29,'dq':[0.]*29};state['q'][22]=math.radians(2.01)
        self.assertFalse(app.arrival_metrics(state,[0.]*14)['position_ok'])

    def test_velocity_condition_remains_independent_of_position_tolerance(self):
        state={'q':[0.]*29,'dq':[0.]*29};state['dq'][22]=.151
        metrics=app.arrival_metrics(state,[0.]*14)
        self.assertTrue(metrics['position_ok']);self.assertFalse(metrics['speed_ok'])


if __name__=='__main__':unittest.main()
