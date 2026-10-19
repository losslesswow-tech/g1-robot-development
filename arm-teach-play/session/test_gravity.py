"""Gravity profile and mode boundaries, without native transport or motion."""
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import MagicMock,patch

import transport
import playback
import teaching
from generate_gravity_model import extract
from test_sessions import TeachingBridge,FakeService
from recording_store import RecordingStore,atomic_json

class GravityTests(unittest.TestCase):
    def test_official_profile_contains_two_ftp_hands_without_rubber_double_count(self):
        profile=extract()
        for load in profile['loads']:
            self.assertAlmostEqual(load['mass_kg'],.8783)
            self.assertEqual(load['extra_mass_kg'],0)
            self.assertEqual(load['bodies'],30)
        for arm,limits in zip(profile['arms'],profile['ff_limit_nm']):
            self.assertEqual(len(arm),7)
            self.assertTrue(all(0<limit<joint['effort'] for limit,joint in zip(limits,arm)))

    def bridge(self):
        bridge=transport.Bridge.__new__(transport.Bridge)
        bridge.gravity=True;bridge.gravity_model_sha256='verified'
        state={'connected':True,'finished':None,'transport_age_s':0,'age_s':0,'tick_age_s':0,
               'fsm':501,'fsm_mode':0,'rpc_age_s':0,'service_stopped':True,'api_match':True,
               'foreign_age_s':10,'motorstate':[0]*29,'gravity_enabled':True,
               'gravity_model_sha256':'verified','gravity_ready':True}
        bridge.state=lambda:state.copy()
        return bridge,state

    def test_missing_or_mismatched_gravity_profile_blocks_playback(self):
        for change in ({'gravity_enabled':False},{'gravity_model_sha256':'wrong'}):
            bridge,state=self.bridge();state.update(change)
            with self.assertRaisesRegex(RuntimeError,'gravity_profile_unconfirmed'):bridge.snapshot()
        bridge,state=self.bridge();bridge.gravity_model_sha256=None
        with self.assertRaisesRegex(RuntimeError,'gravity_profile_unconfirmed'):bridge.snapshot()

    def test_invalid_imu_or_excessive_torque_blocks_playback(self):
        for error in ('gravity_invalid_imu','gravity_feedforward_limit'):
            bridge,state=self.bridge();state.update(gravity_ready=False,gravity_error=error)
            with self.assertRaisesRegex(RuntimeError,error):bridge.snapshot()

    def test_ready_gravity_does_not_override_stale_or_service_guards(self):
        for change,error in (({'age_s':.2},'stale_state'),({'service_stopped':False},'service_not_ready')):
            bridge,state=self.bridge();state.update(change)
            with self.assertRaisesRegex(RuntimeError,error):bridge.snapshot()
        bridge,state=self.bridge();self.assertEqual(bridge.snapshot(),state)

    def test_teaching_remains_without_gravity_and_playback_explicitly_enables_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);store=RecordingStore(root/'recordings');record=store.create()
            atomic_json(record/'metadata.json',{'saved':True,'phase':'ended'})
            atomic_json(record/'waypoints.json',[{'q_arm':[0.]*14}])
            with patch.object(teaching,'ROOT',root),patch.object(teaching,'Bridge',side_effect=TeachingBridge) as teacher, \
                 patch.object(teaching,'BuiltinArmService',FakeService):
                console=teaching.TeachingConsole({},'fake',store)
                self.assertNotIn('gravity',teacher.call_args.kwargs);console.session.close()
            with patch.object(playback,'Bridge',side_effect=TeachingBridge) as player:
                replay=playback.Replay(record,{},'fake')
                self.assertIs(player.call_args.kwargs['gravity'],True);replay.close()

if __name__=='__main__':unittest.main()
