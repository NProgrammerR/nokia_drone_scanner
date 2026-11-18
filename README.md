# Nokia Drone Scanner

Team project for the **Nokia Student Network Challenge 2026**.

> Team name: _TBD_

## The challenge

### Nokia Defense challenge
How can we use technology to help society stay aware, resilient, and effective in a rapidly changing world?
Build a solution, concept, or prototype that helps people prepare, respond, and recover: understanding
what is happening, sharing critical information, and making faster, better-informed decisions.

### Nokia AI challenge
How can artificial intelligence improve productivity, sustainability, and future communications?

### Deliverables (both tracks)
- Identify a real problem and an innovative solution to address it
- Explain how the technology / AI is used and why it makes the solution better or possible
- Show the impact: who benefits, and what would improve?
- Bring the idea to life with a sketch, mock-up, prototype, user journey, or concept
- **5-minute pitch** to convince the jury why the idea should be taken forward
- A team name

## Our idea: Drone Scanner
Small drones are cheap, everywhere, and increasingly used over hospitals, power plants, public events and
critical infrastructure. Professional counter-drone systems cost tens of thousands of euros. **Drone Scanner**
is a network of cheap sensor nodes (~€20 each) that **detect drones by their radio signals, locate them,
intercept their video feed when possible, and alert operators the moment a drone enters a risk area.**

### How it works
| Layer | What | Cost |
|---|---|---|
| **Sensor node** | ESP32 sniffs Wi-Fi/Bluetooth **Remote ID** broadcasts (ASTM F3411, mandatory in the EU), which gives drone ID, GPS position, altitude and pilot position | ~€5 |
| | RX5808 5.8 GHz receiver + antenna scans for **analog FPV video**, which is detected even when the drone sends no Remote ID | ~€8 |
| | RX5808 video out → USB video grabber, so the feed shows up as a webcam in the console | ~€7 |
| **Network** | Nodes POST detections over Wi-Fi/4G/5G to the server; multiple nodes = RSSI-based position estimate | |
| **Server + AI** | Fuses detections, checks risk zones, scores threat level, pushes live alerts | |
| **Console** | Web GUI: live map, risk zones, drone list, threat details, intercepted video, alerts with sound and desktop notifications | |

**AI / analytics:** each drone gets a threat score from its behaviour: missing Remote ID, inside or approaching a
risk zone, speed, altitude over the 120 m limit. The prototype uses a transparent heuristic (`Hub._evaluate` in
`src/server.py`). The next step is a trained classifier on RF fingerprints and flight tracks to separate
hobby flights from loitering, reconnaissance or hostile behaviour.

## Run the prototype
No dependencies, Python 3.9+ only:
```bash
python3 src/server.py          # opens on http://localhost:8000 with simulated drones
```
Options: `--port 8080`, `--sim-drones 5`, `--no-sim` (only real sensors), `--host 0.0.0.0` (let sensor nodes on the LAN connect).

**Demo tips:** click a drone to see details and its threat reasoning. *Place on map* adds a new risk zone.
*Use capture device* switches the video panel to a real USB video grabber or webcam. *Snapshot* saves evidence frames.
Click anywhere once to enable alert sounds.

### Sensor node API
Sensor nodes (or `curl`) send detections to the server:
```bash
curl -X POST http://localhost:8000/api/detections -H 'Content-Type: application/json' -d '{
  "drone_id": "rid-1581F123", "sensor_id": "node-1", "label": "1581F123", "model": "Quadcopter",
  "protocol": "Wi-Fi Remote ID", "remote_id": true, "lat": 60.2270, "lon": 24.7460,
  "alt": 80, "speed": 6.5, "heading": 120, "rssi": -67, "freq_mhz": 2437,
  "operator": {"lat": 60.2250, "lon": 24.7440}
}'
```
Only `drone_id` is required. Also accepted: `accuracy_m`, `video`, `video_channel`, `video_note`. A JSON array sends several at once.
Other endpoints: `GET /api/state`, `GET /api/stream` (Server-Sent Events), `POST /api/zones`, `DELETE /api/zones/<id>`,
`POST /api/alerts/<id>/ack`, `POST /api/alerts/ack-all`.

### Impact
- **Who benefits:** hospitals, energy and telecom operators, event organisers, police and rescue services, airports.
- **What improves:** detection in seconds instead of someone spotting the drone; the pilot's location (from Remote ID) lets
  authorities act; the intercepted video shows what the drone is filming; cheap nodes make wide-area coverage affordable.

## Repo layout
| Folder | Contents |
|---|---|
| `docs/` | Problem research, concept notes, user journeys |
| `src/` | Prototype: `server.py` (backend + simulator), `web/` (console GUI) |
| `prototype/` | Mock-ups, sketches, demo assets |
| `pitch/` | Pitch deck and script |

## Team
- 
