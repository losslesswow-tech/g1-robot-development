"""Offline tests only: no connection to any hand or navigation host."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("workbench", ROOT / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class FakeDriver:
    def __init__(self):
        self.reads, self.writes = [], []
        self.actual = [500] * 6
        self.result = None
        self.error = None

    def read(self, side):
        self.reads.append(side)
        return {"actual": self.actual[:], "force_raw": [0]*6}

    def move(self, side, values):
        self.writes.append((side, values[:]))
        if self.error:
            raise self.error
        return self.result if self.result is not None else values[:]


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.mapping = server.load_mapping(ROOT / "calibration.example.json")
        self.driver = FakeDriver()
        self.now = 100

    def bench(self, mode="live", verified=True):
        mapping = copy.deepcopy(self.mapping)
        if verified:
            for hand in mapping["hands"].values():
                hand.update(verified=True, model_axes_verified=True)
                for channel in hand["channels"]:
                    channel["evidence"] = "offline test fixture, not a field calibration"
        return server.Workbench(mode, mapping, self.driver, clock=lambda:self.now)

    def armed(self):
        bench = self.bench()
        lease = bench.arm("R", "test-client")
        payload = {"lease": lease["lease"], "client": "test-client", "side": "R",
                   "seq": 1, "values": [501, 502, 503, 504, 505, 506]}
        return bench, payload

    def test_default_simulation_never_reads_or_writes_hardware(self):
        bench = self.bench("simulation")
        bench.status("R", refresh=True)
        with self.assertRaises(server.Rejected):
            bench.arm("R", "test-client")
        self.assertEqual((self.driver.reads, self.driver.writes), ([], []))

    def test_readonly_rejects_motion(self):
        bench = self.bench("readonly")
        self.assertEqual(bench.status("R", refresh=True)["actual"], [500]*6)
        with self.assertRaises(server.Rejected):
            bench.arm("R", "test-client")
        self.assertEqual(self.driver.writes, [])

    def test_unverified_mapping_rejected_before_network(self):
        with self.assertRaises(server.Rejected):
            self.bench(verified=False).arm("R", "test-client")
        self.assertEqual(self.driver.reads, [])

    def test_arming_adopts_actual_without_write(self):
        bench, payload = self.armed()
        self.assertEqual(bench.cache["R"]["actual"], [500]*6)
        self.assertEqual(self.driver.writes, [])
        with self.assertRaises(server.Rejected):
            bench.arm("L", "other-client")

    def test_exact_six_values_and_no_inversion(self):
        bench, payload = self.armed()
        result = bench.pose(payload)
        self.assertEqual(self.driver.writes, [("R", payload["values"])])
        self.assertTrue(result["reached"])

    def test_wrong_owner_side_duplicate_and_invalid_values_rejected(self):
        bench, payload = self.armed()
        for change in ({"client":"other-client"},{"side":"L"},{"values":[True]*6},
                       {"values":[1001]*6},{"values":[500]*5},{"seq":False}):
            with self.assertRaises(server.Rejected):
                bench.pose(dict(payload, **change))
        bench.pose(payload)
        with self.assertRaises(server.Rejected):
            bench.pose(payload)
        self.assertEqual(len(self.driver.writes), 1)

    def test_expired_session_drops_pending_command(self):
        bench, payload = self.armed()
        self.now += 7
        with self.assertRaises(server.Rejected):
            bench.pose(payload)
        self.assertEqual(self.driver.writes, [])

    def test_disarm_does_not_issue_a_fake_motor_stop(self):
        bench, payload = self.armed()
        bench.disarm(payload)
        with self.assertRaises(server.Rejected):
            bench.pose(payload)
        self.assertEqual(self.driver.writes, [])

    def test_failed_arrival_closes_session_without_retry(self):
        bench, payload = self.armed()
        self.driver.result = [500]*6
        payload["values"] = [1000]*6
        self.assertFalse(bench.pose(payload)["reached"])
        self.assertIsNone(bench.lease)
        self.assertEqual(len(self.driver.writes), 1)

    def test_timeout_is_uncertain_and_never_retried(self):
        bench, payload = self.armed()
        self.driver.error = subprocess.TimeoutExpired("original",22)
        with self.assertRaises(subprocess.TimeoutExpired):
            bench.pose(payload)
        self.assertIsNone(bench.lease)
        self.assertFalse(bench.busy)
        self.assertIn("不确定", bench.fault)
        self.assertEqual(len(self.driver.writes), 1)

    def test_concurrent_pose_is_rejected_and_disarm_drops_followup(self):
        bench, payload = self.armed()
        started, finish = threading.Event(), threading.Event()
        original = self.driver.move
        def delayed(side, values):
            started.set()
            finish.wait(2)
            return original(side, values)
        self.driver.move = delayed
        thread = threading.Thread(target=lambda:bench.pose(payload))
        thread.start()
        self.assertTrue(started.wait(1))
        with self.assertRaises(server.Rejected):
            bench.pose(dict(payload, seq=2))
        bench.disarm(payload)
        finish.set()
        thread.join(2)
        self.assertEqual(len(self.driver.writes), 1)
        self.assertIsNone(bench.lease)

    def test_configured_limits_reject_without_clamping(self):
        bench, payload = self.armed()
        bench.mapping["hands"]["R"]["channels"][0]["points"][1]["raw"] = 400
        payload["values"][0] = 399
        with self.assertRaises(server.Rejected):
            bench.pose(payload)
        self.assertEqual(self.driver.writes, [])

    def test_mapping_validation_rejects_ambiguous_and_unsubstantiated_calibration(self):
        for change in ("duplicate", "nonfinite", "zeroaxis", "verified", "string_boolean"):
            data = copy.deepcopy(self.mapping)
            if change=="duplicate":data["hands"]["R"]["channels"][0]["points"][1]["raw"] = 1000
            if change=="nonfinite":data["hands"]["R"]["channels"][0]["points"][0]["degrees"] = float('nan')
            if change=="zeroaxis":data["model"]["finger_axis"] = [0,0,0]
            if change=="verified":data["hands"]["R"]["verified"] = True
            if change=="string_boolean":data["hands"]["R"]["model_axes_verified"] = "false"
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp)/"mapping.json"
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(server.Rejected):server.load_mapping(path)

    def test_vendor_driver_hash_and_original_speed_targets(self):
        driver = server.RepositoryDriver(ROOT/"vendor/repository/inspire_hand_ctl.py")
        writes = []
        values = [1,6,0,0,14,987]
        with patch.object(driver.module,"write_regs",side_effect=lambda ip,addr,v:writes.append((addr,v[:]))), \
             patch.object(driver.module,"read_regs",return_value=values[:]), \
             patch.object(driver.module.time,"sleep"):
            self.assertEqual(driver.module.set_angles("mock", values),values)
        self.assertEqual(writes, [(1522,[300]*6),(1486,values)])

    def test_modified_driver_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"driver.py";path.write_text("raise Exception('must not run')")
            with self.assertRaises(server.Rejected):server.RepositoryDriver(path)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        mapping=server.load_mapping(ROOT/"calibration.example.json")
        cls.bench=server.Workbench("simulation",mapping,FakeDriver())
        cls.http=server.ThreadingHTTPServer(("127.0.0.1",0),server.make_handler(cls.bench,"test-token"))
        cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True);cls.thread.start()
        cls.base="http://127.0.0.1:%d"%cls.http.server_port

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown();cls.http.server_close();cls.thread.join()

    def request(self,path,token=None,origin=None,data=None):
        headers={}
        if token:headers["X-Hand-Token"]=token
        if origin:headers["Origin"]=origin
        request=urllib.request.Request(self.base+path,headers=headers,data=None if data is None else json.dumps(data).encode())
        return urllib.request.urlopen(request,timeout=3)

    def test_static_js_and_no_vendor_source_exposure(self):
        with self.request('/mapping.mjs') as response:
            self.assertIn('javascript',response.headers['Content-Type'])
        for path in ('/../server.py','/vendor/repository/inspire_hand_ctl.py'):
            with self.assertRaises(urllib.error.HTTPError) as context:self.request(path)
            self.assertEqual(context.exception.code,404)

    def test_token_and_origin_checks(self):
        for token,origin,code in ((None,None,401),('bad',None,401),('test-token','https://example.com',403)):
            with self.assertRaises(urllib.error.HTTPError) as context:self.request('/api/status/R',token,origin)
            self.assertEqual(context.exception.code,code)
        with self.request('/api/status/R','test-token') as response:
            self.assertIsNone(json.load(response)['actual'])

    def test_http_simulation_cannot_enable_hardware(self):
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.request('/api/arm','test-token',data={'client':'test-client','side':'R'})
        self.assertEqual(context.exception.code,400)
        self.assertEqual(self.bench.driver.writes,[])


if __name__ == "__main__":
    unittest.main()
