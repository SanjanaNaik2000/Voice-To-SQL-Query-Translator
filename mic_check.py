"""
mic_check.py -- tests the microphone and the offline speech recognition (Whisper), step by step.

Run it on the computer that runs DBChat:     python mic_check.py
Say a short sentence (e.g. "show all employees") when it tells you to speak.
"""
import io
import sys
import wave

OK, BAD = "[ OK ]", "[FAIL]"


def step(n, title):
    print(f"\nStep {n}: {title}")


def main() -> int:
    # ---- 1. packages
    step(1, "required packages")
    try:
        import numpy as np
        import sounddevice as sd
    except (ImportError, OSError) as exc:
        print(f"{BAD} sounddevice / numpy not usable: {exc}\n       Fix: pip install sounddevice numpy"
              "\n       (Linux also needs the PortAudio library: sudo apt install libportaudio2)")
        return 1
    print(f"{OK} sounddevice and numpy are installed")

    # ---- 2. input devices
    step(2, "microphones found")
    try:
        devices = [(i, d) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
    except Exception as exc:
        print(f"{BAD} could not list audio devices: {exc}")
        return 1
    if not devices:
        print(f"{BAD} no input device found. Plug in or enable a microphone and check the system sound settings.")
        return 1
    try:
        default_in = sd.default.device[0]
    except Exception:
        default_in = None
    for i, d in devices:
        mark = "  <- default" if i == default_in else ""
        print(f"       [{i}] {d['name']}{mark}")
    print(f"{OK} {len(devices)} input device(s) available")

    # ---- 3. record and measure the level
    step(3, "recording 4 seconds -- SPEAK NOW (say: show all employees)")
    try:
        info = sd.query_devices(None, "input")
        rate = int(info["default_samplerate"])
        audio = sd.rec(int(4 * rate), samplerate=rate, channels=1, dtype="int16")
        sd.wait()
    except Exception as exc:
        print(f"{BAD} recording failed: {exc}\n       Check that the OS allows microphone access "
              "(Windows: Settings > Privacy > Microphone).")
        return 1
    peak = int(np.abs(audio).max())
    level = peak / 32767 * 100
    print(f"       loudest sample: {peak} of 32767  ({level:.1f}%)")
    if peak < 100:
        print(f"{BAD} almost silent. The microphone is muted, blocked by the OS, or the wrong device is the default.")
        return 1
    print(f"{OK} the microphone picks up sound" + ("  (quiet: speak closer)" if peak < 3000 else ""))

    # ---- 4. offline speech to text (the same code the Speak button uses)
    step(4, "offline speech to text (Whisper, or Vosk as fallback)")
    print("       The first run downloads the Whisper model (about 150 MB) -- please wait...")
    try:
        import voice_service
    except Exception as exc:
        print(f"{BAD} could not load voice_service.py: {exc}  (run this script from the dbchat_flask folder)")
        return 1
    if peak < 10000:                                           # same boost the app applies
        audio = (audio.astype(np.float32) * (20000 / peak)).astype(np.int16)
    try:
        text, engine = voice_service.transcribe(audio, rate, "Database commands: show all employees, orders, products.")
    except voice_service.VoiceError as exc:
        print(f"{BAD} {exc}\n       Fix: pip install faster-whisper   (needs internet once to download the model)")
        return 1
    print(f"       engine used: {engine}")
    print(f"{OK} heard: \"{text}\"")
    print("\nAll good: the 🎙 Speak button in DBChat should work on this computer.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
