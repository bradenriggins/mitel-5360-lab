"""
voice_loop.py — AI voice loop for the Mitel 5360 lab.

One VoiceCall per inbound SIP INVITE to the AI voice extension.

Pipeline per turn:
  RTP in (G.711 µ-law 8 kHz)  -> upsample 16 kHz  -> silero-VAD turn detection
       -> faster-whisper STT  -> SSH bridge to AI backend (see BRIDGE_CMD)
       -> GLaDOS Piper TTS (22.05 kHz)  -> downsample 8 kHz  -> µ-law  -> RTP out

Imported lazily by mitel_lab.py so the heavy ML deps only load on first call.
"""

from __future__ import annotations

import audioop
import json
import os
import queue
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Iterator

import numpy as np
import onnxruntime as ort

VOICE_DIR = Path(__file__).parent.resolve()
MODELS_DIR = VOICE_DIR / "models"
sys.path.insert(0, str(VOICE_DIR))

# Required: set these env vars for your backend
VPS_HOST = os.environ.get("AI_VPS_HOST", "")       # IP/hostname of your AI backend server
VPS_USER = os.environ.get("AI_VPS_USER", "deploy")
VPS_KEY = os.environ.get("AI_VPS_KEY", "")         # path to your SSH private key
BRIDGE_CMD = os.environ.get("AI_BRIDGE_CMD", "/home/deploy/ai-phone/voice.sh")

# Persistent daemon tunnel — replaces the per-turn `ssh ... voice.sh`
# spawn. ControlMaster keeps a single TCP session warm; the daemon on the VPS
# keeps a single Claude Agent SDK session warm. Together they shave ~2-3s of
# cold-start off every turn.
DAEMON_PORT = int(os.environ.get("AI_DAEMON_PORT", "4830"))
DAEMON_LOCAL = ("127.0.0.1", DAEMON_PORT)
SSH_CONTROL_PATH = "/tmp/ai-voice-tunnel.sock"

# Lazy singletons — loaded on first call so mitel_lab.py startup stays fast.
_whisper = None
_tts = None
_vad = None
_lazy_lock = threading.Lock()
_tunnel_lock = threading.Lock()
_tunnel_proc: subprocess.Popen | None = None

# Pre-synthesized greeting (cached at warmup so first-call doesn't pay synth cost).
GREETING_TEXT = os.environ.get("AI_GREETING", "AI voice online. What do you need?")
_cached_greeting_ulaw_8k: bytes | None = None
_cached_greeting_lock = threading.Lock()


def _prebuild_greeting():
    """Synth the greeting once and cache the µ-law-8 kHz bytes."""
    global _cached_greeting_ulaw_8k
    with _cached_greeting_lock:
        if _cached_greeting_ulaw_8k is not None:
            return
        tts = _get_tts()
        audio = tts.generate_speech_audio(GREETING_TEXT)
        if audio.ndim == 2:
            audio = audio[:, 0]
        pcm16_22k = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        pcm16_8k, _ = audioop.ratecv(pcm16_22k, 2, 1, tts.sample_rate, 8000, None)
        _cached_greeting_ulaw_8k = audioop.lin2ulaw(pcm16_8k, 2)


def get_cached_greeting() -> bytes | None:
    return _cached_greeting_ulaw_8k


def _tunnel_alive() -> bool:
    """Probe the local port to check if the tunnel is live."""
    try:
        with socket.create_connection(DAEMON_LOCAL, timeout=0.5) as s:
            s.sendall(b'{"type":"ping"}\n')
            data = s.recv(64)
            return b"pong" in data
    except (OSError, socket.timeout):
        return False


def _ensure_tunnel(on_log: Callable[[str], None] | None = None) -> bool:
    """Stand up (or verify) the SSH local-forward tunnel to the AI backend daemon.

    Uses ControlMaster so the connection multiplexes — many local-forward
    sockets share one TCP+TLS session to the VPS, with ~10ms overhead per call
    instead of ~300ms.
    """
    log = on_log or (lambda *_: None)
    with _tunnel_lock:
        if _tunnel_alive():
            return True
        log("tunnel not alive, (re)establishing")
        # Tear down any stale control socket
        try:
            subprocess.run(
                [
                    "ssh", "-O", "exit",
                    "-o", f"ControlPath={SSH_CONTROL_PATH}",
                    f"{VPS_USER}@{VPS_HOST}",
                ],
                capture_output=True, timeout=3, check=False,
            )
        except Exception:
            pass
        # Establish a backgrounded, multiplexed tunnel
        try:
            result = subprocess.run(
                [
                    "ssh", "-fN",
                    "-o", "BatchMode=yes",
                    "-o", "ExitOnForwardFailure=yes",
                    "-o", "ServerAliveInterval=30",
                    "-o", "ServerAliveCountMax=3",
                    "-o", "ControlMaster=auto",
                    "-o", f"ControlPath={SSH_CONTROL_PATH}",
                    "-o", "ControlPersist=600",
                    "-o", "StrictHostKeyChecking=accept-new",
                    "-o", "IdentitiesOnly=yes",
                    "-i", VPS_KEY,
                    "-L", f"{DAEMON_PORT}:127.0.0.1:{DAEMON_PORT}",
                    "-l", VPS_USER,
                    VPS_HOST,
                ],
                capture_output=True, text=True, timeout=15, check=False,
            )
            if result.returncode != 0:
                log(f"tunnel start rc={result.returncode} stderr={result.stderr.strip()!r}")
                return False
        except Exception as exc:
            log(f"tunnel start exc: {exc!r}")
            return False
        # Wait briefly for the forward to become live
        for _ in range(20):
            if _tunnel_alive():
                log("tunnel ready")
                return True
            time.sleep(0.05)
        log("tunnel never became live")
        return False


def _get_whisper():
    global _whisper
    with _lazy_lock:
        if _whisper is None:
            from faster_whisper import WhisperModel
            _whisper = WhisperModel("base.en", device="cpu", compute_type="int8")
        return _whisper


TTS_BACKEND = os.environ.get("MITEL_VOICE_BACKEND", "elevenlabs")  # "elevenlabs" | "piper" | "glados"
PIPER_VOICE = os.environ.get("MITEL_VOICE_NAME", "en_US-lessac-medium")


def _get_tts():
    global _tts
    with _lazy_lock:
        if _tts is None:
            if TTS_BACKEND == "glados":
                from tts_glados import SpeechSynthesizer
                _tts = SpeechSynthesizer()
            elif TTS_BACKEND == "piper":
                from tts_piper import PiperSpeechSynthesizer
                _tts = PiperSpeechSynthesizer(PIPER_VOICE)
            else:  # elevenlabs (default)
                from tts_elevenlabs import ElevenLabsSpeechSynthesizer
                _tts = ElevenLabsSpeechSynthesizer()
        # Log which voice is in use (helps verify env vars reached the lab)
        backend_label = TTS_BACKEND
        voice_label = ""
        if TTS_BACKEND == "elevenlabs":
            voice_label = f" voice_id={getattr(_tts, 'voice_id', '?')}"
        elif TTS_BACKEND == "piper":
            voice_label = f" voice={getattr(_tts, 'voice_name', '?')}"
        sys.stderr.write(f"[voice_loop] TTS backend={backend_label}{voice_label}\n")
        sys.stderr.flush()
        return _tts


def _get_vad():
    global _vad
    with _lazy_lock:
        if _vad is None:
            opts = ort.SessionOptions()
            opts.log_severity_level = 4
            _vad = ort.InferenceSession(
                str(MODELS_DIR / "silero_vad.onnx"),
                sess_options=opts,
                providers=["CPUExecutionProvider"],
            )
        return _vad


def warm_up(on_log: Callable[[str], None] | None = None):
    """Preload all ML models + bring up the SSH tunnel so the first call is fast."""
    log = on_log or (lambda *_: None)
    t0 = time.time()
    _get_vad(); log(f"vad ready {time.time()-t0:.2f}s")
    t0 = time.time()
    _get_whisper(); log(f"whisper ready {time.time()-t0:.2f}s")
    t0 = time.time()
    _get_tts(); log(f"tts ready {time.time()-t0:.2f}s")
    t0 = time.time()
    _prebuild_greeting(); log(f"greeting cached in {time.time()-t0:.2f}s")
    t0 = time.time()
    ok = _ensure_tunnel(on_log=log); log(f"tunnel ready={ok} in {time.time()-t0:.2f}s")


# ─── RTP / codec helpers ──────────────────────────────────────────────────

_RTP_HEADER = struct.Struct(">BBHII")  # V/P/X/CC, M/PT, seq, ts, ssrc

def _rtp_pack(seq: int, ts: int, ssrc: int, payload: bytes, pt: int = 0) -> bytes:
    return _RTP_HEADER.pack(0x80, pt & 0x7F, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc & 0xFFFFFFFF) + payload


def _allocate_rtp_port(start: int = 35000, end: int = 39998) -> int:
    """Pick an even UDP port in [start, end] that's free."""
    for port in range(start, end, 2):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind(("0.0.0.0", port))
            s.close()
            return port
        except OSError:
            continue
        finally:
            try: s.close()
            except Exception: pass
    raise RuntimeError("no free RTP port in range")


# ─── Voice call state machine ─────────────────────────────────────────────

class VoiceCall:
    PT_PCMU = 0
    FRAME_BYTES = 160       # 20 ms of µ-law @ 8 kHz
    FRAME_DT = 0.020

    VAD_CHUNK_16K = 512     # 512 samples @ 16 kHz (32 ms)
    # RMS energy thresholds (normalized float audio in [-1, 1]).
    # Telephone-bandwidth speech sits at RMS 0.02-0.20. Silence floor ~0.002.
    SPEECH_RMS_ON = 0.020             # entering speech state
    SPEECH_RMS_OFF = 0.012            # leaving speech state (hysteresis)
    VAD_MIN_SPEECH_CHUNKS = 4         # ~130 ms of voiced
    VAD_END_SILENCE_CHUNKS = 8        # ~256 ms of trailing silence ends a turn
    SPECULATIVE_SILENCE_TRIGGER = 3   # ~100 ms of silence kicks speculative STT
    MAX_TURN_SECONDS = 12
    # Window after a TX-side speak during which we ignore inbound audio (avoids
    # the phone's mic picking up its own speaker on speakerphone and creating
    # a self-trigger loop).
    POST_SPEAK_MUTE_S = 0.25

    def __init__(
        self,
        call_id: str,
        peer_rtp_host: str,
        peer_rtp_port: int,
        local_rtp_sock: socket.socket,
        on_log: Callable[[str], None] | None = None,
    ):
        self.call_id = call_id
        self.peer_rtp = (peer_rtp_host, peer_rtp_port)
        self.sock = local_rtp_sock
        self.sock.settimeout(0.5)
        self.on_log = on_log or (lambda *_: None)

        self.ssrc = int.from_bytes(os.urandom(4), "big")
        self.tx_seq = int.from_bytes(os.urandom(2), "big")
        self.tx_ts = int.from_bytes(os.urandom(4), "big")
        self.alive = threading.Event()
        self.alive.set()

        # Gates: don't VAD/STT while we're speaking, don't speak while we're thinking
        self.tx_lock = threading.Lock()
        self.processing = threading.Event()  # set while STT+AI+TTS in flight
        # Serializes _synth_and_enqueue across threads. The TTS singleton and
        # the ratecv state machines are NOT safe under concurrent calls; greet
        # + first turn-handler can overlap if user starts talking during greet.
        self.synth_lock = threading.Lock()

        # Streaming TX: a continuous RTP sender drains tx_queue at 20 ms cadence.
        # Audio chunks from TTS are enqueued; the sender emits silence when idle.
        # `_last_tx_audio_at` tracks when we last sent real (non-silence) audio
        # so the RX side can briefly ignore self-echo on speakerphone.
        self.tx_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=1500)
        self._last_tx_audio_at = 0.0
        self.tx_worker: threading.Thread | None = None

        # Persistent TCP connection to the AI backend daemon, opened lazily on
        # first turn and kept warm for the duration of the call. The daemon's
        # handle_client loop accepts multiple {"type":"prompt"} messages per
        # connection so we save the ~50-100 ms TCP setup on every turn after
        # the first.
        self.ai_sock: socket.socket | None = None
        self._ai_reader = None
        self._ai_lock = threading.Lock()

        # Inbound buffers
        self._inbound_pcm16_8k = bytearray()
        self._turn_chunks_16k: list[np.ndarray] = []
        self._vad_state = np.zeros((2, 1, 128), dtype=np.float32)
        self._speech_chunks = 0
        self._silence_chunks = 0
        self._turn_started_at: float | None = None
        self._ratecv_state_8to16 = None
        self._ratecv_state_22to8 = None

        # Speculative STT: when the user first pauses, we kick a Whisper
        # transcription in the background using audio-so-far. By the time the
        # full silence threshold elapses, the transcript is already done — STT
        # latency disappears into the silence we were waiting for anyway. Each
        # speculative run is invalidated if the user resumes speech, and a new
        # one is scheduled at the next pause.
        self._stt_lock = threading.Lock()
        self._stt_result: str | None = None
        self._stt_running = False
        self._stt_invalidated = False
        self._stt_done_at: float | None = None

        self.tx_thread: threading.Thread | None = None
        self.rx_thread: threading.Thread | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self):
        self.rx_thread = threading.Thread(
            target=self._rx_loop, daemon=True, name=f"vc-rx-{self.call_id[:6]}"
        )
        self.rx_thread.start()
        self.tx_worker = threading.Thread(
            target=self._tx_loop, daemon=True, name=f"vc-tx-{self.call_id[:6]}"
        )
        self.tx_worker.start()
        threading.Thread(target=self._greet, daemon=True, name=f"vc-greet").start()
        # Speculative warmup: poke the AI backend daemon as soon as the call starts
        # so its KV cache is hot by the time the user finishes speaking.
        threading.Thread(target=self._warmup_ai, daemon=True).start()
        self.on_log(f"VoiceCall start peer={self.peer_rtp} local_port={self.sock.getsockname()[1]}")

    def stop(self):
        if not self.alive.is_set():
            return
        self.alive.clear()
        try:
            self.sock.close()
        except Exception:
            pass
        # Close persistent AI connection
        with self._ai_lock:
            if self.ai_sock is not None:
                try:
                    self.ai_sock.close()
                except Exception:
                    pass
                self.ai_sock = None
                self._ai_reader = None
        self.on_log(f"VoiceCall stop call_id={self.call_id[:8]}")

    def _greet(self):
        # Let the phone ACK + RTP path stabilize before talking
        time.sleep(0.8)
        if not self.alive.is_set():
            return
        # Use the pre-synthesized cached greeting if available — saves ~150 ms
        # of TTS on every call (free latency win).
        cached = get_cached_greeting()
        if cached is not None:
            self._enqueue_ulaw_bytes(cached, label="cached greeting")
            return
        try:
            self._speak(GREETING_TEXT)
        except Exception as exc:
            self.on_log(f"greet failed: {exc!r}")

    def _enqueue_ulaw_bytes(self, ulaw_8k: bytes, label: str = "audio"):
        """Chunk pre-encoded µ-law bytes and append to tx_queue."""
        n = self.FRAME_BYTES
        frames = 0
        for i in range(0, len(ulaw_8k), n):
            chunk = ulaw_8k[i : i + n]
            if len(chunk) < n:
                chunk = chunk + b"\xff" * (n - len(chunk))
            try:
                self.tx_queue.put(chunk, timeout=2.0)
            except queue.Full:
                self.on_log("tx_queue full, dropping audio")
                break
            frames += 1
        self.on_log(f"enqueued {label}: {frames} frames")

    # ── RX path: phone → us ───────────────────────────────────────────────

    def _rx_loop(self):
        rx_count = 0
        rx_skipped_tx = 0
        last_log = time.time()
        # Capture raw RTP payload bytes + decoded PCM for offline inspection
        debug_dir = Path("/tmp/voice_loop_debug")
        debug_dir.mkdir(exist_ok=True)
        raw_path = debug_dir / f"raw_payload_{self.call_id[:8]}.bin"
        pcm_path = debug_dir / f"pcm_8k_{self.call_id[:8]}.s16le"
        raw_f = open(raw_path, "wb")
        pcm_f = open(pcm_path, "wb")
        self.on_log(f"debug dumps: {raw_path} / {pcm_path}")
        try:
            while self.alive.is_set():
                try:
                    pkt, _ = self.sock.recvfrom(2048)
                except socket.timeout:
                    if time.time() - last_log > 5:
                        self.on_log(f"rx heartbeat: pkts={rx_count} skipped_tx={rx_skipped_tx}")
                        last_log = time.time()
                    continue
                except OSError:
                    break
                rx_count += 1
                if rx_count == 1:
                    # Inspect the very first packet: version, PT, header layout
                    if len(pkt) >= 12:
                        b0, b1 = pkt[0], pkt[1]
                        v = (b0 >> 6) & 0x3
                        p = (b0 >> 5) & 0x1
                        x = (b0 >> 4) & 0x1
                        cc = b0 & 0xF
                        m = (b1 >> 7) & 0x1
                        pt = b1 & 0x7F
                        self.on_log(
                            f"FIRST RTP len={len(pkt)} V={v} P={p} X={x} CC={cc} M={m} PT={pt} "
                            f"hex_head={pkt[:16].hex()}"
                        )
                if rx_count % 50 == 0:
                    self.on_log(f"rx pkts={rx_count} skipped_tx={rx_skipped_tx}")
                if len(pkt) < 12:
                    continue
                # Properly skip header including CC csrc list + extension if present
                b0, b1 = pkt[0], pkt[1]
                cc = b0 & 0xF
                x = (b0 >> 4) & 0x1
                pt = b1 & 0x7F
                hdr_len = 12 + cc * 4
                if x and len(pkt) >= hdr_len + 4:
                    ext_len_words = int.from_bytes(pkt[hdr_len + 2:hdr_len + 4], "big")
                    hdr_len += 4 + ext_len_words * 4
                payload = pkt[hdr_len:]
                if not payload:
                    continue
                # Dump raw payload bytes regardless
                raw_f.write(payload)
                # Suppress RX while we're processing a turn, or while audio is
                # actively flowing out (and briefly after — phone speakerphone
                # mic can pick up our own TTS).
                if (
                    self.processing.is_set()
                    or self.tx_lock.locked()
                    or (time.time() - self._last_tx_audio_at < self.POST_SPEAK_MUTE_S)
                ):
                    rx_skipped_tx += 1
                    continue
                try:
                    # Decode based on PT — 0 = µ-law, 8 = A-law
                    if pt == 8:
                        pcm16 = audioop.alaw2lin(payload, 2)
                    else:
                        pcm16 = audioop.ulaw2lin(payload, 2)
                except audioop.error as exc:
                    self.on_log(f"decode err pt={pt}: {exc!r}")
                    continue
                pcm_f.write(pcm16)
                self._inbound_pcm16_8k.extend(pcm16)
                self._drain_vad_chunks()
        finally:
            try: raw_f.close()
            except Exception: pass
            try: pcm_f.close()
            except Exception: pass
            self.on_log(f"rx_loop exit, total pkts={rx_count}")

    def _drain_vad_chunks(self):
        # 256 samples * 2 bytes = 512 bytes of 8k PCM16 → 512 samples @ 16k (VAD chunk)
        CHUNK_8K_BYTES = 256 * 2
        while len(self._inbound_pcm16_8k) >= CHUNK_8K_BYTES:
            piece = bytes(self._inbound_pcm16_8k[:CHUNK_8K_BYTES])
            del self._inbound_pcm16_8k[:CHUNK_8K_BYTES]
            piece_16k, self._ratecv_state_8to16 = audioop.ratecv(
                piece, 2, 1, 8000, 16000, self._ratecv_state_8to16
            )
            samples = np.frombuffer(piece_16k, dtype=np.int16).astype(np.float32) / 32768.0
            if samples.shape[0] < self.VAD_CHUNK_16K:
                samples = np.pad(samples, (0, self.VAD_CHUNK_16K - samples.shape[0]))
            samples = samples[: self.VAD_CHUNK_16K]
            self._process_vad_chunk(samples)

    def _process_vad_chunk(self, samples_16k: np.ndarray):
        # Simple RMS energy detector. Silero VAD on this model returned ~0.001
        # regardless of audio energy and was unusable; RMS-based turn detection
        # works reliably for telephone-bandwidth speech (8 kHz µ-law → 16 kHz PCM).
        energy = float(np.sqrt(np.mean(samples_16k ** 2)))

        if not hasattr(self, "_vad_logged"):
            self._vad_logged = 0
            self._was_speech = False
        self._vad_logged += 1
        if self._vad_logged % 15 == 0:
            self.on_log(
                f"chunk #{self._vad_logged} rms={energy:.4f} "
                f"speech_chunks={self._speech_chunks} silence={self._silence_chunks}"
            )

        # Hysteresis: enter speech at SPEECH_RMS_ON, leave at SPEECH_RMS_OFF
        if self._was_speech:
            is_speech = energy > self.SPEECH_RMS_OFF
        else:
            is_speech = energy > self.SPEECH_RMS_ON
        self._was_speech = is_speech

        if is_speech:
            if self._speech_chunks == 0:
                self._turn_started_at = time.time()
            self._speech_chunks += 1
            self._silence_chunks = 0
            self._turn_chunks_16k.append(samples_16k.copy())
            # User resumed talking — invalidate any in-flight speculative STT
            with self._stt_lock:
                if self._stt_running or self._stt_result is not None:
                    self._stt_invalidated = True
                    self._stt_result = None
                    self._stt_done_at = None
        else:
            if self._speech_chunks >= self.VAD_MIN_SPEECH_CHUNKS:
                # We're in a turn; count trailing silence, keep audio for context
                self._silence_chunks += 1
                self._turn_chunks_16k.append(samples_16k.copy())
                # Speculative STT trigger: 3 chunks of silence (~100ms) into a
                # genuine turn, kick a background transcription so by the time
                # the full silence window elapses, the text is ready.
                if (
                    self._silence_chunks == self.SPECULATIVE_SILENCE_TRIGGER
                    and not self._stt_running
                    and self._stt_result is None
                ):
                    self._kick_speculative_stt()
            # else: pure silence before any speech — drop the chunk

        # End-of-turn detection
        turn_too_long = (
            self._turn_started_at is not None
            and time.time() - self._turn_started_at > self.MAX_TURN_SECONDS
        )
        if (
            self._speech_chunks >= self.VAD_MIN_SPEECH_CHUNKS
            and self._silence_chunks >= self.VAD_END_SILENCE_CHUNKS
        ) or turn_too_long:
            audio = np.concatenate(self._turn_chunks_16k) if self._turn_chunks_16k else np.array([], dtype=np.float32)
            self._turn_chunks_16k.clear()
            self._speech_chunks = 0
            self._silence_chunks = 0
            self._turn_started_at = None
            if audio.size > 0:
                self.processing.set()
                # Snapshot speculative STT state. If a speculative pass is
                # still in flight, briefly wait for it — the result is usually
                # within 100-200 ms of EOS and beats a fresh transcribe.
                with self._stt_lock:
                    spec_running = self._stt_running
                    spec_text = self._stt_result
                    spec_done_at = self._stt_done_at
                deadline = time.time() + 0.6
                while spec_text is None and spec_running and time.time() < deadline:
                    time.sleep(0.02)
                    with self._stt_lock:
                        spec_running = self._stt_running
                        spec_text = self._stt_result
                        spec_done_at = self._stt_done_at
                with self._stt_lock:
                    self._stt_result = None
                    self._stt_done_at = None
                    self._stt_invalidated = False
                threading.Thread(
                    target=self._handle_turn,
                    args=(audio, spec_text, spec_done_at),
                    daemon=True,
                    name=f"vc-turn-{self.call_id[:6]}",
                ).start()

    def _kick_speculative_stt(self):
        """Start a Whisper transcription on audio-so-far. Stored result is
        consumed at end-of-turn if not invalidated by resumed speech."""
        audio = np.concatenate(self._turn_chunks_16k).copy() if self._turn_chunks_16k else None
        if audio is None or audio.size == 0:
            return
        with self._stt_lock:
            self._stt_running = True
            self._stt_invalidated = False
        threading.Thread(
            target=self._run_speculative_stt,
            args=(audio,),
            daemon=True,
            name=f"vc-stt-{self.call_id[:6]}",
        ).start()

    def _run_speculative_stt(self, audio_16k_f32: np.ndarray):
        whisper = _get_whisper()
        t0 = time.time()
        try:
            segments, _ = whisper.transcribe(
                audio_16k_f32,
                language="en",
                beam_size=1,
                vad_filter=False,
                without_timestamps=True,
                initial_prompt="Audio over a Mitel SIP phone, eight kilohertz telephony.",
            )
            text = " ".join(s.text.strip() for s in segments).strip()
        except Exception as exc:
            self.on_log(f"speculative STT err: {exc!r}")
            text = ""
        elapsed_ms = int((time.time() - t0) * 1000)
        with self._stt_lock:
            self._stt_running = False
            if self._stt_invalidated:
                self.on_log(f"speculative STT discarded ({elapsed_ms}ms) — user resumed")
                self._stt_result = None
                self._stt_done_at = None
            else:
                self._stt_result = text
                self._stt_done_at = time.time()
                self.on_log(f"speculative STT ready ({elapsed_ms}ms): {text!r}")

    # ── Turn processing ───────────────────────────────────────────────────

    def _handle_turn(
        self,
        audio_16k_f32: np.ndarray,
        speculative_text: str | None,
        speculative_done_at: float | None,
    ):
        try:
            eos_at = time.time()  # end-of-speech reference for all latency logs
            dur = audio_16k_f32.shape[0] / 16000.0
            if speculative_text is not None:
                # We already have a transcript from the speculative pass.
                # Latency for STT = 0ms (transcription happened during silence).
                text = speculative_text
                lag_ms = int((eos_at - (speculative_done_at or eos_at)) * 1000)
                self.on_log(
                    f"turn: {dur:.2f}s STT=speculative (ready {lag_ms}ms before EOS): {text!r}"
                )
            else:
                self.on_log(f"turn: {dur:.2f}s, no speculative — fresh STT")
                whisper = _get_whisper()
                t0 = time.time()
                segments, _ = whisper.transcribe(
                    audio_16k_f32,
                    language="en",
                    beam_size=1,
                    vad_filter=False,
                    without_timestamps=True,
                    initial_prompt="Audio over a Mitel SIP phone, eight kilohertz telephony.",
                )
                text = " ".join(s.text.strip() for s in segments).strip()
                stt_ms = int((time.time() - t0) * 1000)
                self.on_log(f"STT ({stt_ms}ms): {text!r}")

            if not text or len(text.split()) < 2:
                self.on_log("STT empty/short, skipping turn")
                return

            self._stream_ai_to_phone(text, stt_started_at=eos_at)
        except Exception as exc:
            self.on_log(f"turn handler exc: {exc!r}")
        finally:
            self.processing.clear()

    # ── Streaming AI bridge ────────────────────────────────────────

    _SENTENCE_END = re.compile(r"[.!?](?:\s|$)")

    def _get_ai_connection(self):
        """Get the persistent AI socket+reader, opening lazily.

        Returns (sock, reader) or (None, None) if the daemon is unreachable.
        Caller must hold self._ai_lock if reusing.
        """
        if self.ai_sock is not None and self._ai_reader is not None:
            return self.ai_sock, self._ai_reader
        if not _ensure_tunnel(on_log=self.on_log):
            return None, None
        try:
            sock = socket.create_connection(DAEMON_LOCAL, timeout=8)
            sock.settimeout(None)
            self.ai_sock = sock
            self._ai_reader = sock.makefile("rb")
            self.on_log("AI connection opened (persistent for call)")
            return self.ai_sock, self._ai_reader
        except (OSError, socket.timeout) as exc:
            self.on_log(f"AI connect failed: {exc!r}")
            return None, None

    def _close_ai_connection(self):
        if self.ai_sock is not None:
            try:
                self.ai_sock.close()
            except Exception:
                pass
        self.ai_sock = None
        self._ai_reader = None

    def _ai_stream(self, prompt: str) -> Iterator[str]:
        """Send prompt to the persistent AI connection, yield tokens.

        On I/O error, drop the connection, reconnect once, retry.
        """
        with self._ai_lock:
            attempts = 0
            while attempts < 2:
                attempts += 1
                sock, reader = self._get_ai_connection()
                if sock is None or reader is None:
                    if attempts < 2:
                        time.sleep(0.2)
                        continue
                    yield "I cannot reach the AI backend. Try again."
                    return
                try:
                    sock.sendall(
                        (json.dumps({"type": "prompt", "text": prompt}) + "\n").encode()
                    )
                except (OSError, BrokenPipeError) as exc:
                    self.on_log(f"AI send failed attempt {attempts}: {exc!r}")
                    self._close_ai_connection()
                    continue
                # Drain stream until done/error
                try:
                    while True:
                        line = reader.readline()
                        if not line:
                            # Connection closed
                            self._close_ai_connection()
                            if attempts < 2:
                                break  # retry from top of while
                            return
                        try:
                            msg = json.loads(line.decode())
                        except json.JSONDecodeError:
                            continue
                        mtype = msg.get("type")
                        if mtype == "token":
                            text = msg.get("text") or ""
                            if text:
                                yield text
                        elif mtype == "done":
                            return
                        elif mtype == "error":
                            self.on_log(f"AI err: {msg.get('message')!r}")
                            return
                except (OSError, socket.timeout) as exc:
                    self.on_log(f"AI read failed attempt {attempts}: {exc!r}")
                    self._close_ai_connection()
                    if attempts < 2:
                        continue
                    yield "I lost the connection to the AI backend. Try again."
                    return
            # Exhausted retries
            return

    def _stream_ai_to_phone(self, prompt: str, stt_started_at: float):
        """Stream tokens, split into sentences, synth each, enqueue for TX."""
        t0 = time.time()
        first_token_at: float | None = None
        first_audio_at: float | None = None
        buf = ""
        full_response = ""
        for delta in self._ai_stream(prompt):
            if first_token_at is None:
                first_token_at = time.time()
                self.on_log(
                    f"TTFT={int((first_token_at - t0) * 1000)}ms "
                    f"(end-of-speech→first-token={int((first_token_at - stt_started_at) * 1000)}ms)"
                )
            buf += delta
            full_response += delta
            # Pull off all complete sentences from buf
            while True:
                m = self._SENTENCE_END.search(buf)
                if not m:
                    break
                end = m.end()
                sentence = buf[:end].strip()
                buf = buf[end:]
                if sentence:
                    self._synth_and_enqueue(sentence)
                    if first_audio_at is None:
                        first_audio_at = time.time()
                        self.on_log(
                            f"first-audio at {int((first_audio_at - stt_started_at) * 1000)}ms "
                            f"(after first sentence: {sentence!r})"
                        )
        # Flush any trailing text without a terminator
        tail = buf.strip()
        if tail:
            self._synth_and_enqueue(tail)
            if first_audio_at is None:
                first_audio_at = time.time()
        if not full_response.strip():
            self._synth_and_enqueue("AI backend had no reply.")
            return
        self.on_log(f"response done: {full_response[:200]!r}")

    def _warmup_ai(self):
        """Open the persistent AI connection at call start so the first
        turn doesn't pay the TCP setup cost."""
        try:
            time.sleep(0.1)
            with self._ai_lock:
                sock, _ = self._get_ai_connection()
                if sock is None:
                    return
                # Ping to confirm path is alive
                sock.sendall(b'{"type":"ping"}\n')
                # Don't consume reader here — the readline in _ai_stream
                # would skip it. Instead, drain just the pong line.
                line = self._ai_reader.readline()
                self.on_log(f"AI warmup: {line.decode().strip()!r}")
        except Exception as exc:
            self.on_log(f"warmup err (non-fatal): {exc!r}")
            self._close_ai_connection()

    # ── TX path: us → phone ───────────────────────────────────────────────

    def _speak(self, text: str):
        """Backward-compat single-shot: synth full text then enqueue."""
        self._synth_and_enqueue(text)

    def _synth_and_enqueue(self, text: str):
        """Synthesize one sentence (or short phrase) and stream µ-law frames
        into tx_queue as bytes arrive. Serialized via synth_lock so sentences
        play in order; within a sentence, the first audio arrives at the phone
        within ~150-300 ms of the synth call, not after the whole sentence
        finishes generating.

        Uses the streaming `stream_ulaw_8k` interface if the TTS backend has
        one (ElevenLabs); falls back to the legacy float32+ratecv path for
        Piper / GLaDOS.
        """
        if not self.alive.is_set() or not text:
            return
        with self.synth_lock:
            tts = _get_tts()
            if not self.alive.is_set():
                return
            if hasattr(tts, "stream_ulaw_8k"):
                self._stream_synth_ulaw(tts, text)
            else:
                self._legacy_synth_ulaw(tts, text)

    def _stream_synth_ulaw(self, tts, text: str):
        """Streaming path — yields µ-law-8k bytes from the TTS as they arrive
        and pushes 20 ms frames onto tx_queue immediately. Critical for low
        first-audio latency."""
        n = self.FRAME_BYTES
        t0 = time.time()
        first_byte_at: float | None = None
        carry = b""
        total_bytes = 0
        frames = 0
        try:
            for chunk in tts.stream_ulaw_8k(text):
                if not chunk:
                    continue
                if first_byte_at is None:
                    first_byte_at = time.time()
                total_bytes += len(chunk)
                buf = carry + chunk
                idx = 0
                while len(buf) - idx >= n:
                    frame = buf[idx : idx + n]
                    idx += n
                    try:
                        self.tx_queue.put(frame, timeout=2.0)
                    except queue.Full:
                        self.on_log("tx_queue full during stream, dropping")
                        return
                    frames += 1
                carry = buf[idx:]
            # Flush remainder padded to 20 ms
            if carry:
                pad = b"\xff" * (n - len(carry))
                try:
                    self.tx_queue.put(carry + pad, timeout=2.0)
                    frames += 1
                except queue.Full:
                    pass
        except Exception as exc:
            self.on_log(f"stream_synth err: {exc!r}")
            return
        first_byte_ms = int((first_byte_at - t0) * 1000) if first_byte_at else -1
        total_ms = int((time.time() - t0) * 1000)
        audio_dur_s = total_bytes / 8000.0
        self.on_log(
            f"stream-synth({len(text)}c): {audio_dur_s:.2f}s audio, "
            f"first byte {first_byte_ms}ms, full {total_ms}ms, {frames} frames"
        )

    def _legacy_synth_ulaw(self, tts, text: str):
        """Non-streaming path for backends without stream_ulaw_8k (Piper, GLaDOS)."""
        t0 = time.time()
        audio = tts.generate_speech_audio(text)
        if audio.size == 0:
            return
        if audio.ndim == 2:
            audio = audio[:, 0]
        pcm16_22k = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        pcm16_8k, self._ratecv_state_22to8 = audioop.ratecv(
            pcm16_22k, 2, 1, tts.sample_rate, 8000, self._ratecv_state_22to8
        )
        ulaw_8k = audioop.lin2ulaw(pcm16_8k, 2)
        synth_ms = int((time.time() - t0) * 1000)
        n = self.FRAME_BYTES
        frames = 0
        for i in range(0, len(ulaw_8k), n):
            chunk = ulaw_8k[i : i + n]
            if len(chunk) < n:
                chunk = chunk + b"\xff" * (n - len(chunk))
            try:
                self.tx_queue.put(chunk, timeout=2.0)
            except queue.Full:
                self.on_log("tx_queue full, dropping audio")
                break
            frames += 1
        self.on_log(
            f"legacy-synth({len(text)}c): {len(audio)/tts.sample_rate:.2f}s "
            f"in {synth_ms}ms → {frames} frames queued"
        )

    def _tx_loop(self):
        """Continuous RTP TX. Drains tx_queue at 20 ms cadence; fills with
        µ-law silence (0xFF * 160) when the queue is empty so the stream
        stays alive and the phone keeps its jitter buffer primed.
        """
        n = self.FRAME_BYTES
        silence = b"\xff" * n
        target = time.time()
        while self.alive.is_set():
            try:
                payload = self.tx_queue.get_nowait()
                is_audio = True
            except queue.Empty:
                payload = silence
                is_audio = False
            pkt = _rtp_pack(self.tx_seq, self.tx_ts, self.ssrc, payload)
            try:
                self.sock.sendto(pkt, self.peer_rtp)
            except OSError as exc:
                self.on_log(f"tx_loop send err: {exc!r}")
                return
            if is_audio:
                self._last_tx_audio_at = time.time()
            self.tx_seq = (self.tx_seq + 1) & 0xFFFF
            self.tx_ts = (self.tx_ts + n) & 0xFFFFFFFF
            target += self.FRAME_DT
            slack = target - time.time()
            if slack > 0:
                time.sleep(slack)
        self.on_log("tx_loop exit")


# ─── Registry — accessed from mitel_lab.py SIP handler ───────────────────

_calls: dict[str, VoiceCall] = {}
_calls_lock = threading.Lock()


def start_call(
    call_id: str,
    peer_rtp_host: str,
    peer_rtp_port: int,
    on_log: Callable[[str], None] | None = None,
) -> int:
    """Allocate a local RTP socket + spawn VoiceCall. Returns local UDP port."""
    port = _allocate_rtp_port()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    call = VoiceCall(call_id, peer_rtp_host, peer_rtp_port, sock, on_log=on_log)
    call.start()
    with _calls_lock:
        _calls[call_id] = call
    return port


def end_call(call_id: str):
    with _calls_lock:
        call = _calls.pop(call_id, None)
    if call:
        call.stop()


def has_call(call_id: str) -> bool:
    with _calls_lock:
        return call_id in _calls


def active_calls() -> list[str]:
    with _calls_lock:
        return list(_calls.keys())
