"""Observe fresh ROS messages for ten seconds; no publishers or services."""
import json
import time
import sys
import threading
import rospy
from geometry_msgs.msg import Twist

rospy.init_node("g1_wifi_readonly_sensors", anonymous=True, disable_signals=True)
counts, nonzero = {}, []
unexpected_command = threading.Event()


def sensor(message, topic):
    header = getattr(message, "_connection_header", {}) or {}
    if header.get("latching") == "1":
        return
    key = topic + " from " + str(header.get("callerid"))
    counts[key] = counts.get(key, 0) + 1


def velocity(message):
    values = [message.linear.x, message.linear.y, message.angular.z]
    if any(abs(value) > 0.0001 for value in values):
        nonzero.append(values)
        unexpected_command.set()


subscribers = [rospy.Subscriber(topic, rospy.AnyMsg, sensor, callback_args=topic, queue_size=5)
               for topic in ("/livox/lidar", "/livox/imu", "/sdk/imu")]
subscribers.append(rospy.Subscriber("/cmd_vel", Twist, velocity, queue_size=10))
started = time.monotonic()
unexpected_command.wait(10)
for subscriber in subscribers:
    subscriber.unregister()
print(json.dumps({"window_seconds": round(time.monotonic() - started, 3), "fresh_counts": counts,
                  "nonzero_velocity_messages": len(nonzero), "motion_calls": 0}), flush=True)
rospy.signal_shutdown("read-only Wi-Fi observation completed")
lidar = any(key.startswith('/livox/lidar from ') and value > 0 for key, value in counts.items())
imu = any(key.startswith('/livox/imu from ') and value > 0 for key, value in counts.items())
sys.exit(0 if lidar and imu and not nonzero else 1)
