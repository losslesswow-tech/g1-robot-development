import io
import struct
import unittest
from unittest.mock import Mock, patch
from ros_transport_probe import decode_header, encode_header, read_exact, observe, MAX_MESSAGE


class TransportTest(unittest.TestCase):
    def test_ros_header_roundtrip_and_error_payload(self):
        data = encode_header({'callerid': '/sensor', 'type': 'sensor_msgs/Imu', 'error': 'example'})
        self.assertEqual(len(data)-4, struct.unpack('<I', data[:4])[0])
        self.assertEqual(decode_header(data[4:])['error'], 'example')

    def test_truncated_and_oversized_headers_fail(self):
        for data in (b'\x01', struct.pack('<I', 10)+b'a', struct.pack('<I', 1)+b'x'):
            with self.assertRaises(ValueError): decode_header(data)

    def test_fragmented_tcp_reads_and_early_close(self):
        stream=Mock(); stream.recv.side_effect=[b'a',b'bc']
        self.assertEqual(read_exact(stream,3,10**12),b'abc')
        stream.recv.side_effect=[b'a',b'']
        with self.assertRaises(EOFError): read_exact(stream,3,10**12)

    def test_oversized_frame_rejected_before_allocating_or_reading(self):
        stream=Mock()
        with self.assertRaises(ValueError): read_exact(stream,MAX_MESSAGE+1,10**12)
        stream.recv.assert_not_called()

    def test_unverified_remote_publisher_rejected_without_socket(self):
        with patch('ros_transport_probe.socket.create_connection') as connect:
            result=observe('/livox/imu','http://192.0.2.10:1234/')
            self.assertIn('verified',result['error'])
            connect.assert_not_called()


if __name__ == '__main__': unittest.main()
