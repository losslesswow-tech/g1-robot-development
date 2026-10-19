import socket
import struct
import unittest
import io
import json
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch
from dds_discovery import spdp_peers, frame_peers, main


def payload(endian='<', inline=False, address='192.0.2.10', port=34353, writer=b'\x00\x01\x00\xc2'):
    value = struct.pack(endian + 'iI', 1, port) + bytes(12) + socket.inet_aton(address)
    params = struct.pack(endian + 'HH', 0x32, 24) + value + struct.pack(endian + 'HH', 1, 0)
    flags = (1 if endian == '<' else 0) | 4 | (2 if inline else 0)
    body = struct.pack(endian + 'HH', 0, 16) + bytes(4) + writer + bytes(8)
    if inline:
        body += struct.pack(endian + 'HH', 1, 0)
    body += (b'\x00\x03' if endian == '<' else b'\x00\x02') + bytes(2) + params
    return b'RTPS' + bytes(16) + bytes([0x15, flags]) + struct.pack(endian + 'H', len(body)) + body


class DiscoveryTest(unittest.TestCase):
    def test_little_and_big_endian_with_inline_qos(self):
        for endian in ['<', '>']:
            for inline in [False, True]:
                self.assertEqual(spdp_peers(payload(endian, inline), '192.0.2.10'), {'192.0.2.10:34353'})

    def test_rejects_wrong_writer_address_port_and_truncation(self):
        for data in [payload(writer=bytes(4)), payload(address='192.0.2.18'), payload(port=0), payload(port=65536), payload()[:-1]]:
            self.assertEqual(spdp_peers(data, '192.0.2.10'), set())
        self.assertEqual(spdp_peers(payload(), '192.0.2.5'), set())

    def test_ethernet_udp_bounds_and_fragment_rejection(self):
        rtps = payload()
        udp = struct.pack('!HHHH', 35077, 7400, len(rtps) + 8, 0) + rtps
        ip = bytearray(20)
        ip[0], ip[9] = 0x45, 17
        ip[2:4] = struct.pack('!H', len(udp) + 20)
        ip[12:16] = socket.inet_aton('192.0.2.10')
        frame = bytes(12) + b'\x08\x00' + ip + udp
        self.assertEqual(frame_peers(frame), {'192.0.2.10:34353'})
        self.assertEqual(frame_peers(frame[:-1]), set())
        ip[6:8] = b'\x20\x00'
        self.assertEqual(frame_peers(bytes(12) + b'\x08\x00' + ip + udp), set())

    def test_vlan_and_short_ipv4_are_bounded(self):
        rtps = payload()
        udp = struct.pack('!HHHH', 35077, 7400, len(rtps) + 8, 0) + rtps
        ip = bytearray(20)
        ip[0], ip[9] = 0x45, 17
        ip[2:4] = struct.pack('!H', len(udp) + 20)
        ip[12:16] = socket.inet_aton('192.0.2.10')
        frame = bytes(12) + b'\x81\x00\x00\x01\x08\x00' + ip + udp
        self.assertEqual(frame_peers(frame), {'192.0.2.10:34353'})
        for length in range(42):
            self.assertEqual(frame_peers(frame[:length]), set())


class MembershipTest(unittest.TestCase):
    def capture(self, data=None, membership_error=False):
        receiver, membership = MagicMock(), MagicMock()
        for handle in (receiver, membership):
            handle.__enter__.return_value = handle
        if data is None:
            receiver.recvfrom.side_effect = socket.timeout()
        else:
            udp = struct.pack('!HHHH', 35077, 7400, len(data) + 8, 0) + data
            ip = bytearray(20)
            ip[0], ip[9] = 0x45, 17
            ip[2:4] = struct.pack('!H', len(udp) + 20)
            ip[12:16] = socket.inet_aton('192.0.2.10')
            receiver.recvfrom.return_value = (bytes(12) + b'\x08\x00' + ip + udp, None)
        if membership_error:
            membership.setsockopt.side_effect = OSError('membership denied')
        output = io.StringIO()
        with patch('dds_discovery.socket.AF_PACKET', 17, create=True), \
             patch('dds_discovery.socket.if_nametoindex', return_value=3, create=True), \
             patch('dds_discovery.socket.socket', side_effect=[receiver, membership]), \
             patch('dds_discovery.time.monotonic', side_effect=[0, 0, 36]), \
             redirect_stdout(output):
            code = main(['--join-spdp', '--seconds', '35'])
        for handle in (receiver, membership):
            handle.__exit__.assert_called_once()
            handle.send.assert_not_called()
            handle.sendto.assert_not_called()
        return code, json.loads(output.getvalue()), membership

    def test_join_collect_and_close_without_application_sends(self):
        code, report, membership = self.capture(payload())
        self.assertEqual(code, 0)
        self.assertEqual(report['peers'], ['192.0.2.10:34353'])
        self.assertEqual(report['sources']['192.0.2.10']['locator_frames'], 1)
        self.assertEqual(report['application_packet_sends'], 0)
        self.assertTrue(report['kernel_igmp_reports_possible'])
        level, option, request = membership.setsockopt.call_args[0]
        self.assertEqual((level, option), (socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP))
        self.assertEqual(struct.unpack('=4s4si', request), (socket.inet_aton('239.255.0.1'), bytes(4), 3))

    def test_membership_error_closes_sockets_and_reports_before_apply(self):
        code, report, _ = self.capture(membership_error=True)
        self.assertEqual(code, 2)
        self.assertIn('membership denied', report['error'])
        self.assertIsNone(report['group_joined_during_capture'])
        self.assertEqual(report['peers'], [])

    def test_empty_window_reports_counts_without_peers(self):
        code, report, _ = self.capture()
        self.assertEqual(code, 1)
        self.assertEqual(report['received_frames'], 0)
        self.assertIsNone(report['error'])

    def test_non_spdp_rtps_is_distinguished_from_absent_udp(self):
        code, report, _ = self.capture(b'RTPS' + bytes(16))
        self.assertEqual(code, 1)
        self.assertEqual(report['sources']['192.0.2.10'],
                         {'udp_frames': 1, 'rtps_frames': 1, 'locator_frames': 0})


if __name__ == '__main__':
    unittest.main()
