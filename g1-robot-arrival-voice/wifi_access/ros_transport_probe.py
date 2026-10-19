"""Bounded local ROS publisher handshake and message counts; no actuator topics."""
import concurrent.futures
import json
import socket
import struct
import time
import urllib.parse
import xmlrpc.client

TOPICS = {'/livox/lidar': 'sensor_msgs/PointCloud2', '/livox/imu': 'sensor_msgs/Imu'}
CALLER = '/g1_wifi_local_transport_diagnostic'
MAX_MESSAGE = 16 * 1024 * 1024


class Transport(xmlrpc.client.Transport):
    def make_connection(self, host):
        result = super().make_connection(host)
        result.timeout = 2
        return result


def rpc(uri):
    return xmlrpc.client.ServerProxy(uri, transport=Transport())


def read_exact(stream, count, deadline):
    if not 0 <= count <= MAX_MESSAGE:
        raise ValueError('Unexpected ROS frame size')
    result = bytearray()
    while len(result) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout('observation window ended')
        stream.settimeout(remaining)
        chunk = stream.recv(min(count - len(result), 65536))
        if not chunk:
            raise EOFError('publisher closed the TCP connection')
        result.extend(chunk)
    return bytes(result)


def encode_header(fields):
    data = b''.join(struct.pack('<I', len(value)) + value for value in
                    [str(key).encode() + b'=' + str(value).encode() for key, value in fields.items()])
    return struct.pack('<I', len(data)) + data


def decode_header(data):
    result = {}
    offset = 0
    while offset < len(data):
        if len(data) - offset < 4:
            raise ValueError('Truncated ROS header field length')
        count = struct.unpack_from('<I', data, offset)[0]
        offset += 4
        if count == 0 or count > len(data) - offset:
            raise ValueError('Truncated or empty ROS header field')
        field = data[offset:offset + count].decode('utf-8')
        offset += count
        if '=' not in field or len(result) >= 32:
            raise ValueError('Invalid ROS publisher header')
        key, value = field.split('=', 1)
        result[key] = value
    return result


def observe(topic, node_uri, seconds=8):
    report = {'topic': topic, 'messages': 0, 'message_bytes': 0, 'error': None}
    try:
        parsed = urllib.parse.urlsplit(node_uri)
        if parsed.scheme != 'http' or parsed.hostname not in ('192.0.2.6', '192.0.2.5', '127.0.0.1') or not parsed.port:
            raise ValueError('Publisher is not on the verified navigation host')
        local_uri = 'http://127.0.0.1:' + str(parsed.port) + '/'
        endpoint = rpc(local_uri)
        report['publisher_uri'] = node_uri
        report['bus_before'] = endpoint.getBusInfo(CALLER)
        result = endpoint.requestTopic(CALLER, topic, [['TCPROS']])
        report['request_topic'] = result
        if result[0] != 1 or len(result[2]) != 3 or result[2][0] != 'TCPROS':
            raise ValueError('Publisher rejected TCPROS negotiation')
        host, port = result[2][1:]
        if host not in ('192.0.2.6', '192.0.2.5', '127.0.0.1') or not isinstance(port, int) or not 0 < port <= 65535:
            raise ValueError('Unexpected TCPROS destination')
        report['loopback_transport'] = True
        with socket.create_connection(('127.0.0.1', port), timeout=2) as stream:
            stream.sendall(encode_header({'callerid': CALLER, 'topic': topic, 'type': TOPICS[topic],
                                          'md5sum': '*', 'tcp_nodelay': '1'}))
            deadline = time.monotonic() + seconds
            size = struct.unpack('<I', read_exact(stream, 4, deadline))[0]
            if size > 65536:
                raise ValueError('Unexpected ROS header size')
            header = decode_header(read_exact(stream, size, deadline))
            report['publisher_header'] = {key: value for key, value in header.items()
                                          if key in ('callerid', 'topic', 'type', 'md5sum', 'latching', 'error')}
            if 'error' in header or header.get('type') != TOPICS[topic] or header.get('latching') == '1':
                raise ValueError('Publisher header is an error, unexpected type, or latched')
            try:
                while time.monotonic() < deadline:
                    size = struct.unpack('<I', read_exact(stream, 4, deadline))[0]
                    # Drain only. No point/IMU payloads are written or returned.
                    read_exact(stream, size, deadline)
                    report['messages'] += 1
                    report['message_bytes'] += size
            except socket.timeout:
                report['window_ended'] = True
        report['bus_after'] = endpoint.getBusInfo(CALLER)
    except Exception as error:
        report['error'] = str(error)
    return report


def main():
    report = {'motion_calls': 0, 'network_configuration_changed': False,
              'ros_subscription_handshakes_only': True, 'masters': {}, 'topics': {}}
    master = rpc('http://192.0.2.5:11311')
    for host in ('192.0.2.5', '192.0.2.6'):
        try:
            report['masters'][host] = rpc('http://' + host + ':11311').getUri(CALLER)
        except Exception as error:
            report['masters'][host] = {'error': str(error)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        jobs = {}
        for topic in TOPICS:
            try:
                publishers = master.getSystemState(CALLER)
                if publishers[0] != 1:
                    raise ValueError('ROS graph unavailable')
                names = [names for name, names in publishers[2][0] if name == topic]
                if len(names) != 1 or len(names[0]) != 1:
                    raise ValueError('Expected exactly one LiDAR publisher')
                uri = master.lookupNode(CALLER, names[0][0])
                if uri[0] != 1:
                    raise ValueError('Publisher lookup failed')
                jobs[topic] = pool.submit(observe, topic, uri[2])
            except Exception as error:
                report['topics'][topic] = {'error': str(error)}
        for topic, job in jobs.items():
            report['topics'][topic] = job.result()
    print(json.dumps(report))


if __name__ == '__main__':
    main()
