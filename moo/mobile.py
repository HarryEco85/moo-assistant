"""The Moo phone app link.

The phone app (a web app you install on your home screen) and this PC pass
messages through your own small Supabase project. Everything goes through
locked-down database functions that need Moo's secret pairing code, which is
made on this PC and handed to the phone by QR code.

PC side: every second or so, pick up anything you said in the app, answer it
(with her voice as audio), and keep the app's at-a-glance panel up to date.
"""
import base64
import io
import logging
import queue
import secrets
import threading
import time
import uuid
from urllib.parse import quote

import numpy as np
import requests

from .config import DATA_DIR, get_secret, read_json, set_secret, user_name, write_json

log = logging.getLogger("moo")
STATE_FILE = DATA_DIR / "mobile.json"
APP_URL = "https://harryeco85.github.io/moo-assistant/"


def encode_m4a(audio: np.ndarray, rate: int) -> bytes:
    """float32 mono audio -> AAC in an .m4a (plays on every phone)."""
    import av
    buf = io.BytesIO()
    out = av.open(buf, "w", format="mp4")
    st = out.add_stream("aac", rate=24000)
    st.bit_rate = 48000
    frame = av.AudioFrame.from_ndarray(audio.astype(np.float32).reshape(1, -1), format="flt", layout="mono")
    frame.sample_rate = rate
    rs = av.AudioResampler(format="fltp", layout="mono", rate=24000)
    for f in list(rs.resample(frame)) + list(rs.resample(None)):
        for p in st.encode(f):
            out.mux(p)
    for p in st.encode(None):
        out.mux(p)
    out.close()
    return buf.getvalue()


class Mobile:
    def __init__(self, app):
        self.app = app
        self.status = "off"
        self._stop = threading.Event()
        self._thread = None
        self._last_state = 0
        self._lock = threading.Lock()
        self._poll_every = 0.6

    @property
    def s(self):
        return self.app.settings

    # ---------- setup ----------
    def configured(self) -> bool:
        return bool(self.s.get("mobile_url") and self.s.get("mobile_key") and self.s.get("mobile_enabled", True))

    def token(self) -> str:
        t = get_secret("mobile_token")
        if not t:
            t = secrets.token_urlsafe(32)
            set_secret("mobile_token", t)
        return t

    def _base(self) -> str:
        url = (self.s.get("mobile_url") or "").strip().rstrip("/")
        if url and not url.startswith("http"):
            url = "https://" + url
        return url

    def rpc(self, fn, timeout=20, **params):
        r = requests.post(f"{self._base()}/rest/v1/rpc/{fn}", json=params, timeout=timeout,
                          headers={"apikey": self.s.get("mobile_key", "").strip(), "Content-Type": "application/json"})
        if r.status_code >= 400:
            try:
                msg = r.json().get("message") or r.text
            except ValueError:
                msg = r.text
            raise RuntimeError(f"{r.status_code}: {str(msg)[:200]}")
        return r.json() if r.text else None

    def connect(self) -> str:
        """Check the project is set up, pair this PC with it, and start."""
        if not (self.s.get("mobile_url") and self.s.get("mobile_key")):
            return "Paste the Project URL and the publishable (anon) key first."
        try:
            ok = self.rpc("moo_register", p_token=self.token())
        except Exception as e:
            text = str(e)
            if "moo_register" in text or "404" in text or "PGRST202" in text:
                return ("The project isn't set up yet. Click 'Copy setup script', paste it into Supabase "
                        "(SQL Editor > New query) and press Run, then Connect again.")
            if "401" in text or "Invalid API key" in text:
                return "Supabase didn't accept that key. Copy the publishable (or anon public) key again."
            return f"Couldn't reach Supabase: {text}"
        if not ok:
            return ("That project is already paired with another Moo. In Supabase, run: delete from moo_keys;  "
                    "then click Connect again.")
        self.s["mobile_enabled"] = True
        from .config import save_settings
        save_settings(self.s)
        self.restart()
        return "Connected. Now scan the QR code with your phone's camera."

    def pair_link(self) -> str:
        name = self.s.get("assistant_name", "Moo") or "Moo"
        return (f"{self.s.get('mobile_app_url') or APP_URL}#u={quote(self._base(), safe='')}"
                f"&k={quote(self.s.get('mobile_key', '').strip(), safe='')}&t={quote(self.token(), safe='')}"
                f"&n={quote(name, safe='')}")

    def qr_image(self, size=360):
        import qrcode
        q = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
        q.add_data(self.pair_link())
        q.make(fit=True)
        img = q.make_image(fill_color="black", back_color="white").convert("RGB")
        from PIL import Image
        return img.resize((size, size), Image.NEAREST)

    # ---------- loop ----------
    def start(self):
        if not self.configured() or (self._thread and self._thread.is_alive()):
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, args=(self._stop,), daemon=True, name="mobile")
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.status = "off"

    def restart(self):
        self.stop()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None
        self.start()

    def _loop(self, stop):
        state = read_json(STATE_FILE, {})
        last = state.get("last_id")
        fails = 0
        while not stop.is_set():
            try:
                if last is None:  # first run: start from now
                    last = int(self.rpc("moo_last_id", p_token=self.token()) or 0)
                    write_json(STATE_FILE, {"last_id": last})
                msgs = self.rpc("moo_poll", p_token=self.token(), p_sender="phone", p_after=last) or []
                self.status = "connected"
                fails = 0
                for m in msgs:
                    last = max(last, int(m["id"]))
                    write_json(STATE_FILE, {"last_id": last})
                    if stop.is_set():
                        break
                    try:
                        self._handle(m)
                    except Exception:
                        log.exception("Phone app message failed")
                        self.post("text", "Sorry, something went wrong on the PC with that one.")
                if time.time() - self._last_state > 30:
                    self.push_state()
            except Exception as e:
                fails += 1
                self.status = f"can't reach Supabase ({str(e)[:60]})"
                if fails in (1, 10):
                    log.warning("Phone app link: %s", e)
                stop.wait(min(30, 2 * fails))
                continue
            stop.wait(self._poll_every)

    # ---------- sending ----------
    def post(self, kind, body="", audio=None, mime=None, meta=None):
        if not self.configured():
            return
        try:
            self.rpc("moo_post", timeout=40, p_token=self.token(), p_sender="pc", p_kind=kind, p_body=body,
                     p_audio=audio, p_mime=mime, p_meta=meta or {})
        except Exception as e:
            log.warning("Couldn't send to the phone app: %s", e)

    def _voice(self, text):
        """Her voice for the app, as base64 .m4a (None if voice is off or fails)."""
        mouth = self.app.mouth
        if not mouth or not self.s.get("mobile_voice", True):
            return None
        try:
            import re
            from .voice import _phrases, clean_for_speech
            parts, rate = [], 24000
            for phrase in _phrases(re.split(r"(?<=[.!?])\s+", clean_for_speech(text))):
                audio, r = mouth.synth(phrase)
                if audio is not None and len(audio):
                    if parts and r == rate:
                        parts.append(np.zeros(int(rate * 0.18), dtype=np.float32))
                    parts.append(audio.astype(np.float32))
                    rate = r
            if not parts:
                return None
            return base64.b64encode(encode_m4a(np.concatenate(parts), rate)).decode()
        except Exception as e:
            log.warning("Voice for the phone app failed: %s", e)
            return None

    def say(self, text, kind="text"):
        """Text plus her voice."""
        audio = self._voice(text)
        self.post(kind, text, audio, "audio/mp4" if audio else None)

    def push_text(self, text, speak=False):
        """An alert, reminder or approval result from the PC."""
        if not self.configured() or self.status != "connected":
            return
        if speak:
            self.say(text, "alert")
        else:
            self.post("alert", text)

    def push_state(self):
        self._last_state = time.time()
        try:
            self.rpc("moo_state_set", p_token=self.token(), p_data=self._state())
        except Exception as e:
            log.debug("State push failed: %s", e)

    def _state(self) -> dict:
        app = self.app
        w = getattr(app, "watcher", None)
        stats = w.stats if w else {}
        email = stats.get("email", {})
        inbox = email.get("alert") if email.get("alert") and time.time() - email.get("alert_at", 0) < 6 * 3600 else None
        try:
            rem = app.reminders.list()
        except Exception:
            rem = []
        nxt = rem[0] if rem else None
        approvals = [{"id": a["id"], "title": a["title"], "detail": a.get("detail", ""), "status": a["status"]}
                     for a in app.approvals.pending()] if getattr(app, "approvals", None) else []
        spend = 0.0
        try:
            spend = app.brain.usage.this_month()
        except Exception:
            pass
        return {"name": self.s.get("assistant_name", "Moo") or "Moo", "user": user_name(self.s),
                "title": (self.s.get("header_title") or "").strip(),
                "version": __import__("moo").__version__, "pc_name": __import__("platform").node(),
                "next": (stats.get("meetings", {}).get("text") or "").replace("Next: ", "") or "Nothing coming up",
                "inbox": inbox or "Nothing urgent",
                "reminder": f"{nxt['due'][11:16]} · {nxt['text']}" if nxt else "None set",
                "approvals": approvals, "spend": round(spend, 2), "pc_state": getattr(app.ui, "state", ""),
                "time": time.strftime("%H:%M")}

    # ---------- receiving ----------
    def _handle(self, m):
        kind, body, meta = m.get("kind"), (m.get("body") or "").strip(), m.get("meta") or {}
        if kind == "hello":
            self.push_state()
            return
        if kind == "action":
            aid, action = meta.get("id"), meta.get("action")
            if action == "approve":
                if not self.app.approve(aid, from_app=True):
                    self.post("text", "That one's already been dealt with.")
            elif action == "reject":
                self.app.reject(aid)
                self.post("text", "Rejected, I'll leave it.")
            self.push_state()
            return
        images = []
        if kind == "voice" and m.get("audio"):
            body = self._transcribe(base64.b64decode(m["audio"]))
            self.post("heard", body or "(I couldn't make that out)")
            if not body:
                self.say("Sorry, I didn't catch that. Try again?")
                return
        elif kind == "photo" and m.get("audio"):
            from .screen import bytes_to_block
            images = [bytes_to_block(base64.b64decode(m["audio"]))]
            body = body or "What do you make of this photo?"
        if not body:
            return
        rid = uuid.uuid4().hex[:8]
        speaker = _Speaker(self, rid) if (self.s.get("mobile_voice", True) and self.app.mouth) else None
        reply, extras = self.app.ask_from_phone(body, images=images, source="app",
                                                on_sentence=speaker.add if speaker else None)
        spoke = speaker.finish() if speaker else False
        if spoke:
            self.post("text", reply, meta={"rid": rid, "streamed": True})
        else:
            self.say(reply)
        for label, content in extras:
            if label == "__image__":
                self.post("image", "", base64.b64encode(content).decode(), "image/png")
            else:
                self.post("draft", content, meta={"label": label})
        self.push_state()

    def _transcribe(self, data: bytes) -> str:
        ears = self.app.ears
        if not ears or ears.whisper is None:
            return ""
        from .phone import decode_voice
        from .voice import cloud_transcribe
        pcm = decode_voice(data)
        text = cloud_transcribe(pcm, self.s)
        if text is not None:
            return text
        with ears._wlock:
            segs, _ = ears.whisper.transcribe(pcm, language="en", beam_size=3, vad_filter=True,
                                              initial_prompt=(self.s.get("vocabulary") or "").strip() or None)
            return " ".join(s.text.strip() for s in segs).strip()


class _Speaker:
    """Speaks a reply into the phone app as it's written: the first sentence goes out straight away,
    the rest in batches, so she starts talking long before the whole answer is ready."""

    def __init__(self, mobile, rid):
        self.m, self.rid = mobile, rid
        self.q = queue.Queue()
        self.sent = 0
        self.t = threading.Thread(target=self._run, daemon=True, name="app-speech")
        self.t.start()

    def add(self, sentence):
        if sentence and sentence.strip():
            self.q.put(sentence.strip())

    def finish(self) -> bool:
        """Wait for the last bit to go; True if anything was spoken."""
        self.q.put(None)
        self.t.join(timeout=120)
        return self.sent > 0

    def _run(self):
        done = False
        while not done:
            first = self.q.get()
            if first is None:
                return
            batch = [first]
            while True:  # whatever else has arrived while the last chunk was being spoken goes together
                try:
                    nxt = self.q.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    done = True
                    break
                batch.append(nxt)
            text = " ".join(batch)
            audio = self.m._voice(text)
            if audio:
                self.m.post("speech", text, audio, "audio/mp4", meta={"rid": self.rid, "seq": self.sent})
                self.sent += 1
