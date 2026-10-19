"""Runtime-only credentials for the vendor web layer.

The upstream image embeds credential-like constants in this module.  The safe
overlay intentionally replaces them with environment lookups and does not
start the web controller by default.
"""

import os

ACCESS_KEY_ID = os.environ.get("NLS_ACCESS_KEY_ID", "")
ACCESS_KEY_SECRET = os.environ.get("NLS_ACCESS_KEY_SECRET", "")
ASR_APPKEY = os.environ.get("NLS_ASR_APPKEY", "")
TTS_APPKEY = os.environ.get("NLS_TTS_APPKEY", "")
NLS_URL = os.environ.get("NLS_URL", "wss://nls-gateway.aliyuncs.com/ws/v1")
WAKEUP_WORD = os.environ.get("WAKEUP_WORD", "你好小白")
VOSK_MODEL_PATH = os.environ.get("VOSK_MODEL_PATH", "./vosk-model-small-cn-0.22")
