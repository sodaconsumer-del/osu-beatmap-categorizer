#!/usr/bin/env python3
"""
osu! replay-driven visualizer.

Renders a beatmap difficulty against a REAL replay's recorded cursor path -
not an interpolated guess at movement between hit objects - with correct
AR-based approach circles, OD-based hit windows, and EZ/HR/DT/HT applied to
circle size, approach rate, overall difficulty and playback rate exactly the
way osu! itself applies them. Output is a self-contained HTML fragment meant
to be handed to the visualize widget tool.

Why replay-driven: an early attempt at this simulated cursor movement by
linearly interpolating between consecutive hit object positions/timestamps.
That produces a mechanically-smooth but unrealistic path - real players
don't move at constant velocity between two notes, and jumps in particular
get a curved, accelerate-then-decelerate real path. A .osr replay already
contains the actual recorded (x, y, t) cursor samples, so using it directly
is both easier and correct.

Usage:
    python osu_visualizer.py --osu path/to/diff.osu --osr path/to/replay.osr --output out.html
    python osu_visualizer.py --osz path/to/set.osz --diff "Cutie Patootie" --osr path/to/replay.osr --output out.html

Replay (.osr) binary format and the LZMA-compressed frame stream are ppy's
own documented format; string/int reading reuses the same ULEB128
convention already implemented for osu!.db in classify_maps._OsuDbReader.
AR/OD-to-milliseconds formulas and the DT/HT/EZ/HR stat adjustments are
osu!'s own published values (BeatmapDifficulty.DifficultyRange and
ModEasy/ModHardRock's stat multipliers).
"""

import argparse
import json
import lzma
import os
import struct
import sys
import zipfile

from classify_maps import parse_osu_bytes, mod_adjustments


# --------------------------------------------------------------------------
# Replay (.osr) parsing
# --------------------------------------------------------------------------

class _ReplayReader:
    def __init__(self, data):
        self.d = data
        self.i = 0

    def _take(self, n):
        v = self.d[self.i:self.i + n]
        self.i += n
        return v

    def u8(self):
        return self._take(1)[0]

    def i16(self):
        return struct.unpack("<h", self._take(2))[0]

    def i32(self):
        return struct.unpack("<i", self._take(4))[0]

    def i64(self):
        return struct.unpack("<q", self._take(8))[0]

    def string(self):
        marker = self.u8()
        if marker == 0x00:
            return ""
        length = 0
        shift = 0
        while True:
            b = self.u8()
            length |= (b & 0x7F) << shift
            if not b & 0x80:
                break
            shift += 7
        return self._take(length).decode("utf-8", "replace")


_MOD_BITS = [
    (0, "NF"), (1, "EZ"), (2, "TD"), (3, "HD"), (4, "HR"), (5, "SD"),
    (6, "DT"), (7, "RX"), (8, "HT"), (9, "NC"), (10, "FL"), (11, "AT"),
    (12, "SO"), (13, "AP"), (14, "PF"), (15, "K4"), (16, "K5"), (17, "K6"),
    (18, "K7"), (19, "K8"), (20, "FI"), (21, "RD"), (22, "CN"), (23, "TP"),
    (24, "K9"), (25, "COOP"), (26, "K1"), (27, "K3"), (28, "K2"),
    (29, "SV2"), (30, "MR"),
]


def mods_from_int(m):
    return [name for bit, name in _MOD_BITS if m & (1 << bit)]


def parse_replay(path):
    """Returns dict with mods (list of acronyms) and frames: list of
    (t_ms, x, y, keys), t_ms relative to the start of gameplay on the same
    clock as the beatmap's own hit object timestamps."""
    with open(path, "rb") as f:
        raw = f.read()
    r = _ReplayReader(raw)
    r.u8()  # mode
    r.i32()  # version
    r.string()  # beatmap md5
    player = r.string()
    r.string()  # replay md5
    r.i16(); r.i16(); r.i16(); r.i16(); r.i16(); r.i16()  # hit counts
    r.i32()  # score
    r.i16()  # max combo
    r.u8()  # perfect
    mods_int = r.i32()
    r.string()  # life bar graph
    r.i64()  # timestamp
    replay_len = r.i32()
    compressed = r._take(replay_len)

    frames = []
    if replay_len > 0:
        decompressed = lzma.decompress(compressed, format=lzma.FORMAT_ALONE)
        text = decompressed.decode("ascii", "replace")
        raw_frames = []
        for chunk in text.split(","):
            if not chunk:
                continue
            parts = chunk.split("|")
            if len(parts) != 4:
                continue
            w, x, y, z = parts
            w = int(w)
            if w == -12345:
                continue  # RNG seed marker, not a real frame
            raw_frames.append((w, float(x), float(y), int(z)))
        # First two frames are a fixed sentinel pair (cursor parked at
        # (256,-500) before gameplay, then a known legacy duplicate frame)
        # present in every real replay - their w deltas aren't real elapsed
        # time, so drop both and accumulate fresh from there.
        raw_frames = raw_frames[2:]
        t = 0
        for w, x, y, z in raw_frames:
            t += w
            frames.append((t, x, y, z))

    return {"player": player, "mods": mods_from_int(mods_int), "frames": frames}


# --------------------------------------------------------------------------
# AR/OD -> milliseconds, and mod adjustments to CS/AR/OD
# --------------------------------------------------------------------------

def difficulty_range(value, min_v, mid_v, max_v):
    """osu!'s BeatmapDifficulty.DifficultyRange: piecewise-linear stat (0-10)
    to a millisecond value, hitting min_v at 0, mid_v at 5, max_v at 10."""
    if value > 5:
        return mid_v + (max_v - mid_v) * (value - 5) / 5
    if value < 5:
        return mid_v - (mid_v - min_v) * (5 - value) / 5
    return mid_v


def preempt_fadein_ms(ar):
    preempt = difficulty_range(ar, 1800.0, 1200.0, 450.0)
    fade_in = difficulty_range(ar, 1200.0, 800.0, 300.0)
    return preempt, fade_in


def great_window_ms(od):
    return difficulty_range(od, 80.0, 50.0, 20.0)


def mod_visual_adjustments(mods, cs, ar, od):
    """Extends classify_maps.mod_adjustments (which only handles CS/rate,
    since that's all pattern classification needs) with AR/OD, needed here
    for approach-circle timing and hit windows. EZ/HR halve/scale AR and OD
    the same way they scale CS; DT/HT don't change the AR/OD NUMBERS, they
    change the clock those numbers' millisecond values play out on (handled
    separately via rate)."""
    rate, eff_cs = mod_adjustments(mods, cs)
    eff_ar, eff_od = ar, od
    for m in (mods or []):
        m = m.strip().upper()
        if m == "HR":
            eff_ar = min(eff_ar * 1.4, 10.0)
            eff_od = min(eff_od * 1.4, 10.0)
        elif m == "EZ":
            eff_ar = eff_ar / 2.0
            eff_od = eff_od / 2.0
    return rate, eff_cs, eff_ar, eff_od


# --------------------------------------------------------------------------
# Beatmap loading (plain .osu, or one difficulty out of a .osz)
# --------------------------------------------------------------------------

def load_diff(osu_path=None, osz_path=None, diff_name=None):
    if osu_path:
        with open(osu_path, "rb") as f:
            raw = f.read()
        return parse_osu_bytes(raw, display_name=osu_path, path=osu_path)
    if osz_path:
        with zipfile.ZipFile(osz_path) as z:
            for name in z.namelist():
                if not name.lower().endswith(".osu"):
                    continue
                raw = z.read(name)
                d = parse_osu_bytes(raw, display_name=name, path=name)
                if d is not None and (diff_name is None or d.diff_name == diff_name):
                    return d
    return None


# --------------------------------------------------------------------------
# HTML widget generation
# --------------------------------------------------------------------------

def _decimate_frames(frames, min_gap_ms=12):
    """Real replay frames run ~60-1000Hz depending on client/version - far
    denser than needed for a visual playback (the eye can't tell 12ms
    sampling from 4ms), and every frame costs bytes in the embedded JSON.
    Keeps the first and last frame always, plus one frame per min_gap_ms
    window, and any frame with a key-press change (so click moments never
    get decimated away)."""
    if len(frames) <= 1:
        return list(frames)
    out = [frames[0]]
    last_t = frames[0][0]
    last_keys = frames[0][3]
    for f in frames[1:-1]:
        if f[0] - last_t >= min_gap_ms or f[3] != last_keys:
            out.append(f)
            last_t = f[0]
            last_keys = f[3]
    out.append(frames[-1])
    return out


def compute_render_data(diff, replay, mods_override=None):
    """Everything the renderer needs, mod/AR/OD/rate-adjusted, as plain
    JSON-able data - shared by build_widget_html (HTML+JS playback) and
    osu_visualizer_preview.py (static PNG keyframes for a no-browser look
    at the map). Keep drawing logic out of here; this is data only."""
    mods = mods_override if mods_override is not None else replay["mods"]
    rate, eff_cs, eff_ar, eff_od = mod_visual_adjustments(
        mods, diff.circle_size, diff.approach_rate, diff.overall_difficulty)
    radius = 54.4 - 4.48 * eff_cs
    preempt, fade_in = preempt_fadein_ms(eff_ar)
    great = great_window_ms(eff_od)

    # Object times, replay frame times, and AR/OD-derived ms are all on the
    # SAME nominal map clock (osu!'s internal track time) - raw .osu
    # timestamps require no rate division to line up with a replay's frames,
    # confirmed against real data (last object t=64399 vs last replay frame
    # t=64584, ~185ms apart raw-to-raw with no scaling). rate only matters
    # for how fast that shared nominal clock should be fast-forwarded during
    # PLAYBACK here, which is handled in JS, not by rescaling the data.
    objs = [[round(o[0]), round(o[1]), round(o[2], 1),
             round(o[3], 1), round(o[4], 1), round(o[5], 1)] for o in diff.objs]
    frames = _decimate_frames(replay["frames"])
    frames = [[f[0], round(f[1], 1), round(f[2], 1), f[3]] for f in frames]

    data = {
        "objs": objs, "frames": frames, "radius": round(radius, 2),
        "preempt": round(preempt), "fadeIn": round(fade_in),
        "great": round(great, 1), "mods": mods, "rate": rate,
        "title": f"{diff.title} [{diff.diff_name}]",
        "synthesized": replay["player"] == "(synthesized - no replay)",
    }
    return data


def build_widget_html(diff, replay, mods_override=None):
    """Returns a self-contained HTML fragment (SVG playfield + JS playback)
    driven by the replay's real cursor frames, with AR-correct approach
    circles and mod-adjusted CS/AR/OD/rate. mods_override lets you replay
    the same recording against a different mod combo than it was actually
    set (e.g. show what the NM-recorded path would look like under DT) -
    defaults to the replay's own recorded mods."""
    data = compute_render_data(diff, replay, mods_override=mods_override)
    data_json = json.dumps(data)
    return WIDGET_TEMPLATE.replace("__DATA__", data_json)


WIDGET_TEMPLATE = r"""
<style>
:root { --ov-surface:#f2f0ea; --ov-border:#8a8a86; --ov-text2:#5f5e5a; --ov-btn:#fff; }
@media (prefers-color-scheme: dark) { :root { --ov-surface:#242422; --ov-border:#6a6a66; --ov-text2:#b4b2a9; --ov-btn:#2c2c2a; } }
:root[data-theme="dark"] { --ov-surface:#242422; --ov-border:#6a6a66; --ov-text2:#b4b2a9; --ov-btn:#2c2c2a; }
:root[data-theme="light"] { --ov-surface:#f2f0ea; --ov-border:#8a8a86; --ov-text2:#5f5e5a; --ov-btn:#fff; }
.ov-btn { background:var(--ov-btn,var(--surface-2,#fff)); border:0.5px solid var(--ov-border,var(--border-strong,#888)); border-radius:6px; padding:6px 14px; cursor:pointer; color:inherit; font:inherit; }
.ov-btn:hover { filter:brightness(1.08); }
</style>
<h2 class="sr-only">Replay-driven simulation of the beatmap, using the actual recorded cursor path</h2>
<div style="display:flex;gap:8px;align-items:center;margin:0 0 8px;flex-wrap:wrap;font-family:ui-sans-serif,system-ui,sans-serif">
  <button id="playBtn" class="ov-btn" style="min-width:80px"><i class="ti ti-player-play" aria-hidden="true"></i> Play</button>
  <span style="font-size:13px;color:var(--ov-text2,var(--text-secondary))" id="modsOut"></span>
  <span style="font-size:13px;color:var(--ov-text2,var(--text-secondary));margin-left:auto;font-variant-numeric:tabular-nums" id="timeOut">0.0s</span>
</div>
<div style="position:relative;width:100%;max-width:683px;margin:0 auto">
  <svg id="field" viewBox="0 0 682.6667 384" style="width:100%;height:auto;background:#000;border-radius:8px;display:block">
    <rect x="85.3333" y="0" width="512" height="384" fill="#0b0b0d"/>
    <g id="playfield" transform="translate(85.3333,0)">
      <g id="objs"></g>
      <circle id="cursor" r="8" fill="none" stroke="#7fd8dd" stroke-width="3"/>
      <circle id="cursorDot" r="3" fill="#7fd8dd"/>
    </g>
  </svg>
</div>
<script>
const DATA = __DATA__;
document.getElementById('modsOut').textContent = DATA.title + (DATA.mods.length ? ' +' + DATA.mods.join('') : ' NM') + (DATA.synthesized ? ' · synthesized path' : '');
const objsG = document.getElementById('objs');
const cursor = document.getElementById('cursor'), dot = document.getElementById('cursorDot');
const playBtn = document.getElementById('playBtn'), timeOut = document.getElementById('timeOut');
const R = DATA.radius, PRE = DATA.preempt, FADE = DATA.fadeIn;
const CURSOR_IDLE = '#7fd8dd', CURSOR_HIT = '#ff6eb4';
const totalMs = Math.max(
  DATA.objs.length ? DATA.objs[DATA.objs.length-1][1] + 500 : 0,
  DATA.frames.length ? DATA.frames[DATA.frames.length-1][0] : 0
);
function circleEls(o){
  const g = document.createElementNS('http://www.w3.org/2000/svg','g');
  const c = document.createElementNS('http://www.w3.org/2000/svg','circle');
  c.setAttribute('cx', o[2]); c.setAttribute('cy', o[3]); c.setAttribute('r', R);
  c.setAttribute('fill', 'none'); c.setAttribute('stroke', '#e8e6df'); c.setAttribute('stroke-width', '2');
  const ac = document.createElementNS('http://www.w3.org/2000/svg','circle');
  ac.setAttribute('cx', o[2]); ac.setAttribute('cy', o[3]);
  ac.setAttribute('fill', 'none'); ac.setAttribute('stroke', '#ff6eb4'); ac.setAttribute('stroke-width', '1.5');
  g.appendChild(c); g.appendChild(ac);
  return {g, c, ac};
}
const els = DATA.objs.map(circleEls);
els.forEach(e => { objsG.appendChild(e.g); e.g.style.opacity = 0; });
function findFrame(t){
  const fr = DATA.frames;
  if (!fr.length) return null;
  if (t <= fr[0][0]) return fr[0];
  if (t >= fr[fr.length-1][0]) return fr[fr.length-1];
  let lo = 0, hi = fr.length - 1;
  while (lo < hi - 1) {
    const mid = (lo + hi) >> 1;
    if (fr[mid][0] <= t) lo = mid; else hi = mid;
  }
  const a = fr[lo], b = fr[hi];
  const f = (b[0] - a[0]) > 0 ? (t - a[0]) / (b[0] - a[0]) : 0;
  return [t, a[1] + (b[1]-a[1])*f, a[2] + (b[2]-a[2])*f, a[3]];
}
let playing = false, startPerf = 0, pausedAt = 0;
const RATE = DATA.rate || 1.0;
function frame(now){
  if (!playing) return;
  const t = (now - startPerf) * RATE + pausedAt;
  if (t >= totalMs) { playing = false; playBtn.innerHTML = '<i class="ti ti-player-play" aria-hidden="true"></i> Play'; pausedAt = 0; return; }
  for (let i = 0; i < DATA.objs.length; i++) {
    const o = DATA.objs[i], e = els[i];
    // Hide right at hit time, not slider end - a static head-position
    // marker sitting there for the whole slider duration (while the real
    // replay cursor has already moved on along the slider path) reads as
    // desync/lingering even though the underlying timing data is correct.
    const appear = o[0] - PRE, hit = o[0];
    if (t < appear || t > hit) { e.g.style.opacity = 0; continue; }
    e.g.style.opacity = Math.min(1, (t - appear) / FADE);
    const shrink = Math.max(0, Math.min(1, (hit - t) / PRE));
    e.ac.setAttribute('r', R + shrink * R * 2);
  }
  const f = findFrame(t);
  if (f) {
    cursor.setAttribute('cx', f[1]); cursor.setAttribute('cy', f[2]);
    dot.setAttribute('cx', f[1]); dot.setAttribute('cy', f[2]);
    cursor.setAttribute('stroke', f[3] ? CURSOR_HIT : CURSOR_IDLE);
    dot.setAttribute('fill', f[3] ? CURSOR_HIT : CURSOR_IDLE);
  }
  timeOut.textContent = (t/1000).toFixed(1) + 's / ' + (totalMs/1000).toFixed(1) + 's';
  requestAnimationFrame(frame);
}
playBtn.onclick = () => {
  if (playing) { playing = false; playBtn.innerHTML = '<i class="ti ti-player-play" aria-hidden="true"></i> Play'; pausedAt += (performance.now() - startPerf) * RATE; return; }
  playing = true; startPerf = performance.now(); playBtn.innerHTML = '<i class="ti ti-player-pause" aria-hidden="true"></i> Pause';
  requestAnimationFrame(frame);
};
</script>
"""


def synthesize_frames(diff, sample_ms=12):
    """Fallback cursor path for when there's no real replay: straight-line
    interpolation between each object's end position and the next object's
    start, sampled every sample_ms. Mechanically smooth and NOT how a real
    player moves (no acceleration curve, no overshoot on jumps) - real
    replay data is always better when available. Good enough to see note
    density/spacing/pattern shape without needing a recorded play."""
    objs = diff.objs
    if not objs:
        return []
    frames = [(round(objs[0][0]), objs[0][2], objs[0][3], 0)]
    for i in range(1, len(objs)):
        p, c = objs[i - 1], objs[i]
        t0, t1 = p[1], c[0]
        if t1 <= t0:
            continue
        n = max(1, int((t1 - t0) / sample_ms))
        for k in range(1, n + 1):
            f = k / n
            t = t0 + (t1 - t0) * f
            x = p[4] + (c[2] - p[4]) * f
            y = p[5] + (c[3] - p[5]) * f
            frames.append((round(t), x, y, 1 if f >= 0.97 else 0))
    return frames


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--osu", help="Path to a single .osu file")
    ap.add_argument("--osz", help="Path to a .osz archive (use with --diff)")
    ap.add_argument("--diff", help="Difficulty name to pick out of --osz")
    ap.add_argument("--osr", help="Path to a .osr replay - omit for a synthesized "
                     "(interpolated, approximate) cursor path instead")
    ap.add_argument("--mods", help="Comma-separated mod override, e.g. DT or HR,HD - "
                     "required (defaults to NM) when --osr is omitted, since there's "
                     "no recorded replay to read mods from")
    ap.add_argument("--output", required=True, help="Output HTML fragment path")
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
        source = "replay"
    else:
        replay = {"player": "(synthesized - no replay)", "mods": [], "frames": synthesize_frames(diff)}
        source = "synthesized"

    html = build_widget_html(diff, replay, mods_override=mods_override)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {args.output} ({len(diff.objs)} objects, {len(replay['frames'])} {source} frames, "
          f"mods={mods_override or replay['mods']})")


if __name__ == "__main__":
    main()
