# Nokia Drone Scanner

Team project for the **Nokia Student Network Challenge 2026** (Defense track, with an AI angle).

> Team name: _Kef10_

## The problem
Drones are cheap, everywhere, and increasingly a security threat: drone strikes in Haiti have killed
thousands of people, and unidentified drones over Finnish and European ports, airports and power plants
are now a regular occurrence. Professional counter-drone systems are expensive, separate security infrastructure.

## Our idea: Drone Scanner
Cheap **ESP32 scanner nodes** plus the **mobile network** feed an **AI risk agent** that weighs every signal and
tells security staff, within seconds, whether a drone is a threat to the site they protect. RED threats are sent
automatically to the Border Guard or police. The software runs on the site's **private 5G network**, so no
separate, expensive security infrastructure is needed.

**Customers:** private security companies protecting VIPs and critical infrastructure, for example the Turku dockyard
and nuclear power plants. **Go-to-market:** start with ports, then scale to other sea ports, airports and power plants.
**Partners:** mobile operators (Telia, DNA) for 5G connectivity and network-based drone detection.

### Threat levels
| Level | Rule (from our plan) | What happens |
|---|---|---|
| 🟢 **GREEN** | Remote ID valid, drone at the edge of the protected area, **or** an authorized flight in the permit system | Logged, no alarm |
| 🟡 **YELLOW** | **No Remote ID**, *or* fairly close to the protection target | Alert to security staff |
| 🔴 **RED** | **No Remote ID *and*** close to the protection target | Alarm, **automatic alert to Border Guard / police** |

Since 2024, drones in the EU must broadcast **Remote ID**, so a missing Remote ID is treated as high risk.
The risk agent can **raise a level by one step** when flight behaviour is suspicious (for example, a drone with Remote ID loitering inside the area).

### The AI risk agent
Every detection is scored from all available signals. The console shows the breakdown for each drone, so the
decision can be explained:

| Signal | Source | Example risk points |
|---|---|---|
| **Remote ID** | ESP32 sniffs Wi-Fi/Bluetooth Remote ID (ASTM F3411): drone ID, position, altitude, pilot position | missing +40 |
| **Radio frequencies** | RX5808 5.8 GHz receiver: analog FPV video/control links, RSSI-based location | unregistered RF +10 |
| **Mobile network** | 5G/6G-connected drones located by the operator network (partner data) | unknown 5G drone +10 |
| **Flight behaviour** | Track analysis: loitering, approaching the target, erratic path, >120 m, high speed | +5 to +10 each |
| **Proximity** | Distance to the protection target (perimeter and close rings per site) | close +30, in area +10 |
| **Permit system** | Port permit system: authorized flights (inspection, media) by Remote ID, time window and site | valid permit → GREEN, no alarm |

The prototype uses a transparent weighted model (`assess()` in `src/server.py`). The next step is to train it
on real RF fingerprints and flight tracks.

### Hardware (per scanner node, ~€20)
| Part | Purpose | Cost |
|---|---|---|
| ESP32 | Remote ID sniffing (Wi-Fi beacon + BLE), sends detections over the private 5G network | ~€5 |
| RX5808 + 5.8 GHz antenna | Detect analog FPV video (drones *without* Remote ID) | ~€8 |
| USB video grabber | Intercepted FPV video shows up as a webcam in the console | ~€7 |

Note: only **analog** FPV video can be viewed. Digital links (DJI etc.) are encrypted; the console says so.

## Run the prototype
No dependencies, Python 3.9+ only:
```bash
python3 src/server.py          # http://localhost:8000, simulated drones at every site
```
Options: `--sites my_sites.json`, `--permits my_permits.json`, `--sim-drones 5` (per site), `--no-sim`,
`--host 0.0.0.0` (let scanner nodes on the network connect), `--authority-log file.jsonl`,
`--authority-webhook URL` (receives RED alerts as JSON).

**Demo tips**
- Use the site tabs at the top of the map to switch between the Turku dockyard, Olkiluoto, Turku airport and the VIP visit.
- Click a drone to see the risk agent's breakdown (signal → points).
- The demo permits include a valid hull inspection, which stays GREEN, and an **expired** media flight (`1581F0000077`), which is not authorized.
- Add a permit or a protected site from the left panel. RED alerts show "→ Border Guard notified".
- Click anywhere once to enable alert sounds. *Desktop alerts* sends browser notifications for RED threats.
- **Video feed:** drop real drone footage into [`src/web/media/`](src/web/media/README.md). It plays with
  static and telemetry overlaid as an intercepted signal. *Use capture device* switches to a real RX5808 → USB grabber.

### Configuration
- `src/sites.json`: protected sites (`type`: port / airport / nuclear / energy / vip / other, `perimeter_m`,
  `close_m`, `authority`, `network`). Three scanner nodes are placed automatically around each site.
- `src/permits.json`: permit system export (`remote_id`, `site_id`, `holder`, `purpose`, `valid_from`, `valid_to`).

### API (for scanner nodes and integrations)
```bash
curl -X POST http://localhost:8000/api/detections -H 'Content-Type: application/json' -d '{
  "drone_id": "rid-1581F123", "sensor_id": "node-1", "label": "1581F123", "uas_id": "1581F123",
  "protocol": "Wi-Fi Remote ID", "remote_id": true, "lat": 60.4335, "lon": 22.1040,
  "alt": 80, "speed": 6.5, "heading": 120, "rssi": -67, "freq_mhz": 2437,
  "operator": {"lat": 60.4310, "lon": 22.1000}
}'
```
Only `drone_id` is required. Also accepted: `cellular`, `accuracy_m`, `via`, `video`, `video_channel`, `video_note`.
A JSON array sends several detections at once.

| Endpoint | |
|---|---|
| `GET /api/state`, `GET /api/stream` | snapshot / live Server-Sent Events |
| `POST /api/sites`, `DELETE /api/sites/<id>` | protected sites |
| `POST /api/permits`, `DELETE /api/permits/<id>` | permit system (`hours` or `valid_from`/`valid_to`) |
| `POST /api/alerts/<id>/ack`, `POST /api/alerts/ack-all` | acknowledge alerts |
| `GET /api/media` | video clips in `src/web/media/` |

## Why Nokia
- **Defense:** protects critical infrastructure and borders, with direct escalation to the Border Guard.
- **Private 5G:** runs on the private wireless networks Nokia already sells to ports, airports and industry,
  so the network becomes a security sensor and no separate system is needed.
- **AI:** a risk agent fuses network, RF and behaviour data into one explainable decision.
- **Operators:** network-based detection of 5G/6G-connected drones is a new service for Telia and DNA.

## Repo layout
| Folder | Contents |
|---|---|
| `docs/` | Challenge brief, research, concept notes |
| `src/` | Prototype: `server.py` (backend, risk agent, simulator), `sites.json`, `permits.json`, `web/` (console) |
| `prototype/` | Mock-ups, sketches, demo assets |
| `pitch/` | Pitch deck and script (5 min) |

## Team
-
