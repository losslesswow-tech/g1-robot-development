"""Passive ROS legacy arrival listener, using the existing local audio editor.

No ROS publishers, navigation services, motion clients, or automatic audio retries.
Both explicit CLI opt-ins and the editor's enable switch are required for audio.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import time
import urllib.request
import urllib.error
import xmlrpc.client

from .legacy_gate import LegacyGate
from .legacy_trace import header_data, pose_data


class TimeoutTransport(xmlrpc.client.Transport):
    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = 2
        return connection


def rpc(uri, method, *args):
    proxy = xmlrpc.client.ServerProxy(uri, transport=TimeoutTransport())
    code, message, value = getattr(proxy, method)(*args)
    if code != 1:
        raise RuntimeError(message)
    return value


def request_json(url, payload=None, token=None, timeout=2):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Voice-Token"] = token
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=timeout) as response:
        return json.load(response)


def load_registry(root, map_name):
    """Exact names from the same saved nav_points.txt used by the nav UI."""
    if not isinstance(map_name, str) or not map_name.strip() or map_name in {".", ".."} or any(c in map_name for c in "/\\\0"):
        raise ValueError("current map is unavailable")
    root = Path(root).resolve()
    filename = (root / map_name / "nav_points.txt").resolve()
    if root not in filename.parents:
        raise ValueError("map path escapes map root")
    raw = filename.read_bytes()
    if len(raw) > 1024 * 1024:
        raise ValueError("map registry too large")
    points = []
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 11:
            raise ValueError("unsupported waypoint row")
        values = [float(v) for v in fields[:7]]
        if not all(math.isfinite(v) for v in values) or sum(v*v for v in values[3:]) < 0.25:
            raise ValueError("invalid waypoint pose")
        points.append({"name": fields[-1], "pose": {"position": values[:3], "orientation": values[3:]}})
    if not points or len({p["name"] for p in points}) != len(points):
        raise ValueError("empty or duplicate waypoint names")
    return points, hashlib.sha256(raw).hexdigest()


def read_context(nav_url, editor_url, map_root, caller, master):
    # The existing get_status endpoint blocks for a 1-second CPU sample.
    # Run all metadata reads off the event loop so cancellation is consumed promptly.
    nav = request_json(nav_url + "/api/extra/get_status")
    try:
        task_voice = request_json(nav_url + "/api/task/voice_status")
        task_owner = task_voice.get("run_id") if task_voice.get("managed") else None
        if task_voice.get("managed") and not task_owner:
            raise RuntimeError("task voice ownership unavailable")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        task_owner = None  # Older navigation task modules have no speech integration.
    map_name = nav["map_name"]
    points, revision = load_registry(map_root, map_name)
    graph = rpc(master, "getSystemState", caller)
    publishers = dict(graph[0])
    if not {"/x_planner", "/w_nav"} <= set(publishers.get("/x_nav/state", [])) or \
            "/x_planner" not in publishers.get("/x_nav/goal_path", []):
        raise RuntimeError("required navigation publishers unavailable")
    epochs = tuple(rpc(master, "lookupNode", caller, name) for name in ("/x_planner", "/w_nav"))
    editor = request_json(editor_url + "/api/state")
    with urllib.request.urlopen(editor_url + "/", timeout=2) as response:
        html = response.read(128 * 1024).decode("utf-8")
    token_match = re.search(r'name="voice-token" content="([a-zA-Z0-9_-]+)"', html)
    if not token_match:
        raise RuntimeError("editor token unavailable")
    token = token_match.group(1)
    enabled = bool(editor.get("automatic", {}).get("enabled"))
    configured = {(p["map_name"], p["point_name"]) for p in editor["config"]["points"]}
    config_hash = hashlib.sha256(json.dumps(editor["config"], sort_keys=True).encode()).hexdigest()
    return {"map_name": map_name, "points": points, "token": token, "enabled": enabled,
            "configured": configured, "task_owner": task_owner, "at": time.monotonic(),
            "identity": (map_name, revision, epochs, token, enabled, config_hash, task_owner)}


def submission_reason(state, key, configured, busy):
    if state == "task_managed":
        return "task_managed"
    if state != "armed":
        return "read_only"
    if key not in configured:
        return "unconfigured"
    return "busy" if busy else "claimed"


def submit_standalone(nav_url, editor_url, payload, token, started):
    # Recheck ownership at submission, including tasks that ended after arrival.
    # Both processes use the same host monotonic clock; metadata polling alone
    # can miss a short final J step and otherwise replay its point announcement.
    try:
        owner = request_json(nav_url + "/api/task/voice_status", timeout=2)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        owner = {}
    if owner.get("managed") or any(
            window["start"] <= started <= (window["end"] if window["end"] is not None else time.monotonic())
            for window in owner.get("ownership", [])):
        return {"submitted": False, "task_managed": True}
    return request_json(editor_url + "/api/automatic/play", payload, token, 350)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--map-root", default="/nav_map")
    parser.add_argument("--nav-port", type=int, default=9000)
    parser.add_argument("--editor-port", type=int, default=18768)
    parser.add_argument("--allow-legacy-auto", action="store_true")
    parser.add_argument("--allow-robot-audio", action="store_true")
    args = parser.parse_args()
    if not all(0 < port < 65536 for port in (args.nav_port, args.editor_port)):
        parser.error("invalid local port")
    allowed = args.allow_legacy_auto and args.allow_robot_audio
    import rospy
    from nav_msgs.msg import Path as ROSPath
    from std_msgs.msg import String
    rospy.init_node("g1_arrival_voice_legacy", anonymous=True)
    Path(args.database).resolve().parent.mkdir(parents=True, exist_ok=True)
    Path(args.log).resolve().parent.mkdir(parents=True, exist_ok=True)
    log = open(args.log, "a", encoding="utf-8")
    gate = LegacyGate(args.database)
    records = queue.Queue(maxsize=500)
    overflow = __import__("threading").Event()
    executor = ThreadPoolExecutor(max_workers=2)
    future = None
    context_future = None
    context_at = 0
    pending_id = None
    editor_url = "http://127.0.0.1:{}".format(args.editor_port)
    nav_url = "http://127.0.0.1:{}".format(args.nav_port)
    current_map, token, last_result = "", None, "waiting_for_fresh_navigation"
    state, next_refresh = "starting", 0
    last_state = 0
    last_path = 0
    last_error = None
    configured = set()

    def write(event, **fields):
        log.write(json.dumps(dict(at=time.time(), event=event, **fields), ensure_ascii=False) + "\n")
        log.flush()

    def callback(msg, topic):
        connection = msg._connection_header or {}
        record = {"received": time.monotonic(), "topic": topic,
                  "publisher": connection.get("callerid"), "latched": connection.get("latching") == "1"}
        if isinstance(msg, String):
            record["raw"] = msg.data
        else:
            record.update(header=header_data(msg.header), pose_count=len(msg.poses))
            if msg.poses:
                record["end_pose"] = pose_data(msg.poses[-1].pose)
        try:
            records.put_nowait(record)
        except queue.Full:
            overflow.set()

    subscribers = [rospy.Subscriber(topic, String, callback, callback_args=topic, queue_size=100)
                   for topic in ("/x_nav/state", "/planner/cmd", "/node_cmd")]
    subscribers.append(rospy.Subscriber("/x_nav/goal_path", ROSPath, callback,
                                        callback_args="/x_nav/goal_path", queue_size=20))
    write("listener_started", audio_opt_in=allowed, local_ids_are_not_planner_ids=True)
    try:
        while not rospy.is_shutdown():
            now = time.monotonic()
            if overflow.is_set():
                gate.disconnected()
                while not records.empty():
                    records.get_nowait()
                overflow.clear()
                write("queue_overflow_dropped")
            if future is not None and future.done():
                try:
                    result = future.result()
                    last_result = "task_managed" if result.get("task_managed") else "submitted" if result.get("submitted") else "failed_or_unknown"
                    write("audio_result", attempt_id=pending_id, result=result)
                except Exception as exc:
                    last_result = "failed_or_unknown"
                    write("audio_result_unknown", attempt_id=pending_id, error=str(exc)[:500])
                gate.result(pending_id, last_result)
                future, pending_id = None, None
            if context_future is not None and context_future.done():
                try:
                    context = context_future.result()
                    current_map, token = context["map_name"], context["token"]
                    configured, context_at = context["configured"], context["at"]
                    if time.monotonic() - last_state > 3.5:
                        raise RuntimeError("navigation state stream unavailable")
                    # Changing enable state or editor/ROS epoch discards any in-flight association.
                    gate.set_context(context["identity"], context["points"])
                    state = "task_managed" if context["task_owner"] else "armed" if allowed and context["enabled"] else "read_only"
                    active = gate.active or {}
                    request_json(editor_url + "/api/automatic/status", {
                        "mode": "legacy_conservative", "state": state, "map_name": current_map,
                        "point_name": active.get("point_name") or "", "last_result": last_result}, token)
                    if last_error:
                        write("connections_restored")
                    last_error = None
                except Exception as exc:
                    gate.disconnected()
                    state = "unavailable"
                    error = str(exc)[:500]
                    if error != last_error:
                        write("connection_unavailable", error=error)
                    last_error = error
                    # Keep the editor status explicit when ROS/map metadata is unavailable.
                    if token:
                        try:
                            request_json(editor_url + "/api/automatic/status", {
                                "mode": "legacy_conservative", "state": state,
                                "map_name": current_map, "last_result": error}, token)
                        except Exception:
                            pass
                context_future = None
                next_refresh = time.monotonic() + 0.1
            if context_future is None and now >= next_refresh:
                context_future = executor.submit(read_context, nav_url, editor_url, args.map_root,
                                                 rospy.get_name(), os.environ.get("ROS_MASTER_URI", "http://localhost:11311"))
            if gate.context and now - context_at > 3.5:
                gate.disconnected()
                state = "unavailable"
                write("metadata_expired")
            count = 0
            while count < 500:
                try:
                    record = records.get_nowait()
                except queue.Empty:
                    break
                count += 1
                if not record["latched"]:
                    if record["topic"] == "/x_nav/state" and record["publisher"] == "/w_nav":
                        last_state = record["received"]
                    if record["topic"] == "/x_nav/goal_path" and record["publisher"] == "/x_planner":
                        last_path = record["received"]
                outcome = gate.handle(record)
                if outcome not in {"ignored_state", "no_active_attempt", "navigation_evidence", "same_goal_path",
                                   "ignored_topic", "no_context", "foreign_publisher", "goal_outside_start_window"}:
                    active = gate.active or {}
                    write("navigation_event", outcome=outcome, topic=record["topic"],
                          publisher=record["publisher"], raw=record.get("raw"),
                          attempt_id=active.get("id"), point_name=active.get("point_name"))
            candidate = gate.ready()
            if candidate and context_at >= candidate["ready_at"] - gate.settle_seconds:
                if time.monotonic() - last_path > 3.5 or time.monotonic() - last_state > 3.5:
                    gate.invalidate("stale_navigation_stream")
                    write("arrival_dropped_stale_stream")
                else:
                    identity = candidate["id"]
                    key = (candidate["map_name"], candidate["point_name"])
                    reason = submission_reason(state, key, configured, future is not None)
                    if gate.claim(identity, reason):
                        last_result = reason
                        write("arrival_claimed", attempt_id=identity, map_name=key[0], point_name=key[1], result=reason)
                        if reason == "claimed":
                            # One submission only. The editor rechecks its enable switch and saved content.
                            pending_id = identity
                            future = executor.submit(submit_standalone, nav_url, editor_url, {
                                "map_name": key[0], "point_name": key[1], "request_id": "auto-" + identity}, token, candidate["at"])
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        for subscriber in subscribers:
            subscriber.unregister()
        gate.close()
        executor.shutdown(wait=False)
        write("listener_stopped")
        log.close()


if __name__ == "__main__":
    main()
