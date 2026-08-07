#!/usr/bin/env python3
"""
osu! Burst/Stream/Jump Classifier
----------------------------------
Scans a folder of .osu files (e.g. your stable Songs/ folder, or a folder
you exported from lazer with BeatmapExporter), classifies every difficulty
by pattern content, and writes an osu!stable-compatible collection.db
with a separate collection for each category.

Usage:
    python classify_maps.py "C:/Users/you/AppData/Local/osu!/Songs" --output collection.db

    # Just print a report, don't write a collection.db:
    python classify_maps.py "C:/path/to/Songs" --no-db

Categories (default thresholds, all tunable via CLI flags):
    - streams        : contains a run of 8+ consecutive stream-snapped, closely-spaced notes
    - bursts_only     : contains 3-7 note bursts but no full streams
    - jumps_only      : has jump content but no bursts/streams at all
    - plain           : none of the above (low density / normal play)

A note-to-note transition only counts toward a burst/stream run if BOTH:
    1. It's fast relative to the map's current BPM (<= snap_ratio * beat length)
    2. It's spatially close (<= jump_diam_ratio * circle diameter)
Fast-but-far transitions are jumps, not bursts, regardless of how tight the
timing is (e.g. 1/4-snap jump streams at high BPM are NOT bursts).
"""

import argparse
import hashlib
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
    objs: list          # (time_ms, x, y)
    timing_points: list  # (time, beatLength) uninherited only
    circle_size: float
    bpm: float

    burst_count: int = 0
    stream_count: int = 0
    cutstream_count: int = 0
    max_burst_len: int = 0
    max_stream_len: int = 0
    jump_pct: float = 0.0

    # Total notes covered by burst runs (not just run count) and total note
    # count for the diff - used to compare burst coverage against jump
    # coverage proportionally, rather than treating "any burst run at all"
    # as equally significant regardless of how small it is relative to the
    # rest of the map.
    burst_note_total: int = 0
    total_note_count: int = 0

    # multi-label - a diff can be any combination of these, not mutually exclusive
    has_bursts: bool = False
    has_streams: bool = False
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

    mode = int(mode_m.group(1).strip()) if mode_m else 0
    if mode != 0:
        return None  # only classic osu!standard for now (bursts/streams are a std concept)

    title = title_m.group(1).strip() if title_m else display_name
    diff_name = diff_m.group(1).strip() if diff_m else "?"
    cs = float(cs_m.group(1).strip()) if cs_m else 4.0

    tp_section = re.search(r"\[TimingPoints\](.*?)(\[|$)", text, re.S)
    timing_points = []
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
    timing_points.sort()

    ho_section = re.search(r"\[HitObjects\](.*?)(\[|$)", text, re.S)
    objs = []
    if ho_section:
        for line in ho_section.group(1).splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(",")
            if len(parts) < 3:
                continue
            try:
                x = float(parts[0])
                y = float(parts[1])
                t = int(parts[2])
            except ValueError:
                continue
            objs.append((t, x, y))
    objs.sort(key=lambda o: o[0])

    bpm = 60000.0 / timing_points[0][1] if timing_points else 0.0

    return DiffInfo(
        path=path,
        title=title,
        diff_name=diff_name,
        version_hash=md5,
        objs=objs,
        timing_points=timing_points,
        circle_size=cs,
        bpm=bpm,
    )


def _beat_length_at(t, timing_points):
    bl = timing_points[0][1] if timing_points else 500.0
    for tp_t, tp_bl in timing_points:
        if tp_t <= t + 2:
            bl = tp_bl
        else:
            break
    return bl


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def classify_diff(diff: DiffInfo, snap_ratio=0.55,
                   tight_diam_ratio=1.35, spaced_diam_ratio=2.0,
                   burst_min=5, burst_max=9, stream_min=10,
                   jump_velocity_ratio=2.2, jump_pct_threshold=15.0,
                   run_wide_fraction_max=0.4, mean_diam_ratio_max=1.5):
    """
    Terminology (matching osu!'s official beatmap tags):
      - burst  : 5-9 note run
      - stream : 10+ note run
      - spaced stream : a stream where notes don't overlap but spacing/rhythm
        stays consistent - still a stream, not a jump.
      - cutstream : a stream where a MINORITY of notes have much larger
        spacing than the rest - still a stream overall, just with cuts
      - jump   : wide, irregular spacing between consecutive objects. This is
        an independent, spacing-only property - a map can be jump-heavy at
        any snap speed and can co-occur with bursts/streams (jump bursts,
        spaced streams, etc). It is NOT mutually exclusive with bursts/streams.

    A note-to-note transition only joins a burst/stream run if it's fast
    (<= snap_ratio * local beat length). snap_ratio defaults to 0.55 rather
    than a tighter ~0.3 (which would only catch 1/4-snap-or-faster) because
    some mappers author extreme stream/finger-control maps with the file's
    stored BPM deliberately doubled from the song's true felt tempo (for
    finer editor snap precision) - in that case, a real 1/4-snap-of-true-
    tempo stream note lands at exactly 1/2-snap of the doubled, stored
    tempo (confirmed against real "doubled BPM" stream maps: consistent
    gap/beatLength ~0.499 throughout their stream sections). A tighter
    cutoff misses these runs entirely before spacing is even considered;
    run-length filtering (burst_min/stream_min) guards against stray,
    non-stream 1/2-snap taps in normal gameplay turning into false positives.

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
    """
    objs = diff.objs
    if len(objs) < burst_min:
        return diff

    radius = 54.4 - 4.48 * diff.circle_size
    diam = radius * 2

    # Build (is_fast, is_jump_wide) per transition, and independently track
    # jump "velocity" (distance normalized by both circle size and time)
    # for the overall jump density metric.
    transitions = []
    jump_count = 0
    total_gaps = 0
    for i in range(1, len(objs)):
        t0, x0, y0 = objs[i - 1]
        t1, x1, y1 = objs[i]
        gap = t1 - t0
        dist = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        bl = _beat_length_at(t1, diff.timing_points)
        thresh = bl * snap_ratio

        total_gaps += 1
        is_fast = gap <= thresh
        # Only genuinely far spacing (beyond the "spaced stream" tier)
        # counts against a run - non-overlapping-but-readable spacing is
        # normal stream/burst/finger-control content, not a jump.
        is_jump_wide = dist > diam * spaced_diam_ratio
        # Tight = stacked/near-overlapping. Used to detect cutstreams: a
        # stream where most notes are tight but a minority are noticeably
        # wider (but still under the jump-wide cutoff) - matching osu!'s own
        # "cutstreams" tag definition.
        is_tight = dist <= diam * tight_diam_ratio

        norm_dist = dist / diam
        norm_time = max(gap / bl, 0.05)
        # Require BOTH genuinely far spacing (is_jump_wide) AND a high
        # distance/time ratio. Velocity alone isn't enough: a legitimate
        # tight stream has a tiny time-per-note by definition, which
        # inflates distance/time even when the actual spacing is small -
        # that was causing real streams to get flagged as mostly jumps.
        if is_jump_wide and (norm_dist / norm_time) > jump_velocity_ratio:
            jump_count += 1

        transitions.append((is_fast, is_jump_wide, is_tight, norm_dist))

    # Group consecutive fast transitions into runs (timing-only)
    runs = []  # list of list[(is_jump_wide, is_tight, norm_dist)]
    cur = []
    for is_fast, is_jump_wide, is_tight, norm_dist in transitions:
        if is_fast:
            cur.append((is_jump_wide, is_tight, norm_dist))
        else:
            if cur:
                runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)

    bursts = []
    streams = []
    cutstreams = 0
    for run in runs:
        length = len(run) + 1  # transitions -> note count
        if length < burst_min:
            continue
        wide_fraction = sum(1 for w, _, _ in run if w) / len(run) if run else 0
        not_tight_fraction = sum(1 for _, t, _ in run if not t) / len(run) if run else 0
        mean_dist_ratio = sum(nd for _, _, nd in run) / len(run) if run else 0
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
        if burst_min <= length <= burst_max:
            bursts.append(length)
        elif length >= stream_min:
            streams.append(length)
            if not_tight_fraction > 0:
                cutstreams += 1

    diff.burst_count = len(bursts)
    diff.stream_count = len(streams)
    diff.cutstream_count = cutstreams
    diff.max_burst_len = max(bursts, default=0)
    diff.max_stream_len = max(streams, default=0)
    diff.jump_pct = (jump_count / total_gaps * 100) if total_gaps else 0.0
    diff.burst_note_total = sum(bursts)
    diff.total_note_count = len(objs)

    diff.has_bursts = len(bursts) > 0
    diff.has_streams = len(streams) > 0
    diff.has_jumps = diff.jump_pct >= jump_pct_threshold
    diff.has_cutstreams = cutstreams > 0

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
    for dirpath, _, filenames in os.walk(root):
        check_cancel()
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
            if diff is not None and diff.objs:
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
                        if diff is not None and diff.objs:
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
    for full in peek_candidates:
        check_cancel()
        try:
            with open(full, "rb") as f:
                head = f.read(24)
            if head.startswith(_OSU_MAGIC):
                with open(full, "rb") as f:
                    raw = f.read()
                diff = parse_osu_bytes(raw, display_name=os.path.basename(full), path=full)
                if diff is not None and diff.objs:
                    results.append(emit(diff))
                    matched += 1
        except Exception as e:
            errors.append((full, str(e)))
        done += 1
        if progress_cb and done % 200 == 0:
            progress_cb(done, total)
        if log_cb and done % 20000 == 0:
            checked = done - len(osu_paths) - len(osz_paths)
            elapsed = time.time() - t_peek_start
            rate = checked / elapsed if elapsed > 0 else 0
            log(f"  ... {checked}/{len(peek_candidates)} checked, "
                f"{matched} beatmap files found so far ({rate:.0f} files/sec)")

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
            os.path.join(d, exe_name),
            os.path.join(d, "realm-reader", exe_name),
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
                # Current format: "path\tranked_status\tstar_rating".
                # Older compiled helpers may only emit "path\tranked_status"
                # or just a bare path - handled for backward compatibility.
                parts = line.split("\t")
                path_part = parts[0]
                status_part = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
                star_part = None
                if len(parts) > 2 and parts[2].strip() and parts[2].strip().lower() != "unknown":
                    try:
                        star_part = float(parts[2].strip())
                    except ValueError:
                        star_part = None
                entries.append((path_part, status_part, star_part))
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
    for i, (full, ranked_status, star_rating) in enumerate(entries):
        if cancel_event is not None and cancel_event.is_set():
            raise ScanCancelled()
        wait_if_paused(pause_event, cancel_event)
        try:
            diff = parse_osu_file(full)
            if diff is not None and diff.objs:
                diff.ranked_status = ranked_status
                diff.star_rating = star_rating
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


def default_lazer_data_dir():
    """
    Best-guess default location of osu!lazer's top-level data folder (the
    one containing client.realm and files/) per OS. Point the tool at this
    folder (rather than files/ directly) to enable the fast realm-reader
    path when the helper is available.
    """
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA")
        return os.path.join(base, "osu") if base else None
    elif sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/osu")
    else:
        return os.path.expanduser("~/.local/share/osu")


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
    groups = {"Streams": [], "Bursts": [], "Jumps with bursts": [], "Jumps (no bursts)": [], "Misc": []}
    for d in diffs:
        if d.has_streams:
            groups["Streams"].append(d)
            continue

        if d.has_jumps and d.has_bursts:
            burst_coverage = (d.burst_note_total / d.total_note_count) if d.total_note_count else 0.0
            jump_coverage = d.jump_pct / 100.0
            if jump_coverage > burst_coverage:
                groups["Jumps with bursts"].append(d)
            else:
                groups["Bursts"].append(d)
        elif d.has_bursts:
            groups["Bursts"].append(d)
        elif d.has_jumps:
            groups["Jumps (no bursts)"].append(d)
        else:
            groups["Misc"].append(d)
    return groups


def build_output_collections(groups, include_categories=None, ranked_mode="all_together",
                              min_star=None, max_star=None, log_cb=None):
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

    if ranked_mode == "all_together":
        return groups

    any_known = any(d.ranked_status is not None for members in groups.values() for d in members)
    if not any_known:
        log("Note: ranked status isn't available for this scan (only the osu!lazer realm fast path "
            "provides it) - all diffs are being treated as unranked for this filter/split.")

    if ranked_mode == "ranked_only":
        return {label: [d for d in members if d.ranked_status == "ranked"] for label, members in groups.items()}
    elif ranked_mode == "unranked_only":
        return {label: [d for d in members if d.ranked_status != "ranked"] for label, members in groups.items()}
    elif ranked_mode == "split":
        result = {}
        for label, members in groups.items():
            ranked = [d for d in members if d.ranked_status == "ranked"]
            unranked = [d for d in members if d.ranked_status != "ranked"]
            if ranked:
                result[f"{label} - Ranked"] = ranked
            if unranked:
                result[f"{label} - Unranked"] = unranked
        return result
    return groups


DEFAULT_PARAMS = dict(
    snap_ratio=0.55,
    tight_diam_ratio=1.35,
    spaced_diam_ratio=2.0,
    burst_min=3,
    burst_max=9,
    stream_min=10,
    jump_velocity_ratio=2.2,
    jump_pct_threshold=15.0,
    run_wide_fraction_max=0.4,
    mean_diam_ratio_max=1.5,
)


def run_pipeline(songs_folder, output=None, csv_path=None, write_db=True,
                  params=None, progress_cb=None, log_cb=None, cancel_event=None,
                  include_categories=None, ranked_mode="all_together",
                  min_star=None, max_star=None, pause_event=None):
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
        classify_diff(
            d,
            snap_ratio=p["snap_ratio"],
            tight_diam_ratio=p["tight_diam_ratio"],
            spaced_diam_ratio=p["spaced_diam_ratio"],
            burst_min=p["burst_min"],
            burst_max=p["burst_max"],
            stream_min=p["stream_min"],
            jump_velocity_ratio=p["jump_velocity_ratio"],
            jump_pct_threshold=p["jump_pct_threshold"],
            run_wide_fraction_max=p["run_wide_fraction_max"],
            mean_diam_ratio_max=p["mean_diam_ratio_max"],
        )
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
    realm_data_dir = None
    if os.path.isfile(os.path.join(songs_folder, "client.realm")):
        realm_data_dir = songs_folder
    else:
        parent = os.path.dirname(os.path.normpath(songs_folder))
        if os.path.basename(os.path.normpath(songs_folder)).lower() == "files" and \
                os.path.isfile(os.path.join(parent, "client.realm")):
            realm_data_dir = parent
            log(f"Detected you're pointed at a files/ subfolder - found client.realm in the parent folder "
                f"({parent}), trying the fast path from there.")

    if realm_data_dir:
        fast_result = scan_lazer_realm(realm_data_dir, progress_cb=progress_cb, log_cb=log_cb, on_parsed=classify_and_free, cancel_event=cancel_event, pause_event=pause_event)
        if fast_result is not None:
            diffs, errors = fast_result
    else:
        log("No client.realm found (not pointed at a lazer data folder or files/ subfolder) - "
            "skipping the realm fast path.")

    if diffs is None:
        scan_root = songs_folder
        # If we were given lazer's top-level data dir (containing client.realm)
        # but the fast path wasn't usable, fall back to scanning its files/
        # subfolder rather than the whole data dir (which also has scores,
        # skins, etc. we don't need to touch).
        files_subdir = os.path.join(songs_folder, "files")
        if realm_data_dir == songs_folder and os.path.isdir(files_subdir):
            scan_root = files_subdir
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
                w.writerow(["title", "diff_name", "bpm", "has_bursts", "has_streams", "has_jumps", "has_cutstreams",
                            "burst_runs", "stream_runs", "cutstream_runs", "max_burst_len",
                            "max_stream_len", "jump_pct", "burst_note_total", "total_note_count",
                            "ranked_status", "star_rating", "path"])
                for d in diffs:
                    w.writerow([d.title, d.diff_name, round(d.bpm), d.has_bursts, d.has_streams, d.has_jumps, d.has_cutstreams,
                                d.burst_count, d.stream_count, d.cutstream_count, d.max_burst_len,
                                d.max_stream_len, round(d.jump_pct, 1), d.burst_note_total, d.total_note_count,
                                d.ranked_status or "unknown", d.star_rating if d.star_rating is not None else "unknown", d.path])
            log(f"Full per-diff results written to {csv_path}")
        except Exception:
            import traceback
            log("ERROR writing CSV report:")
            log(traceback.format_exc())

    if write_db and output:
        try:
            output_groups = build_output_collections(groups, include_categories=include_categories,
                                                       ranked_mode=ranked_mode, min_star=min_star,
                                                       max_star=max_star, log_cb=log)
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
                         min_star=None, max_star=None):
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

    groups = {"Streams": [], "Bursts": [], "Jumps with bursts": [], "Jumps (no bursts)": [], "Misc": []}
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

            b = row.get("has_bursts") == "True"
            s = row.get("has_streams") == "True"
            j = row.get("has_jumps") == "True"
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

            if s:
                groups["Streams"].append(entry)
            elif j and b:
                try:
                    burst_note_total = int(row.get("burst_note_total") or 0)
                    total_note_count = int(row.get("total_note_count") or 0)
                    jump_pct = float(row.get("jump_pct") or 0)
                except ValueError:
                    burst_note_total = total_note_count = 0
                    jump_pct = 0
                burst_coverage = (burst_note_total / total_note_count) if total_note_count else 0.0
                jump_coverage = jump_pct / 100.0
                if jump_coverage > burst_coverage:
                    groups["Jumps with bursts"].append(entry)
                else:
                    groups["Bursts"].append(entry)
            elif b:
                groups["Bursts"].append(entry)
            elif j:
                groups["Jumps (no bursts)"].append(entry)
            else:
                groups["Misc"].append(entry)

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

    if ranked_mode != "all_together":
        any_known = any(status is not None for entries in groups.values() for _, status, _ in entries)
        if not any_known:
            log("Note: ranked status isn't available in this CSV (only present when the original scan "
                "used the osu!lazer realm fast path) - all diffs are being treated as unranked for this filter/split.")

        if ranked_mode == "ranked_only":
            groups = {label: [(h, s, sr) for h, s, sr in entries if s == "ranked"] for label, entries in groups.items()}
        elif ranked_mode == "unranked_only":
            groups = {label: [(h, s, sr) for h, s, sr in entries if s != "ranked"] for label, entries in groups.items()}
        elif ranked_mode == "split":
            split_groups = {}
            for label, entries in groups.items():
                ranked = [h for h, s, _ in entries if s == "ranked"]
                unranked = [h for h, s, _ in entries if s != "ranked"]
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
                     help="Path to your osu! Songs folder (or a folder of exported .osz/.osu files). "
                          "Not needed with --from-csv.")
    ap.add_argument("--from-csv", default=None,
                     help="Rebuild collection.db from an existing report.csv instead of rescanning "
                          "(fast - just re-hashes the files already listed in the CSV)")
    ap.add_argument("--output", default="collection.db", help="Output collection.db path")
    ap.add_argument("--no-db", action="store_true", help="Only print the report, don't write a collection.db")
    ap.add_argument("--snap-ratio", type=float, default=DEFAULT_PARAMS["snap_ratio"],
                     help="Max gap/beatLength ratio to count as 'fast' timing")
    ap.add_argument("--tight-diam-ratio", type=float, default=DEFAULT_PARAMS["tight_diam_ratio"],
                     help="Max distance/circle-diameter ratio for normal stream spacing")
    ap.add_argument("--spaced-diam-ratio", type=float, default=DEFAULT_PARAMS["spaced_diam_ratio"],
                     help="Max distance/circle-diameter ratio still counted as a 'spaced stream'")
    ap.add_argument("--burst-min", type=int, default=DEFAULT_PARAMS["burst_min"])
    ap.add_argument("--burst-max", type=int, default=DEFAULT_PARAMS["burst_max"])
    ap.add_argument("--stream-min", type=int, default=DEFAULT_PARAMS["stream_min"])
    ap.add_argument("--jump-velocity-ratio", type=float, default=DEFAULT_PARAMS["jump_velocity_ratio"])
    ap.add_argument("--jump-pct-threshold", type=float, default=DEFAULT_PARAMS["jump_pct_threshold"])
    ap.add_argument("--run-wide-fraction-max", type=float, default=DEFAULT_PARAMS["run_wide_fraction_max"])
    ap.add_argument("--mean-diam-ratio-max", type=float, default=DEFAULT_PARAMS["mean_diam_ratio_max"],
                     help="Max average distance/circle-diameter ratio across a run for it to still count "
                          "as a stream/burst rather than a jump pattern that happens to be fast-snapped")
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
                             min_star=args.min_star, max_star=args.max_star)
        return

    if not args.songs_folder:
        print("Error: songs_folder is required unless --from-csv is used", file=sys.stderr)
        sys.exit(1)
    if not os.path.isdir(args.songs_folder):
        print(f"Error: {args.songs_folder} is not a directory", file=sys.stderr)
        sys.exit(1)

    params = dict(
        snap_ratio=args.snap_ratio,
        tight_diam_ratio=args.tight_diam_ratio,
        spaced_diam_ratio=args.spaced_diam_ratio,
        burst_min=args.burst_min,
        burst_max=args.burst_max,
        stream_min=args.stream_min,
        jump_velocity_ratio=args.jump_velocity_ratio,
        jump_pct_threshold=args.jump_pct_threshold,
        run_wide_fraction_max=args.run_wide_fraction_max,
        mean_diam_ratio_max=args.mean_diam_ratio_max,
    )

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
    )

    if not args.no_db:
        print("\nNote: each diff is classified by its DOMINANT pattern - a stream map with a jump")
        print("section is still a stream map. Jumps vs. Bursts is decided by which one actually")
        print("covers more of the map, not just whether a burst run exists at all.")
        print("Back up your existing collection.db before replacing it, or merge with a tool")
        print("like Piotrekol's CollectionManager rather than overwriting directly.")


if __name__ == "__main__":
    main()
