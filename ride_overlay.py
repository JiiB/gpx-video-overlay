#!/usr/bin/env python3
"""
ride_overlay.py - burn a heart-rate / power / elevation / gradient badge
from a GPX file onto a ride video.

Quick start
-----------
1) Render with a rough sync guess (you tell it what real-world clock time
   a given point in the video corresponds to):

     ./ride_overlay.py --video ride.mov --gpx ride.gpx \\
         --sync "0:36=2026-08-22T13:56:20+02:00" \\
         --out ride_overlay.mp4

2) Not sure the sync is exact? Probe a few timestamps first, no rendering:

     ./ride_overlay.py --video ride.mov --gpx ride.gpx \\
         --sync "0:36=2026-08-22T13:56:20+02:00" \\
         --probe "0:50,2:58,3:16"

   Compare the printed "power_smoothed" values against what your head unit
   / Strava shows at those same moments in the video.

3) Know the exact 3s-avg power at a few video timestamps (e.g. by eye from
   the video, or from Strava)? Let the tool auto-fine-tune the offset:

     ./ride_overlay.py --video ride.mov --gpx ride.gpx \\
         --sync "0:36=2026-08-22T13:56:20+02:00" \\
         --calibrate-power "0:50=369,2:58=384,3:16=414" \\
         --out ride_overlay.mp4

Requires ffmpeg/ffprobe on PATH, and the Python packages gpxpy + Pillow
(pip install gpxpy pillow).
"""

import argparse
import bisect
import datetime
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile

try:
    import gpxpy
except ImportError:
    gpxpy = None

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("Missing dependency: pip install pillow", file=sys.stderr)
    raise


# ---------------------------------------------------------------- loading --

def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def load_gpx(path):
    if gpxpy is None:
        sys.exit("Missing dependency: pip install gpxpy")
    with open(path) as f:
        gpx = gpxpy.parse(f)

    def ext_value(point, tag):
        for e in point.extensions:
            if e.tag == "power":
                if tag == "power":
                    return e.text
            elif e.tag.endswith("TrackPointExtension"):
                for c in e:
                    if c.tag.endswith(tag):
                        return c.text
        return None

    points = []
    for trk in gpx.tracks:
        for seg in trk.segments:
            for p in seg.points:
                t = p.time
                if t is None:
                    continue
                if t.tzinfo is None:
                    t = t.replace(tzinfo=datetime.timezone.utc)
                power = ext_value(p, "power")
                hr = ext_value(p, "hr")
                points.append({
                    "time": t,
                    "lat": p.latitude,
                    "lon": p.longitude,
                    "ele": p.elevation,
                    "power": int(power) if power is not None else None,
                    "hr": int(hr) if hr is not None else None,
                })
    points.sort(key=lambda x: x["time"])
    return points


# ------------------------------------------------------------- telemetry --

def build_samples(points, gpx_start, duration, smooth_window, gradient_window, weight_kg=None):
    """Slice `points` to [gpx_start, gpx_start+duration] and compute a
    per-second sample list with trailing-average power and smoothed gradient."""
    end = gpx_start + datetime.timedelta(seconds=duration)
    pad = max(smooth_window, gradient_window) + 2
    pad_start = gpx_start - datetime.timedelta(seconds=pad)
    pad_end = end + datetime.timedelta(seconds=pad)
    padded = [p for p in points if pad_start <= p["time"] <= pad_end]
    if not padded:
        return []

    cumdist = [0.0] * len(padded)
    for i in range(1, len(padded)):
        a, b = padded[i - 1], padded[i]
        if None in (a["lat"], a["lon"], b["lat"], b["lon"]):
            cumdist[i] = cumdist[i - 1]
        else:
            cumdist[i] = cumdist[i - 1] + haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])

    gw = gradient_window
    grad = [None] * len(padded)
    for i in range(len(padded)):
        lo, hi = max(0, i - gw), min(len(padded) - 1, i + gw)
        d_dist = cumdist[hi] - cumdist[lo]
        ele_hi, ele_lo = padded[hi]["ele"], padded[lo]["ele"]
        if d_dist > 3 and ele_hi is not None and ele_lo is not None:
            grad[i] = round((ele_hi - ele_lo) / d_dist * 100, 1)

    sw = smooth_window
    power_raw = [p["power"] for p in padded]
    power_smoothed = [None] * len(padded)
    for i in range(len(padded)):
        lo = max(0, i - (sw - 1))
        vals = [v for v in power_raw[lo:i + 1] if v is not None]
        power_smoothed[i] = round(sum(vals) / len(vals)) if vals else None

    samples = []
    last_grad = None
    for i, p in enumerate(padded):
        if not (gpx_start <= p["time"] <= end):
            continue
        if grad[i] is not None:
            last_grad = grad[i]
        samples.append({
            "t": (p["time"] - gpx_start).total_seconds(),
            "power": power_raw[i],
            "power_smoothed": power_smoothed[i],
            "hr": p["hr"],
            "elevation": p["ele"],
            "gradient": last_grad,
            "wkg": power_smoothed[i] / weight_kg if power_smoothed[i] is not None and weight_kg else None,
        })
    return samples


def sample_at(samples, video_t):
    """nearest-second sample lookup"""
    if not samples:
        return None
    idx = round(video_t)
    times = [s["t"] for s in samples]
    i = bisect.bisect_left(times, idx)
    if i >= len(samples):
        i = len(samples) - 1
    elif i > 0 and abs(times[i - 1] - idx) < abs(times[i] - idx):
        i -= 1
    return samples[i]


# ---------------------------------------------------------------- sync ----

def parse_timecode(s):
    """'196', '196.5', '3:16', '1:03:16' -> seconds (float)"""
    s = s.strip()
    if ":" not in s:
        return float(s)
    parts = [float(x) for x in s.split(":")]
    secs = 0.0
    for part in parts:
        secs = secs * 60 + part
    return secs


def parse_sync_arg(s):
    """'0:36=2026-08-22T13:56:20+02:00' -> (video_seconds, real_time_utc)"""
    if "=" not in s:
        sys.exit(f"--sync must look like VIDEO_TIME=ISO_TIMESTAMP, got: {s}")
    vt_str, real_str = s.split("=", 1)
    video_t = parse_timecode(vt_str)
    real_time = datetime.datetime.fromisoformat(real_str.strip())
    if real_time.tzinfo is None:
        print("warning: --sync real-world time has no timezone offset, assuming UTC", file=sys.stderr)
        real_time = real_time.replace(tzinfo=datetime.timezone.utc)
    real_time_utc = real_time.astimezone(datetime.timezone.utc)
    return video_t, real_time_utc


def parse_calibration_arg(s):
    """'0:50=369,2:58=384,3:16=414' -> [(video_seconds, watts), ...]"""
    pairs = []
    for chunk in s.split(","):
        vt_str, watts_str = chunk.split("=", 1)
        pairs.append((parse_timecode(vt_str), float(watts_str)))
    return pairs


def calibrate_offset(points, base_start, duration, smooth_window, gradient_window,
                      targets, search_window=30):
    """Search integer-second shifts of base_start for the best match against
    known (video_time, watts) pairs. Returns (best_start, best_shift, avg_error)."""
    best = None
    for shift in range(-search_window, search_window + 1):
        candidate_start = base_start + datetime.timedelta(seconds=shift)
        samples = build_samples(points, candidate_start, duration, smooth_window, gradient_window)
        if not samples:
            continue
        errs = []
        for vt, watts in targets:
            s = sample_at(samples, vt)
            if s and s["power_smoothed"] is not None:
                errs.append(abs(s["power_smoothed"] - watts))
        if len(errs) != len(targets):
            continue
        avg_err = sum(errs) / len(errs)
        if best is None or avg_err < best[2]:
            best = (candidate_start, shift, avg_err)
    if best is None:
        sys.exit("Could not calibrate: no GPX data found near the given sync point.")
    return best


# --------------------------------------------------------------- drawing --

FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FONT_REGULAR = "/System/Library/Fonts/Supplemental/Arial.ttf"
WHITE = (255, 255, 255, 255)
DIM = (255, 255, 255, 190)


def draw_heart(draw, box, color):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    r = w / 4
    cy = y0 + h * 0.32
    cx1, cx2 = x0 + w / 4, x0 + 3 * w / 4
    draw.ellipse([cx1 - r, cy - r, cx1 + r, cy + r], fill=color)
    draw.ellipse([cx2 - r, cy - r, cx2 + r, cy + r], fill=color)
    draw.polygon([(x0, cy), (x1, cy), (x0 + w / 2, y1)], fill=color)


def draw_bolt(draw, box, color):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    pts = [
        (0.62, 0.0), (0.22, 0.55), (0.46, 0.55),
        (0.12, 1.0), (0.80, 0.42), (0.52, 0.42), (0.62, 0.0),
    ]
    draw.polygon([(x0 + px * w, y0 + py * h) for px, py in pts], fill=color)


def draw_mountain(draw, box, color):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    draw.polygon(
        [(x0, y1), (x0 + w * 0.4, y0 + h * 0.15), (x0 + w * 0.62, y0 + h * 0.5), (x1, y1)],
        fill=color,
    )
    draw.polygon(
        [(x0 + w * 0.35, y1), (x0 + w * 0.75, y0), (x1 + w * 0.05, y1)],
        fill=color,
    )


def draw_incline(draw, box, color):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    draw.polygon([(x0, y1), (x1, y1), (x1, y0 + h * 0.28)], fill=color)
    ah, aw = h * 0.22, w * 0.16
    draw.polygon([(x1, y0), (x1 - aw, y0 + ah * 0.5), (x1 - aw * 0.15, y0 + ah)], fill=color)


METRICS = {
    "hr": {"icon": draw_heart, "unit": "bpm", "fmt": lambda s: str(s["hr"]) if s["hr"] is not None else None},
    "power": {"icon": draw_bolt, "unit": "W",
              "fmt": lambda s: str(s["power_smoothed"]) if s["power_smoothed"] is not None else None},
    "elevation": {"icon": draw_mountain, "unit": "m",
                  "fmt": lambda s: str(round(s["elevation"])) if s["elevation"] is not None else None},
    "gradient": {"icon": draw_incline, "unit": "grade",
                 "fmt": lambda s: f"{s['gradient']:+.1f}%" if s["gradient"] is not None else None},
    "wkg": {"icon": draw_bolt, "unit": "w/kg",
            "fmt": lambda s: f"{s['wkg']:.1f}" if s.get("wkg") is not None else None},
}


def render_frame(size, sample, metrics, position="bottom-left", badge_scale=1.0):
    W, H = size
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad = int(28 * badge_scale)
    icon_size = int(46 * badge_scale)
    gap = int(14 * badge_scale)
    col_gap = int(46 * badge_scale)
    margin = int(40 * badge_scale)
    font_value = ImageFont.truetype(FONT_BOLD, int(40 * badge_scale))
    font_unit = ImageFont.truetype(FONT_REGULAR, int(22 * badge_scale))

    items = []
    for name in metrics:
        spec = METRICS[name]
        val = spec["fmt"](sample) if sample else None
        items.append((spec["icon"], val if val is not None else "--", spec["unit"]))
    if not items:
        return img

    tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    col_widths = []
    for _, val, unit in items:
        vbbox = tmp.textbbox((0, 0), val, font=font_value)
        ubbox = tmp.textbbox((0, 0), unit, font=font_unit)
        col_widths.append(icon_size + gap + max(vbbox[2] - vbbox[0], ubbox[2] - ubbox[0]))

    badge_w = pad * 2 + sum(col_widths) + col_gap * (len(items) - 1)
    badge_h = pad * 2 + icon_size

    if position == "bottom-left":
        x0, y0 = margin, H - margin - badge_h
    elif position == "bottom-right":
        x0, y0 = W - margin - badge_w, H - margin - badge_h
    elif position == "top-left":
        x0, y0 = margin, margin
    elif position == "top-right":
        x0, y0 = W - margin - badge_w, margin
    else:
        sys.exit(f"unknown --position {position}")

    draw.rounded_rectangle([x0, y0, x0 + badge_w, y0 + badge_h], radius=int(24 * badge_scale), fill=(0, 0, 0, 150))

    cx = x0 + pad
    icon_y0 = y0 + (badge_h - icon_size) / 2
    for (icon_fn, val, unit), col_w in zip(items, col_widths):
        icon_fn(draw, (cx, icon_y0, cx + icon_size, icon_y0 + icon_size), WHITE)
        text_x = cx + icon_size + gap
        vbbox = draw.textbbox((0, 0), val, font=font_value)
        v_h = vbbox[3] - vbbox[1]
        draw.text((text_x, y0 + pad - vbbox[1] - 2), val, font=font_value, fill=WHITE)
        draw.text((text_x, y0 + pad + v_h + 2), unit, font=font_unit, fill=DIM)
        cx += col_w + col_gap

    return img


# ---------------------------------------------------------------- ffmpeg --

def ffprobe_json(video_path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", video_path],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def get_video_info(video_path):
    info = ffprobe_json(video_path)
    vstream = next(s for s in info["streams"] if s["codec_type"] == "video")
    duration = float(info["format"]["duration"])
    width, height = vstream["width"], vstream["height"]
    num, den = vstream["r_frame_rate"].split("/")
    fps = round(float(num) / float(den))
    creation_time = info["format"].get("tags", {}).get("creation_time")
    return {"duration": duration, "width": width, "height": height, "fps": fps, "creation_time": creation_time}


def check_ffmpeg():
    for exe in ("ffmpeg", "ffprobe"):
        if shutil.which(exe) is None:
            sys.exit(f"'{exe}' not found on PATH. Install ffmpeg (e.g. `brew install ffmpeg`).")


# ------------------------------------------------------------------ main --

def main():
    parser = argparse.ArgumentParser(
        description="Overlay heart rate / power / elevation / gradient from a GPX file onto a video.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--video", required=True, help="input video file")
    parser.add_argument("--gpx", required=True, help="GPX file with the same ride's telemetry")
    parser.add_argument("--out", help="output video path (default: <video>_overlay.mp4)")

    sync = parser.add_mutually_exclusive_group()
    sync.add_argument("--sync", metavar="VIDEO_TIME=REAL_TIME",
                       help="e.g. '0:36=2026-08-22T13:56:20+02:00' - what real-world "
                            "clock time a point in the video corresponds to")
    sync.add_argument("--gpx-start", metavar="ISO_TIMESTAMP",
                       help="exact UTC timestamp in the GPX that video time 0:00 corresponds to")

    parser.add_argument("--calibrate-power", metavar="VIDEO_TIME=WATTS[,...]",
                         help="e.g. '0:50=369,2:58=384,3:16=414' - known 3s-avg power readings "
                              "used to auto-fine-tune the sync offset (+/- --calibrate-window seconds)")
    parser.add_argument("--calibrate-window", type=int, default=30,
                         help="search radius in seconds for --calibrate-power (default: 30)")
    parser.add_argument("--nudge", type=float, default=0.0,
                         help="manual fine adjustment in seconds, applied after sync/calibration "
                              "(positive = show data from later in the ride)")

    parser.add_argument("--probe", metavar="VIDEO_TIME[,...]",
                         help="print telemetry at these video timestamps and exit, no rendering "
                              "(e.g. '0:50,2:58,3:16')")

    parser.add_argument("--metrics", default="hr,power,elevation,gradient",
                         help="comma-separated subset/order of: hr,power,wkg,elevation,gradient "
                              "(default: hr,power,elevation,gradient)")
    parser.add_argument("--weight-kg", type=float, default=None,
                         help="rider weight in kg, required if 'wkg' (watts/kg) is included in --metrics")
    parser.add_argument("--position", default="bottom-left",
                         choices=["bottom-left", "bottom-right", "top-left", "top-right"])
    parser.add_argument("--smooth-window", type=int, default=3, help="power trailing-average window, seconds")
    parser.add_argument("--gradient-window", type=int, default=9, help="gradient smoothing half-window, seconds")

    parser.add_argument("--start", type=str, default=None, help="trim: video start time (e.g. '1:30')")
    parser.add_argument("--end", type=str, default=None, help="trim: video end time (e.g. '2:00')")

    parser.add_argument("--crf", type=int, default=18)
    parser.add_argument("--preset", default="medium")
    parser.add_argument("--keep-temp", action="store_true", help="keep the temporary overlay frames directory")

    args = parser.parse_args()

    check_ffmpeg()
    points = load_gpx(args.gpx)
    if not points:
        sys.exit("No telemetry points found in the input file.")

    video_info = get_video_info(args.video)
    clip_start = parse_timecode(args.start) if args.start else 0.0
    clip_end = parse_timecode(args.end) if args.end else video_info["duration"]
    duration = clip_end - clip_start
    if duration <= 0:
        sys.exit("--end must be after --start")

    # Resolve base sync point: base_start is what UTC time video t=0:00 of the
    # ORIGINAL (untrimmed) video corresponds to. All of --sync, --calibrate-power
    # and --probe use video-time relative to that original timeline, independent
    # of any --start/--end trim, so calibrating/probing still works on a clip.
    if args.gpx_start:
        base_start = datetime.datetime.fromisoformat(args.gpx_start)
        if base_start.tzinfo is None:
            base_start = base_start.replace(tzinfo=datetime.timezone.utc)
        base_start = base_start.astimezone(datetime.timezone.utc)
    elif args.sync:
        video_t, real_time_utc = parse_sync_arg(args.sync)
        base_start = real_time_utc - datetime.timedelta(seconds=video_t)
    else:
        if not video_info["creation_time"]:
            sys.exit("No --sync or --gpx-start given, and the video has no creation_time metadata to guess from.")
        base_start = datetime.datetime.fromisoformat(video_info["creation_time"].replace("Z", "+00:00"))
        print(f"warning: no --sync/--gpx-start given, guessing from video metadata: {base_start.isoformat()}\n"
              f"         verify with --probe and/or --calibrate-power before trusting this.", file=sys.stderr)

    full_duration = video_info["duration"]

    if args.calibrate_power:
        targets = parse_calibration_arg(args.calibrate_power)
        base_start, shift, avg_err = calibrate_offset(
            points, base_start, full_duration, args.smooth_window, args.gradient_window,
            targets, args.calibrate_window,
        )
        print(f"calibrated: shifted sync by {shift:+d}s (avg error {avg_err:.1f}W) "
              f"-> gpx_start = {base_start.isoformat()}")

    if args.nudge:
        base_start += datetime.timedelta(seconds=args.nudge)

    samples = build_samples(points, base_start, full_duration, args.smooth_window, args.gradient_window,
                             args.weight_kg)
    if not samples:
        sys.exit("No telemetry found in the resolved sync window - check your --sync/--gpx-start.")

    metrics = [m.strip() for m in args.metrics.split(",") if m.strip()]
    for m in metrics:
        if m not in METRICS:
            sys.exit(f"unknown metric '{m}', choose from: {', '.join(METRICS)}")
    if "wkg" in metrics and not args.weight_kg:
        sys.exit("--metrics includes 'wkg' but no --weight-kg was given.")

    if args.probe:
        for tc in args.probe.split(","):
            vt = parse_timecode(tc)
            s = sample_at(samples, vt)
            print(f"t={vt:>7.1f}s  {s}")
        return

    out_path = args.out or (os.path.splitext(args.video)[0] + "_overlay.mp4")

    frame_count = math.ceil(duration)
    tmp_dir = tempfile.mkdtemp(prefix="ride_overlay_")
    try:
        print(f"rendering {frame_count} overlay frames...")
        for i in range(frame_count):
            s = sample_at(samples, clip_start + i)
            frame = render_frame((video_info["width"], video_info["height"]), s, metrics, args.position)
            frame.save(os.path.join(tmp_dir, f"overlay_{i:05d}.png"))

        cmd = [
            "ffmpeg", "-y",
            "-ss", str(clip_start), "-t", str(duration), "-i", args.video,
            "-framerate", "1", "-i", os.path.join(tmp_dir, "overlay_%05d.png"),
            "-filter_complex",
            f"[1:v]fps={video_info['fps']}[ov];[0:v][ov]overlay=x=0:y=0:shortest=1[v]",
            "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-crf", str(args.crf), "-preset", args.preset,
            "-c:a", "aac", "-b:a", "128k",
            out_path,
        ]
        print("running ffmpeg...")
        subprocess.run(cmd, check=True)
        print(f"done: {out_path}")
    finally:
        if args.keep_temp:
            print(f"kept temp frames at {tmp_dir}")
        else:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
