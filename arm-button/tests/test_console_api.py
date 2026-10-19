import http.cookiejar
import io
import json
from pathlib import Path
import sys
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dreamcontrol_backend import ActionService, ConsoleDriver, Rejected
from server import make_server


class Driver:
    def __init__(self):
        self.value = dict(active=False, connected=True, phase='idle')
        self.commands = []
    def state(self):
        return dict(self.value)
    def launch(self):
        pass
    def close(self):
        self.value.update(active=False, connected=False)
    def send(self, value):
        self.commands.append(value)
        if value['action'] == 'start':
            self.value.update(active=True, phase='running')


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.driver = Driver()
        self.service = ActionService(driver=self.driver)
        self.counter = 0
    def data(self, **values):
        self.counter += 1
        return dict(request_id=f'{self.counter:032x}', **values)

    def test_no_start_on_config_and_duplicate_start_is_rejected(self):
        self.service.config()
        self.service.state('page1')
        self.assertFalse(self.driver.commands)
        data = self.data(target_xyz_m=[.5, -.23, .31], stop_at='released')
        self.service.action('page1', 'start', data)
        with self.assertRaisesRegex(Rejected, 'duplicate'):
            self.service.action('page1', 'start', data)
        self.assertEqual(len(self.driver.commands), 1)

    def test_target_cannot_change_and_other_page_cannot_advance_active_run(self):
        self.service.action('page1', 'start', self.data(target_xyz_m=[.5, -.23, .5]))
        with self.assertRaisesRegex(Rejected, 'target_locked'):
            self.service.action('page1', 'configure', self.data(target_xyz_m=[.47, -.2, .25]))
        with self.assertRaisesRegex(Rejected, 'does_not_own'):
            self.service.action('page2', 'next', self.data(node='raised'))
        self.assertEqual(len(self.driver.commands), 1)

    def test_ssh_backend_needs_explicit_server_control_flag(self):
        service = ActionService(backend='ssh', driver=self.driver)
        with self.assertRaisesRegex(Rejected, 'control_not_enabled'):
            service.action('page1', 'start', self.data(target_xyz_m=[.5, -.23, .31]))
        self.assertFalse(self.driver.commands)

    def test_delayed_idle_status_cannot_clear_pending_start_ownership(self):
        observed = []
        driver = ConsoleDriver(audit=lambda event, **data: observed.append(driver.state()) if event == 'worker_event' else None)
        driver.pending_start = True
        driver.value = dict(active=True, phase='queued')
        class Process:
            stdout = io.StringIO(json.dumps(dict(type='status', active=False, phase='idle')) + '\n'
                                 + json.dumps(dict(type='status', active=True, phase='starting')) + '\n')
            def wait(self):
                return 0
        driver.read(Process())
        self.assertTrue(observed[0]['active'])
        self.assertEqual(observed[0]['phase'], 'queued')
        self.assertEqual(observed[1]['phase'], 'starting')


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.driver = Driver()
        self.service = ActionService(driver=self.driver)
        self.server = make_server(self.service, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.config = json.load(self.client.open(self.base + '/api/dream/config'))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def post(self, action, data, origin=None):
        request = urllib.request.Request(self.base + '/api/dream/' + action,
            json.dumps(data).encode(), headers={'Content-Type': 'application/json', 'Origin': origin or self.base})
        return self.client.open(request)

    def test_origin_csrf_and_coordinate_checks_block_actuation(self):
        data = dict(csrf=self.config['csrf'], page_id='1' * 32, request_id='2' * 32, target_xyz_m=[.5, -.23, .31])
        for changed, origin, expected in [({}, 'http://foreign.example', 403), ({'csrf':'wrong'}, None, 403),
                                           ({'target_xyz_m':[.6, -.23, .31]}, None, 409)]:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.post('start', dict(data, **changed), origin)
            self.assertEqual(caught.exception.code, expected)
        self.assertFalse(self.driver.commands)

    def test_get_and_preset_save_send_no_commands(self):
        self.client.open(self.base + '/')
        self.client.open(self.base + '/api/dream/state?page_id=' + '1' * 32)
        result = self.post('configure', dict(csrf=self.config['csrf'], page_id='1'*32, request_id='3'*32, target_xyz_m=[.47,-.2,.25]))
        self.assertEqual(result.status, 202)
        self.assertFalse(self.driver.commands)


if __name__ == '__main__':
    unittest.main()
