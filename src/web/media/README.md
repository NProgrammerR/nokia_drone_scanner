# Drone footage for the video panel

Put one or more video clips here (`.mp4`, `.webm`, `.ogv` or `.mov`). The console lists them via
`/api/media` and, when a drone with an analog video link is tuned, plays a clip in the
"Video feed" panel. Static, signal tearing and the pilot's on-screen telemetry (channel, RSSI,
altitude, speed) are drawn on top so it still looks like an intercepted FPV signal. Each drone always
gets the same clip. If this folder has no clips, the console falls back to the drawn (synthetic) view.

**Use footage you are allowed to use:**
- your own FPV / drone recordings (best: real and no licence questions), or
- clips with a clear licence that allows reuse, e.g. CC0 / Pexels / Pixabay licence. Note the source
  and licence in `CREDITS.md` in this folder.

Tips: 640×360 or 1280×720 H.264 MP4 is plenty. Keep clips small (< 20 MB) so they fit in the repo:
`ffmpeg -i in.mp4 -vf scale=1280:-2 -c:v libx264 -crf 28 -an -t 60 fpv1.mp4`
