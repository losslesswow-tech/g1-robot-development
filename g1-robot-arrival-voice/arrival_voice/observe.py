"""Read-only ROS1 trace collector for the deployed legacy navigation protocol.

Does not publish, navigate, initialize DDS, or play audio. Raw state 13 is not
converted to a goal-bound success event because its payload has no task identity.
"""
import argparse
import json
import time


def describe_state(raw):
    parts = raw.split("#")
    if len(parts) == 4 and parts[0] in {"10", "11"}:
        return {"type": "target" if parts[0] == "10" else "planning_failed",
                "target": parts[1], "progress": parts[2], "total": parts[3],
                "goal_correlated": False}
    names = {"-1": "idle", "12": "aligning", "13": "arrived_without_goal_id",
             "14": "path_planned", "15": "planning_failed", "16": "relocalization_required",
             "3": "obstacle_replan_wait", "5": "obstacle_replan_failed",
             "7": "deviation_replan_failed", "9": "abnormal_replan_failed"}
    return {"type": names.get(raw, "other"), "goal_correlated": False}


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    import rospy
    from std_msgs.msg import String
    rospy.init_node("g1_arrival_voice_observer", anonymous=True, disable_signals=False)
    subscriptions = []

    def record(msg, topic):
        item = {"topic": topic, "observed_at_unix": time.time(),
                "raw": msg.data, "publisher": msg._connection_header.get("callerid")}
        if topic == "/x_nav/state":
            item["decoded"] = describe_state(msg.data)
        print(json.dumps(item, ensure_ascii=False), flush=True)

    for topic in ("/x_nav/state", "/planner/cmd", "/node_cmd"):
        subscriptions.append(rospy.Subscriber(topic, String, record, callback_args=topic, queue_size=100))
    rospy.spin()


if __name__ == "__main__":
    main()
