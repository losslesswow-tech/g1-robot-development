"""Opt-in legacy association. Local attempt IDs are NOT planner task IDs."""
import math
import os
import sqlite3
import time
import uuid


def match_point(pose, points, tolerance=0.03):
    """Match saved XY/yaw; the planner projects goal height onto its ground map."""
    position = pose.get("position", [])
    if len(position) != 3 or any(type(v) not in (int, float) or not math.isfinite(v) for v in position):
        return None
    matches = []
    orientation = pose.get("orientation", [])
    if len(orientation) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) for v in orientation):
        return None
    norm = math.sqrt(sum(v*v for v in orientation))
    if norm <= 0.5:
        return None
    def yaw(quaternion):
        length = math.sqrt(sum(v*v for v in quaternion))
        x, y, z, w = (v / length for v in quaternion)
        return math.atan2(2 * (w*z + x*y), 1 - 2 * (y*y + z*z))
    goal_yaw = yaw(orientation)
    for point in points:
        saved = point["pose"]
        if math.dist(position[:2], saved["position"][:2]) > tolerance:
            continue
        angle = goal_yaw - yaw(saved["orientation"])
        if abs(math.atan2(math.sin(angle), math.cos(angle))) > 0.10:
            continue
        matches.append(point["name"])
    return matches[0] if len(matches) == 1 else None


class LegacyGate:
    """Fail closed on missing evidence, with the acknowledged untagged-result limit.

    All methods run on one event-loop thread, or under the caller's mutex when
    threaded=True. Audio submission is deliberately
    outside this class and must follow claim() immediately, without retries.
    """
    def __init__(self, database, plan_publisher="/x_planner", arrival_publisher="/w_nav",
                 max_seconds=300, settle_seconds=0.5, clock=time.monotonic, threaded=False,
                 point_matcher=match_point):
        self.clock = clock
        self.max_seconds = max_seconds
        self.settle_seconds = settle_seconds
        self.plan_publisher = plan_publisher
        self.arrival_publisher = arrival_publisher
        self.point_matcher = point_matcher
        self.context = None
        self.context_since = self.clock()
        self.points = []
        self.active = None
        self.latest_goal = None
        self.file_lock = open(str(database) + ".lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if self.file_lock.seek(0, 2) == 0:
                    self.file_lock.write(b"0")
                    self.file_lock.flush()
                self.file_lock.seek(0)
                msvcrt.locking(self.file_lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file_lock.close()
            raise RuntimeError("another legacy consumer owns this database") from None
        self.db = sqlite3.connect(str(database), isolation_level=None, check_same_thread=not threaded)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS legacy_attempts (attempt_id TEXT PRIMARY KEY, map_name TEXT, point_name TEXT, state TEXT, result TEXT)")
        self.db.execute("UPDATE legacy_attempts SET state='interrupted' WHERE state IN ('pending','ready')")

    def close(self):
        self.invalidate("stopped")
        self.db.close()
        self.file_lock.close()

    def invalidate(self, reason):
        if self.active:
            self.db.execute("UPDATE legacy_attempts SET state=? WHERE attempt_id=?", (reason, self.active["id"]))
        self.active = None
        self.latest_goal = None
        return reason

    def set_context(self, identity, points):
        if identity != self.context:
            self.invalidate("context_changed")
            self.context_since = self.clock()
        self.context = identity
        self.points = points

    def disconnected(self):
        self.context = None
        return self.invalidate("disconnected")

    def _bind_goal(self, goal):
        if not self.active or not goal or abs(goal["at"] - self.active["at"]) > 2:
            return "goal_outside_start_window"
        name = self.point_matcher(goal["pose"], self.points)
        self.active["point_name"] = name
        self.db.execute("UPDATE legacy_attempts SET point_name=? WHERE attempt_id=?", (name, self.active["id"]))
        return "target_matched" if name else "unknown_or_ambiguous_target"

    def handle(self, record):
        now = self.clock()
        if record.get("latched"):
            return "latched_ignored"
        if now - record.get("received", now) > 1:
            return self.invalidate("backlog")
        topic, publisher, raw = record.get("topic"), record.get("publisher"), record.get("raw", "")
        if topic == "/planner/cmd" and raw in {"cancel_nav", "disable_localplanner", "replan_goal"}:
            return self.invalidate("cancelled_or_paused")
        if topic == "/node_cmd" and raw.startswith(("exit_", "launch_", "switch_map")):
            return self.invalidate("navigation_mode_changed")
        if not self.context:
            return "no_context"
        if record.get("received", now) < self.context_since:
            return "previous_context_ignored"
        if self.active and now - self.active["at"] > self.max_seconds:
            self.invalidate("expired")
        if topic == "/x_nav/goal_path" and publisher == self.plan_publisher:
            if record.get("header", {}).get("frame_id") != "map" or not record.get("pose_count"):
                return self.invalidate("invalid_goal_path")
            goal = {"at": now, "pose": record["end_pose"]}
            if self.active and now - self.active["at"] > 2:
                if self.point_matcher(goal["pose"], self.points) != self.active["point_name"]:
                    return self.invalidate("goal_changed_without_start")
                return "same_goal_path"
            self.latest_goal = goal
            return self._bind_goal(goal)
        if topic != "/x_nav/state":
            return "ignored_topic"
        if publisher not in {self.plan_publisher, self.arrival_publisher}:
            return "foreign_publisher"
        if raw in {"-2", "3", "5", "7", "9", "15", "16"} or raw.startswith("11#"):
            return self.invalidate("planning_or_navigation_failed")
        if raw == "14" and publisher == self.plan_publisher:
            recent = self.latest_goal
            self.invalidate("superseded")
            identity = uuid.uuid4().hex
            self.active = {"id": identity, "at": now, "map_name": self.context[0],
                           "point_name": None, "executing": False, "ready_at": None}
            self.db.execute("INSERT INTO legacy_attempts VALUES (?,?,NULL,'pending',NULL)", (identity,self.context[0]))
            self.latest_goal = recent
            self._bind_goal(recent)
            return "started"
        if not self.active:
            return "untracked_result" if raw == "13" else "no_active_attempt"
        if publisher == self.arrival_publisher and (raw == "12" or raw.startswith("1#")):
            self.active["executing"] = True
            return "navigation_evidence"
        if raw == "13" and publisher == self.arrival_publisher:
            if self.active["ready_at"] is not None:
                return "duplicate_arrival"
            if not self.active["point_name"] or not self.active["executing"]:
                return self.invalidate("unproven_arrival")
            self.active["ready_at"] = now + self.settle_seconds
            self.db.execute("UPDATE legacy_attempts SET state='ready' WHERE attempt_id=?", (self.active["id"],))
            return "arrival_pending_settle"
        return "ignored_state"

    def ready(self):
        if not self.active or self.active["ready_at"] is None:
            return None
        if self.clock() - self.active["at"] > self.max_seconds:
            self.invalidate("expired")
            return None
        if self.clock() < self.active["ready_at"]:
            return None
        return dict(self.active)

    def claim(self, attempt_id, state="claimed"):
        candidate = self.ready()
        if not candidate or candidate["id"] != attempt_id:
            return False
        changed = self.db.execute("UPDATE legacy_attempts SET state=? WHERE attempt_id=? AND state='ready'", (state,attempt_id)).rowcount
        self.active = None
        self.latest_goal = None
        return changed == 1

    def result(self, attempt_id, result):
        self.db.execute("UPDATE legacy_attempts SET result=? WHERE attempt_id=?", (str(result)[:1000],attempt_id))
