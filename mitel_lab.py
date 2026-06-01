#!/usr/bin/env python3
import argparse
import glob
import html
import json
import sys
import math
import os
import random
import re
import socket
import socketserver
import ssl
import subprocess
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
import base64


_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

# ── Lab network config — override with env vars for your setup ────────────────
PHONE_MAC = os.environ.get("PHONE_MAC", "08000F69435B")
PHONE_USER = os.environ.get("PHONE_USER", "7001")
PHONE_NAME = os.environ.get("PHONE_NAME", "Mitel Lab")
LAB_HOST = os.environ.get("LAB_HOST", "192.168.4.30")   # your machine's LAN IP
PHONE_HOST = os.environ.get("PHONE_HOST", "192.168.4.33")  # Mitel 5360 LAN IP

# AI voice extension — dial this SIP extension to talk to the AI voice bridge.
# See voice/voice_loop.py for the STT → Claude → TTS pipeline.
AI_EXT_USER = os.environ.get("AI_EXT_USER", "7002")
AI_EXT_NAME = os.environ.get("AI_EXT_NAME", "AI Voice")
VOICE_DIR = os.environ.get("VOICE_DIR", os.path.join(_REPO_ROOT, "voice"))
LOG_LIMIT = 300
PHONE_WEB_USER = os.environ.get("PHONE_WEB_USER", "admin")
PHONE_WEB_PASS = os.environ.get("PHONE_WEB_PASS", "5360")
PHONE_ADMIN_TUNNEL_PORT = int(os.environ.get("MITEL_PHONE_ADMIN_PORT", "18070"))
PHONE_ADMIN_LOCAL_URL = f"http://127.0.0.1:{PHONE_ADMIN_TUNNEL_PORT}"
PHONE_ADMIN_LAN_URL = f"http://{LAB_HOST}:{PHONE_ADMIN_TUNNEL_PORT}"
STATIC_FILE_DIR = os.environ.get("STATIC_FILE_DIR", os.path.join(_REPO_ROOT, "static-files"))
RESEARCH_DIR = os.environ.get("RESEARCH_DIR", os.path.join(_REPO_ROOT, "research"))
SOUND_DIR = os.path.join(RESEARCH_DIR, "sounds")
MINET_PROBE_LOG = os.path.join(RESEARCH_DIR, "logs", "minet-probe-events.log")
MINET_CAPTURE_DIR = os.path.join(RESEARCH_DIR, "logs", "captures")
MINET_RELAY_CAPTURE_DIR = os.path.join(RESEARCH_DIR, "logs", "relay-captures")
TFTP_DIAG_LOG = os.path.join(RESEARCH_DIR, "logs", "tftp-diagnostics.log")
BOOTZ_AUDIO_LOG = os.path.join(RESEARCH_DIR, "logs", "bootz-audio.log")
BOOTZ_PHONE_HOST = os.environ.get("BOOTZ_PHONE_HOST", "192.168.0.70")
TFTP_FORCE_BLKSIZE = 0
TFTP_FORCE_WNDSIZE = 0
TFTP_SKIP_MAINIP = True  # when True, return TFTP error-1 for MainIp5360.bin so phone skips firmware load
SAFE_HTML_URL = f"http://{LAB_HOST}/app"
# Empty provisioning URL stops the standalone phone from entering "HTML App Upgrade".
NO_BOOT_HTML_URL = ""
RAW_FULLSCREEN_URL = f"http://{LAB_HOST}/raw-fullscreen"
OFFICIAL_GRM_URL = f"http://{LAB_HOST}/files/ApartmentLabGRM.official.spx"
RICH_GRM_URL = f"http://{LAB_HOST}/files/ApartmentLabGRM.rich.spx"
MITEL_SAMPLE_GRM_URL = f"http://{LAB_HOST}/files/5360-FullScreenGUISample.spx"
MITEL_SAMPLE_GRM_MCD_URL = f"http://{LAB_HOST}/db/htmlapps/apps/5360-FullScreenGUISample.spx"
MITEL_SAMPLE_FULLSCREEN_URL = f"http://{LAB_HOST}/files/5360-FullScreenSample.spx"
APARTMENT_REDIRECT_FS_URL = f"http://{LAB_HOST}/files/ApartmentLabRedirectFS.spx"
APARTMENT_NOTIFICATION_URL = f"http://{LAB_HOST}/files/ApartmentLabNotification.spx"
MITEL_HELP_URL = f"http://{LAB_HOST}/files/help5360_en.spx"
MITEL_DEFAULT_SCREENSAVER_URL = f"http://{LAB_HOST}/files/Default_ScreenSaver.spx"
LAB_HTML_URL = f"http://{LAB_HOST}/files/lab.html"
APARTMENT_FSA_URL = f"http://{LAB_HOST}/files/ApartmentLabFSA.spx"
APPLICATION_MASTER_URL = f"http://{LAB_HOST}/files/5360-applications-master.txt"
NOTIFICATION_FILE_DIR = os.path.join(RESEARCH_DIR, "notification-html-5360-lab")
APP_KEY_BINDINGS = (
    ("4", "34"),
    ("34", "64"),
)
PHONE_APP_FEATURE_CODE = "20"  # 5360 SIP web UI: HTML Application feature
MINET_REPLAY_ENABLED = False
MINET_REPLAY_LOCK = threading.Lock()
MINET_REPLAY_FRAMES = {}
MINET_REPLAY_INDEX = {}
MINET_REPLAY_META = {"loaded_at": None, "capture_dir": MINET_RELAY_CAPTURE_DIR, "counts": {}, "files": 0}


CALLER_PRESETS = {
    "lab": {"label": "Apartment Lab", "frequency": 440, "seconds": 8, "mode": "tone"},
    "red": {"label": "RED PHONE", "frequency": 660, "seconds": 12, "mode": "tone"},
    "future": {"label": "Future You", "frequency": 523, "seconds": 8, "mode": "scanner"},
    "ops": {"label": "Kitchen Operations", "frequency": 330, "seconds": 8, "mode": "pulse"},
    "spirit": {"label": "SPIRIT BOX", "frequency": 187, "seconds": 45, "mode": "spirit"},
}

AUDIO_MODES = {
    "tone": "Tone",
    "pulse": "Pulse",
    "scanner": "Radio scanner",
    "spirit": "Spirit box",
    "file": "WAV file",
}

CONTROL_ACTIONS = [
    {"id": "nativeAdminTop", "label": "Native admin", "kind": "link", "href": f"{PHONE_ADMIN_LAN_URL}/", "zone": "admin"},
    {"id": "nativeAdminLink", "label": "Open native admin", "kind": "link", "href": f"{PHONE_ADMIN_LAN_URL}/", "zone": "admin"},
    {"id": "downloadConfigLink", "label": "Download config", "kind": "link", "href": f"{PHONE_ADMIN_LAN_URL}/download.txt", "zone": "admin"},
    {"id": "bootzAudioTop", "label": "Primary BOOTZ call", "kind": "post", "endpoint": "/api/bootz-audio", "zone": "audio", "risk": "rings"},
    {"id": "bootzAudio", "label": "Call via BOOTZ", "kind": "post", "endpoint": "/api/bootz-audio", "zone": "audio", "risk": "rings"},
    {"id": "ring", "label": "Call SIP target", "kind": "post", "endpoint": "/api/ring", "zone": "audio", "risk": "rings"},
    {"id": "stop", "label": "Stop ringing", "kind": "post", "endpoint": "/api/stop", "zone": "audio"},
    {"id": "reregister", "label": "Re-register", "kind": "post", "endpoint": "/api/reregister", "zone": "admin"},
    {"id": "setMessage", "label": "Set feed text", "kind": "post", "endpoint": "/api/message", "zone": "display"},
    {"id": "setKeyApp", "label": "Set Apartment key", "kind": "post", "endpoint": "/api/apartment-key", "zone": "delivery"},
    {"id": "probeSpx", "label": "Probe SPX", "kind": "post", "endpoint": "/api/probe-spx", "zone": "delivery"},
    {"id": "launchTemporary", "label": "Launch temporary", "kind": "post", "endpoint": "/api/launch-temporary", "zone": "delivery", "risk": "html-url"},
    {"id": "restoreNativeBoot", "label": "Restore native boot", "kind": "post", "endpoint": "/api/restore-native-boot", "zone": "recovery"},
    {"id": "serveProvision", "label": "Serve on boot", "kind": "post", "endpoint": "/api/provisioning-html", "zone": "provisioning"},
    {"id": "serveMandatory", "label": "Serve mandatory", "kind": "post", "endpoint": "/api/provisioning-html", "zone": "provisioning", "risk": "html-upgrade"},
    {"id": "setHtmlApp", "label": "Set global URL", "kind": "post", "endpoint": "/api/html-app", "zone": "provisioning", "risk": "html-upgrade"},
    {"id": "restoreSipMode", "label": "Restore SIP config", "kind": "post", "endpoint": "/api/restore-sip-mode", "zone": "recovery"},
    {"id": "setAiVoiceKey", "label": "Program AI voice speed-dial", "kind": "post", "endpoint": "/api/set-ai-voice-key", "zone": "audio"},
    {"id": "clearPhoneKeys", "label": "Clear all phone keys", "kind": "post", "endpoint": "/api/clear-phone-keys", "zone": "recovery"},
    {"id": "aiVoiceStatus", "label": "AI voice status", "kind": "get", "endpoint": "/api/ai-voice-status", "zone": "audio"},
    {"id": "enableCustomGui", "label": "Enable GUI path", "kind": "post", "endpoint": "/api/enable-custom-gui", "zone": "recovery", "risk": "reboot"},
    {"id": "clearUpgrade", "label": "Clear upgrade error", "kind": "post", "endpoint": "/api/clear-html-upgrade", "zone": "recovery", "risk": "reboot"},
    {"id": "rebootPhone", "label": "Reboot phone", "kind": "post", "endpoint": "/api/reboot-phone", "zone": "recovery", "risk": "reboot"},
    {"id": "minetProbe", "label": "MiNET probe + reboot", "kind": "post", "endpoint": "/api/minet-probe", "zone": "research", "risk": "reboot"},
    {"id": "loadReplay", "label": "Load replay captures", "kind": "post", "endpoint": "/api/minet-replay-load", "zone": "research"},
    {"id": "toggleReplay", "label": "Toggle replay", "kind": "post", "endpoint": "/api/minet-replay-enable", "zone": "research"},
    {"id": "runControlCheck", "label": "Run button check", "kind": "get", "endpoint": "/api/control-check", "zone": "admin"},
]

CONTROL_ENDPOINTS = {
    "/api/ring",
    "/api/bootz-audio",
    "/api/stop",
    "/api/reregister",
    "/api/message",
    "/api/html-app",
    "/api/apartment-key",
    "/api/probe-spx",
    "/api/launch-temporary",
    "/api/provisioning-html",
    "/api/enable-custom-gui",
    "/api/clear-html-upgrade",
    "/api/reboot-phone",
    "/api/restore-native-boot",
    "/api/minet-probe",
    "/api/minet-replay-load",
    "/api/minet-replay-enable",
    "/api/restore-sip-mode",
    "/api/control-check",
    "/api/set-ai-voice-key",
    "/api/ai-voice-status",
    "/api/clear-phone-keys",
}


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.events = []
        self.registered = None
        self.last_options = None
        self.invites = {}
        self.call_tones = {}
        self.message = "Apartment hotline armed"
        self.config_html_url = NO_BOOT_HTML_URL
        self.config_html_mandatory = "0"
        self.config_sip_mode = "sip"
        self.config_key_app = "FullScreenSample"
        self.minet_probe = None
        self.mitel_probe_hits = []
        self.notification_pending = False
        self.notification_seq = 0
        self.notification_last_pull = None
        self.notification_last_fire = None
        self.notification_uri = "application.htm"
        self.sip_notify_ack_body = ""
        self.sip_notify_ack_content_type = "text/plain"

    def log(self, kind, message):
        entry = (time.strftime("%H:%M:%S"), kind, message)
        with self.lock:
            self.events.append(entry)
            self.events = self.events[-LOG_LIMIT:]
        print(f"{entry[0]} {kind} {message}", flush=True)

    def snapshot(self):
        with self.lock:
            return {
                "events": list(self.events),
                "registered": self.registered,
                "last_options": self.last_options,
                "invites": dict(self.invites),
                "call_tones": dict(self.call_tones),
                "message": self.message,
                "config_html_url": self.config_html_url,
                "config_html_mandatory": self.config_html_mandatory,
                "config_sip_mode": self.config_sip_mode,
                "config_key_app": self.config_key_app,
                "minet_probe": dict(self.minet_probe) if self.minet_probe else None,
                "mitel_probe_hits": list(self.mitel_probe_hits),
                "notification_pending": self.notification_pending,
                "notification_seq": self.notification_seq,
                "notification_last_pull": self.notification_last_pull,
                "notification_last_fire": self.notification_last_fire,
                "notification_uri": self.notification_uri,
                "sip_notify_ack_body": self.sip_notify_ack_body,
                "sip_notify_ack_content_type": self.sip_notify_ack_content_type,
            }

    def mark_call(self, call_id, status):
        with self.lock:
            if call_id in self.invites:
                self.invites[call_id] = status

    def clear_call_tone(self, call_id):
        with self.lock:
            self.call_tones.pop(call_id, None)

    def drop_call(self, call_id):
        with self.lock:
            self.invites.pop(call_id, None)
            self.call_tones.pop(call_id, None)


state = State()


def sip_header(message, name):
    match = re.search(rf"^{re.escape(name)}:\s*(.+)$", message, re.I | re.M)
    return match.group(1).strip() if match else ""


def sip_short_header(message, long_name, short_name):
    return sip_header(message, long_name) or sip_header(message, short_name)


def contact_uri(contact):
    match = re.search(r"<([^>]+)>", contact)
    uri = match.group(1) if match else contact.strip()
    match = re.search(r"sips?:([^@;>]+@)?([^:;>]+)(?::(\d+))?", uri)
    if not match:
        return None
    return {
        "uri": uri,
        "host": match.group(2),
        "port": int(match.group(3) or "5060"),
    }


def parse_sdp_audio(body):
    host = None
    port = None
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("c=IN IP4 "):
            host = line.split()[-1]
        elif line.startswith("m=audio "):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    port = int(parts[1])
                except ValueError:
                    pass
    if host and port:
        return host, port
    return None


def ulaw_sample(sample):
    bias = 0x84
    clip = 32635
    sample = max(-clip, min(clip, sample))
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    sample += bias
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        mask >>= 1
        exponent -= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


def list_sound_files():
    os.makedirs(SOUND_DIR, exist_ok=True)
    files = []
    for path in sorted(glob.glob(os.path.join(SOUND_DIR, "*.wav"))):
        files.append(os.path.basename(path))
    return files


def wav_to_8k_samples(filename, max_seconds=120):
    safe_name = os.path.basename(filename or "")
    if not safe_name:
        raise ValueError("sound file required")
    path = os.path.join(SOUND_DIR, safe_name)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    with wave.open(path, "rb") as wav:
        if wav.getcomptype() != "NONE":
            raise ValueError("only uncompressed PCM WAV files are supported")
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        source_rate = wav.getframerate()
        total_frames = min(wav.getnframes(), int(source_rate * max_seconds))
        raw = wav.readframes(total_frames)
    frame_width = channels * sample_width
    if frame_width <= 0 or source_rate <= 0:
        raise ValueError("invalid WAV parameters")
    target_frames = max(1, int(total_frames * 8000 / source_rate))

    def read_channel(offset):
        if sample_width == 1:
            return (raw[offset] - 128) << 8
        if sample_width == 2:
            return int.from_bytes(raw[offset:offset + 2], "little", signed=True)
        if sample_width == 3:
            value = int.from_bytes(raw[offset:offset + 3], "little", signed=False)
            if value & 0x800000:
                value -= 0x1000000
            return value >> 8
        if sample_width == 4:
            return int.from_bytes(raw[offset:offset + 4], "little", signed=True) >> 16
        raise ValueError("unsupported WAV sample width")

    out = []
    for target_index in range(target_frames):
        source_index = min(total_frames - 1, int(target_index * source_rate / 8000))
        base = source_index * frame_width
        value = 0
        for channel in range(channels):
            value += read_channel(base + channel * sample_width)
        out.append(int(value / channels))
    return out


def pseudo_noise(sample_index):
    value = (sample_index * 1103515245 + 12345) & 0x7fffffff
    return ((value >> 8) / 4194304.0) - 256.0


def generated_audio_sample(mode, sample_index, frequency):
    t = sample_index / 8000.0
    if mode == "pulse":
        gate = 1 if int(t * 7) % 2 == 0 else 0
        tone = math.sin(2 * math.pi * frequency * t)
        click = pseudo_noise(sample_index) * 20
        return int(gate * 8500 * tone + click)
    if mode == "scanner":
        segment = sample_index // 960
        step = [220, 330, 440, 550, 660, 880, 990][segment % 7]
        sweep = step + 90 * math.sin(2 * math.pi * 0.7 * t)
        gate = 0.25 + 0.75 * (1 if int(t * 12) % 3 else 0)
        return int(gate * 6500 * math.sin(2 * math.pi * sweep * t) + pseudo_noise(sample_index) * 18)
    if mode == "spirit":
        segment = sample_index // 640
        rnd = random.Random(segment * 7919 + 17)
        gate = rnd.random() > 0.18
        base = rnd.choice([95, 123, 151, 187, 221, 277, 333])
        formant_a = base * rnd.choice([2.0, 2.4, 3.1, 3.7])
        formant_b = base * rnd.choice([4.3, 5.1, 6.2, 7.4])
        wobble = 1 + 0.09 * math.sin(2 * math.pi * (0.8 + rnd.random()) * t)
        voice = (
            math.sin(2 * math.pi * base * wobble * t)
            + 0.45 * math.sin(2 * math.pi * formant_a * t)
            + 0.22 * math.sin(2 * math.pi * formant_b * t)
        )
        chop = 1 if int(t * rnd.choice([9, 11, 13, 17])) % 2 else 0.35
        noise = pseudo_noise(sample_index) * 22
        return int((voice * 4300 * chop if gate else 0) + noise)
    return int(9000 * math.sin(2 * math.pi * frequency * t))


def send_audio_rtp(host, port, seconds=8, frequency=440, mode="tone", sound=""):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ssrc = random.randrange(1, 2**32 - 1)
    seq = random.randrange(0, 65535)
    timestamp = random.randrange(0, 2**32 - 1)
    started = time.time()
    samples_sent = 0
    file_samples = []
    failed = False
    try:
        if mode == "file" and sound:
            file_samples = wav_to_8k_samples(sound, max_seconds=seconds)
            if not file_samples:
                mode = "tone"
        while time.time() - started < seconds:
            payload = bytearray()
            for i in range(160):
                sample_index = samples_sent + i
                if file_samples:
                    value = file_samples[sample_index % len(file_samples)]
                else:
                    value = generated_audio_sample(mode, sample_index, frequency)
                payload.append(ulaw_sample(value))
            header = (
                bytes([0x80, 0x00])
                + seq.to_bytes(2, "big")
                + timestamp.to_bytes(4, "big")
                + ssrc.to_bytes(4, "big")
            )
            sock.sendto(header + payload, (host, port))
            seq = (seq + 1) % 65536
            timestamp = (timestamp + 160) % 2**32
            samples_sent += 160
            time.sleep(0.02)
    except Exception as exc:
        failed = True
        state.log("rtp", f"audio send failed mode={mode}: {exc!r}")
    finally:
        sock.close()
    if not failed:
        state.log("rtp", f"sent {seconds}s {mode} audio to {host}:{port}")


def make_response(request, code, reason, extra_headers=None, body=""):
    via = sip_short_header(request, "Via", "v")
    from_h = sip_short_header(request, "From", "f")
    to_h = sip_short_header(request, "To", "t")
    call_id = sip_header(request, "Call-ID") or sip_short_header(request, "Call-ID", "i")
    cseq = sip_header(request, "CSeq")
    headers = [
        f"SIP/2.0 {code} {reason}",
        f"Via: {via}",
        f"From: {from_h}",
        f"To: {to_h}",
        f"Call-ID: {call_id}",
        f"CSeq: {cseq}",
        "Server: Mitel apartment lab",
    ]
    if extra_headers:
        headers.extend(extra_headers)
    headers.append(f"Content-Length: {len(body.encode('utf-8'))}")
    return ("\r\n".join(headers) + "\r\n\r\n" + body).encode("utf-8")


# ─── AI voice extension ───────────────────────────────────────────────────
#
# When the phone presses the AI voice speed-dial key, it INVITEs
# sip:AI_EXT_USER@LAB_HOST. We answer 200 OK with our SDP, allocate a local RTP port,
# and hand the call to voice_loop.VoiceCall which runs STT/Claude/TTS until
# the phone hangs up.

_voice_loop_mod = None
_voice_loop_lock = threading.Lock()


def _get_voice_loop():
    global _voice_loop_mod
    with _voice_loop_lock:
        if _voice_loop_mod is None:
            if VOICE_DIR not in sys.path:
                sys.path.insert(0, VOICE_DIR)
            import voice_loop as vl  # type: ignore
            _voice_loop_mod = vl
            # Warm up models in the background so the first call is fast
            def _warm():
                try:
                    vl.warm_up(on_log=lambda m: state.log("voice-warm", m))
                except Exception as exc:
                    state.log("voice-warm", f"warm-up err: {exc!r}")
            threading.Thread(target=_warm, daemon=True, name="voice-warmup").start()
        return _voice_loop_mod


def _is_ai_voice_invite(request_text):
    first = request_text.splitlines()[0] if request_text else ""
    if not first.startswith("INVITE "):
        return False
    return f"sip:{AI_EXT_USER}@" in first or f"sip:{AI_EXT_USER}:" in first


def _ai_voice_sdp_answer(our_rtp_port):
    return "\r\n".join([
        "v=0",
        f"o=ai-voice 0 0 IN IP4 {LAB_HOST}",
        "s=AI Voice",
        f"c=IN IP4 {LAB_HOST}",
        "t=0 0",
        f"m=audio {our_rtp_port} RTP/AVP 0",
        "a=rtpmap:0 PCMU/8000",
        "a=ptime:20",
        "a=sendrecv",
        "",
    ])


def _ai_voice_200_ok(request_text, our_rtp_port):
    via = sip_short_header(request_text, "Via", "v") or ""
    from_h = sip_short_header(request_text, "From", "f") or ""
    to_h = sip_short_header(request_text, "To", "t") or ""
    call_id = sip_header(request_text, "Call-ID") or sip_short_header(request_text, "Call-ID", "i") or ""
    cseq = sip_header(request_text, "CSeq") or "1 INVITE"
    if ";tag=" not in to_h:
        to_h = f"{to_h};tag=aivoice{random.randrange(10**8, 10**9):x}"
    sdp = _ai_voice_sdp_answer(our_rtp_port)
    headers = [
        "SIP/2.0 200 OK",
        f"Via: {via}",
        f"From: {from_h}",
        f"To: {to_h}",
        f"Call-ID: {call_id}",
        f"CSeq: {cseq}",
        f"Contact: <sip:{AI_EXT_USER}@{LAB_HOST}:5060>",
        "Allow: INVITE, ACK, CANCEL, BYE, OPTIONS",
        "Server: Mitel apartment lab / AI voice bridge",
        "Content-Type: application/sdp",
        f"Content-Length: {len(sdp.encode('utf-8'))}",
    ]
    return ("\r\n".join(headers) + "\r\n\r\n" + sdp).encode("utf-8")


def _handle_ai_voice_invite(sock, addr, text):
    call_id = sip_header(text, "Call-ID") or f"aivoice-{random.randrange(10**12):x}"
    body = text.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in text else ""
    audio = parse_sdp_audio(body)
    if not audio:
        sock.sendto(make_response(text, 488, "Not Acceptable Here"), addr)
        state.log("sip->", f"{addr[0]}:{addr[1]} 488 INVITE 7002 missing SDP")
        return
    peer_host, peer_port = audio
    try:
        vl = _get_voice_loop()
        our_port = vl.start_call(
            call_id, peer_host, peer_port,
            on_log=lambda m, _cid=call_id: state.log("voice", f"[{_cid[:8]}] {m}"),
        )
    except Exception as exc:
        state.log("voice", f"start_call failed: {exc!r}")
        sock.sendto(make_response(text, 500, "Server Internal Error"), addr)
        return
    # 100 Trying first, then 200 OK with SDP. Phone will ACK and start RTP.
    sock.sendto(make_response(text, 100, "Trying"), addr)
    sock.sendto(_ai_voice_200_ok(text, our_port), addr)
    state.log(
        "sip->",
        f"{addr[0]}:{addr[1]} 200 INVITE answered, ext={AI_EXT_USER} "
        f"peer_rtp={peer_host}:{peer_port} local_rtp={our_port}",
    )


def sip_udp_server(bind_host, bind_port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind_host, bind_port))
    state.log("sip", f"UDP listening on {bind_host}:{bind_port}")
    while True:
        data, addr = sock.recvfrom(8192)
        text = data.decode("utf-8", "replace")
        first = text.splitlines()[0] if text.splitlines() else ""
        state.log("sip<-", f"{addr[0]}:{addr[1]} {first}")
        if first.startswith("REGISTER "):
            contact = sip_header(text, "Contact") or f"<sip:{PHONE_USER}@{addr[0]}:{addr[1]}>"
            parsed = contact_uri(contact) or {"uri": contact, "host": addr[0], "port": addr[1]}
            state.registered = {
                "addr": addr,
                "contact": contact,
                "uri": parsed["uri"],
                "host": parsed["host"],
                "port": parsed["port"],
                "at": time.strftime("%H:%M:%S"),
            }
            expires = sip_header(text, "Expires") or "3600"
            response = make_response(text, 200, "OK", [
                f"Contact: {contact};expires={expires}",
                "Expires: 3600",
            ])
            sock.sendto(response, addr)
            state.log("sip->", f"{addr[0]}:{addr[1]} 200 REGISTER accepted as {PHONE_USER}")
        elif first.startswith("OPTIONS "):
            response = make_response(text, 200, "OK", [
                "Allow: INVITE, ACK, CANCEL, BYE, OPTIONS, REGISTER",
                "Accept: application/sdp",
            ])
            sock.sendto(response, addr)
            state.log("sip->", f"{addr[0]}:{addr[1]} 200 OPTIONS")
        elif _is_ai_voice_invite(text):
            _handle_ai_voice_invite(sock, addr, text)
        elif first.startswith("INVITE "):
            # Unknown extension — politely decline so the phone stops retrying.
            sock.sendto(make_response(text, 404, "Not Found"), addr)
            state.log("sip->", f"{addr[0]}:{addr[1]} 404 INVITE for unknown extension")
        elif first.startswith("ACK "):
            # Phone acknowledged our 200 OK. Voice loop is already running.
            call_id = sip_header(text, "Call-ID") or ""
            state.log("sip<-", f"{addr[0]}:{addr[1]} ACK call={call_id[:24]}")
        elif first.startswith("SIP/2.0"):
            call_id = sip_header(text, "Call-ID")
            cseq = sip_header(text, "CSeq")
            if call_id and call_id in state.invites:
                state.mark_call(call_id, f"{first} / {cseq}")
            if call_id and (" 481 " in first or " 487 " in first):
                state.drop_call(call_id)
            if " 200 " in first and call_id:
                ack = build_ack(text)
                target = state.registered["addr"] if state.registered else addr
                sock.sendto(ack, target)
                state.log("sip->", f"{target[0]}:{target[1]} ACK for answered call")
                body = text.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in text else ""
                audio = parse_sdp_audio(body)
                if audio:
                    call_audio = state.snapshot()["call_tones"].get(call_id, {})
                    start_thread(
                        send_audio_rtp,
                        audio[0],
                        audio[1],
                        int(call_audio.get("seconds", 8)),
                        int(call_audio.get("frequency", 440)),
                        call_audio.get("mode", "tone"),
                        call_audio.get("sound", ""),
                    )
        elif first.startswith("BYE "):
            call_id = sip_header(text, "Call-ID")
            response = make_response(text, 200, "OK")
            sock.sendto(response, addr)
            if call_id:
                state.drop_call(call_id)
                if _voice_loop_mod is not None and _voice_loop_mod.has_call(call_id):
                    try:
                        _voice_loop_mod.end_call(call_id)
                    except Exception as exc:
                        state.log("voice", f"end_call err: {exc!r}")
            state.log("sip->", f"{addr[0]}:{addr[1]} 200 BYE")
        else:
            # Surface unknown SIP methods (notably NOTIFY) for reverse engineering.
            if first.startswith("NOTIFY "):
                event = sip_header(text, "Event")
                ctype = sip_header(text, "Content-Type")
                body = text.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in text else ""
                preview = body[:160].replace("\r", " ").replace("\n", " ")
                state.log(
                    "sip-notify",
                    f"{addr[0]}:{addr[1]} event={event or '-'} content_type={ctype or '-'} body_len={len(body)} body_preview={preview!r}",
                )
                snap = state.snapshot()
                ack_body = snap.get("sip_notify_ack_body", "")
                if ack_body:
                    response = make_response(
                        text,
                        200,
                        "OK",
                        [f"Content-Type: {snap.get('sip_notify_ack_content_type', 'text/plain')}"],
                        ack_body,
                    )
                else:
                    response = make_response(text, 200, "OK")
            else:
                response = make_response(text, 200, "OK")
            sock.sendto(response, addr)


def sip_tcp_server(bind_host, bind_port):
    server = socketserver.ThreadingTCPServer((bind_host, bind_port), SipTCPHandler)
    server.allow_reuse_address = True
    state.log("sip", f"TCP listening on {bind_host}:{bind_port}")
    server.serve_forever()


class SipTCPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data = self.request.recv(8192)
        if not data:
            return
        text = data.decode("utf-8", "replace")
        first = text.splitlines()[0] if text.splitlines() else ""
        state.log("sip<-tcp", f"{self.client_address[0]}:{self.client_address[1]} {first}")
        if first.startswith("REGISTER "):
            response = make_response(text, 200, "OK", ["Expires: 3600"])
        else:
            response = make_response(text, 200, "OK", ["Allow: INVITE, ACK, CANCEL, BYE, OPTIONS, REGISTER"])
        self.request.sendall(response)


def build_invite(target_host, target_port, user=PHONE_USER, caller="Apartment Lab", audio=None):
    call_id = f"{random.randrange(10**8, 10**9)}@mitel-lab"
    branch = f"z9hG4bK{random.randrange(10**8, 10**9)}"
    tag = f"lab{random.randrange(10000, 99999)}"
    sdp = "\r\n".join([
        "v=0",
        f"o=mitel-lab 1 1 IN IP4 {LAB_HOST}",
        "s=Apartment Lab Call",
        f"c=IN IP4 {LAB_HOST}",
        "t=0 0",
        "m=audio 40000 RTP/AVP 0 8 101",
        "a=rtpmap:0 PCMU/8000",
        "a=rtpmap:8 PCMA/8000",
        "a=rtpmap:101 telephone-event/8000",
        "a=sendrecv",
        "",
    ])
    headers = [
        f"INVITE sip:{user}@{target_host}:{target_port} SIP/2.0",
        f"Via: SIP/2.0/UDP {LAB_HOST}:5060;branch={branch}",
        "Max-Forwards: 70",
        f"From: \"{caller}\" <sip:lab@{LAB_HOST}>;tag={tag}",
        f"To: <sip:{user}@{target_host}>",
        f"Call-ID: {call_id}",
        "CSeq: 1 INVITE",
        f"Contact: <sip:lab@{LAB_HOST}:5060>",
        "Allow: INVITE, ACK, CANCEL, BYE, OPTIONS",
        "Content-Type: application/sdp",
        f"Content-Length: {len(sdp.encode('utf-8'))}",
    ]
    state.invites[call_id] = "sent"
    if audio:
        state.call_tones[call_id] = audio
    return call_id, ("\r\n".join(headers) + "\r\n\r\n" + sdp).encode("utf-8")


def build_ack(response):
    via = sip_short_header(response, "Via", "v")
    from_h = sip_short_header(response, "From", "f")
    to_h = sip_short_header(response, "To", "t")
    call_id = sip_header(response, "Call-ID")
    headers = [
        f"ACK sip:{PHONE_USER}@{PHONE_HOST}:5060 SIP/2.0",
        f"Via: {via}",
        f"From: {from_h}",
        f"To: {to_h}",
        f"Call-ID: {call_id}",
        "CSeq: 1 ACK",
        "Content-Length: 0",
    ]
    return ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8")


def ring_phone(caller="Apartment Lab", frequency=440, seconds=8, mode="tone", sound=""):
    snap = state.snapshot()
    reg = snap["registered"]
    target_host = PHONE_HOST
    target_port = 5060
    if reg:
        target_host = reg["host"] or reg["addr"][0]
        target_port = reg["port"] or reg["addr"][1]
    call_id, invite = build_invite(
        target_host,
        target_port,
        caller=caller,
        audio={"frequency": frequency, "seconds": seconds, "mode": mode, "sound": sound},
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(invite, (target_host, target_port))
    sock.close()
    state.log("sip->", f"{target_host}:{target_port} INVITE call-id={call_id} audio={mode}")
    return call_id


def start_bootz_audio_call(caller="SPIRIT BOX", frequency=187, seconds=45, mode="spirit"):
    if mode not in ("tone", "scanner", "spirit"):
        mode = "spirit"
    seconds = max(2, min(int(seconds), 120))
    frequency = max(40, min(int(frequency), 1800))
    os.makedirs(os.path.dirname(BOOTZ_AUDIO_LOG), exist_ok=True)
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        "bootz",
        "python",
        r"C:\Users\brade\mitel_spirit_box.py",
        "--phone",
        BOOTZ_PHONE_HOST,
        "--mode",
        mode,
        "--seconds",
        str(seconds),
        "--frequency",
        str(frequency),
        "--caller",
        caller[:40],
        "--answer-timeout",
        "45",
    ]
    with open(BOOTZ_AUDIO_LOG, "ab", buffering=0) as handle:
        handle.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(cmd)} ---\n".encode())
        subprocess.Popen(cmd, stdout=handle, stderr=subprocess.STDOUT)
    state.log("bootz-audio", f"started {mode} call to {BOOTZ_PHONE_HOST} seconds={seconds}")
    return f"started BOOTZ {mode} audio call to {BOOTZ_PHONE_HOST}"


def stop_calls():
    snap = state.snapshot()
    reg = snap["registered"]
    if not reg:
        state.log("sip", "stop requested but phone is not registered")
        return "not registered"
    target = reg["addr"]
    sent = 0
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for call_id, status in snap["invites"].items():
        if "180" in status or status == "sent" or "100" in status:
            branch = f"z9hG4bKcancel{random.randrange(10**8, 10**9)}"
            msg = "\r\n".join([
                f"CANCEL sip:{PHONE_USER}@{target[0]}:{target[1]} SIP/2.0",
                f"Via: SIP/2.0/UDP {LAB_HOST}:5060;branch={branch}",
                "Max-Forwards: 70",
                f"From: \"Apartment Lab\" <sip:lab@{LAB_HOST}>;tag=labstop",
                f"To: <sip:{PHONE_USER}@{target[0]}>",
                f"Call-ID: {call_id}",
                "CSeq: 1 CANCEL",
                f"Contact: <sip:lab@{LAB_HOST}:5060>",
                "Content-Length: 0",
                "", "",
            ]).encode("utf-8")
            sock.sendto(msg, target)
            sent += 1
            state.mark_call(call_id, "cancel sent")
            state.clear_call_tone(call_id)
            state.log("sip->", f"{target[0]}:{target[1]} CANCEL call-id={call_id}")
    sock.close()
    return f"sent {sent} cancel(s)"


def first(form, key, default=""):
    values = form.get(key)
    return values[0] if values else default


def bounded_int(value, low, high, default):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def phone_request(path, data=None, timeout=5):
    upstreams = [PHONE_ADMIN_LOCAL_URL, f"http://{PHONE_HOST}"]
    body = None
    headers = {}
    if data is not None:
        body = data.encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    token = base64.b64encode(f"{PHONE_WEB_USER}:{PHONE_WEB_PASS}".encode("ascii")).decode("ascii")
    headers["Authorization"] = f"Basic {token}"
    last_error = None
    for base_url in upstreams:
        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
        request = Request(url, data=body, headers=headers, method="POST" if data is not None else "GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "replace")
        except Exception as exc:
            last_error = exc
    raise last_error


def phone_reregister():
    phone_request("RegistrationStatus", "RegStatus2=1&cmd1=Re-register&helpuprl=0")
    state.log("phone", "requested SIP re-registration")
    return "requested SIP re-registration"


def set_phone_html_url(target_url):
    data = urlencode({
        "html_url": target_url,
        "htmlpuseraccess": "1",
        "cmd1": "Apply",
        "helpuprl": "0",
    })
    phone_request("FeatureConfig", data, timeout=10)
    state.log("phone", f"set HTML application URL to {target_url}")
    return f"set HTML application URL to {target_url}"


def reboot_phone():
    data = urlencode({
        "regduration": "3600",
        "sessiontimer": "1800",
        "voicemailkey": "",
        "ipport": "5060",
        "btimeout": "3",
        "longcontact": "0",
        "langcode": "en_US",
        "e911num": "",
        "e911svrip": "0.0.0.0",
        "e911port": "5060",
        "stunip": "",
        "gruu_ctl": "1",
        "proxyrequire_ctl": "0",
        "wanurl": "",
        "dlpEnable": "0",
        "wanaddr1": "",
        "batphoneflag": "0",
        "batphonedialaddr": "operator@example.com",
        "batphonedialstyl": "0",
        "poundkeydial": "1",
        "dialtonekey": "12",
        "blf_pickup": "*98",
        "rss_feed": f"http://{LAB_HOST}/feed",
        "tls_root_cert_url": "",
        "htmlpuseraccess": "1",
        "remote_reboot": "1",
        "cmd1": "Reboot",
        "helpuprl": "0",
    })
    phone_request("AdvancedFeatures", data, timeout=15)
    state.log("phone", "requested remote reboot")
    return "requested remote reboot"


def enable_custom_gui(reboot=False):
    # Standalone SIP safety: avoid html_filename/mandatory boot paths because
    # they are processed as HTML App Upgrade and can drop to Communication Error.
    set_provisioning_html(NO_BOOT_HTML_URL, "0")
    set_phone_html_url(NO_BOOT_HTML_URL)
    set_html_app_keys("FullScreenSample", bind_by_name=True)
    message = "standalone-safe mode: boot HTML disabled; OPEN LAB UI bound by app name"
    if reboot:
        reboot_phone()
        message += "; reboot requested"
    state.log("phone", message)
    return message


def restore_native_boot():
    set_provisioning_html(NO_BOOT_HTML_URL, "0")
    try:
        set_phone_html_url(NO_BOOT_HTML_URL)
        phone_note = "phone Feature Config cleared"
    except Exception as exc:
        phone_note = f"phone unreachable ({exc!r}); lab provision cleared only"
    state.log("phone", f"HTML boot/upgrade URL cleared; {phone_note}")
    return f"HTML boot/upgrade URL cleared; {phone_note}"


def clear_html_upgrade_error(reboot=False):
    set_provisioning_html(NO_BOOT_HTML_URL, "0")
    set_phone_html_url(NO_BOOT_HTML_URL)
    message = "cleared html_filename and HTML Application URL (mandatory=0)"
    if reboot:
        reboot_phone()
        message += "; reboot requested"
    state.log("phone", message)
    return message


def fetch_phone_download_config(timeout=10):
    return phone_request("download.txt", timeout=timeout)


def control_check():
    post_actions = [item for item in CONTROL_ACTIONS if item.get("kind") == "post"]
    get_actions = [item for item in CONTROL_ACTIONS if item.get("kind") == "get"]
    link_actions = [item for item in CONTROL_ACTIONS if item.get("kind") == "link"]
    missing_endpoints = sorted(
        {
            item["endpoint"]
            for item in post_actions + get_actions
            if item.get("endpoint") not in CONTROL_ENDPOINTS
        }
    )
    native_admin = {
        "ok": False,
        "url": PHONE_ADMIN_LAN_URL,
        "local_url": PHONE_ADMIN_LOCAL_URL,
        "model": "",
        "ip": "",
        "sip_mode": "",
        "error": "",
    }
    try:
        config = fetch_phone_download_config(timeout=4)
        native_admin["ok"] = "<Parameter" in config
        model_match = re.search(r'<Parameter\s+Model="([^"]+)"', config)
        ip_match = re.search(r"<ipadr>(.*?)</ipadr>", config, re.S)
        sip_match = re.search(r"<sip_mode>(.*?)</sip_mode>", config, re.S)
        native_admin["model"] = model_match.group(1) if model_match else ""
        native_admin["ip"] = html.unescape(ip_match.group(1).strip()) if ip_match else ""
        native_admin["sip_mode"] = html.unescape(sip_match.group(1).strip()) if sip_match else ""
    except Exception as exc:
        native_admin["error"] = repr(exc)
    return {
        "ok": not missing_endpoints and native_admin["ok"],
        "actions": CONTROL_ACTIONS,
        "counts": {
            "total": len(CONTROL_ACTIONS),
            "post": len(post_actions),
            "get": len(get_actions),
            "links": len(link_actions),
        },
        "missing_endpoints": missing_endpoints,
        "native_admin": native_admin,
    }


def current_phone_html_url():
    config = fetch_phone_download_config()
    match = re.search(r"<html_filename>(.*?)</html_filename>", config, re.S)
    return html.unescape(match.group(1).strip()) if match else ""


def set_html_app_keys(app_address, label="OPEN LAB UI", bind_by_name=True):
    """Program Fea=20 keys. bind_by_name=True uses installed app name (pmksmode=1)."""
    pmksmode = "1" if bind_by_name else "0"
    for endpoint, key_number in APP_KEY_BINDINGS:
        data = urlencode({
            "pmknum": key_number,
            "pmkfeature": PHONE_APP_FEATURE_CODE,
            "userlist": "0",
            "pmkDes": label[:20],
            "pmksaddr": app_address,
            "pmksmode": pmksmode,
            "pmksaddr2": "",
            "pmksmode2": "0",
            "cmd1": "Apply",
            "pmkuprl": "",
            "helpuprl": "0",
        })
        phone_request(f"ProgramKeyConfig{endpoint}", data, timeout=10)
    mode_label = "app name" if bind_by_name else "URL"
    state.log("phone", f"set HTML app keys ({mode_label}) to {app_address}")
    return f"set HTML app keys ({mode_label}) to {app_address}"


# 5360 SIP-UI programmable-key feature codes (verified from ProgramKeyConfig HTML):
#   0 = not programmed
#   1 = Speed Dial Key       ← what we want for the AI voice extension
#   5 = Shared Line
#   6..13 = Line 1..Line 8
#   20 = HTML Application
KEY_FEATURE_NOT_PROGRAMMED = "0"
KEY_FEATURE_SPEED_DIAL = "1"
KEY_FEATURE_HTML_APP = "20"


def _program_key(key_number, feature, label="", addr="", mode="0", addr2="", mode2="0"):
    """POST to /ProgramKeyConfig{N} where N is the actual key number (1..32).
    The Mitel 5360 web admin uses the URL path to identify the target key — the
    pmknum form field is just metadata.
    """
    data = urlencode({
        "pmknum": str(key_number),
        "pmkfeature": str(feature),
        "userlist": "0",
        "pmkDes": (label or "")[:20],
        "pmksaddr": addr,
        "pmksmode": mode,
        "pmksaddr2": addr2,
        "pmksmode2": mode2,
        "cmd1": "Apply",
        "pmkuprl": "",
        "helpuprl": "0",
    })
    phone_request(f"ProgramKeyConfig{key_number}", data, timeout=10)


def set_ai_voice_speed_dial_key(key_number=2, label="AI VOICE"):
    """Program a Mitel 5360 programmable key as a Speed Dial calling ext 7002.

    Defaults to key 2 (Page 1, Key01 right side — typically empty on a 5360).
    Fea=1 == Speed Dial Key. pmksaddr is the SIP target. The phone needs to be
    registered with the lab for the call to actually go through; the key itself
    is programmed independently.
    """
    sip_target = f"sip:{AI_EXT_USER}@{LAB_HOST}"
    _program_key(
        key_number=key_number,
        feature=KEY_FEATURE_SPEED_DIAL,
        label=label,
        addr=sip_target,
        mode="0",
    )
    state.log("phone", f"set AI voice speed-dial: key={key_number} fea=1 -> {sip_target}")
    return f"programmed key {key_number} as AI voice speed-dial -> {sip_target}"


def clear_phone_key(key_number):
    """Set a programmable key to 'not programmed' (Fea=0). Useful for cleaning
    out stale HTML-app or Speed-Dial bindings from earlier experiments."""
    _program_key(key_number=key_number, feature=KEY_FEATURE_NOT_PROGRAMMED)
    state.log("phone", f"cleared key {key_number}")
    return f"cleared key {key_number}"


def clear_phone_keys_range(start=1, end=32, skip_lines=True):
    """Clear all programmable keys in [start..end]. With skip_lines=True (default),
    preserves keys whose current binding is a Line key (Fea=6..13). Returns a
    summary of which keys were touched.

    Because we can't cheaply read each key's current feature without fetching the
    whole ProgramKeyConfig{N} page per key, this just clears everything in range
    unless skip_lines is False. Lines are normally bound via XML provisioning
    anyway, so the phone will rebuild them on next config refresh.
    """
    touched = []
    for n in range(start, end + 1):
        try:
            _program_key(key_number=n, feature=KEY_FEATURE_NOT_PROGRAMMED)
            touched.append(n)
        except Exception as exc:
            state.log("phone", f"clear key {n} failed: {exc!r}")
    state.log("phone", f"cleared {len(touched)} keys in range {start}..{end}")
    return f"cleared {len(touched)} keys in range {start}..{end}"


def set_apartment_key_url(target_url, label="OPEN LAB UI"):
    bind_by_name = not (
        target_url.startswith("http://")
        or target_url.startswith("https://")
        or target_url.startswith("tftp://")
    )
    return set_html_app_keys(target_url, label=label, bind_by_name=bind_by_name)


def install_fsa_sip_workflow(
    load_target="fullscreen",
    key_app="FullScreenSample",
    use_master=False,
    reboot=False,
):
    """Safe SIP path: never put .spx in html_filename (causes Communication Error).

    On standalone 5360, Feature Config html_url mirrors into html_filename and triggers
    HTML App Upgrade. Use key URL launch (lab.html) instead; keep boot URLs empty.
    """
    set_provisioning_html(NO_BOOT_HTML_URL, "0")
    set_phone_html_url(NO_BOOT_HTML_URL)
    app_name = "FullScreenSample"
    if key_app.startswith("http://") or key_app.startswith("https://"):
        # URL mode is unreliable on 5360 standalone SIP; preserve for explicit URL requests only.
        set_html_app_keys(key_app, bind_by_name=False)
        app_name = key_app
    elif key_app in ("lab", "labhtml", "fullscreen", "fullscreensample"):
        set_html_app_keys("FullScreenSample", bind_by_name=True)
        app_name = "FullScreenSample"
    else:
        set_html_app_keys(key_app, bind_by_name=True)
        app_name = key_app
        state.log(
            "phone",
            f"bound app-name {key_app!r} on standalone SIP",
        )
    with state.lock:
        state.config_key_app = app_name
    phone_reregister()
    message = f"safe SIP: html_filename empty; keys={app_name}(app-name)"
    if reboot:
        reboot_phone()
        message += "; reboot requested"
    state.log("phone", message)
    return message


def set_provisioning_html(target_url, mandatory="0"):
    with state.lock:
        state.config_html_url = target_url
        state.config_html_mandatory = "1" if str(mandatory) == "1" else "0"
    state.log("provision", f"serve html={target_url} mandatory={state.config_html_mandatory}")
    return f"serving provisioning HTML URL {target_url} mandatory={state.config_html_mandatory}"


def append_minet_probe_log(message):
    os.makedirs(os.path.dirname(MINET_PROBE_LOG), exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S %z")
    with open(MINET_PROBE_LOG, "a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {message}\n")


def set_sip_mode(mode, reason="manual"):
    if mode not in ("sip", "minet"):
        raise ValueError("mode must be 'sip' or 'minet'")
    with state.lock:
        state.config_sip_mode = mode
        if mode == "sip":
            state.minet_probe = None
    state.log("provision", f"serve sip_mode={mode} reason={reason}")
    append_minet_probe_log(f"config sip_mode={mode} reason={reason}")
    return f"serving sip_mode={mode}"


def _safe_peer_tag(addr):
    return f"{addr[0].replace(':', '_').replace('/', '_')}-{addr[1]}"


def _write_capture(lane, port, addr, data):
    if not data:
        return ""
    try:
        os.makedirs(MINET_CAPTURE_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S")
        path = os.path.join(
            MINET_CAPTURE_DIR,
            f"{stamp}-{lane}-p{port}-{_safe_peer_tag(addr)}.bin",
        )
        with open(path, "wb") as handle:
            handle.write(data)
        return path
    except Exception as exc:
        state.log("capture", f"failed to write {lane} capture: {exc!r}")
        return ""


def _capture_port_from_name(name):
    match = re.search(r"-p(\d+)-", name or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def load_minet_replay_frames(capture_dir=MINET_RELAY_CAPTURE_DIR):
    pattern = os.path.join(capture_dir, "*controller_to_phone.bin")
    paths = sorted(glob.glob(pattern))
    by_port = {}
    for path in paths:
        port = _capture_port_from_name(os.path.basename(path))
        if port is None:
            continue
        try:
            with open(path, "rb") as handle:
                blob = handle.read()
        except Exception as exc:
            state.log("replay", f"skip unreadable {path}: {exc!r}")
            continue
        if not blob:
            continue
        by_port.setdefault(port, []).append(blob)
    counts = {str(port): len(frames) for port, frames in sorted(by_port.items())}
    with MINET_REPLAY_LOCK:
        MINET_REPLAY_FRAMES.clear()
        MINET_REPLAY_FRAMES.update(by_port)
        MINET_REPLAY_INDEX.clear()
        MINET_REPLAY_INDEX.update({port: 0 for port in by_port})
        MINET_REPLAY_META.update(
            {
                "loaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "capture_dir": capture_dir,
                "counts": counts,
                "files": len(paths),
            }
        )
    state.log("replay", f"loaded replay frames ports={counts or '{}'} files={len(paths)} dir={capture_dir}")
    return {
        "ok": True,
        "capture_dir": capture_dir,
        "ports": counts,
        "files": len(paths),
    }


def set_minet_replay_enabled(enable):
    global MINET_REPLAY_ENABLED
    MINET_REPLAY_ENABLED = bool(enable)
    state.log("replay", f"enabled={MINET_REPLAY_ENABLED}")
    return {"ok": True, "enabled": MINET_REPLAY_ENABLED}


def next_minet_replay_frame(port):
    if not MINET_REPLAY_ENABLED:
        return None
    with MINET_REPLAY_LOCK:
        frames = MINET_REPLAY_FRAMES.get(port) or []
        if not frames:
            return None
        idx = MINET_REPLAY_INDEX.get(port, 0)
        if idx >= len(frames):
            idx = 0
        frame = frames[idx]
        MINET_REPLAY_INDEX[port] = idx + 1
        return frame, idx + 1, len(frames)


def maybe_send_minet_replay(sock, port, addr, lane):
    replay = next_minet_replay_frame(port)
    if not replay:
        return False
    frame, seq, total = replay
    try:
        sock.sendall(frame)
        state.log(
            "mitel->",
            f"{addr[0]}:{addr[1]} port={port} replay lane={lane} frame={seq}/{total} bytes={len(frame)}",
        )
        record_mitel_probe_hit("replay-out", port, addr, frame, note=f"lane={lane} frame={seq}/{total}")
        return True
    except Exception as exc:
        state.log("replay", f"send failed port={port} peer={addr[0]}:{addr[1]} exc={exc!r}")
        return False


def build_minet_vers_ack(recv_data):
    """Build a msg_type=3 ack in response to a port-6800 msg_type=2 Vers frame.

    We echo the phone's header fields with msg_type=3 and swap the IP to the
    controller address. The version_count and command-name fields are zeroed so
    the phone does not expect a version payload in the response.
    """
    if len(recv_data) < 80:
        return b""
    if int.from_bytes(recv_data[0:4], "big") != 2:
        return b""
    controller_ip = bytes(int(x) for x in LAB_HOST.split("."))
    resp = bytearray(80)
    resp[0:4]  = (3).to_bytes(4, "big")   # msg_type = 3 (ack)
    resp[4:8]  = recv_data[4:8]            # echo word_at_4
    resp[8:12] = recv_data[8:12]           # echo seq_id
    # bytes 12-27: zero (no controller MAC; echo product code)
    resp[20:24] = recv_data[20:24]         # echo word_at_20 (product code)
    resp[28:32] = controller_ip            # controller IP in the IP field
    resp[32:68] = recv_data[32:68]         # echo words 32-67 unchanged
    resp[68:72] = recv_data[68:72]         # echo cookie
    resp[72:76] = b"\x00\x00\x00\x00"     # version_count = 0 in response
    resp[76:80] = b"\x00\x00\x00\x00"     # no command name
    return bytes(resp)


def build_minet_controller_hello():
    """Minimal controller-hello to send proactively on the 6801 TLS channel.

    The phone resets 6801 after TLS handshake if the server does not speak
    first.  This sends a bare msg_type=1 header so the phone knows a
    controller is present, using the last Vers frame data if available.
    """
    with state.lock:
        last_vers = getattr(state, "_last_vers_data", None)
    if last_vers and len(last_vers) >= 80:
        ack = build_minet_vers_ack(last_vers)
        if ack:
            return ack
    # Fallback: minimal 8-byte greeting with controller IP
    controller_ip = bytes(int(x) for x in LAB_HOST.split("."))
    msg = bytearray(32)
    msg[0:4] = (1).to_bytes(4, "big")   # msg_type = 1 (hello)
    msg[28:32] = controller_ip
    return bytes(msg)


def decode_6800_packet(data):
    """Structured view of a port-6800 cleartext MiNET-style packet.

    Field layout was inferred from repeated captures (phone MAC at offset 12,
    phone IPv4 at offset 28, phone TCP source port echoed at offset 32, an
    ASCII length-prefixed string at offset 72). Unknown words are exposed as
    raw hex/decimal so a human can correlate them across captures.
    """
    out = {"length": len(data)}
    if len(data) < 76:
        out["error"] = "shorter than the 76-byte header observed in captures"
        out["hex"] = data.hex()
        return out
    out["msg_type"] = int.from_bytes(data[0:4], "big")
    out["word_at_4"] = int.from_bytes(data[4:8], "big")
    out["sequence_or_id"] = int.from_bytes(data[8:12], "big")
    out["phone_mac"] = data[12:18].hex()
    out["pad_18"] = data[18:20].hex()
    out["word_at_20"] = int.from_bytes(data[20:24], "big")
    out["word_at_24"] = int.from_bytes(data[24:28], "big")
    out["phone_ipv4"] = ".".join(str(b) for b in data[28:32])
    out["phone_tcp_src_port"] = int.from_bytes(data[32:34], "big")
    out["pad_34"] = data[34:36].hex()
    for offset in (36, 40, 44, 48, 52, 56, 60, 64, 68):
        out[f"word_at_{offset}"] = int.from_bytes(data[offset:offset + 4], "big")
    string_length = int.from_bytes(data[72:76], "big")
    out["string_length"] = string_length
    string_end = 76 + max(0, min(string_length, len(data) - 76))
    raw_string = data[76:string_end]
    out["string"] = raw_string.decode("ascii", "replace") if raw_string else ""
    out["tail_bytes"] = max(0, len(data) - string_end)
    out["tail_hex"] = data[string_end:].hex() if string_end < len(data) else ""
    return out


def record_mitel_probe_hit(lane, port, addr, data=b"", note=""):
    preview = data[:80]
    ascii_preview = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in preview)
    capture_path = _write_capture(lane, port, addr, data) if data else ""
    decoded = None
    if port == 6800 and data:
        try:
            decoded = decode_6800_packet(data)
        except Exception as exc:
            decoded = {"error": repr(exc)}
    hit = {
        "at": time.strftime("%H:%M:%S"),
        "lane": lane,
        "port": port,
        "peer": f"{addr[0]}:{addr[1]}",
        "bytes": len(data),
        "hex": preview.hex(),
        "ascii": ascii_preview,
        "note": note,
        "capture": os.path.basename(capture_path) if capture_path else "",
        "decoded": decoded,
    }
    with state.lock:
        state.mitel_probe_hits.append(hit)
        state.mitel_probe_hits = state.mitel_probe_hits[-60:]
    append_minet_probe_log(
        f"{lane} peer={hit['peer']} port={port} bytes={len(data)} "
        f"hex={hit['hex']} ascii={json.dumps(ascii_preview)} "
        f"capture={hit['capture'] or '-'} note={note}"
    )
    if decoded and "error" not in decoded:
        append_minet_probe_log(
            f"{lane}-decoded peer={hit['peer']} port={port} "
            f"msg_type={decoded.get('msg_type')} seq={decoded.get('sequence_or_id')} "
            f"mac={decoded.get('phone_mac')} ip={decoded.get('phone_ipv4')} "
            f"phone_src_port={decoded.get('phone_tcp_src_port')} "
            f"string_len={decoded.get('string_length')} "
            f"string={json.dumps(decoded.get('string', ''))} "
            f"tail_bytes={decoded.get('tail_bytes')} tail_hex={decoded.get('tail_hex')}"
        )


def finish_minet_probe_after(seconds, started_at):
    time.sleep(seconds)
    with state.lock:
        current = state.minet_probe
        should_restore = bool(current and current.get("started_at") == started_at)
    if should_restore:
        set_sip_mode("sip", "minet-probe-timeout")
        state.log("minet-probe", f"lab config auto-restored to SIP after {seconds}s")


def start_minet_probe(seconds=180, reboot=False):
    seconds = max(30, min(int(seconds), 600))
    started_at = time.time()
    with state.lock:
        state.config_sip_mode = "minet"
        state.minet_probe = {
            "active": True,
            "started_at": started_at,
            "started": time.strftime("%H:%M:%S"),
            "expires_at": started_at + seconds,
            "seconds": seconds,
            "reboot_requested": bool(reboot),
        }
    append_minet_probe_log(f"probe start seconds={seconds} reboot={bool(reboot)} phone={PHONE_HOST} lab={LAB_HOST}")
    state.log("minet-probe", f"serving sip_mode=minet for {seconds}s reboot={bool(reboot)}")
    start_thread(finish_minet_probe_after, seconds, started_at)
    message = f"MiNET probe armed for {seconds}s"
    if reboot:
        message = f"{message}; reboot requested"
        reboot_phone()
    return {
        "ok": True,
        "message": message,
        "seconds": seconds,
        "reboot": bool(reboot),
        "log": MINET_PROBE_LOG,
    }


def app_url_for_target(target):
    if target in ("none", "clear", "empty"):
        return NO_BOOT_HTML_URL
    if target == "safe":
        return SAFE_HTML_URL
    if target == "rawfs":
        return RAW_FULLSCREEN_URL
    if target == "official":
        return OFFICIAL_GRM_URL
    if target == "rich":
        return RICH_GRM_URL
    if target == "sample":
        return MITEL_SAMPLE_GRM_URL
    if target == "mcdsample":
        return MITEL_SAMPLE_GRM_MCD_URL
    if target == "fullscreen":
        return MITEL_SAMPLE_FULLSCREEN_URL
    if target == "apartmentfsa":
        return APARTMENT_FSA_URL
    if target == "lab":
        return LAB_HTML_URL
    if target == "redirectfs":
        return APARTMENT_REDIRECT_FS_URL
    if target == "notification":
        return APARTMENT_NOTIFICATION_URL
    if target == "help":
        return MITEL_HELP_URL
    if target == "screensaver":
        return MITEL_DEFAULT_SCREENSAVER_URL
    if target in ("master", "appmaster"):
        return APPLICATION_MASTER_URL
    raise ValueError(f"unknown app target {target!r}")


def is_upgrade_prone_html_url(target_url):
    """On 5360 standalone SIP, any non-empty html_filename enters HTML upgrade flow."""
    return bool((target_url or "").strip())


def probe_spx_delivery(target, wait_seconds=5):
    base_url = app_url_for_target(target)
    filename = os.path.basename(urlparse(base_url).path)
    if not filename.lower().endswith(".spx"):
        raise ValueError("probe target must be an SPX package")
    separator = "&" if "?" in base_url else "?"
    target_url = f"{base_url}{separator}probe={int(time.time())}"
    marker = len(state.snapshot()["events"])
    try:
        set_phone_html_url(target_url)
        deadline = time.time() + wait_seconds
        fetched = False
        fetch_event = ""
        while time.time() < deadline:
            for event in state.snapshot()["events"][marker:]:
                message = event[2]
                if f"GET /files/{filename}" in message:
                    fetched = True
                    fetch_event = message
                    break
            if fetched:
                break
            time.sleep(0.25)
        restored = set_phone_html_url(SAFE_HTML_URL)
        current_url = current_phone_html_url()
        state.log("probe", f"{target} fetched={fetched} restored={current_url}")
        return {
            "ok": True,
            "target": target,
            "url": target_url,
            "fetched": fetched,
            "event": fetch_event,
            "restore": restored,
            "current_url": current_url,
        }
    except Exception:
        try:
            set_phone_html_url(SAFE_HTML_URL)
        finally:
            raise


def launch_html_temporarily(target, hold_seconds=30):
    base_url = app_url_for_target(target)
    target_url = base_url
    if urlparse(base_url).path.lower().endswith(".spx"):
        separator = "&" if "?" in base_url else "?"
        target_url = f"{base_url}{separator}launch={int(time.time())}"
    set_phone_html_url(target_url)
    state.log("launch", f"holding {target_url} for {hold_seconds}s before restore")

    def restore_later():
        time.sleep(hold_seconds)
        try:
            set_phone_html_url(SAFE_HTML_URL)
            state.log("launch", f"restored safe URL after {hold_seconds}s")
        except Exception as exc:
            state.log("launch", f"restore failed: {exc!r}")

    start_thread(restore_later)
    return {
        "ok": True,
        "target": target,
        "url": target_url,
        "hold_seconds": hold_seconds,
        "restore_url": SAFE_HTML_URL,
    }


def status_payload():
    snap = state.snapshot()
    events = [
        {"time": t, "lane": lane, "message": message}
        for t, lane, message in snap["events"][-120:]
    ]
    return {
        "phone": {
            "host": PHONE_HOST,
            "mac": PHONE_MAC,
            "user": PHONE_USER,
            "lab_host": LAB_HOST,
            "native_admin_url": PHONE_ADMIN_LAN_URL,
            "native_admin_local_url": PHONE_ADMIN_LOCAL_URL,
            "bootz_host": BOOTZ_PHONE_HOST,
        },
        "registered": snap["registered"],
        "message": snap["message"],
        "events": events,
        "active_calls": snap["invites"],
        "presets": CALLER_PRESETS,
        "audio_modes": AUDIO_MODES,
        "sound_files": list_sound_files(),
        "control_actions": CONTROL_ACTIONS,
        "app_targets": {
            "safe": SAFE_HTML_URL,
            "rawfs": RAW_FULLSCREEN_URL,
            "official": OFFICIAL_GRM_URL,
            "rich": RICH_GRM_URL,
            "sample": MITEL_SAMPLE_GRM_URL,
            "mcdsample": MITEL_SAMPLE_GRM_MCD_URL,
            "fullscreen": MITEL_SAMPLE_FULLSCREEN_URL,
            "apartmentfsa": APARTMENT_FSA_URL,
            "lab": LAB_HTML_URL,
            "redirectfs": APARTMENT_REDIRECT_FS_URL,
            "notification": APARTMENT_NOTIFICATION_URL,
            "help": MITEL_HELP_URL,
            "screensaver": MITEL_DEFAULT_SCREENSAVER_URL,
        },
        "provisioning": {
            "html_url": snap["config_html_url"],
            "mandatory": snap["config_html_mandatory"],
            "sip_mode": snap["config_sip_mode"],
            "minet_probe": snap["minet_probe"],
            "mitel_probe_hits": snap["mitel_probe_hits"],
        },
        "notification": {
            "pending": snap["notification_pending"],
            "seq": snap["notification_seq"],
            "last_pull": snap["notification_last_pull"],
            "last_fire": snap["notification_last_fire"],
            "uri": snap["notification_uri"],
        },
        "sip_notify_ack": {
            "body_len": len(snap["sip_notify_ack_body"] or ""),
            "content_type": snap["sip_notify_ack_content_type"],
        },
        "minet_replay": {
            "enabled": MINET_REPLAY_ENABLED,
            "loaded_at": MINET_REPLAY_META.get("loaded_at"),
            "capture_dir": MINET_REPLAY_META.get("capture_dir"),
            "ports": MINET_REPLAY_META.get("counts", {}),
            "files": MINET_REPLAY_META.get("files", 0),
        },
    }


def rss_feed():
    message = html.escape(state.snapshot()["message"])
    now = time.strftime("%a, %d %b %Y %H:%M:%S %z")
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>Mitel Apartment Lab</title>
<link>http://{LAB_HOST}/</link>
<description>Apartment hotline feed</description>
<item>
<title>{message}</title>
<description>{message}</description>
<pubDate>{now}</pubDate>
</item>
</channel>
</rss>
'''


def arm_notification_invoke(relative_uri="application.htm"):
    uri = (relative_uri or "application.htm").strip().lstrip("/")
    with state.lock:
        state.notification_pending = True
        state.notification_seq += 1
        state.notification_uri = uri
        state.notification_last_fire = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "uri": uri,
            "seq": state.notification_seq,
        }
        seq = state.notification_seq
    state.log("notify", f"armed invoke seq={seq} uri=/{uri}")
    return {"ok": True, "seq": seq, "uri": f"/{uri}"}


def set_sip_notify_ack(body="", content_type="text/plain"):
    with state.lock:
        state.sip_notify_ack_body = body or ""
        state.sip_notify_ack_content_type = (content_type or "text/plain").strip() or "text/plain"
    state.log(
        "sip-notify",
        f"ack-template set body_len={len(state.sip_notify_ack_body)} content_type={state.sip_notify_ack_content_type}",
    )
    return {
        "ok": True,
        "body_len": len(state.sip_notify_ack_body),
        "content_type": state.sip_notify_ack_content_type,
    }


def notification_pull_response(peer_ip):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with state.lock:
        pending = state.notification_pending
        uri = state.notification_uri
        seq = state.notification_seq
        state.notification_last_pull = {"at": now, "peer": peer_ip, "pending": pending, "seq": seq}
        if pending:
            state.notification_pending = False
    if pending:
        state.log("notify", f"{peer_ip} pull -> INVOKE seq={seq} uri=/{uri}")
        return f'''<?xml version="1.0"?>
<Notification-Pull_Response>
  <service-require>
    <notification>
      <invoke>
        <relative-uri>{html.escape(uri)}</relative-uri>
      </invoke>
    </notification>
  </service-require>
</Notification-Pull_Response>
'''
    state.log("notify", f"{peer_ip} pull -> idle seq={seq}")
    return '''<?xml version="1.0"?>
<Notification-Pull_Response>
  <service-require/>
</Notification-Pull_Response>
'''


def phone_app(params=None):
    params = params or {}
    snap = state.snapshot()
    action_status = ""
    action = first(params, "action")
    if action == "ring":
        preset = first(params, "preset", "lab")
        config = CALLER_PRESETS.get(preset, CALLER_PRESETS["lab"])
        call_id = ring_phone(config["label"], config["frequency"], config["seconds"], config.get("mode", "tone"))
        action_status = f"Ringing: {html.escape(config['label'])}<br>{html.escape(call_id)}"
    elif action == "stop":
        action_status = html.escape(stop_calls())
    elif action == "reregister":
        action_status = html.escape(phone_reregister())
    elif action == "msg":
        value = first(params, "value", "Apartment hotline armed")[:120]
        state.message = value
        state.log("message", state.message)
        action_status = f"Message set:<br>{html.escape(state.message)}"

    page = first(params, "page", "home")
    if action_status:
        page = "action"
    snap = state.snapshot()
    message = html.escape(snap["message"])
    registered = "REGISTERED" if snap["registered"] else "WAITING"
    now = html.escape(time.strftime("%H:%M:%S"))
    active = html.escape(next(reversed(snap["invites"].values()), "No active call"))
    meta = '<meta http-equiv="refresh" content="5">' if page != "action" else '<meta http-equiv="refresh" content="2;url=/app">'
    nav = '''<table width="100%" cellpadding="8" cellspacing="4">
<tr>
<td bgcolor="#10261d" align="center"><a href="/app"><font face="Arial" size="3">HOME</font></a></td>
<td bgcolor="#10261d" align="center"><a href="/app?page=sound"><font face="Arial" size="3">SOUND</font></a></td>
<td bgcolor="#10261d" align="center"><a href="/app?page=status"><font face="Arial" size="3">STATUS</font></a></td>
<td bgcolor="#10261d" align="center"><a href="/app?page=tools"><font face="Arial" size="3">TOOLS</font></a></td>
</tr>
</table>'''
    if page == "sound":
        body = '''<font face="Arial" size="5"><b>SOUNDBOARD</b></font><br><br>
<table width="100%" cellpadding="10" cellspacing="6">
<tr><td bgcolor="#330000" align="center"><a href="/app?action=ring&preset=red"><font face="Arial" size="4">RED PHONE</font></a></td></tr>
<tr><td bgcolor="#1b1b1b" align="center"><a href="/app?action=ring&preset=spirit"><font face="Arial" size="4">SPIRIT BOX</font></a></td></tr>
<tr><td bgcolor="#1f2430" align="center"><a href="/app?action=ring&preset=future"><font face="Arial" size="4">FUTURE YOU</font></a></td></tr>
<tr><td bgcolor="#1f3028" align="center"><a href="/app?action=ring&preset=ops"><font face="Arial" size="4">KITCHEN OPS</font></a></td></tr>
<tr><td bgcolor="#302915" align="center"><a href="/app?action=ring&preset=lab"><font face="Arial" size="4">APARTMENT LAB</font></a></td></tr>
</table>'''
    elif page == "status":
        body = f'''<font face="Arial" size="5"><b>STATUS</b></font><br><br>
<font face="Arial" size="3">SIP: {registered}</font><br>
<font face="Arial" size="3">Extension: {PHONE_USER}</font><br>
<font face="Arial" size="3">Phone: {PHONE_HOST}</font><br>
<font face="Arial" size="3">Lab: {LAB_HOST}</font><br>
<font face="Arial" size="3">Last: {active}</font><br>
<font face="Arial" size="3">Time: {now}</font>'''
    elif page == "tools":
        body = '''<font face="Arial" size="5"><b>TOOLS</b></font><br><br>
<table width="100%" cellpadding="10" cellspacing="6">
<tr><td bgcolor="#331a1a" align="center"><a href="/app?action=stop"><font face="Arial" size="4">STOP RINGING</font></a></td></tr>
<tr><td bgcolor="#1c2633" align="center"><a href="/app?action=reregister"><font face="Arial" size="4">RE-REGISTER SIP</font></a></td></tr>
<tr><td bgcolor="#1c3329" align="center"><a href="/app?action=msg&value=Apartment%20hotline%20armed"><font face="Arial" size="4">ARM HOTLINE</font></a></td></tr>
<tr><td bgcolor="#332d1c" align="center"><a href="/app?action=msg&value=Reverse%20engineering%20desk%20is%20awake"><font face="Arial" size="4">RE DESK AWAKE</font></a></td></tr>
</table>'''
    elif page == "action":
        body = f'''<font face="Arial" size="5"><b>DONE</b></font><br><br>
<font face="Arial" size="3">{action_status}</font><br><br>
<font face="Arial" size="2">Returning home...</font>'''
    else:
        body = f'''<font face="Arial" size="5"><b>APARTMENT LAB</b></font><br>
<font face="Arial" size="3">{message}</font><br><br>
<table width="100%" cellpadding="10" cellspacing="6">
<tr><td bgcolor="#330000" align="center"><a href="/app?action=ring&preset=red"><font face="Arial" size="4">RING RED PHONE</font></a></td></tr>
<tr><td bgcolor="#1b1b1b" align="center"><a href="/app?action=ring&preset=spirit"><font face="Arial" size="4">SPIRIT BOX</font></a></td></tr>
<tr><td bgcolor="#10261d" align="center"><a href="/app?page=sound"><font face="Arial" size="4">SOUNDBOARD</font></a></td></tr>
<tr><td bgcolor="#1c2633" align="center"><a href="/app?page=status"><font face="Arial" size="4">SYSTEM STATUS</font></a></td></tr>
</table>
<font face="Arial" size="2">SIP {registered} / {PHONE_USER} / {now}</font>'''
    return f'''<html>
<head>
<title>Apartment Lab</title>
{meta}
<script type="text/javascript">
function reportDims(reason) {{
  var w = window.innerWidth || 0;
  var h = window.innerHeight || 0;
  var sw = (window.screen && window.screen.width) || 0;
  var sh = (window.screen && window.screen.height) || 0;
  var p = "{page}";
  var q = "/api/dims?reason=" + encodeURIComponent(reason || "tick") +
          "&page=" + encodeURIComponent(p) +
          "&w=" + encodeURIComponent(w) +
          "&h=" + encodeURIComponent(h) +
          "&sw=" + encodeURIComponent(sw) +
          "&sh=" + encodeURIComponent(sh);
  var i = new Image();
  i.src = q;
}}
window.onload = function () {{
  reportDims("onload");
  setTimeout(function() {{ reportDims("after-1s"); }}, 1000);
  setInterval(function() {{ reportDims("heartbeat"); }}, 5000);
}};
</script>
</head>
<body bgcolor="#000000" text="#00ff99" link="#00ff99" vlink="#00ff99">
<img src="/api/dims?reason=inline&page={page}" width="1" height="1" alt="">
{nav}
<center>{body}</center>
</body>
</html>
'''


def raw_fullscreen_app(params=None):
    params = params or {}
    action_status = ""
    action = first(params, "action")
    if action == "ring":
        preset = first(params, "preset", "lab")
        config = CALLER_PRESETS.get(preset, CALLER_PRESETS["lab"])
        call_id = ring_phone(config["label"], config["frequency"], config["seconds"], config.get("mode", "tone"))
        action_status = f"Ringing: {html.escape(config['label'])}"
    elif action == "stop":
        action_status = html.escape(stop_calls())
    elif action == "msg":
        value = first(params, "value", "Apartment hotline armed")[:120]
        state.message = value
        state.log("message", state.message)
        action_status = f"Message set: {html.escape(state.message)}"

    page = first(params, "page", "home")
    status = html.escape(state.snapshot()["message"])
    now = html.escape(time.strftime("%H:%M"))

    if page == "sound":
        body = f'''<table width="100%" height="100%" cellpadding="6" cellspacing="6" border="0">
        <tr>
          <td colspan="2" height="30" align="center"><font face="Mitel_53xx_Large_V2,Arial" size="4" color="#ffb86c"><b>SOUNDBOARD</b></font></td>
        </tr>
        <tr>
          <td width="50%" bgcolor="#330000" align="center" valign="middle" style="border:2px solid #ff5555;">
            <a href="/raw-fullscreen?page=sound&amp;action=ring&amp;preset=red" style="text-decoration:none; color:#ff5555;">
            <font face="Mitel_53xx_Large_V2,Arial" size="5"><b>RED PHONE</b></font></a></td>
          <td width="50%" bgcolor="#101010" align="center" valign="middle" style="border:2px solid #9fb0c5;">
            <a href="/raw-fullscreen?page=sound&amp;action=ring&amp;preset=spirit" style="text-decoration:none; color:#edf3f8;">
            <font face="Mitel_53xx_Large_V2,Arial" size="5"><b>SPIRIT BOX</b></font></a></td>
        </tr>
        <tr>
          <td width="50%" bgcolor="#0d2620" align="center" valign="middle" style="border:2px solid #50fa7b;">
            <a href="/raw-fullscreen?page=sound&amp;action=ring&amp;preset=ops" style="text-decoration:none; color:#50fa7b;">
            <font face="Mitel_53xx_Large_V2,Arial" size="5"><b>KITCHEN OPS</b></font></a></td>
          <td width="50%" bgcolor="#1a0d33" align="center" valign="middle" style="border:2px solid #bd93f9;">
            <a href="/raw-fullscreen?page=sound&amp;action=ring&amp;preset=future" style="text-decoration:none; color:#bd93f9;">
            <font face="Mitel_53xx_Large_V2,Arial" size="5"><b>FUTURE YOU</b></font></a></td>
        </tr>
        <tr>
          <td colspan="2" bgcolor="#1d2c40" height="36" align="center" valign="middle" style="border:1px solid #5aa6ff;">
            <a href="/files/lab.html" style="text-decoration:none; color:#5aa6ff;">
            <font face="Mitel_53xx_Large_V2,Arial" size="5"><b>&lt; BACK TO MAIN</b></font></a></td>
        </tr>
        </table>'''
    else:
        # Default active page
        if action_status:
            display_text = action_status
        else:
            display_text = "ACTION COMPLETED SUCCESSFULLY"
            
        body = f'''<table width="100%" height="100%" cellpadding="12" cellspacing="12" border="0">
        <tr>
          <td bgcolor="#12253f" align="center" valign="middle" style="border:2px solid #5aa6ff;">
            <font face="Mitel_53xx_Large_V2,Arial" size="5" color="#5aa6ff"><b>{display_text}</b></font><br><br>
            <table bgcolor="#1d2c40" cellpadding="8" cellspacing="0" style="border:1px solid #5aa6ff;"><tr><td>
              <a href="/files/lab.html" style="text-decoration:none; color:#e6eef8;">
              <font face="Mitel_53xx_Large_V2,Arial" size="5"><b>&lt; BACK TO MAIN</b></font></a>
            </td></tr></table>
          </td>
        </tr>
        </table>'''

    return f'''<html>
<head>
<title>Apartment Lab</title>
<script type="text/javascript">
window.onload = function () {{
  try {{ RequestFullScreenBrowser(); }} catch (e) {{}}
}};
</script>
</head>
<body bgcolor="#08111d" text="#e6eef8" link="#e6eef8" vlink="#e6eef8" alink="#e6eef8" topmargin="0" leftmargin="0" marginwidth="0" marginheight="0">
<table width="100%" height="100%" cellpadding="0" cellspacing="0" border="0">
<tr><td bgcolor="#0e1b2e" height="42" valign="middle" style="border-bottom:2px solid #5aa6ff;">
  <table width="100%" cellpadding="8" cellspacing="0" border="0"><tr>
    <td align="left"><font face="Mitel_53xx_Large_V2,Arial" size="5" color="#5aa6ff"><b>APARTMENT LAB</b></font></td>
    <td align="right"><font face="Mitel_53xx_Large_V2,Arial" size="5"><b>{now}</b></font></td>
  </tr></table>
</td></tr>
<tr><td bgcolor="#0a1320" height="28" align="center" valign="middle">
  <font face="Mitel_53xx_Large_V2,Arial" size="3" color="#9fb3c8">{status}</font>
</td></tr>
<tr><td valign="middle">
  {body}
</td></tr>
<tr><td bgcolor="#0a1320" height="22" align="center" valign="middle" style="border-top:1px solid #1d2c40;">
  <font face="Arial" size="2" color="#5d7488">SIP {PHONE_USER} connected</font>
</td></tr>
</table>
</body>
</html>
'''


def generic_cfg():
    snap = state.snapshot()
    return f'''<Parameter Model="5360">
    <tftp_task_enable>1</tftp_task_enable>
    <local_sip_port>5060</local_sip_port>
    <sip_mode>{snap["config_sip_mode"]}</sip_mode>
    <rtp_base_port>40000</rtp_base_port>
    <register_expire>3600</register_expire>
    <outbound_state>0</outbound_state>
    <html_enable>1</html_enable>
    <sip_mode>{snap["config_sip_mode"]}</sip_mode>
    <html_filename>{snap["config_html_url"]}</html_filename>
    <htmlapp_mandatory_dwnld>{snap["config_html_mandatory"]}</htmlapp_mandatory_dwnld>
    <rss_feed>http://{LAB_HOST}/feed</rss_feed>
</Parameter>
'''


def mac_cfg():
    snap = state.snapshot()
    key_app = snap["config_key_app"]
    mode_val = "0" if (key_app.startswith("http://") or key_app.startswith("https://")) else "1"
    return f'''<Parameter Model="5360">
    <pkDescription>
        <Key Line="1" Fea="6" Des="Lab Line" Addr="" Mode="1" UserID="{PHONE_USER}"></Key>
        <Key Line="28" Fea="{PHONE_APP_FEATURE_CODE}" Des="OPEN LAB UI" Addr="{key_app}" Mode="{mode_val}" UserID=""></Key>
        <Key Line="58" Fea="{PHONE_APP_FEATURE_CODE}" Des="OPEN LAB UI" Addr="{key_app}" Mode="{mode_val}" UserID=""></Key>
    </pkDescription>
    <user_list>
        <User State="1" ID="{PHONE_USER}" DispName="{PHONE_NAME}" Pwd="" AuthName="{PHONE_USER}" Realm="" RegSvr="{LAB_HOST}" RegPort="5060" RegScheme="2" ProxySvr="{LAB_HOST}" ProxyPort="5060" ProxyScheme="2" VMSvr="" VMPort="5060" VMScheme="2" OutSvr="" OutPort="5060" OutCtr="0" Ring="1" Line="0" EventSvr="" EventPort="5060" EventScheme="2"></User>
    </user_list>
    <html_enable>1</html_enable>
    <html_filename>{snap["config_html_url"]}</html_filename>
    <htmlapp_mandatory_dwnld>{snap["config_html_mandatory"]}</htmlapp_mandatory_dwnld>
    <rss_feed>http://{LAB_HOST}/feed</rss_feed>
</Parameter>
'''


def mcd_html_app_config(device="5360"):
    packages = [
        "5360-FullScreenGUISample.spx",
        "ApartmentLabGRM.rich.spx",
        "ApartmentLabGRM.official.spx",
        "ApartmentLabRedirectFS.spx",
        "ApartmentLabNotification.spx",
        "Default_ScreenSaver.spx",
    ]
    entries = "\n".join(f"  <ApplicationPackage>{name}</ApplicationPackage>" for name in packages)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<HtmlApplications device="{device}">
{entries}
</HtmlApplications>
'''


CONFIGS = {
    "/MN_Generic.cfg": generic_cfg,
    "/MN_Generic.CFG": generic_cfg,
    "/MN_GENERIC.CFG": generic_cfg,
    f"/MN_{PHONE_USER}.cfg": mac_cfg,
    f"/MN_{PHONE_USER}.CFG": mac_cfg,
    f"/mn_{PHONE_USER}.cfg": mac_cfg,
    f"/MN_{PHONE_MAC}.cfg": mac_cfg,
    f"/MN_{PHONE_MAC}.CFG": mac_cfg,
    f"/mn_{PHONE_MAC.lower()}.cfg": mac_cfg,
}


MCD_HTML_CONFIGS = {
    "/db/htmlapps/config/5330.xml": lambda: mcd_html_app_config("5330"),
    "/db/htmlapps/config/5340.xml": lambda: mcd_html_app_config("5340"),
    "/db/htmlapps/config/5360.xml": lambda: mcd_html_app_config("5360"),
}


def _mcd_real_path(virtual_path):
    clean = virtual_path.strip() or "/"
    if not clean.startswith("/"):
        clean = "/" + clean
    clean = os.path.normpath(clean)
    if clean.startswith("/db/htmlapps/apps"):
        suffix = clean[len("/db/htmlapps/apps"):].strip("/")
        if not suffix:
            return STATIC_FILE_DIR
        return os.path.join(STATIC_FILE_DIR, os.path.basename(suffix))
    if clean.startswith("/db/htmlapps/config"):
        suffix = clean[len("/db/htmlapps/config"):].strip("/")
        if not suffix:
            return None
        return f"/db/htmlapps/config/{os.path.basename(suffix)}"
    return None


def _ftp_list_lines(virtual_dir):
    if virtual_dir.rstrip("/") == "/db/htmlapps/config":
        names = sorted(os.path.basename(path) for path in MCD_HTML_CONFIGS)
    elif virtual_dir.rstrip("/") == "/db/htmlapps/apps":
        names = sorted(name for name in os.listdir(STATIC_FILE_DIR) if name.lower().endswith(".spx"))
    else:
        names = []
    return "\r\n".join(
        f"-rw-r--r-- 1 mitel mitel {len(name)} May 21 12:00 {name}" for name in names
    ) + ("\r\n" if names else "")


class MCDFTPHandler(socketserver.BaseRequestHandler):
    timeout = 60

    def setup(self):
        self.cwd = "/"
        self.passive = None
        self.active_addr = None
        self.request.settimeout(self.timeout)
        state.log("ftp", f"{self.client_address[0]} connected")

    def send_line(self, line):
        self.request.sendall((line + "\r\n").encode("utf-8"))

    def recv_line(self):
        chunks = []
        while True:
            char = self.request.recv(1)
            if not char:
                return ""
            if char == b"\n":
                break
            if char != b"\r":
                chunks.append(char)
        return b"".join(chunks).decode("utf-8", "replace")

    def abs_path(self, raw):
        raw = raw.strip()
        if not raw:
            return self.cwd
        if raw.startswith("/"):
            return os.path.normpath(raw)
        return os.path.normpath(self.cwd.rstrip("/") + "/" + raw)

    def open_data_socket(self):
        if self.passive:
            listener = self.passive
            self.passive = None
            listener.settimeout(15)
            conn, _addr = listener.accept()
            return conn
        if self.active_addr:
            conn = socket.create_connection(self.active_addr, timeout=15)
            self.active_addr = None
            return conn
        raise RuntimeError("no FTP data connection")

    def close_passive(self):
        if self.passive:
            try:
                self.passive.close()
            except OSError:
                pass
            self.passive = None

    def handle(self):
        self.send_line("220 Mitel 3300 Controller FTP ready")
        while True:
            line = self.recv_line()
            if not line:
                break
            command, _, arg = line.partition(" ")
            command = command.upper()
            arg = arg.strip()
            state.log("ftp<-", f"{self.client_address[0]} {command} {arg}")
            try:
                if command == "USER":
                    self.send_line("331 Password required")
                elif command == "PASS":
                    self.send_line("230 Logged in")
                elif command == "SYST":
                    self.send_line("215 UNIX Type: L8")
                elif command in ("TYPE", "MODE", "STRU"):
                    self.send_line("200 OK")
                elif command == "PWD":
                    self.send_line(f'257 "{self.cwd}"')
                elif command == "CWD":
                    target = self.abs_path(arg)
                    if target.rstrip("/") in ("/", "/db", "/db/htmlapps", "/db/htmlapps/apps", "/db/htmlapps/config"):
                        self.cwd = target
                        self.send_line("250 Directory changed")
                    else:
                        self.send_line("550 Directory unavailable")
                elif command == "PASV":
                    self.close_passive()
                    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    listener.bind(("0.0.0.0", 0))
                    listener.listen(1)
                    port = listener.getsockname()[1]
                    self.passive = listener
                    nums = LAB_HOST.split(".") + [str(port // 256), str(port % 256)]
                    self.send_line(f"227 Entering Passive Mode ({','.join(nums)})")
                elif command == "EPSV":
                    self.close_passive()
                    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    listener.bind(("0.0.0.0", 0))
                    listener.listen(1)
                    port = listener.getsockname()[1]
                    self.passive = listener
                    self.send_line(f"229 Entering Extended Passive Mode (|||{port}|)")
                elif command == "PORT":
                    parts = [int(part) for part in arg.split(",")]
                    if len(parts) != 6:
                        self.send_line("501 Bad PORT")
                    else:
                        self.active_addr = (".".join(str(part) for part in parts[:4]), parts[4] * 256 + parts[5])
                        self.send_line("200 PORT command successful")
                elif command in ("LIST", "NLST"):
                    target = self.abs_path(arg) if arg else self.cwd
                    listing = _ftp_list_lines(target)
                    self.send_line("150 Opening data connection")
                    with self.open_data_socket() as data:
                        data.sendall(listing.encode("utf-8"))
                    self.send_line("226 Transfer complete")
                elif command == "RETR":
                    target = self.abs_path(arg)
                    real = _mcd_real_path(target)
                    if isinstance(real, str) and real.startswith("/db/htmlapps/config/") and real in MCD_HTML_CONFIGS:
                        body = MCD_HTML_CONFIGS[real]().encode("utf-8")
                    elif real and os.path.isfile(real):
                        with open(real, "rb") as handle:
                            body = handle.read()
                    else:
                        self.send_line("550 File unavailable")
                        continue
                    self.send_line("150 Opening data connection")
                    with self.open_data_socket() as data:
                        data.sendall(body)
                    state.log("ftp", f"RETR {target} bytes={len(body)}")
                    self.send_line("226 Transfer complete")
                elif command == "STOR":
                    target = self.abs_path(arg)
                    real = _mcd_real_path(target)
                    if not real or isinstance(real, str) and real.startswith("/db/htmlapps/config/"):
                        self.send_line("550 Upload path unavailable")
                        continue
                    os.makedirs(os.path.dirname(real), exist_ok=True)
                    self.send_line("150 Opening data connection")
                    total = 0
                    with self.open_data_socket() as data, open(real, "wb") as handle:
                        while True:
                            chunk = data.recv(65536)
                            if not chunk:
                                break
                            total += len(chunk)
                            handle.write(chunk)
                    state.log("ftp", f"STOR {target} bytes={total}")
                    self.send_line("226 Transfer complete")
                elif command == "DELE":
                    target = self.abs_path(arg)
                    real = _mcd_real_path(target)
                    if real and os.path.isfile(real):
                        os.remove(real)
                    self.send_line("250 Deleted")
                elif command == "NOOP":
                    self.send_line("200 OK")
                elif command == "QUIT":
                    self.send_line("221 Goodbye")
                    break
                else:
                    self.send_line("502 Command not implemented")
            except Exception as exc:
                state.log("ftp", f"{command} failed: {exc!r}")
                self.send_line("451 Local processing error")
        self.close_passive()


def mcd_ftp_server(bind_host, bind_port):
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    server = socketserver.ThreadingTCPServer((bind_host, bind_port), MCDFTPHandler)
    state.log("ftp", f"MCD filesystem listening on {bind_host}:{bind_port}")
    server.serve_forever()


def mixml_response(raw, headers=None):
    headers = headers or {}
    lowered = raw.lower()
    command = "HTMLAPPUPGRADE"
    if "generateusernamepassword" in lowered:
        command = "GenerateUsernamePassword"
    elif "htmlappupgrade" in lowered:
        command = "HTMLAPPUPGRADE"
    elif "login" in lowered:
        command = "Login"
    elif "authenticate" in lowered:
        command = "Authenticate"
    session_id = "mitel-lab-session"
    if headers.get("SOAPAction") or "<soap" in lowered or ":envelope" in lowered:
        if command == "GenerateUsernamePassword":
            payload = f"""<executeAndWaitReturn>
  <result>MissRC-Success</result>
  <mcResult>MimcRC-Success</mcResult>
  <sessionId>{session_id}</sessionId>
  <output>mitel</output>
  <output>mitel</output>
  <returnValues>
    <item>mitel</item>
    <item>mitel</item>
  </returnValues>
</executeAndWaitReturn>"""
        elif command in ("Login", "Authenticate"):
            payload = f"""<{command.lower()}Return>
  <result>MissRC-Success</result>
  <sessionId>{session_id}</sessionId>
</{command.lower()}Return>"""
        else:
            payload = f"""<executeAndWaitReturn>
  <result>MissRC-Success</result>
  <mcResult>MimcRC-Success</mcResult>
  <command>{command}</command>
</executeAndWaitReturn>"""
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
  <soapenv:Body>
    {payload}
  </soapenv:Body>
</soapenv:Envelope>
'''
    if command == "GenerateUsernamePassword":
        output = """  <Output>mitel</Output>
  <Output>mitel</Output>"""
    else:
        output = ""
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<MiXML>
  <Result>MissRC-Success</Result>
  <McResult>MimcRC-Success</McResult>
  <SessionId>{session_id}</SessionId>
  <Command>{command}</Command>
{output}
</MiXML>
'''


class LabHTTP(BaseHTTPRequestHandler):
    server_version = "MitelLab/1.0"

    def log_message(self, _fmt, *_args):
        return

    def read_form(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        form = parse_qs(body)
        form["__raw"] = [body]
        return form

    def send_json(self, payload, status=200):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_static_file(self, filename, include_body=True, display_path=None):
        path = os.path.join(STATIC_FILE_DIR, os.path.basename(filename))
        shown_path = display_path or f"/files/{os.path.basename(filename)}"
        if not os.path.exists(path):
            state.log("http", f"{self.client_address[0]} {self.command} {shown_path} -> 404")
            self.send_json({"ok": False, "error": "file not found"}, 404)
            return
        with open(path, "rb") as handle:
            body = handle.read()
        content_type = "application/octet-stream"
        if filename.lower().endswith((".htm", ".html")):
            content_type = "text/html; charset=utf-8"
        elif filename.lower().endswith(".xml"):
            content_type = "text/xml"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)
        method = "GET" if include_body else "HEAD"
        state.log("http", f"{self.client_address[0]} {method} {shown_path} bytes={len(body)}")

    def send_notification_file(self, filename):
        path = os.path.join(NOTIFICATION_FILE_DIR, os.path.basename(filename))
        shown_path = f"/finalnotification/{os.path.basename(filename)}"
        if not os.path.exists(path):
            state.log("http", f"{self.client_address[0]} GET {shown_path} -> 404")
            self.send_json({"ok": False, "error": "file not found"}, 404)
            return
        with open(path, "rb") as handle:
            body = handle.read()
        content_type = "application/octet-stream"
        if filename.lower().endswith(".xml"):
            content_type = "text/xml"
        elif filename.lower().endswith(".js"):
            content_type = "application/javascript"
        elif filename.lower().endswith((".htm", ".html")):
            content_type = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        state.log("http", f"{self.client_address[0]} GET {shown_path} bytes={len(body)}")

    def send_mcd_html_config(self, path, include_body=True):
        config = MCD_HTML_CONFIGS.get(path)
        if not config:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = config().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)
        method = "GET" if include_body else "HEAD"
        state.log("http", f"{self.client_address[0]} {method} {path} bytes={len(body)}")

    def do_HEAD(self):
        path = urlparse(self.path).path
        if path.startswith("/files/"):
            self.send_static_file(path.rsplit("/", 1)[-1], include_body=False)
            return
        if path.startswith("/db/htmlapps/apps/"):
            self.send_static_file(path.rsplit("/", 1)[-1], include_body=False, display_path=path)
            return
        if path in MCD_HTML_CONFIGS:
            self.send_mcd_html_config(path, include_body=False)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        params = parse_qs(url.query)
        if path == "/api/status":
            self.send_json(status_payload())
            return
        if path == "/api/control-check":
            self.send_json(control_check())
            return
        if path == "/api/ai-voice-status":
            calls = _voice_loop_mod.active_calls() if _voice_loop_mod is not None else []
            self.send_json({
                "ok": True,
                "loaded": _voice_loop_mod is not None,
                "extension": AI_EXT_USER,
                "active_calls": calls,
                "count": len(calls),
            })
            return
        if path == "/api/dims":
            w = first(params, "w", "?")
            h = first(params, "h", "?")
            sw = first(params, "sw", "?")
            sh = first(params, "sh", "?")
            state.log("browser", f"{self.client_address[0]} dims window={w}x{h} screen={sw}x{sh}")
            self.send_json({"ok": True})
            return
        if path == "/feed":
            body = rss_feed().encode("utf-8")
            state.log("http", f"{self.client_address[0]} GET /feed bytes={len(body)}")
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/app":
            body = phone_app(params).encode("utf-8")
            state.log("http", f"{self.client_address[0]} GET /app bytes={len(body)}")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/raw-fullscreen":
            body = raw_fullscreen_app(params).encode("utf-8")
            state.log("http", f"{self.client_address[0]} GET /raw-fullscreen bytes={len(body)}")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/files/"):
            self.send_static_file(path.rsplit("/", 1)[-1])
            return
        if path.startswith("/db/htmlapps/apps/"):
            self.send_static_file(path.rsplit("/", 1)[-1], display_path=path)
            return
        if path in MCD_HTML_CONFIGS:
            self.send_mcd_html_config(path)
            return
        if path.startswith("/finalnotification/"):
            filename = path.rsplit("/", 1)[-1]
            if filename.lower() == "xml-response.xml":
                body = notification_pull_response(self.client_address[0]).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/xml")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_notification_file(filename)
            return
        if path in CONFIGS:
            body = CONFIGS[path]().encode("utf-8")
            state.log("http", f"{self.client_address[0]} GET {path} -> config")
            self.send_response(200)
            self.send_header("Content-Type", "text/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/ring":
            call_id = ring_phone()
            body = f"ring sent {call_id}\n".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/stop":
            message = stop_calls()
            body = f"{message}\n".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ("", "/", "/admin"):
            body = dashboard().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        global TFTP_SKIP_MAINIP, TFTP_FORCE_BLKSIZE, TFTP_FORCE_WNDSIZE
        path = urlparse(self.path).path
        form = self.read_form()
        try:
            if path == "/cgi-bin/mixmlrequest":
                raw = first(form, "__raw", "")
                body = mixml_response(raw, dict(self.headers)).encode("utf-8")
                state.log(
                    "mixml",
                    f"{self.client_address[0]} POST /cgi-bin/mixmlrequest bytes={len(raw)} "
                    f"soap={bool(self.headers.get('SOAPAction'))}",
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/xml")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/ring":
                preset = first(form, "preset", "lab")
                config = dict(CALLER_PRESETS.get(preset, CALLER_PRESETS["lab"]))
                custom = first(form, "caller", "").strip()
                if custom:
                    config["label"] = custom[:40]
                mode = first(form, "mode", config.get("mode", "tone"))
                if mode not in AUDIO_MODES:
                    mode = config.get("mode", "tone")
                sound = os.path.basename(first(form, "sound", ""))
                frequency = bounded_int(first(form, "frequency", config["frequency"]), 40, 1800, config["frequency"])
                seconds = bounded_int(first(form, "seconds", config["seconds"]), 2, 120, config["seconds"])
                call_id = ring_phone(config["label"], frequency, seconds, mode, sound)
                self.send_json({"ok": True, "call_id": call_id})
                return
            if path == "/api/bootz-audio":
                preset = first(form, "preset", "spirit")
                config = dict(CALLER_PRESETS.get(preset, CALLER_PRESETS["spirit"]))
                custom = first(form, "caller", "").strip()
                if custom:
                    config["label"] = custom[:40]
                mode = first(form, "mode", config.get("mode", "spirit"))
                if mode == "file":
                    mode = "spirit"
                frequency = bounded_int(first(form, "frequency", config["frequency"]), 40, 1800, config["frequency"])
                seconds = bounded_int(first(form, "seconds", config["seconds"]), 2, 120, config["seconds"])
                self.send_json({"ok": True, "message": start_bootz_audio_call(config["label"], frequency, seconds, mode)})
                return
            if path == "/api/stop":
                self.send_json({"ok": True, "message": stop_calls()})
                return
            if path == "/api/reregister":
                self.send_json({"ok": True, "message": phone_reregister()})
                return
            if path == "/api/message":
                message = first(form, "message", "").strip()[:120]
                state.message = message or "Apartment hotline armed"
                state.log("message", state.message)
                self.send_json({"ok": True, "message": state.message})
                return
            if path == "/api/notification-fire":
                relative_uri = first(form, "uri", "application.htm")
                self.send_json(arm_notification_invoke(relative_uri))
                return
            if path == "/api/sip-notify-ack":
                body = first(form, "body", "")
                content_type = first(form, "content_type", "text/plain")
                self.send_json(set_sip_notify_ack(body, content_type))
                return
            if path == "/api/html-app":
                target_url = app_url_for_target(first(form, "target", "safe"))
                force = first(form, "force", "0") == "1"
                if is_upgrade_prone_html_url(target_url) and not force:
                    self.send_json(
                        {
                            "ok": False,
                            "error": "blocked: html_filename launch enters HTML App Upgrade on standalone SIP; pass force=1 to override",
                            "url": target_url,
                        },
                        409,
                    )
                    return
                self.send_json({"ok": True, "message": set_phone_html_url(target_url), "url": target_url})
                return
            if path == "/api/apartment-key":
                target_url = app_url_for_target(first(form, "target", "safe"))
                self.send_json({"ok": True, "message": set_apartment_key_url(target_url), "url": target_url})
                return
            if path == "/api/probe-spx":
                target = first(form, "target", "official")
                wait_seconds = bounded_int(first(form, "seconds", "5"), 2, 20, 5)
                self.send_json(probe_spx_delivery(target, wait_seconds))
                return
            if path == "/api/provisioning-html":
                target = first(form, "target", "safe")
                target_url = app_url_for_target(target) if target else NO_BOOT_HTML_URL
                mandatory = first(form, "mandatory", "0")
                force = first(form, "force", "0") == "1"
                if is_upgrade_prone_html_url(target_url) and not force:
                    self.send_json(
                        {
                            "ok": False,
                            "error": "blocked: provisioning html_filename enters HTML App Upgrade on standalone SIP; pass force=1 to override",
                            "url": target_url,
                            "mandatory": "1" if mandatory == "1" else "0",
                        },
                        409,
                    )
                    return
                self.send_json({"ok": True, "message": set_provisioning_html(target_url, mandatory), "url": target_url, "mandatory": "1" if mandatory == "1" else "0"})
                return
            if path == "/api/enable-custom-gui":
                reboot = first(form, "reboot", "0") == "1"
                self.send_json({"ok": True, "message": enable_custom_gui(reboot), "url": SAFE_HTML_URL, "reboot": reboot})
                return
            if path == "/api/restore-native-boot":
                self.send_json({"ok": True, "message": restore_native_boot(), "url": NO_BOOT_HTML_URL, "mandatory": "0"})
                return
            if path == "/api/clear-html-upgrade":
                reboot = first(form, "reboot", "1") == "1"
                self.send_json({"ok": True, "message": clear_html_upgrade_error(reboot), "url": NO_BOOT_HTML_URL, "mandatory": "0"})
                return
            if path == "/api/force-install":
                from pathlib import Path
                script = Path(RESEARCH_DIR) / "tools" / "push_phone_install.py"
                import subprocess
                proc = subprocess.run(
                    [sys.executable, str(script)],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                self.send_json({
                    "ok": proc.returncode == 0,
                    "stdout": proc.stdout[-4000:],
                    "stderr": proc.stderr[-2000:],
                })
                return
            if path == "/api/install-fsa-sip":
                load_target = first(form, "load", "fullscreen")
                key_app = first(form, "key_app", "FullScreenSample")
                use_master = first(form, "master", "0") == "1"
                reboot = first(form, "reboot", "0") == "1"
                message = install_fsa_sip_workflow(
                    load_target=load_target,
                    key_app=key_app,
                    use_master=use_master,
                    reboot=reboot,
                )
                self.send_json({"ok": True, "message": message, "key_app": key_app, "master": use_master})
                return
            if path == "/api/reboot-phone":
                self.send_json({"ok": True, "message": reboot_phone()})
                return
            if path == "/api/minet-probe":
                seconds = bounded_int(first(form, "seconds", "180"), 30, 600, 180)
                reboot = first(form, "reboot", "0") == "1"
                self.send_json(start_minet_probe(seconds, reboot))
                return
            if path == "/api/minet-replay-load":
                capture_dir = first(form, "dir", MINET_RELAY_CAPTURE_DIR).strip() or MINET_RELAY_CAPTURE_DIR
                self.send_json(load_minet_replay_frames(capture_dir))
                return
            if path == "/api/minet-replay-enable":
                enable = first(form, "enable", "1") == "1"
                self.send_json(set_minet_replay_enabled(enable))
                return
            if path == "/api/restore-sip-mode":
                self.send_json({"ok": True, "message": set_sip_mode("sip", "restore-sip-mode-api")})
                return
            if path == "/api/set-ai-voice-key":
                key_number = int(first(form, "key", "2") or "2")
                label = first(form, "label", "AI VOICE") or "AI VOICE"
                try:
                    msg = set_ai_voice_speed_dial_key(key_number=key_number, label=label)
                    self.send_json({"ok": True, "message": msg})
                except Exception as exc:
                    self.send_json({"ok": False, "error": repr(exc)})
                return
            if path == "/api/clear-phone-keys":
                start = int(first(form, "start", "1") or "1")
                end = int(first(form, "end", "32") or "32")
                try:
                    msg = clear_phone_keys_range(start=start, end=end)
                    self.send_json({"ok": True, "message": msg})
                except Exception as exc:
                    self.send_json({"ok": False, "error": repr(exc)})
                return
            if path == "/api/tftp-skip-mainip":
                enable = first(form, "enable", "1") == "1"
                TFTP_SKIP_MAINIP = enable
                state.log("tftp", f"TFTP_SKIP_MAINIP set to {TFTP_SKIP_MAINIP}")
                self.send_json({"ok": True, "tftp_skip_mainip": TFTP_SKIP_MAINIP})
                return
            if path == "/api/tftp-options":
                if first(form, "skip_mainip", ""):
                    TFTP_SKIP_MAINIP = first(form, "skip_mainip", "0") == "1"
                blksize = first(form, "force_blksize", "").strip()
                wndsize = first(form, "force_wndsize", "").strip()
                if blksize:
                    TFTP_FORCE_BLKSIZE = bounded_int(blksize, 8, 65464, TFTP_FORCE_BLKSIZE or 512)
                if wndsize:
                    TFTP_FORCE_WNDSIZE = bounded_int(wndsize, 1, 64, TFTP_FORCE_WNDSIZE or 1)
                state.log(
                    "tftp",
                    f"options skip_mainip={TFTP_SKIP_MAINIP} "
                    f"force_blksize={TFTP_FORCE_BLKSIZE or '-'} "
                    f"force_wndsize={TFTP_FORCE_WNDSIZE or '-'}",
                )
                self.send_json(
                    {
                        "ok": True,
                        "tftp_skip_mainip": TFTP_SKIP_MAINIP,
                        "force_blksize": TFTP_FORCE_BLKSIZE,
                        "force_wndsize": TFTP_FORCE_WNDSIZE,
                    }
                )
                return
            if path == "/api/launch-temporary":
                target = first(form, "target", "rich")
                hold_seconds = bounded_int(first(form, "seconds", "30"), 5, 120, 30)
                self.send_json(launch_html_temporarily(target, hold_seconds))
                return
            self.send_json({"ok": False, "error": "unknown endpoint"}, 404)
        except Exception as exc:
            state.log("api", f"{path} failed: {exc!r}")
            self.send_json({"ok": False, "error": repr(exc)}, 500)


def dashboard():
    return f'''<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mitel Lab Admin</title>
<style>
/* Impeccable product register: restrained, state-rich, task-first admin UI. */
:root {{
  color-scheme: dark;
  --bg: oklch(16% 0.012 165);
  --surface: oklch(22% 0.014 158);
  --surface-2: oklch(26% 0.017 154);
  --surface-3: oklch(31% 0.019 148);
  --line: oklch(40% 0.028 145);
  --line-strong: oklch(52% 0.038 136);
  --text: oklch(93% 0.012 120);
  --muted: oklch(73% 0.032 120);
  --soft: oklch(62% 0.033 132);
  --accent: oklch(79% 0.145 78);
  --accent-strong: oklch(85% 0.15 78);
  --accent-text: oklch(18% 0.034 100);
  --good: oklch(75% 0.14 154);
  --warn: oklch(80% 0.14 83);
  --bad: oklch(69% 0.16 28);
  --info: oklch(74% 0.12 203);
  --violet: oklch(72% 0.12 292);
  --focus: oklch(82% 0.13 190);
  --shadow: 0 18px 56px oklch(7% 0.02 160 / .38);
}}
* {{ box-sizing:border-box; }}
html {{ background:var(--bg); overflow-x:hidden; }}
body {{
  margin:0;
  font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
  background:
    radial-gradient(circle at 15% -4%, oklch(38% 0.055 78 / .36), transparent 30rem),
    radial-gradient(circle at 82% 8%, oklch(36% 0.06 202 / .24), transparent 29rem),
    linear-gradient(180deg, oklch(19% 0.018 156), var(--bg) 23rem);
  color:var(--text);
  overflow-x:hidden;
}}
button, input, select, textarea {{ font:inherit; }}
a {{ color:inherit; }}
.shell {{ width:min(1440px, 100%); margin:0 auto; padding:20px; }}
.shell > * {{ min-width:0; }}
.topbar {{
  display:grid;
  grid-template-columns:minmax(0, 1fr) auto;
  gap:18px;
  align-items:end;
  padding:8px 0 18px;
}}
.topbar > * {{ min-width:0; }}
.eyebrow {{ color:var(--accent); font-size:12px; font-weight:800; letter-spacing:.12em; text-transform:uppercase; }}
h1 {{ margin:4px 0 0; font-size:29px; line-height:1.12; letter-spacing:0; }}
.subtle {{ color:var(--muted); overflow-wrap:anywhere; }}
.top-actions {{ display:flex; flex-wrap:wrap; gap:8px; justify-content:flex-end; }}
.command-strip {{
  display:grid;
  grid-template-columns:minmax(0, 1.2fr) repeat(3, minmax(160px, .7fr));
  gap:10px;
  margin-bottom:16px;
  min-width:0;
}}
.command-card {{
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:12px;
  min-height:72px;
  min-width:0;
  padding:13px;
  border:1px solid var(--line);
  border-radius:8px;
  background:linear-gradient(180deg, oklch(25% 0.017 154 / .86), oklch(20% 0.014 158 / .92));
  box-shadow:var(--shadow);
}}
.command-card.primary {{ border-color:oklch(60% 0.08 78); background:linear-gradient(180deg, oklch(31% 0.043 84 / .9), oklch(23% 0.022 120 / .94)); }}
.command-card > * {{ min-width:0; max-width:100%; }}
.command-card > div:first-child {{ flex:1 1 auto; }}
.command-title {{ font-weight:850; }}
.command-meta {{ margin-top:3px; color:var(--muted); font-size:12px; overflow-wrap:anywhere; word-break:break-word; }}
.badge {{
  display:inline-flex;
  align-items:center;
  min-height:24px;
  padding:0 8px;
  border-radius:999px;
  border:1px solid var(--line);
  color:var(--muted);
  font-size:12px;
  font-weight:760;
  white-space:nowrap;
}}
.badge.ok {{ color:var(--good); border-color:oklch(58% 0.08 154); background:oklch(28% 0.04 154 / .5); }}
.badge.warn {{ color:var(--warn); border-color:oklch(58% 0.09 83); background:oklch(29% 0.04 83 / .44); }}
.badge.bad {{ color:var(--bad); border-color:oklch(57% 0.1 28); background:oklch(27% 0.04 28 / .46); }}
.layout {{ display:grid; grid-template-columns:276px minmax(0, 1fr); gap:16px; align-items:start; }}
.layout > *, .content, .grid-main > *, .metrics > *, .panel {{ min-width:0; }}
.rail {{
  position:sticky;
  top:16px;
  display:flex;
  flex-direction:column;
  gap:12px;
}}
.panel {{
  background:linear-gradient(180deg, var(--surface-2), var(--surface));
  border:1px solid var(--line);
  border-radius:8px;
  box-shadow:var(--shadow);
}}
.panel.feature {{ border-color:oklch(52% 0.052 202); background:linear-gradient(180deg, oklch(27% 0.032 194 / .92), var(--surface)); }}
.panel.pad {{ padding:16px; }}
.panel-title {{
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:12px;
  margin:0 0 14px;
}}
.panel h2 {{ margin:0; font-size:13px; line-height:1.2; letter-spacing:.08em; text-transform:uppercase; color:var(--muted); }}
.panel h3 {{ margin:0 0 10px; font-size:15px; line-height:1.25; letter-spacing:0; }}
.nav {{ display:grid; gap:4px; padding:8px; }}
.nav a {{
  display:flex;
  justify-content:space-between;
  align-items:center;
  min-height:38px;
  padding:8px 10px;
  border-radius:7px;
  color:var(--muted);
  text-decoration:none;
  min-width:0;
}}
.nav a span {{ min-width:0; overflow-wrap:anywhere; }}
.nav a:hover, .nav a:focus-visible {{ background:var(--surface-3); color:var(--text); outline:none; }}
.nav a span:last-child {{ color:var(--soft); font-size:12px; }}
.status-stack {{ display:grid; gap:8px; }}
.status-row {{ display:grid; grid-template-columns:16px minmax(0, 1fr); gap:9px; align-items:start; min-height:28px; }}
.dot {{ width:10px; height:10px; margin-top:5px; border-radius:99px; background:var(--bad); box-shadow:0 0 0 4px oklch(69% 0.16 28 / .13); }}
.dot.ok {{ background:var(--good); box-shadow:0 0 0 4px oklch(75% 0.14 154 / .13); }}
.dot.warn {{ background:var(--warn); box-shadow:0 0 0 4px oklch(80% 0.14 83 / .12); }}
.row-label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.06em; }}
.row-value {{ margin-top:1px; min-width:0; overflow-wrap:anywhere; font-weight:700; }}
.content {{ display:grid; gap:16px; }}
.metrics {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; }}
.metric {{
  min-height:92px;
  padding:13px;
  border:1px solid var(--line);
  border-radius:8px;
  background:oklch(20% 0.012 92 / .84);
}}
.label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.06em; }}
.value {{ margin-top:8px; overflow-wrap:anywhere; font-size:15px; font-weight:760; }}
.value.small {{ font-size:13px; font-weight:650; }}
.grid-main {{ display:grid; grid-template-columns:minmax(0, 1.04fr) minmax(360px, .96fr); gap:16px; align-items:start; }}
.stack {{ display:grid; gap:16px; }}
.controls {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }}
.field {{ display:flex; flex-direction:column; gap:6px; }}
.field > span {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.06em; }}
input, select, textarea {{
  width:100%;
  min-height:39px;
  border-radius:7px;
  border:1px solid var(--line-strong);
  background:oklch(18% 0.011 92);
  color:var(--text);
  padding:8px 10px;
  outline:none;
}}
textarea {{ min-height:82px; resize:vertical; }}
input:focus, select:focus, textarea:focus {{ border-color:var(--focus); box-shadow:0 0 0 3px oklch(73% 0.12 194 / .16); }}
.buttons {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:14px; }}
button, .button {{
  display:inline-flex;
  align-items:center;
  justify-content:center;
  gap:7px;
  min-height:39px;
  border:1px solid transparent;
  border-radius:7px;
  padding:0 13px;
  background:var(--accent);
  color:var(--accent-text);
  font-weight:800;
  text-decoration:none;
  cursor:pointer;
  transition:background-color 180ms ease-out, border-color 180ms ease-out, transform 180ms ease-out;
}}
button:hover, .button:hover {{ background:var(--accent-strong); }}
button:active, .button:active {{ transform:translateY(1px); }}
button.secondary, .button.secondary {{ background:var(--surface-3); color:var(--text); border-color:var(--line-strong); }}
button.secondary:hover, .button.secondary:hover {{ background:oklch(35% 0.024 96); }}
button.danger {{ background:var(--bad); color:oklch(18% 0.035 28); }}
button.ghost, .button.ghost {{ background:transparent; color:var(--muted); border-color:var(--line); }}
button:disabled {{ opacity:.55; cursor:not-allowed; transform:none; }}
.button-row {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(168px,1fr)); gap:8px; }}
.control-audit {{ display:grid; gap:8px; margin-top:12px; }}
.audit-row {{
  display:grid;
  grid-template-columns:18px minmax(0, 1fr) auto;
  gap:9px;
  align-items:center;
  min-height:30px;
  padding:7px 8px;
  border:1px solid var(--line);
  border-radius:7px;
  background:oklch(19% 0.012 154 / .82);
}}
.audit-row span:nth-child(2) {{ overflow-wrap:anywhere; }}
.audit-dot {{ width:9px; height:9px; border-radius:99px; background:var(--warn); }}
.audit-row.ok .audit-dot {{ background:var(--good); }}
.audit-row.bad .audit-dot {{ background:var(--bad); }}
.audit-row small {{ color:var(--soft); }}
.notice {{
  min-height:42px;
  padding:10px 12px;
  border-radius:7px;
  border:1px solid var(--line);
  background:oklch(19% 0.012 92 / .82);
  overflow-wrap:anywhere;
}}
.notice.strong {{ border-color:oklch(55% 0.075 74); background:oklch(25% 0.035 76 / .7); }}
.split {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; }}
.danger-zone {{ border-color:oklch(52% 0.09 28); background:linear-gradient(180deg, oklch(25% 0.035 35 / .8), var(--surface)); }}
.trace {{ width:100%; border-collapse:collapse; table-layout:fixed; }}
.trace th, .trace td {{ padding:9px 10px; border-top:1px solid var(--line); vertical-align:top; }}
.trace th {{ text-align:left; color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.06em; }}
.trace td:first-child {{ width:72px; color:var(--soft); }}
.trace td:nth-child(2) {{ width:112px; color:var(--accent); font-weight:750; }}
.trace td:last-child {{ overflow-wrap:anywhere; }}
.trace-wrap {{ max-height:390px; overflow:auto; border-top:1px solid var(--line); }}
.trace-wrap .trace th {{ position:sticky; top:0; background:var(--surface); }}
.toast {{ color:var(--muted); font-weight:650; min-height:20px; }}
.toast.ok {{ color:var(--good); }}
.toast.bad {{ color:var(--bad); }}
.toast.warn {{ color:var(--warn); }}
.admin-address {{ font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:12px; color:var(--info); overflow-wrap:anywhere; }}
@media (max-width: 1080px) {{
  .command-strip, .layout, .grid-main {{ grid-template-columns:1fr; }}
  .rail {{ position:static; grid-row:auto; }}
  .metrics {{ grid-template-columns:repeat(2,minmax(0,1fr)); }}
}}
@media (max-width: 700px) {{
  html, body {{ width:100%; max-width:100%; }}
  .shell {{ width:100vw; max-width:100vw; padding:12px; overflow:hidden; }}
  .topbar, .command-strip, .layout, .content, .grid-main, .stack {{ width:100%; max-width:100%; min-width:0; overflow:hidden; }}
  .topbar > *, .command-strip > *, .layout > *, .content > *, .grid-main > *, .stack > *, .panel, .metric {{ max-width:100%; min-width:0; }}
  .topbar {{ grid-template-columns:1fr; align-items:start; }}
  .topbar .subtle {{ max-width:calc(100vw - 24px); }}
  .top-actions {{ justify-content:flex-start; }}
  .command-card {{ flex-direction:column; align-items:flex-start; width:100%; max-width:calc(100vw - 24px); }}
  .command-card .button, .command-card button {{ align-self:flex-start; max-width:100%; }}
  .panel-title {{ align-items:flex-start; flex-direction:column; }}
  .audit-row {{ grid-template-columns:18px minmax(0, 1fr); }}
  .audit-row small {{ grid-column:2; }}
  .metrics, .controls, .split, .button-row {{ grid-template-columns:1fr; }}
  h1 {{ font-size:24px; }}
  .trace td:nth-child(2), .trace th:nth-child(2) {{ display:none; }}
}}
</style>
</head>
<body>
<main class="shell">
  <header class="topbar">
    <div>
      <div class="eyebrow">Mitel 5360 Lab</div>
      <h1>Apartment Lab Admin</h1>
      <div class="subtle">MacBook {LAB_HOST} · BOOTZ {BOOTZ_PHONE_HOST} · ext {PHONE_USER}</div>
    </div>
    <div class="top-actions">
      <a class="button secondary" href="/admin">Dashboard</a>
      <a class="button" id="nativeAdminTop" href="{PHONE_ADMIN_LAN_URL}/" target="_blank" rel="noreferrer">Native admin</a>
    </div>
  </header>
  <section class="command-strip" id="commands">
    <div class="command-card primary">
      <div>
        <div class="command-title">Primary path: BOOTZ audio</div>
        <div class="command-meta">Streams generated audio to the phone through the Windows bridge.</div>
      </div>
      <button id="bootzAudioTop">Call</button>
    </div>
    <div class="command-card">
      <div>
        <div class="command-title">Native admin</div>
        <div class="command-meta admin-address">{PHONE_ADMIN_LAN_URL}/</div>
      </div>
      <a class="button secondary" href="{PHONE_ADMIN_LAN_URL}/" target="_blank" rel="noreferrer">Open</a>
    </div>
    <div class="command-card">
      <div>
        <div class="command-title">Control check</div>
        <div class="command-meta" id="controlSummary">Not run yet</div>
      </div>
      <button class="secondary" id="runControlCheck">Check</button>
    </div>
    <div class="command-card">
      <div>
        <div class="command-title">Last action</div>
        <div class="command-meta" id="lastActionMeta">Idle</div>
      </div>
      <span class="badge warn" id="actionBadge">Ready</span>
    </div>
  </section>
  <div class="layout">
    <aside class="rail">
      <section class="panel">
        <nav class="nav">
          <a href="#status"><span>Status</span><span>live</span></a>
          <a href="#audio"><span>Audio</span><span>call</span></a>
          <a href="#display"><span>Display</span><span>feed</span></a>
          <a href="#delivery"><span>Phone UI</span><span>keys</span></a>
          <a href="#native"><span>Native admin</span><span>port {PHONE_ADMIN_TUNNEL_PORT}</span></a>
          <a href="#trace"><span>Trace</span><span>events</span></a>
        </nav>
      </section>
      <section class="panel pad" id="healthPanel">
        <div class="panel-title"><h2>Health</h2><div class="toast" id="actionStatus">Ready</div></div>
        <div class="status-stack">
          <div class="status-row"><span class="dot" id="regDot"></span><div><div class="row-label">Registration</div><div class="row-value" id="registration">Checking</div></div></div>
          <div class="status-row"><span class="dot warn" id="adminDot"></span><div><div class="row-label">Admin tunnel</div><div class="row-value" id="adminState">{PHONE_ADMIN_LAN_URL}</div></div></div>
          <div class="status-row"><span class="dot warn" id="replayDot"></span><div><div class="row-label">Replay</div><div class="row-value" id="replayState">Checking</div></div></div>
        </div>
        <div class="control-audit" id="controlAudit"></div>
      </section>
    </aside>
    <div class="content">
      <section class="metrics" id="status">
        <div class="metric"><div class="label">Phone</div><div class="value" id="phoneIdentity">{PHONE_MAC}</div><div class="value small" id="phoneNetwork">SIP {PHONE_HOST}</div></div>
        <div class="metric"><div class="label">Current URL</div><div class="value small" id="provisioning">Checking</div></div>
        <div class="metric"><div class="label">Call State</div><div class="value small" id="callState">No call yet</div></div>
        <div class="metric"><div class="label">Native Admin</div><div class="value small admin-address" id="nativeAdminAddress">{PHONE_ADMIN_LAN_URL}/</div></div>
      </section>
      <div class="grid-main">
        <section class="panel pad feature" id="audio">
          <div class="panel-title"><h2>Audio Control</h2><div class="subtle" id="currentMessage">Apartment hotline armed</div></div>
          <div class="controls">
            <label class="field"><span>Caller preset</span><select id="preset">
              <option value="lab">Apartment Lab</option>
              <option value="red">RED PHONE</option>
              <option value="spirit">Spirit Box</option>
              <option value="future">Future You</option>
              <option value="ops">Kitchen Operations</option>
            </select></label>
            <label class="field"><span>Custom caller ID</span><input id="caller" maxlength="40" placeholder="Optional"></label>
            <label class="field"><span>Audio mode</span><select id="audioMode">
              <option value="tone">Tone</option>
              <option value="pulse">Pulse</option>
              <option value="scanner">Radio scanner</option>
              <option value="spirit">Spirit box</option>
              <option value="file">WAV file</option>
            </select></label>
            <label class="field"><span>Sound file</span><select id="soundFile"><option value="">Generated audio</option></select></label>
            <label class="field"><span>Base frequency</span><input id="frequency" type="number" min="40" max="1800" value="440"></label>
            <label class="field"><span>Seconds</span><input id="seconds" type="number" min="2" max="120" value="8"></label>
          </div>
          <div class="buttons">
            <button id="bootzAudio">Call via BOOTZ</button>
            <button class="secondary" id="ring">Call SIP target</button>
            <button class="danger" id="stop">Stop ringing</button>
            <button class="secondary" id="reregister">Re-register</button>
          </div>
        </section>
        <div class="stack">
          <section class="panel pad" id="display">
            <div class="panel-title"><h2>Display Feed</h2></div>
            <label class="field"><span>Phone message</span><input id="message" maxlength="120" placeholder="Apartment hotline armed"></label>
            <div class="buttons"><button class="secondary" id="setMessage">Set feed text</button></div>
          </section>
          <section class="panel pad" id="native">
            <div class="panel-title"><h2>Native Admin</h2></div>
            <div class="notice strong admin-address" id="nativeAdminCard">{PHONE_ADMIN_LAN_URL}/</div>
            <div class="buttons">
              <a class="button" id="nativeAdminLink" href="{PHONE_ADMIN_LAN_URL}/" target="_blank" rel="noreferrer">Open native admin</a>
              <a class="button secondary" id="downloadConfigLink" href="{PHONE_ADMIN_LAN_URL}/download.txt" target="_blank" rel="noreferrer">Download config</a>
            </div>
          </section>
        </div>
      </div>
      <div class="grid-main">
        <section class="panel pad" id="delivery">
          <div class="panel-title"><h2>Phone UI Delivery</h2><div class="subtle" id="deliveryState">SIP safe mode</div></div>
          <div class="controls">
            <label class="field"><span>Target UI</span><select id="appTarget">
              <option value="safe">Live phone UI (/app)</option>
              <option value="official">Apartment GRM SPX</option>
              <option value="rich">Apartment rich GRM SPX</option>
              <option value="sample">Mitel GRM sample SPX</option>
              <option value="mcdsample">MCD-path GRM sample SPX</option>
              <option value="fullscreen">Mitel full-screen sample SPX</option>
              <option value="redirectfs">Apartment full-screen key SPX</option>
              <option value="notification">Apartment notification SPX</option>
              <option value="help">Mitel bundled help SPX</option>
              <option value="screensaver">Mitel bundled screensaver SPX</option>
            </select></label>
            <label class="field"><span>Temporary launch seconds</span><input id="launchSeconds" type="number" min="5" max="120" value="30"></label>
          </div>
          <div class="split">
            <div>
              <h3>Preferred actions</h3>
              <div class="button-row">
                <button id="setKeyApp">Set Apartment key</button>
                <button class="secondary" id="probeSpx">Probe SPX</button>
                <button class="secondary" id="launchTemporary">Launch temporary</button>
                <button class="secondary" id="restoreNativeBoot">Restore native boot</button>
              </div>
            </div>
            <div>
              <h3>Provisioning</h3>
              <div class="button-row">
                <button class="secondary" id="serveProvision">Serve on boot</button>
                <button class="secondary" id="serveMandatory">Serve mandatory</button>
                <button class="secondary" id="setHtmlApp">Set global URL</button>
                <button class="secondary" id="restoreSipMode">Restore SIP config</button>
              </div>
            </div>
          </div>
        </section>
        <section class="panel pad danger-zone" id="risk">
          <div class="panel-title"><h2>Risk Bench</h2><div class="subtle">Reversible controls</div></div>
          <div class="button-row">
            <button id="enableCustomGui">Enable GUI path</button>
            <button class="secondary" id="clearUpgrade">Clear upgrade error</button>
            <button class="secondary" id="rebootPhone">Reboot phone</button>
            <button class="danger" id="minetProbe">MiNET probe + reboot</button>
            <button class="secondary" id="loadReplay">Load replay captures</button>
            <button class="secondary" id="toggleReplay">Toggle replay</button>
          </div>
        </section>
      </div>
      <section class="panel" id="trace">
        <div class="panel-title" style="padding:16px 16px 0"><h2>Live Trace</h2><div class="subtle" id="traceCount">0 events</div></div>
        <div class="trace-wrap">
          <table class="trace">
            <thead><tr><th>Time</th><th>Lane</th><th>Event</th></tr></thead>
            <tbody id="events"></tbody>
          </table>
        </div>
      </section>
    </div>
  </div>
</main>
<script>
const qs = (id) => document.getElementById(id);
const actionStatus = qs('actionStatus');
const actionBadge = qs('actionBadge');
const CONTROL_ACTIONS = {json.dumps(CONTROL_ACTIONS)};
const RISK_CONFIRM = {{
  serveMandatory: 'Mandatory boot provisioning can re-enter the HTML App Upgrade path. Continue?',
  serveProvision: 'Serving a boot URL can re-enter the HTML App Upgrade path on this phone. Continue?',
  setHtmlApp: 'Setting a global HTML URL can trigger the phone browser or upgrade flow. Continue?',
  launchTemporary: 'This temporarily changes the phone HTML URL, then restores it. Continue?',
  enableCustomGui: 'This changes phone HTML settings and requests a reboot. Continue?',
  clearUpgrade: 'This clears HTML boot settings and requests a reboot. Continue?',
  rebootPhone: 'This will reboot the phone now. Continue?',
  minetProbe: 'This temporarily serves MiNET mode and reboots the phone. Continue?'
}};

function setActionState(kind, message) {{
  actionStatus.className = `toast ${{kind || ''}}`;
  actionStatus.textContent = message;
  actionBadge.className = `badge ${{kind || 'warn'}}`;
  actionBadge.textContent = kind === 'ok' ? 'OK' : kind === 'bad' ? 'Error' : 'Ready';
  qs('lastActionMeta').textContent = message;
}}

async function post(path, data = {{}}, button = null) {{
  const body = new URLSearchParams(data);
  if (button) {{
    button.disabled = true;
    button.dataset.originalText = button.textContent;
    button.textContent = 'Working';
  }}
  setActionState('warn', 'Working');
  try {{
    const response = await fetch(path, {{ method: 'POST', body }});
    const contentType = response.headers.get('content-type') || '';
    const json = contentType.includes('application/json')
      ? await response.json()
      : {{ ok: response.ok, message: await response.text() }};
    if (!response.ok) throw new Error(json.error || json.message || `HTTP ${{response.status}}`);
    if (!json.ok) throw new Error(json.error || 'Request failed');
    setActionState('ok', json.message || 'Done');
    await refresh();
    return json;
  }} catch (error) {{
    setActionState('bad', error.message || String(error));
    throw error;
  }} finally {{
    if (button) {{
      button.disabled = false;
      button.textContent = button.dataset.originalText;
    }}
  }}
}}

function runAction(button, path, data = {{}}, options = {{}}) {{
  const id = button && button.id;
  if (id && RISK_CONFIRM[id] && !confirm(RISK_CONFIRM[id])) {{
    setActionState('warn', 'Cancelled');
    return Promise.resolve(null);
  }}
  return post(path, data, button).catch((error) => {{
    console.error(error);
    return null;
  }});
}}

function renderEvents(events) {{
  qs('events').innerHTML = events.slice().reverse().map((event) => `
    <tr><td>${{escapeHtml(event.time)}}</td><td>${{escapeHtml(event.lane)}}</td><td>${{escapeHtml(event.message)}}</td></tr>
  `).join('');
  qs('traceCount').textContent = `${{events.length}} events`;
}}

function escapeHtml(value) {{
  return String(value).replace(/[&<>"']/g, (char) => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[char]));
}}

function renderControlAudit(result, domIssues = []) {{
  const rows = [];
  const native = result && result.native_admin ? result.native_admin : {{}};
  rows.push({{
    label: 'Native admin tunnel',
    meta: native.ok ? `${{native.model || 'Mitel'}} ${{native.ip || ''}} ${{native.sip_mode || ''}}` : (native.error || 'unavailable'),
    ok: Boolean(native.ok)
  }});
  const missingEndpoints = result && result.missing_endpoints ? result.missing_endpoints : [];
  rows.push({{
    label: 'Server endpoints',
    meta: missingEndpoints.length ? missingEndpoints.join(', ') : `${{(result.counts && result.counts.total) || CONTROL_ACTIONS.length}} mapped`,
    ok: !missingEndpoints.length
  }});
  rows.push({{
    label: 'DOM wiring',
    meta: domIssues.length ? domIssues.join(', ') : 'all visible controls found',
    ok: !domIssues.length
  }});
  qs('controlAudit').innerHTML = rows.map((row) => `
    <div class="audit-row ${{row.ok ? 'ok' : 'bad'}}"><span class="audit-dot"></span><span>${{escapeHtml(row.label)}}</span><small>${{escapeHtml(row.meta)}}</small></div>
  `).join('');
  const ok = rows.every((row) => row.ok);
  qs('controlSummary').textContent = ok
    ? `${{(result.counts && result.counts.total) || CONTROL_ACTIONS.length}} controls wired`
    : 'control issue found';
  return ok;
}}

async function runControlCheck(button = null) {{
  if (button) {{
    button.disabled = true;
    button.dataset.originalText = button.textContent;
    button.textContent = 'Checking';
  }}
  try {{
    const response = await fetch('/api/control-check', {{ cache: 'no-store' }});
    const result = await response.json();
    const domIssues = CONTROL_ACTIONS
      .filter((item) => !document.getElementById(item.id))
      .map((item) => `${{item.id}} missing`);
    const ok = renderControlAudit(result, domIssues);
    setActionState(ok ? 'ok' : 'bad', ok ? 'All controls wired' : 'Control check failed');
    return result;
  }} catch (error) {{
    renderControlAudit({{ ok: false, missing_endpoints: [], native_admin: {{ ok: false, error: error.message }} }}, []);
    setActionState('bad', error.message || String(error));
    return null;
  }} finally {{
    if (button) {{
      button.disabled = false;
      button.textContent = button.dataset.originalText;
    }}
  }}
}}

function latestCall(activeCalls) {{
  const entries = Object.entries(activeCalls || {{}});
  if (!entries.length) return 'No call yet';
  const [callId, status] = entries[entries.length - 1];
  return `${{callId}} / ${{status}}`;
}}

async function refresh() {{
  const response = await fetch('/api/status', {{ cache: 'no-store' }});
  const data = await response.json();
  const reg = data.registered;
  qs('regDot').classList.toggle('ok', Boolean(reg));
  qs('registration').textContent = reg ? `${{reg.uri || 'registered'}} at ${{reg.at || 'phone'}}` : 'Not registered';
  const provisioning = data.provisioning || {{}};
  const replay = data.minet_replay || {{}};
  const phone = data.phone || {{}};
  const nativeAdmin = phone.native_admin_url || '{PHONE_ADMIN_LAN_URL}';
  qs('nativeAdminAddress').textContent = `${{nativeAdmin}}/`;
  qs('nativeAdminCard').textContent = `${{nativeAdmin}}/`;
  qs('adminState').textContent = nativeAdmin;
  qs('adminDot').classList.add('ok');
  qs('adminDot').classList.remove('warn');
  qs('nativeAdminLink').href = `${{nativeAdmin}}/`;
  qs('nativeAdminTop').href = `${{nativeAdmin}}/`;
  qs('downloadConfigLink').href = `${{nativeAdmin}}/download.txt`;
  qs('phoneIdentity').textContent = `${{phone.mac || '{PHONE_MAC}'}} · ${{phone.user || '{PHONE_USER}'}}`;
  qs('phoneNetwork').textContent = `SIP ${{phone.host || '{PHONE_HOST}'}} · BOOTZ ${{phone.bootz_host || '{BOOTZ_PHONE_HOST}'}}`;
  qs('provisioning').textContent = `${{provisioning.html_url || 'empty'}} · mandatory=${{provisioning.mandatory || '0'}} · mode=${{provisioning.sip_mode || 'sip'}}`;
  qs('deliveryState').textContent = `replay=${{replay.enabled ? 'on' : 'off'}} · captures=${{replay.files || 0}}`;
  qs('replayState').textContent = replay.enabled ? 'Enabled' : 'Off';
  qs('replayDot').classList.toggle('ok', Boolean(replay.enabled));
  qs('replayDot').classList.toggle('warn', !replay.enabled);
  qs('currentMessage').textContent = data.message || '';
  qs('callState').textContent = latestCall(data.active_calls);
  const soundSelect = qs('soundFile');
  const currentSound = soundSelect.value;
  const files = data.sound_files || [];
  soundSelect.innerHTML = '<option value="">Generated audio</option>' + files.map((file) => `<option value="${{escapeHtml(file)}}">${{escapeHtml(file)}}</option>`).join('');
  soundSelect.value = files.includes(currentSound) ? currentSound : '';
  renderEvents(data.events || []);
}}

qs('ring').addEventListener('click', (event) => runAction(event.currentTarget, '/api/ring', {{
  preset: qs('preset').value,
  caller: qs('caller').value,
  frequency: qs('frequency').value,
  seconds: qs('seconds').value,
  mode: qs('audioMode').value,
  sound: qs('soundFile').value
}}));
function bootzAudioPayload() {{
  return {{
    preset: qs('preset').value,
    caller: qs('caller').value,
    frequency: qs('frequency').value,
    seconds: qs('seconds').value,
    mode: qs('audioMode').value
  }};
}}
qs('bootzAudio').addEventListener('click', (event) => runAction(event.currentTarget, '/api/bootz-audio', bootzAudioPayload()));
qs('bootzAudioTop').addEventListener('click', (event) => runAction(event.currentTarget, '/api/bootz-audio', {{
  preset: qs('preset').value,
  caller: qs('caller').value,
  frequency: qs('frequency').value,
  seconds: qs('seconds').value,
  mode: qs('audioMode').value
}}));
qs('stop').addEventListener('click', (event) => runAction(event.currentTarget, '/api/stop'));
qs('reregister').addEventListener('click', (event) => runAction(event.currentTarget, '/api/reregister'));
qs('setMessage').addEventListener('click', (event) => runAction(event.currentTarget, '/api/message', {{ message: qs('message').value }}));
qs('setHtmlApp').addEventListener('click', (event) => runAction(event.currentTarget, '/api/html-app', {{ target: qs('appTarget').value, force: 1 }}));
qs('setKeyApp').addEventListener('click', (event) => runAction(event.currentTarget, '/api/apartment-key', {{ target: qs('appTarget').value }}));
qs('probeSpx').addEventListener('click', (event) => runAction(event.currentTarget, '/api/probe-spx', {{ target: qs('appTarget').value, seconds: 5 }}));
qs('launchTemporary').addEventListener('click', (event) => runAction(event.currentTarget, '/api/launch-temporary', {{ target: qs('appTarget').value, seconds: qs('launchSeconds').value }}));
qs('serveProvision').addEventListener('click', (event) => runAction(event.currentTarget, '/api/provisioning-html', {{ target: qs('appTarget').value, mandatory: 0, force: 1 }}));
qs('serveMandatory').addEventListener('click', (event) => runAction(event.currentTarget, '/api/provisioning-html', {{ target: qs('appTarget').value, mandatory: 1, force: 1 }}));
qs('enableCustomGui').addEventListener('click', (event) => runAction(event.currentTarget, '/api/enable-custom-gui', {{ reboot: 1 }}));
qs('clearUpgrade').addEventListener('click', (event) => runAction(event.currentTarget, '/api/clear-html-upgrade', {{ reboot: 1 }}));
qs('rebootPhone').addEventListener('click', (event) => runAction(event.currentTarget, '/api/reboot-phone'));
qs('restoreNativeBoot').addEventListener('click', (event) => runAction(event.currentTarget, '/api/restore-native-boot'));
qs('minetProbe').addEventListener('click', (event) => runAction(event.currentTarget, '/api/minet-probe', {{ seconds: 180, reboot: 1 }}));
qs('loadReplay').addEventListener('click', async (event) => {{
  const result = await runAction(event.currentTarget, '/api/minet-replay-load');
  if (result) setActionState('ok', `Loaded replay captures: ${{JSON.stringify(result.ports || {{}})}}`);
}});
qs('toggleReplay').addEventListener('click', async (event) => {{
  const status = await fetch('/api/status', {{ cache: 'no-store' }}).then((r) => r.json());
  const enabled = !(status.minet_replay && status.minet_replay.enabled);
  await runAction(event.currentTarget, '/api/minet-replay-enable', {{ enable: enabled ? 1 : 0 }});
}});
qs('restoreSipMode').addEventListener('click', (event) => runAction(event.currentTarget, '/api/restore-sip-mode'));
qs('setAiVoiceKey') && qs('setAiVoiceKey').addEventListener('click', (event) => runAction(event.currentTarget, '/api/set-ai-voice-key'));
qs('clearPhoneKeys') && qs('clearPhoneKeys').addEventListener('click', (event) => {{
  if (!confirm('Clear ALL programmable keys (1..32) on the phone? Line keys will return on next config refresh.')) return;
  runAction(event.currentTarget, '/api/clear-phone-keys');
}});
qs('aiVoiceStatus') && qs('aiVoiceStatus').addEventListener('click', async (event) => {{
  const r = await fetch('/api/ai-voice-status').then(r => r.json()).catch(e => ({{ ok:false, error:String(e) }}));
  event.currentTarget.title = JSON.stringify(r);
  alert(`AI voice: ${{r.count}} active, loaded=${{r.loaded}}`);
}});
qs('runControlCheck').addEventListener('click', (event) => runControlCheck(event.currentTarget));
qs('preset').addEventListener('change', async () => {{
  const response = await fetch('/api/status', {{ cache: 'no-store' }});
  const data = await response.json();
  const preset = data.presets[qs('preset').value];
  if (preset) {{
    qs('frequency').value = preset.frequency;
    qs('seconds').value = preset.seconds;
    qs('audioMode').value = preset.mode || 'tone';
  }}
}});
refresh();
runControlCheck();
setInterval(refresh, 1500);
</script>
</body>
</html>
'''


def _drain_socket(sock, total_cap=65536, idle_timeout=1.0):
    sock.settimeout(idle_timeout)
    chunks = []
    received = 0
    try:
        while received < total_cap:
            chunk = sock.recv(min(8192, total_cap - received))
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
    except (socket.timeout, ssl.SSLWantReadError):
        pass
    return b"".join(chunks)


def mitel_raw_probe(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    sock.listen(8)
    state.log("mitel", f"raw listening on {port}")
    while True:
        conn, addr = sock.accept()
        with conn:
            try:
                data = _drain_socket(conn)
            except Exception as exc:
                record_mitel_probe_hit("raw-error", port, addr, note=repr(exc))
                state.log("mitel raw", f"{addr[0]}:{addr[1]} {exc!r}")
                continue
            record_mitel_probe_hit("raw", port, addr, data)
            state.log(
                "mitel<-",
                f"{addr[0]}:{addr[1]} port={port} bytes={len(data)} "
                f"hex={data[:48].hex()}{'...' if len(data) > 48 else ''}",
            )
            # Port 6800: respond to Vers frame (msg_type=2) with ack (msg_type=3)
            if port == 6800 and len(data) >= 80:
                if int.from_bytes(data[0:4], "big") == 2:
                    with state.lock:
                        state._last_vers_data = data
                    ack = build_minet_vers_ack(data)
                    if ack:
                        try:
                            conn.sendall(ack)
                            state.log(
                                "mitel->",
                                f"{addr[0]}:{addr[1]} port=6800 sent Vers-Ack "
                                f"{len(ack)}b hex={ack[:16].hex()}",
                            )
                        except Exception as exc:
                            state.log("mitel->", f"6800 ack send failed: {exc!r}")
            # Port 6802: AES-encrypted channel; send a minimal 8-byte ack and capture
            elif port == 6802 and data:
                try:
                    # Phone sends a fixed 176-byte challenge; echo it back (8-byte ack alone did not advance).
                    ack_6802 = data if len(data) == 176 else (2).to_bytes(4, "big") + (0).to_bytes(4, "big")
                    conn.sendall(ack_6802)
                    state.log(
                        "mitel->",
                        f"{addr[0]}:{addr[1]} port=6802 sent 8-byte ack",
                    )
                except Exception as exc:
                    state.log("mitel->", f"6802 ack send failed: {exc!r}")
            # Optional replay lane for raw ports using captured controller frames.
            if port in (3999, 6800, 6802):
                maybe_send_minet_replay(conn, port, addr, "raw")


def mitel_tls_probe(port, cert, key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers("ECDHE+AESGCM:ECDHE+AES:DHE+AES:AESGCM:AES:@SECLEVEL=1")
    ctx.load_cert_chain(cert, key)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    sock.listen(8)
    state.log("mitel", f"TLS listening on {port}")
    while True:
        conn, addr = sock.accept()
        cipher_info = None
        data = b""
        try:
            with ctx.wrap_socket(conn, server_side=True) as tls:
                cipher_info = tls.cipher()
                state.log("mitel<-tls", f"{addr[0]}:{addr[1]} port={port} cipher={cipher_info}")
                if port == 6801:
                    # Phone often resets if the server talks first with the wrong frame.
                    first = _drain_socket(tls, idle_timeout=2.5)
                    if first:
                        record_mitel_probe_hit("tls-first", port, addr, first, note=f"cipher={cipher_info}")
                        state.log(
                            "mitel<-tls",
                            f"{addr[0]}:{addr[1]} first-bytes={len(first)} "
                            f"hex={first[:48].hex()}{'...' if len(first) > 48 else ''}",
                        )
                    if len(first) >= 80 and int.from_bytes(first[0:4], "big") == 2:
                        with state.lock:
                            state._last_vers_data = first
                        ack = build_minet_vers_ack(first)
                        if ack:
                            tls.sendall(ack)
                            state.log(
                                "mitel->",
                                f"{addr[0]}:{addr[1]} port=6801 sent Vers-Ack "
                                f"{len(ack)}b hex={ack[:16].hex()}",
                            )
                    elif not first:
                        hello = build_minet_controller_hello()
                        if hello:
                            tls.sendall(hello)
                            state.log(
                                "mitel->",
                                f"{addr[0]}:{addr[1]} port=6801 sent controller-hello "
                                f"{len(hello)}b hex={hello[:16].hex()}",
                            )
                    try:
                        data = first + _drain_socket(tls)
                    except Exception as exc:
                        record_mitel_probe_hit(
                            "tls-partial",
                            port,
                            addr,
                            data=first,
                            note=f"cipher={cipher_info} drain_exc={exc!r}",
                        )
                        state.log("mitel tls", f"{addr[0]}:{addr[1]} drain {exc!r} after {len(first)} bytes")
                        continue
                else:
                    try:
                        data = _drain_socket(tls)
                    except Exception as exc:
                        record_mitel_probe_hit(
                            "tls-partial",
                            port,
                            addr,
                            data=data,
                            note=f"cipher={cipher_info} drain_exc={exc!r}",
                        )
                        state.log("mitel tls", f"{addr[0]}:{addr[1]} drain {exc!r} after {len(data)} bytes")
                        continue
                record_mitel_probe_hit("tls", port, addr, data, note=f"cipher={cipher_info}")
                state.log(
                    "mitel<-tls",
                    f"{addr[0]}:{addr[1]} bytes={len(data)} "
                    f"hex={data[:48].hex()}{'...' if len(data) > 48 else ''}",
                )
                # Avoid replaying on TLS ports until we capture plaintext app-level frames.
                if port in (3998, 6881):
                    maybe_send_minet_replay(tls, port, addr, "tls")
        except Exception as exc:
            record_mitel_probe_hit(
                "tls-error",
                port,
                addr,
                data=data,
                note=f"cipher={cipher_info} exc={exc!r}",
            )
            state.log("mitel tls", f"{addr[0]}:{addr[1]} {exc!r}")


def secure_html_change_server(port, cert, key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers("ECDHE+AESGCM:ECDHE+AES:DHE+AES:AESGCM:AES:@SECLEVEL=1")
    ctx.load_cert_chain(cert, key)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    sock.listen(8)
    state.log("mcd-2002", f"secure HTML change listener on {port}")
    while True:
        conn, addr = sock.accept()
        start_thread(handle_secure_html_change, ctx, conn, addr, port)


def handle_secure_html_change(ctx, conn, addr, port):
    with conn:
        try:
            with ctx.wrap_socket(conn, server_side=True) as tls:
                tls.settimeout(10)
                cipher_info = tls.cipher()
                state.log("mcd-2002", f"{addr[0]}:{addr[1]} TLS cipher={cipher_info}")
                tls.sendall(b"login: ")
                login = _read_telnet_line(tls)
                tls.sendall(b"password: ")
                _password = _read_telnet_line(tls)
                tls.sendall(b"\r\nMitel 3300 ICP\r\n> ")
                command = _read_telnet_line(tls)
                state.log("mcd-2002<-", f"{addr[0]}:{addr[1]} login={login!r} command={command!r}")
                if "SacSendAllHtmlAppChange" in command:
                    tls.sendall(b"SacSendAllHtmlAppChange: Complete\r\n> ")
                else:
                    tls.sendall(b"Command complete\r\n> ")
                record_mitel_probe_hit("mcd-2002", port, addr, command.encode("utf-8", "replace"), note=f"cipher={cipher_info}")
        except Exception as exc:
            state.log("mcd-2002", f"{addr[0]}:{addr[1]} {exc!r}")


def _read_telnet_line(sock):
    chunks = []
    while len(chunks) < 4096:
        char = sock.recv(1)
        if not char:
            break
        if char in (b"\n", b"\r"):
            if chunks:
                break
            continue
        chunks.append(char)
    return b"".join(chunks).decode("utf-8", "replace")


def start_https_server(bind_host, bind_port, cert, key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers("ECDHE+AESGCM:ECDHE+AES:DHE+AES:AESGCM:AES:@SECLEVEL=1")
    ctx.load_cert_chain(cert, key)
    server = ThreadingHTTPServer((bind_host, bind_port), LabHTTP)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    state.log("https", f"MiXML/config listening on {bind_host}:{bind_port}")
    server.serve_forever()


def parse_tftp_rrq(data):
    if len(data) < 4 or data[:2] != b"\x00\x01":
        return None
    parts = data[2:].split(b"\x00")
    if len(parts) < 2:
        return None
    filename = parts[0].decode("utf-8", "replace")
    mode = parts[1].decode("ascii", "replace").lower()
    options = {}
    tail = parts[2:]
    for i in range(0, len(tail) - 1, 2):
        if not tail[i]:
            continue
        options[tail[i].decode("ascii", "replace").lower()] = tail[i + 1].decode("ascii", "replace")
    return filename, mode, options


def tftp_error(sock, addr, code, message):
    sock.sendto(b"\x00\x05" + code.to_bytes(2, "big") + message.encode("ascii", "replace") + b"\x00", addr)


def _append_tftp_diag(message):
    try:
        os.makedirs(os.path.dirname(TFTP_DIAG_LOG), exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S %z")
        with open(TFTP_DIAG_LOG, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except Exception as exc:
        state.log("tftp", f"diag write failed: {exc!r}")


def tftp_serve_file(addr, filename, body, options):
    raw_block_size = options.get("blksize", "512") or "512"
    raw_window_size = options.get("wndsize", "1") or "1"
    try:
        block_size = min(max(int(raw_block_size), 8), 4096)
    except ValueError:
        block_size = 512
    if TFTP_FORCE_BLKSIZE:
        block_size = min(block_size, TFTP_FORCE_BLKSIZE)
    try:
        window_size = min(max(int(raw_window_size), 1), 32)
    except ValueError:
        window_size = 1
    if TFTP_FORCE_WNDSIZE:
        window_size = min(window_size, TFTP_FORCE_WNDSIZE)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    server_port = sock.getsockname()[1]
    sock.settimeout(2)
    _append_tftp_diag(
        f"start file={filename} bytes={len(body)} peer={addr[0]}:{addr[1]} "
        f"server_port={server_port} requested_options={dict(options)} "
        f"negotiated_blksize={block_size} negotiated_wndsize={window_size} "
        f"force_blksize={TFTP_FORCE_BLKSIZE or '-'} "
        f"force_wndsize={TFTP_FORCE_WNDSIZE or '-'}"
    )
    try:
        if options:
            oack_parts = []
            if "blksize" in options:
                oack_parts.extend([b"blksize", str(block_size).encode("ascii")])
            if "tsize" in options:
                oack_parts.extend([b"tsize", str(len(body)).encode("ascii")])
            if "wndsize" in options:
                oack_parts.extend([b"wndsize", str(window_size).encode("ascii")])
            oack = b"\x00\x06" + b"\x00".join(oack_parts) + b"\x00"
            sock.sendto(oack, addr)
            try:
                ack, ack_addr = sock.recvfrom(2048)
            except socket.timeout:
                state.log("tftp", f"{addr[0]}:{addr[1]} OACK timeout {filename}")
                _append_tftp_diag(
                    f"oack-timeout file={filename} peer={addr[0]}:{addr[1]} "
                    f"server_port={server_port} blksize={block_size} wndsize={window_size}"
                )
                return
            if ack[:2] != b"\x00\x04":
                _append_tftp_diag(
                    f"oack-rejected file={filename} peer={addr[0]}:{addr[1]} "
                    f"ack_peer={ack_addr[0]}:{ack_addr[1]} opcode={ack[:2].hex()} "
                    f"raw={ack[:32].hex()}"
                )
                state.log("tftp", f"{addr[0]}:{addr[1]} rejected OACK opcode={ack[:2].hex()} for {filename}")
                return
            addr = ack_addr
        block = 1
        offset = 0
        while True:
            sent = []
            last_chunk = None
            window_start_offset = offset
            for _ in range(window_size):
                chunk = body[offset:offset + block_size]
                packet = b"\x00\x03" + block.to_bytes(2, "big") + chunk
                sent.append((block, packet, len(chunk)))
                last_chunk = chunk
                offset += block_size
                block = (block + 1) % 65536
                if len(chunk) < block_size:
                    break
            last_block = sent[-1][0]
            for _ in range(4):
                for _, packet, _chunk_len in sent:
                    sock.sendto(packet, addr)
                try:
                    ack, ack_addr = sock.recvfrom(2048)
                    if ack[:2] == b"\x00\x05":
                        code = int.from_bytes(ack[2:4], "big", signed=False)
                        signed_code = int.from_bytes(ack[2:4], "big", signed=True)
                        message = ack[4:].rstrip(b"\x00").decode("ascii", "replace")
                        bytes_sent_in_window = sum(length for _, _, length in sent)
                        diag = (
                            f"abort file={filename} peer={ack_addr[0]}:{ack_addr[1]} "
                            f"server_port={server_port} "
                            f"raw_options={dict(options)} blksize={block_size} wndsize={window_size} "
                            f"last_block_sent={last_block} window_blocks={[b for b, _, _ in sent]} "
                            f"window_start_offset={window_start_offset} "
                            f"window_bytes={bytes_sent_in_window} "
                            f"total_bytes_sent={window_start_offset + bytes_sent_in_window} "
                            f"file_bytes={len(body)} "
                            f"err_code=0x{code:04x} signed={signed_code} message={message!r}"
                        )
                        _append_tftp_diag(diag)
                        state.log(
                            "tftp",
                            f"{ack_addr[0]}:{ack_addr[1]} aborted {filename} "
                            f"code=0x{code:04x} signed={signed_code} message={message!r} "
                            f"last_block={last_block} blksize={block_size} wndsize={window_size}",
                        )
                        return
                    if ack[:2] == b"\x00\x04" and int.from_bytes(ack[2:4], "big") == last_block:
                        break
                except socket.timeout:
                    continue
            else:
                _append_tftp_diag(
                    f"no-ack file={filename} peer={addr[0]}:{addr[1]} "
                    f"server_port={server_port} last_block={last_block} "
                    f"blksize={block_size} wndsize={window_size}"
                )
                state.log("tftp", f"{addr[0]}:{addr[1]} no ACK for {filename} block={last_block}")
                return
            if last_chunk is not None and len(last_chunk) < block_size:
                _append_tftp_diag(
                    f"complete file={filename} peer={addr[0]}:{addr[1]} "
                    f"server_port={server_port} bytes={len(body)} "
                    f"blksize={block_size} wndsize={window_size}"
                )
                state.log("tftp", f"served {filename} to {addr[0]}:{addr[1]} bytes={len(body)}")
                return
    finally:
        sock.close()


def tftp_server(bind_host, bind_port, firmware_dir):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind_host, bind_port))
    state.log("tftp", f"listening on {bind_host}:{bind_port}")
    while True:
        data, addr = sock.recvfrom(8192)
        rrq = parse_tftp_rrq(data)
        if not rrq:
            state.log("tftp<-", f"{addr[0]}:{addr[1]} unsupported packet {data[:16].hex()}")
            continue
        filename, mode, options = rrq
        state.log("tftp<-", f"{addr[0]}:{addr[1]} RRQ {filename} mode={mode} options={options}")
        path_key = "/" + filename
        if path_key in CONFIGS:
            start_thread(tftp_serve_file, addr, filename, CONFIGS[path_key]().encode("utf-8"), options)
            continue
        basename = os.path.basename(filename)
        if TFTP_SKIP_MAINIP and basename == "MainIp5360.bin":
            tftp_error(sock, addr, 1, "MainIp5360.bin skipped by lab config")
            state.log("tftp->", f"{addr[0]}:{addr[1]} SKIP MainIp5360.bin (TFTP_SKIP_MAINIP)")
            continue
        firmware_path = os.path.join(firmware_dir, basename)
        if os.path.exists(firmware_path):
            with open(firmware_path, "rb") as handle:
                body = handle.read()
            start_thread(tftp_serve_file, addr, filename, body, options)
            continue
        tftp_error(sock, addr, 1, "not found in Mitel lab")
        state.log("tftp->", f"{addr[0]}:{addr[1]} ERROR missing {filename}")


def ensure_cert(cert, key):
    if os.path.exists(cert) and os.path.exists(key):
        return
    os.system(
        f"openssl req -x509 -newkey rsa:2048 -nodes -days 7 "
        f"-subj '/CN=mitel-lab' -keyout {key} -out {cert} >/dev/null 2>&1"
    )


def start_thread(fn, *args):
    thread = threading.Thread(target=fn, args=args, daemon=True)
    thread.start()
    return thread


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--http-port", type=int, default=80)
    parser.add_argument("--sip-port", type=int, default=5060)
    parser.add_argument("--tftp-port", type=int, default=69)
    parser.add_argument("--https-port", type=int, default=443)
    parser.add_argument("--ftp-port", type=int, default=21)
    parser.add_argument("--mcd-change-port", type=int, default=2002)
    parser.add_argument("--firmware-dir", default=STATIC_FILE_DIR)
    parser.add_argument("--cert", default="/tmp/mitel_tls_probe.crt")
    parser.add_argument("--key", default="/tmp/mitel_tls_probe.key")
    parser.add_argument("--replay-dir", default=MINET_RELAY_CAPTURE_DIR)
    args = parser.parse_args()
    ensure_cert(args.cert, args.key)
    try:
        load_minet_replay_frames(args.replay_dir)
    except Exception as exc:
        state.log("replay", f"initial load failed dir={args.replay_dir}: {exc!r}")
    start_thread(sip_udp_server, args.host, args.sip_port)
    start_thread(sip_tcp_server, args.host, args.sip_port)
    # Warm voice models at startup so the first call doesn't pay
    # the 15s Whisper cold-start cost.
    def _warm_voice():
        try:
            _get_voice_loop()
        except Exception as exc:
            state.log("voice-warm", f"startup warm err: {exc!r}")
    start_thread(_warm_voice)
    for port in (3999, 6800, 6802):
        start_thread(mitel_raw_probe, port)
    start_thread(mitel_tls_probe, 3998, args.cert, args.key)
    start_thread(mitel_tls_probe, 6801, args.cert, args.key)
    start_thread(mitel_tls_probe, 6881, args.cert, args.key)
    start_thread(secure_html_change_server, args.mcd_change_port, args.cert, args.key)
    start_thread(mcd_ftp_server, args.host, args.ftp_port)
    start_thread(start_https_server, args.host, args.https_port, args.cert, args.key)
    start_thread(tftp_server, args.host, args.tftp_port, args.firmware_dir)
    start_thread(tftp_server, args.host, 20001, args.firmware_dir)
    server = ThreadingHTTPServer((args.host, args.http_port), LabHTTP)
    state.log("http", f"dashboard/config listening on {args.host}:{args.http_port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
