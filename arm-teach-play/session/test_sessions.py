"""Lifecycle and timing regressions without DDS or robot commands."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import teaching
import playback
from recording_store import RecordingStore, atomic_json
import test_replay
from test_replay import Clock, FakeBridge


class TeachingBridge(FakeBridge):
    def __init__(self, *args, **kwargs):
        super().__init__()
        self.closed = False
    def snapshot(self):
        return dict(super().snapshot(), fsm_mode=0, tick=1, mode_machine=5)
    def close(self):self.closed = True


class FakeService:
    def __init__(self, *args):self.restore_calls=[]
    def state(self):return {'busy':False,'result':None}
    def restore_after_handback(self, bridge):
        if not bridge.handback_confirmed():raise AssertionError('restore before handback')
        self.restore_calls.append(bridge)
        return {'verified':True,'operation':'restore','message':'fake restored'}


class Sessions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = RecordingStore(self.root / 'recordings')
        self.clock = Clock()
        self.patches = [patch.object(teaching, 'ROOT', self.root),
                        patch.object(teaching, 'Bridge', TeachingBridge),
                        patch.object(teaching, 'BuiltinArmService', FakeService),
                        patch.object(teaching, 'time', self.clock),
                        patch.object(playback, 'time', self.clock),
                        patch.object(playback, 'Bridge', TeachingBridge)]
        self.patches.append(patch.object(playback, 'BuiltinArmService', FakeService))
        # Formatting timestamps is independent from the simulated monotonic clock.
        import time
        self.clock.time_ns = time.time_ns
        self.clock.strftime = time.strftime
        self.clock.gmtime = time.gmtime
        for item in self.patches:
            item.start();self.addCleanup(item.stop)
        self.runtime = {'upstream_commit': 'test'}

    def record(self, console, name='测试项目', limit=30, stop_after=8):
        with patch.object(threading.Thread, 'start', lambda self: None):
            console.action('begin', {'name': name, 'onsite': True, 'max_seconds': limit})
        session = console.session
        start = self.clock.now
        def tick():
            session.last_heartbeat = self.clock.now
            if session.phase == 'teaching' and not session.waypoints:
                session.mark('测试点')
            if stop_after is not None and self.clock.now-start >= stop_after:
                session.stop.set()
        self.clock.on_sleep = tick
        session.record(session.bridge.snapshot())
        return session

    def saved(self, name, points=1):
        path = self.store.create()
        atomic_json(path / 'metadata.json', {'name': name, 'saved': True, 'phase': 'ended'})
        atomic_json(path / 'waypoints.json', [{'q_arm': [0.]*14, 'label': '点'} for _ in range(points)])
        return path

    def test_two_recordings_same_name_are_preserved_and_selectable(self):
        app = teaching.TeachingConsole(self.runtime, 'fake', self.store)
        first = self.record(app)
        first_file = (first.directory / 'samples.jsonl').read_bytes()
        first.save('第一条')
        app.new()
        self.assertTrue(first.bridge.closed)
        self.assertEqual(app.session.state()['elapsed_s'], 0)
        self.assertIsNone(app.session.directory)
        second = self.record(app)
        second.save('第一条')
        self.assertNotEqual(first.directory, second.directory)
        self.assertEqual((first.directory / 'samples.jsonl').read_bytes(), first_file)
        catalog = self.store.catalog()
        self.assertEqual(len(catalog), 2)
        self.assertTrue(all(item['playable'] for item in catalog))
        replay = playback.PlaybackConsole(self.runtime, 'fake', self.store, first.directory.name)
        self.assertEqual(replay.state()['total'], 1)
        before = replay.replay
        replay.select(second.directory.name)
        self.assertTrue(before.bridge.closed)
        self.assertEqual(replay.state()['selected_id'], second.directory.name)
        self.assertEqual(replay.replay.bridge.commands, [])
        replay.replay.close();second.close()

    def test_record_timer_freezes_after_stop_and_excludes_four_second_preparation(self):
        app = teaching.TeachingConsole(self.runtime, 'fake', self.store)
        session = self.record(app)
        elapsed = session.state()['elapsed_s']
        self.assertAlmostEqual(elapsed, 4., delta=.021)
        self.clock.now += 1000
        self.assertEqual(session.state()['elapsed_s'], elapsed)
        meta = json.loads((session.directory / 'metadata.json').read_text())
        self.assertEqual(meta['recording_duration_s'], elapsed)
        session.close()

    def test_record_limit_ends_normally_and_saves(self):
        app = teaching.TeachingConsole(self.runtime, 'fake', self.store)
        session = self.record(app, stop_after=None)
        self.assertEqual(session.phase, 'ended')
        self.assertEqual(session.error, '')
        self.assertAlmostEqual(session.state()['elapsed_s'], 30, delta=.021)
        self.assertTrue(self.store.describe(session.directory.name)['playable'])
        self.assertEqual(session.bridge.commands[0][1], 0)
        self.assertTrue(session.bridge.released)
        self.assertEqual(app.service.restore_calls,[session.bridge])
        self.assertTrue(session.restore_result['verified'])
        session.close()

    def test_cannot_new_or_switch_while_active(self):
        app = teaching.TeachingConsole(self.runtime, 'fake', self.store)
        app.session.phase = 'teaching'
        with self.assertRaises(RuntimeError):app.new()
        self.assertFalse(app.session.bridge.closed)
        app.session.phase = 'readonly';app.session.close()
        a, b = self.saved('A', 2), self.saved('B', 7)
        replay = playback.PlaybackConsole(self.runtime, 'fake', self.store, a.name)
        replay.replay.phase = 'holding'
        with self.assertRaises(RuntimeError):replay.select(b.name)
        self.assertEqual(replay.state()['selected_id'], a.name)
        replay.replay.phase = 'readonly';replay.replay.close()

    def test_invalid_name_onsite_and_limit_do_not_create_recordings(self):
        app = teaching.TeachingConsole(self.runtime, 'fake', self.store)
        for data in ({'name': '', 'onsite': True}, {'name': '测试', 'onsite': False},
                     {'name': '测试', 'onsite': True, 'max_seconds': 601}):
            with self.assertRaises((ValueError, RuntimeError)):app.action('begin', data)
        self.assertEqual(self.store.catalog(), [])
        self.assertEqual(app.session.bridge.commands, [])
        app.session.close()

    def test_unfinished_or_empty_recording_cannot_play_and_path_traversal_rejected(self):
        empty = self.saved('无关键点', 0)
        draft = self.saved('未结束', 2)
        atomic_json(draft / 'metadata.json', {'phase': 'teaching', 'saved': False})
        for value in (empty.name, draft.name, '../session', str(empty), '..', 'A/B'):
            with self.assertRaises(ValueError):self.store.playback(value)

    def test_arbitrary_point_counts_and_invalid_pose_validation(self):
        for count in (1, 2, 7):
            path = self.saved('数量测试', count)
            self.assertEqual(len(playback.load_points(path)), count)
        path = self.saved('损坏记录')
        atomic_json(path / 'waypoints.json', [{'q_arm': [100.]*14}])
        with self.assertRaisesRegex(ValueError, 'invalid_recorded_pose'):playback.load_points(path)

    def test_waiting_90_seconds_does_not_spend_next_stage_time_or_motion_timer(self):
        r = test_replay.Tests().fixture()
        r.started = 10.;r.phase = 'holding';r.completed = 1;r.requested = 1
        r.holding_started = 20.;r.motion_elapsed = 8.;r.segment_elapsed = 8.
        r.seed = [0.]*17
        before = r.state()
        self.clock.now = 110.
        self.assertEqual(r.state()['elapsed_s'], before['elapsed_s'])
        self.assertEqual(r.state()['stage_elapsed_s'], 8.)
        r.next(True)
        self.assertEqual(r.phase, 'moving')
        self.assertEqual(r.requested, 2)
        self.assertEqual(r.segment_elapsed, 0.)
        self.clock.on_sleep = lambda: setattr(r, 'last_heartbeat', self.clock.now)
        r.run()
        self.assertEqual(r.completed, 2)
        self.assertEqual(r.error, '')
        self.assertLess(r.motion_elapsed, 30.)
        frozen = r.state()
        self.clock.now += 1000
        self.assertEqual(r.state()['session_elapsed_s'], frozen['session_elapsed_s'])
        self.assertEqual(r.state()['elapsed_s'], frozen['elapsed_s'])

    def test_holding_has_independent_bounded_timeout(self):
        r = test_replay.Tests().fixture()
        r.started = self.clock.now-20;r.phase = 'holding';r.completed = 1
        r.target = [0.]*14;r.seed = [0.]*17;r.holding_started = self.clock.now
        r.last_heartbeat = self.clock.now
        self.clock.on_sleep = lambda: setattr(r, 'last_heartbeat', self.clock.now)
        r.run()
        self.assertEqual(r.completed, 1)
        self.assertEqual(r.error, '')
        self.assertTrue(r.bridge.released)
        self.assertEqual(r.phase, 'ended')
        self.assertEqual(r.motion_elapsed, 0.)
        self.assertTrue(any(event == 'holding_limit_reached' for event, _ in r.events))

    def test_full_two_stage_loop_keeps_streaming_during_90_second_wait(self):
        r = test_replay.Tests().fixture()
        r.events = []
        def log(event, **values):
            r.events.append((event, values))
            if event == 'point_reached' and r.completed == 2:r.stopped.set()
        r.log = log
        hold_samples = []
        def tick():
            r.last_heartbeat = self.clock.now
            if r.phase == 'holding' and r.completed == 1:
                hold_samples.append((self.clock.now, r.motion_elapsed, len(r.bridge.commands)))
                if self.clock.now-r.holding_started >= 90:r.next(True)
        self.clock.on_sleep = tick
        with patch.object(threading.Thread, 'start', lambda self: None):r.next(True)
        r.run()
        self.assertEqual(r.completed, 2)
        self.assertEqual(r.error, '')
        self.assertGreater(len(hold_samples), 4000)
        self.assertEqual(hold_samples[0][1], hold_samples[-1][1])
        self.assertGreater(hold_samples[-1][2], hold_samples[0][2]+4000)
        self.assertEqual(sum(event == 'stage_requested' for event, _ in r.events), 2)
        self.assertLess(r.motion_elapsed, 30)

    def test_failed_service_restore_is_visible_and_recording_is_still_saved(self):
        app = teaching.TeachingConsole(self.runtime, 'fake', self.store)
        app.service.restore_after_handback = lambda bridge: {'verified':False,'message':'fake query failure'}
        session = self.record(app)
        self.assertTrue(session.bridge.released)
        self.assertEqual(session.phase,'ended_with_error')
        self.assertIn('fake query failure',session.error)
        self.assertTrue(session.saved)
        meta=json.loads((session.directory/'metadata.json').read_text(encoding='utf-8'))
        self.assertFalse(meta['service_restore']['verified'])
        session.restored({'verified':True,'message':'retried'})
        self.assertEqual(session.phase,'ended')
        self.assertEqual(session.error,'')
        session.close()

    def test_new_recording_is_blocked_while_service_is_restoring(self):
        app = teaching.TeachingConsole(self.runtime, 'fake', self.store)
        original=app.service.restore_after_handback
        def restore(bridge):
            self.assertEqual(app.session.phase,'restoring_service')
            with self.assertRaises(RuntimeError):app.new()
            return original(bridge)
        app.service.restore_after_handback=restore
        session=self.record(app)
        self.assertEqual(session.phase,'ended')
        session.close()


if __name__ == '__main__':unittest.main()
