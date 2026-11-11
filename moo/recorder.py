"""Record a Teams (or Zoom, Meet, phone-on-speaker) meeting: both sides, transcribed as it goes.

Two streams are captured at once:
  - your microphone  -> "you"
  - what the PC plays (the other people in the call, via Windows loopback) -> "them"
Each is cut into phrases on the pauses and transcribed while the meeting runs (Groq in the cloud when set up,
otherwise the PC's own speech recognition), so the transcript is ready seconds after you stop.
The audio is kept as an .m4a next to the transcript.

Works with a headset or speakers. With speakers, your mic also hears the other side; those echoes are
recognised and dropped from your lines.
"""
import difflib
import logging
import queue
import re
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

log = logging.getLogger("moo")
RATE = 16000
BLOCK = 480  # 30 ms at 16 kHz
TEAMS_TITLE = re.compile(r"(meeting|call|huddle)\b.*\|\s*microsoft teams|microsoft teams.*\b(meeting|call)\b|"
                         r"^zoom meeting|\bgoogle meet\b|meet - ", re.I)


def _soundcard_sources():
    """(mic, loopback) block generators from Windows, float32 mono at 16 kHz."""
    import soundcard as sc

    def gen(dev):
        with dev.recorder(samplerate=48000, channels=1, blocksize=1440) as r:
            while True:
                data = r.record(numframes=1440)  # 30 ms
                mono = data.mean(axis=1) if data.ndim > 1 else data
                yield mono[: len(mono) // 3 * 3].reshape(-1, 3).mean(axis=1).astype(np.float32)
    spk = sc.default_speaker()
    loop = sc.get_microphone(id=str(spk.name), include_loopback=True)
    mic = sc.default_microphone()
    return (lambda: gen(mic)), (lambda: gen(loop))


class _Stream:
    """Cuts one audio stream into spoken phrases and keeps its audio, padded with silence for any gaps."""
    def __init__(self, who, out_path, on_phrase, t0):
        self.who, self.on_phrase, self.t0 = who, on_phrase, t0
        self.wav = wave.open(str(out_path), "wb")
        self.wav.setnchannels(1)
        self.wav.setsampwidth(2)
        self.wav.setframerate(RATE)
        self.written = 0
        self.noise, self.seg, self.seg_start, self.quiet = None, [], 0.0, 0
        self.level = 0.0

    def feed(self, block, now):
        # keep the file in step with the clock (loopback sends nothing while the PC is silent)
        due = int((now - self.t0) * RATE) - len(block)
        if due - self.written > RATE // 2:
            pad = due - self.written
            self.wav.writeframes(np.zeros(pad, np.int16).tobytes())
            self.written += pad
            self._end_phrase()
        pcm = (np.clip(block, -1, 1) * 32767).astype(np.int16)
        self.wav.writeframes(pcm.tobytes())
        self.written += len(pcm)
        rms = float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2))) if len(pcm) else 0.0
        self.level = rms
        if self.noise is None:
            self.noise = min(rms, 300.0)
        self.noise = self.noise * 0.9 + rms * 0.1 if rms < self.noise else self.noise * 0.998 + rms * 0.002
        speaking = rms > max(self.noise * 3.0, 220.0)
        if speaking:
            if not self.seg:
                self.seg_start = self.written / RATE - len(pcm) / RATE
            self.seg.append(pcm)
            self.quiet = 0
        elif self.seg:
            self.seg.append(pcm)
            self.quiet += 1
            if self.quiet >= 27:  # ~0.8 s pause
                self._end_phrase()
        if self.seg and len(self.seg) >= 830:  # 25 s of talking without a break
            self._end_phrase()

    def _end_phrase(self):
        if self.seg and len(self.seg) - self.quiet >= 15:  # at least ~0.45 s of sound
            self.on_phrase(self.who, self.seg_start, np.concatenate(self.seg))
        self.seg, self.quiet = [], 0

    def close(self):
        self._end_phrase()
        self.wav.close()


class MeetingRecorder:
    def __init__(self, settings, transcribe_local=None, sources=None):
        self.s = settings
        self.transcribe_local = transcribe_local  # fn(float32 pcm) -> text, the PC's own Whisper
        self.sources = sources  # for tests: (mic_factory, loop_factory)
        self.started = None
        self.title = ""
        self._stop = threading.Event()
        self._q = queue.Queue()
        self.lines = []
        self._threads = []
        self.folder = None

    @property
    def running(self):
        return self.started is not None

    def start(self, title="", folder=None):
        if self.running:
            return "already"
        mic_f, loop_f = self.sources or _soundcard_sources()
        stamp = datetime.now()
        self.title = title or "Meeting"
        safe = re.sub(r'[<>:"/\\|?*]+', "", self.title)[:60]
        self.folder = Path(folder) / f"{stamp:%Y-%m-%d %H%M} {safe}"
        self.folder.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self.lines, self.started = [], time.time()
        self.streams = {"you": _Stream("you", self.folder / "you.wav", self._phrase, self.started),
                        "them": _Stream("them", self.folder / "them.wav", self._phrase, self.started)}
        self._threads = [threading.Thread(target=self._capture, args=(w, f), daemon=True, name=f"rec-{w}")
                         for w, f in (("you", mic_f), ("them", loop_f))]
        self._threads.append(threading.Thread(target=self._worker, daemon=True, name="rec-transcribe"))
        for t in self._threads:
            t.start()
        return "started"

    def _capture(self, who, factory):
        try:
            gen = factory()
            for block in gen:
                if self._stop.is_set():
                    break
                self.streams[who].feed(block, time.time())
        except Exception as e:
            log.warning("Recording %s stopped: %s", who, e)
            self.errors = getattr(self, "errors", []) + [f"{who}: {e}"]

    def _phrase(self, who, t, pcm16):
        self._q.put((who, t, pcm16))

    def _worker(self):
        from .voice import cloud_transcribe
        while True:
            item = self._q.get()
            try:
                if item is None:
                    return
                who, t, pcm16 = item
                pcm = pcm16.astype(np.float32) / 32768.0
                text = cloud_transcribe(pcm, self.s)
                if text is None and self.transcribe_local:
                    text = self.transcribe_local(pcm)
                text = (text or "").strip()
                if text and text.lower().strip(" .!") not in ("thank you", "thanks for watching", "you", "bye", "so"):
                    self.lines.append((t, who, text))
            except Exception as e:
                log.warning("Meeting transcription failed: %s", e)
            finally:
                self._q.task_done()

    def stop(self):
        """Stop, finish transcribing, save audio and transcript. Returns a dict."""
        if not self.running:
            return None
        self._stop.set()
        for t in self._threads[:2]:
            t.join(timeout=3)
        for st in self.streams.values():
            st.close()
        self._q.put(None)
        self._q.join()
        secs = time.time() - self.started
        self.started = None
        lines = self._dedupe(sorted(self.lines))
        audio = self._mix()
        you = self.s.get("user_name") or "Me"
        transcript = "\n".join(f"[{int(t // 60):02d}:{int(t % 60):02d}] {you if w == 'you' else 'Them'}: {x}"
                               for t, w, x in lines)
        (self.folder / "transcript.txt").write_text(
            f"{self.title}, {datetime.now():%A %d %B %Y}\nLength {int(secs // 60)} min\n\n{transcript}\n", encoding="utf-8")
        return {"lines": lines, "transcript": transcript, "seconds": secs, "folder": self.folder, "audio": audio,
                "errors": getattr(self, "errors", [])}

    @staticmethod
    def _dedupe(lines):
        """With speakers, the mic hears the other side too: drop 'you' lines that echo a 'them' line."""
        them = [(t, x.lower()) for t, w, x in lines if w == "them"]
        out = []
        for t, w, x in lines:
            if w == "you":
                low = x.lower()
                if any(abs(t - t2) < 8 and difflib.SequenceMatcher(None, low, x2).ratio() > 0.6 for t2, x2 in them):
                    continue
            out.append((t, w, x))
        return out

    def _mix(self):
        """you.wav + them.wav -> meeting.m4a (and the wavs are removed)."""
        try:
            import av
            a = np.memmap(self.folder / "you.wav", dtype=np.int16, mode="r", offset=44)
            b = np.memmap(self.folder / "them.wav", dtype=np.int16, mode="r", offset=44)
            n = max(len(a), len(b))
            out_path = self.folder / "meeting.m4a"
            out = av.open(str(out_path), "w", format="mp4")
            st = out.add_stream("aac", rate=RATE)
            st.bit_rate = 32000
            rs = av.AudioResampler(format="fltp", layout="mono", rate=RATE)
            step = RATE * 10
            for i in range(0, n, step):
                ca = np.zeros(min(step, n - i), np.float32)
                cb = ca.copy()
                pa, pb = a[i:i + step], b[i:i + step]
                ca[:len(pa)] = pa / 32768.0
                cb[:len(pb)] = pb / 32768.0
                mix = np.clip(ca * 0.7 + cb * 0.7, -1, 1).reshape(1, -1)
                fr = av.AudioFrame.from_ndarray(mix, format="flt", layout="mono")
                fr.sample_rate = RATE
                for f in rs.resample(fr):
                    for p in st.encode(f):
                        out.mux(p)
            for f in rs.resample(None):
                for p in st.encode(f):
                    out.mux(p)
            for p in st.encode(None):
                out.mux(p)
            out.close()
            del a, b
            for f in ("you.wav", "them.wav"):
                try:
                    (self.folder / f).unlink()
                except OSError:
                    pass
            return out_path
        except Exception as e:
            log.warning("Couldn't make the meeting audio file: %s", e)
            return None


def meeting_window():
    """The title of a Teams/Zoom/Meet meeting window that's open now, if any."""
    import sys
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes
    found = []
    user32 = ctypes.windll.user32

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if TEAMS_TITLE.search(buf.value):
                    found.append(buf.value)
        return True
    user32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(cb), 0)
    return found[0] if found else None
