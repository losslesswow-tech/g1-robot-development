import io
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

from arrival_voice.ssh_audio import MARKER, SSHPlayer


class SSHTests(unittest.TestCase):
    def test_dead_control_master_never_submits_audio(self):
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 255, b"", b"Control socket connect failed")
        player = SSHPlayer("operator@192.0.2.18", "eth0", control_path="test-control", run=run)
        with self.assertRaisesRegex(RuntimeError, "SSH 会话已断开"):
            player.play({"mode":"tts", "text":"test", "speaker_id":0}, "test")
        self.assertEqual(len(calls), 1)
        self.assertIn("check", calls[0])

    def test_live_control_master_is_used_without_direct_fallback(self):
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if "check" in args:
                return subprocess.CompletedProcess(args, 0, b"", b"Master running")
            self.assertIn("ProxyCommand=false", args)
            return subprocess.CompletedProcess(args, 0, (MARKER + '{"volume":{"volume":70}}').encode(), b"")
        player = SSHPlayer("operator@192.0.2.18", "eth0", control_path="test-control", run=run)
        self.assertEqual(player.probe(), {"volume":70})
        self.assertEqual(len(calls), 2)

    def test_probe_is_strict_and_read_only(self):
        calls = []
        def run(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, (MARKER + '{"volume":{"volume":70}}\n').encode(), b"")
        player = SSHPlayer("operator@192.0.2.8", "eth0", run=run)
        self.assertEqual(player.probe(), {"volume": 70})
        args, kwargs = calls[0]
        self.assertIn("StrictHostKeyChecking=yes", args)
        self.assertIn("BatchMode=yes", args)
        self.assertEqual(json.loads(kwargs["input"]), {"operation": "probe", "interface": "eth0"})

    def test_timeout_and_auth_failure_are_not_retried(self):
        for failure in ("timeout", "authentication"):
            calls = []
            def run(args, **kwargs):
                calls.append(args)
                if failure == "timeout":
                    raise subprocess.TimeoutExpired(args, 30)
                return subprocess.CompletedProcess(args, 255, b"", b"Permission denied")
            player = SSHPlayer("operator@192.0.2.8", "eth0", run=run)
            with self.assertRaises(RuntimeError):
                player.play({"mode": "tts", "text": "test", "speaker_id": 0}, "test")
            self.assertEqual(len(calls), 1)

    def test_invalid_target_and_tts_never_start_ssh(self):
        for target in ("-oProxyCommand=bad", "operator@host;bad", "host"):
            with self.assertRaises(ValueError):
                SSHPlayer(target, "eth0")
        def forbidden(*args, **kwargs):
            self.fail("SSH must not run for invalid input")
        player = SSHPlayer("operator@192.0.2.8", "eth0", run=forbidden)
        for message in ({"mode": "tts", "text": "", "speaker_id": 0},
                        {"mode": "tts", "text": "test", "speaker_id": True}, {"mode": "shell"}):
            with self.assertRaises(ValueError):
                player.play(message, "test")

    def test_wav_is_data_and_local_path_is_not_sent(self):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 1600)
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "a.wav"
            media.write_bytes(buffer.getvalue())
            def run(args, **kwargs):
                payload = json.loads(kwargs["input"])
                self.assertNotIn("file", payload["message"])
                self.assertIn("pcm_wav", payload)
                self.assertEqual(kwargs["timeout"], 30.1)
                return subprocess.CompletedProcess(args, 0, (MARKER + '{"submitted":true}\n').encode(), b"")
            SSHPlayer("operator@192.0.2.8", "eth0", run=run).play({"mode": "wav", "file": str(media)}, "test")

    def test_remote_executes_original_player_with_fake_sdk(self):
        # Execute the actual remote bootstrap with a fake SDK, not a fake response.
        prefix = '''
import sys, types
for name in ("unitree_sdk2py", "unitree_sdk2py.core", "unitree_sdk2py.core.channel", "unitree_sdk2py.g1", "unitree_sdk2py.g1.audio", "unitree_sdk2py.g1.audio.g1_audio_client"):
    sys.modules[name] = types.ModuleType(name)
sys.modules["unitree_sdk2py.core.channel"].ChannelFactoryInitialize = lambda domain, interface: None
class Client:
    def SetTimeout(self, value): pass
    def Init(self): pass
    def GetVolume(self): return 0, {"volume": 70}
    def TtsMaker(self, text, speaker):
        assert text == "test ' $()" and speaker == 0
        print("FAKE_TTS_CALLED")
        return 0
sys.modules["unitree_sdk2py.g1.audio.g1_audio_client"].AudioClient = Client
'''
        results = []
        def run(args, **kwargs):
            launcher = shlex.split(args[-1])[-1]
            result = subprocess.run([sys.executable, "-c", prefix + launcher], **kwargs)
            results.append(result)
            return result
        player = SSHPlayer("operator@192.0.2.8", "eth0", run=run)
        self.assertEqual(player.probe(), {"volume": 70})
        self.assertNotIn(b"FAKE_TTS_CALLED", results[0].stdout)
        player.play({"mode": "tts", "text": "test ' $()", "speaker_id": 0}, "test")
        self.assertIn(b"FAKE_TTS_CALLED", results[1].stdout)


if __name__ == "__main__":
    unittest.main()
