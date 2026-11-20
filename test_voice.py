"""
test_voice.py -- tests the voice pipeline logic WITHOUT a microphone or a downloaded model.

A stand-in object plays the role of the Whisper model, so this checks everything around it:
resampling, the table/column "hint", engine choice, the Vosk fallback, noise handling and the
wiring into the app.  (Real accuracy can only be judged on your own microphone: use mic_check.py.)
Run:  python test_voice.py
"""
import os
import tempfile
from types import SimpleNamespace

import numpy as np

import app as app_module
import voice_service as vs
from seed_demo_data import seed

failed = 0


def check(name, ok):
    global failed
    failed += not ok
    print("PASS " if ok else "FAIL ", name)


class FakeWhisper:
    """Same interface as faster_whisper.WhisperModel.transcribe."""
    def __init__(self, text="show the orders", no_speech=0.01):
        self.text, self.no_speech, self.calls = text, no_speech, []

    def transcribe(self, audio, **kw):
        self.calls.append((audio, kw))
        return iter([SimpleNamespace(text=" " + self.text, no_speech_prob=self.no_speech)]), None


def tone(rate, seconds=1.0):
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    return (np.sin(2 * np.pi * 440 * t) * 12000).astype(np.int16).reshape(-1, 1)


# ---- 1. audio conversion
for rate in (48000, 44100, 16000):
    x = vs._to_whisper_input(tone(rate), rate)
    check(f"{rate} Hz -> 16 kHz float32 in [-1, 1]",
          x.dtype == np.float32 and abs(len(x) - 16000) <= 2 and float(np.abs(x).max()) <= 1.0)

# ---- 2. Whisper path
fake = FakeWhisper()
vs._model = fake
vs.ENGINE = "whisper"
text, engine = vs.transcribe(tone(48000), 48000, hint="Database commands: orders, employees.")
check("Whisper text is returned, trimmed", (text, engine) == ("show the orders", "whisper"))
audio_in, kw = fake.calls[0]
check("English, VAD on, no previous-text drift", kw["language"] == "en" and kw["vad_filter"] and kw["condition_on_previous_text"] is False)
check("table / column hint is passed to Whisper", kw["initial_prompt"] == "Database commands: orders, employees.")
check("model receives 16 kHz float audio", audio_in.dtype == np.float32 and abs(len(audio_in) - 16000) <= 2)

vs._model = FakeWhisper(no_speech=0.9)
try:
    vs.transcribe(tone(16000), 16000); ok = False
except vs.VoiceError as e:
    ok = "Nothing was recognised" in str(e)
check("segments that are probably not speech are dropped", ok)

vs._model = FakeWhisper(text="Thank you.")
try:
    vs.transcribe(tone(16000), 16000); ok = False
except vs.VoiceError as e:
    ok = "Nothing was recognised" in str(e)
check("noise mis-heard as 'Thank you.' is rejected", ok)

# ---- 3. engine choice and fallback
vs.ENGINE = "auto"
orig_get, orig_vosk = vs._get_whisper, vs._transcribe_vosk
vs._get_whisper = lambda: (_ for _ in ()).throw(vs.VoiceError("model not downloaded"))
vs._transcribe_vosk = lambda audio, rate: "show all orders"
check("auto falls back to Vosk if Whisper cannot load", vs.transcribe(tone(16000), 16000) == ("show all orders", "vosk"))

vs._transcribe_vosk = lambda audio, rate: (_ for _ in ()).throw(vs.VoiceError("vosk model missing"))
try:
    vs.transcribe(tone(16000), 16000); ok = False
except vs.VoiceError as e:
    ok = "whisper: model not downloaded" in str(e) and "vosk: vosk model missing" in str(e)
check("when both fail, the message explains both", ok)

vs._get_whisper, vs._transcribe_vosk = orig_get, (lambda audio, rate: "from vosk")
vs._model = FakeWhisper(text="from whisper")
vs.ENGINE = "vosk"
check("ENGINE=vosk skips Whisper", vs.transcribe(tone(16000), 16000) == ("from vosk", "vosk"))
vs.ENGINE = "whisper"
check("ENGINE=whisper skips Vosk", vs.transcribe(tone(16000), 16000) == ("from whisper", "whisper"))
vs._transcribe_vosk = orig_vosk
vs.ENGINE = "auto"

# ---- 4. recording problems are reported, not raised as crashes
try:
    vs.record(); ok = None
except vs.VoiceError as e:
    ok = "not available" in str(e) or "Could not record" in str(e) or "Almost no sound" in str(e)
check("no microphone / audio library gives a readable VoiceError", ok in (True, None))

# ---- 5. wired into the app: hint contains the database's own names
db_path = os.path.join(tempfile.mkdtemp(), "v.db"); seed(db_path)
ws = app_module.create_app(db_path).config["WORKSPACE"]
hint = ws._voice_hint()
check("hint lists the tables and columns", all(w in hint for w in ("employees", "orders", "products", "salary", "product name")))

vs._model = FakeWhisper(text="show the orders")
vs.record = lambda device=None, seconds=5: (tone(48000), 48000)        # pretend the microphone worked
ws.run_voice()
check("spoken 'show the orders' runs the query", ws.state.exception is None and ws.state.sql == 'SELECT * FROM "orders";')
check("voice prompt is saved to history", "show the orders" in [h.query for h in ws.history.get_queries(ws.connection_name)])

vs._model = FakeWhisper(text="show the odors")
ws.run_voice()
check("a mis-heard word shows what was heard", ws.state.exception and 'Voice input heard: "show the odors"' in ws.state.exception["message"])

print("\nAll passed" if not failed else f"\n{failed} FAILED")
raise SystemExit(1 if failed else 0)
