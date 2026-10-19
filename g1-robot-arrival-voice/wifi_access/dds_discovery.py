"""Bounded SPDP reception; optional socket-scoped membership, no DDS sends."""
import argparse
from contextlib import ExitStack
import ipaddress
import json
import socket
import struct
import time

G1_ADDRESSES = {'192.0.2.10', '192.0.2.8'}
SPDP_GROUP = '239.255.0.1'


def parameters(data, offset, endian):
    result = []
    while offset + 4 <= len(data):
        pid, length = struct.unpack_from(endian + 'HH', data, offset)
        offset += 4
        if pid == 1:
            return result, offset
        if length % 4 or offset + length > len(data):
            raise ValueError('Truncated or unaligned parameter')
        result.append((pid, data[offset:offset + length]))
        offset += length
    raise ValueError('Missing parameter sentinel')


def spdp_peers(payload, source):
    """Extract only UDPv4 metatraffic unicast locators from SPDP DATA."""
    if source not in G1_ADDRESSES or len(payload) < 20 or payload[:4] != b'RTPS':
        return set()
    peers = set()
    offset = 20
    while offset + 4 <= len(payload):
        kind, flags = payload[offset:offset + 2]
        endian = '<' if flags & 1 else '>'
        length = struct.unpack_from(endian + 'H', payload, offset + 2)[0]
        end = offset + 4 + length if length else len(payload)
        if end > len(payload):
            break
        body = payload[offset + 4:end]
        offset = end
        if kind != 0x15 or not flags & 4 or len(body) < 24 or body[8:12] != b'\x00\x01\x00\xc2':
            continue
        inline_offset = 4 + struct.unpack_from(endian + 'H', body, 2)[0]
        if inline_offset < 20 or inline_offset > len(body) - 4:
            continue
        try:
            if flags & 2:
                _, inline_offset = parameters(body, inline_offset, endian)
            encoding = body[inline_offset:inline_offset + 2]
            if encoding not in [b'\x00\x02', b'\x00\x03']:
                continue
            wire_endian = '<' if encoding == b'\x00\x03' else '>'
            entries, _ = parameters(body, inline_offset + 4, wire_endian)
            for pid, value in entries:
                if pid != 0x32 or len(value) != 24:
                    continue
                locator_kind, port = struct.unpack_from(wire_endian + 'iI', value)
                address = str(ipaddress.IPv4Address(value[-4:]))
                if locator_kind == 1 and 0 < port <= 65535 and address == source and value[8:20] == bytes(12):
                    peers.add(address + ':' + str(port))
        except (ValueError, struct.error):
            continue
    return peers


def frame_udp(frame):
    if len(frame) < 14:
        return None
    offset, ethernet_type = 14, frame[12:14]
    for _ in range(2):
        if ethernet_type not in (b'\x81\x00', b'\x88\xa8'):
            break
        if len(frame) < offset + 4:
            return None
        ethernet_type = frame[offset + 2:offset + 4]
        offset += 4
    if ethernet_type != b'\x08\x00' or len(frame) < offset + 20:
        return None
    ip = frame[offset:]
    ihl = (ip[0] & 15) * 4
    total = struct.unpack_from('!H', ip, 2)[0]
    fragment = struct.unpack_from('!H', ip, 6)[0]
    if ip[0] >> 4 != 4 or ihl < 20 or len(ip) < total or total < ihl + 8 or ip[9] != 17 or fragment & 0x3fff:
        return None
    source = socket.inet_ntoa(ip[12:16])
    udp_length = struct.unpack_from('!H', ip, ihl + 4)[0]
    if udp_length < 8 or ihl + udp_length > total:
        return None
    return source, socket.inet_ntoa(ip[16:20]), ip[ihl + 8:ihl + udp_length]


def frame_peers(frame):
    packet = frame_udp(frame)
    return spdp_peers(packet[2], packet[0]) if packet else set()


def join_spdp(stack, interface):
    """Closing this socket releases only our membership; kernel may emit IGMP."""
    membership = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
    request = struct.pack('=4s4si', socket.inet_aton(SPDP_GROUP), bytes(4),
                          socket.if_nametoindex(interface))
    membership.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, request)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interface', choices=['eth0', 'enp132s0'], default='eth0')
    parser.add_argument('--seconds', type=int, choices=range(2, 46), default=10)
    parser.add_argument('--join-spdp', action='store_true',
                        help='Temporarily receive 239.255.0.1 on this interface; kernel may send IGMP')
    args = parser.parse_args(argv)
    peers = set()
    packets = 0
    sources = {address: {'udp_frames': 0, 'rtps_frames': 0, 'locator_frames': 0}
               for address in sorted(G1_ADDRESSES)}
    joined, error = False, None
    try:
        with ExitStack() as stack:
            receiver = stack.enter_context(socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3)))
            receiver.bind((args.interface, 0))
            receiver.settimeout(0.25)
            if args.join_spdp:
                join_spdp(stack, args.interface)
                joined = True
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline:
                try:
                    frame, _ = receiver.recvfrom(8192)
                except socket.timeout:
                    continue
                packets += 1
                packet = frame_udp(frame)
                if not packet or packet[0] not in sources:
                    continue
                source, _, payload = packet
                sources[source]['udp_frames'] += 1
                sources[source]['rtps_frames'] += int(payload.startswith(b'RTPS'))
                found = spdp_peers(payload, source)
                sources[source]['locator_frames'] += int(bool(found))
                peers.update(found)
                if len(peers) > 64:
                    raise RuntimeError('Unexpected number of G1 participants')
    except (OSError, RuntimeError) as exc:
        error = str(exc)
    print(json.dumps({'peers': sorted(peers), 'window_seconds': args.seconds,
                      'received_frames': packets, 'packet_sends': 0,
                      'application_packet_sends': 0,
                      'group_membership_requested': args.join_spdp,
                      'group_joined_during_capture': SPDP_GROUP if joined else None,
                      'kernel_igmp_reports_possible': joined, 'sources': sources,
                      'error': error}))
    return 2 if error else (0 if peers else 1)


if __name__ == '__main__':
    raise SystemExit(main())
