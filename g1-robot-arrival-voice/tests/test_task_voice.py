import json
from pathlib import Path
import tempfile
import threading
import unittest
import io
from unittest.mock import patch
from urllib.error import HTTPError

from arrival_voice.task_voice import TaskVoice, speech_target, validate_speech_steps
from arrival_voice.legacy_auto import read_context, submission_reason, submit_standalone


class TaskVoiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / '1009').mkdir()
        self.registry = self.root / '1009/nav_points.txt'
        self.registry.write_text('0 0 0 0 0 0 1 0 0 0 origin\n2 1 -0.5 0 0 0 1 0 0 0 2\n')
        self.now = 10
        self.calls = []
        self.response = {'submitted': True, 'simulation': False}
        self.prepare_wait = None
        def request(url, payload=None, token=None, timeout=2):
            if url.endswith('/api/state'):
                if self.prepare_wait:
                    self.prepare_wait.wait(2)
                return {'live': True, 'config': {'points': [
                    {'map_name': '1009', 'point_name': '2', 'mode': 'wav', 'file': 'audio/' + 'a'*64 + '.wav'}]}}
            self.calls.append(payload)
            if isinstance(self.response, Exception):
                raise self.response
            return self.response
        self.voice = TaskVoice(self.root / 'state', self.root, request=request, clock=lambda: self.now)
        self.voice.editor_token = lambda: 'test-token'
        self.voice.start(True)

    def tearDown(self):
        if self.prepare_wait:
            self.prepare_wait.set()
        if self.voice.job:
            self.voice.job['thread'].join(3)
        self.voice.gate.close()
        self.voice.db.close()
        self.directory.cleanup()

    def finish(self, index=0, loop=0, target=None):
        target = target or {'speechText': 'hello'}
        self.voice.step(loop, index, target)
        self.voice.job['thread'].join(3)
        return self.voice.step(loop, index, target)

    def record(self, raw, publisher='/w_nav'):
        self.voice.observe({'topic': '/x_nav/state', 'raw': raw, 'publisher': publisher, 'received': self.now})

    def path(self, x=2):
        self.voice.observe({'topic': '/x_nav/goal_path', 'publisher': '/x_planner', 'received': self.now,
                            'header': {'frame_id': 'map'}, 'pose_count': 1,
                            'end_pose': {'position': [x, 1, -0.48], 'orientation': [0, 0, 0, 1]}})

    def test_validation_and_legacy_text(self):
        self.assertEqual(speech_target('hello')['speechText'], 'hello')
        for target in ({}, {'speechText': -1}, {'speechText': 'x', 'speakerId': True},
                       {'speechMode': 'saved', 'speechRef': '[]'},
                       {'speechMode': 'wav', 'speechFile': '../file.wav'}):
            with self.assertRaises(ValueError):
                speech_target(target)
        self.assertTrue(validate_speech_steps({'subTasks': [{'action': 'J', 'target': {'speechText': 'hi'}}]}))
        with self.assertRaises(ValueError):
            validate_speech_steps({'defaults': {'errorSteps': [{'action': 'J'}]}})

    def test_one_submission_per_step_loop_and_run(self):
        self.assertEqual(self.finish()['state'], 'submitted')
        self.assertEqual(self.finish()['state'], 'submitted')
        self.assertEqual(len(self.calls), 1)
        self.finish(loop=1)
        self.voice.start(True)
        self.finish()
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len({c['request_id'] for c in self.calls}), 3)

    def test_timeout_and_dry_run_stop_without_retry(self):
        for result in (TimeoutError('unknown'), {'submitted': True, 'simulation': True}):
            self.voice.start(True)
            self.response = result
            count = len(self.calls)
            self.assertEqual(self.finish()['state'], 'failed')
            self.assertEqual(self.finish()['state'], 'failed')
            self.assertEqual(len(self.calls), count + 1)

    def test_saved_and_imported_wav_use_existing_audio_endpoint(self):
        target = {'speechMode': 'saved', 'speechRef': json.dumps(['1009', '2'])}
        self.finish(target=target)
        self.assertEqual(self.calls[-1]['point']['mode'], 'wav')
        self.finish(index=1, target={'speechMode': 'wav', 'speechFile': 'audio/' + 'b'*64 + '.wav'})
        self.assertEqual(self.calls[-1]['point']['file'], 'audio/' + 'b'*64 + '.wav')

    def test_pause_and_cancel_before_submit(self):
        self.prepare_wait = threading.Event()
        self.voice.step(0, 0, {'speechText': 'hello'})
        self.voice.pause()
        self.prepare_wait.set()
        self.voice.job['thread'].join(3)
        self.assertEqual(self.calls, [])
        self.voice.resume()
        self.assertEqual(self.finish()['state'], 'submitted')
        self.prepare_wait.clear()
        self.voice.step(0, 1, {'speechText': 'next'})
        self.voice.cancel()
        self.prepare_wait.set()
        self.voice.job['thread'].join(3)
        self.assertEqual(len(self.calls), 1)

    def test_navigation_requires_new_plan_expected_target_and_arrival(self):
        self.voice.begin_navigation('1009', 2)
        self.record('13')
        self.assertEqual(self.voice.navigation_state(), 'pending')
        self.record('14', '/x_planner')
        self.path()
        self.record('12')
        self.record('13', '/foreign')
        self.now += 1
        self.assertEqual(self.voice.navigation_state(), 'pending')
        self.record('13')
        self.now += 1
        self.assertEqual(self.voice.navigation_state(), 'arrived')

    def test_wrong_target_failure_registry_change_and_pause_reject_arrival(self):
        for kind in ('wrong', 'failed', 'changed', 'paused'):
            self.voice.begin_navigation('1009', 2)
            self.record('14', '/x_planner')
            self.path(9 if kind == 'wrong' else 2)
            self.record('12')
            if kind == 'failed':
                self.record('16')
            if kind == 'paused':
                self.voice.pause()
                self.voice.resume()
            self.record('13')
            self.now += 1
            if kind == 'changed':
                self.registry.write_text(self.registry.read_text() + '3 3 0 0 0 0 1 0 0 0 3\n')
            self.assertNotEqual(self.voice.navigation_state(), 'arrived')

    def pose(self, **changes):
        record = {'topic': '/base_link/odom', 'publisher': '/x_nav', 'received': self.now,
                  'frame_id': 'map', 'stamp_age': 0.05, 'position': [2, 1, -0.5]}
        record.update(changes)
        self.voice.observe(record)

    def line_arrival(self, x=2.12, orientation=None):
        self.voice.begin_navigation('1009', 2, mode='line')
        self.record('14', '/x_planner')
        self.voice.observe({'topic': '/x_nav/goal_path', 'publisher': '/x_planner', 'received': self.now,
                            'header': {'frame_id': 'map'}, 'pose_count': 20,
                            'end_pose': {'position': [x, 1, -0.48],
                                         'orientation': orientation or [0, 0, .7071, .7071]}})
        self.record('1#19#20')
        self.record('13')
        self.now += .6

    def test_line_heading_and_sampled_endpoint_accept_fresh_pose(self):
        for orientation in ([0, 0, .7071, .7071], [0, 0, 0, 0]):
            self.line_arrival(orientation=orientation)
            self.pose(position=[1.91, 1.06, -.5])
            self.assertEqual(self.voice.navigation_state(), 'arrived')

    def test_line_uses_bounded_controller_reach_distance(self):
        self.voice.line_reach_meters = .30
        self.line_arrival(x=2.27)
        self.pose(position=[2.27, 1, -.5])
        self.assertEqual(self.voice.navigation_state(), 'arrived')
        for distance in (0, .31, float('nan'), True):
            with self.assertRaises(ValueError):
                TaskVoice(self.root / 'unused', self.root, line_reach_meters=distance)

    def test_line_rejects_missing_stale_foreign_wrong_frame_far_or_invalid_pose(self):
        for changes in (None, {'stamp_age': 2}, {'publisher': '/foreign'}, {'frame_id': 'odom'},
                        {'position': [2.21, 1, 0]}, {'position': [float('nan'), 1, 0]},
                        {'latched': True}, {'received': self.now - 5}, {'stamp_age': -.1}):
            with self.subTest(changes=changes):
                self.line_arrival()
                if changes is not None:
                    self.pose(**changes)
                self.assertEqual(self.voice.navigation_state(), 'failed')

    def test_line_wrong_endpoint_duplicate_saved_points_and_changed_map_reject(self):
        self.line_arrival(x=3)
        self.pose()
        self.assertEqual(self.voice.navigation_state(), 'failed')
        self.registry.write_text(self.registry.read_text() + '2.1 1 0 0 0 0 1 0 0 0 duplicate\n')
        self.line_arrival()
        self.pose()
        self.assertEqual(self.voice.navigation_state(), 'failed')

    def test_line_requires_progress_start_and_real_arrival(self):
        for omit in ('start', 'progress', 'arrival'):
            self.voice.begin_navigation('1009', 2, mode='line')
            if omit != 'start':
                self.record('14', '/x_planner')
            self.path()
            if omit != 'progress':
                self.record('12')
            if omit != 'arrival':
                self.record('13')
            self.now += .6
            self.pose()
            self.assertNotEqual(self.voice.navigation_state(), 'arrived')

    def test_line_cancel_failure_and_changed_point_during_settle_reject(self):
        for change in ('cancel', 'failed', 'registry'):
            self.line_arrival()
            self.pose()
            if change == 'cancel':
                self.voice.observe({'topic': '/planner/cmd', 'publisher': '/x_planner',
                                    'raw': 'cancel_nav', 'received': self.now})
            elif change == 'failed':
                self.record('16')
            else:
                self.registry.write_text(self.registry.read_text() + '4 4 0 0 0 0 1 0 0 0 changed\n')
            self.assertEqual(self.voice.navigation_state(), 'failed')

    def test_normal_navigation_retains_exact_xy_and_heading_check(self):
        self.voice.begin_navigation('1009', 2)
        self.record('14', '/x_planner')
        self.path(x=2.12)
        self.record('12')
        self.record('13')
        self.now += .6
        self.pose()
        self.assertEqual(self.voice.navigation_state(), 'failed')

    def test_task_ownership_suppresses_point_automatic_and_invalidates_context(self):
        owner = {'managed': True, 'run_id': 'new-task'}
        def request(url):
            if url.endswith('/get_status'):
                return {'map_name': '1009'}
            if url.endswith('/voice_status'):
                return owner
            return {'automatic': {'enabled': True}, 'config': {'points': []}}
        def rpc(uri, method, *args):
            if method == 'getSystemState':
                return [[['/x_nav/state', ['/x_planner', '/w_nav']], ['/x_nav/goal_path', ['/x_planner']]], [], []]
            return 'http://node/' + args[-1]
        with patch('arrival_voice.legacy_auto.request_json', request), \
             patch('arrival_voice.legacy_auto.rpc', rpc), \
             patch('arrival_voice.legacy_auto.urllib.request.urlopen', lambda *a, **kw: io.BytesIO(b'<meta name="voice-token" content="test-token">')):
            managed = read_context('http://nav', 'http://editor', self.root, '/test', 'http://master')
            self.assertEqual(managed['task_owner'], 'new-task')
            owner.update(managed=False, run_id='')
            unmanaged = read_context('http://nav', 'http://editor', self.root, '/test', 'http://master')
            self.assertNotEqual(managed['identity'], unmanaged['identity'])
        self.assertEqual(submission_reason('task_managed', ('1009','2'), {('1009','2')}, False), 'task_managed')
        self.assertEqual(submission_reason('armed', ('1009','2'), {('1009','2')}, False), 'claimed')

    def test_completed_task_still_suppresses_delayed_standalone_arrival(self):
        self.now += 5
        self.voice.cancel()
        calls = []
        def request(url, *args, **kwargs):
            if url.endswith('/voice_status'):
                return self.voice.status()
            calls.append(url)
            return {'submitted': True}
        with patch('arrival_voice.legacy_auto.request_json', request):
            self.assertTrue(submit_standalone('http://nav', 'http://editor', {}, 'token', 12)['task_managed'])
            self.assertEqual(calls, [])
            self.assertTrue(submit_standalone('http://nav', 'http://editor', {}, 'token', 16)['submitted'])
        self.assertEqual(len(calls), 1)

    def test_cancel_before_plan_result_invalidates_task_arrival(self):
        self.voice.begin_navigation('1009', 2)
        self.voice.observe({'topic': '/planner/cmd', 'publisher': '/master_node',
                            'raw': 'cancel_nav', 'received': self.now})
        self.record('14', '/x_planner')
        self.path()
        self.record('12')
        self.record('13')
        self.now += 1
        self.assertEqual(self.voice.navigation_state(), 'failed')


if __name__ == '__main__':
    unittest.main()
