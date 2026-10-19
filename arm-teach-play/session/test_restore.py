"""Restoration sequencing and confirmation without service switches or DDS."""
import math
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import service_control
from transport import Bridge
import playback
import test_replay
from test_service_control import query


class RestoreTests(unittest.TestCase):
    def service(self):
        runtime={'host':'fake','container':'fake','interface':'eno1',
                 'service_control':{'binary_path':'/fixed/service','binary_sha256':'hash'}}
        return service_control.BuiltinArmService(runtime,'fake')

    def bridge(self, **changes):
        finish=dict(returncode=0,reason='RELEASE',final_weight=0,sent=20,zero_weight_frames=10)
        finish.update(changes)
        b=types.SimpleNamespace(finished=finish)
        b.process=types.SimpleNamespace(poll=lambda:finish['returncode'])
        b.handback_confirmed=lambda:Bridge.handback_confirmed(b)
        return b

    def test_zero_weight_alone_does_not_prove_handback(self):
        service=self.service();service.worker=lambda *args:self.fail('worker started without handback')
        for change in ({'final_weight':1},{'zero_weight_frames':9}):
            self.assertFalse(service.restore_after_handback(self.bridge(**change))['verified'])
        b=self.bridge();b.process.poll=lambda:None
        self.assertFalse(service.restore_after_handback(b)['verified'])
        b=self.bridge();b.process.poll=lambda:255
        self.assertFalse(service.restore_after_handback(b)['verified'])

    def test_select_stop_does_not_automatically_restore(self):
        service=self.service();service.worker=lambda *args:self.fail('restore attempted after Select')
        result=service.restore_after_handback(self.bridge(reason='controller_select_stop'))
        self.assertFalse(result['verified']);self.assertTrue(result['skipped'])

    def test_restore_is_one_fixed_call_under_lock_followed_by_running_confirmation(self):
        service=self.service();commands=[]
        replies=[{'returncode':0,'stdout':'hash file'},query(1),
                 {'returncode':0,'events':[{'event':'service_restore'}]},query(0)]
        def run(command, timeout):
            commands.append(command);return replies.pop(0)
        service.run=run
        with tempfile.TemporaryDirectory() as folder,patch.object(service_control,'ROOT',Path(folder)):
            result=service.restore_after_handback(self.bridge())
        self.assertTrue(result['verified']);self.assertEqual(result['operation'],'restore')
        restores=[c for c in commands if '--restore' in c]
        self.assertEqual(len(restores),1)
        self.assertIn('/tmp/g1-right-arm-bounded.lock',restores[0])
        self.assertEqual(restores[0][-3:],['/fixed/service','--restore','eno1'])
        self.assertEqual(commands[-1][-3:],['/fixed/service','--query','eno1'])

    def test_zero_exit_without_running_post_query_is_failure(self):
        service=self.service()
        replies=[{'returncode':0,'stdout':'hash file'},query(1),{'returncode':0,'events':[]},query(1)]
        service.run=lambda *args:replies.pop(0)
        with self.assertRaises(RuntimeError):service.execute({},'restore')

    def test_already_running_does_not_restart_service(self):
        service=self.service();commands=[]
        replies=[{'returncode':0,'stdout':'hash file'},query(0)]
        def run(command, timeout):commands.append(command);return replies.pop(0)
        service.run=run
        result=service.execute({},'restore')
        self.assertTrue(result['verified']);self.assertEqual(result['switch_calls'],0)
        self.assertFalse(any('--restore' in c for c in commands))

    def test_arrival_timeout_still_releases_then_restores_once(self):
        r=test_replay.Tests().fixture();r.bridge=test_replay.FakeBridge(math.radians(8.4))
        calls=[]
        def restore(bridge):
            self.assertTrue(bridge.released)
            self.assertEqual(r.phase,'restoring_service')
            calls.append('restore')
            return {'verified':True,'message':'fake restored'}
        r.service=types.SimpleNamespace(restore_after_handback=restore)
        clock=test_replay.Clock();clock.on_sleep=lambda:setattr(r,'last_heartbeat',clock.now)
        with patch.object(playback,'time',clock),patch.object(playback.threading.Thread,'start',lambda self:None):
            r.next(True);r.run()
        self.assertEqual(calls,['restore'])
        self.assertEqual(r.error,'target_not_reached_no_next_stage')
        self.assertEqual(r.phase,'ended_with_error')

    def test_unconfirmed_playback_release_does_not_restore(self):
        r=test_replay.Tests().fixture()
        r.bridge.handback_confirmed=lambda:False
        r.service=types.SimpleNamespace(restore_after_handback=lambda b:self.fail('unconfirmed handback restored'))
        clock=test_replay.Clock();clock.on_sleep=lambda:setattr(r,'last_heartbeat',clock.now)
        with patch.object(playback,'time',clock),patch.object(playback.threading.Thread,'start',lambda self:None):
            r.next(True);r.run()
        self.assertEqual(r.phase,'release_unconfirmed')

    def test_service_stays_busy_until_retry_callback_has_updated_session(self):
        service=self.service();service.busy=True
        service.execute=lambda *args:{'verified':True,'operation':'restore','message':'fake restored'}
        calls=[]
        def callback(result):
            self.assertTrue(service.state()['busy'])
            self.assertTrue(result['verified']);calls.append('updated')
        with tempfile.TemporaryDirectory() as folder,patch.object(service_control,'ROOT',Path(folder)):
            service.worker('restore',callback)
        self.assertEqual(calls,['updated'])
        self.assertFalse(service.state()['busy'])


if __name__=='__main__':unittest.main()
