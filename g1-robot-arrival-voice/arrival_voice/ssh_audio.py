"""Run the unchanged UnitreePlayer on a G1 through an authenticated SSH session."""
import argparse
import base64
import json
import re
import shlex
import subprocess
import uuid
import wave
from pathlib import Path


MARKER = "G1_ARRIVAL_AUDIO_RESULT "
REMOTE = r'''
import base64, json, platform, sys, tempfile
from pathlib import Path
try:
    cfg = json.load(sys.stdin)
    player = UnitreePlayer(cfg["interface"])
    sdk_module = sys.modules.get(type(player.client).__module__)
    result = {"volume": player.probe(), "audible_completion_verified": False,
              "python": platform.python_version(), "architecture": platform.machine(),
              "sdk_source": getattr(sdk_module, "__file__", None),
              "declared_api_version": getattr(player.client, "GetApiVersion", lambda: None)()}
    if cfg["operation"] == "play":
        message = cfg["message"]
        with tempfile.TemporaryDirectory(prefix="g1-arrival-audio-") as directory:
            if message["mode"] == "wav":
                media = Path(directory) / "test.wav"
                media.write_bytes(base64.b64decode(cfg["pcm_wav"], validate=True))
                message["file"] = str(media)
            player.play(message, cfg["task_id"])
        result["submitted"] = True
    print("G1_ARRIVAL_AUDIO_RESULT " + json.dumps(result), flush=True)
except Exception as exc:
    print("G1_ARRIVAL_AUDIO_RESULT " + json.dumps({"error": str(exc), "audible_completion_verified": False}), flush=True)
    sys.exit(1)
'''


class SSHPlayer:
    def __init__(self, target, interface, control_path=None,
                 python="/home/operator/g1_voice/.venv/bin/python",
                 dds_lib="/home/operator/cyclonedds_ws/install/cyclonedds/lib", run=subprocess.run):
        if not target or not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]*@[a-zA-Z0-9][a-zA-Z0-9.-]*", target):
            raise ValueError("SSH target must be an explicit user@host")
        if not interface or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,64}", interface):
            raise ValueError("explicit robot DDS interface required")
        for value in (python, dds_lib):
            if not value.startswith("/") or "\n" in value or "\0" in value:
                raise ValueError("remote Python and DDS library must be absolute paths")
        self.target, self.interface, self.run = target, interface, run
        self.ssh = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                    "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=10",
                    "-o", "ServerAliveCountMax=2"]
        self.control_path = str(Path(control_path).expanduser().resolve()) if control_path else None
        if self.control_path:
            # Only reuse the deliberately authenticated session; never fall back
            # to an unexpected new password/key connection after a mux failure.
            self.ssh += ["-S", self.control_path, "-o", "ProxyCommand=false"]
        source = Path(__file__).with_name("audio.py").read_text(encoding="utf-8") + "\n" + REMOTE
        launcher = "import base64; exec(base64.b64decode({!r}))".format(base64.b64encode(source.encode()).decode())
        self.command = "export LD_LIBRARY_PATH=" + shlex.quote(dds_lib) + '${LD_LIBRARY_PATH:+:"$LD_LIBRARY_PATH"}; exec ' + shlex.quote(python) + " -c " + shlex.quote(launcher)

    def _call(self, payload, timeout):
        payload["interface"] = self.interface
        try:
            if self.control_path:
                check = self.run(["ssh", "-S", self.control_path, "-O", "check", self.target],
                                 capture_output=True, timeout=5)
                if check.returncode != 0:
                    raise RuntimeError("机器人 SSH 会话已断开。请在导航宿主机重启独立音频连接并输入密码；本次未提交播报。")
            result = self.run(self.ssh + [self.target, self.command],
                              input=json.dumps(payload).encode(), capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("SSH audio timeout; playback outcome unknown. Do not retry automatically.") from exc
        except OSError as exc:
            raise RuntimeError("Cannot start SSH: {}".format(exc)) from exc
        lines = result.stdout.decode("utf-8", errors="replace").splitlines()
        responses = [line[len(MARKER):] for line in lines if line.startswith(MARKER)]
        if not responses:
            error = result.stderr.decode("utf-8", errors="replace")[-1000:]
            if result.returncode == 255:
                raise RuntimeError("机器人 SSH 连接或认证失效。请在导航宿主机重启独立音频连接并输入密码；本次结果未确认，不会自动重播。 " + error)
            raise RuntimeError("SSH audio returned no result (exit {}): {}".format(result.returncode, error))
        response = json.loads(responses[-1])
        if result.returncode != 0 or response.get("error"):
            raise RuntimeError("Remote audio failed: {}".format(response.get("error", result.returncode)))
        return response

    def probe(self):
        self.last_probe = self._call({"operation": "probe"}, 25)
        return self.last_probe["volume"]

    def play(self, message, task_id):
        payload = {"operation": "play", "message": dict(message), "task_id": task_id}
        timeout = 30
        if message.get("mode") == "wav":
            media = Path(message["file"])
            if media.stat().st_size > 10 * 1024 * 1024:
                raise ValueError("WAV exceeds 10 MB")
            with wave.open(str(media), "rb") as wav:
                if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getcomptype()) != (16000, 1, 2, "NONE"):
                    raise ValueError("WAV must be 16kHz mono 16-bit PCM")
                frames = wav.getnframes()
                if not 0 < frames <= 16000 * 300 or len(wav.readframes(frames)) != frames * 2:
                    raise ValueError("WAV is empty, too long, or truncated")
                timeout += frames / 16000
            payload["pcm_wav"] = base64.b64encode(media.read_bytes()).decode()
            payload["message"].pop("file")
        elif (message.get("mode") != "tts" or not isinstance(message.get("text"), str)
              or not message["text"].strip() or len(message["text"]) > 1000
              or type(message.get("speaker_id")) is not int or message["speaker_id"] not in (0, 1)):
            raise ValueError("Invalid TTS message")
        response = self._call(payload, timeout)
        if response.get("submitted") is not True:
            raise RuntimeError("Audio submission not confirmed; do not retry automatically")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--control-path")
    parser.add_argument("--python", default="/home/operator/g1_voice/.venv/bin/python")
    parser.add_argument("--dds-lib", default="/home/operator/cyclonedds_ws/install/cyclonedds/lib")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--text")
    group.add_argument("--wav")
    parser.add_argument("--allow-robot-audio", action="store_true")
    args = parser.parse_args()
    if (args.text is not None or args.wav is not None) and not args.allow_robot_audio:
        parser.error("playback requires --allow-robot-audio")
    player = SSHPlayer(args.target, args.interface, args.control_path, args.python, args.dds_lib)
    print(json.dumps({"GetVolume": player.probe()}, ensure_ascii=False), flush=True)
    print(json.dumps({"remote_environment": player.last_probe}, ensure_ascii=False), flush=True)
    if args.text is not None or args.wav is not None:
        message = {"mode": "tts", "text": args.text, "speaker_id": 0} if args.text is not None else {"mode": "wav", "file": args.wav}
        player.play(message, uuid.uuid4().hex)
        print(json.dumps({"submitted": True, "audible_completion_verified": False}), flush=True)


if __name__ == "__main__":
    main()
