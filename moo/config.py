"""Settings and file locations for Moo Lite.

Everything Moo keeps lives in %APPDATA%\\MooLite (your user profile only):
settings.json, memory.json, reminders.json, history.json, the Outlook
sign-in cache and downloaded voice/wake-word models.
The Claude API key is NOT stored there: it goes in Windows Credential Manager.
"""
import json
import os
from pathlib import Path

APP_NAME = "MooLite"

if os.name == "nt":
    DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
else:  # lets the code be tested off Windows
    DATA_DIR = Path.home() / f".{APP_NAME.lower()}"

MODELS_DIR = DATA_DIR / "models"
SETTINGS_FILE = DATA_DIR / "settings.json"
LOG_FILE = DATA_DIR / "moo.log"

DEFAULTS = {
    # What Moo calls you, and what she's called
    "user_name": "",
    "assistant_name": "Moo",
    # Personality
    "reply_length": "normal",  # short / normal / detailed
    "personality_notes": "",  # free text, e.g. "dry humour, call me boss"
    "about_you": "",  # your job and business, so she understands your world
    "vocabulary": "",  # words she should recognise when you speak (names, trade terms)
    "home_location": "",  # postcode or town, for weather and travel times
    "email_signoff": "",
    # Extras
    "web_search": True,  # Claude's web search, about 1p per search
    "outlook_drafts": True,  # let Moo save email drafts (needs Mail.ReadWrite from IT)
    # Look
    "theme": "Ocean",
    "text_size": "Medium",
    "window_opacity": 100,
    "always_on_top": False,
    # The brain. Haiku is cheapest; switch to "claude-sonnet-5-5" for harder questions.
    "model": "claude-haiku-5-5",
    "max_tokens": 1024,
    # Voice: "natural" = Microsoft natural voices (free, needs internet);
    # "piper" = offline voice. Natural falls back to offline automatically.
    "tts_engine": "natural",
    "natural_voice": "en-GB-SoniaNeural",
    # "azure" = Azure Speech voices (free tier; more British voices). Key goes in Credential Manager.
    "azure_voice": "en-GB-SoniaNeural",
    "azure_region": "uksouth",
    # Clearer speech: slightly slower and evened-out volume
    "clear_speech": True,
    "speak_aloud": True,
    "speech_rate": 0,  # percent, -30 slower .. +30 quicker
    "speech_pitch": 0,  # Hz, -20 lower .. +20 higher
    "speech_volume": 100,  # percent
    # Listening: pause (seconds) that counts as "finished talking"
    "listen_pause": 0.7,
    # Offline voice file in the models folder (setup lets you audition them)
    "voice": "en_GB-cori-high",
    "voice_speed": 1.0,  # below 1 = faster, above 1 = slower
    # Speech recognition (Whisper). "small.en" is a good balance; "medium.en" is more accurate on a GPU.
    "whisper_model": "small.en",
    # Wake word: a custom "hey_moo.onnx" in the models folder is used if present,
    # otherwise the built-in "hey_jarvis" model.
    "wake_word_enabled": True,
    "wake_word_model": "hey_moo",
    "wake_word_fallback": "hey_jarvis",
    "wake_word_threshold": 0.5,
    # Keyboard shortcut to talk to Moo (works anywhere in Windows)
    "hotkey": "ctrl+alt+m",
    # Outlook / Microsoft 365 (filled in after your IT company registers the app)
    "ms_client_id": "",
    "ms_tenant_id": "",
    # Time zone for calendar and reminders
    "timezone": "Europe/London",
    # Morning briefing on weekdays (24h "HH:MM"); "" switches it off
    "briefing_time": "08:00",
    # Folders Moo searches if Windows Search isn't available; empty = Documents, Desktop, Downloads, OneDrive
    "search_folders": [],
    # Updates
    "auto_update": True,
    "update_repo": "HarryEco85/moo-assistant",
    # Monthly safety cap on Claude spend in US dollars; Moo stops thinking when reached
    "monthly_budget_usd": 10.0,
}


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict:
    ensure_dirs()
    settings = dict(DEFAULTS)
    if SETTINGS_FILE.exists():
        try:
            settings.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    _apply_seed(settings)
    save_settings(settings)
    return settings


def _apply_seed(settings):
    """A personal profile_seed.json next to the app (never published) fills in
    any of your details that are still blank. Used once per key."""
    seed_file = Path(__file__).resolve().parent.parent / "profile_seed.json"
    seed = read_json(seed_file, {})
    applied = set(settings.get("_seed_applied", []))
    for k, v in seed.items():
        if k not in applied and not settings.get(k):
            settings[k] = v
        applied.add(k)
    if seed:
        settings["_seed_applied"] = sorted(applied)


def user_name(settings) -> str:
    return (settings.get("user_name") or "").strip() or "there"


def save_settings(settings: dict):
    ensure_dirs()
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, data):
    ensure_dirs()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# ---- API key in Windows Credential Manager ----
KEYRING_SERVICE = "MooLite"
KEYRING_USER = "anthropic_api_key"


def get_api_key() -> str:
    env = os.environ.get("ANTHROPIC_API_KEY")
    if env:
        return env
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER) or ""
    except Exception:
        return ""


def set_api_key(key: str):
    import keyring
    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key.strip())


AZURE_USER = "azure_speech_key"


def get_azure_key() -> str:
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, AZURE_USER) or ""
    except Exception:
        return ""


def set_azure_key(key: str):
    import keyring
    if key.strip():
        keyring.set_password(KEYRING_SERVICE, AZURE_USER, key.strip())
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, AZURE_USER)
        except Exception:
            pass
