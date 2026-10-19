#!/usr/bin/env python3
"""Browser hand workbench. Python 3.8+, stdlib only; simulation by default."""
import argparse
import hashlib
import importlib.util
import json
import math
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie, CookieError
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
DRIVER_HASH = "d44845c8b28fea7ea87dc3e2df878e060468bb5c91e6436bbf62835d546b0df8"
NAMES = ["小指", "无名指", "中指", "食指", "拇指弯曲", "拇指旋转"]


class Rejected(ValueError):
    pass


def six(values):
    if not isinstance(values, list) or len(values) != 6:
        raise Rejected("需要六个原始位置值")
    if any(type(v) is not int or not 0 <= v <= 1000 for v in values):
        raise Rejected("原始位置必须是 0–1000 的整数")
    return values[:]


def load_mapping(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != 1 or set(data.get("hands", {})) != {"L", "R"}:
        raise Rejected("映射文件格式错误")
    model = data.get("model", {})
    for name in ("finger_axis", "thumb_rotation_axis", "thumb_bend_axis", "thumb_base"):
        vector = model.get(name)
        if not isinstance(vector, list) or len(vector) != 3 or any(
                type(v) not in (int, float) or not math.isfinite(v) for v in vector):
            raise Rejected("模型转轴或位置向量无效")
        if name.endswith("axis") and sum(v*v for v in vector) < 1e-12:
            raise Rejected("转轴不能为零向量")
    for name in ("finger_secondary_ratio", "thumb_secondary_ratio", "thumb_rest_yaw_degrees"):
        value = model.get(name)
        if type(value) not in (int, float) or not math.isfinite(value):
            raise Rejected("模型指节联动参数无效")
    for side, hand in data["hands"].items():
        if (type(hand.get("verified")) is not bool or type(hand.get("model_axes_verified")) is not bool
                or len(hand.get("channels", [])) != 6):
            raise Rejected("每只手须配置六路和 verified 字段")
        for index, channel in enumerate(hand["channels"]):
            if channel.get("index") != index:
                raise Rejected("通道顺序必须与仓库相同")
            points = channel.get("points", [])
            if len(points) < 2:
                raise Rejected("每路至少需要两个角度/原始值标定点")
            angles, raw = [], []
            for point in points:
                a, r = point.get("degrees"), point.get("raw")
                if type(a) not in (int, float) or not math.isfinite(a):
                    raise Rejected("模型角度须为有限数值")
                if type(r) is not int or not 0 <= r <= 1000:
                    raise Rejected("标定原始值必须在 0–1000 内")
                angles.append(a)
                raw.append(r)
            if any(a >= b for a, b in zip(angles, angles[1:])):
                raise Rejected("模型角度标定点须严格递增")
            if not (all(a < b for a, b in zip(raw, raw[1:])) or
                    all(a > b for a, b in zip(raw, raw[1:]))):
                raise Rejected("每路原始值标定点须单调，不能有重复值")
            if hand["verified"] and (not isinstance(channel.get("evidence"), str) or not channel["evidence"].strip()):
                raise Rejected("已标定通道须记录现场依据")
        if hand["verified"] and not hand.get("model_axes_verified"):
            raise Rejected("实物联动须核实示意模型转轴和指节联动")
    return data


def limits(mapping, side, values):
    for channel, value in zip(mapping["hands"][side]["channels"], six(values)):
        endpoints = [point["raw"] for point in channel["points"]]
        if not min(endpoints) <= value <= max(endpoints):
            raise Rejected("目标超出此通道已配置的范围")


class RepositoryDriver:
    """Import an unchanged, hash-pinned repository driver; never substitute targets."""
    def __init__(self, path):
        self.path = Path(path).resolve()
        if hashlib.sha256(self.path.read_bytes()).hexdigest() != DRIVER_HASH:
            raise Rejected("仓库控制脚本校验不符，拒绝实物连接")
        spec = importlib.util.spec_from_file_location("original_hand", self.path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def read(self, side):
        m = self.module
        return {"actual": six(m.read_regs(m.HANDS[side], m.ANGLE_ACT, 6)),
                "force_raw": m.read_regs(m.HANDS[side], m.FORCE_ACT, 6)}

    def read_targets(self, side):
        if hashlib.sha256(self.path.read_bytes()).hexdigest() != DRIVER_HASH:
            raise Rejected("仓库控制脚本校验不符，拒绝读取目标")
        m = self.module
        return six(m.read_regs(m.HANDS[side], m.ANGLE_SET, 6))

    def move(self, side, values):
        # Use original set_angles(), including speed=300 and its arrival loop.
        # A separate child provides a bounded wait; killing it is NOT a motor stop.
        if hashlib.sha256(self.path.read_bytes()).hexdigest() != DRIVER_HASH:
            raise Rejected("仓库控制脚本已变更，未发送目标")
        program = (
            "import importlib.util,json,sys; "
            "s=importlib.util.spec_from_file_location('original_hand',sys.argv[1]); "
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m); "
            "v=json.loads(sys.argv[3]);a=m.set_angles(m.HANDS[sys.argv[2]],v); "
            "print(json.dumps({'actual':a}))"
        )
        result = subprocess.run([sys.executable, "-u", "-c", program,
                                 str(self.path), side, json.dumps(values)],
                                capture_output=True, text=True, timeout=22)
        if result.returncode:
            raise RuntimeError("仓库控制脚本执行异常；结果不确定，请查看实物")
        return six(json.loads(result.stdout)["actual"])


class Workbench:
    def __init__(self, mode, mapping, driver=None, clock=time.monotonic):
        self.mode, self.mapping, self.driver, self.clock = mode, mapping, driver, clock
        self.lock = threading.RLock()
        self.io_lock = threading.Lock()
        self.cache = {side: {"actual": None, "force_raw": None,
                             "sampled_at": None, "error": None} for side in ("L", "R")}
        self.lease = None
        self.busy = False
        self.fault = None

    def config(self):
        return {"mode": self.mode, "names": NAMES, "mapping": self.mapping,
                "mapping_sha256": hashlib.sha256(json.dumps(self.mapping, sort_keys=True).encode()).hexdigest(),
                "speed": 300, "driver_sha256": DRIVER_HASH,
                "model": "schematic", "lease_seconds": 6}

    def _expire(self):
        if self.lease and self.clock() >= self.lease["expires"]:
            self.lease = None

    def status(self, side, refresh=False):
        if side not in self.cache:
            raise Rejected("请选择左手或右手")
        with self.lock:
            self._expire()
            sample = self.cache[side]
            should_read = (refresh and self.mode != "simulation" and
                           (sample["sampled_at"] is None or self.clock() - sample["sampled_at"] > .5))
        if should_read and self.io_lock.acquire(blocking=False):
            try:
                read = self.driver.read(side)
                with self.lock:
                    self.cache[side].update(read, sampled_at=self.clock(), error=None)
            except Exception as exc:
                with self.lock:
                    self.cache[side]["error"] = str(exc)
                    self.lease = None
                    self.fault = "读取失败，已禁止后续写入"
            finally:
                self.io_lock.release()
        with self.lock:
            sample = dict(self.cache[side])
            sampled = sample.pop("sampled_at")
            sample.update(age_seconds=None if sampled is None else self.clock() - sampled,
                          busy=self.busy, armed=bool(self.lease), fault=self.fault)
            return sample

    def arm(self, side, client):
        if self.mode != "live":
            raise Rejected("当前模式不允许实物动作")
        if side not in ("L", "R") or not isinstance(client, str) or not 8 <= len(client) <= 100:
            raise Rejected("无效的控制会话")
        hand = self.mapping["hands"][side]
        if not hand["verified"] or not hand.get("model_axes_verified"):
            raise Rejected("此手尚未完成角度和转轴标定，不能开启实物联动")
        with self.lock:
            self._expire()
            if self.lease or self.busy:
                raise Rejected("已有控制会话或动作仍在执行")
            # Reserve the session BEFORE the read, preventing simultaneous claims.
            lease = {"id": uuid.uuid4().hex, "client": client, "side": side,
                     "expires": self.clock() + 6, "seq": -1}
            self.lease = lease
        try:
            with self.io_lock:
                state = self.driver.read(side)
            limits(self.mapping, side, state["actual"])
            with self.lock:
                if self.lease is not lease or self.clock() >= lease["expires"]:
                    self.lease = None
                    raise Rejected("连接耗时过长，请重新连接")
                self.cache[side].update(state, sampled_at=self.clock(), error=None)
                self.fault = None
                return {"lease": lease["id"], "actual": state["actual"]}
        except Exception:
            with self.lock:
                if self.lease is lease:
                    self.lease = None
            raise

    def _owner(self, payload):
        self._expire()
        lease = self.lease
        if not lease or payload.get("lease") != lease["id"] or payload.get("client") != lease["client"]:
            raise Rejected("控制会话已失效，请重新连接")
        return lease

    def heartbeat(self, payload):
        with self.lock:
            lease = self._owner(payload)
            lease["expires"] = self.clock() + 6
        return {"ok": True}

    def disarm(self, payload):
        with self.lock:
            self._owner(payload)
            self.lease = None
        return {"ok": True, "message": "已禁止后续写入；已发出的目标可能仍在执行"}

    def task_arm(self, action, client):
        raise Rejected("此模式不支持导航手指任务")

    def task_pose(self, payload):
        raise Rejected("此模式不支持导航手指任务")

    def _move_pose(self, side, values, lease):
        return self.driver.move(side, values)

    def _pose_result(self, actual, values, lease):
        errors = [abs(a-b) for a, b in zip(actual, values)]
        return {"actual": actual, "target": values,
                "reached": max(errors) < 8, "errors": errors}

    def pose(self, payload):
        values = six(payload.get("values"))
        with self.lock:
            lease = self._owner(payload)
            side, seq = lease["side"], payload.get("seq")
            if payload.get("side") != side or type(seq) is not int or seq <= lease["seq"]:
                raise Rejected("手侧或指令序号不符")
            if self.busy:
                raise Rejected("上一目标尚未完成，不接收排队动作")
            limits(self.mapping, side, values)
            lease["seq"] = seq
            self.busy = True
        try:
            with self.io_lock:
                with self.lock:
                    self._expire()
                    if self.lease is not lease:
                        raise Rejected("等待期间会话已失效，未发送此目标")
                actual = six(self._move_pose(side, values, lease))
            result = self._pose_result(actual, values, lease)
            reached = result['reached']
            with self.lock:
                self._expire()
                self.cache[side].update(actual=actual, force_raw=None,
                                        sampled_at=self.clock(), error=None)
                if not reached:
                    self.lease = None
                    self.fault = "目标未到位，已禁止后续写入；不自动重试"
            return dict(result, session_active=self.lease is lease)
        except Exception:
            with self.lock:
                self.lease = None
                self.fault = "动作结果不确定，已禁止后续写入；请查看实物"
            raise
        finally:
            with self.lock:
                self.busy = False


def make_handler(workbench, token, builtin_connection=False):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_):
            pass

        def send_json(self, status, data):
            content = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def authenticated(self):
            origin = self.headers.get("Origin")
            if (origin and urlsplit(origin).netloc != self.headers.get("Host") or
                    self.headers.get("Sec-Fetch-Site") == "cross-site"):
                self.send_json(403, {"error": "不接受跨站控制请求"})
                return False
            supplied = self.headers.get("X-Hand-Token", "")
            if not supplied and builtin_connection:
                cookie = SimpleCookie()
                try:
                    cookie.load(self.headers.get('Cookie', ''))
                    item = cookie.get('g1_hand_%s' % self.server.server_port)
                    supplied = item.value if item else ''
                except CookieError:
                    supplied = ''
            if not secrets.compare_digest(supplied, token):
                self.send_json(401, {"error": "请输入启动窗口中的连接口令"})
                return False
            return True

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/api/config":
                return self.send_json(200, dict(workbench.config(), builtin_connection=builtin_connection))
            if path.startswith("/api/status/"):
                if not self.authenticated():
                    return
                try:
                    return self.send_json(200, workbench.status(path.rsplit("/", 1)[-1], refresh=True))
                except Rejected as exc:
                    return self.send_json(400, {"error": str(exc)})
            target = (ROOT / "web" / (path.lstrip("/") or "index.html")).resolve()
            if (ROOT / "web").resolve() not in target.parents:
                return self.send_error(404)
            if not target.is_file():
                return self.send_error(404)
            mime = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
                    ".css": "text/css; charset=utf-8", ".json": "application/json"}.get(target.suffix, "application/octet-stream")
            content = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("X-Content-Type-Options", "nosniff")
            if builtin_connection and target.name == 'index.html':
                self.send_header('Cache-Control', 'no-store')
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self):
            if not self.authenticated():
                return
            if builtin_connection and urlsplit(self.path).path == '/api/connect':
                # Only an ALREADY authenticated client may receive this cookie.
                content = b'{"ok":true}'
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(content)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Set-Cookie', 'g1_hand_%s=%s; Path=/; HttpOnly; SameSite=Strict' %
                                 (self.server.server_port, token))
                self.end_headers()
                self.wfile.write(content)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise Rejected("请求长度不符")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise Rejected("请求格式不符")
                routes = {"/api/arm": lambda: workbench.arm(data.get("side"), data.get("client")),
                          "/api/task/arm": lambda: workbench.task_arm(data.get("action"), data.get("client")),
                          "/api/task/pose": lambda: workbench.task_pose(data),
                          "/api/heartbeat": lambda: workbench.heartbeat(data),
                          "/api/disarm": lambda: workbench.disarm(data),
                          "/api/pose": lambda: workbench.pose(data)}
                action = routes.get(urlsplit(self.path).path)
                if action is None:
                    return self.send_json(404, {"error": "接口不存在"})
                self.send_json(200, action())
            except (Rejected, ValueError, TypeError) as exc:
                self.send_json(400, {"error": str(exc)})
            except Exception as exc:
                self.send_json(503, {"error": str(exc) or "设备操作失败，请查看实物"})
    return Handler


def claim_live_process():
    """Prevent two instances of THIS workbench writing concurrently on one host."""
    handle = open(Path(tempfile.gettempdir()) / "g1-hand-workbench.lock", "a+b")
    try:
        if sys.platform == "win32":
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        handle.close()
        raise Rejected("此宿主机已有本程序的实物控制实例")
    return handle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("simulation", "readonly", "live"), default="simulation")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--mapping", type=Path, default=ROOT / "calibration.example.json")
    parser.add_argument("--driver", type=Path, default=ROOT / "vendor/repository/inspire_hand_ctl.py")
    args = parser.parse_args()
    mapping = load_mapping(args.mapping)
    driver = None if args.mode == "simulation" else RepositoryDriver(args.driver)
    if args.mode == "live" and not any(hand["verified"] for hand in mapping["hands"].values()):
        parser.error("没有已标定的手；请先用 simulation 或 readonly 模式")
    process_lock = claim_live_process() if args.mode == "live" else None
    workbench = Workbench(args.mode, mapping, driver)
    token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer((args.bind, args.port), make_handler(workbench, token))
    print("Hand workbench: http://%s:%s  mode=%s" % (args.bind, server.server_port, args.mode), flush=True)
    print("Connection token: " + token, flush=True)
    print("Disabling output does not cancel a previously sent motor target.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if process_lock:
            process_lock.close()


if __name__ == "__main__":
    main()
