import json
from pathlib import Path
import tempfile
import unittest

from arrival_voice.legacy_auto import load_registry
from arrival_voice.legacy_gate import LegacyGate, match_point


POSE = {"position": [2.09256, 1.08847, -0.49177],
        "orientation": [0.00201018, 0.0455873, -0.00470877, 0.998947]}
PATH_POSE = {"position": [2.092560052871704, 1.0884699821472168, -0.48386698961257935],
             "orientation": [0, 0, -0.004631480873367706, 0.9999892746349431]}
POINTS = [{"name": "2", "pose": POSE}]


class LegacyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Path(self.tmp.name) / "attempts.sqlite3"
        self.now = 100.0
        self.gate = LegacyGate(self.database, clock=lambda: self.now)
        self.gate.set_context(("1009", "revision", "epochs"), POINTS)

    def tearDown(self):
        self.gate.close()
        self.tmp.cleanup()

    def emit(self, raw, publisher="/w_nav", topic="/x_nav/state", **extra):
        return self.gate.handle(dict(topic=topic, publisher=publisher, raw=raw, received=self.now, **extra))

    def path(self, pose=PATH_POSE, **extra):
        return self.emit("", "/x_planner", "/x_nav/goal_path", header={"frame_id": "map"},
                         pose_count=48, end_pose=pose, **extra)

    def start(self):
        self.emit("14", "/x_planner")
        self.path()
        self.now += 1
        self.emit("1#4#48")

    def arrived(self):
        self.emit("13")
        self.now += 0.6
        return self.gate.ready()

    def test_actual_goal_path_matches_saved_point(self):
        self.assertEqual(match_point(PATH_POSE, POINTS), "2")
        self.assertIsNone(match_point(PATH_POSE, POINTS + [{"name": "duplicate", "pose": POSE}]))
        self.assertIsNone(match_point(dict(PATH_POSE, position=[2.5, 1, 0]), POINTS))
        self.assertIsNone(match_point(dict(PATH_POSE, orientation=[float("nan"), 0, 0, 1]), POINTS))

    def test_only_completed_navigation_claims_once(self):
        self.start()
        for _ in range(20):
            self.emit("12")
            self.assertIsNone(self.gate.ready())
        candidate = self.arrived()
        self.assertEqual((candidate["map_name"], candidate["point_name"]), ("1009", "2"))
        self.assertEqual(self.emit("13"), "duplicate_arrival")
        self.assertTrue(self.gate.claim(candidate["id"]))
        self.assertFalse(self.gate.claim(candidate["id"]))
        self.assertEqual(self.emit("13"), "untracked_result")

    def test_ground_projected_origin_and_yaw_bound(self):
        origin = [{"name": "原点", "pose": {"position": [0,0,0], "orientation": [0,0,0,1]}}]
        projected = {"position": [0,0,-0.484268], "orientation": [0,0,0,1]}
        self.assertEqual(match_point(projected, origin), "原点")
        self.assertIsNone(match_point(dict(projected, position=[.1,0,-.484268]), origin))
        self.assertIsNone(match_point(dict(projected, orientation=[0,0,.1,.995]), origin))
        self.assertIsNone(match_point(dict(projected, orientation=[0,0,0,0]), origin))

    def test_no_start_no_execution_no_matching_goal(self):
        self.assertEqual(self.emit("13"), "untracked_result")
        self.emit("14", "/x_planner")
        self.path()
        self.assertEqual(self.emit("13"), "unproven_arrival")
        self.start()
        self.path(dict(PATH_POSE, position=[9, 9, 9]))
        self.assertIsNone(self.arrived())

    def test_cancel_pause_failure_and_mode_change_drop_arrival(self):
        for topic, raw in (("/planner/cmd", "cancel_nav"), ("/planner/cmd", "disable_localplanner"),
                           ("/x_nav/state", "15"), ("/x_nav/state", "16"),
                           ("/x_nav/state", "11#2#1#1"), ("/node_cmd", "exit_navigation")):
            with self.subTest(raw=raw):
                self.start()
                self.emit(raw, topic=topic)
                self.assertIsNone(self.arrived())

    def test_cancel_during_settle_prevents_claim(self):
        self.start()
        self.emit("13")
        self.assertIsNone(self.gate.ready())
        self.emit("cancel_nav", topic="/planner/cmd")
        self.now += 1
        self.assertIsNone(self.gate.ready())

    def test_foreign_publisher_and_latched_messages_ignored(self):
        self.emit("14", "/other")
        self.assertIsNone(self.gate.active)
        self.emit("14", "/x_planner", latched=True)
        self.assertIsNone(self.gate.active)
        self.start()
        self.emit("13", "/x_planner")
        self.now += 1
        self.assertIsNone(self.gate.ready())

    def test_context_change_disconnect_and_old_queue_drop(self):
        for kind in ("context", "disconnect", "backlog"):
            self.gate.set_context(("1009", "revision", "epochs"), POINTS)
            self.start()
            if kind == "context":
                self.gate.set_context(("other_map", "revision", "epochs"), POINTS)
            elif kind == "disconnect":
                self.gate.disconnected()
            else:
                self.gate.handle({"topic": "/x_nav/state", "raw": "13", "publisher": "/w_nav", "received": self.now - 2})
            self.assertIsNone(self.arrived())
        self.now += 1
        self.gate.set_context(("1009", "new_revision"), POINTS)
        self.assertEqual(self.gate.handle({"topic": "/x_nav/state", "raw": "14", "publisher": "/x_planner", "received": self.now - 0.2}), "previous_context_ignored")

    def test_changed_path_and_expired_attempt_drop(self):
        self.start()
        self.now += 2
        self.path(dict(PATH_POSE, position=[9, 9, 9]))
        self.assertIsNone(self.arrived())
        self.start()
        self.now += 301
        self.assertIsNone(self.arrived())

    def test_restart_never_resumes_and_single_consumer_lock(self):
        self.start()
        with self.assertRaises(RuntimeError):
            LegacyGate(self.database)
        # Mimic an abrupt exit, retaining pending state in SQLite.
        self.gate.db.close()
        self.gate.file_lock.close()
        self.gate = LegacyGate(self.database, clock=lambda: self.now)
        self.gate.set_context(("1009", "revision", "epochs"), POINTS)
        self.assertEqual(self.emit("13"), "untracked_result")
        self.assertEqual(self.gate.db.execute("SELECT state FROM legacy_attempts").fetchone()[0], "interrupted")

    def test_new_navigation_can_announce_same_point_again(self):
        ids = []
        for _ in range(2):
            self.start()
            candidate = self.arrived()
            ids.append(candidate["id"])
            self.assertTrue(self.gate.claim(candidate["id"]))
        self.assertNotEqual(*ids)


class RegistryTests(unittest.TestCase):
    def test_registry_and_path_validation(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "1009"
            folder.mkdir()
            filename = folder / "nav_points.txt"
            filename.write_text("2.09256 1.08847 -0.49177 0.00201018 0.0455873 -0.00470877 0.998947 0 0 0 2\n")
            points, revision = load_registry(root, "1009")
            self.assertEqual(points, POINTS)
            self.assertEqual(len(revision), 64)
            for name in ("", "..", "../1009", "/1009", "a\\1009"):
                with self.assertRaises(ValueError):
                    load_registry(root, name)
            filename.write_text(filename.read_text() * 2)
            with self.assertRaises(ValueError):
                load_registry(root, "1009")


if __name__ == "__main__":
    unittest.main()
