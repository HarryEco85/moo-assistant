"""Moo's voice (Microsoft natural voices online, Piper offline as backup), hearing (Whisper, offline) and wake word
(openWakeWord, offline). Your voice never leaves the PC; only the transcribed text goes to Claude,
and Moo's replies go to Microsoft to be spoken.
"""
import logging
import os
import collections
import queue
import re
import sys
import threading
import time
from pathlib import Path

import numpy as np

from .config import DATA_DIR, MODELS_DIR, get_azure_key, get_secret, read_json, write_json

log = logging.getLogger("moo")
RATE = 16000
FRAME = 1280  # 80 ms, what openWakeWord expects


def _add_cuda_dll_dirs():
    """faster-whisper needs NVIDIA's cuBLAS/cuDNN DLLs; installed via pip into site-packages/nvidia."""
    if os.name != "nt":
        return
    for base in sys.path:
        nv = Path(base) / "nvidia"
        if nv.is_dir():
            for bin_dir in nv.glob("*/bin"):
                try:
                    os.add_dll_directory(str(bin_dir))
                    os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
                except OSError:
                    pass


def clean_for_speech(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"[*_#`>|]", "", text)
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", text)
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"[A-Za-z]:\\\S+", "the file", text)  # don't read out long paths
    text = re.sub(r"£\s?([\d,]+(?:\.\d+)?)", r"\1 pounds", text).replace("&", " and ")
    text = re.sub(r"[\U00010000-\U0010FFFF\u2600-\u27BF]", "", text)  # emoji
    for pattern, spoken in _SAY_AS:
        text = re.sub(pattern, spoken, text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


# abbreviations that text-to-speech tends to mumble or spell oddly
_SAY_AS = [
    (r"\be\.g\.", "for example"), (r"\bi\.e\.", "that is"), (r"\betc\.", "and so on"),
    (r"\bapprox\b\.?", "approximately"), (r"\bvs\b\.?", "versus"), (r"\bw/c\b", "week commencing"),
    (r"\bASAP\b", "as soon as possible"), (r"\bFYI\b", "for your information"),
    (r"\bmins\b", "minutes"), (r"\bhrs\b", "hours"), (r"\bno\.\s?(\d)", r"number \1"),
    (r"(\d)\s?-\s?(\d)", r"\1 to \2"), (r"\s/\s", " or "),
]


# Only clear, widely understood English accents are offered
ACCENTS = {"en-GB": "British", "en-IE": "Irish", "en-US": "American", "en-AU": "Australian",
           "en-CA": "Canadian", "en-NZ": "New Zealand"}
CHILD_VOICES = {"Maisie", "Ana"}  # child voices, left out
VOICES_CACHE = DATA_DIR / "voices.json"
SOURCE_LABEL = {"kokoro": "Kokoro, on this PC", "natural": "Microsoft", "azure": "Azure", "elevenlabs": "ElevenLabs"}

# Kokoro: free, runs on this PC (downloaded once, about 120 MB)
KOKORO_FILES = {
    "kokoro-v1.0.int8.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}
KOKORO_VOICES = [  # (id, name, accent, gender), best-sounding first within each group
    ("bf_emma", "Emma", "British", "female"), ("bf_isabella", "Isabella", "British", "female"),
    ("bf_alice", "Alice", "British", "female"), ("bf_lily", "Lily", "British", "female"),
    ("bm_george", "George", "British", "male"), ("bm_fable", "Fable", "British", "male"),
    ("bm_lewis", "Lewis", "British", "male"), ("bm_daniel", "Daniel", "British", "male"),
    ("af_heart", "Heart", "American", "female"), ("af_bella", "Bella", "American", "female"),
    ("af_nicole", "Nicole", "American", "female"), ("af_sarah", "Sarah", "American", "female"),
    ("af_aoede", "Aoede", "American", "female"), ("af_kore", "Kore", "American", "female"),
    ("am_michael", "Michael", "American", "male"), ("am_fenrir", "Fenrir", "American", "male"),
    ("am_puck", "Puck", "American", "male"), ("am_eric", "Eric", "American", "male"),
]

_FALLBACK_FREE = [("en-GB-SoniaNeural", "Female"), ("en-GB-LibbyNeural", "Female"), ("en-GB-RyanNeural", "Male"),
                  ("en-GB-ThomasNeural", "Male"), ("en-IE-EmilyNeural", "Female"), ("en-IE-ConnorNeural", "Male"),
                  ("en-AU-NatashaNeural", "Female"), ("en-AU-WilliamNeural", "Male"), ("en-US-AriaNeural", "Female"),
                  ("en-US-GuyNeural", "Male"), ("en-US-JennyNeural", "Female"), ("en-US-AndrewNeural", "Male")]


def _voice(engine, vid, name, accent, gender):
    label = f"{name} · {accent} {gender} · {SOURCE_LABEL[engine]}"
    return {"engine": engine, "id": vid, "label": label, "accent": accent, "gender": gender, "name": name}


def _ms_voice(engine, short_name, gender):
    locale = short_name[:5]
    if locale not in ACCENTS:
        return None
    name = short_name[6:].replace("Neural", "")
    if name.endswith("Multilingual"):
        name = name[: -len("Multilingual")]
    if name in CHILD_VOICES:
        return None
    return _voice(engine, short_name, name, ACCENTS[locale], gender.lower())


def list_voices(settings) -> list:
    """Clean-English male and female voices from every source available:
    Kokoro (free, local), Microsoft (free), plus Azure and ElevenLabs if keys are set."""
    out = [_voice("kokoro", vid, n, a, g) for vid, n, a, g in KOKORO_VOICES]
    key = get_azure_key()
    if key:
        try:
            import requests
            region = settings.get("azure_region", "uksouth").strip() or "uksouth"
            r = requests.get(f"https://{region}.tts.speech.microsoft.com/cognitiveservices/voices/list",
                             headers={"Ocp-Apim-Subscription-Key": key}, timeout=15)
            r.raise_for_status()
            out += [v for v in (_ms_voice("azure", x["ShortName"], x.get("Gender", "")) for x in r.json()) if v]
        except Exception as e:
            log.warning("Couldn't list Azure voices: %s", e)
    free = []
    try:
        import asyncio
        import edge_tts
        free = [(v["ShortName"], v.get("Gender", "")) for v in asyncio.run(edge_tts.list_voices())]
    except Exception as e:
        log.warning("Couldn't list free voices: %s", e)
        cached = read_json(VOICES_CACHE, [])
        free = [(c["id"], c["gender"]) for c in cached if c.get("engine") == "natural"] or _FALLBACK_FREE
    out += [v for v in (_ms_voice("natural", n, g) for n, g in free) if v]
    el_key = get_secret("elevenlabs_key")
    if el_key:
        out += _elevenlabs_voices(el_key)
    try:
        write_json(VOICES_CACHE, [v for v in out if v["engine"] == "natural"])
    except Exception:
        pass
    accent_order = ["British", "Irish", "American", "Australian", "Canadian", "New Zealand"]
    engine_order = ["elevenlabs", "kokoro", "azure", "natural"]
    out.sort(key=lambda v: (engine_order.index(v["engine"]), accent_order.index(v["accent"])
                            if v["accent"] in accent_order else 9, v["gender"] != "female"))
    out.append({"engine": "piper", "id": "", "label": "Offline backup voice (robotic)", "accent": "British",
                "gender": "female", "name": "Offline"})
    return out


def _elevenlabs_voices(key) -> list:
    import requests
    try:
        r = requests.get("https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": key}, timeout=15)
        r.raise_for_status()
    except Exception as e:
        log.warning("Couldn't list ElevenLabs voices: %s", e)
        return []
    out = []
    names = {"british": "British", "english": "British", "irish": "Irish", "american": "American",
             "australian": "Australian", "canadian": "Canadian"}
    for v in r.json().get("voices", []):
        labels = {k: str(val).lower() for k, val in (v.get("labels") or {}).items()}
        accent = next((names[k] for k in names if k in labels.get("accent", "")), None)
        if not accent:
            continue
        gender = labels.get("gender", "")
        gender = "female" if "female" in gender else ("male" if "male" in gender else "voice")
        out.append(_voice("elevenlabs", v["voice_id"], v.get("name", "Voice"), accent, gender))
    return out


_kokoro = None
_kokoro_lock = threading.Lock()


def _kokoro_model():
    """Load Kokoro, downloading it the first time (about 120 MB)."""
    global _kokoro
    with _kokoro_lock:
        if _kokoro is None:
            import urllib.request
            folder = MODELS_DIR / "kokoro"
            folder.mkdir(parents=True, exist_ok=True)
            for name, url in KOKORO_FILES.items():
                dest = folder / name
                if not dest.exists() or dest.stat().st_size < 1_000_000:
                    log.info("Downloading %s", name)
                    tmp = dest.with_suffix(dest.suffix + ".part")
                    urllib.request.urlretrieve(url, tmp)
                    tmp.replace(dest)
            from kokoro_onnx import Kokoro
            _kokoro = Kokoro(str(folder / "kokoro-v1.0.int8.onnx"), str(folder / "voices-v1.0.bin"))
        return _kokoro


def _kokoro_synth(text, voice, rate_pct):
    speed = max(0.6, min(1.0 + rate_pct / 100, 1.6))
    samples, rate = _kokoro_model().create(text, voice=voice, speed=speed,
                                           lang="en-gb" if voice.startswith("b") else "en-us")
    return np.asarray(samples, dtype=np.float32), rate


def _elevenlabs_synth(text, voice_id, key, rate_pct):
    import requests
    import miniaudio
    speed = max(0.7, min(1.0 + rate_pct / 100, 1.2))
    r = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                      params={"output_format": "mp3_44100_128"},
                      headers={"xi-api-key": key, "Content-Type": "application/json"},
                      json={"text": text, "model_id": "eleven_flash_v2_5", "voice_settings": {"speed": speed}},
                      timeout=30)
    r.raise_for_status()
    dec = miniaudio.decode(r.content, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=44100)
    return np.frombuffer(dec.samples, dtype=np.int16).astype(np.float32) / 32768.0, 44100


def _trim(audio, rate, gap=0.2):
    """Cut the dead air text-to-speech adds before and after each phrase, then
    leave one short, natural gap so phrases flow into each other."""
    if not len(audio):
        return audio
    level = np.abs(audio)
    thresh = max(0.008, float(level.max()) * 0.02)
    loud = np.nonzero(level > thresh)[0]
    if not len(loud):
        return audio
    pad = int(rate * 0.03)
    a, b = max(0, loud[0] - pad), min(len(audio), loud[-1] + pad)
    return np.concatenate([audio[a:b], np.zeros(int(rate * gap), dtype=np.float32)])


def _phrases(sentences, min_len=110, first_len=40):
    """Join short sentences so they're spoken in one breath, with proper
    intonation, instead of as separate clips. The first phrase only needs to
    be short-ish, so she starts talking as soon as possible."""
    buf, need = "", first_len
    for s in sentences:
        buf = f"{buf} {s}".strip() if buf else s
        if len(buf) >= need:
            yield buf
            buf, need = "", min_len
    if buf:
        yield buf


def _normalise(audio):
    """Even, clear volume: lift quiet voices and trim peaks to the same level."""
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if peak > 1e-4:
        audio = audio * (0.89 / peak)
    return audio.astype(np.float32)


def _edge_synth(text, voice, rate_pct, pitch=0):
    """Free Microsoft natural voice -> (float32 mono audio, sample rate)."""
    import asyncio
    import edge_tts
    import miniaudio

    async def fetch():
        mp3 = bytearray()
        comm = edge_tts.Communicate(text, voice, rate=f"{rate_pct:+d}%", pitch=f"{pitch:+d}Hz")
        async for chunk in comm.stream():
            if chunk.get("type") == "audio":
                mp3.extend(chunk["data"])
        return bytes(mp3)

    mp3 = asyncio.run(fetch())
    if not mp3:
        raise RuntimeError("no audio returned")
    dec = miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=24000)
    audio = np.frombuffer(dec.samples, dtype=np.int16).astype(np.float32) / 32768.0
    return audio, 24000


def _azure_synth(text, voice, rate_pct, key, region, pitch=0):
    """Azure Speech (free tier available): studio-quality 48 kHz audio."""
    import io
    import wave
    from xml.sax.saxutils import escape

    import requests
    ssml = (f"<speak version='1.0' xml:lang='en-GB' xmlns='http://www.w3.org/2001/10/synthesis'>"
            f"<voice name='{voice}'><prosody rate='{rate_pct:+d}%' pitch='{pitch:+d}Hz'>{escape(text)}</prosody>"
            f"</voice></speak>")
    r = requests.post(f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1",
                      headers={"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/ssml+xml",
                               "X-Microsoft-OutputFormat": "riff-48khz-16bit-mono-pcm", "User-Agent": "MooLite"},
                      data=ssml.encode("utf-8"), timeout=20)
    r.raise_for_status()
    with wave.open(io.BytesIO(r.content)) as w:
        rate = w.getframerate()
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    return audio, rate


class Mouth:
    """Speaks with a Microsoft natural voice, falling back to the offline Piper
    voice if there's no internet. Sentences are prepared one ahead of playback
    so long answers flow without gaps, and Moo can be interrupted at any point."""

    def __init__(self, settings, piper=None, load=True):
        self.settings = settings
        self.piper = piper
        self._stop = threading.Event()
        self.speaking = False
        self.level = 0.0  # current loudness 0..1, drives the orb animation
        self.last_engine, self.last_error = "", ""  # which voice actually spoke, and why not the chosen one
        if load:
            self._load_piper()

    @property
    def voice(self):  # "can Moo speak at all?"
        return True if self.settings.get("tts_engine", "natural") != "piper" else self.piper

    def _load_piper(self):
        name = self.settings.get("voice", "en_GB-cori-high")
        model = MODELS_DIR / f"{name}.onnx"
        if not model.exists():
            log.warning("Offline voice file %s missing.", model)
            return
        try:
            from piper import PiperVoice
            self.piper = PiperVoice.load(str(model))
        except Exception as e:
            log.exception("Couldn't load offline voice: %s", e)

    def _name_flow(self, text):
        """No comma before the user's name: "Morning, Harry." pauses awkwardly,
        "Morning Harry." flows."""
        name = (self.settings.get("user_name") or "").strip()
        if name:
            text = re.sub(rf",\s+({re.escape(name)})\b", r" \1", text, flags=re.I)
        return text

    def _num(self, key, default):
        try:
            return float(self.settings.get(key, default))
        except (TypeError, ValueError):
            return default

    def _pitch(self) -> int:
        return int(max(-30, min(self._num("speech_pitch", 0), 30)))

    def _speed_pct(self) -> int:
        """speech_rate: percent, negative = slower, positive = quicker."""
        pct = int(self._num("speech_rate", 0))
        return max(-50, min(pct, 100))

    def synth(self, sentence):
        audio, rate = self._synth_raw(self._name_flow(sentence))
        if audio is not None:
            audio = _trim(audio, rate)
            if self.settings.get("clear_speech", True):
                audio = _normalise(audio)
            vol = max(0.2, min(self._num("speech_volume", 100) / 100, 1.6))
            audio = np.clip(audio * vol, -1, 1).astype(np.float32)
        return audio, rate

    def _synth_raw(self, sentence):
        engine = self.settings.get("tts_engine", "natural")
        if engine == "elevenlabs":
            try:
                key = get_secret("elevenlabs_key")
                if not key:
                    raise RuntimeError("no ElevenLabs key set")
                out = _elevenlabs_synth(sentence, self.settings.get("elevenlabs_voice", ""), key, self._speed_pct())
                self.last_engine, self.last_error = "elevenlabs", ""
                return out
            except Exception as e:
                log.warning("ElevenLabs voice unavailable (%s); trying the free voice.", e)
                self.last_error = f"ElevenLabs: {str(e)[:120]}"
                engine = "natural"
        if engine == "kokoro":
            try:
                out = _kokoro_synth(sentence, self.settings.get("kokoro_voice", "bf_emma"), self._speed_pct())
                self.last_engine, self.last_error = "kokoro", ""
                return out
            except Exception as e:
                log.warning("Kokoro voice unavailable (%s); trying the free voice.", e)
                self.last_error = f"Kokoro: {str(e)[:120]}"
                engine = "natural"
        if engine == "azure":
            key = get_azure_key()
            try:
                if not key:
                    raise RuntimeError("no Azure key set")
                out = _azure_synth(sentence, self.settings.get("azure_voice", "en-GB-SoniaNeural"), self._speed_pct(),
                                   key, self.settings.get("azure_region", "uksouth").strip() or "uksouth", self._pitch())
                self.last_engine, self.last_error = "azure", ""
                return out
            except Exception as e:
                log.warning("Azure voice unavailable (%s); trying the free voice.", e)
                self.last_error = f"Azure: {str(e)[:120]}"
                engine = "natural"
        if engine == "natural":
            try:
                out = _edge_synth(sentence, self.settings.get("natural_voice", "en-GB-SoniaNeural"), self._speed_pct(),
                                  self._pitch())
                self.last_engine = "natural"
                return out
            except Exception as e:
                log.warning("Natural voice unavailable (%s); using offline voice.", e)
                self.last_error = (self.last_error + "; " if getattr(self, "last_error", "") else "") + \
                    f"Microsoft voices: {str(e)[:120]}"
        self.last_engine = "piper"
        if self.piper is None:
            return None, 0
        from piper import SynthesisConfig
        cfg = SynthesisConfig(length_scale=1.0 / max(0.5, 1 + self._speed_pct() / 100))
        chunks = list(self.piper.synthesize(sentence, syn_config=cfg))
        if not chunks:
            return None, 0
        return np.concatenate([c.audio_float_array for c in chunks]).astype(np.float32), chunks[0].sample_rate

    def stop(self):
        self._stop.set()
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass

    def say(self, text: str, on_start=None, on_end=None, force=False):
        sentences = [x for x in re.split(r"(?<=[.!?])\s+", clean_for_speech(text)) if x.strip()]
        self.speak_iter(iter(sentences), on_start, on_end, force)

    def speak_iter(self, sentences, on_start=None, on_end=None, force=False):
        """Speak sentences as they arrive (from any iterator, e.g. a live reply).
        The next sentence is prepared while the current one plays, so there are
        no gaps, and Moo can be interrupted at any point."""
        if not force and not self.settings.get("speak_aloud", True):
            for _ in sentences:  # drain so the sender never blocks
                pass
            return
        import sounddevice as sd
        stop = self._stop = threading.Event()  # this reply's own stop switch
        ready = queue.Queue(maxsize=3)

        def producer():
            for sentence in _phrases(sentences):
                if stop.is_set():
                    continue  # keep draining the iterator
                sentence = clean_for_speech(sentence)
                if not sentence:
                    continue
                try:
                    ready.put(self.synth(sentence))
                except Exception as e:
                    log.exception("Speech synthesis failed: %s", e)
            ready.put(None)

        threading.Thread(target=producer, daemon=True, name="tts").start()
        started = False
        try:
            while True:
                item = ready.get()
                if item is None:
                    break
                if stop.is_set():
                    continue
                audio, rate = item
                if audio is None or not len(audio):
                    continue
                if not started:
                    started = True
                    self.speaking = True
                    if on_start:
                        on_start()
                # loudness envelope in 40 ms steps for the animation
                step = max(1, int(rate * 0.04))
                n = len(audio) // step
                env = np.sqrt(np.mean(audio[: n * step].reshape(-1, step) ** 2, axis=1)) if n else np.zeros(1)
                env = np.clip(env / (env.max() + 1e-6), 0, 1)
                sd.play(audio, rate)
                t0 = time.time()
                dur = len(audio) / rate
                while not stop.is_set():
                    el = time.time() - t0
                    if el >= dur + 0.02:
                        break
                    self.level = float(env[min(int(el / 0.04), len(env) - 1)])
                    time.sleep(0.02)
                sd.stop()
        except Exception as e:
            log.exception("Speech failed: %s", e)
        finally:
            self.level = 0.0
            self.speaking = False
            if started and on_end:
                on_end()


_WAKE = re.compile(r"^\W*(hey|hay|hello|hallo|hullo|helo|hiya|hi|high)\W+"
                   r"(moo+|mooh|mu+|moe|mo|moon|mew|more|moore|mood|mooed|mou|mohr)\b\W*(.*)$", re.I)
_GREET = re.compile(r"^\W*(hey|hay|hello|hallo|hullo|helo|hiya|hi|high)\b", re.I)


def match_wake(text, name="Moo"):
    """'Hey Moo' (or her name) at the start of a phrase. Returns what came after it ('' if nothing),
    or None if it wasn't the wake phrase. Without 'hey', only a clear 'Moo' counts."""
    t = (text or "").strip()
    if name.lower() != "moo":
        m = re.match(rf"^\W*(hey|hi|hello|hiya)\W+({re.escape(name)})\b\W*(.*)$", t, re.I)
    else:
        m = _WAKE.match(t)
    if not m or not m.group(1):
        return None  # only "Hello Moo" / "Hey Moo": a bare name or "a mo" in passing doesn't count
    return m.group(3).strip()


def cloud_transcribe(pcm, settings, prompt=None) -> str:
    """Optional: Groq's hosted Whisper (large-v3-turbo). Much faster and more accurate than a laptop
    processor, free tier is plenty for one person. Returns None if not set up or it fails."""
    from .config import get_secret
    key = get_secret("groq_key")
    if not key or not settings.get("cloud_hearing", True) or pcm is None or len(pcm) < 1600:
        return None
    try:
        import io
        import wave
        import requests
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes((np.clip(pcm, -1, 1) * 32767).astype(np.int16).tobytes())
        data = {"model": "whisper-large-v3-turbo", "language": "en", "temperature": "0", "response_format": "json"}
        vocab = prompt or (settings.get("vocabulary") or "").strip()
        if vocab:
            data["prompt"] = vocab[:600]  # the prompt is capped at 224 tokens
        r = requests.post("https://api.groq.com/openai/v1/audio/transcriptions", timeout=12,
                          headers={"Authorization": f"Bearer {key}"},
                          files={"file": ("speech.wav", buf.getvalue(), "audio/wav")}, data=data)
        if r.status_code != 200:
            log.warning("Cloud hearing failed (%s): %s", r.status_code, r.text[:160])
            return None
        return (r.json().get("text") or "").strip()
    except Exception as e:
        log.warning("Cloud hearing failed: %s", e)
        return None


class Ears:
    """One microphone stream. Normally listens for the wake word; when asked,
    records one utterance and transcribes it."""

    def __init__(self, settings, on_wake, on_barge=None, is_speaking=None):
        self.settings = settings
        self.on_wake = on_wake
        self.on_barge = on_barge  # you started talking over her
        self.is_speaking = is_speaking or (lambda: False)
        self._preroll = collections.deque(maxlen=10)  # last ~0.8 s, so your first words aren't lost
        self.tiny = None  # small speech model that listens for "Hey Moo"
        self._spot = {"seg": [], "on": False, "quiet": 0, "noise": None, "skip": False}
        self._spot_q = queue.Queue(maxsize=8)
        self._wlock = threading.Lock()  # one transcription at a time on the main model
        self.notes_lines = []  # (time, text) while taking notes
        self._notes_q = queue.Queue()
        self._notes_seg = {"seg": [], "on": False, "quiet": 0, "noise": None}
        self.notes_started = None
        self._talk = None  # barge-in tracking while she speaks
        self.frames = queue.Queue(maxsize=400)
        self._record_q = queue.Queue()
        self.mode = "wake"  # "wake" or "record" or "off"
        self.whisper = None
        self.wake_model = None
        self.wake_name = None
        self.stream = None
        self.available = False
        self.models_ready = False
        self.wake_error = ""
        self.whisper_device = ""
        self.last_cloud = None  # None until she's heard you once
        self.last_transcribe_s = None
        self._last_wake = 0.0

    # ----- setup -----
    def start(self):
        try:
            import sounddevice as sd
            self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME,
                                         callback=self._callback)
            self.stream.start()
            self.available = True
        except Exception as e:
            log.exception("No microphone available: %s", e)
            return
        threading.Thread(target=self._load_models, daemon=True, name="ears-load").start()
        threading.Thread(target=self._wake_loop, daemon=True, name="ears-wake").start()

    def _load_models(self):
        try:
            self._load_wake_word()  # quick, so do it first
            self._load_tiny()
            self._load_whisper()
        finally:
            self.models_ready = True

    def _load_wake_word(self):
        try:
            from openwakeword.model import Model
            custom = MODELS_DIR / f"{self.settings.get('wake_word_model', 'hey_moo')}.onnx"
            if custom.exists():
                self.wake_model = Model(wakeword_models=[str(custom)], inference_framework="onnx")
                self.wake_name = custom.stem
            else:
                fallback = self.settings.get("wake_word_fallback", "hey_jarvis")
                try:
                    self.wake_model = Model(wakeword_models=[fallback], inference_framework="onnx")
                except Exception:
                    from openwakeword.utils import download_models  # models missing: fetch and retry
                    download_models([fallback])
                    self.wake_model = Model(wakeword_models=[fallback], inference_framework="onnx")
                self.wake_name = fallback
            log.info("Wake word ready: %s", self.wake_name)
        except Exception as e:
            log.exception("Wake word unavailable: %s", e)
            self.wake_error = str(e)

    def _load_tiny(self):
        """The "Hey Moo" listener: a tiny speech model that only hears short phrases."""
        if self.settings.get("wake_engine", "moo") != "moo" or self.tiny is not None:
            return
        try:
            from faster_whisper import WhisperModel
            self.tiny = WhisperModel("tiny.en", device="cpu", compute_type="int8", cpu_threads=2,
                                     download_root=str(MODELS_DIR / "whisper"))
            threading.Thread(target=self._spot_worker, daemon=True, name="hey-moo").start()
            log.info("Hey Moo listener ready")
        except Exception as e:
            log.warning("Hey Moo listener unavailable (%s); using %s", e, self.wake_name)

    def _load_whisper(self):
        # GPU if available, otherwise CPU
        _add_cuda_dll_dirs()
        from faster_whisper import WhisperModel
        size = self.settings.get("whisper_model", "small.en")
        for device, compute in (("cuda", "float16"), ("cpu", "int8")):
            if device == "cpu" and size == "small.en" and not self.settings.get("whisper_model_chosen"):
                size = "base.en"  # no graphics card: the lighter model is about 3x quicker on the processor
            try:
                model = WhisperModel(size, device=device, compute_type=compute,
                                     download_root=str(MODELS_DIR / "whisper"))
                self.whisper = model
                self.whisper_device = "GPU" if device == "cuda" else "CPU"
                log.info("Whisper %s loaded on %s", size, device)
                break
            except Exception as e:
                log.warning("Whisper on %s failed: %s", device, e)

    @property
    def wake_phrase(self) -> str:
        if self.tiny is not None and self.settings.get("wake_engine", "moo") == "moo":
            name = self.settings.get("assistant_name", "Moo") or "Moo"
            return f"Hey {name}"
        return (self.wake_name or "").replace("_", " ").title()

    def _callback(self, indata, frames, time_info, status):
        try:
            self.frames.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass

    # ----- wake word -----
    def _barge(self, frame) -> bool:
        """While she's talking: did you start speaking over her?
        Learns how loud her own voice is through the mic, then waits for
        something clearly louder that lasts a moment (your voice, not hers)."""
        if not self.settings.get("barge_in", True) or self.mode != "wake":
            self._talk = None
            return False
        if not self.is_speaking():
            self._talk = None
            return False
        rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
        t = self._talk
        if t is None:
            t = self._talk = {"start": time.time(), "hist": collections.deque(maxlen=40), "hits": 0}
        loud = False
        if time.time() - t["start"] > 0.8 and len(t["hist"]) >= 8:
            echo = float(np.percentile(list(t["hist"]), 80))
            sens = {"low": 4.0, "medium": 2.6, "high": 1.6}.get(self.settings.get("barge_sensitivity", "medium"), 2.6)
            loud = rms > max(echo * sens, 700.0)
        if loud:
            t["hits"] += 1
        else:
            t["hits"] = 0
            t["hist"].append(rms)
        if t["hits"] >= 3:  # about a quarter of a second of you
            self._talk = None
            return True
        return False

    def _wake_loop(self):
        while True:
            frame = self.frames.get()
            self._preroll.append(frame)
            if self.on_barge and self._barge(frame):
                pre = list(self._preroll)
                self._preroll.clear()
                self.on_barge(pre)
                continue
            if self.mode == "record":
                self._record_q.put(frame)
                continue
            if self.mode == "notes" and not self.is_speaking():
                self._notes_frame(frame)
            if not self.settings.get("wake_word_enabled", True) or self.mode not in ("wake", "notes"):
                continue
            if self.tiny is not None and self.settings.get("wake_engine", "moo") == "moo":
                if not self.is_speaking():
                    self._spot_frame(frame)
                continue
            if self.wake_model is None:
                continue
            threshold = float(self.settings.get("wake_word_threshold", 0.5))
            scores = self.wake_model.predict(frame)
            if max(scores.values(), default=0) >= threshold and time.time() - self._last_wake > 2:
                self._last_wake = time.time()
                self.wake_model.reset()
                self.on_wake()

    # ----- "Hey Moo" -----
    @staticmethod
    def _segment(st, frame, end_quiet, max_frames, min_level=350.0):
        """Split the mic into phrases by loudness. Returns a finished phrase (list of frames) or None."""
        rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
        if st["noise"] is None:
            st["noise"] = min(rms, 300.0)
        if not st["on"]:  # background level: drops quickly, rises slowly (so talking doesn't become "background")
            st["noise"] = 0.9 * st["noise"] + 0.1 * rms if rms < st["noise"] else 0.995 * st["noise"] + 0.005 * rms
        level = max(st["noise"] * 3.0, min_level)
        if rms > level:
            if not st["on"]:
                st["on"], st["seg"] = True, list(st.get("pre", []))
            st["quiet"] = 0
            st["seg"].append(frame)
        elif st["on"]:
            st["seg"].append(frame)
            st["quiet"] += 1
            if st["quiet"] >= end_quiet:
                seg, st["seg"], st["on"], st["quiet"] = st["seg"], [], False, 0
                return seg
        else:
            st.setdefault("pre", collections.deque(maxlen=3)).append(frame)
        if st["on"] and len(st["seg"]) >= max_frames:
            seg, st["seg"], st["on"], st["quiet"] = st["seg"], [], False, 0
            return seg
        return None

    def _spot_frame(self, frame):
        seg = self._segment(self._spot, frame, end_quiet=6, max_frames=66)  # pause ~0.5 s, phrase up to ~5 s
        if seg is None:
            return
        dur = len(seg) * FRAME / RATE
        if 0.35 <= dur <= 5.0:
            try:
                self._spot_q.put_nowait(np.concatenate(seg))
            except queue.Full:
                pass

    def _spot_worker(self):
        while True:
            audio = self._spot_q.get()
            try:
                pcm = audio.astype(np.float32) / 32768.0
                segs, _ = self.tiny.transcribe(pcm, language="en", beam_size=1, without_timestamps=True,
                                               initial_prompt="Hello Moo.", condition_on_previous_text=False)
                text = " ".join(s.text.strip() for s in segs).strip()
            except Exception as e:
                log.warning("Hey Moo check failed: %s", e)
                continue
            name = self.settings.get("assistant_name", "Moo") or "Moo"
            hit = match_wake(text, name)
            if hit is None or time.time() - self._last_wake < 2 or self.mode not in ("wake", "notes"):
                continue
            # double-check with the better ears before waking (cuts out false alarms from office chatter)
            sure = cloud_transcribe(pcm, self.settings, prompt="Hello Moo. Hey Moo.")
            if sure is not None:
                checked = match_wake(sure, name)
                if checked is not None:
                    hit = checked
                elif not _GREET.match(sure):  # the better ears didn't even hear a hello: chatter
                    log.info("Wake phrase not confirmed (%r vs %r)", text[:60], sure[:60])
                    continue
            log.info("Wake check: tiny %r, cloud %r", text[:60], (sure or "")[:60])
            self._last_wake = time.time()
            log.info("Heard the wake phrase: %r", text[:80])
            self.on_wake(hit or None)

    # ----- meeting notes -----
    def start_notes(self):
        self.notes_lines, self.notes_started = [], time.time()
        self._notes_seg = {"seg": [], "on": False, "quiet": 0, "noise": None}
        self.mode = "notes"
        threading.Thread(target=self._notes_worker, daemon=True, name="notes").start()

    def stop_notes(self) -> list:
        """Stop taking notes; waits for the last bit to be written up. Returns [(seconds in, text)]."""
        if self.mode == "notes":
            self.mode = "wake"
        seg = self._notes_seg.get("seg")
        if seg:
            self._notes_q.put((time.time(), np.concatenate(seg)))
        self._notes_q.put(None)
        self._notes_q.join()
        self.notes_started, lines = None, self.notes_lines
        return lines

    def _notes_frame(self, frame):
        seg = self._segment(self._notes_seg, frame, end_quiet=13, max_frames=320, min_level=300.0)
        if seg is not None and len(seg) * FRAME / RATE >= 0.6:  # a pause of ~1 s, or 25 s of talking
            self._notes_q.put((time.time(), np.concatenate(seg)))

    def _notes_worker(self):
        while True:
            item = self._notes_q.get()
            try:
                if item is None:
                    return
                ts, audio = item
                pcm = audio.astype(np.float32) / 32768.0
                with self._wlock:
                    segs, _ = self.whisper.transcribe(
                        pcm, language="en", beam_size=int(self.settings.get("whisper_beam", 3) or 1),
                        vad_filter=True, without_timestamps=True, condition_on_previous_text=False,
                        initial_prompt=(self.settings.get("vocabulary") or "").strip() or None)
                    text = " ".join(s.text.strip() for s in segs).strip()
                if text and not match_wake(text, self.settings.get("assistant_name", "Moo") or "Moo"):
                    self.notes_lines.append((ts - (self.notes_started or ts), text))
            except Exception as e:
                log.warning("Notes transcription failed: %s", e)
            finally:
                self._notes_q.task_done()

    # ----- recording -----
    def reload_whisper(self):
        threading.Thread(target=self._load_whisper, daemon=True, name="whisper-reload").start()

    def listen(self, max_seconds=45, start_timeout=7, silence=None, preroll=None) -> str:
        """Record one utterance (stops when you pause) and return the text."""
        if not self.available:
            raise RuntimeError("No microphone found.")
        if self.whisper is None:
            raise RuntimeError("Speech recognition is still loading, give it a moment.")
        if silence is None:
            try:
                silence = max(0.4, min(float(self.settings.get("listen_pause", 1.2)), 4.0))
            except (TypeError, ValueError):
                silence = 1.2
        self._record_q = queue.Queue()
        # drain stale audio
        while not self.frames.empty():
            try:
                self.frames.get_nowait()
            except queue.Empty:
                break
        back_to = "notes" if self.mode == "notes" else "wake"
        self.mode = "record"
        audio, started = list(preroll or []), bool(preroll)
        speech_at = time.time()
        t0 = time.time()
        noise = None
        try:
            while True:
                try:
                    frame = self._record_q.get(timeout=1)
                except queue.Empty:
                    continue
                rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
                if noise is None:
                    noise = rms
                noise = 0.95 * noise + 0.05 * rms if not started else noise
                level = max(noise * 3.0, 350.0)
                audio.append(frame)
                now = time.time()
                if rms > level:
                    started = True
                    speech_at = now
                if not started and now - t0 > start_timeout:
                    return ""
                if started and now - speech_at > silence:
                    break
                if now - t0 > max_seconds:
                    break
        finally:
            self.mode = back_to
        pcm = np.concatenate(audio).astype(np.float32) / 32768.0
        t0 = time.time()

        def transcribe():
            with self._wlock:
                return _transcribe()

        def _transcribe():
            segments, _ = self.whisper.transcribe(
                pcm, language="en", beam_size=int(self.settings.get("whisper_beam", 3) or 1), vad_filter=True,
                without_timestamps=True, condition_on_previous_text=False,
                initial_prompt=(self.settings.get("vocabulary") or "").strip() or None)
            return " ".join(seg.text.strip() for seg in segments).strip()

        t0c = time.time()
        text = cloud_transcribe(pcm, self.settings)
        if text is not None:
            self.last_cloud = True
            self.last_transcribe_s = time.time() - t0c
            log.info("Heard in the cloud in %.1fs", self.last_transcribe_s)
            if text.lower().strip(" .!") in ("thank you", "thanks for watching", "you", "bye"):
                return ""  # what speech models "hear" in near-silence
            return text
        self.last_cloud = False
        try:
            text = transcribe()
        except Exception as e:
            if self.whisper_device != "GPU":
                raise
            # graphics card libraries missing or broken: switch to the CPU for good
            log.warning("GPU transcription failed (%s); switching to CPU.", e)
            from faster_whisper import WhisperModel
            self.whisper = WhisperModel(self.settings.get("whisper_model", "small.en"), device="cpu",
                                        compute_type="int8", download_root=str(MODELS_DIR / "whisper"))
            self.whisper_device = "CPU"
            text = transcribe()
        self.last_transcribe_s = time.time() - t0
        return text
