"""Execute the patched vendor task flow with fake ROS/navigation/audio transports."""
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from arrival_voice.task_voice import TaskVoice
from navigation_task.patch_task import patch_task


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / '1009').mkdir()
        (self.root / '1009/nav_points.txt').write_text('0 0 0 0 0 0 1 0 0 0 origin\n2 1 -.5 0 0 0 1 0 0 0 2\n')
        (self.root / 'task.json').write_text('[]')
        self.now, self.calls, self.motion, self.plans = 10, [], [], []
        self.plan_result = 1
        def audio(url, payload=None, token=None, timeout=2):
            if url.endswith('/api/state'):
                return {'live': True, 'config': {'points': []}}
            self.calls.append(payload)
            return {'submitted': True, 'simulation': False}
        self.voice = TaskVoice(self.root / 'state', self.root, request=audio, clock=lambda: self.now)
        self.voice.editor_token = lambda: 'test-token'
        class Blueprint:
            def __init__(self, *args, **kwargs):
                pass
            def route(self, *args, **kwargs):
                return lambda function: function
        class String:
            def __init__(self, data='', publisher='/w_nav'):
                self.data = data
                self._connection_header = {'callerid': publisher}
        self.String = String
        modules = {
            'rospy': types.SimpleNamespace(get_param=lambda key, default=None: str(self.root) if key == '/x_nav/nav_map_path' else default,
                                          Publisher=lambda *a, **kw: types.SimpleNamespace(publish=self.motion.append), Subscriber=lambda *a, **kw: None),
            'flask': types.SimpleNamespace(Blueprint=Blueprint, jsonify=lambda value: value, request=None, current_app=None),
            'std_msgs.msg': types.SimpleNamespace(String=String),
            'nav_msgs.msg': types.SimpleNamespace(Path=object, Odometry=object),
            'extra': types.SimpleNamespace(get_current_map_name=lambda: '1009'),
            'requests': types.SimpleNamespace(RequestException=RuntimeError,
                                              get=lambda *a, **kw: self.fail('Unexpected external HTTP request'),
                                              post=lambda *a, **kw: self.fail('Unexpected external HTTP request')),
            'croniter': types.SimpleNamespace(croniter=None),
            'g1_task_voice': types.SimpleNamespace(TaskVoice=lambda *a, **kw: self.voice,
                                                  register_voice_routes=lambda b: None,
                                                  validate_speech_steps=__import__('arrival_voice.task_voice', fromlist=['validate_speech_steps']).validate_speech_steps)
        }
        source = patch_task((Path(__file__).parents[1] / 'navigation_task/upstream/task.py').read_text(encoding='utf-8'))
        namespace = {'__name__': 'task_fixture'}
        with patch.dict('sys.modules', modules):
            exec(compile(source, 'patched-task.py', 'exec'), namespace)
            def plan(*args):
                self.plans.append(args)
                return self.plan_result
            self.server = namespace['TaskServer'](types.SimpleNamespace(pub_cmd=types.SimpleNamespace(publish=self.motion.append)),
                                                   types.SimpleNamespace(call_nav_function_service_cmd_string=plan))
        namespace['time'] = types.SimpleNamespace(monotonic=lambda: self.now)
        namespace['rospy'].Time = types.SimpleNamespace(now=lambda: self.now)
        self.namespace = namespace

    def tearDown(self):
        if self.voice.job:
            self.voice.job['thread'].join(3)
        self.voice.gate.close()
        self.voice.db.close()
        self.directory.cleanup()

    def start(self, steps):
        self.server.all_task = [{'taskName': 'test', 'subTasks': steps}]
        self.server.do_task(0)

    def emit(self, raw, publisher='/w_nav'):
        self.server.nav_state_callback1(self.String(raw, publisher))

    def arrive(self):
        self.emit('14', '/x_planner')
        pose = types.SimpleNamespace(position=types.SimpleNamespace(x=2, y=1, z=-.48),
                                     orientation=types.SimpleNamespace(x=0, y=0, z=0, w=1))
        self.server._voice_goal_callback(types.SimpleNamespace(_connection_header={'callerid': '/x_planner'},
                                         header=types.SimpleNamespace(frame_id='map'), poses=[types.SimpleNamespace(pose=pose)]))
        self.emit('12')
        self.emit('13')
        self.now += 1
        self.emit('-1')

    def test_navigation_then_speech_advances_only_after_proven_arrival(self):
        self.start([{'action': 'E', 'target': {'waypointIndex': 2}}, {'action': 'J', 'target': {'speechText': 'arrived'}}])
        self.emit('-1')
        self.assertEqual(len(self.plans), 1)
        self.emit('13', '/foreign')
        self.assertEqual(self.server.task_index, 0)
        self.assertEqual(self.calls, [])
        self.arrive()
        self.assertEqual(self.server.task_index, 1)
        self.emit('-1')
        self.voice.job['thread'].join(3)
        self.emit('-1')
        self.assertEqual(self.server.task_index, 2)
        self.emit('-1')
        self.assertFalse(self.voice.managed)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.motion, [])

    def test_rejected_plan_stops_task_before_speech(self):
        self.plan_result = 0
        self.start([{'action': 'L', 'target': {'waypointIndex': 2}}, {'action': 'J', 'target': {'speechText': 'hello'}}])
        self.emit('-1')
        self.assertFalse(self.server.task_buf)
        self.emit('13')
        self.assertEqual(self.calls, [])

    def test_line_arrival_speech_then_next_navigation(self):
        self.start([{'action': 'L', 'target': {'waypointIndex': 2}},
                    {'action': 'J', 'target': {'speechText': '到达五号点'}},
                    {'action': 'L', 'target': {'waypointIndex': 1}}])
        self.emit('-1')
        self.assertEqual(self.voice.nav['mode'], 'line')
        self.emit('14', '/x_planner')
        pose = types.SimpleNamespace(position=types.SimpleNamespace(x=2.12, y=1, z=-.48),
                                     orientation=types.SimpleNamespace(x=0, y=0, z=.7071, w=.7071))
        self.server._voice_goal_callback(types.SimpleNamespace(_connection_header={'callerid': '/x_planner'},
                header=types.SimpleNamespace(frame_id='map'), poses=[types.SimpleNamespace(pose=pose)]))
        self.emit('1#19#20')
        self.emit('13')
        self.now += .6
        class Stamp:
            def __rsub__(stamp, now):
                return types.SimpleNamespace(to_sec=lambda: .05)
        self.server._voice_pose_callback(types.SimpleNamespace(_connection_header={'callerid': '/x_nav'},
                header=types.SimpleNamespace(frame_id='map', stamp=Stamp()),
                pose=types.SimpleNamespace(pose=types.SimpleNamespace(position=types.SimpleNamespace(x=1.91,y=1.06,z=-.5)))))
        self.emit('-1')
        self.assertEqual(self.server.task_index, 1)
        self.emit('-1')
        self.voice.job['thread'].join(3)
        self.emit('-1')
        self.assertEqual(self.calls[0]['point']['text'], '到达五号点')
        self.emit('-1')
        self.assertEqual([p[2] for p in self.plans], ['2', '1'])
        self.assertEqual(self.motion, [])

    def test_invalid_speech_is_not_saved_or_started(self):
        with self.assertRaises(ValueError):
            self.server.create_task({'taskName': 'invalid', 'subTasks': [{'action': 'J', 'target': {}}]})
        self.assertEqual(self.server.all_task, [])
        self.start([{'action': 'J', 'target': {}}])
        self.assertFalse(self.voice.managed)
        self.assertFalse(self.server.task_buf)

    def test_voice_failure_stops_before_next_navigation(self):
        self.voice.editor_token = lambda: (_ for _ in ()).throw(RuntimeError('Disconnected'))
        self.start([{'action': 'J', 'target': {'speechText': 'hello'}}, {'action': 'E', 'target': {'waypointIndex': 2}}])
        self.emit('-1')
        self.voice.job['thread'].join(3)
        self.emit('-1')
        self.assertFalse(self.server.task_buf)
        self.emit('-1')
        self.assertEqual(self.plans, [])
        self.assertEqual(self.motion, [])


if __name__ == '__main__':
    unittest.main()
