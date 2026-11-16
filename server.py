#!/usr/bin/env python3
"""
Race Back 2 Bath - spectator feed for Traccar.

Polls your Traccar server with a read-only account, works out start/finish
times from geofence events, and serves a public spectator page plus a cached
/data.json. Spectators never talk to Traccar directly, so your organiser
login stays private and Traccar only gets one request per poll however many
people are watching.

Standard library only (Python 3.9+). Configure with environment variables;
see config.example.env.
"""
import base64
import json
import math
import os
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- config ---

def env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default

TRACCAR_URL = env("TRACCAR_URL", "http://localhost:8082").rstrip("/")
TRACCAR_EMAIL = env("TRACCAR_EMAIL")
TRACCAR_PASSWORD = env("TRACCAR_PASSWORD")
TRACCAR_TOKEN = env("TRACCAR_TOKEN")

RACE_NAME = env("RACE_NAME", "Race Back 2 Bath")
RACE_START = env("RACE_START")                       # e.g. 2026-11-28T09:00:00+00:00
START_GEOFENCE = env("START_GEOFENCE", "Start")      # geofence name in Traccar
FINISH_GEOFENCE = env("FINISH_GEOFENCE", "Finish")   # geofence name in Traccar
FINISH_LAT = float(env("FINISH_LAT", "51.3782"))     # University of Bath, Claverton Down
FINISH_LNG = float(env("FINISH_LNG", "-2.3264"))

DELAY_MINUTES = float(env("DELAY_MINUTES", "0"))     # privacy delay for spectators
POLL_SECONDS = float(env("POLL_SECONDS", "15"))
STALE_MINUTES = float(env("STALE_MINUTES", "10"))    # "no signal" after this long
TRAIL_MIN_METRES = float(env("TRAIL_MIN_METRES", "150"))

HOST = env("HOST", "127.0.0.1")
PORT = int(env("PORT", "8090"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
OVERRIDES_FILE = env("OVERRIDES_FILE", os.path.join(BASE_DIR, "overrides.json"))

# ---------------------------------------------------------------- helpers ---

def parse_time(s):
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    # Traccar sometimes sends +0000 without a colon
    if len(s) > 5 and (s[-5] in "+-") and s[-3] != ":":
        s = s[:-2] + ":" + s[-2:]
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None

def haversine_m(lat1, lng1, lat2, lng2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))

def parse_area(area):
    """Parse Traccar geofence WKT (lat lon order). Returns dict or None."""
    if not area:
        return None
    a = area.strip().upper()
    try:
        if a.startswith("CIRCLE"):
            inner = area[area.index("(") + 1: area.rindex(")")]
            pt, radius = inner.split(",")
            lat, lng = [float(x) for x in pt.split()]
            return {"type": "circle", "lat": lat, "lng": lng, "radius": float(radius)}
        if a.startswith("POLYGON"):
            inner = area[area.index("((") + 2: area.rindex("))")]
            pts = [[float(c) for c in p.split()] for p in inner.split(",")]
            return {"type": "polygon", "points": pts}
    except (ValueError, IndexError):
        pass
    return None

def area_centre(geo):
    if not geo:
        return None
    if geo["type"] == "circle":
        return geo["lat"], geo["lng"]
    pts = geo["points"]
    return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)

# ---------------------------------------------------------------- traccar ---

class Traccar:
    def __init__(self):
        if TRACCAR_TOKEN:
            self.auth = "Bearer " + TRACCAR_TOKEN
        elif TRACCAR_EMAIL and TRACCAR_PASSWORD:
            raw = f"{TRACCAR_EMAIL}:{TRACCAR_PASSWORD}".encode()
            self.auth = "Basic " + base64.b64encode(raw).decode()
        else:
            sys.exit("Set TRACCAR_EMAIL and TRACCAR_PASSWORD (or TRACCAR_TOKEN).")

    def get(self, path, params=None):
        url = TRACCAR_URL + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        req = urllib.request.Request(url, headers={
            "Authorization": self.auth, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "null")

# ---------------------------------------------------------------- tracker ---

class Tracker:
    def __init__(self):
        self.api = Traccar()
        self.lock = threading.Lock()
        self.snapshot = json.dumps({"race": RACE_NAME, "ready": False}).encode()
        self.positions = {}       # deviceId -> {positionId: position}
        self.last_fetch_to = None
        self.geofences = []
        self.geofence_fetched = 0
        self.last_error = None

    def window_start(self, now):
        if RACE_START:
            return parse_time(RACE_START) - timedelta(hours=1)
        return now - timedelta(hours=24)

    def load_overrides(self):
        try:
            with open(OVERRIDES_FILE) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            print("overrides.json could not be read:", e, file=sys.stderr)
            return {}

    def poll(self):
        now = datetime.now(timezone.utc)
        devices = self.api.get("/api/devices") or []
        ids = [d["id"] for d in devices]

        if time.time() - self.geofence_fetched > 60:
            self.geofences = self.api.get("/api/geofences") or []
            self.geofence_fetched = time.time()

        events = []
        if ids:
            # Route history, fetched incrementally with a small overlap
            # Every 5 minutes re-read the whole race window, so positions a phone
            # buffered while out of signal and sent late are still picked up.
            frm = self.window_start(now)
            full = time.time() - getattr(self, "last_full", 0) > 300
            if self.last_fetch_to and not full:
                frm = max(frm, self.last_fetch_to - timedelta(minutes=10))
            if full:
                self.last_full = time.time()
            route = self.api.get("/api/reports/route", {
                "deviceId": ids, "from": iso(frm), "to": iso(now)}) or []
            for p in route:
                self.positions.setdefault(p["deviceId"], {})[p["id"]] = p
            self.last_fetch_to = now

            events = self.api.get("/api/reports/events", {
                "deviceId": ids, "type": ["geofenceEnter", "geofenceExit"],
                "from": iso(self.window_start(now)), "to": iso(now)}) or []

        data = self.build(now, devices, events)
        with self.lock:
            self.snapshot = json.dumps(data, separators=(",", ":")).encode()

    def build(self, now, devices, events):
        cutoff = now - timedelta(minutes=DELAY_MINUTES)
        race_start = parse_time(RACE_START) if RACE_START else None
        overrides = self.load_overrides()

        gf_by_name = {g.get("name", "").strip().lower(): g for g in self.geofences}
        start_gf = gf_by_name.get(START_GEOFENCE.strip().lower())
        finish_gf = gf_by_name.get(FINISH_GEOFENCE.strip().lower())
        finish_geo = parse_area(finish_gf["area"]) if finish_gf else None
        start_geo = parse_area(start_gf["area"]) if start_gf else None
        flat, flng = area_centre(finish_geo) or (FINISH_LAT, FINISH_LNG)

        # earliest relevant geofence events per device (only those visible after delay)
        starts, finishes = {}, {}
        for e in events:
            t = parse_time(e.get("eventTime"))
            if not t or t > cutoff:
                continue
            dev, gid = e.get("deviceId"), e.get("geofenceId")
            if start_gf and gid == start_gf["id"] and e["type"] == "geofenceExit":
                if race_start is None or t >= race_start - timedelta(minutes=5):
                    if dev not in starts or t < starts[dev]:
                        starts[dev] = t
            if finish_gf and gid == finish_gf["id"] and e["type"] == "geofenceEnter":
                finishes.setdefault(dev, []).append(t)

        pairs = []
        for d in devices:
            did = d["id"]
            ov = overrides.get(d.get("name")) or overrides.get(d.get("uniqueId")) or {}
            pts = sorted(
                (p for p in self.positions.get(did, {}).values()
                 if p.get("valid", True) and parse_time(p["fixTime"]) <= cutoff),
                key=lambda p: p["fixTime"])

            # trail, thinned so the page stays light
            trail, last = [], None
            for p in pts:
                ll = (round(p["latitude"], 5), round(p["longitude"], 5))
                if last is None or haversine_m(*last, *ll) >= TRAIL_MIN_METRES:
                    trail.append(list(ll)); last = ll
            if pts:
                end = (round(pts[-1]["latitude"], 5), round(pts[-1]["longitude"], 5))
                if not trail or trail[-1] != list(end):
                    trail.append(list(end))

            cur = pts[-1] if pts else None
            start = parse_time(ov["start"]) if ov.get("start") else starts.get(did, race_start)
            finish = None
            if ov.get("finish"):
                finish = parse_time(ov["finish"])
            else:
                cands = [t for t in finishes.get(did, []) if start is None or t > start]
                finish = min(cands) if cands else None

            status = (ov.get("status") or "").lower()
            if status not in ("withdrawn", "dnf"):
                status = "finished" if finish else ("racing" if cur else "waiting")

            # "Last heard from": the newer of the last position fix and the last time
            # Traccar heard from the phone at all. A phone sitting still may keep
            # reporting without Traccar storing new positions (duplicate filtering).
            last_seen = parse_time(cur["fixTime"]) if cur else None
            heard = parse_time(d.get("lastUpdate")) if d.get("lastUpdate") else None
            if heard and cur:
                heard = min(heard, cutoff)   # respect the privacy delay
                if last_seen is None or heard > last_seen:
                    last_seen = heard
            pair = {
                "id": did,
                "name": d.get("name") or d.get("uniqueId"),
                "status": status,
                "note": ov.get("note"),
                "lat": cur["latitude"] if cur else None,
                "lng": cur["longitude"] if cur else None,
                "speedMph": round((cur.get("speed") or 0) * 1.15078, 1) if cur else None,
                "lastSeen": iso(last_seen),
                "stale": bool(last_seen and (cutoff - last_seen) > timedelta(minutes=STALE_MINUTES)),
                "milesToGo": round(haversine_m(cur["latitude"], cur["longitude"], flat, flng) / 1609.344, 1) if cur else None,
                "start": iso(start),
                "finish": iso(finish),
                "elapsedSeconds": int((finish - start).total_seconds()) if (finish and start) else None,
                "trail": trail,
            }
            pairs.append(pair)

        return {
            "race": RACE_NAME,
            "ready": True,
            "generated": iso(now),
            "asOf": iso(cutoff),
            "delayMinutes": DELAY_MINUTES,
            "raceStart": iso(race_start),
            "finish": {"lat": flat, "lng": flng, "area": finish_geo},
            "startArea": start_geo,
            "pairs": pairs,
            "warnings": [w for w in [
                None if finish_gf else f'No geofence named "{FINISH_GEOFENCE}" found - finish times will not be detected.',
            ] if w],
        }

    def run(self):
        while True:
            try:
                self.poll()
                if self.last_error:
                    print("Traccar connection OK again", file=sys.stderr)
                self.last_error = None
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                if msg != self.last_error:
                    traceback.print_exc()
                self.last_error = msg
            time.sleep(POLL_SECONDS)

# ---------------------------------------------------------------- web ---

TRACKER = None

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC_DIR, **kw)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.endswith("/data.json") or path == "/data.json":
            with TRACKER.lock:
                body = TRACKER.snapshot
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "public, max-age=10")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/healthz":
            ok = TRACKER.last_error is None
            body = (b"ok" if ok else TRACKER.last_error.encode())
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()

    def log_message(self, fmt, *args):
        pass  # keep logs quiet; errors from polling are still printed

def main():
    global TRACKER
    TRACKER = Tracker()
    threading.Thread(target=TRACKER.run, daemon=True).start()
    print(f"{RACE_NAME} spectator site on http://{HOST}:{PORT}  (Traccar: {TRACCAR_URL})")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()

if __name__ == "__main__":
    main()
