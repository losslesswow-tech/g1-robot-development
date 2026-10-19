"""Continuous samples, timing repairs and full lifecycle without DDS."""
import json
import math
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from continuous_trajectory import ContinuousTrajectory
from recording_store import RecordingStore,atomic_json
import playback
from test_replay import Clock,FakeBridge


class ContinuousTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=RecordingStore(Path(self.temp.name)/'recordings')
        self.directory=self.store.create();self.clock=Clock();self.clock.time_ns=time.time_ns
        self.restore_calls=[]
        def restore(bridge):
            self.assertTrue(bridge.released);self.assertTrue(bridge.handback_confirmed())
            self.restore_calls.append(bridge)
            return {'verified':True,'operation':'restore','message':'fake restored'}
        self.service=types.SimpleNamespace(restore_after_handback=restore)
        self.bridge=FakeBridge()
        self.patches=[patch.object(playback,'Bridge',return_value=self.bridge),patch.object(playback,'time',self.clock)]
        for item in self.patches:item.start();self.addCleanup(item.stop)
        self.samples([4,5,6,7],[0,20,20,0])

    def samples(self,times,angles,points=0):
        rows=[{'phase':'acquiring','t_s':0,'q_upper':[0.]*17}]
        for t,q in zip(times,angles):
            pose=[0.]*17;pose[3]=math.radians(q)
            rows.append({'phase':'teaching','t_s':t,'q_upper':pose})
        (self.directory/'samples.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf-8')
        atomic_json(self.directory/'metadata.json',{'phase':'ended','saved':True,'sampling_target_hz':50})
        atomic_json(self.directory/'waypoints.json',[{'q_arm':[0.]*14} for _ in range(points)])

    def trajectory(self):return ContinuousTrajectory(self.directory,playback.arm_limits())
    def replay(self):return playback.ContinuousReplay(self.directory,{},'fake',self.service)
    def execute(self,r,heartbeat=True,on_sleep=None):
        def tick():
            if heartbeat:r.last_heartbeat=self.clock.now
            if on_sleep:on_sleep()
        self.clock.on_sleep=tick
        with patch.object(playback.threading.Thread,'start',lambda self:None):r.start(True)
        r.run()

    def test_nonmonotonic_path_and_pause_are_loaded_from_all_samples(self):
        trajectory=self.trajectory()
        self.assertEqual(trajectory.sample_count,4)
        self.assertEqual(trajectory.total_rows,5)
        self.assertAlmostEqual(trajectory.at(1.5*trajectory.time_scale)[0][0],math.radians(20))
        self.assertEqual(trajectory.poses[0],trajectory.poses[-1])
        self.assertGreater(max(q[0] for q in trajectory.poses),0)

    def test_velocity_retiming_bounds_every_raw_interval(self):
        self.samples([4,4.5,5],[0,90,0])
        trajectory=self.trajectory()
        self.assertGreaterEqual(trajectory.time_scale,9)
        for t0,t1,p0,p1 in zip(trajectory.times,trajectory.times[1:],trajectory.poses,trajectory.poses[1:]):
            self.assertLessEqual(max(abs(a-b)/(t1-t0) for a,b in zip(p0,p1)),math.radians(20))

    def test_duplicate_timestamps_preserve_every_pose_and_original_file(self):
        self.samples([4,4,4.04],[0,.5,1])
        original=(self.directory/'samples.jsonl').read_bytes()
        trajectory=self.trajectory()
        self.assertTrue(trajectory.timebase_repaired);self.assertEqual(trajectory.duplicate_timestamps,1)
        self.assertEqual(len(trajectory.poses),3)
        self.assertAlmostEqual(trajectory.recorded_duration_s,.04)
        self.assertTrue(all(b>a for a,b in zip(trajectory.times,trajectory.times[1:])))
        self.assertEqual((self.directory/'samples.jsonl').read_bytes(),original)

    def test_backwards_timestamps_and_unrecoverable_duplicates_are_rejected(self):
        for timestamps in ([4,3,5],[4,4,4.001]):
            self.samples(timestamps,[0,1,2])
            with self.assertRaises(ValueError):self.trajectory()

    def test_invalid_or_out_of_limits_joint_data_is_rejected(self):
        for value in (float('nan'),float('inf'),1000):
            self.samples([4,5],[0,value])
            with self.assertRaises(ValueError):self.trajectory()

    def test_recording_without_keypoints_is_selectable_for_continuous_mode_only(self):
        item=self.store.describe(self.directory.name)
        self.assertFalse(item['playable']);self.assertTrue(item['continuous_playable'])
        self.assertEqual(self.store.playback(self.directory.name,'continuous'),self.directory)
        with self.assertRaises(ValueError):self.store.playback(self.directory.name,'staged')
        atomic_json(self.directory/'metadata.json',{'phase':'teaching','saved':False})
        self.assertFalse(self.store.describe(self.directory.name)['continuous_playable'])

    def test_no_onsite_confirmation_or_duplicate_start_produces_extra_commands(self):
        r=self.replay()
        with self.assertRaises(RuntimeError):r.start(False)
        self.assertEqual(self.bridge.commands,[])
        with patch.object(playback.threading.Thread,'start',lambda self:None):r.start(True)
        with self.assertRaises(RuntimeError):r.start(True)
        self.assertEqual(self.bridge.commands,[])
        r.stopped.set();r.run();r.close()

    def test_one_start_replays_middle_excursion_and_pause_then_restores_once(self):
        r=self.replay();self.execute(r)
        self.assertTrue(r.continuous_complete);self.assertEqual(r.phase,'ended')
        self.assertEqual(r.error,'');self.assertEqual(r.sample_index,4)
        self.assertEqual(self.bridge.commands[0][1],0)
        for (a,_,_),(b,_,_) in zip(self.bridge.commands,self.bridge.commands[1:]):
            self.assertLessEqual(max(abs(x-y) for x,y in zip(a,b))/.02,math.radians(20)+1e-7)
        self.assertGreater(max(q[3] for q,w,s in self.bridge.commands),math.radians(19.5))
        self.assertAlmostEqual(self.bridge.commands[-1][0][3],0,delta=.001)
        plateau=[q[3] for q,w,s in self.bridge.commands if abs(q[3]-math.radians(20))<.001]
        self.assertGreater(len(plateau),40)
        self.assertEqual(self.restore_calls,[self.bridge])
        frozen=r.state();self.clock.now+=1000
        self.assertEqual(r.state()['trajectory_elapsed_s'],frozen['trajectory_elapsed_s'])
        self.assertEqual(r.state()['session_elapsed_s'],frozen['session_elapsed_s'])
        r.close()

    def test_start_pose_failure_never_enters_continuous_path(self):
        self.bridge.bias=math.radians(8.4)
        r=self.replay();self.execute(r)
        self.assertEqual(r.error,'continuous_start_not_reached')
        self.assertEqual(r.trajectory_elapsed,0)
        self.assertFalse(r.continuous_complete)
        self.assertTrue(self.bridge.released);self.assertEqual(len(self.restore_calls),1)
        r.close()

    def test_tracking_fault_freezes_cursor_and_times_out_with_handback(self):
        r=self.replay();paused=[]
        def tick():
            if r.trajectory_elapsed>.4:self.bridge.bias=math.radians(6)
            if r.phase=='tracking_wait':paused.append(r.trajectory_elapsed)
        self.execute(r,on_sleep=tick)
        self.assertEqual(r.error,'continuous_tracking_error_timeout')
        self.assertGreater(len(paused),200)
        self.assertTrue(all(t==paused[0] for t in paused))
        self.assertLess(r.trajectory_elapsed,r.trajectory.duration_s)
        self.assertEqual(len(self.restore_calls),1);r.close()

    def test_temporary_tracking_lag_resumes_same_continuous_path(self):
        r=self.replay();fault_at=None
        def tick():
            nonlocal fault_at
            if fault_at is None and r.trajectory_elapsed>.4:fault_at=self.clock.now
            self.bridge.bias=math.radians(6) if fault_at is not None and self.clock.now-fault_at<1 else 0.
        self.execute(r,on_sleep=tick)
        self.assertTrue(r.continuous_complete);self.assertEqual(r.error,'')
        events=[json.loads(line)['event'] for line in (r.directory/'events.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertIn('tracking_paused',events);self.assertIn('tracking_resumed',events)
        r.close()

    def test_stop_mid_path_releases_and_restores_without_reporting_completion(self):
        r=self.replay()
        self.execute(r,on_sleep=lambda:r.stopped.set() if r.trajectory_elapsed>.5 else None)
        self.assertFalse(r.continuous_complete)
        self.assertLess(r.trajectory_elapsed,r.trajectory.duration_s)
        self.assertEqual(len(self.restore_calls),1);self.assertTrue(self.bridge.released)
        r.close()

    def test_browser_heartbeat_loss_stops_and_restores(self):
        r=self.replay();self.execute(r,heartbeat=False)
        self.assertEqual(r.error,'browser_heartbeat_lost')
        self.assertFalse(r.continuous_complete);self.assertEqual(len(self.restore_calls),1)
        r.close()

    def test_overlong_trajectory_is_rejected_before_bridge_creation(self):
        self.samples([4,904],[0,0])
        with patch.object(playback,'Bridge') as bridge:
            with self.assertRaises(ValueError):self.replay()
            bridge.assert_not_called()

    def test_changing_mode_while_continuous_playback_is_active_is_rejected(self):
        self.samples([4,5,6,7],[0,20,20,0],points=1)
        with patch.object(playback,'BuiltinArmService',return_value=self.service):
            app=playback.PlaybackConsole({},'fake',self.store,self.directory.name,mode='continuous')
        app.replay.phase='continuous'
        with self.assertRaises(RuntimeError):app.select(self.directory.name,'staged')
        self.assertEqual(app.mode,'continuous')
        app.replay.phase='readonly';app.replay.close()


if __name__=='__main__':unittest.main()
