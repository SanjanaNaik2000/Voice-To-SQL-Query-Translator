"""
voice_service.py  --  optional offline voice input: sounddevice + local Whisper (Vosk as fallback)

The Flask server runs on YOUR machine, so "Speak" records from the server computer's own microphone.
Everything runs locally: no cloud service, no API key, no LLM.

    sounddevice      -> int16 PCM samples from the chosen hardware input
    Whisper          -> offline speech-recognition model (via the `faster-whisper` package) turns the
                        audio into text.  It only transcribes; it plays no part in building the SQL.
    Vosk (fallback)  -> used automatically if Whisper is not installed or its model cannot be loaded

Whisper is told the names of your tables and columns (a "hint" prompt), which makes it far more likely
to write "orders" instead of a similar-sounding word.

Settings (environment variables, all optional):
    DBCHAT_VOICE_ENGINE   auto (default) | whisper | vosk
    DBCHAT_WHISPER_MODEL  tiny.en | base.en (default) | small.en | ... (bigger = more accurate, slower)

One-time setup (needs internet once, the model is then cached on disk):
    pip install sounddevice faster-whisper        (the model downloads itself on first use)
    optional fallback:  pip install SpeechRecognition vosk   and   sprc download vosk

The audio libraries are imported lazily, so the dashboard still works if they are not installed;
the Speak button then simply reports what is missing.
"""
from __future__ import annotations

import io
import math
import os
import re
import threading
import wave

import numpy as np

ENGINE = os.environ.get("DBCHAT_VOICE_ENGINE", "auto").strip().lower()
WHISPER_MODEL = os.environ.get("DBCHAT_WHISPER_MODEL", "base.en").strip()
WHISPER_RATE = 16000                                   # Whisper always works on 16 kHz mono audio

# Whisper sometimes "hears" these when there is only noise; a database command is never just this.
_NOISE_PHRASES = {"you", "thank you", "thanks", "thanks for watching", "thank you for watching", "bye"}

_model = None
_model_lock = threading.Lock()


class VoiceError(Exception):
    """A human-readable problem with the voice pipeline."""


def list_input_devices() -> list[dict]:
    """Scan the hardware: every device that has at least one input channel."""
    try:
        import sounddevice as sd
        return [{"index": i, "name": d["name"], "rate": int(d["default_samplerate"])}
                for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
    except Exception:
        return []


# ---------------------------------------------------------------------------- recording
def record(device: int | None = None, seconds: int = 5) -> tuple[np.ndarray, int]:
    """Record a fixed window; returns (int16 mono samples, sample rate)."""
    try:
        import sounddevice as sd
    except (ImportError, OSError) as exc:              # OSError: the PortAudio system library is missing
        raise VoiceError(f"Voice input is not available on this machine ({exc}). "
                         "Run: pip install sounddevice faster-whisper") from exc
    try:
        info = sd.query_devices(device, "input")
        rate = int(info["default_samplerate"])         # native rate: always supported by the hardware
        audio = sd.rec(int(seconds * rate), samplerate=rate, channels=1, dtype="int16", device=device)
        sd.wait()
    except Exception as exc:
        raise VoiceError(f"Could not record from the microphone: {exc}") from exc

    peak = int(np.abs(audio).max())
    if peak < 30:
        raise VoiceError("Almost no sound was captured. Check that the microphone is not muted, that your "
                         "operating system allows microphone access, and pick the right input device.")
    if peak < 10000:                                   # quiet microphone: scale up for the speech model
        audio = (audio.astype(np.float32) * (20000 / peak)).astype(np.int16)
    return audio, rate


# --------------------------------------------------------------------------- Whisper
def _to_whisper_input(audio: np.ndarray, rate: int) -> np.ndarray:
    """int16 samples at any rate -> float32 in [-1, 1] at 16 kHz, the format Whisper expects."""
    x = audio.astype(np.float32).reshape(-1) / 32768.0
    if rate != WHISPER_RATE:
        from scipy.signal import resample_poly         # scipy comes with scikit-learn
        g = math.gcd(WHISPER_RATE, rate)
        x = resample_poly(x, WHISPER_RATE // g, rate // g).astype(np.float32)
    return x


def _get_whisper():
    """Load the model once (the first call downloads it if needed) and reuse it."""
    global _model
    with _model_lock:
        if _model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise VoiceError("Whisper is not installed. Run: pip install faster-whisper") from exc
            try:
                _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
            except Exception as exc:
                raise VoiceError(f"Could not load the Whisper model '{WHISPER_MODEL}': {exc}. The first use "
                                 "downloads it, so it needs an internet connection once.") from exc
        return _model


def _transcribe_whisper(audio: np.ndarray, rate: int, hint: str) -> str:
    model = _get_whisper()
    try:
        segments, _info = model.transcribe(
            _to_whisper_input(audio, rate), language="en", beam_size=1, temperature=0.0,
            vad_filter=True,                           # ignore silence / noise between words
            condition_on_previous_text=False,
            initial_prompt=hint or None)               # table + column names bias the spelling
        parts = [s.text.strip() for s in segments if getattr(s, "no_speech_prob", 0.0) < 0.6]
    except Exception as exc:
        raise VoiceError(f"Whisper could not decode the audio: {exc}") from exc
    return " ".join(p for p in parts if p).strip()


# ----------------------------------------------------------------------------- Vosk
def _transcribe_vosk(audio: np.ndarray, rate: int) -> str:
    try:
        import speech_recognition as sr
    except (ImportError, OSError) as exc:
        raise VoiceError(f"Vosk is not available ({exc}). Run: pip install SpeechRecognition vosk") from exc
    wav = io.BytesIO()                                 # virtual WAV file held entirely in memory
    with wave.open(wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(audio.tobytes())
    wav.seek(0)
    try:
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav) as source:
            data = recognizer.record(source)
        return recognizer.recognize_vosk(data).strip()
    except Exception as exc:
        raise VoiceError(f"Offline Vosk decoding failed: {exc}. Did you download the Vosk model "
                         "(sprc download vosk)?") from exc


# ------------------------------------------------------------------------ public API
def transcribe(audio: np.ndarray, rate: int, hint: str = "") -> tuple[str, str]:
    """Audio -> (text, engine name).  'auto' tries Whisper first and falls back to Vosk."""
    order = {"whisper": ["whisper"], "vosk": ["vosk"]}.get(ENGINE, ["whisper", "vosk"])
    problems: list[str] = []
    for engine in order:
        try:
            text = (_transcribe_whisper(audio, rate, hint) if engine == "whisper"
                    else _transcribe_vosk(audio, rate))
        except VoiceError as exc:
            problems.append(f"{engine}: {exc}")
            continue
        if re.sub(r"[^a-z ]", "", text.lower()).strip() in _NOISE_PHRASES:
            text = ""                                  # noise mis-heard as speech
        if not text:
            raise VoiceError("Nothing was recognised. Speak clearly after pressing the button and try again.")
        return text, engine
    raise VoiceError(" | ".join(problems))


def listen(device: int | None = None, seconds: int = 5, hint: str = "") -> str:
    """Record for a fixed window and return the offline transcript."""
    audio, rate = record(device, seconds)
    return transcribe(audio, rate, hint)[0]


def prewarm() -> None:
    """Pre-load Whisper in a background thread so the first request doesn't wait."""
    if ENGINE in ("auto", "whisper"):
        def _warm():
            try:
                _get_whisper()
            except Exception:
                pass
        threading.Thread(target=_warm, daemon=True).start()
