"""Navigation task speech adapter. No motion clients or navigation service calls."""
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from .legacy_auto import load_registry, request_json
from .legacy_gate import LegacyGate, match_point


LINE_REACH_METERS = 0.20


def match_line_point(pose, points, tolerance=LINE_REACH_METERS):
    """Line paths encode travel direction rather than the saved final heading.

    Associate XY within the configured navigation controller reach boundary;
    a fresh robot pose is independently required before accepting arrival.
    """
    position = pose.get('position', [])
    if len(position) != 3 or any(type(v) not in (int, float) or not math.isfinite(v) for v in position):
        return None
    names = [p['name'] for p in points
             if math.dist(position[:2], p['pose']['position'][:2]) <= tolerance]
    return names[0] if len(names) == 1 else None


def speech_target(target):
    if isinstance(target, str):
        target = {"speechText": target}
    if not isinstance(target, dict):
        raise ValueError("语音步骤内容无效")
    target = copy.deepcopy(target)
    mode = target.get("speechMode", "tts")
    if mode == "tts":
        text = target.get("speechText")
        if not isinstance(text, str) or not text.strip() or len(text) > 1000:
            raise ValueError("语音文字应为 1–1000 字")
        speaker = target.get("speakerId", 0)
        if type(speaker) is not int or speaker not in (0, 1):
            raise ValueError("语音语言无效")
    elif mode == "saved":
        try:
            reference = json.loads(target["speechRef"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("请选择已保存的播报内容") from None
        if not isinstance(reference, list) or len(reference) != 2 or any(
                not isinstance(v, str) or not v.strip() for v in reference):
            raise ValueError("已保存播报引用无效")
    elif mode == "wav":
        if not re.fullmatch(r"audio/[a-f0-9]{64}\.wav", str(target.get("speechFile", ""))):
            raise ValueError("请先导入有效的 WAV 音频")
    else:
        raise ValueError("语音播放方式无效")
    return target


def validate_speech_steps(task):
    defaults = task.get("defaults", {})
    steps = list(task.get("subTasks", []))
    for name in ("beforeSteps", "afterSteps"):
        steps.extend(defaults.get(name, []))
    for step in steps:
        if step.get("action", "").upper() == "J":
            speech_target(step.get("target", {}))
    if any(s.get("action", "").upper() == "J" for s in defaults.get("errorSteps", [])):
        raise ValueError("异常处理步骤暂不支持语音，请将语音放入正常任务流程")
    return any(s.get("action", "").upper() == "J" for s in steps)


class TaskVoice:
    def __init__(self, state_root, map_root="/nav_map", editor_url="http://127.0.0.1:18768",
                 request=request_json, clock=time.monotonic, line_reach_meters=LINE_REACH_METERS):
        if type(line_reach_meters) not in (int, float) or not 0.05 <= line_reach_meters <= 0.30:
            raise ValueError('直线导航到达距离须在0.05至0.30米内')
        self.line_reach_meters = line_reach_meters
        self.root = Path(state_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.map_root, self.editor_url = map_root, editor_url
        self.request, self.clock = request, clock
        self.lock = threading.RLock()
        self.callback_lock = threading.RLock()
        self.db = sqlite3.connect(str(self.root / "task-voice.sqlite3"), check_same_thread=False,
                                  isolation_level=None)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS speech_requests (id TEXT PRIMARY KEY, state TEXT, result TEXT)")
        self.gate = LegacyGate(self.root / "task-navigation.sqlite3", clock=clock, threaded=True,
                               point_matcher=self._match_navigation_point)
        self.run_id, self.managed, self.paused = None, False, False
        self.job, self.nav = None, None
        self.ownership = []
        self.robot_pose = None

    def _match_navigation_point(self, pose, points):
        if self.nav and self.nav.get('mode') == 'line':
            return match_line_point(pose, points, self.line_reach_meters)
        return match_point(pose, points)

    def start(self, managed):
        with self.lock:
            self.cancel()
            self.run_id, self.managed, self.paused = uuid.uuid4().hex, bool(managed), False
            if self.managed:
                self.ownership.append({'start': self.clock(), 'end': None})
                self.ownership = self.ownership[-64:]

    def cancel(self):
        with self.lock:
            if self.managed and self.ownership:
                self.ownership[-1]['end'] = self.clock()
            self.run_id, self.managed = None, False
            self.nav = None
            self.gate.invalidate("task_cancelled")

    def pause(self):
        with self.lock:
            self.paused = True
            self.gate.invalidate("task_paused")
            self.nav = None

    def resume(self):
        with self.lock:
            self.paused = False

    def status(self):
        with self.lock:
            return {"managed": self.managed, "run_id": self.run_id or "", "paused": self.paused,
                    "ownership": copy.deepcopy(self.ownership),
                    "job": self.job["state"] if self.job else "idle",
                    "arrival_verification": "line-xy-live-pose-v1",
                    "line_reach_meters": self.line_reach_meters,
                    "navigation": copy.deepcopy(self.nav)}

    def begin_navigation(self, map_name, point_index, mode='normal'):
        with self.lock:
            if not self.managed:
                return
            if mode not in ('normal', 'line'):
                raise ValueError('任务导航方式无效')
            points, revision = load_registry(self.map_root, map_name)
            index = int(point_index) - 1
            if not 0 <= index < len(points):
                raise ValueError("任务导航点未保存或索引不存在")
            self.gate.invalidate("new_task_navigation")
            self.gate.set_context((map_name, revision, self.run_id), points)
            self.nav = {"name": points[index]["name"], "map": map_name, "revision": revision,
                        "started": self.clock(), "failed": False, "arrived": False,
                        "mode": mode, "target_xy": points[index]['pose']['position'][:2]}
            self.robot_pose = None

    def _line_pose_valid(self):
        pose = self.robot_pose
        return (pose is not None and 0 <= self.clock() - pose['received'] <= 1
                and pose['received'] >= self.nav['started']
                and math.dist(pose['xy'], self.nav['target_xy']) <= self.line_reach_meters)

    def observe(self, record):
        with self.lock:
            if not self.managed or not self.nav or self.paused:
                return
            if record.get('topic') == '/base_link/odom':
                if record.get('publisher') != '/x_nav' or record.get('latched'):
                    return
                position = record.get('position', [])
                age = record.get('stamp_age', float('inf'))
                received = record.get('received', -float('inf'))
                valid = (record.get('frame_id') == 'map' and len(position) == 3
                         and all(type(v) in (int, float) and math.isfinite(v) for v in position)
                         and type(age) in (int, float) and 0 <= age <= 1
                         and 0 <= self.clock() - received <= 1)
                self.robot_pose = {'xy': position[:2], 'received': received} if valid else None
                return
            outcome = self.gate.handle(record)
            if outcome in {"planning_or_navigation_failed", "unproven_arrival", "goal_changed_without_start",
                           "invalid_goal_path", "backlog", "cancelled_or_paused", "navigation_mode_changed"}:
                self.nav["failed"] = True

    def navigation_state(self):
        with self.lock:
            if not self.managed or not self.nav:
                return "idle"
            if self.nav["arrived"]:
                return "arrived"
            if self.nav["failed"] or self.clock() - self.nav["started"] > 300:
                return "failed"
            candidate = self.gate.ready()
            if candidate and not self.paused:
                if self.nav['mode'] == 'line' and not self._line_pose_valid():
                    self.nav['failed'] = True
                    self.nav['failure_reason'] = '直线导航到达时定位不新鲜或未在当前目标点范围内'
                    self.gate.invalidate('line_pose_unverified')
                    return 'failed'
                _, revision = load_registry(self.map_root, self.nav["map"])
                if revision != self.nav["revision"] or candidate["point_name"] != self.nav["name"]:
                    self.nav["failed"] = True
                    return "failed"
                if not self.gate.claim(candidate["id"], "task_arrived"):
                    self.nav['failed'] = True
                    return 'failed'
                self.nav["arrived"] = True
                return "arrived"
            return "pending"

    def editor_token(self):
        # Read the capability only from the same loopback editor used for playback.
        import urllib.request
        with urllib.request.urlopen(self.editor_url + "/", timeout=3) as response:
            html = response.read(128 * 1024).decode("utf-8")
        match = re.search(r'name="voice-token" content="([a-zA-Z0-9_-]+)"', html)
        if not match:
            raise RuntimeError("语音编辑器连接不可用")
        return match.group(1)

    def catalog(self):
        state = self.request(self.editor_url + "/api/state", timeout=3)
        return {"live": bool(state.get("live")), "points": state["config"]["points"]}

    def upload(self, data):
        if not data or len(data) > 10 * 1024 * 1024:
            raise ValueError("音频为空或超过 10 MB")
        import urllib.request
        req = urllib.request.Request(self.editor_url + "/api/upload", data=data,
                                     headers={"X-Voice-Token": self.editor_token(),
                                              "Content-Type": "audio/wav"})
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.load(response)

    def _point(self, target, request_id):
        state = self.catalog()
        if not state["live"]:
            raise RuntimeError("语音后端尚未连接实机")
        mode = target.get("speechMode", "tts")
        if mode == "saved":
            key = json.loads(target["speechRef"])
            matches = [p for p in state["points"] if [p["map_name"], p["point_name"]] == key]
            if len(matches) != 1:
                raise ValueError("引用的播报内容不存在，请重新选择")
            return matches[0]
        point = {"map_name": "任务语音", "point_name": request_id, "mode": mode}
        if mode == "tts":
            point.update(text=target["speechText"], speaker_id=target.get("speakerId", 0))
        else:
            point.update(file=target["speechFile"])
        return point

    def step(self, loop, index, target):
        target = speech_target(target)
        with self.lock:
            if not self.managed or not self.run_id:
                raise RuntimeError("任务语音未初始化")
            identity = "task-" + hashlib.sha256((self.run_id + ":" + str(loop) + ":" + str(index)).encode()).hexdigest()
            row = self.db.execute("SELECT state,result FROM speech_requests WHERE id=?", (identity,)).fetchone()
            if row:
                return json.loads(row[1]) if row[1] else {"state": row[0]}
            if self.paused:
                return {"state": "paused"}
            if self.job and self.job["thread"].is_alive():
                if self.job["id"] != identity:
                    return {"state": "failed", "error": "上一条语音请求仍在执行，任务已停止"}
                return {"state": "pending"}
            run_id = self.run_id
            job = {"id": identity, "state": "preparing"}
            thread = threading.Thread(target=self._submit, args=(job, run_id, target), daemon=True)
            job["thread"] = thread
            self.job = job
            thread.start()
            return {"state": "pending"}

    def _submit(self, job, run_id, target):
        claimed = False
        result = None
        try:
            point = self._point(target, job["id"])
            token = self.editor_token()
            with self.lock:
                if self.run_id != run_id or self.paused:
                    job["state"] = "cancelled_before_submission"
                    return
                self.db.execute("INSERT INTO speech_requests VALUES (?, 'pending', NULL)", (job["id"],))
                claimed = True
                job["state"] = "submitted_or_pending"
            response = self.request(self.editor_url + "/api/test", {
                "point": point, "request_id": job["id"]}, token, 350)
            ok = response.get("submitted") is True and response.get("simulation") is False
            result = {"state": "submitted" if ok else "failed", "response": response}
            if not ok:
                result["error"] = response.get("error", "语音未成功提交，不自动重试")
        except Exception as exc:
            result = {"state": "failed", "error": str(exc)[:500]}
        finally:
            with self.lock:
                if result is not None:
                    # Persist even uncertain failures; polling or resuming must not replay them.
                    if not claimed:
                        self.db.execute("INSERT OR IGNORE INTO speech_requests VALUES (?, 'failed', NULL)", (job["id"],))
                    self.db.execute("UPDATE speech_requests SET state=?,result=? WHERE id=?",
                                    (result["state"], json.dumps(result), job["id"]))
                    job["state"] = result["state"]


def register_voice_routes(blueprint):
    from flask import current_app, jsonify, request

    @blueprint.route('/voice_status', methods=['GET'])
    def voice_status():
        return jsonify(current_app.config['task_manager'].voice.status())

    @blueprint.route('/voice_catalog', methods=['GET'])
    def voice_catalog():
        try:
            return jsonify(current_app.config['task_manager'].voice.catalog())
        except Exception as exc:
            return jsonify({'error': str(exc)[:500]}), 503

    @blueprint.route('/voice_upload', methods=['POST'])
    def voice_upload():
        try:
            if request.content_length is None or request.content_length > 10 * 1024 * 1024:
                return jsonify({'error': '音频为空或超过 10 MB'}), 413
            return jsonify(current_app.config['task_manager'].voice.upload(request.get_data()))
        except Exception as exc:
            return jsonify({'error': str(exc)[:500]}), 400
