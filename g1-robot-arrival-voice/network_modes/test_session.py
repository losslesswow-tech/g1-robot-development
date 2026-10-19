import json
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import session


def snapshot():
    return {'addresses':[{'ifname':'enp132s0','addr_info':[{'local':'192.0.2.6'}]},
                         {'ifname':'wlp131s0f0','addr_info':[{'local':'192.0.2.5'}]}],
            'routes':[{'dst':'192.0.2.7/24','dev':'enp132s0'}], 'bridges':[]}


class SessionTest(unittest.TestCase):
    def test_failed_discovery_saves_report_before_any_network_apply(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(Path,'home',return_value=Path(temp)):
            instance=session.Session('wifi',False)
            report={'peers':[], 'received_frames':294, 'packet_sends':0}
            with patch.object(session,'execute'), \
                 patch.object(instance,'ssh',side_effect=[subprocess.CalledProcessError(1,'ssh'),json.dumps(report)]) as ssh:
                with self.assertRaisesRegex(RuntimeError,'before network apply'):
                    instance.prepare_wifi()
            self.assertEqual(json.loads((instance.directory/'discovery.json').read_text()),report)
            self.assertFalse(instance.host_active or instance.robot_active)
            self.assertFalse(any('--apply' in call.args[0] for call in ssh.call_args_list))

    def test_wired_conflicts_fail_without_installing_routes_or_aliases(self):
        state=snapshot(); session.wired_ready(state)
        state['addresses'][1]['addr_info'].append({'local':'192.0.2.6'})
        with self.assertRaises(ValueError): session.wired_ready(state)
        state=snapshot(); state['routes'].append({'dst':'192.0.2.10/32'})
        with self.assertRaises(ValueError): session.wired_ready(state)
        state=snapshot(); state['addresses'][0]['addr_info']=[]
        with self.assertRaises(ValueError): session.wired_ready(state)

    def test_legacy_bridge_refused_without_stop_restart_or_network_mutation(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(Path,'home',return_value=Path(temp)):
            instance=session.Session('wired',True)
            state=snapshot(); state['bridges']=[{'pid':123}]
            with patch.object(session.network,'inventory',return_value=state), patch.object(session,'execute') as execute:
                with self.assertRaises(ValueError): instance.start()
                execute.assert_not_called()

    def test_stop_bridge_precedes_host_and_robot_rollback(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(Path,'home',return_value=Path(temp)):
            instance=session.Session('wifi',True)
            instance.child=unittest.mock.Mock(); instance.host_active=instance.robot_active=True
            events=[]
            instance.child.wait.side_effect=lambda **kwargs: events.append('bridge_exit')
            with patch.object(instance,'container_file',side_effect=lambda *a,**kw:events.append('bridge_stop')), \
                 patch.object(session,'execute',side_effect=lambda *a,**kw:events.append('host_rollback')), \
                 patch.object(Path,'read_text',return_value=json.dumps({'phase':'rolled_back','remaining':[]})), \
                 patch.object(instance,'ssh',side_effect=lambda cmd,**kw: (events.append('robot_rollback') or json.dumps({'phase':'rolled_back','remaining':[]}))):
                self.assertTrue(instance.close())
            self.assertEqual(events[:3],['bridge_stop','bridge_exit','host_rollback'])
            self.assertIn('robot_rollback',events[3:])

    def test_unknown_robot_rollback_is_reported_as_failure(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(Path,'home',return_value=Path(temp)):
            instance=session.Session('wifi',False); instance.robot_active=True
            with patch.object(instance,'ssh',side_effect=TimeoutError('lost wireless link')):
                self.assertFalse(instance.close())


if __name__=='__main__': unittest.main()
