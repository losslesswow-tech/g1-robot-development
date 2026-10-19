"""Only G1 audio APIs. No movement client, ROS publisher, or system player."""
import json
import time
import uuid
import wave


class DryRunPlayer:
    def play(self, message, task_id):
        print(json.dumps({"dry_run": True, "task_id": task_id, "announcement": message}, ensure_ascii=False))


class UnitreePlayer:
    def __init__(self, interface, client=None, sleep=time.sleep):
        if not interface:
            raise ValueError("explicit DDS network interface required")
        if client is None:
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize
            from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
            ChannelFactoryInitialize(0, interface)
            client = AudioClient()
            client.SetTimeout(10.0)
            client.Init()
        self.client = client
        self.sleep = sleep

    def probe(self):
        code, data = self.client.GetVolume()
        if code != 0:
            raise RuntimeError("G1 GetVolume failed: {}".format(code))
        return data

    def play(self, message, task_id):
        if message["mode"] == "tts":
            code = self.client.TtsMaker(message["text"], message["speaker_id"])
            if code != 0:
                raise RuntimeError("G1 TtsMaker failed: {}".format(code))
            return
        stream_id = uuid.uuid4().hex
        # Separate application name, so cleanup never stops another audio app.
        app = "g1-arrival-voice"
        with wave.open(message["file"], "rb") as wav:
            if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getcomptype()) != (16000, 1, 2, "NONE"):
                raise ValueError("WAV format changed since configuration validation")
            try:
                while True:
                    pcm = wav.readframes(16000)
                    if not pcm:
                        break
                    code, _ = self.client.PlayStream(app, stream_id, pcm)
                    if code != 0:
                        raise RuntimeError("G1 PlayStream failed: {}".format(code))
                    self.sleep(len(pcm) / 32000.0)
            finally:
                self.client.PlayStop(app)
