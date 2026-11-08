"""Moo's voice (Microsoft natural voices online, Piper offline as backup), hearing (Whisper, offline) and wake word
(openWakeWord, offline). Your voice never leaves the PC; only the transcribed text goes to Claude,
and Moo's replies go to Microsoft to be spoken.
"""
import logging
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path

import numpy as np

from .config import DATA_DIR, MODELS_DIR, get_azure_key, read_json, write_json

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


ACCENTS = {
    "en-GB": "British", "en-IE": "Irish", "en-AU": "Australian", "en-NZ": "New Zealand", "en-ZA": "South African",
    "en-CA": "Canadian", "en-US": "American", "en-IN": "Indian", "en-SG": "Singaporean", "en-HK": "Hong Kong",
    "en-KE": "Kenyan", "en-NG": "Nigerian", "en-PH": "Filipino", "en-TZ": "Tanzanian",
}
CHILD_VOICES = {"Maisie", "Ana"}  # child voices, left out of the list
VOICES_CACHE = DATA_DIR / "voices.json"

# Used if Moo can't fetch Microsoft's live list (e.g. offline at the time)
_FALLBACK_FREE = ["en-GB-SoniaNeural", "en-GB-LibbyNeural", "en-IE-EmilyNeural", "en-AU-NatashaNeural",
                  "en-NZ-MollyNeural", "en-ZA-LeahNeural", "en-CA-ClaraNeural", "en-US-AvaMultilingualNeural",
                  "en-US-EmmaMultilingualNeural", "en-US-AriaNeural", "en-US-JennyNeural"]


def _voice_entry(engine, short_name):
    locale = short_name[:5]
    name = short_name[6:].replace("Neural", "")
    if name.endswith("Multilingual"):
        name = name[: -len("Multilingual")]
    accent = ACCENTS.get(locale, locale)
    label = f"{name} · {accent}" + (" · Azure" if engine == "azure" else "")
    return {"engine": engine, "id": short_name, "label": label, "british": locale == "en-GB", "name": name}


def list_voices(settings) -> list:
    """Every adult English female voice available: free Microsoft voices, plus
    Azure voices if an Azure Speech key is set. British first, then by accent."""
    voices = {}
    key = get_azure_key()
    region = settings.get("azure_region", "uksouth").strip() or "uksouth"
    if key:
        try:
            import requests
            r = requests.get(f"https://{region}.tts.speech.microsoft.com/cognitiveservices/voices/list",
                             headers={"Ocp-Apim-Subscription-Key": key}, timeout=15)
            r.raise_for_status()
            for v in r.json():
                if v.get("Locale", "").startswith("en-") and v.get("Gender") == "Female":
                    e = _voice_entry("azure", v["ShortName"])
                    if e["name"] not in CHILD_VOICES:
                        voices[("azure", v["ShortName"])] = e
        except Exception as e:
            log.warning("Couldn't list Azure voices: %s", e)
    try:
        import asyncio
        import edge_tts
        live = asyncio.run(edge_tts.list_voices())
        names = [v["ShortName"] for v in live if v.get("Locale", "").startswith("en-") and v.get("Gender") == "Female"]
    except Exception as e:
        log.warning("Couldn't list free voices: %s", e)
        names = []
    if not names:
        cached = read_json(VOICES_CACHE, [])
        names = [c["id"] for c in cached if c.get("engine") == "natural"] or _FALLBACK_FREE
    for n in names:
        e = _voice_entry("natural", n)
        if e["name"] not in CHILD_VOICES:
            voices[("natural", n)] = e
    out = sorted(voices.values(), key=lambda v: (not v["british"], v["engine"] != "azure",
                                                 v["label"].split(" · ")[1], v["name"]))
    try:
        write_json(VOICES_CACHE, out)
    except Exception:
        pass
    out.append({"engine": "piper", "id": "", "label": "Offline voice (no internet needed)", "british": True,
                "name": "Offline"})
    return out


def _trim(audio, rate, gap=0.12):
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


def _phrases(sentences, min_len=70):
    """Join short sentences so they're spoken in one breath, with proper
    intonation, instead of as separate clips ("Good afternoon Harry." + next)."""
    buf = ""
    for s in sentences:
        buf = f"{buf} {s}".strip() if buf else s
        if len(buf) >= min_len:
            yield buf
            buf = ""
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
        if load:
            self._load_piper()

    @property
    def voice(self):  # "can Moo speak at all?"
        return True if self.settings.get("tts_engine", "natural") in ("natural", "azure") else self.piper

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
        if self.settings.get("clear_speech", True):
            pct -= 7  # a touch slower, every word lands
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
        if engine == "azure":
            key = get_azure_key()
            try:
                if not key:
                    raise RuntimeError("no Azure key set")
                return _azure_synth(sentence, self.settings.get("azure_voice", "en-GB-SoniaNeural"), self._speed_pct(),
                                    key, self.settings.get("azure_region", "uksouth").strip() or "uksouth", self._pitch())
            except Exception as e:
                log.warning("Azure voice unavailable (%s); trying the free voice.", e)
                engine = "natural"
        if engine == "natural":
            try:
                return _edge_synth(sentence, self.settings.get("natural_voice", "en-GB-SoniaNeural"), self._speed_pct(),
                                   self._pitch())
            except Exception as e:
                log.warning("Natural voice unavailable (%s); using offline voice.", e)
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


class Ears:
    """One microphone stream. Normally listens for the wake word; when asked,
    records one utterance and transcribes it."""

    def __init__(self, settings, on_wake):
        self.settings = settings
        self.on_wake = on_wake
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
        return (self.wake_name or "").replace("_", " ").title()

    def _callback(self, indata, frames, time_info, status):
        try:
            self.frames.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass

    # ----- wake word -----
    def _wake_loop(self):
        while True:
            frame = self.frames.get()
            threshold = float(self.settings.get("wake_word_threshold", 0.5))
            enabled = self.settings.get("wake_word_enabled", True)
            if self.mode != "wake" or self.wake_model is None or not enabled:
                if self.mode == "record":
                    self._record_q.put(frame)
                continue
            scores = self.wake_model.predict(frame)
            if max(scores.values(), default=0) >= threshold and time.time() - self._last_wake > 2:
                self._last_wake = time.time()
                self.wake_model.reset()
                self.on_wake()

    # ----- recording -----
    def reload_whisper(self):
        threading.Thread(target=self._load_whisper, daemon=True, name="whisper-reload").start()

    def listen(self, max_seconds=20, start_timeout=6, silence=None) -> str:
        """Record one utterance (stops when you pause) and return the text."""
        if not self.available:
            raise RuntimeError("No microphone found.")
        if self.whisper is None:
            raise RuntimeError("Speech recognition is still loading, give it a moment.")
        if silence is None:
            try:
                silence = max(0.4, min(float(self.settings.get("listen_pause", 0.9)), 3.0))
            except (TypeError, ValueError):
                silence = 0.9
        self._record_q = queue.Queue()
        # drain stale audio
        while not self.frames.empty():
            try:
                self.frames.get_nowait()
            except queue.Empty:
                break
        self.mode = "record"
        audio, started = [], False
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
            self.mode = "wake"
        pcm = np.concatenate(audio).astype(np.float32) / 32768.0
        t0 = time.time()

        def transcribe():
            segments, _ = self.whisper.transcribe(
                pcm, language="en", beam_size=1, vad_filter=True, without_timestamps=True,
                initial_prompt=(self.settings.get("vocabulary") or "").strip() or None)
            return " ".join(seg.text.strip() for seg in segments).strip()

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
