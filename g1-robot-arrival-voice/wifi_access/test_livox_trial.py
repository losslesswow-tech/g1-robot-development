import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import livox_trial as trial


class TrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.driver = self.root / 'driver'
        self.driver.write_bytes(b'original')
        (self.root / 'build').mkdir()
        self.candidate = self.root / 'build/livox_ros_driver2_unicast'
        self.candidate.write_bytes(b'candidate')
        self.manifest = {'original_sha256': trial.digest(self.driver),
                         'candidate_sha256': trial.digest(self.candidate)}

    def test_manifest_rejects_changed_binary(self):
        trial.validate_manifest(self.manifest, self.driver, self.root)
        self.driver.write_bytes(b'changed by operator')
        with self.assertRaises(ValueError):
            trial.validate_manifest(self.manifest, self.driver, self.root)

    def test_manifest_rejects_symlink(self):
        link = self.root / 'symlink'
        link.symlink_to(self.driver)
        with self.assertRaises(ValueError):
            trial.validate_manifest(self.manifest, link, self.root)

    def test_config_does_not_admit_other_lidar(self):
        config = {'lidar_configs': [{'ip': '192.0.2.14'}],
                  'MID360': {'host_net_info': [{'host_ip': '192.0.2.6'}]}}
        trial.config_check(config)
        config['lidar_configs'].append({'ip': '192.0.2.22'})
        with self.assertRaises(ValueError):
            trial.config_check(config)

    def state(self):
        backup = self.root / 'backup'
        backup.write_bytes(b'original')
        state = dict(self.manifest, backup=str(backup), restored=False)
        (self.root / 'state.json').write_text(json.dumps(state))
        return state

    def test_rollback_restores_before_restarting_sensor_only(self):
        self.state()
        self.driver.write_bytes(b'candidate')
        sequence = [[{'pid': 1, 'sha256': self.manifest['candidate_sha256']}], [], [], [],
                    [{'pid': 2, 'sha256': self.manifest['original_sha256']}]]
        calls = []
        def api(endpoint, body):
            self.assertEqual(self.driver.read_bytes(), b'original')
            calls.append((endpoint, body))
        with patch.object(trial, 'DRIVER', self.driver), patch.object(trial, 'api', api), \
                patch.object(trial, 'sensor_processes', side_effect=sequence):
            result = trial.rollback(self.root)
        self.assertEqual(result['phase'], 'sensor_rolled_back')
        self.assertEqual(calls, [('system/stop_node', {'name': 'sensor_node'}),
                                 ('system/start_node', {'name': 'sensor_node'})])

    def test_rollback_refuses_operator_change(self):
        state = self.state()
        self.driver.write_bytes(b'operator change')
        with patch.object(trial, 'DRIVER', self.driver), patch.object(trial, 'api') as api:
            with self.assertRaises(ValueError):
                trial.restore_executable(self.root, state)
            api.assert_not_called()

    def test_finished_watchdog_does_not_restart_sensor(self):
        state = self.state()
        state['restored'] = True
        (self.root / 'state.json').write_text(json.dumps(state))
        with patch.object(trial, 'DRIVER', self.driver), patch.object(trial, 'api') as api:
            self.assertEqual(trial.rollback(self.root)['phase'], 'sensor_already_rolled_back')
            api.assert_not_called()

    def test_signalling_refuses_reused_or_changed_process(self):
        entry = {'pid': 123, 'start_ticks': 'old', 'sha256': 'candidate'}
        class Libc:
            def __init__(self):
                from unittest.mock import Mock
                self.syscall = Mock(return_value=99)
        libc = Libc()
        changed = dict(entry, start_ticks='new')
        with patch.object(trial.platform, 'machine', return_value='x86_64'), \
                patch.object(trial.ctypes, 'CDLL', return_value=libc), \
                patch.object(trial, 'sensor_processes', return_value=[changed]), \
                patch.object(trial.os, 'close') as close:
            with self.assertRaises(ValueError):
                trial.signal_candidate(entry, 'candidate', 15)
            self.assertEqual(libc.syscall.call_count, 1)
            close.assert_called_once_with(99)

    def test_signalling_refuses_other_executable_hash(self):
        entry = {'pid': 123, 'start_ticks': 'same', 'sha256': 'original'}
        class Libc:
            def __init__(self):
                from unittest.mock import Mock
                self.syscall = Mock(return_value=99)
        libc = Libc()
        with patch.object(trial.platform, 'machine', return_value='x86_64'), \
                patch.object(trial.ctypes, 'CDLL', return_value=libc), \
                patch.object(trial, 'sensor_processes', return_value=[entry]), \
                patch.object(trial.os, 'close'):
            with self.assertRaises(ValueError):
                trial.signal_candidate(entry, 'candidate', 15)
            self.assertEqual(libc.syscall.call_count, 1)


if __name__ == '__main__':
    unittest.main()
