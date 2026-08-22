#!/usr/bin/env python3
"""
Tests for the pattern classifier.

Every case here is a synthetic map built note by note, so the correct answer
is known by construction rather than by eyeballing a real beatmap. That makes
these safe to run in CI and safe to lean on when changing thresholds.

They are NOT a substitute for measuring against real labelled maps - see
eval_classifier.py for that. These catch "the logic broke"; the eval catches
"the logic got less accurate".

Run with:  python test_classify.py
       or: pytest test_classify.py
"""

import math
import os

import classify_maps as cm


HEADER = (
    "osu file format v14\n\n"
    "[General]\nMode: 0\n\n"
    "[Metadata]\nTitle:Test\nVersion:Test\n\n"
    "[Difficulty]\nCircleSize:{cs}\nSliderMultiplier:1.4\n\n"
    "[TimingPoints]\n0,{bl},4,2,0,60,1,0\n{extra}\n"
    "[HitObjects]\n"
)


def build(lines, cs=4, bl=300.0, extra=""):
    text = HEADER.format(cs=cs, bl=bl, extra=extra) + "\n".join(lines)
    return cm.parse_osu_bytes(text.encode(), "test")


def circles(n, t0=1000, step=75, x0=100, dx=8):
    """n circles, `step` ms apart, `dx` osu!px apart (tight = stream spacing)."""
    return [f"{x0 + i * dx},100,{int(t0 + i * step)},1,0" for i in range(n)]


def classify(lines, **overrides):
    d = build(lines)
    params = dict(cm.DEFAULT_PARAMS)
    params.update(overrides)
    cm.classify_diff(d, **params)
    return d


# --- speed gate: absolute ms, not ratio to stored BPM ----------------------

def test_fast_run_is_a_stream():
    # 75ms per note = a 200 BPM stream (stream BPM = 15000/ms).
    d = classify(circles(16))
    assert d.has_streams
    assert cm.category_of(d) == "Streams"


def test_half_speed_tapping_is_not_a_stream():
    # 150ms per note is a ~100 BPM stream, i.e. ordinary 1/2 tapping. The old
    # snap-ratio test accepted this; measuring in absolute ms rejects it.
    d = classify(circles(16, step=150))
    assert not d.has_streams


def test_stored_bpm_does_not_affect_the_verdict():
    # Same notes, but the file claims double the tempo. This is the
    # doubled-BPM authoring trick that forced the old snap_ratio to be
    # loosened; judging in milliseconds makes it a non-issue.
    normal = build(circles(16), bl=300.0)
    doubled = build(circles(16), bl=150.0)
    for d in (normal, doubled):
        cm.classify_diff(d, **cm.DEFAULT_PARAMS)
    assert normal.has_streams and doubled.has_streams
    assert normal.stream_count == doubled.stream_count


def test_speed_change_splits_a_run():
    # 8 fast notes then 8 slow ones is not a single 16-note stream.
    d = classify(circles(8) + circles(8, t0=1000 + 8 * 75, step=150))
    assert not d.has_streams


# --- burst length ----------------------------------------------------------

def test_three_notes_is_a_burst():
    # Three circles really is a burst - common in jump and flow-aim maps.
    # Padded past MIN_OBJECTS_TO_CLASSIFY (10) with slow, stationary filler
    # notes so the floor below doesn't swallow this before burst_min gets a
    # say: filler starts >500ms after the burst (way past max_gap_ms) and is
    # itself 500ms apart, so none of it joins the burst or forms a run of its
    # own (a lone slow transition can't reach burst_min anyway).
    filler = [f"100,100,{2000 + i * 500},1,0" for i in range(7)]
    d = classify(circles(3) + filler)
    assert d.total_note_count == 10
    assert d.has_bursts
    assert d.burst_count == 1


def test_two_notes_is_not_a_burst():
    d = classify(circles(2))
    assert not d.has_bursts


def test_a_stacked_quarter_triple_is_a_burst():
    # Same shape as test_three_notes_is_a_burst, but every note sits on the
    # exact same (x,y). This is the single most common way a 1/4 triple is
    # written in 2014-2017 Insanes, and osu!'s stack leniency renders it as a
    # small staircase, so it plays as a burst and players call it one.
    #
    # A previous fix rejected zero-distance runs outright, on the strength of
    # one diff ("ESSE CARA! [INSANE!]") and a large population statistic (33.5%
    # of all burst runs). The statistic was real but not evidence of a bug -
    # a third of bursts being stacked triples is what you would expect, given
    # that is the normal shape. Thirteen hand-sorted maps supplied by the user
    # as "jumps with bursts that got called jumps with no bursts" were all
    # this exact pattern - stacked triples at 78-94ms in 160-200 BPM maps -
    # and the blanket rejection was why every one of them missed. It also only
    # half-worked on its own example: [INSANE!] came out clean, but [HARD!]
    # and [SPECIAL!] in the same mapset still reported 4 and 2 false bursts,
    # because those runs carried enough movement to dodge a spacing test.
    #
    # What that rejection was reaching for is now handled directly by the
    # rhythm gate below: a stack tapped at the map's ordinary 1/2 pulse is not
    # a burst, and that is a statement about its RHYTHM, not its spacing.
    filler = [f"100,100,{2000 + i * 500},1,0" for i in range(7)]
    d = classify(circles(3, dx=0) + filler)
    assert d.total_note_count == 10
    assert d.has_bursts
    assert d.burst_count == 1


# --- rhythm gate: a burst is a step up from the map's own pulse -------------

def test_half_snap_tapping_in_a_fast_map_is_not_a_burst():
    # 240 BPM (250ms beat), notes 125ms apart: ordinary 1/2 tapping, and the
    # bread and butter of every high-BPM jump map. It clears the 140ms
    # absolute cap, so before the rhythm gate existed every such map grew
    # phantom bursts - eleven of the user's hand-sorted "no bursts" maps
    # were exactly this, all at 120-135ms in 223-250 BPM maps.
    filler = [f"100,100,{4000 + i * 500},1,0" for i in range(7)]
    d = build(circles(3, step=125, dx=8) + filler, bl=250.0)
    cm.classify_diff(d, **cm.DEFAULT_PARAMS)
    assert not d.has_bursts
    assert d.burst_count == 0


def test_a_slow_songs_honest_quarter_is_still_not_a_burst():
    # 125 BPM (480ms beat), notes 120ms apart: a real 1/4, passing the rhythm
    # gate at exactly 0.25 of a beat - and still not a burst, because 120ms
    # per note is not burst tapping. Being a fast SNAP is not the same as
    # being FAST, which is what burst_max_gap_ms enforces.
    #
    # Reported on "Jump & Stream Practice [Arastelia's Dizzy]" (125 BPM,
    # 120ms) and the MONTAGEM BATCHI set (130 BPM, 115ms). Across all 28
    # hand-labelled difficulties the split is on speed alone: 75-94ms for the
    # maps that have bursts, 115-134ms for the ones that don't.
    filler = [f"100,100,{4000 + i * 500},1,0" for i in range(7)]
    d = build(circles(3, step=120, dx=8) + filler, bl=480.0)
    cm.classify_diff(d, **cm.DEFAULT_PARAMS)
    assert not d.has_bursts
    assert d.burst_count == 0


def test_the_same_pattern_a_bit_faster_is_a_burst():
    # The control: same shape and same 1/4 snap, but at 170 BPM (353ms beat)
    # the 1/4 is 88ms - inside the 75-94ms band the labelled burst maps
    # actually occupy. Speed is the only thing that changed.
    filler = [f"100,100,{4000 + i * 500},1,0" for i in range(7)]
    d = build(circles(3, step=88, dx=8) + filler, bl=352.9)
    cm.classify_diff(d, **cm.DEFAULT_PARAMS)
    assert d.has_bursts
    assert d.burst_count == 1


def test_the_burst_speed_cap_does_not_touch_streams():
    # burst_max_gap_ms is deliberately scoped to bursts. A sustained run at
    # 120ms - too slow to be a burst - must still register as a stream,
    # because lowering max_gap_ms instead would have silently stopped calling
    # ~130 BPM stream maps streams, a change no label here supports.
    #
    # The 1/2 filler matters: without it the map is 100% notated 1/4 at a
    # too-slow speed, which is precisely the halved-notation signature, and
    # the run would (correctly) be re-read as 1/2 tapping. Here the 1/4 layer
    # is a minority, so the stated 125 BPM is taken at face value.
    filler = [f"{60 + (i % 8) * 40},80,{1000 + i * 240},1,0" for i in range(140)]
    stream = [f"{100 + i * 8},250,{40000 + i * 120},1,0" for i in range(20)]
    d = build(filler + stream, bl=480.0)
    cm.classify_diff(d, **cm.DEFAULT_PARAMS)
    assert not cm.looks_like_halved_notation(d.objs, d.timing_points, 105.0, 0.15)
    assert d.stream_count == 1
    assert d.max_stream_len == 20
    assert d.burst_count == 0, "120ms is still too slow to be a burst"


def test_halved_bpm_notation_does_not_manufacture_bursts():
    # A mapper may notate a 236 BPM song as 118 (508ms beat). What the file
    # calls a 1/4 (127ms) is the 1/2 a player actually feels, so it is not a
    # burst - but a naive snap test would credit it as one. The gate folds
    # the notation back out first (looks_like_halved_notation, then beat_ms
    # /= 2 in classify_diff). Real example: "Chug Jug With You
    # [Cote's Zero Build Match...]", notated 118 BPM, no bursts in it.
    filler = [f"100,100,{4000 + i * 500},1,0" for i in range(7)]
    d = build(circles(3, step=127, dx=8) + filler, bl=508.47)
    cm.classify_diff(d, **cm.DEFAULT_PARAMS)
    assert not d.has_bursts


def test_doubled_bpm_notation_still_finds_bursts():
    # The other direction: a 200 BPM song notated as 400 (150ms beat) writes
    # its 1/4 at 75ms, which the file calls a 1/2. Left alone the gate would
    # reject it as ordinary tapping; looks_like_doubled_notation spots the
    # notation and classify_diff doubles the beat back out, so the burst
    # survives. 400 BPM is over max_plausible_bpm (300), which is the
    # decisive condition - and it comes from the timing points, so every
    # difficulty in a set agrees.
    filler = [f"100,100,{4000 + i * 500},1,0" for i in range(7)]
    d = build(circles(3, step=75, dx=8) + filler, bl=150.0)
    assert cm.looks_like_doubled_notation(d.objs, d.timing_points, 105.0, 0.25, 300.0),         "the fixture must actually read as doubled - otherwise this tests nothing"
    cm.classify_diff(d, **cm.DEFAULT_PARAMS)
    assert d.has_bursts
    # Without the fold the 75ms run is 1/2 of a 150ms beat, over
    # burst_beat_fraction_max, and would be thrown away. Pin that, so if the
    # doubled detector ever stops firing here the test says why.
    honest = build(circles(3, step=75, dx=8) + filler, bl=150.0)
    cm.classify_diff(honest, **dict(cm.DEFAULT_PARAMS, max_plausible_bpm=1e9))
    assert not honest.has_bursts


def test_corrupt_timing_does_not_suppress_every_run():
    # A subnormal beatLength would make `beat * burst_beat_fraction_max`
    # smaller than any real gap, silently rejecting every run in the map.
    # usable_beat_ms reports "no usable tempo" (0.0) below
    # MIN_PLAUSIBLE_BEAT_MS instead, and the gate stands down rather than
    # measuring against a number that isn't a tempo.
    assert cm.usable_beat_ms(1e-320) == 0.0
    assert cm.usable_beat_ms(float("inf")) == 0.0
    # 88ms so the burst speed cap isn't what rejects it - this is testing the
    # timing-corruption path, not burst_max_gap_ms.
    filler = [f"100,100,{4000 + i * 500},1,0" for i in range(7)]
    d = build(circles(3, step=88, dx=8) + filler, bl=1e-320)
    cm.classify_diff(d, **cm.DEFAULT_PARAMS)
    assert d.has_bursts


def test_halved_notation_is_decided_by_the_notes_not_the_stored_bpm():
    # Two conditions, both needed. Neither is a threshold on the stored BPM -
    # an earlier version used one (min_notated_bpm=125) and it was both
    # arbitrary and wrong: MONTAGEM BATCHI is notated 130 and IS halved, five
    # BPM from the line, while a correctly-notated 120 BPM map would have been
    # folded by mistake.
    def halved(lines, bl):
        d = build(lines, bl=bl)
        return cm.looks_like_halved_notation(d.objs, d.timing_points, 105.0, 0.15)

    # 130 BPM (461.5ms beat) tapping its notated 1/4 at 115ms, wall to wall:
    # too slow to be streaming, and far too much of the map to be accents.
    # That is a 260 BPM map's 1/2. (This is literally BATCHI's shape.)
    assert halved(circles(40, step=115, dx=40), 461.5)

    # 190 BPM (315.8ms beat) with the same wall-to-wall 1/4, but at 79ms -
    # that IS streaming, so the tempo is taken at face value. This is the case
    # a bare BPM threshold cannot tell apart from the one above without also
    # ruining every real stream map.
    assert not halved(circles(40, step=79, dx=8), 315.8)

    # 125 BPM with only a little 1/4 (one 4-note burst among 1/2 tapping):
    # accent content, not a backbone, so nothing is folded.
    slow = [f"{100 + i * 30},100,{1000 + i * 240},1,0" for i in range(30)]
    burst = [f"{100 + i * 8},200,{9000 + i * 120},1,0" for i in range(4)]
    assert not halved(slow + burst, 480.0)


def test_halved_notation_needs_both_signals():
    # Share alone would fold a real stream map; slowness alone would fold a
    # quiet slow map whose only 1/4 is a stray burst. Each condition has to be
    # doing work the other cannot, so flipping either one off must un-fold it.
    #
    # 130 BPM, half the map on the notated 1/4 (115ms) and half on 1/2 (230ms).
    # The two sections are far enough apart that the gap between them is a
    # break and is excluded, so the share is a clean 50%.
    quarters = [f"{40 + i * 10},100,{1000 + i * 115},1,0" for i in range(20)]
    halves = [f"{40 + i * 10},300,{10000 + i * 230},1,0" for i in range(20)]
    d = build(quarters + halves, bl=461.5)

    assert cm.looks_like_halved_notation(d.objs, d.timing_points, 105.0, 0.15)
    # Demand the 1/4 layer be 80% of the map, more than this one is -> the
    # "backbone, not accents" condition now fails, so no fold.
    assert not cm.looks_like_halved_notation(d.objs, d.timing_points, 105.0, 0.80)
    # Or say 115ms is fast enough to be a burst -> the "too slow to be real
    # burst content" condition fails instead, and again no fold.
    assert not cm.looks_like_halved_notation(d.objs, d.timing_points, 200.0, 0.15)


def test_sub_floor_diffs_are_not_classified_at_all():
    # 9 fast, tight notes - exactly what burst_max (9) would call a single
    # burst if classification ran. But MIN_OBJECTS_TO_CLASSIFY is 10, and
    # this diff only has 9 objects total, so it must never reach the burst
    # logic: too little data for a pattern verdict to mean anything.
    d = classify(circles(9))
    assert not d.has_bursts
    assert not d.has_streams
    assert not d.has_jumps
    assert cm.category_of(d) == "Misc"


def test_total_note_count_is_recorded_even_below_the_classify_floor():
    # The original form of this bug: total_note_count silently stayed at the
    # dataclass default (0) for any diff too small to classify, so the CSV
    # under-reported a real difficulty's note count. Must be accurate
    # regardless of whether classification actually ran.
    assert classify(circles(1)).total_note_count == 1
    assert classify(circles(5)).total_note_count == 5


def test_is_junk_diff():
    d9 = build(circles(9))
    d10 = build(circles(10))
    assert cm.is_junk_diff(d9)
    assert not cm.is_junk_diff(d10)
    assert cm.is_junk_diff(None)


def test_impossible_note_density_is_junk():
    # 20 notes, 25ms apart = 40/sec - well past MAX_SUSTAINED_NOTES_PER_SEC
    # (30), and past any real player's sustained tapping speed. Real library
    # check: 74,948-note "Left Behind [god has forasken us]" averaged
    # 487/sec - visually confirmed via osu_visualizer_preview.py to be an
    # audio visualizer built out of hit objects, not gameplay (see
    # AGENTS.md).
    fast = build(circles(20, step=25))
    assert cm.is_junk_diff(fast)

    # Same note count, comfortably under the line (25/sec) - a real, if very
    # hard, difficulty must not get caught by the same gate.
    slow = build(circles(20, step=40))
    assert not cm.is_junk_diff(slow)


def test_impossible_note_density_classifies_as_blank_not_a_pattern():
    # Mirrors test_sub_floor_diffs_are_not_classified_at_all - a direct
    # classify_diff() call (bypassing the scan-path is_junk_diff filter)
    # must also refuse to invent a pattern verdict for impossible density.
    d = classify(circles(20, step=25))
    assert not d.has_bursts
    assert not d.has_streams
    assert not d.has_jumps
    assert cm.category_of(d) == "Misc"


def test_junk_diffs_never_reach_scan_results():
    # The actual behaviour that matters: a sub-floor diff isn't just
    # classified as Misc, it's dropped before it's ever added to a scan's
    # results at all - so it never reaches the CSV, collection.db, or the
    # difficulty count. Verified through the real scan_folder path (temp
    # .osu files on disk), not just the is_junk_diff() helper in isolation,
    # since that's where the actual filtering happens.
    import shutil
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix="cm_junk_test_")
    try:
        junk_text = HEADER.format(cs=4, bl=300, extra="") + "\n".join(circles(9))
        real_text = HEADER.format(cs=4, bl=300, extra="") + "\n".join(circles(10))
        with open(os.path.join(tmpdir, "junk.osu"), "w", encoding="utf-8") as f:
            f.write(junk_text)
        with open(os.path.join(tmpdir, "real.osu"), "w", encoding="utf-8") as f:
            f.write(real_text)

        results, errors = cm.scan_folder(tmpdir)
        assert len(results) == 1, "the 9-object junk file should have been dropped"
        assert results[0].diff_name == "Test"
        assert not errors
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_lazer_blob_store_scan_finds_beatmaps_among_other_files():
    # scan_folder's extensionless "peek" path - how a lazer files/ store is
    # read when realm-reader isn't built. It reads through a thread pool in
    # bounded windows, and nothing covered it before: the test above only
    # exercises plain .osu files.
    #
    # Deliberately more files than one window (SCAN_READ_WORKERS * 4) so the
    # batching actually loops, and deliberately mixed with non-beatmap blobs
    # (a lazer store is mostly audio and images) so the magic-header check is
    # doing real work rather than passing everything through.
    import shutil
    import tempfile

    n_maps = cm.SCAN_READ_WORKERS * 4 + 17   # crosses a window boundary
    tmpdir = tempfile.mkdtemp(prefix="cm_blob_test_")
    try:
        real_text = HEADER.format(cs=4, bl=300, extra="") + "\n".join(circles(12))
        for i in range(n_maps):
            # Hash-style names, no extension - exactly how lazer stores them.
            with open(os.path.join(tmpdir, f"{i:064x}"), "w", encoding="utf-8") as f:
                f.write(real_text)
        for i in range(50):
            with open(os.path.join(tmpdir, f"n{i:063x}"), "wb") as f:
                f.write(b"ID3" + bytes(210))   # not a beatmap
        # One that IS a beatmap but is junk-sized: must be dropped by the same
        # is_junk_diff gate the serial version applied.
        with open(os.path.join(tmpdir, "f" * 64), "w", encoding="utf-8") as f:
            f.write(HEADER.format(cs=4, bl=300, extra="") + "\n".join(circles(9)))

        seen = []
        results, errors = cm.scan_folder(tmpdir, on_parsed=seen.append)
        assert not errors, errors
        assert len(results) == n_maps, f"expected {n_maps} beatmaps, got {len(results)}"
        # Every blob that matched must have been parsed in full, not just
        # header-sniffed - a threading bug that dropped or truncated reads
        # would show up here rather than in the count.
        assert all(len(r.objs) == 12 for r in results)
        # on_parsed is how run_pipeline classifies inline, and it must fire
        # once per kept diff from the calling thread.
        assert len(seen) == n_maps
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_scan_pools_are_not_sized_by_cpu_count():
    # These threads sit blocked in read() with the GIL released - the pool
    # hides I/O latency, it does not use cores. The old min(8, cpu*2) capped
    # a 12-core machine at 8 concurrent reads and cost ~1.4x on a real
    # library (see AGENTS.md, "Scan throughput").
    assert cm.SCAN_READ_WORKERS == 64
    # The point isn't the literal 64 so much as that it is a fixed I/O
    # concurrency figure: on this repo's own dev machine a cpu_count-derived
    # value would be 8, so if these ever coincide again the reasoning has
    # been lost.
    assert cm.SCAN_READ_WORKERS > (os.cpu_count() or 4) * 2 or (os.cpu_count() or 4) * 2 > 64


# --- cut streams -----------------------------------------------------------

def test_cut_stream_counts_as_one_stream():
    # 6 notes, one skipped beat, 6 more, all at the same speed. That is a cut
    # stream, not two bursts.
    lines = circles(6) + circles(6, t0=1000 + 6 * 75 + 150)
    d = classify(lines)
    assert d.has_streams, "a stream cut by one skipped beat is still a stream"
    assert d.has_cutstreams
    assert d.burst_count == 0


def test_real_break_does_not_merge():
    # An 8x gap is a break between two separate bursts, not a cut.
    lines = circles(6) + circles(6, t0=1000 + 6 * 75 + 600)
    d = classify(lines)
    assert not d.has_streams
    assert d.burst_count == 2


def test_a_stream_survives_more_than_one_cut():
    # Regression: the rejoin loop measured the run's note gap over the WHOLE
    # run, including cut junctions it had already merged in. A junction spans
    # a skipped note, so its gap is 2-3x the note gap, and averaging it back
    # in dragged the reference upward - each successive cut then looked like
    # a fractional multiple of a rhythm that was not there.
    #
    # On this fixture (5-note segments at 75ms, single skipped beat between
    # each) the second junction read 150/83.3 = 1.80 instead of 2.00, and
    # abs(1.80 - 2) = 0.20 failed gap_consistency_tol (0.18) by two
    # hundredths. A 15-note cut stream came out as a 10-note stream plus a
    # phantom 5-note burst, and it got worse with every extra cut.
    #
    # A stream does not stop being one stream because it was cut twice.
    step, seg = 75, 5
    for n_cuts in (1, 2, 3, 4):
        lines, t = [], 1000
        for s_i in range(n_cuts + 1):
            lines += circles(seg, t0=t)
            t += seg * step + step        # one skipped note between segments
        notes = seg * (n_cuts + 1)
        d = classify(lines)
        assert d.stream_count == 1, f"{n_cuts} cuts: expected one stream, got {d.stream_count}"
        assert d.max_stream_len == notes,             f"{n_cuts} cuts: expected a {notes}-note stream, got {d.max_stream_len}"
        assert d.burst_count == 0,             f"{n_cuts} cuts: rejoining must not leave a phantom burst behind"
        assert d.has_cutstreams


def test_the_cut_reference_gap_ignores_earlier_junctions():
    # The mechanism behind the test above, pinned directly: after one merge
    # the run holds a junction whose gap is double the note gap. If the next
    # merge's reference is taken over the whole run it reads high; taken over
    # the run's own notes it reads exactly the note gap, which is what every
    # other check in the run logic (spacing, run_gap) already uses.
    step, seg = 75, 5
    lines, t = [], 1000
    for _ in range(3):
        lines += circles(seg, t0=t)
        t += seg * step + step
    d = classify(lines)
    # 15 notes, two junctions -> 14 transitions, all claimed by the one run.
    assert d.stream_note_total == 15
    assert d.cutstream_count == 1, "one stream, cut twice - not two cut streams"


def test_cutstream_rejoin_is_rejected_when_the_cut_itself_is_a_jump():
    # Identical timing shape to test_cut_stream_counts_as_one_stream (clean
    # 2x skipped-beat gap, same speed both sides) but the skipped-beat
    # transition itself covers a huge distance - two separate stream
    # clusters on opposite sides of the playfield, not one continuous
    # stream with a quietly skipped note. Real example (ai-classification
    # branch): Night of Knights [TAG4] has cuts up to 6.8x hit-circle
    # diameter between two clusters ~430px apart on the 512px playfield -
    # visually confirmed to be a genuine jump, not a timing artifact.
    first = circles(6, x0=32)
    gap_t = 1000 + 6 * 75 + 150  # same clean 2x-gap timing as the cut-stream test
    second = circles(6, t0=gap_t, x0=464)  # far side of the playfield
    d = classify(first + second)
    assert not d.has_streams, "a real jump between two clusters must not silently rejoin as one stream"
    assert d.burst_count == 2
    assert not d.has_cutstreams


# --- mods ------------------------------------------------------------------



def test_mod_adjustments_match_osu():
    # DT is ModRateAdjust with SpeedChange 1.5.
    assert cm.mod_adjustments(["DT"], 4.0) == (1.5, 4.0)
    # HR is CS * 1.3, capped at 10 (OsuModHardRock.ApplyToDifficulty).
    assert cm.mod_adjustments(["HR"], 4.0) == (1.0, 5.2)
    assert cm.mod_adjustments(["HR"], 9.0) == (1.0, 10.0)
    assert cm.mod_adjustments(["HR", "DT"], 4.0) == (1.5, 5.2)
    assert cm.mod_adjustments(None, 4.0) == (1.0, 4.0)



# --- parsing ---------------------------------------------------------------

def test_spinners_are_dropped():
    # Spinner position is a placeholder and would invent a jump to centre.
    with_spinner = circles(4) + ["256,192,2000,12,0,4000"]
    assert len(build(with_spinner).objs) == 4


def test_slider_end_position_is_used_not_the_head():
    # A slider from (100,100) to (400,100), then a note next to its TAIL.
    # Measuring from the head would call that a full-width jump.
    lines = [
        "100,100,1000,2,0,L|400:100,1,300",
        "410,100,2000,1,0",
    ]
    d = build(lines)
    head, end = d.objs[0][2:4], d.objs[0][4:6]
    assert head == (100.0, 100.0)
    assert abs(end[0] - 400.0) < 1.0, f"slider should end near x=400, got {end}"


def test_slider_duration_is_computed():
    d = build(["100,100,1000,2,0,L|400:100,1,300"])
    start, end = d.objs[0][0], d.objs[0][1]
    # 300px at 1.4 SliderMultiplier, 300ms/beat -> 300/(100*1.4) beats.
    assert abs((end - start) - (300 / 140.0) * 300.0) < 1.0


def test_even_repeats_end_back_at_the_head():
    d = build(["100,100,1000,2,0,L|400:100,2,300"])
    assert d.objs[0][2:4] == d.objs[0][4:6]


def test_tiny_beat_length_does_not_produce_infinite_bpm():
    # A subnormal beatLength overflows 60000/bl to inf with no exception at
    # the division itself - round(inf) later crashes CSV writing with
    # "cannot convert float infinity to integer". Must be clamped at parse.
    d = build(circles(2), bl=1e-320)
    assert d.bpm == 0.0
    assert not float("inf") == d.bpm  # explicit, in case the clamp regresses to inf-passthrough


def test_non_standard_modes_are_skipped():
    text = HEADER.format(cs=4, bl=300, extra="").replace("Mode: 0", "Mode: 3")
    assert cm.parse_osu_bytes((text + "100,100,1000,1,0").encode(), "t") is None


# --- slider path geometry ---------------------------------------------------
#
# These pin the curve maths against facts that are true independently of the
# implementation - the geometry of a semicircle, the endpoint-interpolation
# property of a bezier - rather than against numbers this code produced.

def _end_of(curve, length, x0=0.0, y0=0.0):
    return cm._point_at_path_length(cm._slider_path_points(curve, x0, y0), length)


def _polyline_end(curve, length, x0=0.0, y0=0.0):
    """The old control-polygon walk, kept here as the thing being improved on."""
    pts = [(x0, y0)]
    for token in curve.split("|")[1:]:
        bits = token.split(":")
        if len(bits) == 2:
            pts.append((float(bits[0]), float(bits[1])))
    remaining = length
    for i in range(1, len(pts)):
        ax, ay = pts[i - 1]
        bx, by = pts[i]
        seg = math.hypot(bx - ax, by - ay)
        if seg <= 0:
            continue
        if remaining <= seg:
            f = remaining / seg
            return ax + (bx - ax) * f, ay + (by - ay) * f
        remaining -= seg
    return pts[-1]


def test_a_perfect_circle_slider_follows_its_arc():
    # (0,0) (50,50) (100,0) is the semicircle of radius 50 centred on (50,0).
    # Half its arc is pi*50/2 = 78.54px, which lands exactly on the top of
    # the circle at (50,50). The control polygon instead runs 78.54px into
    # its second leg and lands nearly 8px away, which is the whole point.
    half_arc = math.pi * 50 / 2
    ex, ey = _end_of("P|50:50|100:0", half_arc)
    assert abs(ex - 50) < 0.5 and abs(ey - 50) < 0.5, f"arc ended at {ex:.2f},{ey:.2f}"
    px, py = _polyline_end("P|50:50|100:0", half_arc)
    assert math.hypot(px - ex, py - ey) > 5, \
        "this fixture is supposed to separate the arc from its chord"


def test_a_bezier_ends_where_the_curve_ends_not_where_the_polygon_does():
    # Two facts true of every bezier, neither of them implementation detail:
    #   - the curve is no longer than its control polygon
    #   - the curve passes through its last control point
    # The old walk satisfies neither, because it measures along the polygon.
    curve = "B|100:0|100:100"
    path = cm._slider_path_points(curve, 0.0, 0.0)
    curve_len = sum(math.hypot(path[i][0] - path[i - 1][0],
                               path[i][1] - path[i - 1][1])
                    for i in range(1, len(path)))
    polygon_len = 200.0
    assert curve_len < polygon_len, "a bezier cannot be longer than its polygon"

    ex, ey = _end_of(curve, curve_len)
    assert abs(ex - 100) < 0.5 and abs(ey - 100) < 0.5, \
        f"a full-length bezier must end on its last control point, got {ex:.2f},{ey:.2f}"
    px, py = _polyline_end(curve, curve_len)
    assert math.hypot(px - 100, py - 100) > 30, \
        "the polygon walk should stop well short - that is the error being fixed"


def test_a_repeated_control_point_turns_a_corner():
    # osu! splits a slider at repeated ("red anchor") control points. Here the
    # repeat makes two straight segments meeting at a right angle, total
    # length exactly 100. Treated as one cubic bezier instead, the corner
    # would round off and the path would be shorter than 100.
    curve = "B|50:0|50:0|50:50"
    path = cm._slider_path_points(curve, 0.0, 0.0)
    total = sum(math.hypot(path[i][0] - path[i - 1][0],
                           path[i][1] - path[i - 1][1])
                for i in range(1, len(path)))
    assert abs(total - 100.0) < 0.5, f"expected a 100px corner, got {total:.2f}"
    ex, ey = _end_of(curve, 100.0)
    assert abs(ex - 50) < 0.5 and abs(ey - 50) < 0.5


def test_a_linear_slider_is_unchanged_by_all_of_this():
    # The control: linear sliders were already exact, and must stay exact -
    # they are 35% of every slider in the library.
    for length in (10.0, 50.0, 99.0, 100.0):
        a = _end_of("L|100:0", length)
        b = _polyline_end("L|100:0", length)
        assert math.hypot(a[0] - b[0], a[1] - b[1]) < 1e-9, \
            f"linear moved at length {length}"


def test_a_declared_length_past_the_path_extends_it():
    # osu! lengthens the final segment rather than clamping.
    ex, ey = _end_of("L|100:0", 200.0)
    assert abs(ex - 200) < 1e-6 and abs(ey) < 1e-6, f"got {ex},{ey}"


def test_extension_stops_when_the_last_two_points_coincide():
    # osu-stable performs no extension at all in this case and lazer keeps
    # the quirk. Without it a degenerate slider whose declared length dwarfs
    # its path extrapolates off the playfield - measured at 93 diameters.
    ex, ey = _end_of("L|100:0|100:0", 5000.0)
    assert abs(ex - 100) < 1e-6 and abs(ey) < 1e-6, \
        f"should have stopped at the repeated point, got {ex},{ey}"


def test_slider_duration_is_untouched_by_the_geometry_change():
    # Only the end POSITION moved. Duration comes from length/velocity and
    # must still be exactly osu!'s formula.
    parts = "0,0,1000,2,0,L|100:0,1,100".split(",")
    end_t, _, _ = cm._slider_end(parts, 1000, 0.0, 0.0, [(0.0, 500.0)], [],
                                 1.0)
    # 100px at 100*1.0 px/beat = 1 beat = 500ms.
    assert abs(end_t - 1500.0) < 1e-6, f"expected 1500, got {end_t}"


# --- jump metric -----------------------------------------------------------

def test_short_maps_cannot_be_called_jump_maps():
    # A handful of wide transitions in a tiny map is not a jump map.
    wide = [f"{100 + (i % 2) * 300},100,{1000 + i * 200},1,0" for i in range(10)]
    d = classify(wide)
    assert not d.has_jumps, "below jump_min_transitions, jump_pct must not decide"


def test_breaks_stay_out_of_the_jump_denominator():
    # Same map plus a long silent break; the break must not dilute jump_pct.
    wide = [f"{100 + (i % 2) * 300},100,{1000 + i * 200},1,0" for i in range(60)]
    base = classify(wide).jump_pct
    with_break = classify(wide + [f"{100 + (i % 2) * 300},100,{60000 + i * 200},1,0" for i in range(60)])
    assert abs(with_break.jump_pct - base) < 1.0


def test_overlapping_slider_tail_is_not_a_manufactured_jump():
    # A slider whose computed tail time lands AFTER the next note's start
    # time - real on maps where an SV change lands exactly on the slider's
    # own timestamp (see AGENTS.md) - has no meaningful "how far did the
    # cursor move in how long" to measure. The 1ms move_time floor used to
    # turn that into a manufactured, near-infinite velocity: any spacing at
    # all became a "jump", no matter how far the tail really was.
    wide = [f"{100 + (i % 2) * 300},100,{1000 + i * 200},1,0" for i in range(60)]
    base = classify(wide)

    overlap_pair = [
        # 300px path, 300ms/beat, SliderMultiplier 1.4 -> ~643ms duration,
        # tail near (400,100) at t~5643.
        "100,100,5000,2,0,L|400:100,1,300",
        # Starts at t=5100 - long before the slider's own computed end - and
        # far from its tail (424px away, vs. a ~73px hit-circle diameter).
        "100,400,5100,1,0",
    ]
    with_overlap = classify(wide + overlap_pair)

    # Only the transition INTO the slider joins the denominator; the
    # overlapping slider->next transition must be excluded, not counted.
    assert with_overlap.counted_gaps == base.counted_gaps + 1
    # jump_count isn't stored directly - reconstruct it from jump_pct's own
    # denominator (exact, since jump_pct = jump_count / counted_gaps * 100).
    base_jumps = round(base.jump_pct / 100.0 * base.counted_gaps)
    with_jumps = round(with_overlap.jump_pct / 100.0 * with_overlap.counted_gaps)
    # The huge, but meaningless, slider->next spacing must not itself
    # register as a jump - allow at most the one legitimate new jump from
    # the wide-pattern transition leading into the slider.
    assert with_jumps <= base_jumps + 1


# --- category rules --------------------------------------------------------

def test_category_of_accepts_a_diff_or_raw_values():
    d = classify(circles(16))
    assert cm.category_of(d) == cm.category_of(True, False, False, 0, 0, 0.0) == "Streams"


def test_one_short_stream_in_a_long_map_is_not_a_stream_map():
    # The NiNo case: a 12-note run inside a long map of jumps. 12 notes out of
    # ~400 is 3% coverage - nowhere near enough to call the map a stream map,
    # even when the jump content sits just under jump_pct_threshold so there
    # is technically nothing for it to lose the coverage comparison to.
    lines = circles(12) + [
        f"{100 + (i % 2) * 120},100,{3000 + i * 200},1,0" for i in range(390)
    ]
    d = classify(lines)
    assert not d.has_streams, "3% stream coverage should not flag a map as having streams"
    assert d.stream_count == 1, "the run itself should still be counted for the report"


def test_a_map_that_is_mostly_stream_still_qualifies():
    d = classify(circles(60))
    assert d.has_streams
    assert cm.category_of(d) == "Streams"


def test_stream_threshold_is_tunable():
    lines = circles(12) + [
        f"{100 + (i % 2) * 120},100,{3000 + i * 200},1,0" for i in range(390)
    ]
    assert not classify(lines).has_streams
    assert classify(lines, stream_pct_threshold=1.0).has_streams


def test_burst_map_with_a_real_stream_becomes_a_stream_map():
    # A burst map that streams even once isn't a burst map - it demands
    # sustained stream stamina somewhere, however little of the map that is.
    assert cm.category_of(False, True, False, 40, 400, 0.0,
                           stream_note_total=20, stream_run_count=1,
                           max_stream_len=20) == "Streams"


def test_a_single_minimum_length_run_does_not_promote():
    # A lone run sitting exactly on stream_min is a boundary artifact, not
    # evidence the map streams. This is the NiNo case.
    assert cm.category_of(False, True, False, 40, 400, 0.0,
                           stream_note_total=10, stream_run_count=1,
                           max_stream_len=10) == "Bursts"


def test_burst_map_with_no_stream_at_all_stays_bursts():
    assert cm.category_of(False, True, False, 40, 400, 0.0) == "Bursts"


def test_promotion_does_not_apply_to_jump_maps():
    # The whole point of the coverage floor: a short run inside a jump map is
    # usually tightly-spaced jumps, not streaming.
    assert cm.category_of(False, False, True, 0, 400, 60.0,
                           stream_note_total=20, stream_run_count=1,
                           max_stream_len=20) == "Jumps (no bursts)"


def test_promotion_threshold_is_tunable():
    kw = dict(stream_note_total=11, stream_run_count=1, max_stream_len=11)
    assert cm.category_of(False, True, False, 40, 400, 0.0, **kw) == "Bursts"
    assert cm.category_of(False, True, False, 40, 400, 0.0,
                           burst_promote_stream_len=10, **kw) == "Streams"


def test_stream_note_total_is_recorded():
    # category_of can't compare stream coverage without this being populated.
    d = classify(circles(16))
    assert d.stream_note_total == 16


# --- ranked status ---------------------------------------------------------

def test_loved_and_qualified_are_not_ranked():
    # The reported bug: realm-reader collapsed everything >= 1 in osu!'s
    # status enum to "ranked", which swept in Loved (4) and Qualified (3).
    # Neither awards pp.
    assert not cm.is_ranked("loved")
    assert not cm.is_ranked("qualified")


def test_ranked_and_approved_are_ranked():
    assert cm.is_ranked("ranked")
    assert cm.is_ranked("approved")


def test_unsubmitted_statuses_are_not_ranked():
    for status in ("graveyard", "wip", "pending", "unknown", None):
        assert not cm.is_ranked(status), status


def test_ranked_only_filter_drops_loved():
    class FakeDiff:
        def __init__(self, status):
            self.ranked_status = status
    groups = {"Streams": [FakeDiff("ranked"), FakeDiff("loved"),
                          FakeDiff("approved"), FakeDiff("qualified")]}
    kept = cm.build_output_collections(groups, ranked_mode="ranked_only")["Streams"]
    assert [d.ranked_status for d in kept] == ["ranked", "approved"]


def test_split_puts_loved_on_the_unranked_side():
    class FakeDiff:
        def __init__(self, status):
            self.ranked_status = status
    groups = {"Streams": [FakeDiff("ranked"), FakeDiff("loved")]}
    out = cm.build_output_collections(groups, ranked_mode="split")
    assert [d.ranked_status for d in out["Streams - Ranked"]] == ["ranked"]
    assert [d.ranked_status for d in out["Streams - Unranked"]] == ["loved"]


# --- one transition, one label ---------------------------------------------

def test_the_three_coverages_partition_the_same_denominator():
    # burst/stream/jump coverage are compared against each other, so they have
    # to be measured on one basis and must not overlap. Previously the run
    # pass and the jump pass ran independently and could both claim the same
    # transition; now the runs claim first and the jump test sees the rest.
    lines = (circles(14, t0=1000, step=75, x0=100, dx=8)          # a stream
             + circles(4, t0=4000, step=75, x0=60, dx=6)          # a burst
             + [f"{40 + (i % 2) * 400},90,{6000 + i * 300},1,0"   # wide jumps
                for i in range(30)])
    d = classify(lines)
    total = d.burst_transitions + d.stream_transitions + d.jump_transitions
    assert total <= d.counted_gaps, (total, d.counted_gaps)
    assert d.stream_transitions > 0 and d.jump_transitions > 0


def test_a_transition_inside_a_run_is_not_also_counted_as_a_jump():
    # A tight stream's transitions belong to the stream, full stop. If the
    # jump pass could also count them the two coverages would be comparing
    # overlapping evidence, and "which pattern owns this map" stops meaning
    # anything.
    d = classify(circles(30, step=75, dx=8))
    assert d.stream_transitions == 29
    assert d.jump_transitions == 0
    assert d.jump_pct == 0.0


def test_a_run_rejected_as_too_wide_still_counts_as_jump_evidence():
    # A fast but full-screen-spaced run is not a burst - but it is not nothing
    # either. Its transitions used to be dropped on the floor; they should
    # land in the jump bucket, which is what they are.
    lines = [f"{40 + (i % 2) * 420},{90 + (i % 2) * 200},{1000 + i * 130},1,0"
             for i in range(60)]
    d = classify(lines)
    assert d.burst_count == 0, "full-screen spacing is not a burst"
    assert d.stream_count == 0
    assert d.jump_transitions > 40, d.jump_transitions
    assert d.has_jumps


def test_the_category_is_one_contest_not_an_ordered_chain():
    # Bursts cover 40% of this map, streams 20%, and there are no jumps. The
    # old cascade compared streams first, found nothing to lose to, and
    # returned Streams without ever weighing the burst evidence - order stood
    # in for strength. Ranking all three coverages together gives the honest
    # answer.
    cat = cm.category_of(
        True, True, False,          # has_streams, has_bursts, has_jumps
        48, 200, 0.0,               # burst_note_total, total_note_count, jump_pct
        21, 1, 10,                  # stream_note_total, stream_run_count, max_stream_len
        burst_run_count=8, counted_gaps=100,
    )
    assert cat == "Bursts", cat


def test_ties_still_resolve_the_way_the_old_cascade_did():
    # The cascade gave a stream-vs-jump tie to streams (>=) and a
    # burst-vs-jump tie to bursts (jumps needed a strict >). Ranking must not
    # quietly flip either of those.
    assert cm.category_of(True, False, True, 0, 100, 20.0, 21, 1, 30,
                          counted_gaps=100) == "Streams"
    assert cm.category_of(False, True, True, 28, 100, 20.0, 0, 0, 0,
                          burst_run_count=8, counted_gaps=100) == "Bursts"


# --- Hybrid: streams and jumps in different parts of the map ---------------

def _sectioned(stream_sections, jump_sections, active, **over):
    """A DiffInfo-shaped stand-in with the section counts set directly."""
    d = build(circles(12))
    cm.classify_diff(d, **cm.DEFAULT_PARAMS)
    d.has_streams = d.has_jumps = True
    d.active_sections = active
    d.stream_sections = stream_sections
    d.jump_sections = jump_sections
    p = dict(cm.DEFAULT_PARAMS)
    p.update(over)
    # Recompute just the hybrid flag the way classify_diff does.
    s, j = stream_sections / active, jump_sections / active
    bigger = max(s, j)
    bal = (min(s, j) / bigger) if bigger else 0.0
    d.has_hybrid = (s >= p["hybrid_section_min"] and j >= p["hybrid_section_min"]
                    and bal >= p["hybrid_balance_min"])
    return d


def test_a_balanced_mix_of_streams_and_jumps_is_hybrid():
    # Both patterns own a comparable share of the map's sections. Coverage
    # alone would hand this to whichever edged ahead; sections say it's both.
    d = _sectioned(stream_sections=24, jump_sections=24, active=100)
    assert d.has_hybrid
    assert cm.category_of(d) == "Hybrid"


def test_a_jump_map_with_one_stream_section_is_not_hybrid():
    # 19% streams against 61% jumps clears a flat 15% bar on both sides, but
    # jumps own three times as much - that is a jump map that has a stream
    # section in it, not a mix. This is what hybrid_balance_min is for.
    d = _sectioned(stream_sections=19, jump_sections=61, active=100)
    assert not d.has_hybrid
    assert cm.category_of(d) != "Hybrid"


def test_a_stream_map_with_some_jumps_is_not_hybrid():
    # The mirror image: 44% streams to 18% jumps.
    d = _sectioned(stream_sections=44, jump_sections=18, active=100)
    assert not d.has_hybrid


def test_hybrid_needs_both_patterns_actually_present():
    # Sections alone aren't enough - each pattern must still clear its own
    # coverage bar, so a map is never promoted on evidence too thin to count.
    d = _sectioned(stream_sections=30, jump_sections=30, active=100)
    d.has_streams = False
    assert cm.category_of(d) != "Hybrid"


def test_hybrid_strictness_follows_the_sensitivity_preset():
    # 12% each: a mix, but a thin one. Looser should take it, Balanced and
    # Stricter should not.
    for preset, want in (("Looser", True), ("Balanced", False), ("Stricter", False)):
        d = _sectioned(12, 12, 100, **cm.params_for_sensitivity(preset))
        assert d.has_hybrid is want, preset


def test_sections_ignore_stretches_too_sparse_to_mean_anything():
    # A couple of notes either side of a break must not register as whole
    # sections - they'd swamp the proportions on a quiet map.
    objs_eligible = [0, 1, 2]
    label = {0: "j", 1: "j", 2: "j"}
    objs = [(i * 100.0, i * 100.0, 0, 0, 0, 0) for i in range(4)]
    active, s, b, j = cm.section_pattern_counts(
        objs, objs_eligible, label, section_ms=2000.0, section_min_transitions=4)
    assert active == 0 and j == 0


# --- sensitivity presets ---------------------------------------------------

def test_balanced_preset_is_exactly_the_defaults():
    # The GUI's default selection must not quietly change any threshold - if
    # Balanced ever diverges from DEFAULT_PARAMS, opening the GUI and pressing
    # Run would silently classify differently from the CLI's defaults.
    assert cm.params_for_sensitivity("Balanced") == cm.DEFAULT_PARAMS


def test_presets_only_move_the_sensitivity_knobs():
    # The other settings describe what a pattern IS, not how eager we are to
    # find one. A preset that moved those would be changing the definitions.
    expected = {"max_gap_ms", "burst_beat_fraction_max",
                "stream_pct_threshold", "jump_pct_threshold",
                "hybrid_section_min"}
    for name, overrides in cm.SENSITIVITY_PRESETS.items():
        assert set(overrides) <= expected, f"{name} moves something it shouldn't: {set(overrides) - expected}"


def test_presets_are_complete_and_ordered_as_advertised():
    # Every preset must produce a full param set the classifier can take...
    for name in cm.SENSITIVITY_PRESETS:
        assert set(cm.params_for_sensitivity(name)) == set(cm.DEFAULT_PARAMS)
    # ...and "stricter" must actually be stricter in every direction it moves,
    # otherwise the labels are lying to the user.
    strict = cm.params_for_sensitivity("Stricter")
    loose = cm.params_for_sensitivity("Looser")
    base = cm.DEFAULT_PARAMS
    for key in ("max_gap_ms", "burst_beat_fraction_max"):
        assert strict[key] < base[key] < loose[key], key   # lower = harder to qualify
    for key in ("stream_pct_threshold", "jump_pct_threshold", "hybrid_section_min"):
        assert strict[key] > base[key] > loose[key], key   # higher = must cover more


def test_sensitivity_of_recognises_presets_and_hand_edits():
    # Drives the GUI's "Custom" indicator: a hand-edited threshold must stop
    # a preset radio from claiming to describe the settings.
    for name in cm.SENSITIVITY_PRESETS:
        assert cm.sensitivity_of(cm.params_for_sensitivity(name)) == name
    edited = dict(cm.DEFAULT_PARAMS)
    edited["max_gap_ms"] = 133.0
    assert cm.sensitivity_of(edited) is None


def test_unknown_preset_name_falls_back_to_defaults():
    assert cm.params_for_sensitivity("nonsense") == cm.DEFAULT_PARAMS


def test_every_param_has_a_cli_flag():
    """
    main() does `{key: getattr(args, key) for key in DEFAULT_PARAMS}`, so a
    param added without a matching argparse flag doesn't degrade gracefully -
    it makes the whole CLI die with AttributeError on every run. Caught in
    practice when the section params were added.
    """
    import argparse
    import contextlib
    import io as _io

    parser_holder = {}
    real_parse = argparse.ArgumentParser.parse_args

    def capture(self, *a, **kw):
        parser_holder["p"] = self
        raise SystemExit(0)      # stop before main() does any real work

    argparse.ArgumentParser.parse_args = capture
    try:
        with contextlib.suppress(SystemExit), \
                contextlib.redirect_stdout(_io.StringIO()), \
                contextlib.redirect_stderr(_io.StringIO()):
            cm.main()
    finally:
        argparse.ArgumentParser.parse_args = real_parse

    parser = parser_holder.get("p")
    assert parser is not None, "could not reach the argument parser"
    dests = {a.dest for a in parser._actions}
    missing = set(cm.DEFAULT_PARAMS) - dests
    assert not missing, f"params with no CLI flag (the CLI will crash): {sorted(missing)}"


def test_integer_threshold_fields_accept_whole_number_decimals():
    """
    "40" and "40.0" are the same whole number, and a user typing either into
    an integer field means the same thing. A bare int("40.0") raises, which
    used to block the run with "Threshold fields must be numbers" pointing at
    a field that plainly held one - and silently froze the Custom/preset
    indicator, whose parse failed the same way.

    A genuine non-integer is still an error: 40.5 notes is not a thing.
    """
    import gui

    for key in cm.INT_PARAMS:
        assert gui.parse_param(key, "40") == 40
        assert gui.parse_param(key, "40.0") == 40
        assert isinstance(gui.parse_param(key, "40.0"), int)
        try:
            gui.parse_param(key, "40.5")
        except ValueError:
            pass
        else:
            raise AssertionError(f"{key} accepted a fractional note count")

    # Float params keep their decimals untouched.
    assert gui.parse_param("max_gap_ms", "137.5") == 137.5
    # And genuine junk is still rejected, for both kinds.
    for key in ("burst_min", "max_gap_ms"):
        try:
            gui.parse_param(key, "")
        except ValueError:
            pass
        else:
            raise AssertionError(f"{key} accepted an empty field")


def test_every_param_is_editable_in_the_gui():
    """
    Adding a param to DEFAULT_PARAMS without adding it to gui.py's `sections`
    list leaves it silently uneditable in the GUI - the run path would read
    self.param_vars, not find it, and fall back to the default with no error.
    Builds the real window (never shown) and compares the two sets.

    Skipped rather than failed where there is no display, so this stays safe
    to run in headless CI.
    """
    try:
        import tkinter
        import gui
    except ImportError:
        print("      (skipped - tkinter not available)")
        return
    try:
        app = gui.ClassifierGUI()
    except tkinter.TclError:
        print("      (skipped - no display)")
        return
    try:
        app.withdraw()
        app.update_idletasks()
        missing = set(cm.DEFAULT_PARAMS) - set(app.param_vars)
        extra = set(app.param_vars) - set(cm.DEFAULT_PARAMS)
        assert not missing, f"params with no GUI field: {sorted(missing)}"
        assert not extra, f"GUI fields with no such param: {sorted(extra)}"

        # The disclosure must actually toggle, and a preset must reach the
        # fields the run path reads - otherwise picking one does nothing.
        assert app.adv_open is False, "advanced panel should start collapsed"
        app._toggle_advanced()
        assert app.adv_open is True
        app._toggle_advanced()
        assert app.adv_open is False

        app.sensitivity_var.set("Stricter")
        app._apply_sensitivity()
        want = cm.params_for_sensitivity("Stricter")
        for key, var in app.param_vars.items():
            assert abs(float(var.get()) - float(want[key])) < 1e-9, key

        # A hand-edit must surface as Custom, and undoing it must clear that.
        app.param_vars["max_gap_ms"].set("133")
        app.update_idletasks()
        assert "Custom" in app.custom_sens_label.cget("text")
        app._reset_defaults()
        app.update_idletasks()
        assert app.custom_sens_label.cget("text") == ""
        assert app.sensitivity_var.get() == cm.DEFAULT_SENSITIVITY
    finally:
        app.destroy()


# --- combined "Jumps" collection ------------------------------------------

class _JumpDiff:
    """Minimal stand-in - build_output_collections only reads these two."""
    def __init__(self, name, status=None, stars=None):
        self.name = name
        self.ranked_status = status
        self.star_rating = stars


def _jump_groups():
    return {
        "Streams": [_JumpDiff("s1")],
        "Jumps with bursts": [_JumpDiff("jb1"), _JumpDiff("jb2")],
        "Jumps (no bursts)": [_JumpDiff("jn1")],
        "Misc": [_JumpDiff("m1")],
    }


def test_combine_jumps_is_off_by_default():
    out = cm.build_output_collections(_jump_groups())
    assert cm.COMBINED_JUMPS_LABEL not in out


def test_combine_jumps_adds_a_collection_without_removing_either():
    out = cm.build_output_collections(_jump_groups(), combine_jumps=True)
    # Both originals survive untouched - a jumps+bursts map is in two
    # collections, which is the point.
    assert [d.name for d in out["Jumps with bursts"]] == ["jb1", "jb2"]
    assert [d.name for d in out["Jumps (no bursts)"]] == ["jn1"]
    assert [d.name for d in out["Jumps"]] == ["jb1", "jb2", "jn1"]
    # ...and nothing non-jump leaks in.
    assert [d.name for d in out["Streams"]] == ["s1"]


def test_combined_jumps_sits_with_the_categories_it_summarises():
    # collection.db preserves insertion order and osu! displays them in it,
    # so "Jumps" belongs next to the jump categories, not after Misc.
    out = cm.build_output_collections(_jump_groups(), combine_jumps=True)
    labels = list(out)
    assert labels.index("Jumps") == labels.index("Jumps (no bursts)") + 1
    assert labels.index("Jumps") < labels.index("Misc")


def test_combined_jumps_respects_category_filtering():
    # Unchecking a category means "I don't want these maps", so they must not
    # reappear inside the combined collection by the back door.
    out = cm.build_output_collections(
        _jump_groups(), include_categories=["Jumps with bursts"], combine_jumps=True)
    assert [d.name for d in out["Jumps"]] == ["jb1", "jb2"]
    assert "Jumps (no bursts)" not in out


def test_combined_jumps_is_split_by_ranked_status_like_any_other():
    groups = {
        "Jumps with bursts": [_JumpDiff("jb1", "ranked"), _JumpDiff("jb2", "graveyard")],
        "Jumps (no bursts)": [_JumpDiff("jn1", "ranked")],
    }
    out = cm.build_output_collections(groups, ranked_mode="split", combine_jumps=True)
    assert [d.name for d in out["Jumps - Ranked"]] == ["jb1", "jn1"]
    assert [d.name for d in out["Jumps - Unranked"]] == ["jb2"]


def test_combined_jumps_is_absent_when_there_are_no_jump_maps():
    groups = {"Streams": [_JumpDiff("s1")], "Misc": [_JumpDiff("m1")],
              "Jumps with bursts": [], "Jumps (no bursts)": []}
    out = cm.build_output_collections(groups, combine_jumps=True)
    assert cm.COMBINED_JUMPS_LABEL not in out


def test_streams_win_when_they_cover_the_map():
    # 50 of 100 notes in streams vs 20% jumps - a stream map.
    assert cm.category_of(True, True, True, 10, 100, 20.0, 50) == "Streams"


def test_one_stream_in_a_jump_map_does_not_make_it_a_stream_map():
    # The reported bug: 90% jumps with a single 10-note stream came out as
    # Streams because any stream at all used to win outright.
    assert cm.category_of(True, False, True, 0, 1000, 90.0, 10) == "Jumps (no bursts)"


def test_jump_map_with_a_stream_and_bursts_lands_in_jumps_with_bursts():
    assert cm.category_of(True, True, True, 20, 1000, 90.0, 10) == "Jumps with bursts"


def test_stream_still_wins_when_there_are_no_jumps():
    # No jump content to lose to, so even a modest stream takes the map.
    assert cm.category_of(True, False, False, 0, 1000, 0.0, 10) == "Streams"


def test_jumps_vs_bursts_is_decided_by_coverage():
    # Same flags, different coverage - whichever covers more of the map wins.
    assert cm.category_of(False, True, True, 5, 100, 60.0) == "Jumps with bursts"
    assert cm.category_of(False, True, True, 80, 100, 20.0) == "Bursts"


def test_burst_vs_jump_coverage_uses_matching_denominators_when_available():
    # burst_note_total (30) and the jump count implied by jump_pct (30% of
    # 100 counted_gaps = 30) are numerically equal, but burst_note_total
    # counts NOTES while jump_pct counts TRANSITIONS - comparing them
    # without a shared basis is comparing different units. Real maps built
    # from alternating burst-cluster-then-jump sections drive these two
    # counts to near-equality by construction (ai-classification branch
    # investigation: e.g. burst_note_total=265 vs implied jump_count=262 on
    # one real map), making the old notes-vs-notes comparison essentially
    # coin-flip noise at exactly the point it's supposed to decide.
    #
    # With counted_gaps supplied, burst coverage is measured the same way
    # jump coverage always was: burst_run_count=6 separate 5-note bursts is
    # 24 transitions (each run of N notes is N-1 transitions), so burst
    # coverage becomes 24/100 = 24%, cleanly under jump's 30% - the SHAPE of
    # the map decides this now, not which measure happened to round up.
    assert cm.category_of(False, True, True, 30, 100, 30.0, 0, 0, 0,
                           burst_run_count=6, counted_gaps=100) == "Jumps with bursts"
    # Same raw numbers, no counted_gaps (an old CSV written before this
    # field existed has no way to recover it): falls back to the original
    # notes-vs-notes tie, which resolves to Bursts (the ">" test doesn't
    # award a tie to jumps) - same map, different verdict, entirely because
    # of which basis was available to compare on.
    assert cm.category_of(False, True, True, 30, 100, 30.0) == "Bursts"


def test_one_real_burst_is_enough_for_jumps_with_bursts():
    # One 3-note burst run in an 80%-jump map is still "Jumps with bursts".
    # burst_recurrence_min was briefly 2, on the reasoning that a lone run is
    # incidental - but the user's hand-sorted set says otherwise: five of the
    # thirteen maps they filed as wrongly-called "jumps with no bursts"
    # contain exactly one burst run, ~1-2% of the map, and they belong in
    # "Jumps with bursts" all the same. The population the floor was aimed at
    # was bad DETECTION (1/2-snap runs and rejected stacked triples), and it
    # is fixed where it belongs - see category_of's docstring.
    assert cm.category_of(False, True, True, 3, 100, 80.0,
                           burst_run_count=1, counted_gaps=100) == "Jumps with bursts"


def test_the_recurrence_floor_is_still_reachable():
    # Kept as a parameter even though the default is back to 1, so the
    # stricter reading stays available without re-deriving it.
    assert cm.category_of(False, True, True, 3, 100, 80.0, burst_run_count=1,
                           counted_gaps=100, burst_recurrence_min=2) == "Jumps (no bursts)"
    assert cm.category_of(False, True, True, 8, 100, 80.0, burst_run_count=2,
                           counted_gaps=100, burst_recurrence_min=2) == "Jumps with bursts"


def test_burst_run_count_zero_means_not_supplied_not_genuinely_zero():
    # burst_run_count defaults to 0, same as every pre-existing direct
    # category_of(...) call in this file uses (never passes it) - those
    # calls must keep working exactly as before, i.e. the recurrence gate
    # must NOT fire just because burst_run_count wasn't given.
    assert cm.category_of(False, True, True, 5, 100, 60.0) == "Jumps with bursts"


def test_counted_gaps_is_populated_and_matches_jump_pcts_own_denominator():
    # Wiring check: classify_diff() must actually fill in counted_gaps (not
    # just leave the dataclass default), and it has to be the exact same
    # denominator jump_pct itself was computed against - if these ever
    # drift apart, category_of()'s transitions-basis comparison silently
    # goes back to comparing incompatible numbers.
    d = classify(circles(20, step=75, dx=8))  # one dense, no-break run
    assert d.counted_gaps == len(d.objs) - 1
    assert d.jump_pct == 0.0  # tight spacing - no jumps to compute a % from
    if d.counted_gaps:
        implied_jump_count = round(d.jump_pct / 100 * d.counted_gaps)
        assert implied_jump_count == 0


# --- osu!stable database ---------------------------------------------------

def _uleb(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _str(s):
    if s == "":
        return b"\x00"
    e = s.encode("utf-8")
    return b"\x0b" + _uleb(len(e)) + e


def _build_osu_db(entries, version=20191105):
    """Builds a minimal but structurally exact osu!.db for round-trip tests."""
    import struct as st
    b = bytearray()
    b += st.pack("<i", version)
    b += st.pack("<i", 1)            # folder count
    b += b"\x01"                     # account unlocked
    b += st.pack("<q", 0)            # unlock date
    b += _str("tester")
    b += st.pack("<i", len(entries))
    for e in entries:
        for s in ["artist", "artistU", e["title"], "titleU", "creator",
                  e["diff_name"], "audio.mp3", e["md5"], e["filename"]]:
            b += _str(s)
        b += bytes([e["state"]])
        b += st.pack("<hhh", 1, 2, 3)      # circles / sliders / spinners
        b += st.pack("<q", 0)              # edit date
        b += st.pack("<ffff", 9.0, 4.0, 5.0, 8.0)
        b += st.pack("<d", 1.4)
        for mode_index in range(4):
            if mode_index == 0 and e.get("stars") is not None:
                b += st.pack("<i", 1)
                b += b"\x08" + st.pack("<i", 0)              # mods = 0 (int32)
                b += b"\x0d" + st.pack("<d", e["stars"])     # stars (double)
            else:
                b += st.pack("<i", 0)
        b += st.pack("<iii", 0, 0, 0)      # drain / total / preview
        b += st.pack("<i", 1)              # one timing point
        b += st.pack("<dd", 300.0, 0.0) + b"\x01"
        b += st.pack("<i", e["map_id"])
        b += st.pack("<i", 999)            # set id
        b += st.pack("<i", 0)              # thread id
        b += bytes(4)                      # grades
        b += st.pack("<h", 0)              # offset
        b += st.pack("<f", 0.7)            # stack leniency
        b += bytes([e["mode"]])
        b += _str("") + _str("")           # source, tags
        b += st.pack("<h", 0)              # online offset
        b += _str("")                      # title font
        b += b"\x00" + st.pack("<q", 0) + b"\x00"   # unplayed, last played, osz2
        b += _str(e["folder"])
        b += st.pack("<q", 0)              # last sync
        b += bytes(5)                      # disable-* flags
        b += st.pack("<i", 0)              # last modification
        b += bytes([0])                    # mania scroll speed
    b += st.pack("<i", 0)                  # permissions
    return bytes(b)


def _write_db(tmpname, entries, version=20191105):
    import tempfile
    path = os.path.join(tempfile.gettempdir(), tmpname)
    with open(path, "wb") as f:
        f.write(_build_osu_db(entries, version))
    return path


_DB_ENTRY = dict(title="Song", diff_name="Insane", md5="a" * 32,
                 filename="song [Insane].osu", folder="123 Artist - Song",
                 state=4, map_id=555, mode=0, stars=5.55)


def test_osu_db_round_trip():
    path = _write_db("cat_test_1.db", [_DB_ENTRY])
    rows = list(cm.read_osu_db(path))
    assert len(rows) == 1
    r = rows[0]
    assert r["folder"] == "123 Artist - Song"
    assert r["filename"] == "song [Insane].osu"
    assert r["md5"] == "a" * 32
    assert r["map_id"] == 555
    assert r["ranked_status"] == "ranked"
    assert abs(r["star_rating"] - 5.55) < 0.01


def test_osu_db_filters_non_standard_modes():
    mania = dict(_DB_ENTRY, mode=3, filename="m.osu")
    path = _write_db("cat_test_2.db", [_DB_ENTRY, mania])
    assert len(list(cm.read_osu_db(path, want_mode=0))) == 1
    assert len(list(cm.read_osu_db(path, want_mode=None))) == 2


def test_osu_db_maps_stable_status_bytes():
    # stable's status encoding differs from lazer's and the API's.
    wanted = {1: "unsubmitted", 2: "pending", 4: "ranked",
              5: "approved", 6: "qualified", 7: "loved"}
    entries = [dict(_DB_ENTRY, state=s, filename=f"{s}.osu") for s in wanted]
    rows = list(cm.read_osu_db(_write_db("cat_test_3.db", entries)))
    assert [r["ranked_status"] for r in rows] == list(wanted.values())


def test_loved_from_osu_db_is_not_ranked():
    rows = list(cm.read_osu_db(_write_db(
        "cat_test_4.db", [dict(_DB_ENTRY, state=7)])))
    assert rows[0]["ranked_status"] == "loved"
    assert not cm.is_ranked(rows[0]["ranked_status"])


def test_old_osu_db_versions_are_refused():
    # Pre-20191105 files lay records out differently; guessing would silently
    # produce garbage, so the reader refuses and the caller falls back.
    path = _write_db("cat_test_5.db", [_DB_ENTRY], version=20140609)
    try:
        list(cm.read_osu_db(path))
    except ValueError as e:
        assert "older than" in str(e)
    else:
        raise AssertionError("expected a ValueError for an outdated osu!.db")


def _main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001 - report, don't mask
            failed += 1
            print(f"  ERR  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
