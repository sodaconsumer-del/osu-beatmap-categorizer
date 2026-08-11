#!/usr/bin/env python3
"""
Static-PNG keyframe renderer for osu_visualizer.py's render data - lets
Claude actually look at a map without a browser, a local server, or the
Artifact viewer (all of which have proven flaky/unwanted in practice: the
Browser pane failed to composite frames repeatedly, and a local server was
explicitly rejected as "not user friendly" for what's supposed to be a
zero-setup skill). This renders straight to PNG files with Pillow, which
the Read tool can display directly.

NOT part of the shipped tool - classify_maps.py/gui.py/osu_visualizer.py
stay stdlib-only because they ship in the PyInstaller build (see AGENTS.md).
This is a separate dev-only script for Claude's own visual sanity-checking,
so the Pillow dependency here doesn't touch that build.

Mirrors the same per-frame render rules as WIDGET_TEMPLATE's JS in
osu_visualizer.py (approach circle shrink, instant hide at hit time, cursor
color by key state, 16:9 letterbox) - if you change one, change both, or
what Claude sees here will lie about what the real widget shows.

Usage:
    python osu_visualizer_preview.py --osz set.osz --diff "Name" --osr replay.osr --out-dir keyframes/
    python osu_visualizer_preview.py --osu diff.osu --mods DT --out-dir keyframes/ --n-frames 8
"""
import argparse
import bisect
import os
import sys

from PIL import Image, ImageDraw

from osu_visualizer import (
    compute_render_data, load_diff, parse_replay, synthesize_frames,
)

CANVAS_W, CANVAS_H = 683, 384
PLAYFIELD_W = 512
OFFSET_X = (CANVAS_W - PLAYFIELD_W) / 2

CURSOR_IDLE = (127, 216, 221)
CURSOR_HIT = (255, 110, 180)
CIRCLE_STROKE = (232, 230, 223)
APPROACH_STROKE = (255, 110, 180)
PLAYFIELD_BG = (11, 11, 13)
LETTERBOX_BG = (0, 0, 0)


def _tx(x, y):
    return x + OFFSET_X, y


def find_frame(frames, t):
    if not frames:
        return None
    ts = [f[0] for f in frames]
    if t <= ts[0]:
        return frames[0]
    if t >= ts[-1]:
        return frames[-1]
    i = bisect.bisect_right(ts, t)
    a, b = frames[i - 1], frames[i]
    f = (t - a[0]) / (b[0] - a[0]) if b[0] > a[0] else 0
    return (t, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f, a[3])


def render_frame(data, t):
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), LETTERBOX_BG)
    d = ImageDraw.Draw(img)
    d.rectangle([OFFSET_X, 0, OFFSET_X + PLAYFIELD_W, CANVAS_H], fill=PLAYFIELD_BG)

    R, PRE, FADE = data["radius"], data["preempt"], data["fadeIn"]
    for o in data["objs"]:
        appear, hit = o[0] - PRE, o[0]
        if t < appear or t > hit:
            continue
        cx, cy = _tx(o[2], o[3])
        d.ellipse([cx - R, cy - R, cx + R, cy + R], outline=CIRCLE_STROKE, width=2)
        shrink = max(0.0, min(1.0, (hit - t) / PRE)) if PRE else 0.0
        ar = R + shrink * R * 2
        d.ellipse([cx - ar, cy - ar, cx + ar, cy + ar], outline=APPROACH_STROKE, width=2)

    f = find_frame(data["frames"], t)
    if f:
        fx, fy = _tx(f[1], f[2])
        color = CURSOR_HIT if f[3] else CURSOR_IDLE
        d.ellipse([fx - 8, fy - 8, fx + 8, fy + 8], outline=color, width=3)
        d.ellipse([fx - 3, fy - 3, fx + 3, fy + 3], fill=color)

    return img


def render_keyframes(diff, replay, out_dir, mods_override=None, n_frames=6, timestamps=None):
    data = compute_render_data(diff, replay, mods_override=mods_override)
    os.makedirs(out_dir, exist_ok=True)

    if timestamps is None:
        last_obj = data["objs"][-1][1] if data["objs"] else 0
        last_frame = data["frames"][-1][0] if data["frames"] else 0
        total = max(last_obj, last_frame)
        first_appear = (data["objs"][0][0] - data["preempt"]) if data["objs"] else 0
        timestamps = [round(first_appear + (total - first_appear) * i / (n_frames - 1))
                      for i in range(n_frames)]

    paths = []
    for i, t in enumerate(timestamps):
        img = render_frame(data, t)
        path = os.path.join(out_dir, f"frame_{i:02d}_t{t}.png")
        img.save(path)
        paths.append(path)
    return paths, data


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--osu")
    ap.add_argument("--osz")
    ap.add_argument("--diff")
    ap.add_argument("--osr", help="omit for a synthesized cursor path")
    ap.add_argument("--mods")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--n-frames", type=int, default=6)
    args = ap.parse_args()

    if not args.osu and not args.osz:
        ap.error("need --osu or --osz")
    diff = load_diff(osu_path=args.osu, osz_path=args.osz, diff_name=args.diff)
    if diff is None:
        print("Could not find that difficulty.", file=sys.stderr)
        sys.exit(1)
    mods_override = [m.strip().upper() for m in args.mods.split(",")] if args.mods else None
    if args.osr:
        replay = parse_replay(args.osr)
    else:
        replay = {"player": "(synthesized - no replay)", "mods": [], "frames": synthesize_frames(diff)}

    paths, data = render_keyframes(diff, replay, args.out_dir, mods_override=mods_override,
                                    n_frames=args.n_frames)
    for p in paths:
        print(p)


if __name__ == "__main__":
    main()
