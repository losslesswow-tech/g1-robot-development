"""Synthetic controller and HTTP tests. No SSH, DDS, or robot commands."""
import http.cookiejar
import io
import json
import math
from pathlib import Path
import re
import sys
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from backend import JOINTS,LIMITS,MAX_STEP,WINDOW,Rejected,SimulationDriver,SSHDriver,target_error
from server import Workbench,make_server

class PolicyTests(unittest.TestCase):
    def test_anonymous_read_does_not_claim_control(self):
        w=Workbench(SimulationDriver())
        self.assertFalse(w.state(None)['owned_by_this_page'])

    def test_fault_does_not_display_a_finished_session_as_owned(self):
        driver=SimulationDriver();driver.connect();driver.phase='fault';driver.fault='browser_heartbeat_lost'
        w=Workbench(driver);w.owner='page'
        self.assertFalse(w.state('page')['owned_by_this_page'])
        self.assertIsNone(w.owner)

    def test_native_fault_snapshot_survives_later_query_recovery(self):
        # Exercise the real stream parser using an in-memory pipe, no SSH.
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            key=Path(directory)/'test-key';key.touch()
            driver=SSHDriver(key,'/opt/g1-development/arm-debug/releases/'+'0'*16+'/g1_arm_debug')
            snapshot={'fault':'fsm_mode_query_stale','fsm_mode_query_rc':3104,'phase':'acquiring','weight':.01}
            recovered={'type':'state','fault':'fsm_mode_query_stale','fsm_mode_query_rc':0,'phase':'fault','weight':0,'fault_context':snapshot}
            class Process:stdout=io.StringIO(json.dumps({'type':'fault_event','snapshot':snapshot})+'\n'+json.dumps(recovered)+'\n')
            process=Process();driver.process=process
            audit=Path(directory)/'audit.jsonl';Workbench(driver,'ssh',audit_path=audit)
            driver._read(process)
            saved=json.loads(audit.read_text())
            self.assertEqual(saved['event'],'native_fault')
            self.assertEqual(saved['snapshot']['fsm_mode_query_rc'],3104)
            self.assertEqual(driver.current['fsm_mode_query_rc'],0)
            self.assertEqual(driver.current['fault_context'],snapshot)

    def test_speed_observations_are_audited_separately_from_faults(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory,patch('backend.time.monotonic',return_value=100):
            key=Path(directory)/'test-key';key.touch()
            driver=SSHDriver(key,'/opt/g1-development/arm-debug/releases/'+'0'*16+'/g1_arm_debug')
            sample={'type':'state','phase':'holding','fault':'','legs':[{'index':11,'q_rad':.04,'dq_rad_s':-.336218089,'motorstate':0}],
                    'leg_speed_monitor':{'reference_rad_s':.3,'abort_on_speed':False,'exceedance_samples':[0]*11+[1],'peak_abs_dq_rad_s':[0]*11+[.336218089]}}
            class Process:stdout=io.StringIO(json.dumps(sample)+'\n'+json.dumps(sample)+'\n')
            process=Process();driver.process=process
            audit=Path(directory)/'audit.jsonl';Workbench(driver,'ssh',audit_path=audit);driver._read(process)
            rows=[json.loads(x) for x in audit.read_text().splitlines()]
            self.assertEqual(len(rows),1);self.assertEqual(rows[0]['event'],'leg_speed_observation')
            self.assertEqual(driver.current['phase'],'holding');self.assertFalse(driver.current['fault'])
            self.assertFalse(rows[0]['snapshot']['leg_speed_monitor']['abort_on_speed'])

    def test_native_and_browser_model_limits_agree(self):
        header=(ROOT/'native/safety.hpp').read_text()
        for name,key in (('lower','lower_rad'),('upper','upper_rad')):
            values=[float(x) for x in re.search(r'\b'+name+r'=\{([^}]+)\}',header).group(1).split(',')]
            self.assertEqual(values,[j[key] for j in JOINTS])
        for name,value in (('max_step',MAX_STEP),('session_window',WINDOW)):
            degrees=float(re.search(r'\b'+name+r'=(\d+)\*pi/180',header).group(1))
            self.assertAlmostEqual(math.radians(degrees),value)
        self.assertIn('left_hold_window=3*pi/180',header)

    def test_rejects_nonfinite_wrong_axis_full_range_and_step_bypass(self):
        for i,value in ((15,0),(29,0),(True,0),(22,float('nan')),(22,float('inf')),(22,True),(22,-4),(22,math.radians(20.1))):
            with self.assertRaises(Rejected):target_error(i,value,0,0)
        target_error(22,math.radians(20),0,0)
        for sign in (-1,1):
            target_error(22,sign*math.radians(50),sign*math.radians(30),0)
            with self.assertRaisesRegex(Rejected,'session_exceeds_50'):target_error(22,sign*math.radians(50.1),sign*math.radians(50),0)
        with self.assertRaisesRegex(Rejected,'model_joint_limit'):
            target_error(26,JOINTS[4]['upper_rad']+.01,JOINTS[4]['upper_rad'],JOINTS[4]['upper_rad'])

    def test_single_joint_commands_do_not_change_the_other_thirteen_targets(self):
        with patch('backend.time.monotonic',return_value=100) as clock:
            d=SimulationDriver();d.connect();d.command('ACQUIRE');clock.return_value=100.2;d.state()
            before=d.target[:];d.command('SET',22,before[7]+math.radians(1))
            self.assertEqual(d.target[:7]+d.target[8:],before[:7]+before[8:])
            with self.assertRaisesRegex(Rejected,'busy'):d.command('SET',23,before[8]+.01)

    def test_cumulative_window_survives_multiple_legal_steps(self):
        with patch('backend.time.monotonic',return_value=100) as clock:
            d=SimulationDriver();d.connect();d.command('ACQUIRE');clock.return_value=100.2;d.state()
            seed=d.seed[7]
            for cumulative in (20,40,50):
                d.command('SET',22,seed+math.radians(cumulative))
                for _ in range(100):
                    clock.return_value+=.2;d.command('PING')
                    if d.phase=='holding':break
                self.assertEqual(d.phase,'holding')
            with self.assertRaisesRegex(Rejected,'session_exceeds_50'):d.command('SET',22,seed+math.radians(51))
            self.assertEqual(d.changed_commands,3)

    def test_twenty_degree_trajectory_keeps_existing_peak_rate(self):
        with patch('backend.time.monotonic',return_value=100) as clock:
            d=SimulationDriver();d.connect();d.command('ACQUIRE');clock.return_value=100.2;d.state()
            start=d.q[7];d.command('SET',22,start+math.radians(20))
            self.assertAlmostEqual(d.duration,7.5)
            previous=d.q[7]
            for _ in range(375):
                clock.return_value+=.02;d.command('PING')
                current=d.state()['joints'][7]['q_rad']
                self.assertLessEqual(abs(current-previous)/.02,math.radians(5)+1e-6)
                previous=current
            self.assertAlmostEqual(d.q[7],start+math.radians(20))

    def test_long_target_rejected_if_session_time_cannot_cover_it(self):
        with patch('backend.time.monotonic',return_value=100) as clock:
            d=SimulationDriver();d.connect();d.command('ACQUIRE')
            for _ in range(11):clock.return_value+=2;d.command('PING')
            self.assertEqual(d.phase,'holding')
            before=d.target[:]
            with self.assertRaisesRegex(Rejected,'insufficient_session_time'):d.command('SET',22,d.q[7]+math.radians(20))
            self.assertEqual(d.target,before);self.assertEqual(d.changed_commands,0)

    def test_elapsed_motion_time_does_not_claim_an_unreached_target(self):
        with patch('backend.time.monotonic',return_value=100) as clock:
            d=SimulationDriver();d.connect();d.command('ACQUIRE');clock.return_value=100.2;d.state()
            d.command('SET',22,d.q[7]+math.radians(1.5));clock.return_value=101.3;d.state()
            self.assertEqual(d.phase,'settling')
            d.q[7]=d.target[7]-math.radians(2);clock.return_value=103
            self.assertEqual(d.state()['phase'],'releasing');self.assertEqual(d.fault,'target_not_reached')

    def test_heartbeat_loss_releases_and_latches_fault_without_retry(self):
        with patch('backend.time.monotonic',return_value=100) as clock:
            d=SimulationDriver();d.connect();d.command('ACQUIRE');clock.return_value=104
            self.assertEqual(d.state()['phase'],'releasing');clock.return_value=104.3
            self.assertEqual(d.state()['phase'],'fault')
            with self.assertRaises(Rejected):d.command('SET',22,d.q[7]+.01)
            self.assertEqual(d.changed_commands,0)

    def test_readonly_server_rejects_acquire(self):
        d=SimulationDriver();d.connect();w=Workbench(d,allow_control=False)
        with self.assertRaisesRegex(Rejected,'not_enabled'):w.action('a','acquire',dict(request_id=uuid.uuid4().hex,onsite=True))

class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.driver=SimulationDriver();self.work=Workbench(self.driver,allow_control=True)
        self.server=make_server(self.work,port=0);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_address[1])
        self.client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.config=json.load(self.client.open(self.url+'/api/config'))

    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join()
    def post(self,action,client=None,config=None,origin=None,**data):
        body=dict(csrf=(config or self.config)['csrf'],request_id=uuid.uuid4().hex,**data)
        request=urllib.request.Request(self.url+'/api/'+action,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Origin':origin or self.url})
        return json.load((client or self.client).open(request))

    def test_get_and_connect_are_readonly_and_assets_load(self):
        self.assertEqual(self.config['limits'],LIMITS)
        self.assertEqual(self.driver.changed_commands,0)
        state=self.post('connect');self.assertEqual(state['phase'],'readonly')
        for path in ('/','/app.mjs','/editor.mjs','/diagnostics.mjs','/style.css','/geometry/model.mjs','/geometry/three.module.min.js'):
            with self.client.open(self.url+path) as response:self.assertEqual(response.status,200)
        self.assertEqual(self.driver.changed_commands,0)
        with self.assertRaises(urllib.error.HTTPError):self.client.open(self.url+'/geometry/../backend.py')

    def test_cross_origin_and_forged_csrf_cannot_acquire(self):
        self.post('connect')
        with self.assertRaises(urllib.error.HTTPError) as result:self.post('acquire',origin='http://untrusted.invalid',onsite=True)
        self.assertEqual(result.exception.code,403)
        with self.assertRaises(urllib.error.HTTPError):self.post('acquire',config={'csrf':'forged'},onsite=True)
        self.assertIsNone(self.work.owner);self.assertEqual(self.driver.phase,'readonly')

    def test_only_acquiring_page_can_send_or_release(self):
        self.post('connect');self.post('acquire',onsite=True)
        other=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        conf=json.load(other.open(self.url+'/api/config'))
        for action,data in (('target',dict(index=22,q_rad=self.driver.q[7]+.01)),('release',{}),('disconnect',{})):
            with self.assertRaises(urllib.error.HTTPError):self.post(action,client=other,config=conf,**data)
        self.assertEqual(self.driver.changed_commands,0);self.post('release')

    def test_duplicate_request_is_not_executed_twice(self):
        identifier=uuid.uuid4().hex
        self.work.action('session','connect',{'request_id':identifier})
        with self.assertRaisesRegex(Rejected,'duplicate'):self.work.action('session','connect',{'request_id':identifier})

if __name__=='__main__':unittest.main()
