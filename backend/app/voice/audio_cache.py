"""Small bounded in-memory cache for synthesized patient audio.

Caches only (voice settings + approved patient text) -> audio bytes, so
repeated identical replies (greetings, retries) don't re-consume ElevenLabs
credits. It never stores student audio, microphone data, or anything else.

Bounded LRU: oldest entries are evicted; oversized clips are never cached.
Process-local and best-effort by design - a miss just means one extra API call.
"""
import hashlib
import json
import threading
from collections import OrderedDict

from app.core.config import get_settings

MAX_CACHED_CLIP_BYTES = 2_500_000  # ~2.4 MB; patient replies are short


def make_cache_key(
    voice_id: str, model_id: str, text: str, voice_settings: dict, output_format: str
) -> str:
    """Deterministic key over everything that changes the produced audio."""
    payload = json.dumps(
        {
            "voice_id": voice_id,
            "model_id": model_id,
            "text": text,
            "settings": voice_settings,
            "output_format": output_format,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AudioCache:
    def __init__(self, max_entries: int | None = None) -> None:
        self._max_entries = max_entries or get_settings().elevenlabs_cache_max_entries
        self._entries: OrderedDict[str, bytes] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> bytes | None:
        with self._lock:
            data = self._entries.get(key)
            if data is not None:
                self._entries.move_to_end(key)  # LRU touch
            return data

    def put(self, key: str, data: bytes) -> None:
        if not data or len(data) > MAX_CACHED_CLIP_BYTES or self._max_entries <= 0:
            return
        with self._lock:
            self._entries[key] = data
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def nbytes(self) -> int:
        """Approximate total bytes held by cached audio clips (read-only)."""
        with self._lock:
            return sum(len(v) for v in self._entries.values())

    @property
    def max_entries(self) -> int:
        return self._max_entries


_default_cache: AudioCache | None = None


def get_audio_cache() -> AudioCache:
    global _default_cache
    if _default_cache is None:
        _default_cache = AudioCache()
    return _default_cache
