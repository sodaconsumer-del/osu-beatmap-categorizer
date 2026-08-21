#!/usr/bin/env python3
"""
osu! Burst/Stream/Jump Classifier
----------------------------------
Scans your osu! library, classifies every difficulty by pattern content,
and writes an osu!stable-compatible collection.db with a separate collection
for each category.

Reads the game's own database where it can - osu!.db for stable, client.realm
for lazer - which avoids walking the disk and picks up ranked status, star
ratings and beatmap ids for free. Falls back to scanning a folder of .osu /
.osz files when there's no database to read.

Usage:
    python classify_maps.py "C:/Users/you/AppData/Local/osu!/Songs" --output collection.db

    # Just print a report, don't write a collection.db:
    python classify_maps.py "C:/path/to/Songs" --no-db

Categories (default thresholds, all tunable via CLI flags):
    - Streams            : contains a run of 10+ fast, consistently-timed,
                            closely-spaced notes (cutstreams count too)
    - Bursts             : contains 3-9 note bursts but no full streams
    - Jumps with bursts  : has both, but jumps cover more of the map
    - Jumps (no bursts)  : jump-heavy, no bursts or streams at all
    - Misc               : none of the above (low density / normal play)

A note-to-note transition only counts toward a burst/stream run if BOTH:
    1. It's fast in ABSOLUTE terms (<= max_gap_ms between taps). Stream BPM
       is 15000 / ms, so 140ms is roughly a 107 BPM stream.
    2. It's rhythmically consistent with the rest of the run - a real stream
       doesn't change tapping speed halfway through.
The run as a whole must then be a FAST SNAP for its map, not merely fast in
milliseconds: at most burst_beat_fraction_max (0.4) of a beat per note, which
admits 1/4 and 1/3 and rejects 1/2. Without that, a 240 BPM jump map's
ordinary 1/2 tapping (125ms - under the cap) reads as wall-to-wall bursts.
The stored tempo still isn't trusted blindly, because plenty of maps notate a
doubled or halved one: runs at or under burst_always_fast_ms (110ms) skip the
snap test entirely, and halved notation is folded back out first (see
effective_beat_ms). So the snap test only ever decides the 110-140ms band.

Spacing then decides what KIND of run it is. Fast-but-far transitions are
jumps, not bursts, regardless of how tight the timing is (e.g. 1/4-snap jump
streams at high BPM are NOT bursts). Spacing sets no lower bound: a stacked
1/4 triple - every note on one point, the commonest burst shape there is -
is a burst.

Spacing is measured from the previous object's END, so a long slider whose
tail sits next to the following note isn't mistaken for a full-screen jump.

Classification is NM-only. Mod support was removed deliberately - see
"NM only" in AGENTS.md; mod_adjustments() is kept because osu_visualizer.py
still needs it, and so restoring this is a small job.

Accuracy is measurable rather than a matter of taste - see eval_classifier.py,
which scores a report.csv against hand-labelled maps. Run test_classify.py for
the synthetic unit tests.
"""

import argparse
import bisect
import hashlib
import math
import os
import re
import struct
import sys
from dataclasses import dataclass, field


# --------------------------------------------------------------------------
# .osu parsing
# --------------------------------------------------------------------------

@dataclass
class DiffInfo:
    path: str
    title: str
    diff_name: str
    version_hash: str  # MD5 of raw file content, as used by osu!'s own db
    # (start_ms, end_ms, start_x, start_y, end_x, end_y) per hit object.
    # For circles start == end in both time and position. For sliders, end_ms
    # is when the slider finishes and (end_x, end_y) is where the cursor is
    # left - see _slider_end(). Spinners are dropped at parse time.
    objs: list
    timing_points: list  # (time, beatLength) uninherited only
    sv_points: list      # (time, slider velocity multiplier) from inherited points
    circle_size: float
    bpm: float
    slider_multiplier: float = 1.4
    # Neither is consulted by classify_diff (pattern content doesn't depend
    # on approach time or hit windows) - carried through for consumers that
    # do need them, e.g. osu_visualizer.py's approach-circle/hit-window
    # rendering. approach_rate falls back to overall_difficulty when the
    # .osu has no ApproachRate key, matching osu!'s own pre-AR-field
    # behaviour (maps saved before AR was split out from OD).
    overall_difficulty: float = 5.0
    approach_rate: float = 5.0

    burst_count: int = 0
    stream_count: int = 0
    cutstream_count: int = 0
    max_burst_len: int = 0
    max_stream_len: int = 0
    jump_pct: float = 0.0

    # Total notes covered by burst/stream runs (not just run count) and total
    # note count for the diff - used to compare each pattern's coverage
    # against the others proportionally, rather than treating "any run at
    # all" as equally significant regardless of how small it is relative to
    # the rest of the map.
    burst_note_total: int = 0
    stream_note_total: int = 0
    total_note_count: int = 0
    # Denominator jump_pct is measured against (transitions not excluded as
    # breaks - see jump_gap_cap_ms). Exists so category_of() can compare
    # burst coverage against jump coverage on the SAME basis (transitions)
    # instead of notes vs transitions - see category_of()'s docstring.
    counted_gaps: int = 0

    # How those counted_gaps transitions divide up. A PARTITION: each gameplay
    # transition is described by exactly one of these (or by none, if it is
    # neither run content nor a jump - ordinary spaced tapping), so the three
    # are directly comparable and sum to at most counted_gaps.
    #
    # These replace deriving burst/stream coverage from note counts. A run of
    # N notes is N-1 transitions, so the old conversion was arithmetically
    # exact - what it could not express is that two passes might both claim
    # the same transition. Kept alongside the note totals rather than
    # replacing them, because report.csv and --from-csv still carry those.
    burst_transitions: int = 0
    stream_transitions: int = 0
    jump_transitions: int = 0

    # Sectional structure. Coverage alone says how MUCH of a map is streams
    # and how much is jumps; it can't tell "alternating stream and jump
    # sections" from "evenly mixed throughout", because both average out the
    # same. These count how many sections of the map each pattern actually
    # OWNS - which is what makes the Hybrid category possible. See
    # section_pattern_counts().
    active_sections: int = 0
    stream_sections: int = 0
    burst_sections: int = 0
    jump_sections: int = 0

    # multi-label - a diff can be any combination of these, not mutually exclusive
    has_bursts: bool = False
    has_streams: bool = False
    has_hybrid: bool = False
    has_jumps: bool = False
    has_cutstreams: bool = False

    # "ranked", "unranked", or None if unknown (only available via the
    # osu!lazer realm fast path - a plain filesystem scan of .osu files
    # has no way to know a beatmap's online ranked status, since that's
    # tracked separately by osu! servers, not stored in the .osu file itself)
    ranked_status: object = None

    # Star rating (float), or None if unknown. Same lazer-realm-only
    # availability caveat as ranked_status - a plain filesystem scan has no
    # way to compute this (it needs osu!'s full difficulty calculator,
    # which this tool doesn't reimplement), so it's only populated when the
    # data came from a cached value already stored in client.realm.
    star_rating: object = None

    # osu! online beatmap ID (int), or None if unknown. Same lazer-realm-only
    # availability as the two above. Carried through to the CSV so predictions
    # can be joined against ground-truth labels fetched from the osu! API -
    # see eval_classifier.py.
    online_id: object = None


def parse_osu_file(path):
    with open(path, "rb") as f:
        raw = f.read()
    return parse_osu_bytes(raw, display_name=path, path=path)


def parse_osu_bytes(raw, display_name, path=None):
    md5 = hashlib.md5(raw).hexdigest()
    text = raw.decode("utf-8", errors="ignore")

    title_m = re.search(r"^Title:(.*)$", text, re.M)
    diff_m = re.search(r"^Version:(.*)$", text, re.M)
    cs_m = re.search(r"^CircleSize:(.*)$", text, re.M)
    mode_m = re.search(r"^Mode:(.*)$", text, re.M)
    sm_m = re.search(r"^SliderMultiplier:(.*)$", text, re.M)
    od_m = re.search(r"^OverallDifficulty:(.*)$", text, re.M)
    ar_m = re.search(r"^ApproachRate:(.*)$", text, re.M)

    mode = int(mode_m.group(1).strip()) if mode_m else 0
    if mode != 0:
        return None  # only classic osu!standard for now (bursts/streams are a std concept)

    title = title_m.group(1).strip() if title_m else display_name
    diff_name = diff_m.group(1).strip() if diff_m else "?"
    cs = float(cs_m.group(1).strip()) if cs_m else 4.0
    try:
        od = float(od_m.group(1).strip()) if od_m else 5.0
    except ValueError:
        od = 5.0
    try:
        ar = float(ar_m.group(1).strip()) if ar_m else od  # pre-AR maps: AR follows OD
    except ValueError:
        ar = od
    try:
        slider_multiplier = float(sm_m.group(1).strip()) if sm_m else 1.4
    except ValueError:
        slider_multiplier = 1.4
    if slider_multiplier <= 0:
        slider_multiplier = 1.4

    # Two kinds of timing point, distinguished by the sign of beatLength:
    #   positive -> uninherited, sets the actual tempo
    #   negative -> inherited, sets slider velocity as -100/beatLength
    # Slider velocity is needed to work out how long a slider lasts, which
    # feeds the movement-time term of the jump metric.
    tp_section = re.search(r"\[TimingPoints\](.*?)(\[|$)", text, re.S)
    timing_points = []
    sv_points = []
    if tp_section:
        for line in tp_section.group(1).splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(",")
            if len(parts) < 2:
                continue
            try:
                t = float(parts[0])
                bl = float(parts[1])
            except ValueError:
                continue
            uninherited = parts[6] if len(parts) > 6 else "1"
            if bl > 0 and (uninherited == "1" or len(parts) <= 6):
                timing_points.append((t, bl))
            elif bl < 0:
                sv_points.append((t, -100.0 / bl))
    timing_points.sort()
    sv_points.sort()

    ho_section = re.search(r"\[HitObjects\](.*?)(\[|$)", text, re.S)
    objs = []
    if ho_section:
        for line in ho_section.group(1).splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(",")
            if len(parts) < 4:
                continue
            try:
                x = float(parts[0])
                y = float(parts[1])
                t = int(parts[2])
                obj_type = int(parts[3])
            except ValueError:
                continue

            # Type is a bit field: 1=circle, 2=slider, 8=spinner (bit 2 is
            # new-combo and bits 4-6 are combo-colour skip, so these must be
            # tested as flags, not compared for equality).
            if obj_type & 8:
                # Spinners carry a placeholder position (almost always the
                # playfield centre) that has nothing to do with where the
                # cursor actually goes. Including them invents a jump into
                # and out of the centre of the screen - and 56% of the
                # difficulties in a sample of this library contain at least
                # one - so they're dropped rather than mismeasured.
                continue
            if obj_type & 2:
                end_t, ex, ey = _slider_end(parts, t, x, y, timing_points,
                                             sv_points, slider_multiplier)
            else:
                end_t, ex, ey = float(t), x, y
            objs.append((float(t), end_t, x, y, ex, ey))
    objs.sort(key=lambda o: o[0])

    # A malformed/corrupt map can carry a beatLength near zero (subnormal
    # float, no exception at division - IEEE754 divide overflow just returns
    # inf). round(inf) later crashes the CSV write with "cannot convert float
    # infinity to integer", so clamp here at the source rather than only
    # guarding every downstream consumer.
    bpm = 60000.0 / timing_points[0][1] if timing_points else 0.0
    if not math.isfinite(bpm):
        bpm = 0.0

    return DiffInfo(
        path=path,
        title=title,
        diff_name=diff_name,
        version_hash=md5,
        objs=objs,
        timing_points=timing_points,
        sv_points=sv_points,
        circle_size=cs,
        bpm=bpm,
        slider_multiplier=slider_multiplier,
        overall_difficulty=od,
        approach_rate=ar,
    )


def _beat_length_at(t, timing_points):
    """
    Beat length in ms at time t. Binary search rather than a linear rescan:
    this is called once per note transition, so a linear scan made the whole
    classify pass O(notes x timing_points) - noticeable on maps that carry
    hundreds of timing points.

    Comparing against (t + 2, inf) picks the last point at or before t (with
    the same 2ms grace the linear version used, for notes sitting exactly on
    a timing point but rounded a hair early).
    """
    if not timing_points:
        return 500.0
    i = bisect.bisect_right(timing_points, (t + 2, float("inf"))) - 1
    return timing_points[i][1] if i >= 0 else timing_points[0][1]


def _sv_at(t, sv_points):
    """Slider velocity multiplier at time t. Defaults to 1.0 before the first
    inherited point, matching osu!'s behaviour."""
    if not sv_points:
        return 1.0
    i = bisect.bisect_right(sv_points, (t + 2, float("inf"))) - 1
    return sv_points[i][1] if i >= 0 else 1.0


def _slider_end(parts, t, x, y, timing_points, sv_points, slider_multiplier):
    """
    Works out when a slider finishes and roughly where it leaves the cursor.

    Line format is:
        x,y,time,type,hitSound,curve,slides,length,...
    so parts[5] is the curve, parts[6] the number of slides (1 = no repeat)
    and parts[7] the path length in osu!pixels.

    Duration is exact, straight from osu!'s own formula. The end POSITION is
    an approximation: the true path is a bezier/perfect-circle/catmull curve,
    and computing it properly means reimplementing osu!'s path generator. We
    instead walk `length` pixels along the polyline through the control
    points, which is exact for linear sliders and close for the gentle curves
    that make up most real maps. It over-runs slightly on tightly-curved
    sliders (a chord is shorter than its arc), in which case it clamps to the
    last control point.

    This only feeds spacing, and even a rough tail position is far better
    than the alternative of using the head - a long slider whose tail sits
    next to the following note otherwise reads as a huge jump.
    """
    try:
        slides = int(parts[6])
        length = float(parts[7])
    except (ValueError, IndexError):
        return float(t), x, y
    if slides < 1:
        slides = 1
    if length < 0:
        length = 0.0

    bl = _beat_length_at(t, timing_points)
    sv = _sv_at(t, sv_points)
    px_per_beat = 100.0 * slider_multiplier * sv
    if px_per_beat <= 0:
        return float(t), x, y
    duration = (length / px_per_beat) * bl * slides
    end_t = float(t) + duration

    # An even number of slides lands the cursor back where it started.
    if slides % 2 == 0:
        return end_t, x, y

    curve = parts[5] if len(parts) > 5 else ""
    ex, ey = _point_along_polyline(curve, x, y, length)
    return end_t, ex, ey


def _point_along_polyline(curve, x0, y0, length):
    """Walk `length` pixels from (x0, y0) along the slider's control points."""
    pts = [(x0, y0)]
    for token in curve.split("|")[1:]:  # first token is the curve type letter
        bits = token.split(":")
        if len(bits) != 2:
            continue
        try:
            pts.append((float(bits[0]), float(bits[1])))
        except ValueError:
            continue
    if len(pts) < 2:
        return x0, y0

    remaining = length
    for i in range(1, len(pts)):
        ax, ay = pts[i - 1]
        bx, by = pts[i]
        seg = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
        if seg <= 0:
            continue
        if remaining <= seg:
            f = remaining / seg
            return ax + (bx - ax) * f, ay + (by - ay) * f
        remaining -= seg
    return pts[-1]


# --------------------------------------------------------------------------
# Mods
# --------------------------------------------------------------------------

# Rate-changing mods, from osu!'s ModRateAdjust subclasses. DT/NC default to
# SpeedChange 1.5, HT/DC to 0.75. These scale every time value in the map, so
# they matter a great deal once speed is judged in absolute milliseconds: a
# 150ms 1/2 rhythm becomes a 100ms one under DT, which really is stream speed.
_MOD_RATES = {"DT": 1.5, "NC": 1.5, "HT": 0.75, "DC": 0.75}


def mod_adjustments(mods, circle_size):
    """
    Returns (rate, effective_circle_size) for a set of mod acronyms.

    Only two things a mod does can change how a map is classified here:

      - rate, which scales all timing (DT/NC/HT/DC)
      - circle size, which scales the diameter that all spacing is measured
        against. HR is CS * 1.3 capped at 10, EZ is CS / 2.

    Deliberately ignored: HR's ReflectVerticallyAlongPlayfield. Reflecting
    every object across one axis is an isometry, so it leaves every
    pairwise distance - and therefore every spacing decision made below -
    exactly as it was. HR's OD/AR changes affect hit windows and approach
    time, neither of which is a pattern property.

    NM (no mods) is the baseline and returns (1.0, circle_size) unchanged.
    """
    rate = 1.0
    cs = circle_size
    for m in (mods or []):
        m = m.strip().upper()
        if not m or m == "NM":
            continue
        if m in _MOD_RATES:
            rate *= _MOD_RATES[m]
        elif m == "HR":
            cs = min(cs * 1.3, 10.0)
        elif m == "EZ":
            cs = cs / 2.0
    return rate, cs


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

# Below this many hit objects, a diff is almost certainly junk (a broken
# upload, a storyboard-only "difficulty", a leftover test file) rather than
# real gameplay - a 4-note diff being "60% jumps" is noise, not a finding.
# Hard floor, independent of burst_min (which can be set lower via CLI/GUI).
#
# Excluded entirely, not just reported as Misc: every scan path (scan_folder,
# scan_lazer_realm, scan_stable_db) filters on this before a diff is even
# added to the results list, so junk this small never reaches the CSV,
# collection.db, or the classify step at all. classify_diff() also enforces
# it independently below, as a second line of defense for any caller that
# builds a DiffInfo and classifies it directly rather than through a scan
# (tests do this; --from-csv rebuilds do not re-run classify_diff at all,
# since they work from already-computed CSV columns).
MIN_OBJECTS_TO_CLASSIFY = 10

# No human plays a sustained average faster than this. Real library check
# (ai-classification branch): note density (object count / time span) across
# ~58,400 real diffs decays continuously up to ~27/sec, then a genuine gap -
# nothing between 28 and 30/sec - before a separate cluster of 11 outliers
# from 30/sec up to 3968/sec, all confirmed troll/audio-visualizer content by
# title ("u cant even stream 1000bpm u pleb", "unbeatable", "cancerstream")
# and star rating (up to 356 - real maps top out around 9-10). Visually
# confirmed via osu_visualizer_preview.py on "Left Behind [god has forasken
# us]" (74,948 notes averaging 487/sec): dozens of notes stacked at each of
# four fixed points, firing far faster than any cursor could move or click -
# not gameplay, an audio visualizer built out of hit objects. 30 sits in the
# gap itself, so it doesn't touch the real (if extreme) tail below it.
MAX_SUSTAINED_NOTES_PER_SEC = 30.0


def is_junk_diff(diff):
    """
    True if `diff` isn't worth keeping at all - too few hit objects to mean
    anything, or a note density no human could play (see
    MAX_SUSTAINED_NOTES_PER_SEC above).
    """
    if diff is None or len(diff.objs) < MIN_OBJECTS_TO_CLASSIFY:
        return True
    span_ms = diff.objs[-1][0] - diff.objs[0][0]
    if span_ms <= 0:
        # Every object crammed into a single instant - not a real timeline.
        return True
    if len(diff.objs) / (span_ms / 1000.0) > MAX_SUSTAINED_NOTES_PER_SEC:
        return True
    return False


# Shortest beat length that could be a real notated tempo (100ms = 600 BPM).
# Past this the file is describing something no song does, and the honest
# answer is that its tempo is unknown rather than absurdly fast.
MIN_PLAUSIBLE_BEAT_MS = 100.0


def usable_beat_ms(beat_ms):
    """
    `beat_ms` if it could be a real notated tempo, else 0.0 for "unknown".

    Callers skip any snap test rather than measure against a number that isn't
    a tempo. A subnormal or negative beatLength is the parse-level corruption
    test_tiny_beat_length_does_not_produce_infinite_bpm covers; anything under
    MIN_PLAUSIBLE_BEAT_MS is a "tempo" no song has, so the safe reading is
    that the timing data is junk - not that every run in the map is too slow.
    """
    if not math.isfinite(beat_ms) or beat_ms < MIN_PLAUSIBLE_BEAT_MS:
        return 0.0
    return beat_ms


def section_pattern_counts(objs, eligible, label, section_ms=2000.0,
                            section_dominance=0.5, section_min_transitions=4):
    """
    Split the map into fixed time sections and count how many each pattern
    owns. Returns (active, stream, burst, jump).

    Coverage says how much of a map is streams and how much is jumps. It
    cannot distinguish "a jump half and a stream half" from "evenly mixed all
    the way through" - both average to the same numbers - and those are very
    different maps to play. Sections are what make that difference visible,
    and they are what the Hybrid category is built on.

    `label` maps a transition index to "s"/"b"/"j"; anything absent is
    ordinary spaced tapping and counts toward the section's size but owns
    nothing.

    section_ms = 2000 was picked by looking, not by taste. osu!'s own
    StrainSkill uses 400ms, but that is for strain peaks: at 200 BPM it is
    barely one beat, and printing real maps at that length gives fragmented
    noise with no visible structure. At 2000ms (roughly two bars at 200 BPM)
    the structure of a map like FREEDOM DiVE [ENDLESS DiMENSiONS] reads
    directly off the timeline - distinct jump passages and a long stream
    passage, in the places the map actually has them.

    A section must be at least section_min_transitions long to be counted at
    all - otherwise the sparse tail of a map, or a couple of notes either
    side of a break, would each register as a full "section" and swamp the
    proportions.
    """
    if not eligible:
        return 0, 0, 0, 0
    start = objs[eligible[0]][0]
    buckets = {}
    for i in eligible:
        # Transition i runs from objs[i] to objs[i+1]; date it by its start.
        k = int((objs[i][0] - start) // section_ms)
        buckets.setdefault(k, []).append(label.get(i))
    active = streams = bursts = jumps = 0
    for members in buckets.values():
        if len(members) < section_min_transitions:
            continue
        active += 1
        for tag, bump in (("s", "stream"), ("b", "burst"), ("j", "jump")):
            if members.count(tag) / len(members) >= section_dominance:
                if bump == "stream":
                    streams += 1
                elif bump == "burst":
                    bursts += 1
                else:
                    jumps += 1
                break   # a section is owned by at most one pattern
    return active, streams, bursts, jumps


def looks_like_doubled_notation(objs, timing_points, burst_max_gap_ms=105.0,
                                 doubled_half_share_min=0.25,
                                 max_plausible_bpm=300.0):
    """
    True if this map's stored tempo looks like DOUBLED notation - a song
    really at 180 BPM written as 360.

    The mirror of looks_like_halved_notation(), needed for the same reason
    pointing the other way: the snap test asks "is this faster than 1/2?", and
    under doubled notation the 1/4 a player actually feels is written as a
    1/2, so a real burst reads as ordinary tapping and is thrown away.

    Two conditions, both from the notes:

      1. The NOTATED TEMPO exceeds max_plausible_bpm. Decisive, and read off
         the timing points rather than the notes on purpose: notation is a
         property of the MAPSET, so every difficulty in it must get the same
         answer. Content alone does not do that. Across the eight difficulties
         of "Flowering Night Fever" (a real 290 BPM map) the quiet ones carry
         no 1/4 at all, so they look exactly like doubled notation while their
         harder siblings do not - three of the eight flipped, inflating their
         burst counts. A tempo bound is shared by construction.
      2. The notated 1/2 is a workhorse layer (doubled_half_share_min) AND is
         mostly faster than burst_max_gap_ms - tapped at speeds no real 1/2
         reaches. Same derived threshold the halved detector uses.
      3. The notated 1/4 is essentially absent - under doubled notation it
         would be a real 1/8, which practically nothing writes. Supporting
         evidence only; on its own it is what misfires above.

    Note the asymmetry with the halved detector, which needs no tempo bound:
    there the content genuinely decides, because a halved map's 1/4 layer is
    both large AND too slow to be real burst content. Here it cannot, so the
    tempo has to.

    Measured on the two clean examples:
        The Big Black [NC]       notated 360 - 1/2 50%, 1/4  0%  -> doubled
        Flowering Night Fever    real    290 - 1/2 44%, 1/4 30%  -> honest

    This exists so the snap test can apply to EVERY run. It used to be skipped
    for anything under 110ms, as a blunt way to protect doubled notation, and
    that left every map above ~273 BPM exempt - their ordinary 1/2 jump pulse
    counted as bursts, 127 of them on "Flowering Night Fever [Ekoro's Fever]".
    """
    if not timing_points or len(objs) < 2:
        return False
    beat0 = usable_beat_ms(timing_points[0][1])
    if not beat0 or 60000.0 / beat0 <= max_plausible_bpm:
        return False
    half = half_fast = quarter = total = 0
    for i in range(1, len(objs)):
        gap = objs[i][0] - objs[i - 1][0]
        if not (0 < gap <= 2000):
            continue
        total += 1
        beat = usable_beat_ms(_beat_length_at(objs[i - 1][0], timing_points))
        if not beat:
            continue
        div = beat / gap
        if abs(div - 2.0) / 2.0 < 0.10:
            half += 1
            if gap <= burst_max_gap_ms:
                half_fast += 1
        elif abs(div - 4.0) / 4.0 < 0.10:
            quarter += 1
    if not total or half / total < doubled_half_share_min:
        return False
    if quarter >= half * 0.25:
        return False              # a real 1/4 layer - the tempo is honest
    return half_fast * 2 > half   # and that 1/2 is too fast to be a real 1/2


def looks_like_halved_notation(objs, timing_points, burst_max_gap_ms=105.0,
                                halved_quarter_share_min=0.15):
    """
    True if this map's stored tempo looks like HALVED notation - a song really
    at 260 BPM written as 130, which osu! plays identically either way.

    It matters because the snap test asks "is this run faster than 1/2?", and
    under halved notation the plain 1/2 tapping a player feels is written as
    1/4 and sails through.

    Decided from the NOTES, not from a threshold on the stored BPM. Two
    conditions, both required:

      1. The notated 1/4 carries a WORKHORSE share of the map
         (halved_quarter_share_min). In a correctly-notated map 1/4 is accent
         content - bursts and streams - and stays a minority; under halved
         notation the map's real 1/2 backbone lands there, so it dominates.
      2. Most of that 1/4 layer is SLOWER than burst_max_gap_ms, i.e. too slow
         to be genuine burst or stream content. This is what stops a real
         stream map from being folded: a 180 BPM stream map is half 1/4 notes
         too, but at 83ms each they are obviously real streaming, whereas a
         130 BPM map's 1/4 is 115ms - not tapping anybody streams, so it is
         far more likely to be a 260 BPM map's 1/2.

    Condition 2 is deliberately derived from `burst_max_gap_ms` rather than
    being its own invented number: the same "too slow to be burst content"
    line, measured against 28 hand-labelled difficulties, does both jobs.

    Measured on 35 difficulties with known notation (Chug Jug at 118 and the
    11-diff MONTAGEM BATCHI set at 130, both halved; 23 correctly-notated maps
    from 160 to 250): 33/35 correct. Both misses are BATCHI's Easy and Normal,
    which contain no 1/4 content at all - there is nothing to detect, and
    nothing for the fold to affect either, so they cost nothing.

    Note this is a property of the MAPSET (its timing points), not the
    difficulty - but each diff is judged alone, since classify_diff() never
    sees its siblings. A quiet diff in a halved set may not look halved; that
    is harmless for exactly the reason above.

    Folds once, not repeatedly. Quartered notation is not a thing anyone does.
    """
    if not timing_points or len(objs) < 2:
        return False
    total = quarter = slow_quarter = 0
    for i in range(1, len(objs)):
        gap = objs[i][0] - objs[i - 1][0]
        # Same break cap as elsewhere - a gap this long is a section boundary,
        # not part of the map's rhythm.
        if not (0 < gap <= 2000):
            continue
        total += 1
        beat = usable_beat_ms(_beat_length_at(objs[i - 1][0], timing_points))
        if not beat:
            continue
        # Per-transition local beat, so a map with BPM changes is measured
        # against whatever tempo was actually in force at that point.
        if abs(beat / gap - 4.0) / 4.0 < 0.08:
            quarter += 1
            if gap > burst_max_gap_ms:
                slow_quarter += 1
    if not total or quarter / total < halved_quarter_share_min:
        return False
    return slow_quarter * 2 > quarter


def classify_diff(diff: DiffInfo, max_gap_ms=140.0, gap_consistency_tol=0.18,
                   tight_diam_ratio=1.35, spaced_diam_ratio=2.0,
                   burst_min=3, burst_max=9, stream_min=10,
                   jump_velocity_ratio=0.75, jump_pct_threshold=15.0,
                   jump_min_transitions=40, jump_gap_cap_ms=1000.0,
                   stream_pct_threshold=15.0,
                   run_wide_fraction_max=0.4, mean_diam_ratio_max=1.5,
                   cut_max_multiple=3.0, cut_max_dist_ratio=4.0,
                   burst_beat_fraction_max=0.4, doubled_half_share_min=0.25,
                   max_plausible_bpm=300.0,
                   burst_max_gap_ms=105.0, halved_quarter_share_min=0.15,
                   section_ms=2000.0, section_dominance=0.5, section_min_transitions=4,
                   hybrid_section_min=0.15, hybrid_balance_min=0.5):
    """
    Terminology. Mostly osu!'s official beatmap tags, with two deliberate
    departures called out where they occur - see `burst` and `cutstream`:
      - burst  : 3-9 note run. Three notes really is enough - short 3-note
        bursts are everywhere in jump, aim-control and flow-aim maps, and
        calling those "not a burst" because they're under five doesn't match
        how the pattern is actually talked about.

        This IS a departure from the official tag, which defines
        `streams/bursts` as 5-9 notes and splits the shorter ones off into
        `streams/doubles` (2) and `streams/quads` (4) - leaving triples with
        no tag at all. The choice of 3 is deliberate; the point is that it is
        a choice, not a match.
      - stream : 10+ note run
      - spaced stream : a stream where notes don't overlap but spacing/rhythm
        stays consistent - still a stream, not a jump.
      - cutstream : a stream that is split by a gap but still tapped as one
        stream - two 5-note groups inside the same tapping window make a
        10-note cutstream, not two bursts. It is a TIMING property, not a
        spacing one.

        Worth being precise about, because three sources disagreed. The
        official `streams/cutstreams` tag says "streams in which the SPACING
        of certain notes is much larger than the rest", and this docstring
        used to repeat that - but the implementation had already moved to the
        timing definition, and the timing one is what the community means and
        what the user confirmed. The official wording is just vague. The code
        is right; this text was the stale part.
      - jump   : wide, irregular spacing between consecutive objects. This is
        an independent, spacing-only property - a map can be jump-heavy at
        any snap speed and can co-occur with bursts/streams (jump bursts,
        spaced streams, etc). It is NOT mutually exclusive with bursts/streams.

    SPEED IS JUDGED IN ABSOLUTE MILLISECONDS, not as a ratio to the map's
    stored BPM. This replaces an earlier snap_ratio test and is the single
    biggest accuracy change in here.

    The old test accepted any gap <= 0.55 beats, loosened from ~0.3 to cope
    with maps authored at deliberately doubled BPM (where a true 1/4 stream
    note sits at 1/2 of the stored tempo). Measured over a 400-difficulty
    sample of a real library, that cutoff was letting through 48% of its
    transitions at 1/2 snap, with a clear second population at 150-174ms per
    note - which is a ~90-100 BPM stream, i.e. ordinary 1/2 tapping, not a
    stream at all. Doubled-BPM maps are a niche; they were not half the
    library.

    Working in milliseconds sidesteps the whole problem, because it never
    consults the stored tempo. Stream BPM is just 15000 / ms_per_note, so
    max_gap_ms = 140 is "roughly a 107 BPM stream or faster".

    That is still true, but it is not SUFFICIENT, which is what the rhythm
    gate further down now handles. Milliseconds cannot distinguish a burst
    from a fast map's own pulse: at 240 BPM an ordinary 1/2 tap is 125ms and
    clears this cap, so every high-BPM jump map grew bursts out of its plain
    tapping. The gate asks the question this cap can't - is the run a step UP
    from the beat it sits on - and is deliberately confined to the 110-140ms
    band where notation could mislead, so the "never trust the stored tempo"
    principle above still holds everywhere it was actually load-bearing.

    The second gate is CONSISTENCY: every gap in a run must stay within
    gap_consistency_tol of the run's running mean. A real stream does not
    change tapping speed halfway through, but 39% of runs under the old
    consecutive-fast-transitions rule mixed 1/4 and 1/2 gaps internally.
    Requiring consistency is what makes it safe to keep the speed cutoff
    generous - the two gates together reject far more junk than either alone,
    so neither has to be strict enough to start eating true positives.

    Runs are built purely from TIMING (is this still being consecutively
    tapped fast?). Spacing is then used to judge what KIND of run it is,
    using THREE tiers rather than a single cutoff:
      - tight  : dist <= tight_diam_ratio * diameter (stacked/near-overlapping)
      - spaced : dist <= spaced_diam_ratio * diameter (non-overlapping but
                 still a deliberate, readable stream/finger-control spacing -
                 does NOT count against a run being a stream/burst)
      - jump-wide : beyond spaced_diam_ratio * diameter (genuinely far apart)

    A run is only discarded as "actually a jump run" if too much of it is
    jump-wide - spaced (but not jump-wide) transitions are treated as normal
    stream/burst content.

    SPACING IS MEASURED FROM THE PREVIOUS OBJECT'S END, not its start. For
    circles those are the same point, but sliders are ~30% of a typical
    library, and a long slider whose tail sits right next to the following
    note otherwise reads as a full-screen jump. See _slider_end().

    CUT RUNS ARE REJOINED before lengths are judged - see below. A stream
    interrupted by a skipped beat is still a stream, which is what the
    "cutstreams count as streams" rule is supposed to mean.

    NM only. `rate` stays in the arithmetic below at a fixed 1.0 rather than
    being deleted, so restoring mods later is a matter of feeding it again.
    """
    objs = diff.objs
    # Set regardless of what follows - a diff too sparse to classify still
    # has a real note count, and the CSV should say so rather than leaving
    # the dataclass default (0) in place. That was the original form of this
    # bug: total_note_count silently read 0 for any diff under the old
    # burst_min floor.
    diff.total_note_count = len(objs)
    if len(objs) < MIN_OBJECTS_TO_CLASSIFY:
        return diff
    span_ms = objs[-1][0] - objs[0][0]
    if span_ms <= 0 or len(objs) / (span_ms / 1000.0) > MAX_SUSTAINED_NOTES_PER_SEC:
        # Mirrors is_junk_diff() - a second line of defense for direct
        # classify_diff() callers that bypass the scan-path filtering (see
        # is_junk_diff's docstring). Left with default/blank pattern flags,
        # same as the too-few-objects case above.
        return diff

    rate, eff_cs = 1.0, diff.circle_size
    radius = 54.4 - 4.48 * eff_cs
    diam = max(radius * 2, 1.0)

    # Per transition, three quantities:
    #   tap_gap   - start-to-start, i.e. the interval between taps. This is
    #               the rhythm, and it already accounts for slider duration
    #               because a slider's stored time is its head.
    #   move_time - previous object's END to this object's start, i.e. how
    #               long the cursor actually has to travel. Only differs from
    #               tap_gap on sliders, and it's the honest denominator for a
    #               velocity metric.
    #   dist      - previous object's END position to this object's start.
    transitions = []
    for i in range(1, len(objs)):
        p = objs[i - 1]
        c = objs[i]
        tap_gap = (c[0] - p[0]) / rate
        raw_move = (c[0] - p[1]) / rate
        move_time = max(raw_move, 1.0)
        dist = ((c[2] - p[4]) ** 2 + (c[3] - p[5]) ** 2) ** 0.5
        # A negative raw_move means the previous slider's own computed tail
        # time is AFTER the next object's start - the two are overlapping in
        # time, not sequential. That happens on real (if unusual) maps with
        # an SV change landing on a slider's own timestamp, or similar. When
        # it does, "how far the cursor moved in how long" is meaningless: the
        # 1ms floor turns a merely-large distance into a manufactured
        # near-infinite velocity, which is what was hijacking jump detection
        # here - not the slider-duration math, which checks out against the
        # file's own timing data (see AGENTS.md).
        overlapping = raw_move < 0
        transitions.append((tap_gap, move_time, dist, overlapping))

    # Notation is a property of the whole map, so decide it once here rather
    # than re-deriving it for every run. Uses raw (un-rate-adjusted) times
    # deliberately: whether a mapper halved the tempo is a fact about the
    # file, and DT doesn't change it.
    halved_notation = looks_like_halved_notation(
        objs, diff.timing_points, burst_max_gap_ms, halved_quarter_share_min)
    doubled_notation = (not halved_notation) and looks_like_doubled_notation(
        objs, diff.timing_points, burst_max_gap_ms, doubled_half_share_min,
        max_plausible_bpm)

    # --- which transitions are gameplay at all ------------------------------
    # Gaps longer than the cap are breaks and section boundaries, not
    # gameplay; leaving them in the denominator quietly diluted dense maps and
    # inflated sparse ones. A temporally overlapping transition (see above)
    # has no meaningful velocity to measure, so it's excluded the same way
    # rather than letting the 1ms floor manufacture a jump out of it.
    #
    # This set is the denominator every coverage figure is measured against,
    # and - since the run classification below partitions it - the thing that
    # makes those figures comparable to each other.
    eligible = [i for i, (tap_gap, _, _, overlapping) in enumerate(transitions)
                if tap_gap <= jump_gap_cap_ms and not overlapping]
    counted_gaps = len(eligible)

    # --- run building: fast AND rhythmically consistent ---------------------
    raw_runs = []
    cur = []
    cur_sum = 0.0
    for idx, (tap_gap, _, _, _) in enumerate(transitions):
        if tap_gap > max_gap_ms:
            if cur:
                raw_runs.append(cur)
            cur, cur_sum = [], 0.0
            continue
        if not cur:
            cur, cur_sum = [idx], tap_gap
            continue
        ref = cur_sum / len(cur)
        if ref > 0 and abs(tap_gap - ref) / ref > gap_consistency_tol:
            # Speed changed mid-run: close it here and start a new one.
            raw_runs.append(cur)
            cur, cur_sum = [idx], tap_gap
        else:
            cur.append(idx)
            cur_sum += tap_gap
    if cur:
        raw_runs.append(cur)

    def mean_gap(indices):
        return sum(transitions[j][0] for j in indices) / len(indices) if indices else 0.0

    # --- rejoin cut runs ---------------------------------------------------
    # A stream broken by a skipped beat arrives here as two separate runs
    # with one over-long transition between them. If that transition is a
    # whole-number multiple of the note gap (2x, 3x - i.e. notes are missing
    # from an otherwise steady rhythm) and both sides are at the same speed,
    # it's one cut stream rather than two bursts. Without this, a 12-note
    # stream with a single gap in the middle came out as two 6-note bursts
    # and got filed under Bursts.
    merged_runs = []  # (all_transition_indices, set_of_cut_indices)
    i = 0
    while i < len(raw_runs):
        run = list(raw_runs[i])
        cuts = set()
        while i + 1 < len(raw_runs):
            nxt = raw_runs[i + 1]
            between = nxt[0] - 1
            if between != run[-1] + 1:
                break
            note_gap = mean_gap(run)
            if note_gap <= 0:
                break
            mult = transitions[between][0] / note_gap
            if not (2 <= round(mult) <= cut_max_multiple):
                break
            if abs(mult - round(mult)) > gap_consistency_tol:
                break  # not a clean skipped-note gap, just a pause
            if abs(mean_gap(nxt) - note_gap) / note_gap > gap_consistency_tol:
                break  # the other side is at a different speed
            # A cut spans what would normally be TWO note-hops merged into
            # one (the skipped note sits between them), so up to roughly
            # 2x the normal "still readable, not a jump" ceiling is
            # expected even for a genuine skipped-note stream. Real
            # library check (ai-classification branch): 1979 maps had at
            # least one "cut" whose distance was >3x a hit circle diameter,
            # some past 9x - visually confirmed on Night of Knights [TAG4]
            # (a well-known real stream map) that a 6.8x-diameter "cut" is
            # actually two separate stream clusters on opposite sides of
            # the playfield joined by a genuine full-screen jump, not one
            # continuous stream with a quietly skipped note. Reject the
            # merge rather than disguise a real jump as a timing artifact.
            if transitions[between][2] > diam * cut_max_dist_ratio:
                break
            run = run + [between] + list(nxt)
            cuts.add(between)
            i += 1
        merged_runs.append((run, cuts))
        i += 1

    bursts = []
    streams = []
    cutstreams = 0
    # Transition indices claimed by a burst or a stream run. Everything the
    # runs don't claim is offered to the jump test below, so each gameplay
    # transition ends up described exactly once instead of by two independent
    # passes that could both count it. See "one transition, one label" in
    # AGENTS.md.
    claimed_burst = set()
    claimed_stream = set()
    for run, cuts in merged_runs:
        length = len(run) + 1  # transitions -> note count
        if length < burst_min:
            continue
        # Spacing is judged over the run's own notes. The cut junctions are
        # excluded: the jump across a cut is a property of the cut, not of
        # the stream, and counting it would push cut streams over the
        # wide-fraction cutoff that rejoining them was meant to survive.
        spacing = [j for j in run if j not in cuts]
        if not spacing:
            continue
        wide_fraction = sum(1 for j in spacing if transitions[j][2] > diam * spaced_diam_ratio) / len(spacing)
        mean_dist_ratio = sum(transitions[j][2] for j in spacing) / len(spacing) / diam
        if wide_fraction > run_wide_fraction_max:
            continue  # too much genuinely jump-wide spacing - this is a jump run, not a burst/stream
        if mean_dist_ratio > mean_diam_ratio_max:
            # Individual transitions can dodge the wide-fraction cutoff by
            # chance (e.g. a wide-angle jump shape occasionally swings back
            # close to a previous point), but a real stream/burst - even a
            # "spaced" one - still averages close to circle-diameter scale
            # across the whole run. A run whose AVERAGE spacing is this wide
            # is a jump pattern that happens to be on a fast snap, not a
            # stream, regardless of what fraction of individual transitions
            # technically dodged the wide-fraction check.
            continue
        # --- rhythm gate: is this run a FAST SNAP, or just the map's pulse? --
        # The absolute max_gap_ms cap alone can't tell those apart. At 240 BPM
        # a plain 1/2 tap is 125ms, comfortably under the 140ms cap, so every
        # ordinary 1/2 jump pattern in a high-BPM map arrived here looking
        # like a burst. That is the single biggest source of false bursts:
        # across the user's mislabelled set, eleven "Jumps with bursts"
        # verdicts on maps a player hears no bursts in were ALL 1/2-snap runs
        # at 120-135ms in 223-250 BPM maps.
        #
        # A burst is a rhythmic step UP from the map's own pulse, so judge the
        # run against the beat it sits on: at most burst_beat_fraction_max of
        # a beat per note. 0.4 admits 1/4 (0.25) and 1/3 (0.33) - both real
        # burst rhythms - and rejects 1/2 (0.5) with 20% headroom for timing
        # that isn't perfectly snapped.
        #
        # But the stored tempo is NOT authoritative about snap (the reason the
        # original design refused to consult it at all), so the gate is scoped
        # to where notation can actually mislead:
        #
        #   - Runs at or under burst_always_fast_ms skip it entirely. At 110ms
        #     a note that is a ~136 BPM stream - fast enough that it is burst
        #     tapping whatever the file calls the snap. This is what keeps
        #     DOUBLED notation working: a 200 BPM song written as 400 taps its
        #     1/4 at 75ms, which the file calls a 1/2, and it sails through on
        #     speed alone exactly as it did before this gate existed.
        #   - HALVED notation is folded back out before the fraction is taken,
        #     which is the other direction and the one speed can't rescue.
        #     Whether a map is halved is decided from its own notes - see
        #     looks_like_halved_notation(), computed once above.
        #
        # So the gate only decides runs in the 110-140ms band, which is
        # precisely the ambiguous stretch: too slow to be self-evidently a
        # burst, fast enough to slip under the absolute cap.
        #
        # Measured over `spacing`, not `run`: a cut junction spans a skipped
        # note, so its gap is 2-3x the run's own, and averaging it in would
        # push a genuine cutstream over the line that rejoining it was meant
        # to survive. Same exclusion, same reason, as the spacing checks above.
        run_gap = mean_gap(spacing)
        beat_ms = usable_beat_ms(
            _beat_length_at(objs[run[0]][0], diff.timing_points)) / rate
        if halved_notation:
            beat_ms /= 2.0
        elif doubled_notation:
            beat_ms *= 2.0
        if beat_ms > 0 and run_gap > beat_ms * burst_beat_fraction_max:
            continue
        if burst_min <= length <= burst_max:
            # A burst has to be genuinely FAST, not merely a fast snap. The
            # rhythm gate above asks "is this a step up from the map's pulse",
            # which a slow song's honest 1/4 passes: at 125 BPM a 1/4 is 120ms
            # and at 130 BPM it is 115ms, both comfortably under max_gap_ms.
            # Reported on "Jump & Stream Practice [Arastelia's Dizzy]" (125
            # BPM) and the MONTAGEM BATCHI set (130 BPM) - real 1/4 runs, and
            # not bursts, because 115-120ms per note is not burst tapping.
            #
            # Across all 28 hand-labelled difficulties the separation is
            # clean and it is on SPEED, not snap and not spacing: the maps
            # that do have bursts run 75-94ms per note, the ones that don't
            # run 115-134ms, with nothing in between. Confirmed visually via
            # osu_visualizer_preview.py - the disputed clusters look
            # burst-SHAPED (which is why every spacing check passes them),
            # they are just too slow to be bursts.
            #
            # Deliberately scoped to bursts rather than lowering max_gap_ms,
            # even though on this evidence a ~105ms global cap would also
            # score 28/28. max_gap_ms builds the runs that streams are found
            # in too, so lowering it would silently stop calling ~130 BPM
            # stream maps streams - and every one of these 28 labels is about
            # bursts. There is no labelled stream data here to justify that,
            # so it isn't being changed on a guess.
            if run_gap > burst_max_gap_ms:
                continue
            bursts.append(length)
            claimed_burst.update(run)
        elif length >= stream_min:
            streams.append(length)
            claimed_stream.update(run)
            # A cutstream is now literally a stream that was cut - i.e. one
            # we had to rejoin across a skipped beat. The previous definition
            # keyed on spacing variation instead, which is a different
            # property wearing the same name.
            if cuts:
                cutstreams += 1

    # --- jump density, over what the runs did NOT claim ---------------------
    # Velocity is in diameters per 100ms. That unit is BPM-independent, which
    # is the whole point; note it is NOT the old diameters-per-beat, so the
    # default threshold was re-derived rather than carried over.
    #
    # Runs are evaluated first and win ties by construction: a transition
    # inside a run that survived every spacing check is stream/burst content,
    # and shouldn't also be counted as a jump. In practice the two rarely
    # disagree - a surviving run averages under mean_diam_ratio_max while the
    # jump test needs more than spaced_diam_ratio - but "rarely" was doing
    # load-bearing work in a comparison that is supposed to be between
    # mutually exclusive alternatives, and now it isn't.
    #
    # A run REJECTED for being too jump-spaced isn't discarded either: its
    # transitions land here, and the wide ones are counted as the jumps they
    # are. That is the evidence that used to vanish.
    jump_idx = set()
    for i in eligible:
        if i in claimed_burst or i in claimed_stream:
            continue
        _, move_time, dist, _ = transitions[i]
        norm_dist = dist / diam
        # Require BOTH genuinely far spacing AND a high distance/time ratio.
        # Velocity alone isn't enough: a legitimate tight stream has a tiny
        # time-per-note by definition, which inflates distance/time even when
        # the actual spacing is small - that was causing real streams to get
        # flagged as mostly jumps.
        if dist > diam * spaced_diam_ratio and (norm_dist / (move_time / 100.0)) > jump_velocity_ratio:
            jump_idx.add(i)
    jump_count = len(jump_idx)

    # Coverage, all three on the one basis that makes them comparable: share
    # of gameplay transitions. These partition `eligible`, so they sum to at
    # most 1.0 and can be compared directly rather than pairwise-and-ordered.
    eligible_set = set(eligible)
    diff.burst_transitions = len(claimed_burst & eligible_set)
    diff.stream_transitions = len(claimed_stream & eligible_set)
    diff.jump_transitions = jump_count

    # Where in the map each pattern lives, not just how much of it there is.
    label = {}
    for i in claimed_burst & eligible_set:
        label[i] = "b"
    for i in claimed_stream & eligible_set:
        label[i] = "s"
    for i in jump_idx:
        label[i] = "j"
    (diff.active_sections, diff.stream_sections,
     diff.burst_sections, diff.jump_sections) = section_pattern_counts(
        objs, eligible, label, section_ms, section_dominance, section_min_transitions)

    diff.burst_count = len(bursts)
    diff.stream_count = len(streams)
    diff.cutstream_count = cutstreams
    diff.max_burst_len = max(bursts, default=0)
    diff.max_stream_len = max(streams, default=0)
    diff.jump_pct = (jump_count / counted_gaps * 100) if counted_gaps else 0.0
    diff.counted_gaps = counted_gaps
    diff.burst_note_total = sum(bursts)
    diff.stream_note_total = sum(streams)
    diff.total_note_count = len(objs)

    diff.has_bursts = len(bursts) > 0
    # A stream has to cover a real share of the map, not merely exist.
    #
    # has_jumps has always required clearing a threshold while has_streams was
    # pure presence, and that asymmetry was the bug: a NiNo-style jump map
    # with ONE 10-note run in 402 notes (2.5% coverage) came out as a stream
    # map. It escaped the stream-vs-jump coverage comparison because its
    # jump_pct was 14.5%, a half point under jump_pct_threshold - so
    # has_jumps was false, there was nothing to lose to, and presence won.
    #
    # Measured separation is wide: across ten NiNo mapsets the stream-flagged
    # runs cover 2.5-12.9% of their maps, while known real stream maps sit at
    # 55-96%. 15% sits in the gap with room on both sides.
    stream_coverage_pct = (diff.stream_note_total / diff.total_note_count * 100) \
        if diff.total_note_count else 0.0
    diff.has_streams = len(streams) > 0 and stream_coverage_pct >= stream_pct_threshold
    # Require a floor of actual gameplay before a percentage means anything.
    # Without it a 30-note diff with 5 wide transitions clears a 15% threshold
    # and gets called a jump map on the strength of five jumps.
    diff.has_jumps = counted_gaps >= jump_min_transitions and diff.jump_pct >= jump_pct_threshold
    diff.has_cutstreams = cutstreams > 0

    # Streams and jumps in DIFFERENT PARTS of the map, in comparable amounts.
    # Two conditions:
    #   - each owns at least hybrid_section_min of the map's sections, so
    #     neither is a stray passage;
    #   - and they are BALANCED - the smaller owns at least hybrid_balance_min
    #     of what the larger does. Without this a flat threshold calls a map
    #     with 61% jump sections and 19% stream sections a hybrid, when it is
    #     plainly a jump map that has a stream section in it. "A mix" means
    #     comparable amounts, not merely some of each.
    # Computed here rather than in category_of() for the same reason
    # has_streams/has_jumps are: it is a property of the map measured from its
    # notes, and category_of()'s job is only to rank what is present.
    if diff.active_sections and diff.has_streams and diff.has_jumps:
        s_share = diff.stream_sections / diff.active_sections
        j_share = diff.jump_sections / diff.active_sections
        bigger = max(s_share, j_share)
        balance = (min(s_share, j_share) / bigger) if bigger else 0.0
        diff.has_hybrid = (s_share >= hybrid_section_min
                           and j_share >= hybrid_section_min
                           and balance >= hybrid_balance_min)

    return diff


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------

class ScanCancelled(Exception):
    """Raised to unwind out of a scan/classify run when the user cancels it."""
    pass


def wait_if_paused(pause_event, cancel_event):
    """
    Blocks the calling thread while paused, without blocking forever if a
    cancel comes in during the pause. pause_event uses "set = running,
    cleared = paused" semantics - the same Event can just be flipped by a
    Pause/Resume button. Polls rather than doing a plain blocking wait() so
    a cancel request during a pause is still honored promptly.
    """
    if pause_event is None:
        return
    import time as _time
    while not pause_event.is_set():
        if cancel_event is not None and cancel_event.is_set():
            raise ScanCancelled()
        _time.sleep(0.1)


# Threads used to overlap per-file reads during a scan. Deliberately NOT tied
# to os.cpu_count(): these threads spend essentially all their time blocked in
# open()/read() with the GIL released, so the pool's job is hiding I/O
# LATENCY, not using cores, and core count says nothing about how many reads a
# storage stack will service at once. Tying it to CPUs was the original
# mistake - it capped a 12-core machine at 8 concurrent reads.
#
# Measured on a real osu!stable library (59,129 difficulties on a 7200rpm
# SATA HDD, Defender real-time scanning on), 4 cold 1000-diff slices per
# setting, alternating order so disk region and warm-up can't favour either:
#
#     8 workers (the old cap):  50, 51, 40, 43 diffs/sec  (median 46.6)
#    64 workers:                64, 62, 70, 70 diffs/sec  (median 66.7)
#
# Every 64-run beat every 8-run - a 1.43x speedup with no overlap between
# the two sets, taking that library from 21.1 to 14.8 minutes. 16 and 32 sat
# inside 8's noise band and 96 was erratic, so 64 is where the evidence
# actually is, not a round number picked for looking bold.
#
# ThreadPoolExecutor spawns threads lazily, so a small library never creates
# more threads than it has files.
SCAN_READ_WORKERS = 64

_OSU_MAGIC = b"osu file format"


def scan_folder(root, progress_cb=None, log_cb=None, on_parsed=None, cancel_event=None, pause_event=None):
    """
    Recursively scans `root` for beatmap data. Handles three source types
    in a single pass, auto-detected per file:

      - .osu files       : parsed directly (osu!stable Songs/ folder, or a
                            BeatmapExporter "folder" export)
      - .osz archives     : read in memory, no extraction needed
                            (BeatmapExporter "osz" export)
      - extensionless files : peeked for the "osu file format" magic header
                            and parsed if it matches. This lets you point
                            straight at osu!lazer's own `files/` storage
                            folder (%appdata%/osu/files on Windows,
                            ~/.local/share/osu/files on Linux/Mac) with
                            NO export step at all - lazer stores every
                            imported file (audio, images, .osu, skins) as a
                            SHA-256-named blob with no extension, so a cheap
                            first-bytes peek picks out just the beatmap
                            files without reading anything else in full.

    progress_cb, if given, is called as progress_cb(files_done, files_total)
    periodically so a GUI can show a live progress bar.
    log_cb, if given, receives human-readable status lines with more detail
    than the progress bar alone (file-type breakdown, periodic counts,
    timing, error summaries).
    on_parsed, if given, is called on each DiffInfo immediately after it's
    parsed, before it's appended to the results list. This is used to run
    classification inline (see run_pipeline) so raw per-note data can be
    freed right away instead of every diff in the whole library being held
    in memory at once until a separate classification pass runs later -
    that held-everything-in-memory pattern is what causes out-of-memory
    crashes on very large (100k+ diff) libraries.
    cancel_event, if given, is checked periodically (a threading.Event) -
    if set, raises ScanCancelled to unwind out of the scan promptly.
    """
    import zipfile
    import time
    from concurrent.futures import ThreadPoolExecutor

    def log(msg):
        if log_cb:
            log_cb(msg)

    def emit(diff):
        if on_parsed:
            on_parsed(diff)
        return diff

    def check_cancel():
        if cancel_event is not None and cancel_event.is_set():
            raise ScanCancelled()
        wait_if_paused(pause_event, cancel_event)

    t_start = time.time()
    log(f"Walking directory tree under {root} ...")
    osu_paths = []
    osz_paths = []
    peek_candidates = []
    folders = 0
    # The walk is the longest silent stretch of the whole run: on a 25k-folder
    # osu!stable Songs library it takes around three minutes on its own,
    # during which nothing used to be reported at all. The progress bar sat at
    # zero and the log said nothing, so the app looked frozen and people
    # reasonably concluded stable scanning simply didn't work. Report progress
    # while it happens - the total isn't knowable yet, hence total=None, which
    # tells a GUI to show an indeterminate/pulsing bar rather than a stuck one.
    if progress_cb:
        progress_cb(0, None)
    for dirpath, _, filenames in os.walk(root):
        check_cancel()
        folders += 1
        for fn in filenames:
            low = fn.lower()
            full = os.path.join(dirpath, fn)
            if low.endswith(".osu"):
                osu_paths.append(full)
            elif low.endswith(".osz"):
                osz_paths.append(full)
            elif "." not in fn:
                # No extension at all - candidate for lazer's hash-named blob store
                peek_candidates.append(full)
        if folders % 1000 == 0:
            found = len(osu_paths) + len(osz_paths)
            if progress_cb:
                progress_cb(folders, None)
            if log_cb and folders % 5000 == 0:
                log(f"  ... {folders} folders searched, {found} beatmap files found so far "
                    f"({time.time() - t_start:.0f}s)")

    t_walk = time.time()
    log(f"Directory walk done in {t_walk - t_start:.1f}s. "
        f"Found {len(osu_paths)} .osu files, {len(osz_paths)} .osz archives, "
        f"{len(peek_candidates)} extensionless files to check.")

    total = len(osu_paths) + len(osz_paths) + len(peek_candidates)
    done = 0
    results = []
    errors = []

    if osu_paths:
        log(f"Parsing {len(osu_paths)} .osu files...")
    for full in osu_paths:
        check_cancel()
        try:
            diff = parse_osu_file(full)
            if not is_junk_diff(diff):
                results.append(emit(diff))
        except Exception as e:
            errors.append((full, str(e)))
        done += 1
        if progress_cb and done % 25 == 0:
            progress_cb(done, total)
        if log_cb and done % 5000 == 0:
            log(f"  ... {done}/{len(osu_paths)} .osu files parsed")

    if osz_paths:
        log(f"Reading {len(osz_paths)} .osz archives...")
    for full in osz_paths:
        check_cancel()
        try:
            with zipfile.ZipFile(full, "r") as z:
                for name in z.namelist():
                    if not name.lower().endswith(".osu"):
                        continue
                    try:
                        raw = z.read(name)
                        display = f"{os.path.basename(full)}::{name}"
                        diff = parse_osu_bytes(raw, display_name=display, path=display)
                        if not is_junk_diff(diff):
                            results.append(emit(diff))
                    except Exception as e:
                        errors.append((f"{full}::{name}", str(e)))
        except Exception as e:
            errors.append((full, str(e)))
        done += 1
        if progress_cb and done % 5 == 0:
            progress_cb(done, total)
        if log_cb and done % 500 == 0:
            log(f"  ... {done - len(osu_paths)}/{len(osz_paths)} .osz archives read")

    if peek_candidates:
        log(f"Scanning {len(peek_candidates)} extensionless files for beatmap data "
            f"(this is where a lazer files/ folder spends most of its time)...")
    t_peek_start = time.time()
    matched = 0

    def _peek(full):
        """
        Read-and-parse one blob store candidate. Returns (diff, error) with
        diff None for anything that isn't a beatmap - the magic-header check
        rejects the great majority (audio, images, skin assets) after 24
        bytes, without reading the rest.
        """
        try:
            with open(full, "rb") as f:
                head = f.read(24)
            if not head.startswith(_OSU_MAGIC):
                return None, None
            with open(full, "rb") as f:
                raw = f.read()
            return parse_osu_bytes(raw, display_name=os.path.basename(full), path=full), None
        except Exception as exc:
            return None, str(exc)

    # Same reasoning as the thread pool in scan_stable_db, and the same shape
    # - see the long comment there. This loop is the single slowest phase of
    # any lazer scan (a 180k-file store is ~180k opens), and it is pure I/O
    # wait: measured at 23 files/sec serially on a mechanical drive, i.e.
    # over two hours, with the CPU essentially idle the whole time. The
    # stable path got this treatment on the assumption lazer was already
    # covered by the realm-reader helper - but the helper is optional (it
    # needs a .NET build), and without it every lazer user lands here.
    #
    # Bounded windows rather than one pool.map() over all 180k so cancel and
    # pause are still honored promptly, and so results stay in walk order.
    # emit()/results/errors stay on the calling thread: on_parsed runs
    # classification and mutates caller-owned state, and it is CPU-bound
    # anyway, so there is nothing to win by moving it and a data race to lose.
    peek_workers = SCAN_READ_WORKERS
    peek_window = peek_workers * 4
    peek_pool = ThreadPoolExecutor(max_workers=peek_workers)
    try:
        for start in range(0, len(peek_candidates), peek_window):
            check_cancel()
            batch = peek_candidates[start:start + peek_window]
            for full, (diff, err) in zip(batch, peek_pool.map(_peek, batch)):
                check_cancel()
                if err is not None:
                    errors.append((full, err))
                elif diff is not None and not is_junk_diff(diff):
                    results.append(emit(diff))
                    matched += 1
                done += 1
                if progress_cb and done % 200 == 0:
                    progress_cb(done, total)
                if log_cb and done % 20000 == 0:
                    checked = done - len(osu_paths) - len(osz_paths)
                    elapsed = time.time() - t_peek_start
                    rate = checked / elapsed if elapsed > 0 else 0
                    log(f"  ... {checked}/{len(peek_candidates)} checked, "
                        f"{matched} beatmap files found so far ({rate:.0f} files/sec)")
    finally:
        peek_pool.shutdown(wait=False)

    if progress_cb:
        progress_cb(total, total)

    t_end = time.time()
    log(f"Scan complete in {t_end - t_start:.1f}s. {len(results)} osu!standard difficulties found.")
    if errors:
        log(f"{len(errors)} files failed to read/parse:")
        for path, err in errors[:5]:
            log(f"  - {path}: {err}")
        if len(errors) > 5:
            log(f"  ... and {len(errors) - 5} more")

    return results, errors


# --------------------------------------------------------------------------
# osu!stable database (osu!.db)
# --------------------------------------------------------------------------
#
# Binary layout follows Piotrekol's CollectionManager, specifically
# StableOsuDatabaseReader.ReadNextBeatmap and OsuBinaryReader. That is the
# reference implementation for this format - the community wiki is vaguer and
# in one place actively misleading (it labels the two consecutive ints after
# the timing points "Difficulty ID" then "Beatmap ID", when the first is
# actually the beatmap/difficulty id and the second the set id).
#
# Reading this instead of walking Songs/ is a large win:
#   - no directory walk (~180s on a 25k-folder library)
#   - MD5 comes straight from the db, so no re-hashing every file
#   - PlayMode is known before opening anything, so non-osu!standard
#     difficulties are skipped without touching the disk
#   - ranked status, star rating and online id become available on stable,
#     which previously only the lazer path had

# Minimum osu!.db version CollectionManager supports. Older files laid the
# per-beatmap records out differently (they carried a leading record-size
# int), and rather than guess at a format we cannot test against, we refuse
# and fall back to the filesystem scan.
MIN_OSU_DB_VERSION = 20191105

# osu!stable's cached ranked status byte. These are NOT the same numbers the
# API or lazer's realm use - stable has its own encoding.
_STABLE_STATUS = {
    0: "unknown",
    1: "unsubmitted",
    2: "pending",      # covers pending/wip/graveyard - stable doesn't separate them
    3: "unknown",
    4: "ranked",
    5: "approved",
    6: "qualified",
    7: "loved",
}


class _OsuDbReader:
    """Little-endian reader for osu!'s binary db format."""

    def __init__(self, data):
        self.d = data
        self.i = 0

    def _take(self, n):
        if self.i + n > len(self.d):
            raise EOFError("osu!.db ended mid-record")
        v = self.d[self.i:self.i + n]
        self.i += n
        return v

    def u8(self):
        return self._take(1)[0]

    def boolean(self):
        return self._take(1)[0] != 0

    def i16(self):
        return struct.unpack("<h", self._take(2))[0]

    def i32(self):
        return struct.unpack("<i", self._take(4))[0]

    def i64(self):
        return struct.unpack("<q", self._take(8))[0]

    def f32(self):
        return struct.unpack("<f", self._take(4))[0]

    def f64(self):
        return struct.unpack("<d", self._take(8))[0]

    def string(self):
        """0x0b then a ULEB128 length then UTF-8 bytes; 0x00 means null."""
        marker = self.u8()
        if marker == 0x00:
            return ""
        if marker != 0x0b:
            raise ValueError(f"bad string marker {marker:#x} at offset {self.i - 1}")
        length = 0
        shift = 0
        while True:
            b = self.u8()
            length |= (b & 0x7F) << shift
            if not b & 0x80:
                break
            shift += 7
        return self._take(length).decode("utf-8", "replace")

    def skip(self, n):
        self.i += n

    def conditional(self):
        """
        osu!'s type-tagged value, used inside the star-rating tables. We only
        ever need to step over these, but the width depends on the tag, so
        they have to be decoded rather than skipped blind.
        """
        t = self.u8()
        if t == 1:
            return self.boolean()
        if t == 2:
            return self.u8()
        if t == 3:
            return struct.unpack("<H", self._take(2))[0]
        if t == 4:
            return struct.unpack("<I", self._take(4))[0]
        if t == 5:
            return struct.unpack("<Q", self._take(8))[0]
        if t == 6:
            return struct.unpack("<b", self._take(1))[0]
        if t == 7:
            return self.i16()
        if t == 8:
            return self.i32()
        if t == 9:
            return self.i64()
        if t == 10:
            return self._take(2).decode("utf-16-le", "replace")
        if t == 11:
            return self.string()
        if t == 12:
            return self.f32()
        if t == 13:
            return self.f64()
        if t == 14:
            return self._take(16)
        if t == 15:
            return self.i64()
        if t == 16 or t == 17:
            n = self.i32()
            return self._take(n) if n > 0 else None
        return None


def read_osu_db(path, log_cb=None, want_mode=0):
    """
    Reads osu!stable's osu!.db and yields one dict per difficulty.

    Opened read-only. osu! only flushes this file on a clean exit, so with the
    game running it may be slightly stale - the worst case is a recently
    imported map being missed, which is why the caller falls back to a
    filesystem scan when this yields nothing usable.

    want_mode filters by ruleset before anything is returned (0 = osu!std).
    """
    def log(msg):
        if log_cb:
            log_cb(msg)

    with open(path, "rb") as f:
        r = _OsuDbReader(f.read())

    version = r.i32()
    if version < MIN_OSU_DB_VERSION:
        raise ValueError(
            f"osu!.db version {version} is older than {MIN_OSU_DB_VERSION}; "
            f"its record layout differs and isn't supported")
    folder_count = r.i32()
    r.boolean()          # account unlocked
    r.i64()              # unlock date
    r.string()           # player name
    count = r.i32()
    log(f"osu!.db version {version}: {count} difficulties across {folder_count} folders.")

    for _ in range(count):
        # 9 strings: artist, artist unicode, title, title unicode, creator,
        # difficulty name, audio filename, md5, .osu filename
        fields = [r.string() for _ in range(9)]
        md5, osu_filename = fields[7], fields[8]
        diff_name = fields[5]
        title = fields[2] or fields[3]

        state = r.u8()
        r.skip(2 + 2 + 2)        # circle / slider / spinner counts
        r.skip(8)                # last edit time
        r.skip(4 * 4)            # AR, CS, HP, OD
        r.skip(8)                # slider velocity

        # Star ratings, one table per ruleset (std, taiko, ctb, mania).
        star_rating = None
        for mode_index in range(4):
            pairs = r.i32()
            for _ in range(pairs):
                mods = r.conditional()
                stars = r.conditional()
                # Nomod std rating is the one worth keeping.
                if mode_index == 0 and mods == 0 and isinstance(stars, float):
                    star_rating = round(stars, 2)

        r.skip(4 * 3)            # drain / total / preview time

        timing_points = r.i32()
        r.skip(timing_points * 17)   # double, double, bool

        map_id = r.i32()
        r.i32()                  # map set id
        r.i32()                  # thread id
        r.skip(4)                # 4 grade bytes
        r.skip(2)                # local offset
        r.skip(4)                # stack leniency
        mode = r.u8()
        r.string()               # source
        r.string()               # tags
        r.skip(2)                # online offset
        r.string()               # title font
        r.skip(1)                # unplayed
        r.skip(8)                # last played
        r.skip(1)                # is osz2
        folder = r.string()      # folder name inside Songs/
        r.skip(8)                # last sync
        r.skip(5)                # 5 disable-* booleans
        r.skip(4)                # last modification
        r.skip(1)                # mania scroll speed

        if want_mode is not None and mode != want_mode:
            continue
        if not folder or not osu_filename:
            continue

        yield {
            "folder": folder,
            "filename": osu_filename,
            "md5": md5,
            "title": title,
            "diff_name": diff_name,
            "map_id": map_id if map_id > 0 else None,
            "star_rating": star_rating,
            "ranked_status": _STABLE_STATUS.get(state, "unknown"),
        }


def default_stable_songs_dir(install_dir):
    """Songs/ lives next to osu!.db in a stable install."""
    return os.path.join(install_dir, "Songs")


def find_stable_db(folder):
    """
    Locates osu!.db given either a stable install folder or its Songs/ folder,
    since people point the tool at both. Returns (db_path, songs_dir) or None.
    """
    if not folder or not os.path.isdir(folder):
        return None
    candidates = [folder, os.path.dirname(os.path.normpath(folder))]
    for base in candidates:
        db = os.path.join(base, "osu!.db")
        songs = os.path.join(base, "Songs")
        if os.path.isfile(db) and os.path.isdir(songs):
            return db, songs
    return None


def scan_stable_db(db_path, songs_dir, progress_cb=None, log_cb=None, on_parsed=None,
                    cancel_event=None, pause_event=None):
    """
    Fast path for osu!stable: take the file list from osu!.db instead of
    walking Songs/.

    Returns (results, errors), or None if the db can't be used - callers fall
    back to scan_folder() in that case.
    """
    import time
    from concurrent.futures import ThreadPoolExecutor

    def log(msg):
        if log_cb:
            log_cb(msg)

    t0 = time.time()
    log(f"Found osu!.db - reading the beatmap list from it instead of walking {songs_dir} "
        f"(much faster, and skips non-osu!standard difficulties without opening them).")
    try:
        entries = list(read_osu_db(db_path, log_cb=log_cb, want_mode=0))
    except (OSError, ValueError, EOFError, struct.error) as e:
        log(f"Couldn't read osu!.db ({e}) - falling back to scanning {songs_dir}.")
        return None

    if not entries:
        log("osu!.db listed no osu!standard difficulties - falling back to a filesystem scan.")
        return None

    log(f"osu!.db gave {len(entries)} osu!standard difficulties in {time.time() - t0:.1f}s. Parsing them now.")

    results = []
    errors = []
    missing = 0

    def check_cancel():
        if cancel_event is not None and cancel_event.is_set():
            raise ScanCancelled()
        wait_if_paused(pause_event, cancel_event)

    def _read(e):
        full = os.path.join(songs_dir, e["folder"], e["filename"])
        try:
            return parse_osu_file(full), None
        except OSError:
            return None, "missing"
        except Exception as exc:
            return None, str(exc)

    # A single open()+read() here is a few KB, but on a real Songs/ folder
    # each one still costs tens to hundreds of ms one at a time - real-time
    # antivirus scanning each newly-opened file and per-file seeks on a
    # mechanical drive both dominate over the actual (tiny) parse work.
    # open()/read() release the GIL while blocked on I/O, so a small thread
    # pool overlaps that wait across several files at once instead of
    # paying it file-by-file - this is the whole reason stable was slower
    # than lazer despite using the same fast-path shape (both were fully
    # serial; lazer's realm-reader helper just front-loads the equivalent
    # work into one subprocess call instead of N python-level opens).
    # Submitted in bounded windows rather than one big pool.map() over the
    # whole library so cancel actually stops promptly (map() would otherwise
    # queue every remaining file's read immediately).
    workers = SCAN_READ_WORKERS
    window = workers * 4
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        i = 0
        for start in range(0, len(entries), window):
            check_cancel()
            batch = entries[start:start + window]
            for e, (diff, err) in zip(batch, pool.map(_read, batch)):
                # Per item, not just per window. The window is 4x the worker
                # count, so raising SCAN_READ_WORKERS to 64 stretched it from
                # 32 files to 256 - about four seconds of "Cancel does
                # nothing" at measured throughput, where it used to be under
                # one. Checking here costs one predictable call per file and
                # keeps cancel/pause as responsive as they were before.
                check_cancel()
                if err == "missing":
                    missing += 1
                elif err is not None:
                    full = os.path.join(songs_dir, e["folder"], e["filename"])
                    errors.append((full, err))

                if not is_junk_diff(diff):
                    # osu!.db already knows these, so take them rather than
                    # recomputing or leaving them blank as a folder scan would.
                    diff.ranked_status = e["ranked_status"]
                    diff.star_rating = e["star_rating"]
                    diff.online_id = e["map_id"]
                    if e["md5"]:
                        diff.version_hash = e["md5"]
                    results.append(diff)
                    if on_parsed:
                        on_parsed(diff)

                i += 1
                if progress_cb and i % 25 == 0:
                    progress_cb(i, len(entries))
                if log_cb and i % 5000 == 0:
                    log(f"  ... {i}/{len(entries)} parsed")
    finally:
        # wait=False so a cancel unwinds now rather than after every in-flight
        # read completes. Workers only read and parse; results/errors are
        # appended on this thread alone, so abandoning them races nothing.
        pool.shutdown(wait=False)

    if progress_cb:
        progress_cb(len(entries), len(entries))
    log(f"Stable scan complete in {time.time() - t0:.1f}s. {len(results)} difficulties classified.")
    if missing:
        log(f"{missing} files listed in osu!.db are no longer on disk (deleted outside osu!, "
            f"or the db is stale because osu! hasn't been closed since).")
    if errors:
        log(f"{len(errors)} files failed to parse.")
    return results, errors


def default_realm_reader_path():
    """
    Looks for the compiled realm-reader helper next to this script (source
    checkout) or next to the running executable (PyInstaller build). Returns
    None if not found - callers should fall back to the filesystem scan.
    """
    exe_name = "realm-reader.exe" if sys.platform.startswith("win") else "realm-reader"
    search_dirs = []
    try:
        search_dirs.append(os.path.dirname(os.path.abspath(sys.argv[0])))
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        search_dirs.append(os.path.dirname(sys.executable))
    search_dirs.append(os.path.dirname(os.path.abspath(__file__)))

    for d in search_dirs:
        for candidate in (
            # Next to the app - how a downloaded release used to be laid out.
            os.path.join(d, exe_name),
            # Release layout: the helper and its ~190 runtime DLLs live in
            # their own folder so the release root stays navigable.
            os.path.join(d, "realm-reader", exe_name),
            # Working from source: publishing into realm-reader/ would bury
            # Program.cs under those same DLLs, so the documented build sends
            # them here instead (and .gitignore knows about it).
            os.path.join(d, "realm-reader-dist", exe_name),
            # A plain `dotnet build` leaves it here, which saves anyone
            # hacking on the helper from having to publish just to test.
            os.path.join(d, "realm-reader", "bin", "Release", "net8.0", "win-x64", exe_name),
        ):
            if os.path.isfile(candidate):
                return candidate
    return None


def scan_lazer_realm(data_dir, progress_cb=None, log_cb=None, on_parsed=None, helper_path=None, cancel_event=None, pause_event=None):
    """
    Fast path for osu!lazer libraries: uses the realm-reader helper (if
    available) to resolve every .osu file's on-disk path directly from
    client.realm, then parses just those files - skipping the slower
    filesystem walk-and-peek of the entire files/ blob store.

    Returns (results, errors) on success, or None if the fast path isn't
    usable (helper missing, failed, or found nothing) - callers should fall
    back to scan_folder() over the files/ subfolder in that case.
    """
    import subprocess
    import tempfile
    import time

    def log(msg):
        if log_cb:
            log_cb(msg)

    realm_path = os.path.join(data_dir, "client.realm")
    if not os.path.isfile(realm_path):
        return None

    helper = helper_path or default_realm_reader_path()
    if not helper:
        log("realm-reader helper not found - falling back to filesystem scan of files/ "
            "(this works fine, just slower on large libraries).")
        return None

    log(f"Found client.realm - trying fast path via realm-reader ({helper})...")
    with tempfile.NamedTemporaryFile(mode="r", suffix=".txt", delete=False, encoding="utf-8") as tmp:
        out_path = tmp.name

    t_start = time.time()
    # 10 minutes, not 120s: a freshly-built/downloaded self-contained exe can
    # take a long time to actually START on Windows (antivirus commonly does
    # real-time scanning of a new native binary + its DLLs on first launch),
    # which is unrelated to how long the actual realm read takes once it's
    # running - `dotnet run` doesn't hit this because it's already-trusted,
    # already-JIT'd build artifacts, which is why a published exe can time
    # out even when `dotnet run` against the same code works fine.
    REALM_READER_TIMEOUT = 600
    try:
        proc = subprocess.run([helper, realm_path, out_path], capture_output=True, text=True,
                               timeout=REALM_READER_TIMEOUT)
    except subprocess.TimeoutExpired:
        log(f"realm-reader didn't finish within {REALM_READER_TIMEOUT}s - falling back to filesystem scan. "
            "This is often antivirus scanning a freshly-built/downloaded exe on first run rather than a "
            "real hang; if it keeps happening, try adding an exclusion for the app's folder, or just let "
            "the fallback scan run (it works, just slower).")
        return None
    except Exception as e:
        log(f"realm-reader failed to run ({e}) - falling back to filesystem scan.")
        return None

    if proc.returncode != 0:
        log(f"realm-reader exited with an error - falling back to filesystem scan. Details:")
        if proc.stderr:
            log(proc.stderr.strip())
        return None

    if proc.stderr:
        log(proc.stderr.strip())
    log(f"realm-reader finished in {time.time() - t_start:.1f}s.")

    try:
        with open(out_path, "r", encoding="utf-8") as f:
            entries = []
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                # Current format:
                #   "path\tranked_status\tstar_rating\tonline_id"
                # Older compiled helpers emit fewer columns (down to a bare
                # path) - all handled for backward compatibility, since the
                # helper ships as a prebuilt binary and may lag the Python.
                parts = line.split("\t")
                path_part = parts[0]
                status_part = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
                star_part = None
                if len(parts) > 2 and parts[2].strip() and parts[2].strip().lower() != "unknown":
                    try:
                        star_part = float(parts[2].strip())
                    except ValueError:
                        star_part = None
                online_part = None
                if len(parts) > 3 and parts[3].strip() and parts[3].strip().lower() != "unknown":
                    try:
                        online_part = int(parts[3].strip())
                    except ValueError:
                        online_part = None
                entries.append((path_part, status_part, star_part, online_part))
    except OSError:
        entries = []
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass

    if not entries:
        log("realm-reader found no beatmap paths - falling back to filesystem scan.")
        return None

    log(f"realm-reader resolved {len(entries)} .osu files directly - parsing them now (no filesystem walk needed).")

    def emit(diff):
        if on_parsed:
            on_parsed(diff)
        return diff

    results = []
    errors = []
    for i, (full, ranked_status, star_rating, online_id) in enumerate(entries):
        if cancel_event is not None and cancel_event.is_set():
            raise ScanCancelled()
        wait_if_paused(pause_event, cancel_event)
        try:
            diff = parse_osu_file(full)
            if not is_junk_diff(diff):
                diff.ranked_status = ranked_status
                diff.star_rating = star_rating
                diff.online_id = online_id
                results.append(emit(diff))
        except Exception as e:
            errors.append((full, str(e)))
        if progress_cb and (i + 1) % 25 == 0:
            progress_cb(i + 1, len(entries))
        if log_cb and (i + 1) % 5000 == 0:
            log(f"  ... {i + 1}/{len(entries)} parsed")

    if progress_cb:
        progress_cb(len(entries), len(entries))

    log(f"Fast-path scan complete. {len(results)} osu!standard difficulties found.")
    if errors:
        log(f"{len(errors)} files failed to parse.")

    return results, errors


def default_lazer_files_dir():
    """Best-guess default location of osu!lazer's files/ blob store per OS."""
    data_dir = default_lazer_data_dir()
    return os.path.join(data_dir, "files") if data_dir else None


def resolve_lazer_storage(data_dir, log_cb=None):
    """
    Follows osu!lazer's storage.ini redirect, if there is one.

    When you move your lazer library to another drive, lazer leaves the
    default data folder in place as a stub and drops a storage.ini in it
    pointing at the real location:

        FullPath = D:\\osu-lazer

    The stub keeps a (mostly empty) files/ folder and client.realm.lock, but
    NOT client.realm - so without following this redirect the realm fast
    path is skipped entirely and the fallback scan walks an empty files/
    folder and finds zero beatmaps.

    Returns the redirected folder if storage.ini names one that exists, else
    the folder it was given unchanged. Safe to call on any folder.
    """
    if not data_dir:
        return data_dir
    ini_path = os.path.join(data_dir, "storage.ini")
    if not os.path.isfile(ini_path):
        return data_dir
    try:
        with open(ini_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        return data_dir
    # Not a real ini (no section header), so just pull the one key out.
    m = re.search(r"^\s*FullPath\s*=\s*(.+?)\s*$", text, re.M)
    if not m:
        return data_dir
    target = m.group(1).strip().strip('"')
    if not target:
        return data_dir
    if not os.path.isdir(target):
        if log_cb:
            log_cb(f"Note: {ini_path} points at {target}, but that folder doesn't exist - "
                   f"using {data_dir} as-is.")
        return data_dir
    if log_cb:
        log_cb(f"Followed lazer's storage.ini redirect: {data_dir} -> {target}")
    return target


def default_lazer_data_dir():
    """
    Best-guess default location of osu!lazer's top-level data folder (the
    one containing client.realm and files/) per OS. Point the tool at this
    folder (rather than files/ directly) to enable the fast realm-reader
    path when the helper is available.

    Follows storage.ini if the library has been relocated to another drive -
    see resolve_lazer_storage().
    """
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA")
        base_dir = os.path.join(base, "osu") if base else None
    elif sys.platform == "darwin":
        base_dir = os.path.expanduser("~/Library/Application Support/osu")
    else:
        base_dir = os.path.expanduser("~/.local/share/osu")
    return resolve_lazer_storage(base_dir)


# --------------------------------------------------------------------------
# collection.db writer (osu!stable binary format)
# --------------------------------------------------------------------------

def _write_uleb128_string(buf, s):
    if s is None or s == "":
        buf.append(0x00)
        return
    buf.append(0x0b)
    encoded = s.encode("utf-8")
    n = len(encoded)
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            buf.append(b | 0x80)
        else:
            buf.append(b)
            break
    buf.extend(encoded)


def write_collection_db(path, collections, db_version=20211103):
    """
    collections: dict[name] -> list of md5 hashes
    """
    buf = bytearray()
    buf.extend(struct.pack("<i", db_version))
    buf.extend(struct.pack("<i", len(collections)))
    for name, hashes in collections.items():
        _write_uleb128_string(buf, name)
        buf.extend(struct.pack("<i", len(hashes)))
        for h in hashes:
            _write_uleb128_string(buf, h)
    with open(path, "wb") as f:
        f.write(buf)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

CATEGORIES = ["Streams", "Hybrid", "Bursts", "Jumps with bursts", "Jumps (no bursts)", "Misc"]

# The two categories that are both "a jump map", differing only in whether
# bursts show up alongside the jumps. combine_jumps writes an extra
# collection holding both, for players who just want every jump map in one
# place - see build_output_collections().
JUMP_CATEGORIES = ("Jumps with bursts", "Jumps (no bursts)")
COMBINED_JUMPS_LABEL = "Jumps"

# Which osu! online statuses count as "ranked" for filtering and splitting.
#
# Ranked and Approved are the two that award pp. Loved and Qualified do NOT:
# Loved is a curated section with no pp, and Qualified is still in the queue
# and can be disqualified. Lumping those in with ranked (which this used to
# do, via a >= 1 check on the raw enum) means "ranked only" silently hands
# you a pile of Loved maps.
RANKED_STATUSES = frozenset({"ranked", "approved"})


def is_ranked(status):
    """
    True for statuses that award pp.

    Accepts the real status names emitted by current realm-reader builds, and
    still understands the bare "ranked"/"unranked" that older compiled
    helpers emit - the helper ships as a prebuilt binary and can lag the
    Python it sits next to.
    """
    return status in RANKED_STATUSES


def category_of(has_streams, has_bursts=None, has_jumps=None,
                 burst_note_total=0, total_note_count=0, jump_pct=0.0,
                 stream_note_total=0, stream_run_count=0, max_stream_len=0,
                 burst_promote_stream_len=12, burst_run_count=0, counted_gaps=0,
                 burst_recurrence_min=1, burst_transitions=None, stream_transitions=None,
                 has_hybrid=False):
    """
    The one place the dominant-pattern rules live.

    Call it either with a DiffInfo (everything else is read off it) or with
    the individual values, which is what the CSV path does. Both callers used
    to carry their own copy of these rules, so a threshold change had to be
    made twice and could silently drift.

    Every pattern has to EARN the map by covering more of it than the
    competition. Streams used to win outright on mere presence, so a map that
    was 90% jumps with a single stream buried in it came out as a stream map
    - which is the opposite of what "dominant pattern" means, and
    inconsistent with the coverage comparison bursts had always been subject
    to. Streams now face the same test.

    Coverage is measured in TRANSITIONS for all three patterns when
    counted_gaps is available (the DiffInfo path always has it) - jump_pct
    was already transitions-based, and burst/stream coverage used to be
    notes-based, which isn't the same basis: a note is shared between two
    adjacent runs at their boundary (see AGENTS.md), and more importantly a
    map built from alternating burst-cluster-then-jump sections (see the
    ai-classification branch's investigation) drives burst-notes and
    jump-transitions to near-identical counts by construction, making a
    notes-vs-transitions comparison between them essentially coin-flip noise
    at the exact point (a near-tie) where the comparison actually matters.
    Converting a run's note count to a transition count is exact and free -
    a run of N notes is N-1 transitions, summed over however many runs
    contributed to burst_note_total/stream_note_total via burst_run_count/
    stream_run_count.

    Falls back to the old notes-over-notes basis when counted_gaps isn't
    supplied (0) - the CSV-rebuild path for CSVs written before this field
    existed has no way to recover it, so it keeps the original comparison
    rather than silently mixing bases.

    has_bursts, unlike has_streams, is pure presence - len(bursts) > 0, no
    coverage bar to clear. burst_recurrence_min briefly raised that to 2 runs,
    on the reasoning that 176 "Jumps with bursts" results in a ~9,300-map
    library came from a SINGLE burst run at a median 1.13% coverage, and
    sampled examples looked like plain jump maps. It is back to 1, because
    that reasoning was fixing the wrong layer.

    Those single runs were mostly not bursts at all - they were the two
    detection bugs since fixed: 1/2-snap tapping in high-BPM maps admitted by
    an absolute-ms speed test with no notion of snap (see the rhythm gate in
    classify_diff), and stacked triples wrongly rejected, which suppressed the
    genuine runs that would have kept the honest maps above a count of 2. A
    recurrence floor was compensating for a noisy detector by discarding its
    output wholesale, which also discards the true positives.

    With detection fixed, the user's hand-sorted set settles the question
    directly: of thirteen maps they filed as "jumps with bursts" that the
    classifier had called "jumps with no bursts", five contain exactly ONE
    burst run - a single 1/4 triple in a 120-280 note map, ~1-2% coverage -
    and they are labelled "Jumps with bursts" all the same. One real burst is
    enough; the category means "this jump map has bursts in it", not "bursts
    are a recurring theme".

    The parameter stays rather than being deleted so the behaviour is still
    reachable, and because where the line should sit for run counts 2-9 was
    always an open question - it just isn't one worth answering with a floor
    that costs real maps. Burst coverage has no clean bimodal split to key on
    the way stream coverage did (2.5-12.9% vs 55-96% for real/fake NiNo
    streams): median coverage rises smoothly with run count, 1.13% at 1 run to
    16.53% at 10+, no natural gap anywhere - a burst is capped at 9 notes by
    definition and can never dominate a note count the way one long stream
    run can.

    burst_run_count == 0 is treated as "not supplied" (skip this gate, old
    presence-only behaviour) rather than "genuinely zero runs" - the two
    are indistinguishable from the parameter alone, but harmlessly so: a
    real zero-run map already has has_bursts == False, so this gate would
    never have mattered for it anyway.
    """
    # burst_transitions/stream_transitions are the exact partition counts when
    # the caller has them; None means "derive them the old way" - see below.
    if hasattr(has_streams, "has_streams"):
        d = has_streams
        has_streams, has_bursts, has_jumps = d.has_streams, d.has_bursts, d.has_jumps
        burst_note_total, total_note_count = d.burst_note_total, d.total_note_count
        jump_pct = d.jump_pct
        stream_note_total = d.stream_note_total
        stream_run_count = d.stream_count
        max_stream_len = d.max_stream_len
        burst_run_count = d.burst_count
        counted_gaps = d.counted_gaps
        burst_transitions = d.burst_transitions
        stream_transitions = d.stream_transitions
        has_hybrid = d.has_hybrid

    if burst_run_count and burst_run_count < burst_recurrence_min:
        has_bursts = False

    jump_coverage = jump_pct / 100.0
    if counted_gaps:
        if burst_transitions is None:
            # Raw-args or old-CSV path: recover the transition counts from the
            # note totals. A run of N notes is N-1 transitions, so this is
            # arithmetically exact and agrees with the measured partition on
            # every diff checked - it just can't notice if two passes claimed
            # the same transition, which is what the partition exists to stop.
            stream_transitions = max(0, stream_note_total - stream_run_count)
            burst_transitions = max(0, burst_note_total - burst_run_count)
        stream_coverage = stream_transitions / counted_gaps
        burst_coverage = burst_transitions / counted_gaps
    else:
        stream_coverage = (stream_note_total / total_note_count) if total_note_count else 0.0
        burst_coverage = (burst_note_total / total_note_count) if total_note_count else 0.0

    # A burst map containing a genuine stream run is a stream map. Bursts and
    # streams are the same motion, so the question for a burst player isn't
    # how much of the map streams - it's whether the map ever demands
    # sustained stream stamina at all. A map that asks for it once is not a
    # burst map any more, however little of the map it occupies.
    #
    # The run has to clear burst_promote_stream_len (12) rather than merely
    # reach stream_min (10). A single run sitting exactly on the minimum is a
    # boundary artifact, not evidence the map streams: measured across ten
    # NiNo mapsets, the one difficulty that promoted on a bare 10-note run was
    # the same false positive the stream coverage floor was added to kill.
    # Requiring a couple of notes of headroom drops it and keeps every
    # convincing case (24 promotions across the labelled sets, the smallest
    # of which is a 17-note run).
    #
    # Deliberately scoped to burst outcomes only. The same reasoning does NOT
    # extend to jump maps: those spread their notes far enough apart that a
    # stray run picked up among them is usually a tightly-spaced jump pattern
    # rather than real streaming, which is why one short run in a long jump
    # map leaves it a jump map.
    def burst_or_stream():
        real_stream = stream_run_count > 0 and max_stream_len >= burst_promote_stream_len
        return "Streams" if real_stream else "Bursts"

    # --- one contest, not a chain of pairwise ones -------------------------
    # This used to be an ordered cascade: streams-vs-jumps first, then
    # jumps-vs-bursts, then whatever was left. Two problems with that shape.
    #
    # Streams and bursts never met. A map where bursts out-covered streams,
    # and streams out-covered jumps, was called a stream map on the strength
    # of the first comparison it happened to reach - the burst evidence was
    # never weighed against it at all.
    #
    # And order stood in for strength. Whichever pattern got compared first
    # had an advantage that had nothing to do with how much of the map it
    # actually occupied.
    #
    # All three coverages are now measured on one basis and partition the same
    # denominator (see burst_transitions/stream_transitions/jump_transitions),
    # so they can simply be ranked. Only patterns that cleared their own
    # presence bar are entered - a pattern that isn't really there doesn't get
    # to win by being marginally less absent than the others.
    #
    # Ties keep the old cascade's outcome: it awarded a stream-vs-jump tie to
    # streams (>=) and a burst-vs-jump tie to bursts (jumps needed a strict >).
    # Hence the priority below, which only ever breaks exact ties.
    # --- Hybrid: streams AND jumps, in different parts of the map ----------
    # Coverage can't express this. A map that is jumps for a minute and then
    # streams for a minute averages out to the same numbers as one that mixes
    # both evenly throughout, and the contest below would hand it to whichever
    # edged ahead - losing the thing that actually characterises it.
    #
    # Sections can see it: this asks whether streams OWN a real share of the
    # map's sections and jumps own a real share too. Both patterns still have
    # to clear their own presence bars first, so this only ever splits maps
    # that genuinely have both - it never promotes a map on evidence too thin
    # to count on its own.
    if has_hybrid and has_streams and has_jumps:
        return "Hybrid"

    contenders = []
    if has_streams:
        contenders.append((stream_coverage, 3, "stream"))
    if has_bursts:
        contenders.append((burst_coverage, 2, "burst"))
    if has_jumps:
        contenders.append((jump_coverage, 1, "jump"))
    if not contenders:
        return "Misc"

    winner = max(contenders)[2]
    if winner == "stream":
        return "Streams"
    if winner == "burst":
        return burst_or_stream()
    # Jumps own the map. Bursts alongside them are secondary content, not the
    # map's character - which is the distinction the two jump categories draw.
    return "Jumps with bursts" if has_bursts else "Jumps (no bursts)"


def derive_collections(diffs):
    """
    Groups diffs by DOMINANT pattern, not by presence of every tag. A map
    that's mostly streams with a jump section thrown in is still a stream
    map - a small secondary pattern doesn't change what the map fundamentally
    is.

      - Streams            : has any genuine stream run (10+ notes, tight/
                              fast). Cutstreams (a stream with a minority of
                              wider-spaced notes) count as streams here too,
                              not a separate category - a cutstream is still
                              a stream.
      - Bursts             : has burst run(s), no streams, and burst note
                              coverage is greater than or equal to jump
                              coverage of the map.
      - Jumps with bursts  : has jumps AND bursts, no streams, but JUMP
                              coverage is greater than burst coverage - the
                              map is still fundamentally a jump map, bursts
                              are secondary.
      - Jumps (no bursts)  : jump-heavy, no bursts or streams at all.
      - Misc               : none of the above (low-density / normal play).

    Bursts vs. Jumps is decided by comparing how much of the map each
    pattern actually covers (burst note count / total notes vs. jump_pct),
    not just whether a burst run exists at all - a map that's 90% jumps
    with one small incidental burst thrown in is still fundamentally a jump
    map, not a burst map. This mirrors how the osu! community actually
    talks about maps - "this is a stream map" even if it has a jump
    section, but a map isn't relabeled a "burst map" just because it has
    one short burst somewhere in an otherwise pure jump map.
    """
    groups = {label: [] for label in CATEGORIES}
    for d in diffs:
        groups[category_of(d)].append(d)
    return groups


def build_output_collections(groups, include_categories=None, ranked_mode="all_together",
                              min_star=None, max_star=None, combine_jumps=False, log_cb=None):
    """
    Applies category selection, star rating range, and ranked-status
    handling on top of the dominant-pattern groups from derive_collections().
    This is what actually controls what ends up in the output collection.db -
    e.g. an aim-only player could pass include_categories=["Jumps (no bursts)"]
    to get just that one collection and nothing else.

    min_star / max_star filter by star rating (inclusive), supporting
    decimals (e.g. max_star=6.5). Only meaningful when star rating is
    available (currently only the osu!lazer realm fast path provides it) -
    diffs with unknown star rating are excluded from a star-filtered output
    rather than guessed at, with a warning logged.

    ranked_mode:
      - "all_together" : ignore ranked status entirely (default)
      - "ranked_only"  : drop any diff that isn't ranked/approved/qualified/loved
      - "unranked_only": drop any diff that IS ranked/approved/qualified/loved
      - "split"         : each category becomes two, e.g. "Streams - Ranked" /
                          "Streams - Unranked"

    combine_jumps adds an EXTRA "Jumps" collection holding every diff from
    both jump categories, without removing either of them - so a jumps+bursts
    map lands in "Jumps with bursts" and in "Jumps". It changes only how the
    output is grouped; no diff is reclassified, and report.csv still records
    the specific category. It is applied after category and star filtering
    (so unchecking a jump category keeps those maps out of the combined one
    too - unchecking means "I don't want these maps") and before ranked
    handling (so "split" splits the combined collection the same way it
    splits every other one).

    Ranked status is only known when the diffs came from the osu!lazer realm
    fast path - anything else has ranked_status=None, which is treated as
    "not ranked" for filtering/splitting purposes (with a warning logged).
    """
    def log(msg):
        if log_cb:
            log_cb(msg)

    if include_categories:
        valid_categories = set(groups.keys())
        unknown = [c for c in include_categories if c not in valid_categories]
        if unknown:
            log(f"WARNING: these requested categories don't match any known category and will be ignored: "
                f"{', '.join(unknown)}. Valid categories are: {', '.join(sorted(valid_categories))}.")
        groups = {label: members for label, members in groups.items() if label in include_categories}
        if not any(groups.values()):
            log("WARNING: after category filtering, nothing matched - collection.db will be empty. "
                "Check --categories against the valid category names above.")

    if min_star is not None or max_star is not None:
        any_known = any(d.star_rating is not None for members in groups.values() for d in members)
        if not any_known:
            log("Note: star rating isn't available for this scan (only the osu!lazer realm fast path "
                "provides it) - nothing will match this star rating filter.")

        def in_range(d):
            if d.star_rating is None:
                return False
            if min_star is not None and d.star_rating < min_star:
                return False
            if max_star is not None and d.star_rating > max_star:
                return False
            return True

        groups = {label: [d for d in members if in_range(d)] for label, members in groups.items()}

    if combine_jumps:
        combined = [d for label in JUMP_CATEGORIES for d in groups.get(label, [])]
        if combined:
            # Rebuilt in order rather than appended so the combined collection
            # sits with the jump categories it summarises instead of after
            # Misc - collection.db preserves insertion order and osu! shows
            # them in that order.
            rebuilt = {}
            for label, members in groups.items():
                rebuilt[label] = members
                if label == JUMP_CATEGORIES[-1]:
                    rebuilt[COMBINED_JUMPS_LABEL] = combined
            if COMBINED_JUMPS_LABEL not in rebuilt:
                rebuilt[COMBINED_JUMPS_LABEL] = combined
            groups = rebuilt

    if ranked_mode == "all_together":
        return groups

    any_known = any(d.ranked_status is not None for members in groups.values() for d in members)
    if not any_known:
        log("Note: ranked status isn't available for this scan (only the osu!lazer realm fast path "
            "provides it) - all diffs are being treated as unranked for this filter/split.")

    if ranked_mode == "ranked_only":
        return {label: [d for d in members if is_ranked(d.ranked_status)] for label, members in groups.items()}
    elif ranked_mode == "unranked_only":
        return {label: [d for d in members if not is_ranked(d.ranked_status)] for label, members in groups.items()}
    elif ranked_mode == "split":
        result = {}
        for label, members in groups.items():
            ranked = [d for d in members if is_ranked(d.ranked_status)]
            unranked = [d for d in members if not is_ranked(d.ranked_status)]
            if ranked:
                result[f"{label} - Ranked"] = ranked
            if unranked:
                result[f"{label} - Unranked"] = unranked
        return result
    return groups


DEFAULT_PARAMS = dict(
    # Speed gate, in absolute ms per note. Stream BPM = 15000 / ms, so 140ms
    # is about a 107 BPM stream. Replaces the old snap_ratio, which compared
    # against the map's stored BPM and so broke on doubled-BPM maps.
    max_gap_ms=140.0,
    # How much a gap may deviate from its run's running mean before the run
    # is considered to have changed speed and is split.
    gap_consistency_tol=0.18,
    tight_diam_ratio=1.35,
    spaced_diam_ratio=2.0,
    # 3, not 5: a three-circle burst is a real and very common pattern.
    burst_min=3,
    burst_max=9,
    stream_min=10,
    # Diameters per 100ms. NOT the old diameters-per-beat - the unit changed
    # when the BPM dependence came out, so this default was re-derived (it
    # approximates the old behaviour at ~200 BPM) and wants validating
    # against a labelled set rather than trusting.
    jump_velocity_ratio=0.75,
    jump_pct_threshold=15.0,
    # Minimum number of in-play transitions before jump_pct is allowed to
    # decide anything.
    jump_min_transitions=40,
    # Gaps longer than this are breaks, and are kept out of the jump_pct
    # denominator entirely.
    jump_gap_cap_ms=1000.0,
    # Minimum share of a map's notes that must sit in stream runs before it
    # counts as having streams at all. The mirror of jump_pct_threshold -
    # without it, one short run in a long jump map claimed the whole map.
    stream_pct_threshold=15.0,
    run_wide_fraction_max=0.4,
    mean_diam_ratio_max=1.5,
    # Largest skipped-note gap that still counts as a cut within one stream
    # rather than a genuine break between two runs.
    cut_max_multiple=3.0,
    # Largest cut-transition distance, as a multiple of hit-circle diameter,
    # that still counts as "a skipped note" rather than a real jump hiding
    # inside what would otherwise read as one continuous cutstream. A cut
    # spans two normal note-hops merged into one, so ~2x the ordinary
    # "still readable" spacing ceiling (spaced_diam_ratio) is expected even
    # for a genuine skip - beyond that it's a jump, not a timing artifact.
    cut_max_dist_ratio=4.0,
    # Rhythm gate. A run has to be a step UP from the map's own pulse to be a
    # burst, not just fast in the abstract: at most this fraction of a beat
    # per note. 0.4 admits 1/4 (0.25) and 1/3 (0.33) and rejects 1/2 (0.5).
    # Without it, a 240 BPM map's ordinary 1/2 tapping (125ms) cleared the
    # 140ms absolute cap and every jump map at that tempo grew phantom bursts.
    burst_beat_fraction_max=0.4,
    # Share of note gaps on the notated 1/2 before that layer counts as the
    # map's backbone - one of two signals for DOUBLED notation. Replaces a
    # blanket "runs under 110ms skip the snap test", which protected doubled
    # notation by exempting every map above ~273 BPM along with it.
    doubled_half_share_min=0.25,
    # Above this notated tempo, doubled-BPM authoring is the likelier reading.
    # Read off the timing points, not the notes, so every difficulty in a
    # mapset agrees - notation belongs to the set, and content alone gives
    # different answers for its quiet and busy diffs.
    max_plausible_bpm=300.0,
    # Slowest a run may tap and still be a BURST (streams are unaffected -
    # see the comment at the burst/stream split). 105ms is about a 143 BPM
    # stream. Measured over 28 hand-labelled difficulties: the ones with
    # bursts run 75-94ms per note, the ones without 115-134ms, no overlap.
    burst_max_gap_ms=105.0,
    # Share of a map's note gaps that must sit on the notated 1/4 before that
    # layer counts as the map's backbone rather than accent content - one of
    # the two content signals that decide halved notation. There is
    # deliberately no BPM threshold; see looks_like_halved_notation().
    halved_quarter_share_min=0.15,
    # Sectional structure. 2000ms is roughly two bars at 200 BPM and was
    # picked by printing real maps' section timelines - osu!'s own 400ms
    # strain sections are far too short to show pattern structure. A section
    # is "owned" by a pattern when that pattern holds section_dominance of it,
    # and sections with fewer than section_min_transitions notes are ignored
    # so a map's sparse tail can't swamp the proportions.
    section_ms=2000.0,
    section_dominance=0.5,
    section_min_transitions=4,
    # Hybrid: streams and jumps must each own this share of the map's
    # sections, AND be balanced - the smaller at least hybrid_balance_min of
    # the larger, so "a jump map with one stream section" isn't called a mix.
    hybrid_section_min=0.15,
    hybrid_balance_min=0.5,
)

# Named starting points for the GUI's "Detection sensitivity" control, so the
# common case is one click instead of nineteen numbers.
#
# Only five knobs move. They are the ones that actually answer "how much gets
# flagged": how fast a run has to be, how much of a rhythm step-up a burst
# needs, how much of a map streams/jumps must cover to own it, and how readily
# a map counts as a Hybrid rather than one thing or the other. Everything
# else describes what a pattern IS rather than how eager we are to find one,
# and moving those would change the definitions rather than the sensitivity.
#
# "Balanced" is exactly DEFAULT_PARAMS - the measured, evidence-backed
# defaults. The other two are deliberate over- and under-shoots for people who
# want a different trade, NOT rival claims about what is correct. Anything
# these set is still individually editable underneath.
SENSITIVITY_PRESETS = {
    "Balanced": {},
    "Stricter": dict(
        max_gap_ms=120.0,               # ~125 BPM stream before it counts as fast
        burst_beat_fraction_max=0.30,   # 1/4 only - drops 1/3 snap
        stream_pct_threshold=25.0,      # streams must cover a quarter of the map
        jump_pct_threshold=20.0,
        hybrid_section_min=0.25,        # Hybrid only for unambiguous mixes
    ),
    "Looser": dict(
        max_gap_ms=160.0,               # ~94 BPM stream still counts as fast
        burst_beat_fraction_max=0.45,   # allows sloppier-than-1/3 snapping
        stream_pct_threshold=10.0,
        jump_pct_threshold=12.0,
        hybrid_section_min=0.10,
    ),
}
DEFAULT_SENSITIVITY = "Balanced"


def params_for_sensitivity(name):
    """Full param dict for a named preset. Unknown names fall back to defaults."""
    params = dict(DEFAULT_PARAMS)
    params.update(SENSITIVITY_PRESETS.get(name, {}))
    return params


def sensitivity_of(params):
    """
    Which preset `params` corresponds to, or None if it matches none of them
    (i.e. the user has hand-edited something). Lets the GUI show "Custom"
    honestly rather than keeping a preset selected that no longer describes
    the settings.
    """
    for name in SENSITIVITY_PRESETS:
        if all(abs(float(params[k]) - float(v)) < 1e-9
               for k, v in params_for_sensitivity(name).items()
               if k in params and isinstance(v, (int, float))):
            return name
    return None


# Params that must stay integers when parsed from a string (CLI/GUI).
INT_PARAMS = ("burst_min", "burst_max", "stream_min", "jump_min_transitions",
              "section_min_transitions")


def run_pipeline(songs_folder, output=None, csv_path=None, write_db=True,
                  params=None, progress_cb=None, log_cb=None, cancel_event=None,
                  include_categories=None, ranked_mode="all_together",
                  min_star=None, max_star=None, combine_jumps=False,
                  pause_event=None):
    """
    Core pipeline used by both the CLI and the GUI:
      1. scan_folder() over .osu/.osz files
      2. classify_diff() each result
      3. derive_collections() into the final category groups
      4. optionally write a CSV and/or collection.db

    progress_cb(done, total) is forwarded from scan_folder for a live progress bar.
    log_cb(str) receives human-readable status lines (what main() would otherwise print).
    cancel_event, if given, is checked periodically during the scan/classify
    phase - if set, raises ScanCancelled to stop promptly without writing
    a CSV or collection.db (a partial/interrupted run isn't something you'd
    want to trust as the actual output).
    include_categories, if given, restricts which categories ("Streams",
    "Bursts", "Jumps", "Misc") get written to the output collection.db -
    e.g. pass ["Jumps"] to only output a Jumps collection and skip writing
    the rest entirely. The CSV report always includes every diff regardless,
    so you can still audit the full scan.
    ranked_mode controls how ranked status factors into collection names
    (only meaningful when ranked status is actually available - currently
    only the osu!lazer realm fast path provides it):
      - "all_together" (default): ranked status ignored, one collection per category
      - "ranked_only": only ranked/approved/loved/qualified diffs are included
      - "unranked_only": only pending/WIP/graveyard diffs are included
      - "split": two collections per category, e.g. "Streams - Ranked" / "Streams - Unranked"
    Returns a dict: {diffs, errors, groups, counts}
    """
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)

    def log(msg):
        if log_cb:
            log_cb(msg)

    import time

    log(f"=== Starting run on {songs_folder} ===")

    classify_count = [0]

    def classify_and_free(d):
        if cancel_event is not None and cancel_event.is_set():
            raise ScanCancelled()
        wait_if_paused(pause_event, cancel_event)
        classify_diff(d, **{k: p[k] for k in DEFAULT_PARAMS})
        # Free per-note data immediately - only the summary fields (counts,
        # booleans, hash) are needed from here on. Classifying inline like
        # this (instead of parsing the whole library first, THEN classifying
        # in a second pass) keeps peak memory bounded to roughly one file's
        # worth of raw note data at a time, instead of holding every note of
        # every difficulty in the whole library in memory simultaneously -
        # that was the cause of out-of-memory crashes on very large
        # (100k+ diff) libraries.
        d.objs = []
        d.timing_points = []
        classify_count[0] += 1
        if log_cb and classify_count[0] % 20000 == 0:
            log(f"  ... {classify_count[0]} classified so far")

    diffs = errors = None

    # Figure out where client.realm actually is: either the given folder
    # directly, or one level up if the given folder is itself a files/
    # subfolder (a very natural thing to point at, so worth handling rather
    # than silently skipping the fast path with no explanation).
    # Candidate data folders to look for client.realm in, in priority order:
    # the folder given, that folder's parent (if we were handed a files/
    # subfolder, a very natural thing to point at), and the storage.ini
    # redirect target of either (lazer leaves a stub behind when the library
    # has been moved to another drive - see resolve_lazer_storage).
    realm_data_dir = None
    normed = os.path.normpath(songs_folder)
    parent = os.path.dirname(normed)
    is_files_subdir = os.path.basename(normed).lower() == "files"

    candidates = [(songs_folder, None)]
    if is_files_subdir:
        candidates.append((parent, f"Detected you're pointed at a files/ subfolder - looking for "
                                    f"client.realm in the parent folder ({parent})."))
    redirected = resolve_lazer_storage(songs_folder)
    if redirected != songs_folder:
        candidates.append((redirected, None))
    if is_files_subdir:
        redirected_parent = resolve_lazer_storage(parent)
        if redirected_parent != parent:
            candidates.append((redirected_parent, None))

    for candidate, note in candidates:
        if os.path.isfile(os.path.join(candidate, "client.realm")):
            if note:
                log(note)
            if candidate != songs_folder:
                log(f"Using lazer data folder {candidate} (found client.realm there).")
            realm_data_dir = candidate
            break

    if realm_data_dir:
        fast_result = scan_lazer_realm(realm_data_dir, progress_cb=progress_cb, log_cb=log_cb, on_parsed=classify_and_free, cancel_event=cancel_event, pause_event=pause_event)
        if fast_result is not None:
            diffs, errors = fast_result
    else:
        log("No client.realm found (not pointed at a lazer data folder or files/ subfolder) - "
            "skipping the realm fast path.")

    # osu!stable fast path. Same idea as the lazer realm path: take the file
    # list from the game's own database rather than walking the disk. On a
    # 25k-folder library the walk alone costs ~180s, and it also has to hash
    # every file and open non-osu!standard difficulties only to discard them.
    if diffs is None:
        stable = find_stable_db(songs_folder)
        if stable:
            db_path, songs_dir = stable
            stable_result = scan_stable_db(
                db_path, songs_dir, progress_cb=progress_cb, log_cb=log_cb,
                on_parsed=classify_and_free, cancel_event=cancel_event,
                pause_event=pause_event)
            if stable_result is not None:
                diffs, errors = stable_result

    if diffs is None:
        scan_root = songs_folder
        # If we identified a lazer data dir (containing client.realm) but the
        # fast path wasn't usable, fall back to scanning its files/ subfolder
        # rather than the whole data dir (which also has scores, skins, etc.
        # we don't need to touch). This deliberately uses realm_data_dir, not
        # songs_folder, so a storage.ini redirect is still honored on the slow
        # path - otherwise we'd walk the near-empty stub folder and report
        # zero beatmaps.
        base = realm_data_dir or resolve_lazer_storage(songs_folder, log_cb=log)
        files_subdir = os.path.join(base, "files")
        if base != songs_folder or realm_data_dir == songs_folder:
            if os.path.isdir(files_subdir):
                scan_root = files_subdir
        if scan_root != songs_folder:
            log(f"Scanning {scan_root}")
        diffs, errors = scan_folder(scan_root, progress_cb=progress_cb, log_cb=log_cb, on_parsed=classify_and_free, cancel_event=cancel_event, pause_event=pause_event)
    log(f"Classified {len(diffs)} difficulties.")

    groups = derive_collections(diffs)
    counts = {label: len(members) for label, members in groups.items()}

    log("Summary (by dominant pattern):")
    for label, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        log(f"  {label}: {count}")

    if csv_path:
        try:
            import csv
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                mods_str = "NM"      # NM-only for now; column kept so the CSV shape is stable
                w.writerow(["title", "diff_name", "bpm", "has_bursts", "has_streams", "has_jumps", "has_cutstreams",
                            "has_hybrid", "active_sections", "stream_sections", "jump_sections",
                            "burst_runs", "stream_runs", "cutstream_runs", "max_burst_len",
                            "max_stream_len", "jump_pct", "burst_note_total", "stream_note_total",
                            "total_note_count", "counted_gaps",
                            "burst_transitions", "stream_transitions", "jump_transitions",
                            "ranked_status", "star_rating", "online_id", "mods", "category", "path"])
                def safe_round(x, ndigits=None):
                    # round() raises OverflowError on inf and ValueError on
                    # nan - one bad map (a corrupt beatLength, say) shouldn't
                    # abort the whole CSV.
                    if not isinstance(x, (int, float)) or not math.isfinite(x):
                        return 0
                    return round(x, ndigits)

                for d in diffs:
                    w.writerow([d.title, d.diff_name, safe_round(d.bpm), d.has_bursts, d.has_streams, d.has_jumps, d.has_cutstreams,
                                d.has_hybrid, d.active_sections, d.stream_sections, d.jump_sections,
                                d.burst_count, d.stream_count, d.cutstream_count, d.max_burst_len,
                                d.max_stream_len, safe_round(d.jump_pct, 1), d.burst_note_total,
                                d.stream_note_total, d.total_note_count, d.counted_gaps,
                                d.burst_transitions, d.stream_transitions, d.jump_transitions,
                                d.ranked_status or "unknown", d.star_rating if d.star_rating is not None else "unknown",
                                d.online_id if d.online_id is not None else "unknown", mods_str,
                                category_of(d), d.path])
            log(f"Full per-diff results written to {csv_path}")
        except Exception:
            import traceback
            log("ERROR writing CSV report:")
            log(traceback.format_exc())

    if write_db and output:
        try:
            output_groups = build_output_collections(groups, include_categories=include_categories,
                                                       ranked_mode=ranked_mode, min_star=min_star,
                                                       max_star=max_star, combine_jumps=combine_jumps,
                                                       log_cb=log)
            collections = {label: [d.version_hash for d in members] for label, members in output_groups.items() if members}
            if not collections:
                log("WARNING: no maps matched your combined filters (categories/ranked/star range) - "
                    "collection.db will be written but empty.")
            write_collection_db(output, collections)
            log(f"collection.db written to {output}")
        except Exception:
            import traceback
            log("ERROR writing collection.db:")
            log(traceback.format_exc())

    return {"diffs": diffs, "errors": errors, "groups": groups, "counts": counts}


def collection_from_csv(csv_path, output_db, log_cb=None, include_categories=None, ranked_mode="all_together",
                         min_star=None, max_star=None, combine_jumps=False):
    """
    Rebuilds a collection.db from a previously-generated report.csv, without
    rescanning the library. Useful if a run produced the CSV successfully
    but crashed (e.g. out of memory) before writing the collection.db, or if
    you just want to regenerate the .db after editing thresholds' effects
    are already reflected in an existing CSV.

    Re-opens each file listed in the CSV's `path` column to compute its MD5
    hash (needed for collection.db) - this is far cheaper than a full
    rescan since no note-level classification happens, just a hash of each
    already-known file. Rows whose path is inside a .osz archive
    (format "archive.osz::inner.osu") or no longer exists on disk are
    skipped and counted, since only the archive's basename was recorded,
    not enough to reopen it.

    include_categories / ranked_mode work the same as in run_pipeline() -
    see build_output_collections() for details. ranked_status is read from
    the CSV's own ranked_status column (only meaningful if that CSV was
    produced via the osu!lazer realm fast path).
    """
    import csv as csv_module

    def log(msg):
        if log_cb:
            log_cb(msg)

    groups = {label: [] for label in CATEGORIES}
    total = 0
    skipped = 0

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv_module.DictReader(f)
        for row in reader:
            total += 1
            path = row.get("path", "")
            if "::" in path or not path or not os.path.isfile(path):
                skipped += 1
                continue
            try:
                with open(path, "rb") as bf:
                    h = hashlib.md5(bf.read()).hexdigest()
            except OSError:
                skipped += 1
                continue

            ranked_status = row.get("ranked_status")
            if ranked_status in (None, "", "unknown"):
                ranked_status = None
            star_rating_str = row.get("star_rating")
            star_rating = None
            if star_rating_str and star_rating_str.lower() != "unknown":
                try:
                    star_rating = float(star_rating_str)
                except ValueError:
                    star_rating = None
            entry = (h, ranked_status, star_rating)

            # Newer CSVs carry the decided category outright. Older ones
            # don't, so fall back to re-deriving it from the raw columns via
            # the same shared rules run_pipeline uses.
            category = (row.get("category") or "").strip()
            if category not in groups:
                try:
                    burst_note_total = int(row.get("burst_note_total") or 0)
                    stream_note_total = int(row.get("stream_note_total") or 0)
                    total_note_count = int(row.get("total_note_count") or 0)
                    stream_runs = int(row.get("stream_runs") or 0)
                    max_stream_len = int(row.get("max_stream_len") or 0)
                    jump_pct = float(row.get("jump_pct") or 0)
                    # Both added after counted_gaps existed - "or 0" makes a
                    # CSV written before this field existed fall back to
                    # category_of()'s notes-basis comparison automatically
                    # (counted_gaps=0 there means "unknown, use the old way").
                    burst_runs = int(row.get("burst_runs") or 0)
                    counted_gaps = int(row.get("counted_gaps") or 0)
                    # Exact partition counts, written since the run-partition
                    # change. Absent in older CSVs, where None makes
                    # category_of derive them from the note totals instead -
                    # the two agree except when a run contains a transition
                    # that was excluded from the denominator (an
                    # overlapping-slider one), which the derivation can't see.
                    bt = row.get("burst_transitions")
                    st = row.get("stream_transitions")
                    burst_tr = int(bt) if bt not in (None, "") else None
                    stream_tr = int(st) if st not in (None, "") else None
                except ValueError:
                    burst_note_total = stream_note_total = total_note_count = 0
                    stream_runs = max_stream_len = 0
                    jump_pct = 0.0
                    burst_runs = counted_gaps = 0
                    burst_tr = stream_tr = None
                category = category_of(
                    row.get("has_streams") == "True",
                    row.get("has_bursts") == "True",
                    row.get("has_jumps") == "True",
                    burst_note_total, total_note_count, jump_pct, stream_note_total,
                    stream_runs, max_stream_len,
                    burst_run_count=burst_runs, counted_gaps=counted_gaps,
                    burst_transitions=burst_tr, stream_transitions=stream_tr,
                    has_hybrid=row.get("has_hybrid") == "True",
                )
            groups[category].append(entry)

            if log_cb and total % 20000 == 0:
                log(f"  ... {total} rows processed")

    if include_categories:
        valid_categories = set(groups.keys())
        unknown = [c for c in include_categories if c not in valid_categories]
        if unknown:
            log(f"WARNING: these requested categories don't match any known category and will be ignored: "
                f"{', '.join(unknown)}. Valid categories are: {', '.join(sorted(valid_categories))}.")
        groups = {label: entries for label, entries in groups.items() if label in include_categories}
        if not any(groups.values()):
            log("WARNING: after category filtering, nothing matched - collection.db will be empty. "
                "Check --categories against the valid category names above.")

    if min_star is not None or max_star is not None:
        any_known = any(sr is not None for entries in groups.values() for _, _, sr in entries)
        if not any_known:
            log("Note: star rating isn't available in this CSV (only present when the original scan "
                "used the osu!lazer realm fast path) - nothing will match this star rating filter.")

        def in_range(entry):
            _, _, sr = entry
            if sr is None:
                return False
            if min_star is not None and sr < min_star:
                return False
            if max_star is not None and sr > max_star:
                return False
            return True

        groups = {label: [e for e in entries if in_range(e)] for label, entries in groups.items()}

    # Same placement and reasoning as build_output_collections()'s copy: after
    # category/star filtering, before ranked handling. This path can't share
    # that function because it works on (md5, status, stars) tuples read back
    # from the CSV rather than DiffInfo objects.
    if combine_jumps:
        combined = [e for label in JUMP_CATEGORIES for e in groups.get(label, [])]
        if combined:
            rebuilt = {}
            for label, entries in groups.items():
                rebuilt[label] = entries
                if label == JUMP_CATEGORIES[-1]:
                    rebuilt[COMBINED_JUMPS_LABEL] = combined
            if COMBINED_JUMPS_LABEL not in rebuilt:
                rebuilt[COMBINED_JUMPS_LABEL] = combined
            groups = rebuilt

    if ranked_mode != "all_together":
        any_known = any(status is not None for entries in groups.values() for _, status, _ in entries)
        if not any_known:
            log("Note: ranked status isn't available in this CSV (only present when the original scan "
                "used the osu!lazer realm fast path) - all diffs are being treated as unranked for this filter/split.")

        if ranked_mode == "ranked_only":
            groups = {label: [(h, s, sr) for h, s, sr in entries if is_ranked(s)] for label, entries in groups.items()}
        elif ranked_mode == "unranked_only":
            groups = {label: [(h, s, sr) for h, s, sr in entries if not is_ranked(s)] for label, entries in groups.items()}
        elif ranked_mode == "split":
            split_groups = {}
            for label, entries in groups.items():
                ranked = [h for h, s, _ in entries if is_ranked(s)]
                unranked = [h for h, s, _ in entries if not is_ranked(s)]
                if ranked:
                    split_groups[f"{label} - Ranked"] = ranked
                if unranked:
                    split_groups[f"{label} - Unranked"] = unranked
            groups = split_groups

    if ranked_mode != "split":
        groups = {label: [h for h, _, _ in entries] for label, entries in groups.items()}

    collections = {label: hashes for label, hashes in groups.items() if hashes}
    if not collections:
        log("WARNING: no maps matched your combined filters (categories/ranked/star range) - "
            "collection.db will be written but empty.")
    write_collection_db(output_db, collections)
    log(f"Processed {total} rows ({skipped} skipped - missing file or inside an unreadable .osz entry).")
    log(f"collection.db written to {output_db}")
    counts = {label: len(hashes) for label, hashes in groups.items()}
    log("Summary (by dominant pattern):")
    for label, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        log(f"  {label}: {count}")
    return {"total": total, "skipped": skipped, "groups": groups, "counts": counts}


def main():
    ap = argparse.ArgumentParser(description="Classify osu! maps by burst/stream/jump content.")
    ap.add_argument("songs_folder", nargs="?", default=None,
                     help="Path to your osu! install folder for stable (not Songs/ itself - osu!.db "
                          "lives one level up from there), your lazer data folder, or a plain folder "
                          "of exported .osz/.osu files. Not needed with --from-csv.")
    ap.add_argument("--from-csv", default=None,
                     help="Rebuild collection.db from an existing report.csv instead of rescanning "
                          "(fast - just re-hashes the files already listed in the CSV)")
    ap.add_argument("--output", default="collection.db", help="Output collection.db path")
    ap.add_argument("--no-db", action="store_true", help="Only print the report, don't write a collection.db")
    ap.add_argument("--max-gap-ms", type=float, default=DEFAULT_PARAMS["max_gap_ms"],
                     help="Max milliseconds between notes to count as 'fast'. Stream BPM = 15000/ms, "
                          "so 140 is about a 107 BPM stream. Replaces the old --snap-ratio, which "
                          "compared against the map's stored BPM and broke on doubled-BPM maps.")
    ap.add_argument("--gap-consistency-tol", type=float, default=DEFAULT_PARAMS["gap_consistency_tol"],
                     help="How far a gap may stray from its run's running mean before the run is split. "
                          "A real stream doesn't change tapping speed partway through.")
    ap.add_argument("--tight-diam-ratio", type=float, default=DEFAULT_PARAMS["tight_diam_ratio"],
                     help="Max distance/circle-diameter ratio for normal stream spacing")
    ap.add_argument("--spaced-diam-ratio", type=float, default=DEFAULT_PARAMS["spaced_diam_ratio"],
                     help="Max distance/circle-diameter ratio still counted as a 'spaced stream'")
    ap.add_argument("--burst-min", type=int, default=DEFAULT_PARAMS["burst_min"],
                     help="Fewest notes that count as a burst (default 3 - three-circle bursts are "
                          "common in jump and flow-aim maps)")
    ap.add_argument("--burst-max", type=int, default=DEFAULT_PARAMS["burst_max"])
    ap.add_argument("--stream-min", type=int, default=DEFAULT_PARAMS["stream_min"])
    ap.add_argument("--jump-velocity-ratio", type=float, default=DEFAULT_PARAMS["jump_velocity_ratio"],
                     help="Jump speed cutoff in circle diameters per 100ms (note: this used to be "
                          "diameters per BEAT - the unit changed when the BPM dependence was removed)")
    ap.add_argument("--jump-pct-threshold", type=float, default=DEFAULT_PARAMS["jump_pct_threshold"])
    ap.add_argument("--jump-min-transitions", type=int, default=DEFAULT_PARAMS["jump_min_transitions"],
                     help="Minimum in-play transitions before jump percentage is allowed to decide anything")
    ap.add_argument("--jump-gap-cap-ms", type=float, default=DEFAULT_PARAMS["jump_gap_cap_ms"],
                     help="Gaps longer than this are breaks and stay out of the jump-percentage denominator")
    ap.add_argument("--stream-pct-threshold", type=float, default=DEFAULT_PARAMS["stream_pct_threshold"],
                     help="Minimum %% of a map's notes that must be in stream runs before it counts as "
                          "having streams. Mirrors --jump-pct-threshold; stops one short run in a long "
                          "jump map claiming the whole map.")
    ap.add_argument("--run-wide-fraction-max", type=float, default=DEFAULT_PARAMS["run_wide_fraction_max"])
    ap.add_argument("--mean-diam-ratio-max", type=float, default=DEFAULT_PARAMS["mean_diam_ratio_max"],
                     help="Max average distance/circle-diameter ratio across a run for it to still count "
                          "as a stream/burst rather than a jump pattern that happens to be fast-snapped")
    ap.add_argument("--cut-max-multiple", type=float, default=DEFAULT_PARAMS["cut_max_multiple"],
                     help="Largest skipped-note gap still treated as a cut inside one stream rather than "
                          "a break between two separate runs")
    ap.add_argument("--cut-max-dist-ratio", type=float, default=DEFAULT_PARAMS["cut_max_dist_ratio"],
                     help="Largest cut-transition distance (as a multiple of hit-circle diameter) still "
                          "treated as a skipped note rather than a real jump between two separate runs")
    ap.add_argument("--burst-beat-fraction-max", type=float, default=DEFAULT_PARAMS["burst_beat_fraction_max"],
                     help="Slowest snap that still counts as burst/stream tapping, as a fraction of a beat "
                          "per note. 0.4 admits 1/4 and 1/3 snap and rejects 1/2, which stops a high-BPM "
                          "jump map's ordinary 1/2 tapping from reading as bursts")
    ap.add_argument("--max-plausible-bpm", type=float, default=DEFAULT_PARAMS["max_plausible_bpm"],
                     help="Above this notated tempo, doubled-BPM authoring is the likelier reading. "
                          "Taken from the timing points so every difficulty in a mapset agrees.")
    ap.add_argument("--doubled-half-share-min", type=float,
                     default=DEFAULT_PARAMS["doubled_half_share_min"],
                     help="Share of note gaps on the notated 1/2 before that layer counts as the map's "
                          "backbone - one of two content signals for DOUBLED-BPM authoring (a 360 BPM "
                          "file that really plays at 180). The other is that the notated 1/4 is absent, "
                          "since under doubled notation it would be a real 1/8.")
    ap.add_argument("--burst-max-gap-ms", type=float, default=DEFAULT_PARAMS["burst_max_gap_ms"],
                     help="Slowest ms-per-note a run may tap and still count as a burst. Streams are "
                          "not affected. Stops a slow song's honest 1/4 (120ms at 125 BPM) reading as "
                          "a burst when it is simply not fast enough to be one.")
    ap.add_argument("--halved-quarter-share-min", type=float,
                     default=DEFAULT_PARAMS["halved_quarter_share_min"],
                     help="Share of note gaps on the notated 1/4 before that layer is treated as the "
                          "map's backbone rather than accents - one of two content signals used to spot "
                          "halved-BPM authoring (a 130 BPM file that really plays at 260). No BPM "
                          "threshold is involved; the notes decide.")
    ap.add_argument("--section-ms", type=float, default=DEFAULT_PARAMS["section_ms"],
                     help="Length of one section, in ms. Sections are how the Hybrid category tells "
                          "'jumps then streams' apart from 'both mixed evenly throughout' - coverage "
                          "alone averages those to the same numbers.")
    ap.add_argument("--section-dominance", type=float, default=DEFAULT_PARAMS["section_dominance"],
                     help="Share of a section one pattern must hold to own it (0.5 = half its notes)")
    ap.add_argument("--section-min-transitions", type=int,
                     default=DEFAULT_PARAMS["section_min_transitions"],
                     help="Fewest notes for a section to be counted at all, so a map's sparse tail "
                          "can't register as full sections and skew the proportions")
    ap.add_argument("--hybrid-section-min", type=float, default=DEFAULT_PARAMS["hybrid_section_min"],
                     help="Share of a map's sections that streams must own AND jumps must own before "
                          "it is called Hybrid rather than one or the other")
    ap.add_argument("--hybrid-balance-min", type=float, default=DEFAULT_PARAMS["hybrid_balance_min"],
                     help="How balanced that mix must be: the smaller side must own at least this "
                          "fraction of what the larger does. Stops a jump map with one stream section "
                          "counting as a mix.")
    ap.add_argument("--combine-jumps", action="store_true",
                     help="Also write a single \"Jumps\" collection containing every jump map, on top of "
                          "the separate \"Jumps with bursts\" and \"Jumps (no bursts)\" ones. Nothing is "
                          "reclassified - a jumps+bursts map simply appears in both.")
    ap.add_argument("--csv", default=None, help="Optional path to dump full per-diff results as CSV")
    ap.add_argument("--categories", default=None,
                     help="Comma-separated list of categories to include in the output collection.db "
                          "(Streams,Bursts,Jumps,Misc). Default: all of them. E.g. --categories Jumps "
                          "to only get a Jumps collection and skip writing the rest.")
    ap.add_argument("--ranked-mode", choices=["all_together", "ranked_only", "unranked_only", "split"],
                     default="all_together",
                     help="How ranked status factors into collections - only meaningful when ranked "
                          "status is available (currently only the osu!lazer realm fast path provides it). "
                          "'split' makes separate 'X - Ranked' / 'X - Unranked' collections per category.")
    ap.add_argument("--min-star", type=float, default=None,
                     help="Only include diffs with star rating >= this value (decimals OK, e.g. 4.5). "
                          "Only meaningful when star rating is available (osu!lazer realm fast path only).")
    ap.add_argument("--max-star", type=float, default=None,
                     help="Only include diffs with star rating <= this value (decimals OK, e.g. 6.5).")
    args = ap.parse_args()

    include_categories = [c.strip() for c in args.categories.split(",")] if args.categories else None

    if args.from_csv:
        collection_from_csv(args.from_csv, args.output, log_cb=print,
                             include_categories=include_categories, ranked_mode=args.ranked_mode,
                             min_star=args.min_star, max_star=args.max_star,
                             combine_jumps=args.combine_jumps)
        return

    if not args.songs_folder:
        print("Error: songs_folder is required unless --from-csv is used", file=sys.stderr)
        sys.exit(1)
    if not os.path.isdir(args.songs_folder):
        print(f"Error: {args.songs_folder} is not a directory", file=sys.stderr)
        sys.exit(1)

    params = {key: getattr(args, key) for key in DEFAULT_PARAMS}

    def cli_progress(done, total):
        if total:
            print(f"  ... {done}/{total} files scanned", end="\r")

    result = run_pipeline(
        args.songs_folder,
        output=args.output,
        csv_path=args.csv,
        write_db=not args.no_db,
        params=params,
        progress_cb=cli_progress,
        log_cb=print,
        include_categories=include_categories,
        ranked_mode=args.ranked_mode,
        min_star=args.min_star,
        max_star=args.max_star,
        combine_jumps=args.combine_jumps,
    )

    if not args.no_db:
        print("\nNote: each diff is classified by its DOMINANT pattern - a stream map with a jump")
        print("section is still a stream map. Jumps vs. Bursts is decided by which one actually")
        print("covers more of the map, not just whether a burst run exists at all.")
        print("Back up your existing collection.db before replacing it, or merge with a tool")
        print("like Piotrekol's CollectionManager rather than overwriting directly.")


if __name__ == "__main__":
    main()
