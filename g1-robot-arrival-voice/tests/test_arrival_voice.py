import json
import tempfile
import unittest
import wave
from pathlib import Path

from arrival_voice.audio import UnitreePlayer
from arrival_voice.config import load_config
from arrival_voice.core import ArrivalGate, Event
from arrival_voice.observe import describe_state


class FakePlayer:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def play(self, message, task_id):
        self.calls.append((task_id, message))
        if self.fail:
            raise TimeoutError("uncertain delivery")


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Path(self.tmp.name) / "state.sqlite3"
        self.messages = {("1009", "2"): {"mode": "tts", "text": "二号点", "speaker_id": 0}}
        self.now = 0.0
        self.gate = ArrivalGate(self.database, self.messages, clock=lambda: self.now)
        self.player = FakePlayer()

    def tearDown(self):
        self.gate.close()
        self.tmp.cleanup()

    def send(self, kind, seq, task="a", point="2", map_name="1009"):
        return self.gate.handle(Event(task, map_name, point, kind, seq), self.player)

    def start(self, task="a", point="2", map_name="1009"):
        self.send("accepted", 0, task, point, map_name)
        self.send("navigating", 1, task, point, map_name)

    def test_arrival_submits_once(self):
        self.start()
        self.assertEqual(self.send("arrived", 2), "audio_submitted")
        self.send("arrived", 3)
        self.send("accepted", 4)
        self.assertEqual(len(self.player.calls), 1)

    def test_same_point_new_attempt_announces_again(self):
        for task in ("a", "b"):
            self.start(task)
            self.send("arrived", 2, task)
        self.assertEqual(len(self.player.calls), 2)

    def test_cancel_or_failure_reject_late_arrival(self):
        for kind in ("cancelled", "failed"):
            self.start(kind)
            self.send(kind, 2, kind)
            self.send("arrived", 3, kind)
        self.assertEqual(self.player.calls, [])

    def test_untracked_arrival_never_announces(self):
        self.send("arrived", 2)
        self.start()
        self.send("arrived", 3)
        self.assertEqual(self.player.calls, [])

    def test_acceptance_is_not_navigation_evidence(self):
        self.send("accepted", 0)
        self.assertEqual(self.send("arrived", 1), "no_navigation_evidence")
        self.send("navigating", 2)
        self.send("arrived", 3)
        self.assertEqual(self.player.calls, [])

    def test_wrong_point_or_map(self):
        self.start()
        self.assertEqual(self.send("arrived", 2, point="原点"), "identity_mismatch")
        self.assertEqual(self.send("arrived", 2, map_name="elsewhere"), "identity_mismatch")
        self.assertEqual(self.player.calls, [])

    def test_replacement_invalidates_old_goal(self):
        self.start("a")
        self.start("b")
        self.send("arrived", 2, "a")
        self.assertEqual(self.player.calls, [])
        self.send("arrived", 2, "b")
        self.assertEqual([x[0] for x in self.player.calls], ["b"])

    def test_stale_sequence(self):
        self.start()
        self.send("arrived", 1)
        self.send("navigating", 0)
        self.assertEqual(self.player.calls, [])

    def test_expired_task(self):
        self.start()
        self.now = 301
        self.assertEqual(self.send("arrived", 2), "expired")
        self.assertEqual(self.player.calls, [])

    def test_restart_does_not_resume_pending_task(self):
        self.start()
        self.gate.close()
        self.gate = ArrivalGate(self.database, self.messages)
        self.send("arrived", 2)
        self.assertEqual(self.player.calls, [])

    def test_restart_preserves_deduplication(self):
        self.start()
        self.send("arrived", 2)
        self.gate.close()
        self.gate = ArrivalGate(self.database, self.messages)
        self.start()
        self.send("arrived", 3)
        self.assertEqual(len(self.player.calls), 1)

    def test_uncertain_audio_is_not_retried(self):
        self.player.fail = True
        self.start()
        self.assertEqual(self.send("arrived", 2), "audio_failed_or_unknown")
        self.send("arrived", 3)
        self.assertEqual(len(self.player.calls), 1)

    def test_crash_after_claim_never_replays(self):
        self.start()

        class CrashingPlayer:
            def play(self, message, task_id):
                raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self.gate.handle(Event("a", "1009", "2", "arrived", 2), CrashingPlayer())
        self.gate.close()
        self.gate = ArrivalGate(self.database, self.messages)
        self.send("arrived", 3)
        self.assertEqual(self.player.calls, [])

    def test_unconfigured_point_is_silent(self):
        self.start(point="unknown")
        self.assertEqual(self.send("arrived", 2, point="unknown"), "unconfigured")
        self.assertEqual(self.player.calls, [])

    def test_unknown_cancellation_prevents_late_acceptance(self):
        self.send("cancelled", 2)
        self.start()
        self.send("arrived", 3)
        self.assertEqual(self.player.calls, [])

    def test_second_consumer_refused(self):
        with self.assertRaisesRegex(RuntimeError, "another consumer"):
            ArrivalGate(self.database, self.messages)

    def test_invalid_events(self):
        for kind, sequence in (("idle", 0), ("nearby", 0), ([], 0), ("arrived", True), ("arrived", -1)):
            with self.assertRaises(ValueError):
                Event("a", "1009", "2", kind, sequence)


class ConfigAndAudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self, points, **kwargs):
        self.config.write_text(json.dumps(dict(version=1, points=points, **kwargs)), encoding="utf-8")

    def wav(self, rate=16000, frames=17000):
        filename = self.root / "test.wav"
        with wave.open(str(filename), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(b"\0\0" * frames)
        return filename

    def point(self, **kwargs):
        return dict(map_name="1009", point_name="2", **kwargs)

    def test_tts_config(self):
        self.write_config([self.point(text="欢迎")])
        messages, duration = load_config(self.config)
        self.assertEqual(messages[("1009", "2")]["speaker_id"], 0)
        self.assertEqual(duration, 300)

    def test_invalid_config(self):
        for points in ([self.point(text="")], [self.point(text="欢迎", speaker_id=True)],
                       [self.point(text="欢迎"), self.point(text="重复")],
                       [self.point(mode="shell")], [self.point(mode="wav", file="../secret.wav")]):
            self.write_config(points)
            with self.assertRaises(ValueError):
                load_config(self.config)

    def test_invalid_timeout(self):
        for value in (0, -1, float("nan"), float("inf"), True):
            self.write_config([], max_task_seconds=value)
            with self.assertRaises(ValueError):
                load_config(self.config)

    def test_invalid_wav(self):
        self.wav(rate=44100)
        self.write_config([self.point(mode="wav", file="test.wav")])
        with self.assertRaises(ValueError):
            load_config(self.config)

    def test_audio_tts_only_calls_audio_api(self):
        class Client:
            def TtsMaker(self, text, speaker):
                self.request = (text, speaker)
                return 0
        client = Client()
        player = UnitreePlayer("test", client=client)
        player.play({"mode": "tts", "text": "欢迎", "speaker_id": 0}, "task")
        self.assertEqual(client.request, ("欢迎", 0))

    def test_audio_probe_failure(self):
        class Client:
            def GetVolume(self):
                return 3104, None
        with self.assertRaises(RuntimeError):
            UnitreePlayer("test", client=Client()).probe()

    def test_wav_chunks_paced_and_stopped(self):
        self.wav()
        self.write_config([self.point(mode="wav", file="test.wav")])
        messages, _ = load_config(self.config)
        class Client:
            def __init__(self):
                self.chunks = []
                self.stopped = []
            def PlayStream(self, app, stream, pcm):
                self.chunks.append((app, stream, len(pcm)))
                return 0, None
            def PlayStop(self, app):
                self.stopped.append(app)
        client, sleeps = Client(), []
        UnitreePlayer("test", client=client, sleep=sleeps.append).play(messages[("1009", "2")], "task")
        self.assertEqual([c[2] for c in client.chunks], [32000, 2000])
        self.assertEqual(len({c[1] for c in client.chunks}), 1)
        self.assertEqual(sleeps, [1.0, 0.0625])
        self.assertEqual(client.stopped, ["g1-arrival-voice"])

    def test_legacy_arrival_has_no_goal_identity(self):
        self.assertEqual(describe_state("13"), {"type": "arrived_without_goal_id", "goal_correlated": False})
        self.assertEqual(describe_state("10#2#1#1")["target"], "2")
        self.assertEqual(describe_state("12")["type"], "aligning")
        self.assertEqual(describe_state("-1")["type"], "idle")


if __name__ == "__main__":
    unittest.main()
