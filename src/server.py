#!/usr/bin/env python3
"""Drone Scanner prototype server.

Serves the web console, receives detections from sensor nodes (ESP32 etc.)
over HTTP, checks them against risk zones and pushes live updates and alerts
to browsers via Server-Sent Events. A built-in simulator generates realistic
drone traffic so the console can be demoed without hardware.

Standard library only:
    python3 src/server.py            # simulator on, http://localhost:8000
    python3 src/server.py --no-sim --host 0.0.0.0   # real sensors on the LAN
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import queue
import random
import re
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

WEB_DIR = Path(__file__).resolve().parent / "web"
EARTH_R = 6_371_000.0
CENTER = (60.2236, 24.7569)  # Espoo, Finland
DRONE_TIMEOUT_S = 10.0
TICK_S = 0.5


# --------------------------------------------------------------------------- geo
def dist_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(h))


def offset(lat, lon, north_m, east_m):
    return (
        lat + math.degrees(north_m / EARTH_R),
        lon + math.degrees(east_m / (EARTH_R * math.cos(math.radians(lat)))),
    )


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


# ------------------------------------------------------------------ demo config
def _sensor(sid, name, n, e):
    lat, lon = offset(*CENTER, n, e)
    return {"id": sid, "name": name, "hw": "ESP32 Remote ID sniffer + RX5808 5.8 GHz",
            "lat": round(lat, 6), "lon": round(lon, 6), "range_m": 1200}


def _zone(zid, name, n, e, r, level):
    lat, lon = offset(*CENTER, n, e)
    return {"id": zid, "name": name, "lat": round(lat, 6), "lon": round(lon, 6),
            "radius_m": r, "level": level}


SENSORS = [
    _sensor("node-1", "Sensor node 1", 0, 0),
    _sensor("node-2", "Sensor node 2", -650, -700),
    _sensor("node-3", "Sensor node 3", -450, 800),
]

DEFAULT_ZONES = [
    _zone("z-hospital", "Hospital (demo)", 420, -650, 250, "critical"),
    _zone("z-substation", "Power substation (demo)", -550, 600, 180, "critical"),
    _zone("z-school", "School (demo)", -350, -450, 200, "restricted"),
    _zone("z-event", "Public event (demo)", 450, 550, 300, "restricted"),
]


# ------------------------------------------------------------------------- hub
DETECTION_FIELDS = {
    "sensor_id": str, "label": str, "model": str, "protocol": str, "source": str,
    "remote_id": bool, "video": bool, "video_channel": str, "video_note": str,
    "lat": float, "lon": float, "alt": float, "speed": float, "heading": float,
    "rssi": float, "freq_mhz": float, "accuracy_m": float,
}


def clean_detection(raw: dict) -> dict:
    if not isinstance(raw, dict) or not str(raw.get("drone_id", "")).strip():
        raise ValueError("each detection needs a non-empty 'drone_id'")
    det = {"drone_id": str(raw["drone_id"]).strip()[:64]}
    for key, typ in DETECTION_FIELDS.items():
        if raw.get(key) is None:
            continue
        try:
            det[key] = typ(raw[key]) if typ is not str else str(raw[key])[:120]
        except (TypeError, ValueError):
            raise ValueError(f"field '{key}' has the wrong type")
    op = raw.get("operator")
    if isinstance(op, dict) and op.get("lat") is not None and op.get("lon") is not None:
        det["operator"] = {"lat": float(op["lat"]), "lon": float(op["lon"])}
    return det


def parse_zone(body: dict) -> dict:
    try:
        lat, lon = float(body["lat"]), float(body["lon"])
        radius = float(body.get("radius_m", 200))
    except (KeyError, TypeError, ValueError):
        raise ValueError("lat, lon and radius_m must be numbers")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180 and 10 <= radius <= 20000):
        raise ValueError("lat/lon/radius_m out of range")
    level = body.get("level", "restricted")
    if level not in ("critical", "restricted"):
        raise ValueError("level must be 'critical' or 'restricted'")
    name = str(body.get("name") or "Unnamed zone").strip()[:60]
    return {"id": f"z-{random.getrandbits(32):08x}", "name": name, "lat": lat, "lon": lon,
            "radius_m": round(radius), "level": level}


class Hub:
    """Shared state: drones, zones, alerts, and SSE subscribers."""

    def __init__(self, simulation: bool):
        self.simulation = simulation
        self.lock = threading.Lock()
        self.subs: list[queue.Queue] = []
        self.drones: dict[str, dict] = {}
        self.zones: dict[str, dict] = {z["id"]: dict(z) for z in DEFAULT_ZONES}
        self.alerts: list[dict] = []
        self.inside: set[tuple[str, str]] = set()
        self.prev_edge: dict[tuple[str, str], float] = {}
        self.announced: set[str] = set()
        self.seq = itertools.count(1)

    # -- pub/sub
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self.lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def publish(self, msg: dict | str):
        data = msg if isinstance(msg, str) else json.dumps(msg)
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass

    def snapshot(self) -> str:
        with self.lock:
            return json.dumps({
                "type": "snapshot", "simulation": self.simulation, "sensors": SENSORS,
                "zones": list(self.zones.values()), "drones": list(self.drones.values()),
                "alerts": self.alerts[-100:],
            })

    def zone_list(self) -> list[dict]:
        with self.lock:
            return [dict(z) for z in self.zones.values()]

    # -- detections
    def ingest(self, det: dict):
        now = time.time()
        did = det["drone_id"]
        with self.lock:
            d = self.drones.get(did) or {"id": did, "first_seen": now, "trail": [],
                                         "label": did, "model": "Unknown"}
            d.update({k: v for k, v in det.items() if k != "drone_id"})
            d["last_seen"] = now
            if d.get("lat") is not None and d.get("lon") is not None:
                t = d["trail"]
                if not t or dist_m(t[-1][0], t[-1][1], d["lat"], d["lon"]) > 4:
                    t.append([round(d["lat"], 6), round(d["lon"], 6)])
                    del t[:-60]
            self.drones[did] = d
            new_alerts = self._evaluate(d, now)
        for a in new_alerts:
            self.publish({"type": "alert", "alert": a})

    def _alert(self, now, severity, kind, d, message, zone=None) -> dict:
        a = {"id": next(self.seq), "ts": now, "severity": severity, "kind": kind,
             "drone_id": d["id"], "drone_label": d.get("label"), "message": message,
             "zone_id": zone["id"] if zone else None, "zone_name": zone["name"] if zone else None,
             "lat": d.get("lat"), "lon": d.get("lon"), "acked": False}
        self.alerts.append(a)
        del self.alerts[:-300]
        return a

    def _evaluate(self, d: dict, now: float) -> list[dict]:
        """Zone checks + heuristic threat score (placeholder for an ML classifier)."""
        out, reasons, score = [], [], 0
        label = d.get("label", d["id"])
        if not d.get("remote_id"):
            score += 35
            reasons.append("No Remote ID broadcast (non-compliant / hidden)")
            if d["id"] not in self.announced:
                self.announced.add(d["id"])
                out.append(self._alert(now, "warning", "unidentified", d,
                                       f"Unidentified drone signal: {label} ({d.get('protocol', 'RF')})"))
        zones_in, approaching = [], []
        if d.get("lat") is not None and d.get("lon") is not None:
            for z in self.zones.values():
                edge = dist_m(d["lat"], d["lon"], z["lat"], z["lon"]) - z["radius_m"]
                key = (d["id"], z["id"])
                prev = self.prev_edge.get(key)
                self.prev_edge[key] = edge
                if edge <= 0:
                    zones_in.append(z)
                    if key not in self.inside:
                        self.inside.add(key)
                        sev = "critical" if z["level"] == "critical" else "high"
                        out.append(self._alert(now, sev, "enter", d,
                                               f"{label} entered {z['level']} zone '{z['name']}'", z))
                else:
                    if key in self.inside:
                        self.inside.discard(key)
                        out.append(self._alert(now, "info", "exit", d,
                                               f"{label} left zone '{z['name']}'", z))
                    if edge < 250 and prev is not None and edge < prev - 0.5:
                        approaching.append(z)
        if zones_in:
            critical = any(z["level"] == "critical" for z in zones_in)
            score += 40 if critical else 25
            reasons.append("Inside " + ", ".join(z["name"] for z in zones_in))
        elif approaching:
            score += 15
            reasons.append("Approaching " + ", ".join(z["name"] for z in approaching))
        if (d.get("speed") or 0) > 15:
            score += 10
            reasons.append("High speed")
        if (d.get("alt") or 0) > 120:
            score += 10
            reasons.append("Above 120 m altitude limit")
        d["threat"] = min(100, score)
        d["reasons"] = reasons or ["Compliant flight, outside risk zones"]
        d["zones"] = [z["id"] for z in zones_in]
        return out

    def prune(self):
        cutoff = time.time() - DRONE_TIMEOUT_S
        with self.lock:
            for did in [k for k, d in self.drones.items() if d["last_seen"] < cutoff]:
                del self.drones[did]
                self.inside = {k for k in self.inside if k[0] != did}
                self.prev_edge = {k: v for k, v in self.prev_edge.items() if k[0] != did}
                self.announced.discard(did)
            return json.dumps({"type": "drones", "ts": time.time(),
                               "drones": list(self.drones.values())})

    # -- zones / alerts
    def add_zone(self, zone: dict):
        with self.lock:
            self.zones[zone["id"]] = zone
        self._publish_zones()

    def delete_zone(self, zid: str) -> bool:
        with self.lock:
            if self.zones.pop(zid, None) is None:
                return False
            self.inside = {k for k in self.inside if k[1] != zid}
            self.prev_edge = {k: v for k, v in self.prev_edge.items() if k[1] != zid}
        self._publish_zones()
        return True

    def _publish_zones(self):
        self.publish({"type": "zones", "zones": self.zone_list()})

    def ack(self, alert_id: int | None):
        changed = []
        with self.lock:
            for a in self.alerts:
                if not a["acked"] and (alert_id is None or a["id"] == alert_id):
                    a["acked"] = True
                    changed.append(dict(a))
        for a in changed:
            self.publish({"type": "alert_update", "alert": a})
        return len(changed)


# ------------------------------------------------------------------- simulator
KINDS = [
    {"model": "Consumer quadcopter", "remote_id": True, "speed": (5, 11), "alt": (40, 110),
     "protocol": "Wi-Fi Remote ID beacon (ASTM F3411)", "freq": 2437,
     "video_note": "Digital link (encrypted), not viewable"},
    {"model": "Survey fixed-wing", "remote_id": True, "speed": (14, 19), "alt": (90, 135),
     "protocol": "Bluetooth Remote ID (ASTM F3411)", "freq": 2402,
     "video_note": "No video downlink detected"},
    {"model": "FPV quad, no Remote ID", "remote_id": False, "speed": (9, 22), "alt": (15, 70),
     "protocol": "Analog FPV video, 5.8 GHz", "freq": None, "video_note": None},
]
FPV_CHANNELS = [("R1", 5658), ("R2", 5695), ("E1", 5705), ("F4", 5800), ("R8", 5917)]


class SimDrone:
    def __init__(self, kind: dict, zones: list[dict]):
        self.kind = kind
        self.id = f"sim-{random.getrandbits(24):06x}"
        ang = random.uniform(0, 2 * math.pi)
        self.lat, self.lon = offset(*CENTER, math.cos(ang) * 1100, math.sin(ang) * 1100)
        self.speed = random.uniform(*kind["speed"])
        self.cur_speed = self.speed
        self.alt = self.alt_target = random.uniform(*kind["alt"])
        self.loiter = 0.0
        self.done = False
        self.err = [0.0, 0.0]
        if kind["remote_id"]:
            self.label = f"1581F{random.randint(10**7, 10**8 - 1)}"
            self.operator = offset(self.lat, self.lon, random.uniform(-60, 60), random.uniform(-60, 60))
        else:
            self.chan, self.freq = random.choice(FPV_CHANNELS)
            self.label = f"FPV {self.chan}"
            self.operator = None
        self.waypoints = []
        for _ in range(random.randint(2, 4)):
            if zones and random.random() < 0.6:
                z = random.choice(zones)
                r = z["radius_m"] * 0.5
                self.waypoints.append(offset(z["lat"], z["lon"], random.uniform(-r, r), random.uniform(-r, r)))
            else:
                self.waypoints.append(offset(*CENTER, random.uniform(-900, 900), random.uniform(-900, 900)))
        out = random.uniform(0, 2 * math.pi)
        self.waypoints.append(offset(*CENTER, math.cos(out) * 1500, math.sin(out) * 1500))
        self.heading = bearing_deg(self.lat, self.lon, *self.waypoints[0])

    def step(self, dt: float):
        if not self.waypoints:
            self.done = True
            return
        if self.loiter > 0:
            self.loiter -= dt
            self.heading = (self.heading + 25 * dt) % 360
            self.cur_speed = self.speed * 0.4
        else:
            tlat, tlon = self.waypoints[0]
            if dist_m(self.lat, self.lon, tlat, tlon) < max(20, self.speed * dt * 2):
                self.waypoints.pop(0)
                self.loiter = random.choice([0, 0, random.uniform(6, 20)])
                return
            want = bearing_deg(self.lat, self.lon, tlat, tlon)
            diff = (want - self.heading + 540) % 360 - 180
            self.heading = (self.heading + max(-60 * dt, min(60 * dt, diff))) % 360
            self.cur_speed = self.speed
        h = math.radians(self.heading)
        self.lat, self.lon = offset(self.lat, self.lon, math.cos(h) * self.cur_speed * dt,
                                    math.sin(h) * self.cur_speed * dt)
        if random.random() < 0.02:
            self.alt_target = random.uniform(*self.kind["alt"])
        self.alt += max(-2 * dt, min(2 * dt, self.alt_target - self.alt))

    def detection(self) -> dict | None:
        best = min(SENSORS, key=lambda s: dist_m(s["lat"], s["lon"], self.lat, self.lon))
        d = dist_m(best["lat"], best["lon"], self.lat, self.lon)
        if d > best["range_m"]:
            return None
        k = self.kind
        det = {"drone_id": self.id, "sensor_id": best["id"], "label": self.label, "model": k["model"],
               "protocol": k["protocol"], "source": "simulator", "remote_id": k["remote_id"],
               "alt": round(self.alt, 1), "speed": round(self.cur_speed, 1),
               "heading": round(self.heading), "video": not k["remote_id"],
               "rssi": round(-38 - 25 * math.log10(max(d, 10) / 10) + random.gauss(0, 1.5))}
        if k["remote_id"]:
            det.update(lat=round(self.lat, 6), lon=round(self.lon, 6), freq_mhz=k["freq"],
                       video_note=k["video_note"],
                       operator={"lat": round(self.operator[0], 6), "lon": round(self.operator[1], 6)})
        else:  # RF-only: position estimated from multi-sensor RSSI, so it is noisy
            self.err = [max(-60, min(60, e + random.gauss(0, 6))) for e in self.err]
            lat, lon = offset(self.lat, self.lon, *self.err)
            det.update(lat=round(lat, 6), lon=round(lon, 6), accuracy_m=90, freq_mhz=self.freq,
                       video_channel=f"{self.chan} · {self.freq} MHz")
        return det


def run_simulator(hub: Hub, stop: threading.Event, target: int):
    zones = hub.zone_list()
    drones = [SimDrone(k, zones) for k in KINDS]
    next_spawn, last = time.time() + 8, time.time()
    while not stop.is_set():
        now = time.time()
        dt, last = now - last, now
        for sd in drones:
            sd.step(dt)
        before = len(drones)
        drones = [sd for sd in drones if not sd.done]
        if len(drones) < before and next_spawn < now:
            next_spawn = now + random.uniform(4, 12)
        if len(drones) < target and now >= next_spawn:
            drones.append(SimDrone(random.choices(KINDS, weights=[3, 1, 2])[0], hub.zone_list()))
            next_spawn = now + random.uniform(5, 15)
        for sd in drones:
            det = sd.detection()
            if det:
                hub.ingest(det)
        stop.wait(TICK_S)


def run_broadcaster(hub: Hub, stop: threading.Event):
    while not stop.is_set():
        hub.publish(hub.prune())
        stop.wait(TICK_S)


# ------------------------------------------------------------------------ http
class Handler(SimpleHTTPRequestHandler):
    hub: Hub

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/stream"):
            super().log_message(fmt, *args)

    def send_json(self, obj, status=200):
        body = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/stream":
            return self.stream()
        if path == "/api/state":
            return self.send_json(self.hub.snapshot())
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self.read_json()
            if path == "/api/detections":
                dets = [clean_detection(x) for x in (body if isinstance(body, list) else [body])]
                for det in dets:
                    self.hub.ingest(det)
                return self.send_json({"accepted": len(dets)}, 202)
            if path == "/api/zones":
                zone = parse_zone(body)
                self.hub.add_zone(zone)
                return self.send_json(zone, 201)
            if path == "/api/alerts/ack-all":
                return self.send_json({"acked": self.hub.ack(None)})
            m = re.fullmatch(r"/api/alerts/(\d+)/ack", path)
            if m:
                return self.send_json({"acked": self.hub.ack(int(m.group(1)))})
        except (ValueError, json.JSONDecodeError) as e:
            return self.send_json({"error": str(e)}, 400)
        self.send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        m = re.fullmatch(r"/api/zones/([\w-]+)", urlparse(self.path).path)
        if m and self.hub.delete_zone(m.group(1)):
            return self.send_json({"deleted": m.group(1)})
        self.send_json({"error": "not found"}, 404)

    def stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        q = self.hub.subscribe()
        try:
            self.wfile.write(f"data: {self.hub.snapshot()}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    self.wfile.write(f"data: {q.get(timeout=15)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.hub.unsubscribe(q)


def main():
    ap = argparse.ArgumentParser(description="Drone Scanner prototype server")
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 so sensor nodes on the LAN can post")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-sim", action="store_true", help="disable simulated drones")
    ap.add_argument("--sim-drones", type=int, default=3, help="number of simultaneous simulated drones")
    args = ap.parse_args()

    hub = Hub(simulation=not args.no_sim)
    Handler.hub = hub
    stop = threading.Event()
    threading.Thread(target=run_broadcaster, args=(hub, stop), daemon=True).start()
    if not args.no_sim:
        threading.Thread(target=run_simulator, args=(hub, stop, args.sim_drones), daemon=True).start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    shown = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    print(f"Drone Scanner console: http://{shown}:{args.port}  (simulation {'off' if args.no_sim else 'on'})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
