"""
tts_piper.py — TTS wrapper using the official piper-tts library.

Matches the interface used by tts_glados.SpeechSynthesizer so voice_loop can
swap backends transparently:
    tts.sample_rate -> int
    tts.generate_speech_audio(text) -> np.ndarray  (float32, range [-1, 1])

Voice is selected by `voice_name` (default "en_US-lessac-medium").
Models live in voice/voices/<voice>.onnx + .onnx.json (relative to this file).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from piper import PiperVoice

VOICES_DIR = Path(__file__).parent / "voices"


class PiperSpeechSynthesizer:
    def __init__(self, voice_name: str = "en_US-lessac-medium") -> None:
        model = VOICES_DIR / f"{voice_name}.onnx"
        config = VOICES_DIR / f"{voice_name}.onnx.json"
        if not model.is_file():
            raise FileNotFoundError(
                f"Piper voice model missing: {model}. "
                "Download from rhasspy/piper-voices on HuggingFace."
            )
        self.voice_name = voice_name
        self._voice = PiperVoice.load(str(model), config_path=str(config))
        # Probe sample rate via a no-op synth
        first = next(iter(self._voice.synthesize(".")), None)
        self.sample_rate: int = first.sample_rate if first is not None else 22050

    def generate_speech_audio(self, text: str) -> NDArray[np.float32]:
        if not text or not text.strip():
            return np.array([], dtype=np.float32)
        chunks = list(self._voice.synthesize(text))
        if not chunks:
            return np.array([], dtype=np.float32)
        pcm_bytes = b"".join(c.audio_int16_bytes for c in chunks)
        return (np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0)
