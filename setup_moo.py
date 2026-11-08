"""One-off setup: downloads Moo's voice and wake-word models, lets you pick
her voice by ear, and stores your Claude API key in Windows Credential Manager.
Run by install.bat; safe to run again any time (e.g. to change voice).
"""
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from moo.config import MODELS_DIR, ensure_dirs, get_api_key, load_settings, save_settings, set_api_key

VOICE_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB"
VOICES = [  # (file name, description) - all British female
    ("en_GB-cori-high", "Cori: clear, polished southern English (recommended)"),
    ("en_GB-jenny_dioco-medium", "Jenny: warmer, softer"),
    ("en_GB-southern_english_female-low", "Southern English female: crisper, a little more synthetic"),
]
SAMPLE = ("Good morning, {name}. You've got two meetings today and three emails that look like they "
          "need you. Shall I run through them?")


def download(url, dest):
    if dest.exists() and dest.stat().st_size > 1000:
        return
    print(f"  downloading {dest.name} ...", end="", flush=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    urllib.request.urlretrieve(url, tmp)
    tmp.replace(dest)
    print(" done")


def get_voice(name):
    speaker, quality = name[len("en_GB-"):].rsplit("-", 1)
    for ext in (".onnx", ".onnx.json"):
        download(f"{VOICE_BASE}/{speaker}/{quality}/{name}{ext}", MODELS_DIR / f"{name}{ext}")


def audition(settings):
    import numpy as np
    import sounddevice as sd
    from piper import PiperVoice
    print("\nMoo speaks with Microsoft's natural British voice (Sonia) by default; change it in Settings.")
    print("This offline voice is her backup for when there's no internet. You'll hear each one.")
    for i, (name, desc) in enumerate(VOICES, 1):
        try:
            get_voice(name)
            voice = PiperVoice.load(str(MODELS_DIR / f"{name}.onnx"))
            chunks = list(voice.synthesize(SAMPLE.format(name=settings["user_name"] or "there")))
            audio = np.concatenate([c.audio_float_array for c in chunks])
            print(f"  {i}. {desc}")
            sd.play(audio, chunks[0].sample_rate)
            sd.wait()
            time.sleep(0.4)
        except Exception as e:
            print(f"  {i}. {desc}  (couldn't play: {e})")
    choice = input("\nWhich backup voice? Type 1, 2 or 3 (Enter = 1, 'r' = replay): ").strip().lower()
    if choice == "r":
        return audition(settings)
    idx = int(choice) - 1 if choice in ("1", "2", "3") else 0
    settings["voice"] = VOICES[idx][0]
    print(f"  Moo will use: {VOICES[idx][1]}")


def main():
    ensure_dirs()
    settings = load_settings()
    print("=== Moo Lite setup ===\n")

    name = input("What should Moo call you?" + (f" (Enter = {settings['user_name']})" if settings['user_name'] else "") + ": ").strip()
    if name:
        settings["user_name"] = name

    print("\n1) Backup voice")
    try:
        audition(settings)
    except Exception as e:
        print(f"  Voice audition failed ({e}); using the default voice.")
        try:
            get_voice(settings["voice"])
        except Exception as e2:
            print(f"  Couldn't download the voice: {e2}. Moo will reply in text until this is fixed.")

    print("\n2) Wake word models")
    try:
        from openwakeword.utils import download_models
        download_models()
        print("  ready. Until you add a custom 'Hey Moo' model, the wake word is 'Hey Jarvis'.")
    except Exception as e:
        print(f"  Couldn't download wake word models ({e}). The hotkey and mic button still work.")

    print("\n3) Claude API key")
    if get_api_key():
        keep = input("  A key is already saved. Replace it? (y/N): ").strip().lower()
        if keep != "y":
            print("  Keeping the saved key.")
        else:
            new = input("  Paste your key: ").strip()
            if new.startswith("sk-ant-"):
                set_api_key(new)
            else:
                print("  That isn't a Claude key (they start sk-ant-), so I kept the old one.")
    else:
        key = input("  Paste your Claude API key (from console.anthropic.com), or Enter to do it later: ").strip()
        while key and not key.startswith("sk-ant-"):
            key = input("  That isn't a Claude key (they start sk-ant-). Paste it again, or Enter to skip: ").strip()
        if key:
            set_api_key(key)
            print("  Saved securely in Windows Credential Manager.")

    print("\n4) Outlook (optional now)")
    print("  Once your IT company gives you the two IDs, enter them here or later in Moo's settings.")
    cid = input(f"  Application (client) ID (Enter to skip){' [' + settings['ms_client_id'] + ']' if settings['ms_client_id'] else ''}: ").strip()
    if cid:
        settings["ms_client_id"] = cid
        tid = input("  Directory (tenant) ID: ").strip()
        if tid:
            settings["ms_tenant_id"] = tid

    save_settings(settings)
    print("\nAll set. Start Moo from the desktop shortcut.")
    print("The first start takes a minute or two while speech recognition downloads (about 500 MB).")


if __name__ == "__main__":
    main()
