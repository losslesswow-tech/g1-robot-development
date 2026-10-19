import unittest
import xml.etree.ElementTree as ET
import json
from pathlib import Path
from unittest.mock import patch
from profiles import plan, dds_xml, peers


class ProfilesTest(unittest.TestCase):
    def test_profile_cannot_silently_change_network_topology(self):
        data=plan('wired'); data['navigation_ip']='192.0.2.16'
        with patch.object(Path,'read_text',return_value=json.dumps(data)):
            with self.assertRaises(ValueError): plan('wired')

    def test_wired_has_no_network_mutations_and_both_default_readonly(self):
        self.assertEqual(plan('wired')['changes'], [])
        for mode in ('wired', 'wifi'):
            self.assertFalse(plan(mode)['default_motion_enabled'])
            self.assertFalse(plan(mode)['automatic_mode_switch'])
            self.assertEqual(plan(mode)['audio_url'], 'http://127.0.0.1:18768')

    def test_xml_uses_explicit_discovery_ports_without_multicast(self):
        root = ET.fromstring(dds_xml({'peers': ['192.0.2.10:34353'], 'packet_sends': 0}))
        self.assertEqual(root.find('.//Peer').get('Address'), '192.0.2.10:34353')
        self.assertEqual(root.find('.//NetworkInterface').get('address'), '192.0.2.6')
        self.assertEqual(root.find('.//AllowMulticast').text, 'false')

    def test_peer_injection_stale_or_missing_discovery_rejected(self):
        for value in ('192.0.2.18:34353', '192.0.2.10:0', '192.0.2.10:65536', '"/><Peer Address="1.2.3.4'):
            with self.assertRaises(ValueError): peers({'peers': [value], 'packet_sends': 0})
        with self.assertRaises(ValueError): peers({'peers': [], 'packet_sends': 0})
        with self.assertRaises(ValueError): peers({'peers': ['192.0.2.10:34353'], 'packet_sends': 1})
        with self.assertRaises(ValueError): plan('auto')


if __name__ == '__main__':
    unittest.main()
