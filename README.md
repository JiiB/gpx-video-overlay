# GPX video overlay

Two tools for burning a heart-rate / power / w/kg / elevation / gradient badge
from a GPX file onto a ride video:

- **`ride_overlay.py`** — the CLI that actually renders the overlaid video.
- **`index.html`** — a browser page (no server, no install) for
  interactively finding the right sync offset before you render, by playing
  the video with the same badge drawn live on a canvas on top of it.

This repo is set up to be served directly as a static site (e.g. GitHub
Pages) — `index.html` at the root is the whole preview tool, no build step.
A local `samples/` folder (git-ignored) is just a scratch space for your own
ride videos/GPX files while testing; nothing in it is required to use either
tool.

## Requirements

- `ffmpeg` / `ffprobe` on your `PATH` (e.g. `brew install ffmpeg`)
- Python 3 with:
  ```
  pip install gpxpy pillow
  ```
- `index.html` needs nothing at all — just open it in a browser.

## Quick start

### 1. Find the sync offset

You need to tell the tool what real-world clock time some point in the video
corresponds to. Two ways to find that:

- **Visually**: open `index.html` in a browser, load your video and
  GPX file, and use the anchor/nudge/calibrate controls until the live badge
  matches what you know about the ride (see below).
- **From memory**: if you remember roughly when in the ride the video was
  shot (e.g. off your bike computer's clock), use that directly.

### 2. Render with a rough sync guess

```
python3 ride_overlay.py --video ride.mov --gpx ride.gpx \
    --sync "0:36=2026-08-22T13:56:20+02:00" \
    --out ride_overlay.mp4
```

`--sync VIDEO_TIME=REAL_TIME` means "this point in the video is this
real-world clock time" — `VIDEO_TIME` accepts seconds, `M:SS`, or `H:MM:SS`;
`REAL_TIME` is an ISO-8601 timestamp, ideally with a timezone offset.

### 3. Check it before committing to a full render

```
python3 ride_overlay.py --video ride.mov --gpx ride.gpx \
    --sync "0:36=2026-08-22T13:56:20+02:00" \
    --probe "0:50,2:58,3:16"
```

Prints the telemetry (power, HR, elevation, gradient) at those video
timestamps without rendering anything, so you can compare against what you
know actually happened at those moments.

### 4. Fine-tune automatically from known power readings (optional)

If you filmed your cycling computer's screen at a few moments and can read
the exact 3-second-average power off it (or know it from a head unit/Strava),
the tool can search for the offset that best matches those readings — useful
for correcting small clock drift the rough sync above can't catch:

```
python3 ride_overlay.py --video ride.mov --gpx ride.gpx \
    --sync "0:36=2026-08-22T13:56:20+02:00" \
    --calibrate-power "0:50=369,2:58=384,3:16=414" \
    --out ride_overlay.mp4
```

It searches ± `--calibrate-window` seconds (default 30) around your rough
sync for the integer-second shift that best matches the given readings, and
uses that instead.

### 5. Nudge manually if needed

`--nudge SECONDS` applies one last manual adjustment on top of everything
else (positive = pull in telemetry from later in the ride).

## `index.html`

Open the file directly in a browser (double-click it, or drag it into a tab
— no server needed). It's a preview/calibration tool only — it does **not**
export a video. Everything works fully offline except the route map, which
loads Leaflet + OpenStreetMap tiles over the network. If the map tiles don't
load when opening the file directly (`file://`), it's because OpenStreetMap's
tile servers reject requests with no `Referer` header, which is what a
`file://` page sends — serve the folder over local HTTP instead
(`python3 -m http.server` in this folder, then open
`http://localhost:8000/index.html`) and it'll work. Workflow:

1. Load your video file and your GPX file. A route map and a whole-ride power
   chart appear once the GPX loads.
2. Position the video wherever you like with its own player controls (pause
   on a frame you recognize), then find the matching moment in the GPX file
   by dragging the **GPX time** slider, clicking a point on the **map**, or
   clicking/dragging on the **power chart** — all three move the same
   position and redraw the badge on top of the video live. Once it matches
   the frame, hit **Set anchor**. This is the same idea as `--sync` above,
   just found visually instead of typed in.
   - The power chart supports zooming for finer selection: scroll to zoom in
     around the cursor, shift+drag to pan, double-click to reset.
3. Optionally expand **"3. Auto-calibrate from known power"** (collapsed by
   default), enter a few (video time, known watts) pairs — e.g. read off your
   cycling computer in the footage — and hit **Auto-calibrate** to fine-tune
   the offset the same way `--calibrate-power` does.
4. Scrub/play the video and watch the badge track the footage. Use the
   **nudge** control for any last manual adjustment.
5. Under **Badge options**, pick which metrics to show and where. To include
   **W/kg**, check it and enter your weight in kilos in the field that
   appears next to it — it's computed live from the power value, so no
   separate telemetry is needed.
6. Hit **Copy CLI command** to copy a ready-to-run `ride_overlay.py` command
   with the resolved sync (and `--weight-kg` if W/kg is enabled) baked in,
   and run that to produce the actual overlaid video file.

## Flag reference

Run `python3 ride_overlay.py --help` for the full, current list. Summary:

| Flag | Purpose |
|---|---|
| `--video PATH` | input video (required) |
| `--gpx PATH` | GPX telemetry source (required) |
| `--out PATH` | output video (default: `<video>_overlay.mp4`) |
| `--sync VIDEO_TIME=REAL_TIME` | rough sync anchor |
| `--gpx-start ISO_TIMESTAMP` | exact UTC time in the GPX that video 0:00 corresponds to (alternative to `--sync`) |
| `--calibrate-power VIDEO_TIME=WATTS[,...]` | auto fine-tune sync from known power readings |
| `--calibrate-window N` | search radius in seconds for `--calibrate-power` (default 30) |
| `--nudge SECONDS` | manual final adjustment |
| `--probe VIDEO_TIME[,...]` | print telemetry at given times, no render |
| `--metrics hr,power,wkg,elevation,gradient` | which badge items to show, and in what order (default: `hr,power,elevation,gradient`) |
| `--weight-kg N` | rider weight in kg, required if `wkg` (watts/kg) is in `--metrics` |
| `--position bottom-left\|bottom-right\|top-left\|top-right` | badge corner |
| `--smooth-window N` | power trailing-average window, seconds (default 3) |
| `--gradient-window N` | gradient smoothing half-window, seconds (default 9) |
| `--start` / `--end` | render only a trimmed portion of the video |
| `--crf` / `--preset` | libx264 encode quality/speed |
| `--keep-temp` | keep the generated overlay PNG frames for debugging |
