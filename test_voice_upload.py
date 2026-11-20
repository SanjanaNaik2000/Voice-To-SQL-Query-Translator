"""
test_voice_upload.py -- tests the "upload a recorded voice file" feature WITHOUT a model download.
A stand-in object plays the Whisper model; a throw-away auth database is used (your auth.db is untouched).
Run:  python test_voice_upload.py
"""
import io
import os
import tempfile
from types import SimpleNamespace

import auth_db

tmp = tempfile.mkdtemp()
auth_db.AUTH_DB_PATH = os.path.join(tmp, "auth_test.db")      # isolate from the real auth.db

import app as app_module
import voice_service as vs
from seed_demo_data import seed

failed = 0


def check(name, ok):
    global failed
    failed += not ok
    print("PASS " if ok else "FAIL ", name)


class FakeWhisper:
    def __init__(self, text="show the orders", no_speech=0.01):
        self.text, self.no_speech, self.paths = text, no_speech, []

    def transcribe(self, audio, **kw):
        self.paths.append((audio, os.path.exists(audio) if isinstance(audio, str) else None))
        return iter([SimpleNamespace(text=" " + self.text, no_speech_prob=self.no_speech)]), None


# ---- service level
vs._model = FakeWhisper()
check("transcribe_file returns the text", vs.transcribe_file("x.wav", hint="h") == "show the orders")
vs._model = FakeWhisper(text="Thank you.")
try:
    vs.transcribe_file("x.wav"); ok = False
except vs.VoiceError:
    ok = True
check("noise-only recording is rejected", ok)

# ---- through the web app
db_path = os.path.join(tmp, "v.db"); seed(db_path)
app = app_module.create_app(db_path)
ws = app.config["WORKSPACE"]
auth_db.create_user("tester", "Test@12345")
c = app.test_client()
c.post("/login", data={"username": "tester", "password": "Test@12345"})

fake = FakeWhisper(text="show the orders")
vs._model = fake
r = c.post("/upload-voice", data={"voice_file": (io.BytesIO(b"RIFFfakeaudio"), "my recording.m4a")},
           content_type="multipart/form-data")
check("upload redirects back to the page", r.status_code == 303)
check("uploaded file reached Whisper as a real file on disk", fake.paths and fake.paths[0][1] is True)
check("temporary copy is deleted afterwards", not os.path.exists(fake.paths[0][0]))
check("spoken text ran the query", ws.state.exception is None and ws.state.sql == 'SELECT * FROM "orders";')

r = c.post("/upload-voice", data={"voice_file": (io.BytesIO(b"hello"), "notes.txt")},
           content_type="multipart/form-data")
check("non-audio file is refused politely", ws.state.exception and "Unsupported audio type" in ws.state.exception["message"])

r = c.post("/upload-voice", data={}, content_type="multipart/form-data")
check("no file chosen gives a message", ws.state.exception and "choose an audio file" in ws.state.exception["message"])

vs._model = FakeWhisper(text="show the odors")
c.post("/upload-voice", data={"voice_file": (io.BytesIO(b"x"), "a.wav")}, content_type="multipart/form-data")
check("mis-heard word shows what was heard", ws.state.exception and 'Voice input heard: "show the odors"' in ws.state.exception["message"])

page = c.get("/").get_data(as_text=True)
check("upload form is on the page", 'enctype="multipart/form-data"' in page and 'name="voice_file"' in page)

anon = app.test_client().post("/upload-voice", data={}, content_type="multipart/form-data")
check("login is required", anon.status_code in (302, 401))

print("\nAll passed" if not failed else f"\n{failed} FAILED")
raise SystemExit(1 if failed else 0)
