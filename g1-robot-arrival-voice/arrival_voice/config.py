"""Strict point-to-announcement configuration with no implicit TTS service."""
import json
import math
import wave
from pathlib import Path


def load_config(filename):
    filename = Path(filename).resolve()
    raw = json.loads(filename.read_text(encoding="utf-8"))
    if (not isinstance(raw, dict) or type(raw.get("version")) is not int
            or raw.get("version") != 1 or not isinstance(raw.get("points"), list)):
        raise ValueError("expected version=1 and points array")
    duration = raw.get("max_task_seconds", 300)
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("max_task_seconds must be finite and positive")
    messages = {}
    for point in raw["points"]:
        if not isinstance(point, dict):
            raise ValueError("each point must be an object")
        key = (point.get("map_name"), point.get("point_name"))
        if any(not isinstance(v, str) or not v.strip() for v in key) or key in messages:
            raise ValueError("map/point must be nonempty unique strings")
        mode = point.get("mode", "tts")
        if mode == "tts":
            text = point.get("text")
            speaker = point.get("speaker_id", 0)
            if not isinstance(text, str) or not text.strip() or len(text) > 1000:
                raise ValueError("TTS text must contain 1..1000 characters")
            if type(speaker) is not int or speaker not in (0, 1):
                raise ValueError("speaker_id must be 0 (Chinese) or 1 (English)")
            messages[key] = {"mode": mode, "text": text, "speaker_id": speaker}
        elif mode == "wav":
            relative = point.get("file")
            if not isinstance(relative, str) or not relative:
                raise ValueError("WAV file must be a relative path")
            media = (filename.parent / relative).resolve()
            if Path(relative).is_absolute() or filename.parent not in media.parents:
                raise ValueError("WAV path must stay within configuration directory")
            with wave.open(str(media), "rb") as wav:
                if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getcomptype()) != (16000, 1, 2, "NONE"):
                    raise ValueError("WAV must be 16kHz mono signed 16-bit PCM")
                if not 0 < wav.getnframes() <= 16000 * 300:
                    raise ValueError("WAV duration must be >0 and <=300 seconds")
                if len(wav.readframes(wav.getnframes())) != wav.getnframes() * 2:
                    raise ValueError("truncated WAV")
            messages[key] = {"mode": mode, "file": str(media)}
        else:
            raise ValueError("mode must be tts or wav")
    return messages, duration
