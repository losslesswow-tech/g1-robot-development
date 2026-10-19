"""Bounded, read-only ROS trace for calibrating legacy arrival association."""
import argparse
import json
import threading
import time
from pathlib import Path


def header_data(header):
    return {"seq": header.seq, "stamp": [header.stamp.secs, header.stamp.nsecs],
            "frame_id": header.frame_id}


def pose_data(pose):
    return {"position": [pose.position.x, pose.position.y, pose.position.z],
            "orientation": [pose.orientation.x, pose.orientation.y, pose.orientation.z, pose.orientation.w]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--seconds", type=int, default=1800)
    args = parser.parse_args()
    if not 0 < args.seconds <= 3600:
        parser.error("seconds must be between 1 and 3600")
    import rospy
    from nav_msgs.msg import Odometry, Path as ROSPath
    from std_msgs.msg import String
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    handle = output.open("a", encoding="utf-8")
    started = time.monotonic()
    rospy.init_node("g1_arrival_voice_trace", anonymous=True)
    def write(item):
        item.update(observed_at=time.time(), elapsed=time.monotonic() - started)
        with lock:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
            handle.flush()
    def record(msg, topic):
        connection = msg._connection_header or {}
        item = {"topic": topic, "publisher": connection.get("callerid"),
                "latched": connection.get("latching") == "1"}
        if isinstance(msg, String):
            item["raw"] = msg.data
        elif isinstance(msg, Odometry):
            item.update(header=header_data(msg.header), pose=pose_data(msg.pose.pose))
        else:
            item.update(header=header_data(msg.header), pose_count=len(msg.poses))
            if msg.poses:
                item["end_pose"] = pose_data(msg.poses[-1].pose)
        write(item)
    subscribers = []
    for topic in ("/x_nav/state", "/planner/cmd", "/node_cmd"):
        subscribers.append(rospy.Subscriber(topic, String, record, callback_args=topic, queue_size=100))
    subscribers.append(rospy.Subscriber("/x_nav/planner/end_point_pose", Odometry, record,
                                        callback_args="/x_nav/planner/end_point_pose", queue_size=20))
    subscribers.append(rospy.Subscriber("/x_nav/goal_path", ROSPath, record,
                                        callback_args="/x_nav/goal_path", queue_size=10))
    write({"type": "trace_started", "node": rospy.get_name(), "read_only": True})
    try:
        while not rospy.is_shutdown() and time.monotonic() - started < args.seconds:
            time.sleep(0.1)
    finally:
        for subscriber in subscribers:
            subscriber.unregister()
        write({"type": "trace_stopped"})
        handle.close()


if __name__ == "__main__":
    main()
