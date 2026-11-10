"""Your Tesla, through Tesla's Fleet API.

Works with a Teslemetry token (simplest: teslemetry.com, about $30 a year, commands included)
or any Fleet-API-compatible service; set the address in Settings > Accounts.
Anything that opens the car (unlocking, boot, frunk, windows, remote start) goes through the
approval queue first, so a misheard command can't open your car.
"""
import json
import logging
import math
import threading
import time
from datetime import datetime

import requests

from .config import DATA_DIR, get_secret, read_json, write_json

log = logging.getLogger("moo")
SCHEDULE_FILE = DATA_DIR / "car_schedule.json"
DEFAULT_BASE = "https://api.teslemetry.com"

# what you can ask for -> (Fleet API command, extra body, needs your OK first, what to say)
COMMANDS = {
    "climate_on": ("auto_conditioning_start", {}, False, "Climate's on, warming up the car."),
    "climate_off": ("auto_conditioning_stop", {}, False, "Climate's off."),
    "lock": ("door_lock", {}, False, "Locked."),
    "unlock": ("door_unlock", {}, True, "Unlocked."),
    "charge_start": ("charge_start", {}, False, "Charging started."),
    "charge_stop": ("charge_stop", {}, False, "Charging stopped."),
    "charge_port_open": ("charge_port_door_open", {}, False, "Charge port's open."),
    "charge_port_close": ("charge_port_door_close", {}, False, "Charge port's closed."),
    "honk": ("honk_horn", {}, False, "Beep beep."),
    "flash": ("flash_lights", {}, False, "Lights flashed."),
    "sentry_on": ("set_sentry_mode", {"on": True}, False, "Sentry mode's on."),
    "sentry_off": ("set_sentry_mode", {"on": False}, False, "Sentry mode's off."),
    "open_boot": ("actuate_trunk", {"which_trunk": "rear"}, True, "Boot's open."),
    "open_frunk": ("actuate_trunk", {"which_trunk": "front"}, True, "Frunk's open."),
    "vent_windows": ("window_control", {"command": "vent", "lat": 0, "lon": 0}, True, "Windows vented."),
    "defrost_on": ("set_preconditioning_max", {"on": True}, False, "Max defrost's on."),
    "defrost_off": ("set_preconditioning_max", {"on": False}, False, "Max defrost's off."),
}


def _km(a, b):
    (la1, lo1), (la2, lo2) = a, b
    p = math.pi / 180
    h = (math.sin((la2 - la1) * p / 2) ** 2 + math.cos(la1 * p) * math.cos(la2 * p) *
         math.sin((lo2 - lo1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(h))


class Car:
    def __init__(self, settings):
        self.settings = settings
        self._vin = None
        self._lock = threading.Lock()

    # ---------- plumbing ----------
    @property
    def enabled(self) -> bool:
        return bool(get_secret("tesla_token"))

    def _base(self):
        return (self.settings.get("tesla_api_base") or DEFAULT_BASE).rstrip("/")

    def _req(self, method, path, body=None, timeout=30):
        r = requests.request(method, f"{self._base()}{path}", json=body, timeout=timeout,
                             headers={"Authorization": f"Bearer {get_secret('tesla_token')}"})
        if r.status_code == 408:
            raise Asleep()
        if r.status_code in (401, 403):
            raise RuntimeError("Tesla access was refused: check the token in Settings > Accounts "
                               "(and that the virtual key is installed for commands).")
        if r.status_code >= 400:
            raise RuntimeError(f"Tesla said {r.status_code}: {r.text[:160]}")
        return r.json().get("response")

    def vin(self):
        if self._vin:
            return self._vin
        want = (self.settings.get("tesla_vin") or "").strip()
        cars = self._req("GET", "/api/1/vehicles") or []
        if not cars:
            raise RuntimeError("No cars found on that Tesla account.")
        car = next((c for c in cars if c.get("vin") == want), cars[0])
        self._vin = car["vin"]
        return self._vin

    # ---------- reading ----------
    def status(self, wake=False) -> str:
        if not self.enabled:
            return "The Tesla isn't connected yet (Settings > Accounts > Tesla)."
        vin = self.vin()
        path = (f"/api/1/vehicles/{vin}/vehicle_data?endpoints="
                "charge_state%3Bclimate_state%3Bdrive_state%3Bvehicle_state%3Blocation_data")
        try:
            d = self._req("GET", path)
        except Asleep:
            if not wake:
                return ("The car's asleep, so I can't read it without waking it (that uses a little of the "
                        "monthly allowance). Ask me to wake it if you need live figures.")
            self._req("POST", f"/api/1/vehicles/{vin}/wake_up")
            for _ in range(10):
                time.sleep(3)
                try:
                    d = self._req("GET", path)
                    break
                except Asleep:
                    continue
            else:
                return "I tried to wake the car but it didn't answer. It may have no signal."
        return self._describe(d)

    def _describe(self, d) -> str:
        c, cl = d.get("charge_state") or {}, d.get("climate_state") or {}
        dr, vs = d.get("drive_state") or {}, d.get("vehicle_state") or {}
        lines = [f"Car: {vs.get('vehicle_name') or 'Tesla'}"]
        if c:
            rng = c.get("battery_range")
            miles = f", about {int(rng)} miles" if rng else ""
            lines.append(f"Battery {c.get('battery_level')}%{miles} (limit {c.get('charge_limit_soc')}%). "
                         f"Charging: {c.get('charging_state')}"
                         + (f", {c.get('minutes_to_full_charge')} min to the limit"
                            if c.get("charging_state") == "Charging" else ""))
        if cl:
            lines.append(f"Inside {cl.get('inside_temp')}°C, outside {cl.get('outside_temp')}°C, "
                         f"climate {'on' if cl.get('is_climate_on') else 'off'}.")
        if vs:
            doors_open = any(vs.get(k) for k in ("df", "dr", "pf", "pr"))
            boot = "boot open" if vs.get("rt") else ""
            lines.append(f"{'Locked' if vs.get('locked') else 'UNLOCKED'}"
                         + (", a door is open" if doors_open else "") + (f", {boot}" if boot else "")
                         + f", sentry {'on' if vs.get('sentry_mode') else 'off'}. "
                         f"Odometer {int(vs.get('odometer') or 0)} miles.")
        lat, lon = dr.get("latitude"), dr.get("longitude")
        if lat and lon:
            where = f"at {lat:.5f}, {lon:.5f}"
            try:
                from .extras import geocode
                if self.settings.get("home_location"):
                    hlat, hlon, _ = geocode(self.settings["home_location"])
                    km = _km((lat, lon), (hlat, hlon))
                    where = "at home" if km < 0.25 else f"{km * 0.621:.1f} miles from home"
            except Exception:
                pass
            moving = dr.get("shift_state") in ("D", "R") or (dr.get("speed") or 0) > 0
            lines.append(f"Location: {where}" + (f", driving at {dr.get('speed')} mph" if moving else ", parked")
                         + f". Map: https://maps.google.com/?q={lat},{lon}")
        return "\n".join(lines)

    # ---------- doing ----------
    def command(self, action, value=None) -> str:
        if not self.enabled:
            return "The Tesla isn't connected yet (Settings > Accounts > Tesla)."
        if action == "set_temp":
            t = float(value or 21)
            name, body, msg = "set_temps", {"driver_temp": t, "passenger_temp": t}, f"Set to {t:g} degrees."
        elif action == "charge_limit":
            pct = max(50, min(100, int(value or 80)))
            name, body, msg = "set_charge_limit", {"percent": pct}, f"Charge limit set to {pct}%."
        elif action in COMMANDS:
            name, body, _, msg = COMMANDS[action]
        else:
            return f"I can't do '{action}' on the car."
        out = self._req("POST", f"/api/1/vehicles/{self.vin()}/command/{name}", body or {}, timeout=60)
        if isinstance(out, dict) and out.get("result") is False:
            return f"The car said no: {out.get('reason') or 'no reason given'}."
        return msg

    @staticmethod
    def needs_ok(action) -> bool:
        return COMMANDS.get(action, (None, None, False))[2]

    # ---------- scheduled ("warm the car at 7:30") ----------
    def schedule(self, action, when: str, value=None) -> str:
        try:
            due = datetime.fromisoformat(when.replace("Z", ""))
        except ValueError:
            return "I need a time like 2026-11-11T07:30."
        items = read_json(SCHEDULE_FILE, [])
        items.append({"action": action, "value": value, "due": due.strftime("%Y-%m-%d %H:%M")})
        write_json(SCHEDULE_FILE, items)
        return f"Booked: {action.replace('_', ' ')} at {due.strftime('%H:%M on %a %d %b')}."

    def due(self):
        """Run anything scheduled for now; returns [(action, result)]."""
        items = read_json(SCHEDULE_FILE, [])
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        ran, keep = [], []
        for it in items:
            if it["due"] <= now:
                try:
                    ran.append((it["action"], self.command(it["action"], it.get("value"))))
                except Exception as e:
                    ran.append((it["action"], f"didn't work: {str(e)[:100]}"))
            else:
                keep.append(it)
        if ran:
            write_json(SCHEDULE_FILE, keep)
        return ran


class Asleep(Exception):
    pass
