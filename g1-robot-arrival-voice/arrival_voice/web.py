"""Local editor for announcement text, WAV imports, and explicit manual tests."""
import argparse
import hashlib
import io
import json
import os
import re
import secrets
import tempfile
import threading
import time
import wave
import zipfile
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .audio import DryRunPlayer, UnitreePlayer
from .config import load_config

STATIC = Path(__file__).with_name("web_assets")
MAX_UPLOAD = 10 * 1024 * 1024


class Editor:
    def __init__(self, config, player=None, live=False, synthesizer=None,
                 task_status_url="http://127.0.0.1:9000/api/task/voice_status"):
        self.config = Path(config).resolve()
        self.config.parent.mkdir(parents=True, exist_ok=True)
        if not self.config.exists():
            self.config.write_bytes(Path(__file__).with_name("config.example.json").read_bytes())
        load_config(self.config)
        self.audio_dir = self.config.parent / "audio"
        self.player = player or DryRunPlayer()
        self.live = live
        self.synthesizer = synthesizer
        self.task_status_url = task_status_url
        self.token = secrets.token_urlsafe(32)
        self.config_lock = threading.RLock()
        self.play_lock = threading.Lock()
        self.requests = OrderedDict()
        self.automatic_enabled = False
        self.automatic_report = {}
        self.automatic_seen = 0

    def state(self):
        with self.config_lock:
            raw = json.loads(self.config.read_text(encoding="utf-8"))
            connected = bool(self.automatic_report) and time.monotonic() - self.automatic_seen < 8
            automatic = dict(self.automatic_report, connected=connected,
                             enabled=self.automatic_enabled)
        return {"config": raw, "live": self.live, "automatic_arrival_connected": connected,
                "tts": self.synthesizer.status() if self.synthesizer else {"provider": "sdk"},
                "automatic": automatic}

    def task_run(self):
        # Existing task status is read-only; do not call navigation controls.
        from .legacy_auto import request_json
        state = request_json(self.task_status_url, timeout=2)
        if (not state.get("managed") or state.get("paused") or not state.get("run_id")
                or state.get("job") != "submitted_or_pending"):
            raise ValueError("任务已暂停或取消，本次未提交机器人播音。")
        return state["run_id"]

    def automatic_status(self, report):
        if report.get("mode") != "legacy_conservative":
            raise ValueError("自动触发模式无效。")
        with self.config_lock:
            self.automatic_report = {key: str(report.get(key, ""))[:500]
                                     for key in ("mode", "state", "map_name", "point_name", "last_result")}
            self.automatic_seen = time.monotonic()
        return {"enabled": self.automatic_enabled}

    def automatic_enable(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("启用状态无效。")
        with self.config_lock:
            if enabled and (not self.automatic_report or time.monotonic() - self.automatic_seen >= 8
                            or self.automatic_report.get("state") == "unavailable"):
                raise ValueError("导航监听器尚未连接，请先检查连接。")
            self.automatic_enabled = enabled
        return {"enabled": enabled}

    def automatic_test(self, payload):
        with self.config_lock:
            if not self.automatic_enabled or time.monotonic() - self.automatic_seen >= 8:
                raise ValueError("自动播报已暂停或监听连接已断开。")
            raw = json.loads(self.config.read_text(encoding="utf-8"))
            matches = [p for p in raw["points"] if
                       (p["map_name"], p["point_name"]) == (payload.get("map_name"), payload.get("point_name"))]
            if len(matches) != 1:
                raise ValueError("该导航点未配置播报内容。")
            point = matches[0]
        # Reuse exactly the same validated TTS/WAV transport as manual playback.
        return self.test(point, payload.get("request_id"), automatic=True)

    def validate_point(self, point):
        if not isinstance(point, dict):
            raise ValueError("点位内容格式不正确。")
        for field in ("map_name", "point_name"):
            if not isinstance(point.get(field), str) or not point[field].strip() or len(point[field]) > 256:
                raise ValueError("请填写地图名称和导航点名称（最长 256 字）。")
        if point.get("mode") not in ("tts", "wav"):
            raise ValueError("请选择文字转语音或预录音频。")
        if point["mode"] == "wav":
            if not re.fullmatch(r"audio/[a-f0-9]{64}\.wav", str(point.get("file", ""))):
                raise ValueError("请先导入一个有效的 WAV 文件。")
        candidate = {"version": 1, "points": [point]}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".json", dir=self.config.parent,
                                             encoding="utf-8", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(candidate, handle, ensure_ascii=False)
            messages, _ = load_config(temporary)
            return messages[(point["map_name"], point["point_name"])]
        except (ValueError, OSError, wave.Error, EOFError) as exc:
            raise ValueError("内容无效：{}".format(exc)) from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def save(self, point):
        with self.config_lock:
            self.validate_point(point)
            raw = json.loads(self.config.read_text(encoding="utf-8"))
            key = (point["map_name"], point["point_name"])
            clean = {"map_name": key[0], "point_name": key[1], "mode": point["mode"]}
            if point["mode"] == "tts":
                clean.update(text=point["text"], speaker_id=point.get("speaker_id", 0))
            else:
                clean.update(file=point["file"], display_name=str(point.get("display_name", "导入音频"))[:200])
            raw["points"] = [p for p in raw["points"] if (p["map_name"], p["point_name"]) != key]
            raw["points"].append(clean)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile("w", suffix=".json", dir=self.config.parent,
                                                 encoding="utf-8", delete=False) as handle:
                    temporary = Path(handle.name)
                    json.dump(raw, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                load_config(temporary)
                os.replace(str(temporary), str(self.config))
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return {"saved": True, "point": clean}

    def upload(self, data):
        if not data or len(data) > MAX_UPLOAD:
            raise ValueError("音频为空或超过 10 MB。")
        try:
            with wave.open(io.BytesIO(data), "rb") as wav:
                if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getcomptype()) != (16000, 1, 2, "NONE"):
                    raise ValueError("请选择 16 kHz、单声道、16 位 PCM WAV。")
                frames = wav.getnframes()
                if not 0 < frames <= 16000 * 300:
                    raise ValueError("音频时长应大于 0 秒且不超过 5 分钟。")
                if len(wav.readframes(frames)) != frames * 2:
                    raise ValueError("WAV 文件不完整。")
        except (wave.Error, EOFError) as exc:
            raise ValueError("无法读取音频，请导入有效的 PCM WAV 文件。") from exc
        name = hashlib.sha256(data).hexdigest() + ".wav"
        self.audio_dir.mkdir(exist_ok=True)
        destination = self.audio_dir / name
        if destination.is_symlink():
            raise ValueError("音频存储路径无效。")
        with self.config_lock:
            if not destination.exists():
                destination.write_bytes(data)
        return {"file": "audio/" + name, "duration": round(frames / 16000, 3), "bytes": len(data)}

    def test(self, point, request_id, automatic=False):
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{16,100}", request_id):
            raise ValueError("测试请求标识无效，请重新点击。")
        message = self.validate_point(point)
        with self.config_lock:
            if automatic and (not self.automatic_enabled or time.monotonic() - self.automatic_seen >= 8):
                raise ValueError("自动播报已暂停或监听连接已断开。")
            if request_id in self.requests:
                return dict(self.requests[request_id], duplicate=True)
            if not self.play_lock.acquire(blocking=False):
                raise ValueError("当前测试尚未结束，请稍后再试。")
            # Reserve before calling audio; an uncertain timeout must not be replayed.
            self.requests[request_id] = {"submitted": False, "simulation": not self.live,
                                         "status": "pending", "audible_completion_verified": False}
            while len(self.requests) > 128:
                self.requests.popitem(last=False)
        try:
            if message["mode"] == "tts" and self.synthesizer:
                # Leave ten seconds of margin within TaskVoice's 350-second HTTP budget.
                deadline = time.monotonic() + 340
                run_id = self.task_run() if request_id.startswith("task-") else None
                data = self.synthesizer.synthesize(message["text"], message["speaker_id"])
                from .cosyvoice import wav_duration
                duration = wav_duration(data)
                if run_id is not None and self.task_run() != run_id:
                    raise ValueError("任务已切换，本次未提交机器人播音。")
                with self.config_lock:
                    if automatic and (not self.automatic_enabled or time.monotonic() - self.automatic_seen >= 8):
                        raise ValueError("自动播报已暂停或监听连接已断开，本次未提交机器人播音。")
                if time.monotonic() + duration + 30 > deadline:
                    raise ValueError("合成与播放超出任务语音时间预算，本次未提交机器人播音。")
                with tempfile.TemporaryDirectory(prefix="g1-cosyvoice-") as directory:
                    media = Path(directory) / "speech.wav"
                    media.write_bytes(data)
                    self.player.play({"mode": "wav", "file": str(media)}, "manual-" + request_id)
            else:
                self.player.play(message, "manual-" + request_id)
            result = {"submitted": True, "simulation": not self.live,
                      "status": "submitted", "audible_completion_verified": False}
        except Exception as exc:
            result = {"submitted": False, "simulation": not self.live,
                      "status": "failed_or_unknown", "error": str(exc)[:500],
                      "audible_completion_verified": False}
        finally:
            self.play_lock.release()
        with self.config_lock:
            self.requests[request_id] = result
        return result

    def export(self):
        with self.config_lock:
            load_config(self.config)
            raw = json.loads(self.config.read_text(encoding="utf-8"))
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("config.json", json.dumps(raw, ensure_ascii=False, indent=2))
                media = {p["file"] for p in raw["points"] if p.get("mode") == "wav"}
                for relative in sorted(media):
                    archive.write(self.config.parent / relative, relative)
            return buffer.getvalue()


def handler_for(editor):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Do not put announcement text or capability tokens in access logs.
            pass

        def valid_host(self):
            port = self.server.server_address[1]
            return self.headers.get("Host") in {"127.0.0.1:{}".format(port), "localhost:{}".format(port)}

        def reply(self, status, payload, mime="application/json; charset=utf-8", download=None):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if isinstance(payload, dict) else payload
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; media-src 'self' blob:; object-src 'none'; frame-ancestors 'none'")
            if download:
                self.send_header("Content-Disposition", 'attachment; filename="{}"'.format(download))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self.valid_host():
                return self.reply(403, {"error": "仅允许本地访问。"})
            try:
                if self.path == "/":
                    html = (STATIC / "index.html").read_text(encoding="utf-8").replace("__VOICE_TOKEN__", editor.token)
                    return self.reply(200, html.encode("utf-8"), "text/html; charset=utf-8")
                if self.path in {"/app.js", "/style.css"}:
                    mime = "text/javascript; charset=utf-8" if self.path.endswith(".js") else "text/css; charset=utf-8"
                    return self.reply(200, (STATIC / self.path[1:]).read_bytes(), mime)
                if self.path == "/api/state":
                    return self.reply(200, editor.state())
                if self.path == "/api/export.zip":
                    return self.reply(200, editor.export(), "application/zip", "g1-announcements.zip")
                if re.fullmatch(r"/media/[a-f0-9]{64}\.wav", self.path):
                    filename = editor.audio_dir / self.path.split("/")[-1]
                    if filename.resolve().parent != editor.audio_dir.resolve():
                        raise ValueError("音频路径无效。")
                    return self.reply(200, filename.read_bytes(), "audio/wav")
                return self.reply(404, {"error": "未找到内容。"})
            except (OSError, ValueError) as exc:
                return self.reply(400, {"error": str(exc)[:500]})

        def do_POST(self):
            port = self.server.server_address[1]
            origin = self.headers.get("Origin")
            allowed_origins = {"http://127.0.0.1:{}".format(port), "http://localhost:{}".format(port)}
            if (not self.valid_host() or self.headers.get("X-Voice-Token") != editor.token
                    or (origin and origin not in allowed_origins)):
                return self.reply(403, {"error": "请求来源无效，请重新打开本地页面。"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                limit = MAX_UPLOAD if self.path == "/api/upload" else 64 * 1024
                if not 0 < length <= limit:
                    return self.reply(413, {"error": "文件或请求过大。"})
                data = self.rfile.read(length)
                if len(data) != length:
                    raise ValueError("传输不完整。")
                if self.path == "/api/upload":
                    return self.reply(200, editor.upload(data))
                payload = json.loads(data.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("请求内容应为 JSON 对象。")
                if self.path == "/api/save":
                    return self.reply(200, editor.save(payload))
                if self.path == "/api/test":
                    return self.reply(200, editor.test(payload.get("point"), payload.get("request_id")))
                if self.path == "/api/automatic/status":
                    return self.reply(200, editor.automatic_status(payload))
                if self.path == "/api/automatic/enable":
                    return self.reply(200, editor.automatic_enable(payload.get("enabled")))
                if self.path == "/api/automatic/play":
                    return self.reply(200, editor.automatic_test(payload))
                return self.reply(404, {"error": "未找到操作。"})
            except (ValueError, TypeError, UnicodeError, OSError) as exc:
                return self.reply(400, {"error": str(exc)[:500]})
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=".local-voice/web/config.json")
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--backend", choices=("dry-run", "unitree", "unitree-ssh"), default="dry-run")
    parser.add_argument("--interface")
    parser.add_argument("--allow-robot-audio", action="store_true")
    parser.add_argument("--ssh-target")
    parser.add_argument("--ssh-control-path")
    parser.add_argument("--ssh-python", default="/home/operator/g1_voice/.venv/bin/python")
    parser.add_argument("--ssh-dds-lib", default="/home/operator/cyclonedds_ws/install/cyclonedds/lib")
    parser.add_argument("--tts-provider", choices=("sdk", "cosyvoice"), default="sdk")
    parser.add_argument("--tts-config", help="CosyVoice settings JSON; API key comes from its named environment variable")
    args = parser.parse_args()
    if args.backend != "dry-run" and not (args.interface and args.allow_robot_audio):
        parser.error("robot audio requires --interface and --allow-robot-audio")
    if args.backend == "unitree-ssh" and not args.ssh_target:
        parser.error("unitree-ssh requires --ssh-target")
    if not 0 < args.port < 65536:
        parser.error("port must be between 1 and 65535")
    if args.tts_provider == "cosyvoice" and not args.tts_config:
        parser.error("cosyvoice requires --tts-config")
    if args.tts_config and args.tts_provider != "cosyvoice":
        parser.error("--tts-config requires --tts-provider cosyvoice")
    synthesizer = None
    if args.tts_provider == "cosyvoice":
        from .cosyvoice import CosyVoiceSynthesizer
        synthesizer = CosyVoiceSynthesizer.from_file(args.tts_config)
    if args.backend == "unitree-ssh":
        from .ssh_audio import SSHPlayer
        player = SSHPlayer(args.ssh_target, args.interface, args.ssh_control_path, args.ssh_python, args.ssh_dds_lib)
    else:
        player = UnitreePlayer(args.interface) if args.backend == "unitree" else DryRunPlayer()
    if args.backend != "dry-run":
        print(json.dumps({"GetVolume": player.probe()}, ensure_ascii=False), flush=True)
    editor = Editor(args.config, player, args.backend != "dry-run", synthesizer=synthesizer)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(editor))
    print("G1 voice editor: http://127.0.0.1:{}/".format(args.port), flush=True)
    print("Backend: {} | Config: {}".format(args.backend, editor.config), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
