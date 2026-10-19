"""Connection failures preserve project access without creating an arm writer."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import playback
import teaching
import transport
from recording_store import RecordingStore, atomic_json
from test_sessions import TeachingBridge, FakeService


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = {'host':'test.invalid','container':'test','native_path':'/test/native',
                        'interface':'eno1','binary_sha256':hashlib.sha256(b'verified').hexdigest()}

    def test_network_failures_keep_bridge_disconnected_and_cannot_send(self):
        failures = [subprocess.TimeoutExpired('ssh',20), subprocess.CalledProcessError(255,'ssh'),
                    OSError('network unavailable')]
        for index, failure in enumerate(failures):
            folder = self.root / str(index);folder.mkdir()
            with patch.object(transport.subprocess,'check_output',side_effect=failure), \
                 patch.object(transport.subprocess,'Popen') as launch:
                bridge = transport.Bridge(self.runtime,'fake',folder)
                self.assertIsNone(bridge.process)
                with self.assertRaisesRegex(RuntimeError,'连接或程序校验失败'):bridge.snapshot()
                with self.assertRaises(RuntimeError):bridge.send([0.]*17,0.,0.)
                self.assertFalse(bridge.handback_confirmed())
                launch.assert_not_called()
                bridge.close();bridge.close()
                self.assertTrue(bridge.events.closed)

    def test_checksum_failure_never_launches_native_process(self):
        with patch.object(transport.subprocess,'check_output',return_value=b'wrong'), \
             patch.object(transport.subprocess,'Popen') as launch:
            bridge = transport.Bridge(self.runtime,'fake',self.root)
            with self.assertRaisesRegex(RuntimeError,'native_checksum_mismatch'):bridge.snapshot()
            launch.assert_not_called();bridge.close()

    def test_recording_library_survives_failure_and_new_retries_readonly(self):
        store = RecordingStore(self.root/'recordings')
        with patch.object(teaching,'ROOT',self.root), \
             patch.object(teaching,'BuiltinArmService',FakeService), \
             patch.object(transport.subprocess,'check_output',side_effect=subprocess.TimeoutExpired('ssh',20)), \
             patch.object(transport.subprocess,'Popen') as launch:
            console = teaching.TeachingConsole(self.runtime,'fake',store)
            state = console.state()
            self.assertFalse(state['connected']);self.assertEqual(state['sent'],0)
            self.assertIn('连接或程序校验失败',state['transport_error'])
            with self.assertRaises(RuntimeError):console.action('begin',{'onsite':True,'name':'test'})
            self.assertEqual(store.catalog(),[]);launch.assert_not_called()
            previous = console.session
            with patch.object(teaching,'Bridge',TeachingBridge):console.new()
            self.assertTrue(previous.bridge.events.closed)
            self.assertTrue(console.state()['connected']);self.assertEqual(console.state()['sent'],0)
            console.session.close()

    def test_continuous_preview_survives_failure_and_load_retries_readonly(self):
        store = RecordingStore(self.root/'recordings');folder=store.create()
        atomic_json(folder/'metadata.json',{'saved':True,'phase':'ended','sampling_target_hz':50})
        atomic_json(folder/'waypoints.json',[])
        samples=[{'phase':'teaching','t_s':t,'q_upper':[0.]*17} for t in (4.,5.)]
        (folder/'samples.jsonl').write_text(''.join(json.dumps(s)+'\n' for s in samples),encoding='utf-8')
        with patch.object(playback,'BuiltinArmService',FakeService), \
             patch.object(transport.subprocess,'check_output',side_effect=subprocess.TimeoutExpired('ssh',20)), \
             patch.object(transport.subprocess,'Popen') as launch:
            console=playback.PlaybackConsole(self.runtime,'fake',store,folder.name,mode='continuous')
            state=console.state()
            self.assertFalse(state['connected']);self.assertEqual(state['continuous']['sample_count'],2)
            self.assertEqual(state['selected_id'],folder.name);self.assertEqual(state['sent'],0)
            with self.assertRaises(RuntimeError):console.action('start',{'onsite':True})
            launch.assert_not_called();previous=console.replay
            with patch.object(playback,'Bridge',TeachingBridge):console.select(folder.name,'continuous')
            self.assertTrue(previous.bridge.events.closed)
            self.assertTrue(console.state()['connected']);self.assertEqual(console.state()['sent'],0)
            console.replay.close()


if __name__=='__main__':unittest.main()
