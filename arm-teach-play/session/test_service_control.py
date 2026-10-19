"""Stop-service regressions use fake SSH results; no robot-side writes."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import playback
import teaching
import service_control as app


def query(status=0, version='1.0.0.2'):
    return {'returncode': 0, 'events': [
        {'event': 'api_version', 'client': '1.0.0.2', 'server': version},
        {'event': 'service_list', 'return_code': 0,
         'services': [{'name': 'g1_arm_example', 'status_raw': status, 'protect_raw': 0}]}]}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.runtime = {'host': 'fake', 'container': 'fake', 'interface': 'eno1',
                        'service_control': {'binary_path': '/pinned/g1_arm_service', 'binary_sha256': 'pinned'}}
        self.service = app.BuiltinArmService(self.runtime, 'fake')
        self.commands = []
        self.evidence = {}
        self.responses = [{'returncode': 0, 'stdout': 'pinned  /pinned/g1_arm_service'}, query(),
                          {'returncode': 0, 'events': [{'event': 'service_stop'}]}, query(1)]
        def run(command, timeout):
            self.commands.append(command)
            return self.responses.pop(0)
        self.service.run = run

    def telemetry(self):
        return dict(connected=True, finished=None, age_s=0., tick_age_s=0., transport_age_s=0.,
                    fsm=501, fsm_mode=0, rpc_age_s=0., api_match=True, weight=0, sent=0)

    def test_only_fixed_service_utility_stop_is_called_once_under_shared_lock(self):
        result = self.service.execute(self.evidence)
        self.assertTrue(result['verified'])
        self.assertEqual(result['switch_calls'], 1)
        stops = [c for c in self.commands if '--stop' in c]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0][-3:], ['/pinned/g1_arm_service', '--stop', 'eno1'])
        self.assertIn('/tmp/g1-right-arm-bounded.lock', stops[0])
        self.assertIn('flock', stops[0])
        self.assertEqual(sum('--query' in c for c in self.commands), 2)
        self.assertTrue(all('arm_sdk' not in ' '.join(c) for c in self.commands))

    def test_already_stopped_does_not_switch_again(self):
        self.responses[1] = query(1)
        result = self.service.execute(self.evidence)
        self.assertTrue(result['verified'])
        self.assertFalse(result['changed'])
        self.assertEqual(result['switch_calls'], 0)
        self.assertFalse(any('--stop' in c for c in self.commands))

    def test_checksum_or_version_mismatch_never_sends_stop(self):
        for failure in ('checksum', 'version'):
            with self.subTest(failure=failure):
                self.setUp()
                if failure == 'checksum':self.responses[0]['stdout'] = 'wrong'
                else:self.responses[1] = query(version='different')
                with self.assertRaises(RuntimeError):self.service.execute(self.evidence)
                self.assertFalse(any('--stop' in c for c in self.commands))

    def test_stop_zero_exit_is_insufficient_without_post_query_confirmation(self):
        self.responses[-1] = query(0)
        with self.assertRaisesRegex(RuntimeError, '仍未确认'):self.service.execute(self.evidence)
        self.assertEqual(sum('--stop' in c for c in self.commands), 1)

    def test_failed_stop_is_not_retried_even_when_post_query_is_stopped(self):
        self.responses[2]['returncode'] = 8
        with self.assertRaisesRegex(RuntimeError, '返回 8'):self.service.execute(self.evidence)
        self.assertEqual(sum('--stop' in c for c in self.commands), 1)

    def test_control_lock_conflict_is_reported_without_retry(self):
        self.responses[2]['returncode'] = 75
        with self.assertRaisesRegex(RuntimeError, '控制锁'):self.service.execute(self.evidence)
        self.assertEqual(len(self.commands), 3)

    def test_unconfirmed_conditions_and_active_or_stale_telemetry_do_not_start_worker(self):
        for onsite, changes in [(False, {}), (True, {'fsm_mode': 1}), (True, {'sent': 1}),
                                (True, {'weight': 1}), (True, {'age_s': .16}), (True, {'rpc_age_s': 3}),
                                (True, {'api_match': False}), (True, {'connected': False})]:
            telemetry = self.telemetry();telemetry.update(changes)
            with self.subTest(changes=changes, onsite=onsite):
                with self.assertRaises(RuntimeError):self.service.request_stop(onsite, telemetry)
                self.assertFalse(self.service.state()['busy'])
                self.assertEqual(self.commands, [])

    def test_worker_reports_failure_and_clears_busy_and_writes_evidence(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(app, 'ROOT', Path(folder)):
            self.service.busy = True
            self.responses[2]['returncode'] = 75
            self.service.worker()
            state = self.service.state()
            self.assertFalse(state['busy'])
            self.assertFalse(state['result']['verified'])
            saved = list(Path(folder).rglob('*.json'))
            self.assertEqual(len(saved), 1)
            self.assertEqual(json.loads(saved[0].read_text(encoding='utf-8'))['motion_commands_sent'], 0)

    def test_console_handlers_reject_service_stop_during_control(self):
        for module, phase, name in ((teaching, 'teaching', 'session'), (playback, 'holding', 'replay')):
            console = (module.TeachingConsole if module is teaching else module.PlaybackConsole).__new__(
                module.TeachingConsole if module is teaching else module.PlaybackConsole)
            console.lock = threading.RLock()
            setattr(console, name, type('ActiveSession', (), {'phase': phase})())
            with self.assertRaises(RuntimeError):console.action('stop-service', {'onsite': True})


if __name__ == '__main__':unittest.main()
