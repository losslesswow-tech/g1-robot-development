"""Offline contracts for cloud synthesis and the existing editor/robot boundary."""
import io
import json
from pathlib import Path
import tempfile
import struct
import unittest
from unittest.mock import patch
import urllib.error
import wave

from arrival_voice.cosyvoice import CosyVoiceSynthesizer, endpoint_for, normalize_wav, wav_duration
from arrival_voice.web import Editor


BASE = "https://test-space.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
AUDIO = "http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/test.wav?signature=private"


def wav_bytes(frames=1600, rate=16000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\0\0" * frames)
    return buffer.getvalue()


class CloudTests(unittest.TestCase):
    def make(self, reply=None, audio=None):
        self.calls = []
        def open_url(req, timeout):
            self.calls.append((req, timeout))
            if len(self.calls) == 1:
                return io.BytesIO(json.dumps(reply or {"output": {"finish_reason": "stop", "audio": {"url": AUDIO}}}).encode())
            return io.BytesIO(wav_bytes() if audio is None else audio)
        return CosyVoiceSynthesizer({"base_url": BASE}, key="test-secret", open_url=open_url)

    def test_compatible_base_becomes_documented_tts_endpoint(self):
        self.assertEqual(endpoint_for(BASE), "https://test-space.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer")
        for value in ("http://test.cn-beijing.maas.aliyuncs.com", "https://example.com", BASE + "?key=test",
                      "https://dashscope.aliyuncs.com.evil.test", "https://developer@example.invalid",
                      "https://dashscope.aliyuncs.com:8443", BASE + "/chat/completions"):
            with self.assertRaises(ValueError):
                endpoint_for(value)

    def test_requests_16k_wav_and_keeps_key_off_download_and_status(self):
        synth = self.make()
        self.assertEqual(wav_duration(synth.synthesize("测试", 1)), 0.1)
        request = self.calls[0][0]
        payload = json.loads(request.data)
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")
        self.assertEqual(payload["input"]["sample_rate"], 16000)
        self.assertEqual(payload["input"]["format"], "wav")
        self.assertEqual(payload["input"]["language_hints"], ["en"])
        self.assertFalse(payload["input"]["enable_ssml"])
        self.assertIsNone(self.calls[1][0].get_header("Authorization"))
        self.assertTrue(self.calls[1][0].full_url.startswith("https://"))
        self.assertNotIn("test-secret", json.dumps(synth.status()))

    def test_untrusted_audio_urls_are_rejected_before_fetch(self):
        for url in ("http://127.0.0.1/test.wav", "file:///etc/passwd", "https://example.com/test.wav",
                    "https://evil.test.oss-cn-beijing.aliyuncs.com/a", "https://developer@example.invalid/a"):
            synth = self.make(reply={"output": {"finish_reason": "stop", "audio": {"url": url}}})
            with self.assertRaises(ValueError):
                synth.synthesize("test")
            self.assertEqual(len(self.calls), 1)

    def test_invalid_or_incomplete_cloud_result_never_downloads(self):
        for reply in ({"error": "bad"}, {"output": None}, {"output": {"finish_reason": None, "audio": {"url": AUDIO}}}):
            synth = self.make(reply=reply)
            with self.assertRaises(ValueError):
                synth.synthesize("test")
            self.assertEqual(len(self.calls), 1)

    def test_bad_wav_and_truncation_rejected(self):
        for data in (b"bad", wav_bytes(rate=24000), wav_bytes(frames=0), wav_bytes()[:-10]):
            with self.assertRaises(ValueError):
                self.make(audio=data).synthesize("test")

    def test_observed_cloud_streaming_header_is_repaired_without_changing_pcm(self):
        original = wav_bytes()
        raw = bytearray(original)
        struct.pack_into("<I", raw, 4, 0x7FFFFFBF)
        struct.pack_into("<I", raw, 40, 0x7FFFFF9B)
        self.assertEqual(self.make(audio=bytes(raw)).synthesize("test"), original)
        with self.assertRaises(ValueError):
            normalize_wav(bytes(raw[:-1]))
        struct.pack_into("<I", raw, 40, 123456789)
        with self.assertRaises(ValueError):
            normalize_wav(bytes(raw))

    def test_incomplete_http_download_is_not_repaired(self):
        synth = self.make()
        class Response(io.BytesIO):
            def getheader(self, name): return "9999"
        synth.open_url = lambda request, timeout: Response(b"{}")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            synth.synthesize("test")

    def test_http_error_is_safe_and_not_retried(self):
        calls = []
        def open_url(req, timeout):
            calls.append(req)
            raise urllib.error.HTTPError(req.full_url, 401, "test-secret echoed", {}, io.BytesIO(b"test-secret"))
        synth = CosyVoiceSynthesizer({"base_url": BASE}, key="test-secret", open_url=open_url)
        with self.assertRaisesRegex(RuntimeError, "HTTP 401") as error:
            synth.synthesize("test")
        self.assertNotIn("test-secret", str(error.exception))
        self.assertEqual(len(calls), 1)

    def test_redirect_never_forwards_authentication(self):
        from arrival_voice.cosyvoice import NoRedirect
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://example.com"))

    def test_key_required_and_plaintext_config_refused(self):
        for cfg, key in (({"base_url": BASE}, ""), ({"base_url": BASE, "api_key": "secret"}, "test"),
                         ({"base_url": BASE, "timeout_seconds": 46}, "test"),
                         ({"base_url": BASE, "timeout_seconds": float("nan")}, "test")):
            with self.assertRaises(ValueError):
                CosyVoiceSynthesizer(cfg, key=key)

    def test_synthesis_deadline_shared_with_download(self):
        clock = iter([0, 0, 0, 46])
        synth = self.make()
        synth.clock = lambda: next(clock)
        with self.assertRaises((TimeoutError, RuntimeError)):
            synth.synthesize("test")
        self.assertEqual(len(self.calls), 1)


class EditorSynthesisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.calls, self.synthesis = [], []
        owner = self
        class Synth:
            def status(self): return {"provider": "cosyvoice", "model": "cosyvoice-v3-flash", "voice": "longanyang"}
            def synthesize(self, text, speaker_id):
                owner.synthesis.append(text)
                if owner.effect:
                    owner.effect()
                return wav_bytes()
        class Player:
            def play(self, message, task_id):
                owner.calls.append((dict(message), task_id))
                if message["mode"] == "wav":
                    owner.assertEqual(wav_duration(Path(message["file"]).read_bytes()), 0.1)
        self.effect = None
        self.editor = Editor(Path(self.tmp.name) / "config.json", Player(), live=True, synthesizer=Synth())
        self.point = {"map_name": "任务语音", "point_name": "text", "mode": "tts", "text": "欢迎", "speaker_id": 0}

    def tearDown(self):
        self.tmp.cleanup()

    def test_text_generates_wav_then_uses_existing_player_once_and_cleans_up(self):
        self.assertTrue(self.editor.test(self.point, "manual-request-0001")["submitted"])
        self.assertEqual(self.synthesis, ["欢迎"])
        self.assertEqual(self.calls[0][0]["mode"], "wav")
        self.assertFalse(Path(self.calls[0][0]["file"]).exists())
        self.assertTrue(self.editor.test(self.point, "manual-request-0001")["duplicate"])
        self.assertEqual(len(self.synthesis), 1)
        self.assertEqual(len(self.calls), 1)

    def test_imported_recording_bypasses_cloud(self):
        media = self.editor.upload(wav_bytes())
        self.assertTrue(self.editor.test(dict(self.point, mode="wav", file=media["file"]), "manual-request-0001")["submitted"])
        self.assertEqual(self.synthesis, [])
        self.assertEqual(len(self.calls), 1)

    def test_saved_text_automatic_flow_keeps_using_synthesis(self):
        self.editor.save(self.point)
        self.editor.automatic_status({"mode": "legacy_conservative", "state": "read_only"})
        self.editor.automatic_enable(True)
        result = self.editor.automatic_test({"map_name": "任务语音", "point_name": "text", "request_id": "auto-request-0001"})
        self.assertTrue(result["submitted"])
        self.assertEqual(len(self.synthesis), 1)

    def test_synthesis_failure_never_falls_back_or_retries(self):
        def fail(): raise RuntimeError("cloud failed")
        self.effect = fail
        result = self.editor.test(self.point, "manual-request-0001")
        self.assertFalse(result["submitted"])
        self.editor.test(self.point, "manual-request-0001")
        self.assertEqual(len(self.synthesis), 1)
        self.assertEqual(self.calls, [])

    def test_task_cancel_or_change_during_synthesis_blocks_playback(self):
        for state in ({"managed": False}, {"managed": True, "paused": True, "run_id": "old"},
                      {"managed": True, "paused": False, "run_id": "new", "job": "submitted_or_pending"}):
            active = {"managed": True, "paused": False, "run_id": "old", "job": "submitted_or_pending"}
            with patch("arrival_voice.legacy_auto.request_json", side_effect=[active, state]):
                result = self.editor.test(self.point, "task-request-000" + str(len(self.synthesis)))
            self.assertFalse(result["submitted"])
        self.assertEqual(self.calls, [])

    def test_task_status_unavailable_blocks_synthesis(self):
        with patch("arrival_voice.legacy_auto.request_json", side_effect=TimeoutError("status unavailable")):
            self.assertFalse(self.editor.test(self.point, "task-request-0001")["submitted"])
        self.assertEqual(self.synthesis, [])

    def test_task_active_allows_generated_wav(self):
        active = {"managed": True, "paused": False, "run_id": "run", "job": "submitted_or_pending"}
        with patch("arrival_voice.legacy_auto.request_json", return_value=active):
            self.assertTrue(self.editor.test(self.point, "task-request-0001")["submitted"])
        self.assertEqual(len(self.calls), 1)

    def test_automatic_disable_during_synthesis_blocks_playback(self):
        self.editor.automatic_status({"mode": "legacy_conservative", "state": "read_only"})
        self.editor.automatic_enable(True)
        self.effect = lambda: self.editor.automatic_enable(False)
        self.assertFalse(self.editor.test(self.point, "auto-request-0001", automatic=True)["submitted"])
        self.assertEqual(self.calls, [])

    def test_playback_budget_exhausted_blocks_robot_submission(self):
        ticks = iter([0, 341])
        with patch("arrival_voice.web.time.monotonic", side_effect=lambda: next(ticks)):
            result = self.editor.test(self.point, "manual-request-0001")
        self.assertFalse(result["submitted"])
        self.assertEqual(self.calls, [])

    def test_plain_sdk_mode_and_config_are_preserved(self):
        before = self.editor.config.read_bytes()
        self.editor.test(self.point, "manual-request-0001")
        self.assertEqual(self.editor.config.read_bytes(), before)
        self.assertEqual(self.editor.state()["tts"]["provider"], "cosyvoice")
        self.editor.synthesizer = None
        self.editor.test(self.point, "manual-request-0002")
        self.assertEqual(self.calls[-1][0]["mode"], "tts")


if __name__ == "__main__":
    unittest.main()
