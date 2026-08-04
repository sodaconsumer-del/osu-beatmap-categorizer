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

    # multi-label - a diff can be any combination of these, not mutually exclusive
    has_bursts: bool = False
    has_streams: bool = False
    has_jumps: bool = False
    has_cutstreams: bool = False


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

def classify_diff(diff: DiffInfo, snap_ratio=0.30,
                   tight_diam_ratio=1.35, spaced_diam_ratio=2.0,
                   burst_min=5, burst_max=9, stream_min=10,
                   jump_velocity_ratio=2.2, jump_pct_threshold=15.0,
                   run_wide_fraction_max=0.4):
    """
    Terminology (matching osu!'s official beatmap tags):
      - burst  : 5-9 note run
      - stream : 10+ note run
      - spaced stream : a stream where notes don't overlap but spacing/rhythm
        stays consistent - still a stream, not a jump
      - cutstream : a stream where a MINORITY of notes have much larger
        spacing than the rest - still a stream overall, just with cuts
      - jump   : wide, irregular spacing between consecutive objects. This is
        an independent, spacing-only property - a map can be jump-heavy at
        any snap speed and can co-occur with bursts/streams (jump bursts,
        spaced streams, etc). It is NOT mutually exclusive with bursts/streams.

    Runs are built purely from TIMING (is this still being consecutively
    tapped fast?). Spacing is then used to judge what KIND of run it is:
    if only a minority of transitions in the run are wide-spaced, it's a
    stream/burst (possibly a cutstream). If the majority of the run is
    wide-spaced throughout (e.g. ESSE CARA!, where nearly every 1/4-snap
    note is jumped 3-5 circle diameters away), it's a jump run, not a
    stream/burst, even though the timing alone looked fast enough.
    """
    objs = diff.objs
    if len(objs) < burst_min:
        return diff

    radius = 54.4 - 4.48 * diff.circle_size
    diam = radius * 2

    # Build (is_fast, is_wide) per transition, and independently track
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
        is_wide = dist > diam * tight_diam_ratio  # beyond "tight" = spaced or wider

        norm_dist = dist / diam
        norm_time = max(gap / bl, 0.05)
        # Require BOTH a genuinely wide distance (is_wide) AND a high
        # distance/time ratio. Velocity alone isn't enough: a legitimate
        # tight stream has a tiny time-per-note by definition, which
        # inflates distance/time even when the actual spacing is small -
        # that was causing real streams to get flagged as mostly jumps.
        # Requiring is_wide too means only transitions that are BOTH
        # spaced far apart AND covered quickly count as jumps (e.g. ESSE
        # CARA!'s 1/4-snap jumps), while tight-spaced fast transitions
        # (ordinary streams, however dense) never do, regardless of how
        # small the time-per-note is.
        if is_wide and (norm_dist / norm_time) > jump_velocity_ratio:
            jump_count += 1

        transitions.append((is_fast, is_wide))

    # Group consecutive fast transitions into runs (timing-only)
    runs = []  # list of list[is_wide]
    cur = []
    for is_fast, is_wide in transitions:
        if is_fast:
            cur.append(is_wide)
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
        wide_fraction = sum(run) / len(run) if run else 0
        if wide_fraction > run_wide_fraction_max:
            continue  # this is a jump run, not a burst/stream
        if burst_min <= length <= burst_max:
            bursts.append(length)
        elif length >= stream_min:
            streams.append(length)
            if wide_fraction > 0:
                cutstreams += 1

    diff.burst_count = len(bursts)
    diff.stream_count = len(streams)
    diff.cutstream_count = cutstreams
    diff.max_burst_len = max(bursts, default=0)
    diff.max_stream_len = max(streams, default=0)
    diff.jump_pct = (jump_count / total_gaps * 100) if total_gaps else 0.0

    diff.has_bursts = len(bursts) > 0
    diff.has_streams = len(streams) > 0
    diff.has_jumps = diff.jump_pct >= jump_pct_threshold
    diff.has_cutstreams = cutstreams > 0

    return diff


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------

_OSU_MAGIC = b"osu file format"


def scan_folder(root, progress_cb=None, log_cb=None, on_parsed=None):
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

    t_start = time.time()
    log(f"Walking directory tree under {root} ...")
    osu_paths = []
    osz_paths = []
    peek_candidates = []
    for dirpath, _, filenames in os.walk(root):
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


def scan_lazer_realm(data_dir, progress_cb=None, log_cb=None, on_parsed=None, helper_path=None):
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

    try:
        proc = subprocess.run([helper, realm_path, out_path], capture_output=True, text=True, timeout=120)
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

    try:
        with open(out_path, "r", encoding="utf-8") as f:
            paths = [line.strip() for line in f if line.strip()]
    except OSError:
        paths = []
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass

    if not paths:
        log("realm-reader found no beatmap paths - falling back to filesystem scan.")
        return None

    log(f"realm-reader resolved {len(paths)} .osu files directly - parsing them now (no filesystem walk needed).")

    def emit(diff):
        if on_parsed:
            on_parsed(diff)
        return diff

    results = []
    errors = []
    for i, full in enumerate(paths):
        try:
            diff = parse_osu_file(full)
            if diff is not None and diff.objs:
                results.append(emit(diff))
        except Exception as e:
            errors.append((full, str(e)))
        if progress_cb and (i + 1) % 25 == 0:
            progress_cb(i + 1, len(paths))
        if log_cb and (i + 1) % 5000 == 0:
            log(f"  ... {i + 1}/{len(paths)} parsed")

    if progress_cb:
        progress_cb(len(paths), len(paths))

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
    Groups diffs by their EXACT combination of tags, so each diff lands in
    exactly one collection rather than being duplicated across several
    overlapping ones. A hybrid map (bursts + jumps + streams) goes into a
    collection literally named "Streams, Bursts, Jumps" - not separately
    into "Streams" AND "Bursts" AND some other "Hybrid" bucket.

    Tag order in the name is always Streams, Bursts, Cutstreams, Jumps
    (matching the CSV/report column order) regardless of which tags are
    present, so combination names stay consistent and predictable. A diff
    with none of the four tags goes into "Misc".
    """
    groups = {}
    for d in diffs:
        parts = []
        if d.has_streams:
            parts.append("Streams")
        if d.has_bursts:
            parts.append("Bursts")
        if d.has_cutstreams:
            parts.append("Cutstreams")
        if d.has_jumps:
            parts.append("Jumps")
        name = ", ".join(parts) if parts else "Misc"
        groups.setdefault(name, []).append(d)
    return groups


DEFAULT_PARAMS = dict(
    snap_ratio=0.30,
    tight_diam_ratio=1.35,
    spaced_diam_ratio=2.0,
    burst_min=3,
    burst_max=9,
    stream_min=10,
    jump_velocity_ratio=2.2,
    jump_pct_threshold=15.0,
    run_wide_fraction_max=0.4,
)


def run_pipeline(songs_folder, output=None, csv_path=None, write_db=True,
                  params=None, progress_cb=None, log_cb=None):
    """
    Core pipeline used by both the CLI and the GUI:
      1. scan_folder() over .osu/.osz files
      2. classify_diff() each result
      3. derive_collections() into the final category groups
      4. optionally write a CSV and/or collection.db

    progress_cb(done, total) is forwarded from scan_folder for a live progress bar.
    log_cb(str) receives human-readable status lines (what main() would otherwise print).
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
    if os.path.isfile(os.path.join(songs_folder, "client.realm")):
        fast_result = scan_lazer_realm(songs_folder, progress_cb=progress_cb, log_cb=log_cb, on_parsed=classify_and_free)
        if fast_result is not None:
            diffs, errors = fast_result

    if diffs is None:
        scan_root = songs_folder
        # If we were given lazer's top-level data dir (containing client.realm)
        # but the fast path wasn't usable, fall back to scanning its files/
        # subfolder rather than the whole data dir (which also has scores,
        # skins, etc. we don't need to touch).
        files_subdir = os.path.join(songs_folder, "files")
        if os.path.isfile(os.path.join(songs_folder, "client.realm")) and os.path.isdir(files_subdir):
            scan_root = files_subdir
        diffs, errors = scan_folder(scan_root, progress_cb=progress_cb, log_cb=log_cb, on_parsed=classify_and_free)
    log(f"Classified {len(diffs)} difficulties.")

    groups = derive_collections(diffs)
    counts = {label: len(members) for label, members in groups.items()}

    log("Summary (one collection per exact tag combination):")
    for label, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        log(f"  {label}: {count}")

    if csv_path:
        try:
            import csv
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["title", "diff_name", "bpm", "has_bursts", "has_streams", "has_jumps", "has_cutstreams",
                            "burst_runs", "stream_runs", "cutstream_runs", "max_burst_len",
                            "max_stream_len", "jump_pct", "path"])
                for d in diffs:
                    w.writerow([d.title, d.diff_name, round(d.bpm), d.has_bursts, d.has_streams, d.has_jumps, d.has_cutstreams,
                                d.burst_count, d.stream_count, d.cutstream_count, d.max_burst_len,
                                d.max_stream_len, round(d.jump_pct, 1), d.path])
            log(f"Full per-diff results written to {csv_path}")
        except Exception:
            import traceback
            log("ERROR writing CSV report:")
            log(traceback.format_exc())

    if write_db and output:
        try:
            collections = {label: [d.version_hash for d in members] for label, members in groups.items() if members}
            write_collection_db(output, collections)
            log(f"collection.db written to {output}")
        except Exception:
            import traceback
            log("ERROR writing collection.db:")
            log(traceback.format_exc())

    return {"diffs": diffs, "errors": errors, "groups": groups, "counts": counts}


def collection_from_csv(csv_path, output_db, log_cb=None):
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
    """
    import csv as csv_module

    def log(msg):
        if log_cb:
            log_cb(msg)

    groups = {}
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
            c = row.get("has_cutstreams") == "True"

            parts = []
            if s:
                parts.append("Streams")
            if b:
                parts.append("Bursts")
            if c:
                parts.append("Cutstreams")
            if j:
                parts.append("Jumps")
            name = ", ".join(parts) if parts else "Misc"
            groups.setdefault(name, []).append(h)

            if log_cb and total % 20000 == 0:
                log(f"  ... {total} rows processed")

    collections = {label: hashes for label, hashes in groups.items() if hashes}
    write_collection_db(output_db, collections)
    log(f"Processed {total} rows ({skipped} skipped - missing file or inside an unreadable .osz entry).")
    log(f"collection.db written to {output_db}")
    counts = {label: len(hashes) for label, hashes in groups.items()}
    log("Summary (one collection per exact tag combination):")
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
    ap.add_argument("--csv", default=None, help="Optional path to dump full per-diff results as CSV")
    args = ap.parse_args()

    if args.from_csv:
        collection_from_csv(args.from_csv, args.output, log_cb=print)
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
    )

    if not args.no_db:
        print("\nNote: each diff lands in exactly one collection, named for its exact tag combination")
        print("(e.g. 'Streams, Bursts, Jumps') - no duplicates across collections.")
        print("Back up your existing collection.db before replacing it, or merge with a tool")
        print("like Piotrekol's CollectionManager rather than overwriting directly.")


if __name__ == "__main__":
    main()
