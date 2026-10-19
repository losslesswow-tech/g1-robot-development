import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import wave
import zipfile
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from pathlib import Path

from arrival_voice.config import load_config
from arrival_voice.web import Editor, MAX_UPLOAD, handler_for


def wav_bytes(rate=16000, frames=4000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\0\0" * frames)
    return buffer.getvalue()


class Player:
    def __init__(self):
        self.calls = []
        self.fail = False

    def play(self, message, task_id):
        self.calls.append((message, task_id))
        if self.fail:
            raise TimeoutError("uncertain")


class EditorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.player = Player()
        self.config = Path(self.tmp.name) / "config.json"
        self.editor = Editor(self.config, self.player)
        self.point = {"map_name": "1009", "point_name": "展区", "mode": "tts", "text": "欢迎参观。", "speaker_id": 0}

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_and_replace_existing_point(self):
        self.editor.save(self.point)
        self.editor.save(dict(self.point, text="新的内容"))
        messages, _ = load_config(self.config)
        self.assertEqual(messages[("1009", "展区")]["text"], "新的内容")
        self.assertEqual(len(self.editor.state()["config"]["points"]), 3)
        self.assertEqual(self.player.calls, [])

    def test_invalid_draft_preserves_config(self):
        original = self.config.read_bytes()
        with self.assertRaises(ValueError):
            self.editor.save(dict(self.point, text=""))
        self.assertEqual(self.config.read_bytes(), original)

    def test_wav_import_save_export_roundtrip(self):
        data = wav_bytes()
        imported = self.editor.upload(data)
        self.assertEqual(imported["duration"], 0.25)
        self.assertEqual(imported["file"], self.editor.upload(data)["file"])
        self.assertEqual(len(list(self.editor.audio_dir.iterdir())), 1)
        point = dict(self.point, mode="wav", file=imported["file"], display_name="提示音.wav")
        self.editor.save(point)
        with zipfile.ZipFile(io.BytesIO(self.editor.export())) as archive:
            self.assertEqual(archive.read(imported["file"]), data)
            self.assertEqual(set(archive.namelist()), {"config.json", imported["file"]})
            extracted = Path(self.tmp.name) / "exported"
            archive.extractall(extracted)
        messages, _ = load_config(extracted / "config.json")
        self.assertEqual(messages[("1009", "展区")]["mode"], "wav")

    def test_reject_bad_audio(self):
        for data in (b"", b"not wav", wav_bytes(rate=44100), wav_bytes(frames=0), wav_bytes()[:-8]):
            with self.assertRaises(ValueError):
                self.editor.upload(data)
        self.assertFalse(self.editor.audio_dir.exists())

    def test_reject_large_audio(self):
        with self.assertRaises(ValueError):
            self.editor.upload(b"x" * (MAX_UPLOAD + 1))

    def test_reject_unimported_audio_path(self):
        for relative in ("../secret.wav", "/tmp/file.wav", "audio/other.wav"):
            with self.assertRaises(ValueError):
                self.editor.save(dict(self.point, mode="wav", file=relative))

    def test_manual_test_deduplicates_request(self):
        self.assertTrue(self.editor.test(self.point, "manual-request-0001")["simulation"])
        self.assertTrue(self.editor.test(self.point, "manual-request-0001")["duplicate"])
        self.assertEqual(len(self.player.calls), 1)
        self.assertNotIn("展区", [p["point_name"] for p in self.editor.state()["config"]["points"]])

    def test_manual_timeout_not_retried(self):
        self.player.fail = True
        first = self.editor.test(self.point, "manual-request-0001")
        self.assertEqual(first["status"], "failed_or_unknown")
        self.assertFalse(self.editor.test(self.point, "manual-request-0001")["submitted"])
        self.assertEqual(len(self.player.calls), 1)

    def test_concurrent_manual_test_refused(self):
        self.editor.play_lock.acquire()
        try:
            with self.assertRaisesRegex(ValueError, "尚未结束"):
                self.editor.test(self.point, "manual-request-0001")
        finally:
            self.editor.play_lock.release()
        self.assertEqual(self.player.calls, [])

    def test_automatic_requires_live_listener_enable_and_saved_point(self):
        self.editor.save(self.point)
        payload = dict(map_name="1009", point_name="展区", request_id="auto-request-0001")
        with self.assertRaises(ValueError):
            self.editor.automatic_enable(True)
        self.editor.automatic_status({"mode": "legacy_conservative", "state": "read_only"})
        with self.assertRaises(ValueError):
            self.editor.automatic_test(payload)
        self.editor.automatic_enable(True)
        self.assertTrue(self.editor.automatic_test(payload)["submitted"])
        self.assertTrue(self.editor.automatic_test(payload)["duplicate"])
        self.assertEqual(len(self.player.calls), 1)
        self.editor.automatic_enable(False)
        with self.assertRaises(ValueError):
            self.editor.automatic_test(dict(payload, request_id="auto-request-0002"))

    def test_automatic_status_expires_and_unknown_point_does_not_play(self):
        self.editor.automatic_status({"mode": "legacy_conservative", "state": "read_only"})
        self.editor.automatic_enable(True)
        with self.assertRaises(ValueError):
            self.editor.automatic_test(dict(map_name="unknown", point_name="none", request_id="auto-request-0001"))
        with patch("arrival_voice.web.time.monotonic", return_value=self.editor.automatic_seen + 9):
            self.assertFalse(self.editor.state()["automatic_arrival_connected"])
            with self.assertRaises(ValueError):
                self.editor.automatic_test(dict(map_name="1009", point_name="2", request_id="auto-request-0002"))
        self.assertEqual(self.player.calls, [])


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.player = Player()
        self.editor = Editor(Path(self.tmp.name) / "config.json", self.player)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.editor))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:{}".format(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, route, data=None, headers=None):
        request = urllib.request.Request(self.base + route, data=data, headers=headers or {})
        return urllib.request.urlopen(request, timeout=3)

    def test_page_assets_and_state(self):
        with self.request("/") as response:
            page = response.read().decode("utf-8")
            self.assertIn(self.editor.token, page)
            self.assertIn("输入讲解文字", page)
        for route in ("/app.js", "/style.css", "/api/state"):
            with self.request(route) as response:
                self.assertEqual(response.status, 200)
        self.assertEqual(self.player.calls, [])

    def test_post_requires_token_and_local_origin(self):
        for headers in ({}, {"X-Voice-Token": self.editor.token, "Origin": "https://foreign.example"}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/upload", wav_bytes(), headers)
            self.assertEqual(error.exception.code, 403)
        self.assertFalse(self.editor.audio_dir.exists())

    def test_upload_and_save_via_http(self):
        headers = {"X-Voice-Token": self.editor.token, "Origin": self.base}
        with self.request("/api/upload", wav_bytes(), headers) as response:
            media = json.load(response)
        point = {"map_name": "1009", "point_name": "音乐点", "mode": "wav", "file": media["file"]}
        with self.request("/api/save", json.dumps(point).encode(), headers) as response:
            self.assertTrue(json.load(response)["saved"])
        with self.request("/media/" + media["file"].split("/")[-1]) as response:
            self.assertEqual(response.read(), wav_bytes())
        with self.request("/api/export.zip") as response:
            self.assertIsNone(zipfile.ZipFile(io.BytesIO(response.read())).testzip())

    def test_non_object_json_and_invalid_host_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/test", b"[]", {"X-Voice-Token": self.editor.token})
        self.assertEqual(error.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/state", headers={"Host": "foreign.example"})
        self.assertEqual(error.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
