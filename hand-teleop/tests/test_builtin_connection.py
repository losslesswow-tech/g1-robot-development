"""Automatic connection keeps authentication and motion preparation separate."""
import http.cookiejar
import json
from pathlib import Path
import sys
import threading
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server


class BuiltinConnectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bench = server.Workbench('simulation', server.load_mapping(ROOT/'calibration.example.json'))
        cls.http = server.ThreadingHTTPServer(('127.0.0.1', 0),
                        server.make_handler(cls.bench, 'offline-secret', True))
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:%d' % cls.http.server_port

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join()

    def browser(self):
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def test_opening_page_connects_but_never_prepares_motion(self):
        browser = self.browser()
        with browser.open(self.base+'/', timeout=3) as response:
            self.assertNotIn(b'offline-secret', response.read())
            self.assertIsNone(response.headers.get('Set-Cookie'))
        request = urllib.request.Request(self.base+'/api/connect', data=b'{}',
                                         headers={'X-Hand-Token':'offline-secret'})
        with browser.open(request, timeout=3) as response:
            cookie = response.headers['Set-Cookie']
            self.assertIn('HttpOnly', cookie)
            self.assertIn('SameSite=Strict', cookie)
        with browser.open(self.base+'/api/config', timeout=3) as response:
            data = response.read()
            self.assertTrue(json.loads(data)['builtin_connection'])
            self.assertNotIn(b'offline-secret', data)
        with browser.open(self.base+'/api/status/R', timeout=3) as response:
            self.assertFalse(json.load(response)['armed'])
        self.assertIsNone(self.bench.lease)

    def test_missing_cookie_still_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.browser().open(self.base+'/api/status/R', timeout=3)
        self.assertEqual(error.exception.code, 401)

    def test_cookie_cannot_be_used_from_another_site(self):
        browser = self.browser()
        request = urllib.request.Request(self.base+'/api/connect', data=b'{}',
                                         headers={'X-Hand-Token':'offline-secret'})
        browser.open(request, timeout=3).close()
        for headers in ({'Origin':'https://example.com'}, {'Sec-Fetch-Site':'cross-site'}):
            request = urllib.request.Request(self.base+'/api/status/R', headers=headers)
            with self.assertRaises(urllib.error.HTTPError) as error:
                browser.open(request, timeout=3)
            self.assertEqual(error.exception.code, 403)
        self.assertIsNone(self.bench.lease)

    def test_cross_site_page_load_does_not_issue_cookie(self):
        request = urllib.request.Request(self.base+'/', headers={'Sec-Fetch-Site':'cross-site'})
        with self.browser().open(request, timeout=3) as response:
            self.assertIsNone(response.headers.get('Set-Cookie'))

    def test_visitors_cannot_obtain_credential_without_authentication(self):
        for headers in ({}, {'X-Hand-Token':'wrong-secret'},
                        {'X-Hand-Token':'offline-secret','Origin':'https://example.com'}):
            request = urllib.request.Request(self.base+'/api/connect', data=b'{}', headers=headers)
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.browser().open(request, timeout=3)
            self.assertIn(error.exception.code, (401,403))
            self.assertIsNone(error.exception.headers.get('Set-Cookie'))


if __name__ == '__main__':
    unittest.main()
