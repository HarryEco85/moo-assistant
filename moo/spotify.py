"""Spotify: play, pause, skip, find and queue music, and move it between your devices (PC, phone, car, speakers).

Setup (once, about two minutes): developer.spotify.com > Dashboard > Create app.
  Redirect URI: http://127.0.0.1:8888/callback   (tick "Web API")
Copy the Client ID into Settings > Accounts > Spotify and press Connect. Controlling playback needs Premium.
Sign-in uses PKCE, so no client secret is stored; the refresh token is kept in Windows Credential Manager.
"""
import base64
import hashlib
import http.server
import logging
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser

import requests

from .config import get_secret, set_secret

log = logging.getLogger("moo")
REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = ("user-read-playback-state user-modify-playback-state user-read-currently-playing "
          "playlist-read-private playlist-read-collaborative user-library-read user-top-read user-read-recently-played")
API = "https://api.spotify.com/v1"


class Spotify:
    def __init__(self, settings):
        self.s = settings
        self._token, self._expires = None, 0
        self._lock = threading.Lock()

    @property
    def client_id(self):
        return (self.s.get("spotify_client_id") or "").strip()

    @property
    def enabled(self):
        return bool(self.client_id and get_secret("spotify_refresh"))

    # ---------- sign-in ----------
    def connect(self, timeout=180) -> str:
        if not self.client_id:
            return "Add the Spotify Client ID first (Settings > Accounts > Spotify)."
        verifier = base64.urlsafe_b64encode(os.urandom(48)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        got = {}

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if q.get("state", [""])[0] == state:
                    got.update({k: v[0] for k, v in q.items()})
                ok = "code" in got
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write((f"<html><body style='font-family:sans-serif;padding:40px'><h2>"
                                  f"{'Spotify connected to Moo. You can close this tab.' if ok else 'That did not work. Try again from Moo.'}"
                                  "</h2></body></html>").encode())
        try:
            srv = http.server.HTTPServer(("127.0.0.1", 8888), H)
        except OSError:
            return "Port 8888 is busy on this PC, so I can't finish the Spotify sign-in. Close whatever's using it and retry."
        srv.timeout = 1
        webbrowser.open("https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
            "client_id": self.client_id, "response_type": "code", "redirect_uri": REDIRECT, "scope": SCOPES,
            "code_challenge_method": "S256", "code_challenge": challenge, "state": state}))
        end = time.time() + timeout
        while "code" not in got and "error" not in got and time.time() < end:
            srv.handle_request()
        srv.server_close()
        if "code" not in got:
            return f"Spotify sign-in didn't finish ({got.get('error', 'timed out')})."
        r = requests.post("https://accounts.spotify.com/api/token", timeout=15, data={
            "grant_type": "authorization_code", "code": got["code"], "redirect_uri": REDIRECT,
            "client_id": self.client_id, "code_verifier": verifier})
        if r.status_code != 200:
            return f"Spotify refused the sign-in: {r.text[:120]}"
        self._store(r.json())
        try:
            me = self._api("GET", "/me")
            prem = "" if me.get("product") == "premium" else " (it's not Premium, so I can show what's playing but not control it)"
            return f"Connected to Spotify as {me.get('display_name') or me.get('id')}{prem}."
        except Exception:
            return "Connected to Spotify."

    def _store(self, tok):
        self._token = tok["access_token"]
        self._expires = time.time() + int(tok.get("expires_in", 3600)) - 60
        if tok.get("refresh_token"):
            set_secret("spotify_refresh", tok["refresh_token"])

    def _access(self):
        with self._lock:
            if self._token and time.time() < self._expires:
                return self._token
            refresh = get_secret("spotify_refresh")
            if not refresh:
                raise RuntimeError("Spotify isn't connected (Settings > Accounts > Spotify > Connect).")
            r = requests.post("https://accounts.spotify.com/api/token", timeout=15, data={
                "grant_type": "refresh_token", "refresh_token": refresh, "client_id": self.client_id})
            if r.status_code != 200:
                raise RuntimeError("Spotify sign-in has expired: press Connect in Settings > Accounts again.")
            self._store(r.json())
            return self._token

    def _api(self, method, path, params=None, body=None):
        r = requests.request(method, API + path, params=params, json=body, timeout=12,
                             headers={"Authorization": f"Bearer {self._access()}"})
        if r.status_code == 204 or not r.content:
            return {}
        if r.status_code == 404 and "NO_ACTIVE_DEVICE" in r.text:
            raise NoDevice()
        if r.status_code == 403 and "PREMIUM" in r.text.upper():
            raise RuntimeError("Spotify only lets Premium accounts be controlled remotely.")
        if r.status_code >= 400:
            raise RuntimeError(f"Spotify said {r.status_code}: {r.text[:150]}")
        return r.json()

    # ---------- doing ----------
    def devices(self):
        return self._api("GET", "/me/player/devices").get("devices", [])

    def _pick_device(self, name=""):
        devs = self.devices()
        if not devs:
            return None, devs
        if name:
            n = name.lower()
            for d in devs:
                if n in d["name"].lower() or n in d["type"].lower():
                    return d, devs
        active = next((d for d in devs if d.get("is_active")), None)
        if active:
            return active, devs
        pref = next((d for d in devs if d["type"].lower() == "computer"), devs[0])
        return pref, devs

    def _with_device(self, fn, device=""):
        try:
            if device:
                raise NoDevice()
            return fn(None)
        except NoDevice:
            d, devs = self._pick_device(device)
            if not d:
                return ("Spotify isn't open on any device. Open the Spotify app on the PC or your phone (it can be "
                        "paused) and ask again.")
            return fn(d["id"])

    def now_playing(self) -> str:
        p = self._api("GET", "/me/player")
        if not p or not p.get("item"):
            return "Nothing's playing on Spotify."
        it = p["item"]
        who = ", ".join(a["name"] for a in it.get("artists", [])) or it.get("show", {}).get("name", "")
        dev = p.get("device", {})
        return (f"{'Playing' if p.get('is_playing') else 'Paused'}: {it['name']} by {who}"
                f" ({it.get('album', {}).get('name', '')}) on {dev.get('name', '?')}, volume {dev.get('volume_percent')}%."
                f" Shuffle {'on' if p.get('shuffle_state') else 'off'}.")

    def search(self, query, kind="track"):
        res = self._api("GET", "/search", {"q": query, "type": kind, "limit": 5, "market": "from_token"})
        return res.get(kind + "s", {}).get("items", [])

    def play(self, query="", kind="", device="") -> str:
        body, label = {}, ""
        if query:
            q = query.strip()
            if kind in ("", "auto"):
                low = q.lower()
                kind = ("playlist" if "playlist" in low or low.startswith(("my ", "liked")) else
                        "album" if " album" in low else "track")
            if kind == "liked" or query.lower().strip() in ("my liked songs", "liked songs", "my music"):
                tracks = self._api("GET", "/me/tracks", {"limit": 50}).get("items", [])
                if not tracks:
                    return "Your Liked Songs is empty."
                body, label = {"uris": [t["track"]["uri"] for t in tracks]}, "your Liked Songs"
            elif kind == "playlist":
                mine = self._api("GET", "/me/playlists", {"limit": 50}).get("items", [])
                term = q.lower().replace("playlist", "").replace("my ", "").strip()
                hit = next((p for p in mine if p and term and term in p["name"].lower()), None)
                if not hit:
                    found = self.search(term or q, "playlist")
                    hit = next((p for p in found if p), None)
                if not hit:
                    return f"I couldn't find a playlist called {q}."
                body, label = {"context_uri": hit["uri"]}, f"the playlist {hit['name']}"
            elif kind == "artist":
                found = self.search(q, "artist")
                if not found:
                    return f"I couldn't find {q} on Spotify."
                body, label = {"context_uri": found[0]["uri"]}, found[0]["name"]
            elif kind == "album":
                found = self.search(q.replace(" album", ""), "album")
                if not found:
                    return f"I couldn't find that album."
                a = found[0]
                body, label = {"context_uri": a["uri"]}, f"{a['name']} by {a['artists'][0]['name']}"
            else:
                found = self.search(q, "track")
                if not found:
                    found_a = self.search(q, "artist")
                    if found_a:
                        body, label = {"context_uri": found_a[0]["uri"]}, found_a[0]["name"]
                    else:
                        return f"I couldn't find {q} on Spotify."
                else:
                    t = found[0]
                    body, label = {"uris": [t["uri"]]}, f"{t['name']} by {t['artists'][0]['name']}"

        def go(dev):
            self._api("PUT", "/me/player/play", {"device_id": dev} if dev else None, body or None)
            return f"Playing {label}." if label else "Playing."
        return self._with_device(go, device)

    def control(self, action, value=None, device="") -> str:
        if action == "pause":
            return self._with_device(lambda d: self._api("PUT", "/me/player/pause", {"device_id": d} if d else None)
                                     or "Paused.")
        if action in ("next", "skip"):
            return self._with_device(lambda d: self._api("POST", "/me/player/next", {"device_id": d} if d else None)
                                     or "Skipped.")
        if action in ("previous", "back"):
            return self._with_device(lambda d: self._api("POST", "/me/player/previous", {"device_id": d} if d else None)
                                     or "Back one.")
        if action == "volume":
            v = max(0, min(100, int(float(value or 50))))
            return self._with_device(lambda d: self._api("PUT", "/me/player/volume",
                                                         {"volume_percent": v, **({"device_id": d} if d else {})})
                                     or f"Spotify volume {v}%.")
        if action == "shuffle":
            on = str(value).lower() not in ("off", "false", "0", "no")
            return self._with_device(lambda d: self._api("PUT", "/me/player/shuffle",
                                                         {"state": str(on).lower(), **({"device_id": d} if d else {})})
                                     or f"Shuffle {'on' if on else 'off'}.")
        if action == "queue":
            found = self.search(str(value or ""), "track")
            if not found:
                return f"I couldn't find {value}."
            t = found[0]
            return self._with_device(lambda d: self._api("POST", "/me/player/queue",
                                                         {"uri": t["uri"], **({"device_id": d} if d else {})})
                                     or f"Queued {t['name']} by {t['artists'][0]['name']}.")
        if action == "devices":
            devs = self.devices()
            return "\n".join(f"- {d['name']} ({d['type']}){' playing here' if d.get('is_active') else ''}"
                             for d in devs) or "Spotify isn't open on any device right now."
        if action == "move":
            d, devs = self._pick_device(device or str(value or ""))
            if not d or (device or value) and (device or str(value)).lower() not in (d["name"] + d["type"]).lower():
                names = ", ".join(x["name"] for x in devs) or "none open"
                return f"I can't see that device. Spotify's open on: {names}."
            self._api("PUT", "/me/player", body={"device_ids": [d["id"]], "play": True})
            return f"Moved it to {d['name']}."
        if action == "playlists":
            mine = self._api("GET", "/me/playlists", {"limit": 30}).get("items", [])
            return "Your playlists: " + ", ".join(p["name"] for p in mine if p) if mine else "No playlists."
        return f"I can't do '{action}' on Spotify."


class NoDevice(Exception):
    pass
