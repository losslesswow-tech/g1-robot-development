"""Offline validation of the forced SSH worker; no hardware/network calls."""
import contextlib
import io
import json
import sys
import types
import unittest
from unittest.mock import patch

from navigation_task.audio_autostart import worker_source, unit, REMOTE_INSTALL


class AutostartTests(unittest.TestCase):
    def run_worker(self, payload):
        calls = []
        class Client:
            def SetTimeout(self, value): pass
            def Init(self): pass
            def GetVolume(self):
                calls.append('GetVolume')
                return 0, {'volume': 100}
            def TtsMaker(self, *args):
                calls.append('TtsMaker')
                return 0
        modules = {'unitree_sdk2py.core.channel': types.SimpleNamespace(ChannelFactoryInitialize=lambda *args: calls.append('channel')),
                   'unitree_sdk2py.g1.audio.g1_audio_client': types.SimpleNamespace(AudioClient=Client)}
        output = io.StringIO()
        data = json.dumps(payload).encode()
        stdin = types.SimpleNamespace(buffer=io.BytesIO(data))
        with patch.dict(sys.modules, modules), patch('sys.stdin', stdin), contextlib.redirect_stdout(output):
            try:
                exec(compile(worker_source(), 'worker.py', 'exec'), {'__name__': 'worker'})
            except SystemExit as failure:
                self.assertEqual(failure.code, 1)
        return calls, output.getvalue()

    def test_startup_only_reads_volume(self):
        calls, output = self.run_worker({'operation': 'probe', 'interface': 'eth0'})
        self.assertEqual(calls, ['channel', 'GetVolume'])
        self.assertIn('G1_ARRIVAL_AUDIO_RESULT', output)
        self.assertNotIn('submitted', output)

    def test_unrecognized_operation_and_interface_never_initialize_sdk(self):
        for payload in ({'operation': 'shell', 'interface': 'eth0'},
                        {'operation': 'probe', 'interface': 'wlan0'},
                        {'operation': 'play', 'interface': 'eth0', 'task_id': 'test',
                         'message': {'mode': 'tts', 'text': '', 'speaker_id': 0}},
                        {'operation': 'play', 'interface': 'eth0', 'task_id': 'test',
                         'message': {'mode': 'wav'}, 'pcm_wav': 'invalid'}):
            calls, output = self.run_worker(payload)
            self.assertEqual(calls, [])
            self.assertIn('error', output)

    def test_valid_tts_uses_existing_audio_api(self):
        calls, output = self.run_worker({'operation': 'play', 'interface': 'eth0', 'task_id': 'test',
                                        'message': {'mode': 'tts', 'text': 'test', 'speaker_id': 0}})
        self.assertEqual(calls, ['channel', 'GetVolume', 'TtsMaker'])
        self.assertIn('"submitted": true', output)

    def test_unit_retries_without_starting_navigation(self):
        text = unit('/home/operator/.local/share/g1-task-voice/releases/test', '/home/operator/config.json')
        self.assertIn('RestartSec=10', text)
        self.assertIn('User=operator', text)
        self.assertNotIn('--ssh-control-path', text)
        for name in ('legacy_auto', 'w_nav', 'x_planner', 'robot_web_controller', 'reboot'):
            self.assertNotIn(name, text)
        with self.assertRaises(ValueError):
            unit('/home/operator/test\nExecStart=bad', '/home/operator/config.json')

    def test_installer_worker_is_fixed_not_ssh_original_command(self):
        compile(REMOTE_INSTALL, 'remote-install.py', 'exec')
        self.assertIn('restrict,command=', REMOTE_INSTALL)
        self.assertNotIn('SSH_ORIGINAL_COMMAND', worker_source())
        self.assertNotIn('exec(', worker_source())


if __name__ == '__main__':
    unittest.main()
