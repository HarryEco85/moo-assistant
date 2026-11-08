"""Talk to Moo from your phone through Telegram.

Moo connects out to Telegram (no router or network setup), so it works
anywhere your phone has signal, as long as the PC is on and Moo is open.
Only the one Telegram account that pairs with the code in Settings can use
her; everyone else is ignored.

Setup (see README): create a bot with @BotFather, paste its token into
Settings > Phone, then send the pairing code to your bot.
"""
import io
import json
import logging
import random
import threading
import time

import numpy as np
import requests

from .config import get_secret, save_settings

log = logging.getLogger("moo")
API = "https://api.telegram.org"


def decode_voice(data: bytes) -> np.ndarray:
    """Telegram voice note (OGG/Opus) -> 16 kHz mono float32 for Whisper."""
    import av
    out = []
    with av.open(io.BytesIO(data)) as container:
        rs = av.AudioResampler(format="flt", layout="mono", rate=16000)
        for frame in container.decode(audio=0):
            for f in rs.resample(frame):
                out.append(f.to_ndarray().reshape(-1))
        for f in rs.resample(None):
            out.append(f.to_ndarray().reshape(-1))
    return np.concatenate(out).astype(np.float32) if out else np.zeros(0, dtype=np.float32)


def encode_voice(audio: np.ndarray, rate: int) -> bytes:
    """float32 mono audio -> OGG/Opus voice note."""
    import av
    buf = io.BytesIO()
    out = av.open(buf, "w", format="ogg")
    st = out.add_stream("libopus", rate=48000)
    st.layout = "mono"
    frame = av.AudioFrame.from_ndarray(audio.astype(np.float32).reshape(1, -1), format="flt", layout="mono")
    frame.sample_rate = rate
    rs = av.AudioResampler(format="s16", layout="mono", rate=48000)
    for f in list(rs.resample(frame)) + list(rs.resample(None)):
        for p in st.encode(f):
            out.mux(p)
    for p in st.encode(None):
        out.mux(p)
    out.close()
    return buf.getvalue()


class Phone:
    def __init__(self, app):
        self.app = app
        self._stop = threading.Event()
        self._thread = None
        self.status = "off"

    # ---------- settings helpers ----------
    @property
    def s(self):
        return self.app.settings

    def token(self):
        return get_secret("telegram_token")

    def paired_id(self):
        return int(self.s.get("telegram_user_id") or 0)

    def pairing_code(self):
        if not self.s.get("telegram_pair_code"):
            self.s["telegram_pair_code"] = f"{random.SystemRandom().randint(0, 999999):06d}"
            save_settings(self.s)
        return self.s["telegram_pair_code"]

    def unpair(self):
        self.s["telegram_user_id"] = 0
        self.s["telegram_pair_code"] = ""
        save_settings(self.s)

    # ---------- Telegram calls ----------
    def _call(self, method, http_timeout=30, **params):
        files = params.pop("files", None)
        r = requests.post(f"{API}/bot{self.token()}/{method}", data=params, files=files, timeout=http_timeout)
        r.raise_for_status()
        return r.json().get("result")

    def send_text(self, text, chat_id=None):
        chat_id = chat_id or self.paired_id()
        if not chat_id or not self.token():
            return
        for i in range(0, len(text), 3900):  # Telegram's message size limit
            self._call("sendMessage", chat_id=chat_id, text=text[i:i + 3900])

    def send_voice(self, text, chat_id=None):
        mouth = self.app.mouth
        if not mouth:
            return
        from .voice import _phrases, clean_for_speech
        import re
        parts, rate = [], 24000
        for phrase in _phrases(re.split(r"(?<=[.!?])\s+", clean_for_speech(text))):
            audio, r = mouth.synth(phrase)
            if audio is not None and len(audio):
                parts.append(audio)
                rate = r
        if parts:
            data = encode_voice(np.concatenate(parts), rate)
            self._call("sendVoice", http_timeout=60, chat_id=chat_id or self.paired_id(),
                       files={"voice": ("moo.ogg", data, "audio/ogg")})

    def notify(self, text):
        """Push something to the phone (reminders, briefing) if paired."""
        if self.s.get("phone_enabled") and self.paired_id() and self.token():
            try:
                self.send_text(text)
            except Exception as e:
                log.warning("Couldn't send to phone: %s", e)

    def send_approval(self, item):
        """An approval request with Approve / Reject buttons."""
        if not (self.s.get("phone_enabled") and self.paired_id() and self.token()):
            return
        text = f"Needs your OK: {item['title']}" + (f"\n{item['detail']}" if item.get("detail") else "")
        markup = {"inline_keyboard": [[{"text": "✅ Approve", "callback_data": f"ap:{item['id']}"},
                                       {"text": "✖ Reject", "callback_data": f"rj:{item['id']}"}]]}
        try:
            self._call("sendMessage", chat_id=self.paired_id(), text=text, reply_markup=json.dumps(markup))
        except Exception as e:
            log.warning("Couldn't send approval to phone: %s", e)

    def _callback(self, cq):
        """A tap on an Approve / Reject button."""
        if cq.get("from", {}).get("id") != self.paired_id():
            return
        data = cq.get("data") or ""
        kind, _, aid = data.partition(":")
        app, ok = self.app, False
        if kind == "ap":
            ok = app.approve(aid, from_phone=True)
            note = "✅ Approved, on it." if ok else "Already handled."
        elif kind == "rj":
            ok = app.reject(aid, from_phone=True)
            note = "✖ Rejected." if ok else "Already handled."
        else:
            note = "?"
        try:
            self._call("answerCallbackQuery", callback_query_id=cq["id"], text=note[:60])
            msg = cq.get("message") or {}
            if msg:
                self._call("editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                           text=f"{msg.get('text', '')}\n\n{note}")
        except Exception as e:
            log.warning("Callback reply failed: %s", e)

    # ---------- loop ----------
    def start(self):
        if self._thread and self._thread.is_alive() and not self._stop.is_set():
            return
        if not (self.s.get("phone_enabled") and self.token()):
            self.status = "off"
            return
        self._stop = threading.Event()  # fresh switch for this run; an old loop keeps its own (set) one
        self._thread = threading.Thread(target=self._loop, args=(self._stop,), daemon=True, name="phone")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def restart(self):
        self.stop()
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None
        self.start()

    def _loop(self, stop):
        offset = None
        try:
            me = self._call("getMe")
            self.status = f"connected as @{me.get('username')}"
            log.info("Phone link %s", self.status)
        except Exception as e:
            self.status = "token not working"
            log.warning("Telegram token check failed: %s", e)
            return
        while not stop.is_set():
            try:
                params = {"timeout": 50, "allowed_updates": '["message","callback_query"]'}
                if offset:
                    params["offset"] = offset
                updates = self._call("getUpdates", http_timeout=65, **params) or []
                for u in updates:
                    offset = u["update_id"] + 1
                    if u.get("callback_query") and not stop.is_set():
                        try:
                            self._callback(u["callback_query"])
                        except Exception:
                            log.exception("Phone button failed")
                        continue
                    msg = u.get("message")
                    if msg and not stop.is_set():
                        try:
                            self._handle(msg)
                        except Exception as e:
                            log.exception("Phone message failed")
                            try:
                                self.send_text(f"Sorry, something went wrong: {str(e)[:150]}", msg["chat"]["id"])
                            except Exception:
                                pass
            except Exception as e:
                log.warning("Phone link hiccup: %s", e)
                self.status = f"reconnecting ({str(e)[:60]})"
                stop.wait(5)

    def _handle(self, msg):
        chat_id, user_id = msg["chat"]["id"], msg["from"]["id"]
        text = (msg.get("text") or "").strip()
        if not self.paired_id():
            if self.pairing_code() in text.replace(" ", ""):
                self.s["telegram_user_id"] = user_id
                save_settings(self.s)
                self.send_text(f"Paired. Hello from {self.s.get('assistant_name', 'Moo')} on your PC. "
                               "Message me or send a voice note any time.", chat_id)
                self.app.ui.add_message("sys", "Phone paired with Telegram.")
            else:
                self.send_text("Send the pairing code shown in Moo's Settings > Phone.", chat_id)
            return
        if user_id != self.paired_id():
            return  # not Harry's account: ignore completely
        voice_in = False
        if msg.get("voice") or msg.get("audio"):
            voice_in = True
            text = self._transcribe(msg.get("voice") or msg.get("audio"))
            if not text:
                self.send_text("Sorry, I couldn't make that out.", chat_id)
                return
        if not text or text.startswith("/start"):
            self.send_text("I'm here. What do you need?", chat_id)
            return
        self._call("sendChatAction", chat_id=chat_id, action="typing")
        reply, drafts = self.app.ask_from_phone(text, heard=voice_in)
        for label, body in drafts:
            if label == "__image__":
                try:
                    self._call("sendPhoto", http_timeout=60, chat_id=chat_id,
                               files={"photo": ("card.png", body, "image/png")})
                except Exception as e:
                    log.warning("Couldn't send card to phone: %s", e)
                continue
            self.send_text(f"{label}:\n\n{body}", chat_id)
        self.send_text(reply, chat_id)
        if voice_in and self.s.get("phone_voice_replies", True):
            try:
                self._call("sendChatAction", chat_id=chat_id, action="record_voice")
                self.send_voice(reply, chat_id)
            except Exception as e:
                log.warning("Voice reply failed: %s", e)

    def _transcribe(self, media) -> str:
        ears = self.app.ears
        if not ears or ears.whisper is None:
            return ""
        f = self._call("getFile", file_id=media["file_id"])
        data = requests.get(f"{API}/file/bot{self.token()}/{f['file_path']}", timeout=60).content
        pcm = decode_voice(data)
        t0 = time.time()
        segments, _ = ears.whisper.transcribe(pcm, language="en", beam_size=1, vad_filter=True,
                                              initial_prompt=(self.s.get("vocabulary") or "").strip() or None)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        log.info("Phone voice note transcribed in %.1fs", time.time() - t0)
        return text
