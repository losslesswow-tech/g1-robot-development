"""CosyVoice HTTP synthesis only. No robot SDK, player, or navigation commands."""
import argparse
import getpass
import http.client
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import wave


MAX_AUDIO = 10 * 1024 * 1024
API_PATH = "/api/v1/services/audio/tts/SpeechSynthesizer"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def endpoint_for(base_url):
    url = urllib.parse.urlsplit(base_url)
    host = url.hostname or ""
    if (url.scheme != "https" or url.username or url.password or url.port not in (None, 443)
            or url.query or url.fragment or not (host == "dashscope.aliyuncs.com" or
            re.fullmatch(r"[a-z0-9][a-z0-9-]*\.cn-beijing\.maas\.aliyuncs\.com", host))
            or url.path.rstrip("/") not in ("", "/compatible-mode/v1", API_PATH)):
        raise ValueError("CosyVoice requires an official Beijing HTTPS endpoint")
    return urllib.parse.urlunsplit(("https", host, API_PATH, "", ""))


def audio_url(value):
    if not isinstance(value, str):
        raise ValueError("CosyVoice returned no audio URL")
    url = urllib.parse.urlsplit(value)
    # The official response example uses HTTP OSS URLs. Use TLS for the download.
    if (url.scheme not in ("http", "https") or url.username or url.password
            or url.port not in (None, 443) or url.fragment
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.oss-cn-beijing\.aliyuncs\.com", url.hostname or "")):
        raise ValueError("CosyVoice returned an unsupported audio host")
    return urllib.parse.urlunsplit(("https", url.hostname, url.path, url.query, ""))


def wav_duration(data):
    if not data or len(data) > MAX_AUDIO:
        raise ValueError("Generated WAV is empty or exceeds 10 MB")
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getcomptype()) != (16000, 1, 2, "NONE"):
                raise ValueError("CosyVoice must return 16kHz mono 16-bit PCM WAV")
            frames = wav.getnframes()
            if not 0 < frames <= 16000 * 300 or len(wav.readframes(frames)) != frames * 2:
                raise ValueError("Generated WAV is empty, too long, or truncated")
            return frames / 16000.0
    except (wave.Error, EOFError) as exc:
        raise ValueError("CosyVoice returned invalid PCM WAV") from exc


def normalize_wav(data):
    # Observed CosyVoice HTTP output has a completed PCM body with streaming
    # placeholder lengths (RIFF 0x7fffffbf / data 0x7fffff9b). Repair only this
    # exact 44-byte PCM header, after the complete bounded HTTP download.
    if (len(data) >= 44 and data[:4] == b"RIFF" and data[8:16] == b"WAVEfmt "
            and data[36:40] == b"data"
            and struct.unpack_from("<I", data, 4)[0] == 0x7FFFFFBF
            and struct.unpack_from("<I", data, 40)[0] == 0x7FFFFF9B):
        if (not 44 < len(data) <= MAX_AUDIO or (len(data) - 44) % 2
                or struct.unpack_from("<IHHIIHH", data, 16) != (16, 1, 1, 16000, 32000, 2, 16)):
            raise ValueError("Invalid CosyVoice streaming WAV header or PCM length")
        fixed = bytearray(data)
        struct.pack_into("<I", fixed, 4, len(data) - 8)
        struct.pack_into("<I", fixed, 40, len(data) - 44)
        data = bytes(fixed)
    wav_duration(data)
    return data


class CosyVoiceSynthesizer:
    def __init__(self, config, key=None, open_url=None, clock=time.monotonic):
        allowed = {"base_url", "model", "voice", "api_key_env", "timeout_seconds"}
        if not isinstance(config, dict) or set(config) - allowed:
            raise ValueError("Unsupported CosyVoice settings; keep API keys in the service environment")
        if not isinstance(config.get("base_url"), str):
            raise ValueError("CosyVoice base_url is required")
        self.endpoint = endpoint_for(config["base_url"])
        self.model = config.get("model", "cosyvoice-v3-flash")
        self.voice = config.get("voice", "longanyang")
        if not isinstance(self.model, str) or not re.fullmatch(r"cosyvoice-[a-zA-Z0-9_.-]{1,100}", self.model):
            raise ValueError("Invalid CosyVoice model ID")
        if not isinstance(self.voice, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,200}", self.voice):
            raise ValueError("Invalid CosyVoice voice ID")
        variable = config.get("api_key_env", "DASHSCOPE_API_KEY")
        if not isinstance(variable, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,99}", variable):
            raise ValueError("Invalid API key environment variable")
        self._key = os.environ.get(variable, "") if key is None else key
        if not isinstance(self._key, str) or not self._key or any(c.isspace() for c in self._key):
            raise ValueError("CosyVoice API key is missing or invalid; configure the service environment")
        self.timeout = config.get("timeout_seconds", 45)
        if type(self.timeout) not in (int, float) or not math.isfinite(self.timeout) or not 1 <= self.timeout <= 45:
            raise ValueError("CosyVoice timeout must be between 1 and 45 seconds")
        self.open_url = open_url or urllib.request.build_opener(NoRedirect()).open
        self.clock = clock

    @classmethod
    def from_file(cls, path, **kwargs):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")), **kwargs)

    def status(self):
        # Configured does not claim that cloud credentials or synthesis were verified.
        return {"provider": "cosyvoice", "model": self.model, "voice": self.voice, "configured": True}

    def _remaining(self, deadline):
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError("CosyVoice synthesis timed out; no robot audio submitted")
        return remaining

    def _read(self, request, limit, deadline):
        try:
            with self.open_url(request, timeout=self._remaining(deadline)) as response:
                declared = response.getheader("Content-Length") if hasattr(response, "getheader") else None
                if declared is not None:
                    try:
                        declared = int(declared)
                    except ValueError:
                        raise ValueError("CosyVoice returned an invalid HTTP length") from None
                    if not 0 <= declared <= limit:
                        raise ValueError("CosyVoice response exceeds the size limit")
                chunks, size = [], 0
                while True:
                    self._remaining(deadline)
                    chunk = getattr(response, "read1", response.read)(min(65536, limit + 1 - size))
                    self._remaining(deadline)
                    if not chunk:
                        if declared is not None and size != declared:
                            raise ValueError("CosyVoice HTTP response is incomplete")
                        return b"".join(chunks)
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > limit:
                        raise ValueError("CosyVoice response exceeds the size limit")
        except urllib.error.HTTPError as exc:
            # Do not reflect server messages, signed URLs, keys, or announcement text.
            raise RuntimeError("CosyVoice HTTP {}; check credentials, model and voice settings".format(exc.code)) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            raise RuntimeError("CosyVoice network request failed; no automatic retry") from None

    def synthesize(self, text, speaker_id=0):
        if not isinstance(text, str) or not text.strip() or len(text) > 1000:
            raise ValueError("TTS text must contain 1..1000 characters")
        if type(speaker_id) is not int or speaker_id not in (0, 1):
            raise ValueError("Invalid speech language")
        deadline = self.clock() + self.timeout
        payload = {"model": self.model, "input": {"text": text, "voice": self.voice,
                   "format": "wav", "sample_rate": 16000, "enable_ssml": False,
                   "language_hints": ["zh" if speaker_id == 0 else "en"]}}
        request = urllib.request.Request(self.endpoint, data=json.dumps(payload).encode("utf-8"),
                                         headers={"Authorization": "Bearer " + self._key,
                                                  "Content-Type": "application/json"})
        try:
            result = json.loads(self._read(request, 256 * 1024, deadline))
            output = result["output"]
            if output.get("finish_reason") != "stop":
                raise ValueError("CosyVoice synthesis did not finish")
            url = audio_url(output["audio"]["url"])
        except (KeyError, TypeError, AttributeError, UnicodeError, json.JSONDecodeError):
            raise ValueError("CosyVoice returned an invalid synthesis response") from None
        # Signed OSS URL authenticates the download; never forward the API key.
        data = self._read(urllib.request.Request(url), MAX_AUDIO, deadline)
        return normalize_wav(data)


def main():
    parser = argparse.ArgumentParser(description="Generate a WAV file with CosyVoice; never play robot audio")
    parser.add_argument("--config", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--key-stdin", action="store_true", help="Read the key from a pipe instead of the environment")
    args = parser.parse_args()
    try:
        key = (getpass.getpass("CosyVoice API key (hidden): ") if sys.stdin.isatty()
               else sys.stdin.readline().strip()) if args.key_stdin else None
        synthesizer = CosyVoiceSynthesizer.from_file(args.config, key=key)
        data = synthesizer.synthesize(args.text)
        destination = Path(args.output).resolve()
        # Do not overwrite a user's recording or follow an existing symlink.
        with destination.open("xb") as handle:
            handle.write(data)
        print(json.dumps(dict(synthesizer.status(), file=str(destination), bytes=len(data),
                              duration=wav_duration(data), robot_audio_submitted=False), ensure_ascii=False))
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        print("CosyVoice check failed: {}. No robot audio submitted.".format(exc), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
