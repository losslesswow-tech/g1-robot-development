import copy
import unittest
import subprocess
from unittest.mock import patch
import network_probe as probe


def snapshot(role):
    interfaces = {probe.HOST_IF: probe.HOST_WIFI_IP, 'enp132s0': None} if role == 'host' else {
        probe.ROBOT_WIFI: probe.ROBOT_WIFI_IP, probe.ROBOT_ETH: '192.0.2.8'}
    return {'addresses': [{'ifname': name, 'addr_info': [{'local': value, 'prefixlen': 24}] if value else []}
                          for name, value in interfaces.items()],
            'routes': [{'dst': 'default', 'gateway': '192.0.2.23', 'dev': probe.HOST_IF},
                       {'dst': '192.0.2.7/24', 'gateway': '192.0.2.24', 'dev': probe.HOST_IF}],
            'proxy': [], 'forwarding': '1'}


class NetworkPlans(unittest.TestCase):
    def test_host_nat_exemptions_only_cover_alias_to_internal_udp_and_icmp(self):
        rules = probe.host_nat_plan('c' * 32)
        self.assertEqual(len(rules), 6)
        self.assertEqual({a['destination'] for a in rules}, set(probe.DESTINATIONS))
        for action in rules:
            self.assertEqual(action['source'], probe.ALIAS)
            self.assertEqual(action['outgoing'], probe.HOST_IF)
            self.assertIn(action['protocol'], ('udp', 'icmp'))
            command = probe.command(action, 'add')
            self.assertEqual(command[:8], ['iptables', '-w', '2', '-t', 'nat', '-I', 'POSTROUTING', '1'])
            self.assertEqual(command[-2:], ['-j', 'ACCEPT'])
            self.assertNotIn('-i', command)
            self.assertNotIn('MASQUERADE', command)
            self.assertNotIn('-F', command)
            self.assertEqual(command[8:], probe.command(action, 'del')[7:])

    def test_partial_host_nat_failure_restores_alias_and_preserves_existing_masquerade(self):
        import tempfile, uuid, json
        from pathlib import Path
        token = uuid.uuid4().hex
        current = snapshot('host')
        original_routes = copy.deepcopy(current['routes'])
        existing_rule = ('-o', probe.HOST_IF, '-j', 'MASQUERADE')
        table = {existing_rule}
        events = []
        def kernel(arguments, check=True):
            if arguments[0] == 'iptables':
                operation = arguments[5]
                tail = tuple(arguments[8:] if operation == '-I' else arguments[7:])
                if operation == '-C':
                    return subprocess.CompletedProcess(arguments, 0 if tail in table else 1, '', '')
                if operation == '-I':
                    events.append('nat_add')
                    if len(table) > 1:
                        raise subprocess.CalledProcessError(4, arguments, stderr='simulated lock failure')
                    table.add(tail)
                elif operation == '-D': table.remove(tail); events.append('nat_delete')
                else: self.fail('Unexpected firewall mutation')
            elif arguments[:3] == ['ip', 'address', 'add']:
                current['addresses'][0]['addr_info'].append({'local': probe.ALIAS, 'prefixlen': 32})
                events.append('alias_add')
            elif arguments[:3] == ['ip', 'address', 'del']:
                current['addresses'][0]['addr_info'] = [a for a in current['addresses'][0]['addr_info']
                                                       if a['local'] != probe.ALIAS]
                events.append('alias_delete')
            else: self.fail('Network routes must not be restored before NAT exemptions: ' + repr(arguments))
            return subprocess.CompletedProcess(arguments, 0, '', '')
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / ('g1-wifi-probe-' + token)
            with patch.object(probe.os, 'geteuid', return_value=0), \
                    patch.object(probe, 'inventory', side_effect=lambda: copy.deepcopy(current)), \
                    patch.object(probe, 'ensure_idle', return_value={}), \
                    patch.object(probe.shutil, 'which', return_value='/usr/sbin/iptables'), \
                    patch.object(probe, 'run', side_effect=kernel), patch.object(probe.subprocess, 'Popen'):
                with self.assertRaises(subprocess.CalledProcessError):
                    probe.apply('host', directory, 180, preserve_source=True)
                self.assertEqual(table, {existing_rule})
                self.assertEqual(current['routes'], original_routes)
                self.assertEqual(events, ['alias_add', 'nat_add', 'nat_add', 'nat_delete', 'alias_delete'])
                self.assertEqual(json.loads((directory / 'state.json').read_text())['phase'], 'rolled_back')
            (Path('/tmp') / ('g1-wifi-result-' + token + '.json')).unlink()

    def test_wired_address_rejects_nat_setup_before_any_firewall_or_signal(self):
        import tempfile
        from pathlib import Path
        current = snapshot('host')
        current['addresses'][1]['addr_info'].append({'local': probe.ALIAS, 'prefixlen': 24})
        with tempfile.TemporaryDirectory() as root:
            with patch.object(probe.os, 'geteuid', return_value=0), \
                    patch.object(probe, 'inventory', return_value=current), \
                    patch.object(probe, 'run') as run, patch.object(probe, 'signal_bridge') as signal:
                with self.assertRaises(ValueError):
                    probe.apply('host', Path(root) / ('g1-wifi-probe-' + 'd' * 32), 180, preserve_source=True)
                run.assert_not_called(); signal.assert_not_called()

    def test_host_observer_only_binds_wifi_and_reports_udp_receive_counters(self):
        import tempfile, json, uuid, socket, select
        from pathlib import Path
        from unittest.mock import MagicMock
        token, sock = uuid.uuid4().hex, MagicMock()
        sock.recvfrom.return_value = (self.frame(source='192.0.2.14', destination=probe.ALIAS),
                                     (probe.HOST_IF, 0x0800, 0, 1, bytes(6)))
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / ('g1-wifi-probe-' + token)
            directory.mkdir()
            (directory / 'state.json').write_text(json.dumps({'role': 'host', 'phase': 'temporary_active'}))
            with patch.object(probe.os, 'geteuid', return_value=0), \
                    patch.object(socket, 'socket', return_value=sock) as opened, \
                    patch.object(select, 'select', return_value=([sock], [], [])), \
                    patch.object(probe, 'diagnose', return_value={}), \
                    patch.object(probe, 'udp_counters', side_effect=[{'sample': 1}, {'sample': 2}]), \
                    patch.object(probe, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run, \
                    patch.object(probe.time, 'monotonic', side_effect=[0, 0, 0, 0, 2]):
                probe.observe(directory, 1)
                opened.assert_called_once()
                run.assert_called_once_with(['iptables', '-w', '2', '-t', 'nat', '-n', '-v', '-x', '-L', 'POSTROUTING'], check=False)
            sock.bind.assert_called_once_with((probe.HOST_IF, 0))
            sock.send.assert_not_called(); sock.sendto.assert_not_called()
            path = Path('/tmp') / ('g1-wifi-packets-' + token + '.json')
            report = json.loads(path.read_text())
            self.assertEqual(report['role'], 'host')
            self.assertEqual(report['initial_udp_counters'], {'sample': 1})
            self.assertEqual(report['final_udp_counters'], {'sample': 2})
            self.assertIn('latest_nat_counters', report)
            path.unlink()

    def frame(self, source='192.0.2.6', destination='192.0.2.10', fragment=0, arp=False):
        import socket, struct
        sender, recipient = bytes.fromhex('3c6d66a3e2ca'), bytes.fromhex('7e1d7560f589')
        ethernet = recipient + sender + struct.pack('!H', 0x0806 if arp else 0x0800)
        if arp:
            return ethernet + struct.pack('!HHBBH6s4s6s4s', 1, 0x0800, 6, 4, 1,
                                          sender, socket.inet_aton(source), bytes(6), socket.inet_aton(destination))
        ip = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 28, 0, fragment, 64, 17, 0,
                         socket.inet_aton(source), socket.inet_aton(destination))
        return ethernet + ip + struct.pack('!HHHH', 7410, 7412, 8, 0)

    def test_header_parser_rejects_short_frames_bad_ihl_and_unrelated_traffic(self):
        frame = self.frame()
        for value in (b'', frame[:13], frame[:32], self.frame(source='192.0.2.25', destination='192.0.2.26')):
            self.assertIsNone(probe.packet_header(value))
        malformed = bytearray(frame); malformed[14] = 0x41
        self.assertIsNone(probe.packet_header(malformed))

    def test_header_parser_reports_arp_and_udp_metadata_without_payload(self):
        udp = probe.packet_header(self.frame() + b'private-payload-should-never-appear')
        self.assertEqual(udp['source_port'], 7410)
        self.assertEqual(udp['destination_port'], 7412)
        self.assertEqual(udp['ttl'], 64)
        self.assertNotIn('payload', udp)
        arp = probe.packet_header(self.frame(arp=True))
        self.assertEqual(arp['operation'], 1)
        self.assertEqual(arp['source_mac'], '02:00:00:00:00:01')
        self.assertEqual(arp['destination'], '192.0.2.10')

    def test_later_ip_fragment_is_not_misread_as_udp_ports(self):
        header = probe.packet_header(self.frame(fragment=1))
        self.assertNotIn('source_port', header)
        self.assertEqual(header['protocol'], 17)

    def test_inactive_diagnostic_does_not_access_network_commands(self):
        import tempfile, json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            (directory / 'state.json').write_text(json.dumps({'role': 'robot', 'phase': 'rolled_back'}))
            with patch.object(probe, 'run') as run:
                with self.assertRaises(ValueError): probe.diagnose(directory)
                run.assert_not_called()

    def test_passive_observer_binds_only_expected_interfaces_and_never_sends(self):
        import tempfile, json, uuid, socket, select
        from pathlib import Path
        from unittest.mock import MagicMock
        token = uuid.uuid4().hex
        sockets = [MagicMock(), MagicMock()]
        for sock, interface in zip(sockets, (probe.ROBOT_WIFI, probe.ROBOT_ETH)):
            sock.recvfrom.return_value = (self.frame(), (interface, 0x0800, 4, 1, bytes(6)))
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / ('g1-wifi-probe-' + token)
            directory.mkdir()
            (directory / 'state.json').write_text(json.dumps({'role': 'robot', 'phase': 'temporary_active'}))
            with patch.object(probe.os, 'geteuid', return_value=0), \
                    patch.object(socket, 'socket', side_effect=sockets), \
                    patch.object(select, 'select', return_value=(sockets, [], [])), \
                    patch.object(probe, 'diagnose', return_value={}), \
                    patch.object(probe, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')), \
                    patch.object(probe.time, 'monotonic', side_effect=[0, 0, 0, 0, 2]):
                probe.observe(directory, 1)
            for sock, interface in zip(sockets, (probe.ROBOT_WIFI, probe.ROBOT_ETH)):
                sock.bind.assert_called_once_with((interface, 0))
                sock.send.assert_not_called(); sock.sendto.assert_not_called(); sock.sendall.assert_not_called()
                sock.close.assert_called_once()
            report_path = Path('/tmp') / ('g1-wifi-packets-' + token + '.json')
            report = json.loads(report_path.read_text())
            self.assertEqual(report['packet_sends'], 0)
            self.assertEqual(len(report['passive_headers']), 2)
            report_path.unlink()

    def status_response(self, navigation, task='无任务', managed=False, paused=False):
        from io import StringIO
        import json
        return [StringIO(json.dumps({'control_status': navigation, 'task_status': task})),
                StringIO(json.dumps({'managed': managed, 'paused': paused}))]

    def test_localization_required_admitted_without_claiming_idle(self):
        with patch.object(probe.urllib.request, 'urlopen',
                          side_effect=self.status_response('导航模式 请重新设置位姿（重定位）')):
            self.assertEqual(probe.ensure_idle()['state_kind'], 'localization_required')

    def test_alignment_progress_and_unknown_status_still_rejected(self):
        for status in ('导航模式 即将到达目标点 对准中', '导航模式 进度: 1/2',
                       '导航模式 路径规划成功', '未知', '导航模式 空闲 外部导航中'):
            with self.subTest(status=status), patch.object(
                    probe.urllib.request, 'urlopen', side_effect=self.status_response(status)):
                with self.assertRaises(ValueError): probe.ensure_idle()

    def test_relocation_does_not_bypass_active_or_paused_tasks(self):
        for task, managed, paused in [('执行任务中', False, False),
                                      ('无任务', True, False), ('无任务', False, True)]:
            with patch.object(probe.urllib.request, 'urlopen', side_effect=self.status_response(
                    '导航模式 请重新设置位姿（重定位）', task, managed, paused)):
                with self.assertRaises(ValueError): probe.ensure_idle()

    def test_firewall_rules_are_narrow_bidirectional_and_uniquely_owned(self):
        actions = probe.firewall_plan('a' * 32)
        self.assertEqual(len(actions), 8)
        for action in actions:
            self.assertEqual({action['source'], action['destination']} & {probe.ALIAS}, {probe.ALIAS})
            self.assertIn(action['protocol'], ('udp', 'icmp'))
            peer = action['destination'] if action['source'] == probe.ALIAS else action['source']
            self.assertIn(peer, ('192.0.2.14', '192.0.2.10'))
            added = probe.command(action, 'add')
            self.assertEqual(added[:8], ['iptables', '-w', '2', '-t', 'filter', '-I', 'FORWARD', '1'])
            self.assertIn('g1-wifi-probe-' + 'a' * 32, added)
            self.assertNotIn('-P', added)
            self.assertNotIn('-F', added)
            deleted = probe.command(action, 'del')
            self.assertIn('-D', deleted)
            self.assertEqual(added[8:], deleted[7:])

    def test_firewall_inventory_failure_is_not_treated_as_cleanup_success(self):
        action = probe.firewall_plan('b' * 32)[0]
        for code, expected in [(0, True), (1, False)]:
            with patch.object(probe, 'run', return_value=subprocess.CompletedProcess([], code, '', '')):
                self.assertEqual(probe.present(action, {}), expected)
        with patch.object(probe, 'run', return_value=subprocess.CompletedProcess([], 4, '', 'lock failure')):
            with self.assertRaises(ValueError): probe.present(action, {})

    def test_firewall_partial_apply_rolls_back_owned_rule_and_preserves_policy(self):
        import tempfile, uuid, json
        from pathlib import Path
        token = uuid.uuid4().hex
        current = snapshot('robot')
        owned_rules, deleted = set(), []
        original_routes = copy.deepcopy(current['routes'])
        def kernel(arguments, check=True):
            code = 0
            if arguments[0] == 'iptables':
                operation = arguments[5]
                tail = tuple(arguments[8:] if operation == '-I' else arguments[7:])
                if operation == '-C': code = 0 if tail in owned_rules else 1
                elif operation == '-I':
                    if owned_rules:
                        raise subprocess.CalledProcessError(4, arguments, stderr='simulated lock failure')
                    owned_rules.add(tail)
                elif operation == '-D':
                    owned_rules.remove(tail); deleted.append(tail)
                else: self.fail('Unexpected iptables command')
            elif arguments[:3] == ['ip', 'route', 'add']:
                current['routes'].append({'dst': arguments[3], 'gateway': arguments[5], 'dev': arguments[7]})
            elif arguments[:3] == ['ip', 'route', 'del']: current['routes'].pop()
            elif arguments[:3] == ['ip', 'neigh', 'add']:
                current['proxy'].append({'dst': probe.ALIAS, 'dev': probe.ROBOT_ETH})
            elif arguments[:3] == ['ip', 'neigh', 'del']: current['proxy'].clear()
            elif arguments[0] == 'ping': code = 1
            else: self.fail('Unexpected command ' + repr(arguments))
            return subprocess.CompletedProcess(arguments, code, '', '')
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / ('g1-wifi-probe-' + token)
            with patch.object(probe.os, 'geteuid', return_value=0), \
                    patch.object(probe, 'inventory', side_effect=lambda: copy.deepcopy(current)), \
                    patch.object(probe.shutil, 'which', return_value='/usr/sbin/iptables'), \
                    patch.object(probe, 'run', side_effect=kernel), patch.object(probe.subprocess, 'Popen'):
                # Filter inspection is independent of mutation simulation.
                real_run = probe.run
                def with_inspection(arguments, check=True):
                    if '-S' in arguments: return subprocess.CompletedProcess(arguments, 0, '-P FORWARD DROP', '')
                    return real_run(arguments, check=check)
                with patch.object(probe, 'run', side_effect=with_inspection):
                    with self.assertRaises(subprocess.CalledProcessError): probe.apply('robot', directory, 180)
                self.assertFalse(owned_rules)
                self.assertEqual(len(deleted), 1)
                self.assertEqual(current['routes'], original_routes)
                self.assertFalse(current['proxy'])
                self.assertEqual(json.loads((directory / 'state.json').read_text())['phase'], 'rolled_back')
            (Path('/tmp') / ('g1-wifi-result-' + token + '.json')).unlink()

    def test_firewall_check_failure_still_cleans_routes_and_reports_unknown_ownership(self):
        import tempfile, uuid, json
        from pathlib import Path
        token = uuid.uuid4().hex
        current = snapshot('robot')
        route = probe.plan('robot', current)[0]
        firewall = probe.firewall_plan(token)[0]
        current['routes'].append({'dst': route['destination'], 'gateway': route['gateway'],
                                  'dev': route['interface']})
        def kernel(arguments, check=True):
            if arguments[0] == 'iptables':
                return subprocess.CompletedProcess(arguments, 4, '', 'cannot inspect')
            self.assertEqual(arguments[:3], ['ip', 'route', 'del'])
            current['routes'].pop()
            return subprocess.CompletedProcess(arguments, 0, '', '')
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / ('g1-wifi-probe-' + token)
            directory.mkdir()
            (directory / 'state.json').write_text(json.dumps({'actions': [route, firewall]}))
            with patch.object(probe, 'inventory', side_effect=lambda: copy.deepcopy(current)), \
                    patch.object(probe, 'run', side_effect=kernel):
                self.assertFalse(probe.rollback(directory))
                state = json.loads((directory / 'state.json').read_text())
                self.assertEqual(len(state['remaining']), 1)
                self.assertEqual(state['remaining'][0]['kind'], 'firewall')
                self.assertIn('verification_error', state['remaining'][0])
                self.assertFalse(probe.route_exists(current, probe.ALIAS))
            (Path('/tmp') / ('g1-wifi-result-' + token + '.json')).unlink()

    def test_preserves_existing_routes_and_default(self):
        before = snapshot('host')
        original = copy.deepcopy(before)
        actions = probe.plan('host', before)
        self.assertEqual(before, original)
        self.assertEqual(len(actions), 4)
        self.assertEqual([a['destination'] for a in actions if a['kind'] == 'route'],
                         [destination + '/32' for destination in probe.DESTINATIONS])
        for action in actions:
            self.assertNotIn('replace', probe.command(action, 'add'))
            self.assertNotIn('default', probe.command(action, 'add'))

    def test_refuses_to_reuse_alias_on_any_interface(self):
        before = snapshot('host')
        before['addresses'].append({'ifname': 'other', 'addr_info': [{'local': probe.ALIAS}]})
        with self.assertRaises(ValueError): probe.plan('host', before)

    def test_refuses_wired_connection(self):
        before = snapshot('host')
        before['addresses'][1]['addr_info'] = [{'local': probe.ALIAS}]
        with self.assertRaises(ValueError): probe.plan('host', before)

    def test_refuses_existing_host_route(self):
        before = snapshot('host')
        before['routes'].append({'dst': '192.0.2.10/32', 'gateway': '192.0.2.24'})
        with self.assertRaises(ValueError): probe.plan('host', before)

    def test_robot_requires_forwarding_and_correct_addresses(self):
        before = snapshot('robot')
        self.assertEqual([a['kind'] for a in probe.plan('robot', before)], ['route', 'proxy'])
        before['forwarding'] = '0'
        with self.assertRaises(ValueError): probe.plan('robot', before)
        before['forwarding'] = '1'
        before['addresses'][0]['addr_info'][0]['local'] = '192.0.2.27'
        with self.assertRaises(ValueError): probe.plan('robot', before)

    def test_existing_proxy_and_return_route_are_never_owned(self):
        before = snapshot('robot')
        before['proxy'] = [{'dst': probe.ALIAS, 'dev': probe.ROBOT_ETH}]
        with self.assertRaises(ValueError): probe.plan('robot', before)
        before['proxy'] = []
        before['routes'].append({'dst': probe.ALIAS})
        with self.assertRaises(ValueError): probe.plan('robot', before)

    def test_rollback_route_match_does_not_match_changed_gateway_or_source(self):
        before = snapshot('host')
        action = probe.plan('host', before)[1]
        before['routes'].append({'dst': action['destination'], 'gateway': action['gateway'],
                                 'dev': action['interface'], 'prefsrc': action['source']})
        self.assertTrue(probe.present(action, before))
        before['routes'][-1]['gateway'] = '192.0.2.24'
        self.assertFalse(probe.present(action, before))
        before['routes'][-1]['gateway'] = action['gateway']
        before['routes'][-1]['prefsrc'] = probe.HOST_WIFI_IP
        self.assertFalse(probe.present(action, before))

    def test_bad_run_identity_cannot_choose_another_rollback_directory(self):
        for token in ('../etc', '/etc', 'abc', None):
            with self.assertRaises((ValueError, TypeError)): probe.private_state(token)

    def test_bridge_pause_precedes_network_changes(self):
        before = snapshot('host')
        before['bridges'] = [{'pid': 123, 'start_ticks': '456', 'state': 'S'}]
        actions = probe.plan('host', before)
        self.assertEqual(actions[0]['kind'], 'process')
        self.assertEqual(actions[1]['kind'], 'address')
        before['bridges'][0]['state'] = 'T'
        with self.assertRaises(ValueError): probe.plan('host', before)
        before['bridges'][0]['state'] = 'S'
        before['bridges'].append({'pid': 124, 'start_ticks': '457', 'state': 'S'})
        with self.assertRaises(ValueError): probe.plan('host', before)

    def test_unsafe_robot_state_stops_before_network_mutation(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            with patch.object(probe.os, 'geteuid', return_value=0), \
                    patch.object(probe, 'inventory', return_value=snapshot('host')), \
                    patch.object(probe, 'ensure_idle', side_effect=ValueError('active task')), \
                    patch.object(probe, 'run') as run:
                with self.assertRaises(ValueError): probe.apply('host', Path(root) / 'state', 180)
                run.assert_not_called()
                self.assertFalse((Path(root) / 'state').exists())

    def test_partial_apply_failure_removes_only_successful_owned_resources(self):
        import tempfile, uuid, json
        from pathlib import Path
        current = snapshot('host')
        original_routes = copy.deepcopy(current['routes'])
        token = uuid.uuid4().hex
        def kernel(arguments, check=True):
            if arguments[:3] == ['ip', 'address', 'add']:
                current['addresses'][0]['addr_info'].append({'local': probe.ALIAS, 'prefixlen': 32})
            elif arguments[:3] == ['ip', 'route', 'add']:
                raise subprocess.CalledProcessError(2, arguments, stderr='simulated rejected add')
            elif arguments[:3] == ['ip', 'address', 'del']:
                current['addresses'][0]['addr_info'] = [a for a in current['addresses'][0]['addr_info'] if a['local'] != probe.ALIAS]
            else:
                self.fail('Unexpected mutation ' + repr(arguments))
            return subprocess.CompletedProcess(arguments, 0, '', '')
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / ('g1-wifi-probe-' + token)
            with patch.object(probe.os, 'geteuid', return_value=0), \
                    patch.object(probe, 'inventory', side_effect=lambda: copy.deepcopy(current)), \
                    patch.object(probe, 'ensure_idle', return_value={}), \
                    patch.object(probe, 'run', side_effect=kernel), \
                    patch.object(probe.subprocess, 'Popen'):
                with self.assertRaises(subprocess.CalledProcessError): probe.apply('host', directory, 180)
                state = json.loads((directory / 'state.json').read_text())
                self.assertEqual(state['phase'], 'rolled_back')
                self.assertEqual([a['kind'] for a in state['actions']], ['address'])
                self.assertEqual(current['routes'], original_routes)
                self.assertNotIn(probe.ALIAS, probe.addresses(current, probe.HOST_IF))
            (Path('/tmp') / ('g1-wifi-result-' + token + '.json')).unlink()

    def test_bridge_resumes_only_after_network_cleanup_and_matching_identity(self):
        import tempfile, uuid, json
        from pathlib import Path
        current = snapshot('host')
        process = {'pid': 123, 'start_ticks': '456', 'state': 'T'}
        route = {'kind': 'route', 'destination': '192.0.2.10/32',
                 'gateway': probe.ROBOT_WIFI_IP, 'interface': probe.HOST_IF, 'source': probe.ALIAS}
        current['routes'].append({'dst': route['destination'], 'gateway': route['gateway'],
                                  'dev': route['interface'], 'prefsrc': route['source']})
        events, token = [], uuid.uuid4().hex
        def remove(arguments, check=True):
            events.append('route_deleted')
            current['routes'].pop()
            return subprocess.CompletedProcess(arguments, 0, '', '')
        def resume(action, sig):
            events.append('bridge_resumed')
            process['state'] = 'S'
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / ('g1-wifi-probe-' + token)
            directory.mkdir()
            (directory / 'state.json').write_text(json.dumps({'actions': [dict(process, kind='process'), route]}))
            with patch.object(probe, 'inventory', side_effect=lambda: copy.deepcopy(current)), \
                    patch.object(probe, 'process_identity', side_effect=lambda pid: copy.deepcopy(process)), \
                    patch.object(probe, 'run', side_effect=remove), patch.object(probe, 'signal_bridge', side_effect=resume):
                self.assertTrue(probe.rollback(directory))
                self.assertEqual(events, ['route_deleted', 'bridge_resumed'])
            (Path('/tmp') / ('g1-wifi-result-' + token + '.json')).unlink()

    def test_failed_network_cleanup_keeps_bridge_paused(self):
        import tempfile, uuid, json
        from pathlib import Path
        current = snapshot('host')
        process = {'pid': 123, 'start_ticks': '456', 'state': 'T'}
        route = {'kind': 'route', 'destination': '192.0.2.10/32',
                 'gateway': probe.ROBOT_WIFI_IP, 'interface': probe.HOST_IF, 'source': probe.ALIAS}
        current['routes'].append({'dst': route['destination'], 'gateway': route['gateway'],
                                  'dev': route['interface'], 'prefsrc': route['source']})
        token = uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / ('g1-wifi-probe-' + token)
            directory.mkdir()
            (directory / 'state.json').write_text(json.dumps({'actions': [dict(process, kind='process'), route]}))
            with patch.object(probe, 'inventory', return_value=current), \
                    patch.object(probe, 'process_identity', return_value=process), \
                    patch.object(probe, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'rejected')), \
                    patch.object(probe, 'signal_bridge') as kill:
                self.assertFalse(probe.rollback(directory))
                kill.assert_not_called()
                state = json.loads((directory / 'state.json').read_text())
                self.assertEqual(state['phase'], 'rollback_incomplete')
            (Path('/tmp') / ('g1-wifi-result-' + token + '.json')).unlink()

    def test_pidfd_signal_rechecks_identity_and_never_falls_back_to_pid(self):
        action = {'pid': 123, 'start_ticks': '456'}
        with patch.object(probe.os, 'pidfd_open', return_value=99, create=True) as opened, \
                patch.object(probe.signal, 'pidfd_send_signal', create=True) as send, \
                patch.object(probe.os, 'close') as close, \
                patch.object(probe, 'process_identity', return_value={'pid': 123, 'start_ticks': '789'}):
            with self.assertRaises(ValueError): probe.signal_bridge(action, probe.signal.SIGSTOP)
            opened.assert_called_once_with(123)
            send.assert_not_called()
            close.assert_called_once_with(99)


if __name__ == '__main__': unittest.main()
