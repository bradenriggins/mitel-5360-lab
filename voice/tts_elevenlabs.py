"""
tts_elevenlabs.py — TTS via the ElevenLabs streaming API.

Two modes for voice_loop:

1. Backward-compat:  tts.generate_speech_audio(text) -> NDArray[np.float32]
   Collects the full audio then returns. Used by the cached-greeting path.

2. Streaming (preferred for live response): tts.stream_ulaw_8k(text) -> Iterator[bytes]
   Yields µ-law 8 kHz bytes as ElevenLabs generates them — no buffering, no
   sample-rate conversion, no µ-law re-encode. Audio arrives at the phone within
   ~150-300 ms of the call instead of waiting for the full sentence.

API key is read from $ELEVENLABS_API_KEY first, then from the canonical env
files (Mac/VPS, mirrored).

Voice and model are env-var-tunable:
    MITEL_ELEVENLABS_VOICE_ID  (default: "JBFqnCBsd6RMkjVDRZzb" = George)
    MITEL_ELEVENLABS_MODEL_ID  (default: "eleven_turbo_v2_5")
"""

from __future__ import annotations

import audioop
import os
from pathlib import Path
from typing import Iterator

import numpy as np
from numpy.typing import NDArray

from elevenlabs.client import ElevenLabs

_KEY_FILES = [
    Path.home() / ".config" / "mitel-lab" / "elevenlabs.env",
    Path("/etc/mitel-lab.env"),
]


def _load_api_key() -> str:
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if key:
        return key
    for path in _KEY_FILES:
        if not path.is_file():
            continue
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if line.startswith("ELEVENLABS_API_KEY="):
                    val = line.split("=", 1)[1].strip()
                    if val.startswith('"') and val.endswith('"'):
                        val = val[1:-1]
                    if val:
                        return val
        except OSError:
            continue
    raise RuntimeError(
        "ELEVENLABS_API_KEY not set and no key file found at " +
        ", ".join(str(p) for p in _KEY_FILES)
    )


class ElevenLabsSpeechSynthesizer:
    # We request µ-law 8 kHz directly so voice_loop can put bytes on the RTP
    # wire with no conversion. `sample_rate` is reported as 22050 only because
    # voice_loop's backward-compat path expects a float32 + sample_rate pair
    # (used by the cached-greeting builder, which ratecvs back down to 8 kHz).
    SAMPLE_RATE = 22050
    PCM_OUTPUT_FORMAT = "pcm_22050"
    ULAW_OUTPUT_FORMAT = "ulaw_8000"

    def __init__(
        self,
        voice_id: str | None = None,
        model_id: str | None = None,
    ) -> None:
        self.voice_id = voice_id or os.environ.get(
            "MITEL_ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb"
        )
        self.model_id = model_id or os.environ.get(
            "MITEL_ELEVENLABS_MODEL_ID", "eleven_turbo_v2_5"
        )
        api_key = _load_api_key()
        self.client = ElevenLabs(api_key=api_key)
        self.sample_rate: int = self.SAMPLE_RATE

    def generate_speech_audio(self, text: str) -> NDArray[np.float32]:
        """Non-streaming path. Returns float32 PCM @ 22050 Hz."""
        text = (text or "").strip()
        if not text:
            return np.array([], dtype=np.float32)
        audio_iter = self.client.text_to_speech.stream(
            voice_id=self.voice_id,
            model_id=self.model_id,
            text=text,
            output_format=self.PCM_OUTPUT_FORMAT,
            optimize_streaming_latency="3",
        )
        pcm_bytes = b"".join(chunk for chunk in audio_iter if chunk)
        if not pcm_bytes:
            return np.array([], dtype=np.float32)
        return np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0

    def stream_ulaw_8k(self, text: str) -> Iterator[bytes]:
        """Streaming path. Yields raw µ-law 8 kHz bytes as ElevenLabs produces
        them — ready to drop straight into RTP frames.
        """
        text = (text or "").strip()
        if not text:
            return
        for chunk in self.client.text_to_speech.stream(
            voice_id=self.voice_id,
            model_id=self.model_id,
            text=text,
            output_format=self.ULAW_OUTPUT_FORMAT,
            optimize_streaming_latency="3",
        ):
            if chunk:
                yield chunk
