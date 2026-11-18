#!/usr/bin/env python3
"""Drone Scanner prototype server.

ESP32 scanner nodes (Remote ID + RF) and the mobile operator's 5G network feed
detections into this server, which runs on the site's private 5G network. A risk
agent weighs every signal (Remote ID, RF signature, mobile network, flight
behaviour, proximity, permits) into a risk score and a GREEN / YELLOW / RED level,
escalates RED threats to the authorities and streams everything live to the web
console via Server-Sent Events. A built-in simulator generates drone traffic
around every site so the console can be demoed without hardware.

Threat levels (from the team's plan):
  GREEN   Remote ID present and valid, drone at most at the edge of the protected area
  YELLOW  No Remote ID, OR fairly close to the protection target
  RED     No Remote ID AND close to the protection target -> alert to Border Guard / police
The risk agent can raise a level by one step when flight behaviour is suspicious.
Drones with a valid permit (Remote ID + time window + site) never raise alarms.
Since 2024 drones in the EU must broadcast Remote ID, so a missing Remote ID is high risk.

Standard library only:
    python3 src/server.py                       # simulator on, http://localhost:8000
    python3 src/server.py --no-sim --host 0.0.0.0
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
import urllib.request
from collections import deque
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

SRC_DIR = Path(__file__).resolve().parent
WEB_DIR = SRC_DIR / "web"
EARTH_R = 6_371_000.0
DRONE_TIMEOUT_S = 10.0
DOWNGRADE_HOLD_S = 5.0  # a level must stay lower this long before downgrading (avoids flapping)
TICK_S = 0.5
LEVEL_RANK = {"green": 0, "yellow": 1, "red": 2}
LEVELS = ["green", "yellow", "red"]
SITE_TYPES = {"port", "airport", "nuclear", "energy", "vip", "other"}
MEDIA_EXT = {".mp4", ".webm", ".ogv", ".mov"}


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


# ------------------------------------------------------------------ validation
DETECTION_FIELDS = {
    "sensor_id": str, "label": str, "uas_id": str, "model": str, "protocol": str, "source": str,
    "via": str, "remote_id": bool, "cellular": bool, "video": bool, "video_channel": str,
    "video_note": str, "lat": float, "lon": float, "alt": float, "speed": float, "heading": float,
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


def parse_site(body: dict, site_id: str | None = None) -> dict:
    try:
        lat, lon = float(body["lat"]), float(body["lon"])
        perimeter = float(body.get("perimeter_m", 1000))
        close = float(body.get("close_m", perimeter / 3))
    except (KeyError, TypeError, ValueError):
        raise ValueError("lat, lon, perimeter_m and close_m must be numbers")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("lat/lon out of range")
    if not (50 <= perimeter <= 50_000 and 10 <= close < perimeter):
        raise ValueError("need 50 <= perimeter_m <= 50000 and 10 <= close_m < perimeter_m")
    typ = body.get("type", "other")
    if typ not in SITE_TYPES:
        raise ValueError(f"type must be one of {sorted(SITE_TYPES)}")
    return {
        "id": site_id or str(body.get("id") or f"site-{random.getrandbits(32):08x}")[:64],
        "name": str(body.get("name") or "Protected site").strip()[:80],
        "type": typ, "lat": lat, "lon": lon,
        "perimeter_m": round(perimeter), "close_m": round(close),
        "authority": str(body.get("authority") or "Border Guard").strip()[:80],
        "network": str(body.get("network") or "Private 5G").strip()[:80],
    }


def _parse_time(v) -> str | None:
    if v in (None, ""):
        return None
    return datetime.fromisoformat(str(v)).isoformat(timespec="seconds")


def parse_permit(body: dict, permit_id: str | None = None) -> dict:
    rid = str(body.get("remote_id") or "").strip()
    if not rid:
        raise ValueError("permit needs the drone's 'remote_id'")
    try:
        if body.get("hours") not in (None, ""):
            start = datetime.now()
            valid_from, valid_to = start.isoformat(timespec="seconds"), \
                (start + timedelta(hours=float(body["hours"]))).isoformat(timespec="seconds")
        else:
            valid_from, valid_to = _parse_time(body.get("valid_from")), _parse_time(body.get("valid_to"))
    except (TypeError, ValueError):
        raise ValueError("valid_from/valid_to must be ISO datetimes, or give 'hours'")
    return {
        "id": permit_id or str(body.get("id") or f"P-{random.randint(10000, 99999)}")[:40],
        "remote_id": rid[:64], "site_id": str(body.get("site_id") or "").strip()[:64] or None,
        "holder": str(body.get("holder") or "Unknown holder").strip()[:80],
        "purpose": str(body.get("purpose") or "Authorized flight").strip()[:80],
        "valid_from": valid_from, "valid_to": valid_to,
    }


def sensors_for(site: dict) -> list[dict]:
    """Three ESP32 scanner nodes around a site, backhauled over the site's (private) 5G."""
    out = []
    for i, ang in enumerate((90, 210, 330), 1):
        r = site["perimeter_m"] * 0.55
        lat, lon = offset(site["lat"], site["lon"], math.cos(math.radians(ang)) * r,
                          math.sin(math.radians(ang)) * r)
        out.append({"id": f"{site['id']}-n{i}", "site_id": site["id"], "name": f"{site['name']} · node {i}",
                    "hw": "ESP32 Remote ID sniffer + RX5808 5.8 GHz", "backhaul": site["network"],
                    "lat": round(lat, 6), "lon": round(lon, 6),
                    "range_m": round(site["perimeter_m"] * 0.9)})
    return out


# ----------------------------------------------------------------- risk agent
def flight_behaviour(hist: deque, site: dict | None, now: float) -> dict:
    """Features from the recent track: loitering, approaching the target, erratic path."""
    out = {"loitering": False, "approaching": False, "erratic": False, "closing_mps": 0.0}
    pts = [h for h in hist if h[0] >= now - 30]
    if len(pts) >= 20 and pts[-1][0] - pts[0][0] >= 15:
        clat = sum(p[1] for p in pts) / len(pts)
        clon = sum(p[2] for p in pts) / len(pts)
        spread = max(dist_m(clat, clon, p[1], p[2]) for p in pts)
        path = sum(dist_m(a[1], a[2], b[1], b[2]) for a, b in zip(pts, pts[1:]))
        net = dist_m(pts[0][1], pts[0][2], pts[-1][1], pts[-1][2])
        # circling: stays in a small area while flying a much longer path than it covers
        out["loitering"] = spread < 150 and path > 60 and net < 0.35 * path
    recent = [h for h in hist if h[0] >= now - 10]
    if site and len(recent) >= 6:
        d0 = dist_m(recent[0][1], recent[0][2], site["lat"], site["lon"])
        d1 = dist_m(recent[-1][1], recent[-1][2], site["lat"], site["lon"])
        dt = max(1.0, recent[-1][0] - recent[0][0])
        out["closing_mps"] = round((d0 - d1) / dt, 1)
        out["approaching"] = d0 - d1 > 40 and not out["loitering"]
    heads = [h[3] for h in recent if h[3] is not None]
    if len(heads) >= 8:
        diffs = [((b - a + 540) % 360) - 180 for a, b in zip(heads, heads[1:])]
        flips = sum(1 for a, b in zip(diffs, diffs[1:]) if a * b < 0 and abs(a) > 5 and abs(b) > 5)
        out["erratic"] = sum(abs(x) for x in diffs) / len(diffs) > 15 and flips >= 3
    return out


def permit_status(d: dict, permits: list[dict], site: dict | None, region: str) -> dict | None:
    rid = d.get("uas_id") or d.get("label")
    if not d.get("remote_id") or not rid:
        return None
    matches = [p for p in permits if p["remote_id"] == rid]
    if not matches:
        return None
    now = datetime.now()
    best = None
    for p in matches:
        status = "valid"
        if p["valid_from"] and now < datetime.fromisoformat(p["valid_from"]):
            status = "not yet valid"
        elif p["valid_to"] and now > datetime.fromisoformat(p["valid_to"]):
            status = "expired"
        elif p["site_id"] and (site is None or p["site_id"] != site["id"] or region == "outside"):
            status = "outside permitted area"
        cand = {**p, "status": status}
        if status == "valid":
            return cand
        best = best or cand
    return best


def assess(d: dict, nearest: tuple | None, behaviour: dict, permit: dict | None) -> dict:
    """Risk agent: weigh every signal into a 0-100 score and a GREEN/YELLOW/RED level.

    Transparent weighted model so the jury (and operators) can see *why*. The weights
    are the baseline a trained model (RF fingerprints + flight tracks) would replace.
    """
    factors = []

    def add(signal, detail, points):
        factors.append({"signal": signal, "detail": detail, "points": points})

    rid = bool(d.get("remote_id"))
    if rid:
        add("Remote ID", f"Broadcasting {d.get('uas_id') or d.get('label')}", 0)
    else:
        add("Remote ID", "Missing: mandatory in the EU since 2024", 40)

    proto = d.get("protocol") or "unknown"
    if d.get("video"):
        add("RF signature", f"Analog FPV video/control link, {num(d.get('freq_mhz'))} MHz (unregistered)", 10)
    elif d.get("rssi") is not None:
        add("RF signature", f"{proto}, {num(d.get('rssi'))} dBm", 0)

    if d.get("cellular"):
        if rid:
            add("Mobile network", "5G-connected drone, identified by operator network", 0)
        else:
            add("Mobile network", "Unknown 5G-connected drone (operator network)", 10)

    region = nearest[2] if nearest else "outside"
    if nearest:
        site, dist = nearest[0], round(nearest[1])
        if region == "close":
            add("Proximity", f"Close to target: {dist} m from {site['name']}", 30)
        elif region == "area":
            add("Proximity", f"Inside protected area, {dist} m from target", 10)
        else:
            add("Proximity", f"Outside protected area, {dist} m from {site['name']}", 0)

    beh = []
    if behaviour["loitering"]:
        beh.append(("loitering", 10))
    if behaviour["approaching"]:
        beh.append((f"approaching target at {behaviour['closing_mps']} m/s", 10))
    if behaviour["erratic"]:
        beh.append(("erratic flight path", 10))
    if (d.get("alt") or 0) > 120:
        beh.append(("above 120 m limit", 5))
    if (d.get("speed") or 0) > 15:
        beh.append(("high speed", 5))
    if beh:
        for text, pts in beh:
            add("Flight behaviour", text.capitalize(), pts)
    else:
        add("Flight behaviour", "Normal", 0)

    authorized = bool(permit and permit["status"] == "valid")
    if permit:
        if authorized:
            add("Permit", f"{permit['id']}: {permit['purpose']} ({permit['holder']})", 0)
        else:
            add("Permit", f"{permit['id']} {permit['status']}: flight not authorized", 10)

    score = min(100, sum(f["points"] for f in factors))

    base = "red" if (not rid and region == "close") else "yellow" if (not rid or region == "close") else "green"
    level, raised = base, False
    if base == "green" and score >= 30:
        level, raised = "yellow", True
    elif base == "yellow" and score >= 85:
        level, raised = "red", True
    if authorized:
        level, score, raised = "green", min(score, 5), False

    if authorized:
        summary = f"Authorized flight ({permit['purpose']})"
    else:
        top = [f["detail"] for f in sorted(factors, key=lambda f: -f["points"]) if f["points"] > 0][:3]
        summary = "; ".join(top) if top else "Remote ID valid, no risk indicators"
    if raised:
        summary = "Risk agent raised level: " + summary
    return {"score": score, "level": level, "base_level": base, "raised": raised,
            "authorized": authorized, "factors": factors, "summary": summary}


def num(v, digits=0):
    return "?" if v is None else f"{float(v):.{digits}f}"


# ------------------------------------------------------------------------- hub
class Hub:
    """Shared state: sites, sensors, permits, drones, alerts, and SSE subscribers."""

    def __init__(self, sites, permits, simulation: bool, authority_log: Path, webhook: str | None):
        self.simulation = simulation
        self.authority_log = authority_log
        self.webhook = webhook
        self.lock = threading.Lock()
        self.subs: list[queue.Queue] = []
        self.sites: dict[str, dict] = {s["id"]: s for s in sites}
        self.sensors: dict[str, list[dict]] = {s["id"]: sensors_for(s) for s in sites}
        self.permits: dict[str, dict] = {p["id"]: p for p in permits}
        self.drones: dict[str, dict] = {}
        self.history: dict[str, deque] = {}
        self.alerts: list[dict] = []
        self.region: dict[tuple[str, str], str] = {}
        self.levels: dict[str, str] = {}
        self.down_since: dict[str, float] = {}
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

    def _all_sensors(self) -> list[dict]:
        return [n for nodes in self.sensors.values() for n in nodes]

    def snapshot(self) -> str:
        with self.lock:
            return json.dumps({
                "type": "snapshot", "simulation": self.simulation,
                "sites": list(self.sites.values()), "sensors": self._all_sensors(),
                "permits": list(self.permits.values()),
                "drones": list(self.drones.values()), "alerts": self.alerts[-100:],
            })

    def site_list(self) -> list[dict]:
        with self.lock:
            return [dict(s) for s in self.sites.values()]

    def sensor_list(self) -> list[dict]:
        with self.lock:
            return self._all_sensors()

    def permit_list(self) -> list[dict]:
        with self.lock:
            return [dict(p) for p in self.permits.values()]

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
                self.history.setdefault(did, deque(maxlen=120)).append(
                    (now, d["lat"], d["lon"], d.get("heading")))
            self.drones[did] = d
            new_alerts = self._evaluate(d, now)
        for a, site in new_alerts:
            if a["level"] == "red" and site:
                self.escalate(a, d, site)
            self.publish({"type": "alert", "alert": a})

    def _alert(self, now, level, kind, d, message, site=None) -> dict:
        a = {"id": next(self.seq), "ts": now, "level": level, "kind": kind,
             "drone_id": d["id"], "drone_label": d.get("label"), "message": message,
             "site_id": site["id"] if site else None, "site_name": site["name"] if site else None,
             "lat": d.get("lat"), "lon": d.get("lon"), "escalated_to": None, "acked": False}
        self.alerts.append(a)
        del self.alerts[:-300]
        return a

    def _evaluate(self, d: dict, now: float) -> list[tuple[dict, dict | None]]:
        out: list[tuple[dict, dict | None]] = []
        label = d.get("label", d["id"])
        nearest = None  # (site, distance, region) of the most relevant site
        if d.get("lat") is not None and d.get("lon") is not None:
            for s in self.sites.values():
                dist = dist_m(d["lat"], d["lon"], s["lat"], s["lon"])
                region = "close" if dist <= s["close_m"] else "area" if dist <= s["perimeter_m"] else "outside"
                if nearest is None or dist / s["perimeter_m"] < nearest[1] / nearest[0]["perimeter_m"]:
                    nearest = (s, dist, region)
        site = nearest[0] if nearest else None
        region = nearest[2] if nearest else "outside"
        behaviour = flight_behaviour(self.history.get(d["id"], deque()), site, now)
        permit = permit_status(d, list(self.permits.values()), site, region)
        risk = assess(d, nearest, behaviour, permit)

        # GREEN info alert when a compliant / permitted drone enters a protected area
        if site:
            key = (d["id"], site["id"])
            prev_region = self.region.get(key, "outside")
            self.region[key] = region
            if prev_region == "outside" and region != "outside" and risk["level"] == "green":
                msg = (f"GREEN: {label} authorized at {site['name']}: {permit['purpose']} ({permit['holder']})"
                       if risk["authorized"] else f"GREEN: {label} at the edge of {site['name']}, Remote ID valid")
                out.append((self._alert(now, "green", "permit" if risk["authorized"] else "edge", d, msg, site), site))

        level = risk["level"]
        prev = self.levels.get(d["id"])
        if prev and LEVEL_RANK[level] < LEVEL_RANK[prev] and not risk["authorized"]:
            since = self.down_since.setdefault(d["id"], now)
            if now - since < DOWNGRADE_HOLD_S:
                level = prev  # hold the higher level until the drone has clearly moved away
            else:
                self.down_since.pop(d["id"], None)
        else:
            self.down_since.pop(d["id"], None)
        self.levels[d["id"]] = level

        if prev != level and not (prev is None and level == "green"):
            if prev is not None and LEVEL_RANK[level] < LEVEL_RANK[prev]:
                out.append((self._alert(now, "info", "downgrade", d,
                                        f"{label} downgraded to {level.upper()}", site), site))
            elif level in ("yellow", "red"):
                out.append((self._alert(now, level, level, d,
                                        f"{level.upper()}: {label} near {site['name'] if site else 'unknown site'}: "
                                        f"{risk['summary']}", site), site))

        d.update(level=level, threat=risk["score"], factors=risk["factors"], summary=risk["summary"],
                 raised=risk["raised"], authorized=risk["authorized"], behaviour=behaviour,
                 permit=permit,
                 site=({"id": site["id"], "name": site["name"], "type": site["type"], "region": region,
                        "distance_m": round(nearest[1])} if site else None))
        return out

    def escalate(self, alert: dict, d: dict, site: dict):
        """Stub hook: forward a RED alert to the authorities responsible for the site.

        In production this would call the Border Guard / police incident API over the
        site's private 5G network (or an operator network slice). Here it appends to a
        JSONL log and optionally POSTs to a webhook.
        """
        authority = site.get("authority") or "Border Guard"
        with self.lock:
            alert["escalated_to"] = authority
        record = {"ts": alert["ts"], "to": authority, "site": site["name"], "alert_id": alert["id"],
                  "message": alert["message"], "risk_score": d.get("threat"),
                  "drone": {k: d.get(k) for k in ("id", "label", "model", "protocol", "lat", "lon",
                                                  "alt", "speed", "heading", "operator")}}
        print(f"[AUTHORITY ALERT → {authority}] {alert['message']}", flush=True)
        try:
            with self.authority_log.open("a") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as e:
            print(f"could not write authority log: {e}", flush=True)
        if self.webhook:
            def post():
                try:
                    req = urllib.request.Request(self.webhook, data=json.dumps(record).encode(),
                                                 headers={"Content-Type": "application/json"})
                    urllib.request.urlopen(req, timeout=5).close()
                except Exception as e:  # noqa: BLE001 - best-effort stub
                    print(f"authority webhook failed: {e}", flush=True)
            threading.Thread(target=post, daemon=True).start()

    def prune(self) -> str:
        cutoff = time.time() - DRONE_TIMEOUT_S
        with self.lock:
            for did in [k for k, d in self.drones.items() if d["last_seen"] < cutoff]:
                del self.drones[did]
                self.history.pop(did, None)
                self.region = {k: v for k, v in self.region.items() if k[0] != did}
                self.levels.pop(did, None)
                self.down_since.pop(did, None)
            return json.dumps({"type": "drones", "ts": time.time(), "drones": list(self.drones.values())})

    # -- sites / permits / alerts
    def add_site(self, site: dict):
        with self.lock:
            self.sites[site["id"]] = site
            self.sensors[site["id"]] = sensors_for(site)
        self._publish_config()

    def delete_site(self, sid: str) -> bool:
        with self.lock:
            if self.sites.pop(sid, None) is None:
                return False
            self.sensors.pop(sid, None)
            self.region = {k: v for k, v in self.region.items() if k[1] != sid}
        self._publish_config()
        return True

    def add_permit(self, permit: dict):
        with self.lock:
            self.permits[permit["id"]] = permit
        self._publish_config()

    def delete_permit(self, pid: str) -> bool:
        with self.lock:
            if self.permits.pop(pid, None) is None:
                return False
        self._publish_config()
        return True

    def _publish_config(self):
        with self.lock:
            msg = {"type": "config", "sites": list(self.sites.values()), "sensors": self._all_sensors(),
                   "permits": list(self.permits.values())}
        self.publish(msg)

    def ack(self, alert_id: int | None) -> int:
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
    {"key": "consumer", "model": "Consumer quadcopter", "remote_id": True, "speed": (6, 12), "alt": (40, 110),
     "protocol": "Wi-Fi Remote ID beacon (ASTM F3411)", "freq": 2437, "weight": 3,
     "video_note": "Digital link (encrypted), not viewable"},
    {"key": "fixedwing", "model": "Survey fixed-wing", "remote_id": True, "speed": (15, 20), "alt": (90, 135),
     "protocol": "Bluetooth Remote ID (ASTM F3411)", "freq": 2402, "weight": 1,
     "video_note": "No video downlink detected"},
    {"key": "fpv", "model": "FPV quad, no Remote ID", "remote_id": False, "speed": (12, 24), "alt": (15, 70),
     "protocol": "Analog FPV video, 5.8 GHz", "freq": None, "weight": 2, "video_note": None},
    {"key": "cellular", "model": "5G-connected drone", "remote_id": None, "speed": (10, 18), "alt": (50, 120),
     "protocol": "5G modem located by mobile operator network", "freq": None, "weight": 1,
     "video_note": "Video over 5G (not interceptable)"},
    {"key": "permitted", "model": "Inspection / media drone", "remote_id": True, "speed": (4, 8), "alt": (20, 80),
     "protocol": "Wi-Fi Remote ID beacon (ASTM F3411)", "freq": 2437, "weight": 1,
     "video_note": "Digital link (encrypted), not viewable"},
]
KIND = {k["key"]: k for k in KINDS}
FPV_CHANNELS = [("R1", 5658), ("R2", 5695), ("E1", 5705), ("F4", 5800), ("R8", 5917)]


class SimDrone:
    def __init__(self, kind: dict, site: dict, permit: dict | None = None):
        self.kind, self.site = kind, site
        self.id = f"sim-{random.getrandbits(24):06x}"
        P, C = site["perimeter_m"], site["close_m"]
        ang = random.uniform(0, 2 * math.pi)
        self.lat, self.lon = offset(site["lat"], site["lon"], math.cos(ang) * P * 1.05, math.sin(ang) * P * 1.05)
        self.remote_id = kind["remote_id"] if kind["remote_id"] is not None else random.random() < 0.5
        self.cellular = kind["key"] == "cellular"
        self.erratic = kind["key"] == "fpv" and random.random() < 0.5
        self.speed = random.uniform(*kind["speed"])
        self.cur_speed = self.speed
        self.alt = self.alt_target = random.uniform(*kind["alt"])
        self.loiter = 0.0
        self.done = False
        self.err = [0.0, 0.0]
        self.operator = None
        if permit:
            self.label = permit["remote_id"]
        elif self.remote_id:
            self.label = f"1581F{random.randint(10**7, 10**8 - 1)}"
        elif self.cellular:
            self.label = f"5G-UE-{random.randint(1000, 9999)}"
        else:
            self.chan, self.freq = random.choice(FPV_CHANNELS)
            self.label = f"FPV {self.chan}"
        if self.remote_id:
            self.operator = offset(self.lat, self.lon, random.uniform(-60, 60), random.uniform(-60, 60))
        # mostly fly into the protected area; about half also approach the target itself
        self.waypoints = []
        for _ in range(random.randint(1, 3)):
            r = random.uniform(C * 0.2, C * 0.8) if random.random() < 0.5 else random.uniform(C * 1.2, P * 0.9)
            a = random.uniform(0, 2 * math.pi)
            self.waypoints.append(offset(site["lat"], site["lon"], math.cos(a) * r, math.sin(a) * r))
        a = random.uniform(0, 2 * math.pi)
        self.waypoints.append(offset(site["lat"], site["lon"], math.cos(a) * P * 1.3, math.sin(a) * P * 1.3))
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
            if dist_m(self.lat, self.lon, tlat, tlon) < max(25, self.speed * dt * 2):
                self.waypoints.pop(0)
                self.loiter = random.choice([0, random.uniform(15, 30)])
                return
            want = bearing_deg(self.lat, self.lon, tlat, tlon)
            if self.erratic:
                want += random.choice([-1, 1]) * random.uniform(30, 70)
            diff = (want - self.heading + 540) % 360 - 180
            turn = 120 if self.erratic else 60
            self.heading = (self.heading + max(-turn * dt, min(turn * dt, diff))) % 360
            self.cur_speed = self.speed
        h = math.radians(self.heading)
        self.lat, self.lon = offset(self.lat, self.lon, math.cos(h) * self.cur_speed * dt,
                                    math.sin(h) * self.cur_speed * dt)
        if random.random() < 0.02:
            self.alt_target = random.uniform(*self.kind["alt"])
        self.alt += max(-2 * dt, min(2 * dt, self.alt_target - self.alt))

    def detection(self, sensors: list[dict]) -> dict | None:
        k = self.kind
        det = {"drone_id": self.id, "label": self.label, "model": k["model"], "source": "simulator",
               "remote_id": self.remote_id, "cellular": self.cellular, "alt": round(self.alt, 1),
               "speed": round(self.cur_speed, 1), "heading": round(self.heading), "video": False}
        if self.remote_id:
            det.update(uas_id=self.label,
                       operator={"lat": round(self.operator[0], 6), "lon": round(self.operator[1], 6)})
        if self.cellular:  # seen by the operator's 5G network, not by a scanner node
            self.err = [max(-120, min(120, e + random.gauss(0, 10))) for e in self.err]
            lat, lon = offset(self.lat, self.lon, *self.err)
            det.update(sensor_id="operator-5g", via="Mobile operator 5G network (partner data)",
                       protocol=k["protocol"] + (" + Remote ID" if self.remote_id else ""),
                       lat=round(lat, 6), lon=round(lon, 6), accuracy_m=150, video_note=k["video_note"])
            return det
        if not sensors:
            return None
        best = min(sensors, key=lambda s: dist_m(s["lat"], s["lon"], self.lat, self.lon))
        d = dist_m(best["lat"], best["lon"], self.lat, self.lon)
        if d > best["range_m"]:
            return None
        det.update(sensor_id=best["id"], via=f"ESP32 node over {best['backhaul']}", protocol=k["protocol"],
                   rssi=round(-38 - 25 * math.log10(max(d, 10) / 10) + random.gauss(0, 1.5)))
        if self.remote_id:
            det.update(lat=round(self.lat, 6), lon=round(self.lon, 6), freq_mhz=k["freq"],
                       video_note=k["video_note"])
        else:  # RF only: position estimated from multi-node RSSI, so it is noisy
            self.err = [max(-60, min(60, e + random.gauss(0, 6))) for e in self.err]
            lat, lon = offset(self.lat, self.lon, *self.err)
            det.update(lat=round(lat, 6), lon=round(lon, 6), accuracy_m=90, freq_mhz=self.freq,
                       video=True, video_channel=f"{self.chan} · {self.freq} MHz")
        return det


def spawn(site: dict, permits: list[dict], key: str | None = None) -> SimDrone:
    site_permits = [p for p in permits if p.get("site_id") == site["id"]]
    if key is None:
        choices = [k for k in KINDS if k["key"] != "permitted" or site_permits]
        key = random.choices(choices, [k["weight"] for k in choices])[0]["key"]
    if key == "permitted" and site_permits:
        return SimDrone(KIND["permitted"], site, random.choice(site_permits))
    return SimDrone(KIND["consumer" if key == "permitted" else key], site)


def run_simulator(hub: Hub, stop: threading.Event, per_site: int):
    drones: list[SimDrone] = []
    next_spawn: dict[str, float] = {}
    last = time.time()
    first = True
    while not stop.is_set():
        now = time.time()
        dt, last = now - last, now
        sites, permits = hub.site_list(), hub.permit_list()
        for sd in drones:
            sd.step(dt)
        drones = [sd for sd in drones if not sd.done]
        for i, s in enumerate(sites):
            count = sum(1 for sd in drones if sd.site["id"] == s["id"])
            if first:  # an FPV (no Remote ID), a compliant drone and a permitted flight at every site
                drones += [spawn(s, permits, "fpv"), spawn(s, permits, "consumer"), spawn(s, permits, "permitted")]
                next_spawn[s["id"]] = now + 10 + i * 3
            elif count < per_site and now >= next_spawn.get(s["id"], 0):
                drones.append(spawn(s, permits))
                next_spawn[s["id"]] = now + random.uniform(6, 15)
        first = False
        sensors = hub.sensor_list()
        for sd in drones:
            det = sd.detection(sensors)
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
        if path == "/api/media":
            media = WEB_DIR / "media"
            files = sorted(p.name for p in media.glob("*") if p.suffix.lower() in MEDIA_EXT) if media.is_dir() else []
            return self.send_json({"clips": [f"media/{quote(n)}" for n in files]})
        if path.startswith("/media/") and self.headers.get("Range"):
            return self.send_range()
        return super().do_GET()

    def send_range(self):
        """Byte-range responses so browsers can stream, seek and loop video clips."""
        fpath = Path(self.translate_path(self.path))
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
        if not fpath.is_file() or not m:
            return super().do_GET()
        size = fpath.stat().st_size
        if m.group(1):
            start, end = int(m.group(1)), int(m.group(2)) if m.group(2) else size - 1
        else:  # suffix range: last N bytes
            start, end = max(0, size - int(m.group(2) or 0)), size - 1
        end = min(end, size - 1)
        if start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(str(fpath)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        try:
            with fpath.open("rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self.read_json()
            if path == "/api/detections":
                dets = [clean_detection(x) for x in (body if isinstance(body, list) else [body])]
                for det in dets:
                    self.hub.ingest(det)
                return self.send_json({"accepted": len(dets)}, 202)
            if path == "/api/sites":
                site = parse_site(body, f"site-{random.getrandbits(32):08x}")
                self.hub.add_site(site)
                return self.send_json(site, 201)
            if path == "/api/permits":
                permit = parse_permit(body)
                self.hub.add_permit(permit)
                return self.send_json(permit, 201)
            if path == "/api/alerts/ack-all":
                return self.send_json({"acked": self.hub.ack(None)})
            m = re.fullmatch(r"/api/alerts/(\d+)/ack", path)
            if m:
                return self.send_json({"acked": self.hub.ack(int(m.group(1)))})
        except (ValueError, json.JSONDecodeError) as e:
            return self.send_json({"error": str(e)}, 400)
        self.send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        m = re.fullmatch(r"/api/(sites|permits)/([\w.-]+)", path)
        if m:
            ok = (self.hub.delete_site if m.group(1) == "sites" else self.hub.delete_permit)(m.group(2))
            if ok:
                return self.send_json({"deleted": m.group(2)})
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
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 so sensor nodes on the network can post")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--sites", type=Path, default=SRC_DIR / "sites.json", help="protected sites JSON file")
    ap.add_argument("--permits", type=Path, default=SRC_DIR / "permits.json",
                    help="flight permits JSON file (from the port's permit system)")
    ap.add_argument("--no-sim", action="store_true", help="disable simulated drones")
    ap.add_argument("--sim-drones", type=int, default=3, help="simulated drones per protected site")
    ap.add_argument("--authority-log", type=Path, default=Path("authority_alerts.jsonl"),
                    help="where escalated RED alerts are logged (stub for Border Guard / police API)")
    ap.add_argument("--authority-webhook", help="optional URL that receives escalated RED alerts as JSON")
    args = ap.parse_args()

    sites = [parse_site(s) for s in json.loads(args.sites.read_text())]
    permits = [parse_permit(p) for p in json.loads(args.permits.read_text())] if args.permits.exists() else []
    hub = Hub(sites, permits, simulation=not args.no_sim,
              authority_log=args.authority_log, webhook=args.authority_webhook)
    Handler.hub = hub
    stop = threading.Event()
    threading.Thread(target=run_broadcaster, args=(hub, stop), daemon=True).start()
    if not args.no_sim:
        threading.Thread(target=run_simulator, args=(hub, stop, args.sim_drones), daemon=True).start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    shown = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    print(f"Drone Scanner console: http://{shown}:{args.port}  ({len(sites)} protected sites, "
          f"{len(permits)} permits, simulation {'off' if args.no_sim else 'on'})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
