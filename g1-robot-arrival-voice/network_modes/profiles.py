"""Offline profile plans and generated DDS XML. No robot/network operations."""
import argparse
import ipaddress
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET


def profile(mode):
    if mode not in ('wired', 'wifi'):
        raise ValueError('Select wired or wifi explicitly; automatic failover is disabled')
    data = json.loads(Path(__file__).with_name(mode + '.json').read_text())
    if data['mode'] != mode or data['default_motion_enabled'] is not False:
        raise ValueError('Profile must default to read-only')
    for name in ('navigation_ip', 'robot_wifi_ip', 'ros_ip', 'radar_ip'):
        ipaddress.IPv4Address(data[name])
    expected={'navigation_ip':'192.0.2.6','robot_wifi_ip':'192.0.2.18',
              'ros_ip':'192.0.2.5','radar_ip':'192.0.2.14',
              'ros_master_uri':'http://192.0.2.5:11311',
              'interface':'enp132s0' if mode=='wired' else 'wlp131s0f0'}
    if any(data.get(key)!=value for key,value in expected.items()):
        raise ValueError('This bundle is pinned to the verified 192.0.2.5 topology')
    if not isinstance(data.get('session_seconds'),int) or not 10<=data['session_seconds']<=1800:
        raise ValueError('Candidate sessions are limited to 10–1800 seconds')
    return data


def peers(report):
    values = report.get('peers', [])
    if report.get('packet_sends') != 0 or not isinstance(values, list) or not 0 < len(values) <= 64:
        raise ValueError('Fresh passive discovery is required')
    for value in values:
        if not isinstance(value, str) or not re.fullmatch(r'192[.]168[.]123[.](161|164):[0-9]{1,5}', value):
            raise ValueError('Unexpected G1 peer address')
        if not 0 < int(value.split(':')[1]) <= 65535:
            raise ValueError('Invalid peer port')
    return sorted(set(values))


def dds_xml(report):
    root = ET.Element('CycloneDDS')
    domain = ET.SubElement(root, 'Domain', Id='0')
    general = ET.SubElement(domain, 'General')
    interfaces = ET.SubElement(general, 'Interfaces')
    ET.SubElement(interfaces, 'NetworkInterface', address='192.0.2.6')
    ET.SubElement(general, 'AllowMulticast').text = 'false'
    ET.SubElement(general, 'DontRoute').text = 'false'
    discovery = ET.SubElement(domain, 'Discovery')
    ET.SubElement(discovery, 'ParticipantIndex').text = 'auto'
    ET.SubElement(discovery, 'MaxAutoParticipantIndex').text = '30'
    peer_root = ET.SubElement(discovery, 'Peers')
    for value in peers(report):
        ET.SubElement(peer_root, 'Peer', Address=value)
    return ET.tostring(root, encoding='unicode')


def plan(mode):
    data = profile(mode)
    data['changes'] = [] if mode == 'wired' else [
        'host Wi-Fi alias .123.10/32 and three /32 routes via .2.83',
        'six host NAT exemptions; existing MASQUERADE preserved',
        'robot return /32 route, one proxy ARP and eight forwarding exceptions',
        'fresh SPDP ports; unicast DDS with original source preserved']
    data['automatic_mode_switch'] = False
    data['automatic_stand_start_damp'] = False
    data['audio_adapter_change'] = False
    return data


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['wired', 'wifi'])
    parser.add_argument('--discovery', type=Path)
    parser.add_argument('--xml-output', type=Path)
    args = parser.parse_args()
    if args.xml_output:
        if args.mode != 'wifi' or not args.discovery:
            parser.error('Wi-Fi XML requires a fresh discovery JSON')
        text = dds_xml(json.loads(args.discovery.read_text()))
        with args.xml_output.open('x', encoding='utf-8') as stream:
            stream.write(text)
    print(json.dumps(plan(args.mode), ensure_ascii=False, indent=2))
