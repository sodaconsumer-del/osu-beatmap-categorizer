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


def classify(lines, mods=None, **overrides):
    d = build(lines)
    params = dict(cm.DEFAULT_PARAMS)
    params.update(overrides)
    cm.classify_diff(d, mods=mods, **params)
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


def test_a_pure_stack_is_not_a_burst():
    # Same shape as test_three_notes_is_a_burst, but every note sits on the
    # exact same (x,y) - zero measured distance, not a cluster a player
    # would ever call a burst. Real library check: 33.5% of ALL burst runs
    # (665,952 of 1,986,316) were exactly this before the fix - confirmed
    # visually on "ESSE CARA! [INSANE!]" (see AGENTS.md).
    filler = [f"100,100,{2000 + i * 500},1,0" for i in range(7)]
    d = classify(circles(3, dx=0) + filler)
    assert d.total_note_count == 10
    assert not d.has_bursts
    assert d.burst_count == 0


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

def test_dt_turns_half_tapping_into_a_stream():
    lines = circles(16, step=150)
    assert not classify(lines).has_streams
    assert classify(lines, mods=["DT"]).has_streams


def test_nm_is_the_baseline():
    lines = circles(16)
    assert cm.category_of(classify(lines)) == cm.category_of(classify(lines, mods=["NM"]))


def test_mod_adjustments_match_osu():
    # DT is ModRateAdjust with SpeedChange 1.5.
    assert cm.mod_adjustments(["DT"], 4.0) == (1.5, 4.0)
    # HR is CS * 1.3, capped at 10 (OsuModHardRock.ApplyToDifficulty).
    assert cm.mod_adjustments(["HR"], 4.0) == (1.0, 5.2)
    assert cm.mod_adjustments(["HR"], 9.0) == (1.0, 10.0)
    assert cm.mod_adjustments(["HR", "DT"], 4.0) == (1.5, 5.2)
    assert cm.mod_adjustments(None, 4.0) == (1.0, 4.0)


def test_hr_shrinks_circles_so_spacing_reads_wider():
    # Same notes, but HR's smaller circles mean the same gap is more diameters.
    lines = circles(16, dx=60)
    assert classify(lines).jump_pct <= classify(lines, mods=["HR"]).jump_pct


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


def test_a_single_incidental_burst_does_not_win_jumps_with_bursts():
    # One 3-note burst run (burst_run_count=1) buried in an otherwise pure
    # jump map - visually confirmed on real examples (ai-classification
    # branch) to read as plain jump maps, not maps with a real "bursts"
    # character. jump_coverage (80%) trivially beats burst_coverage (a
    # single run's worth of transitions, ~2%), so without the recurrence
    # gate this would win "Jumps with bursts" on presence alone.
    assert cm.category_of(False, True, True, 3, 100, 80.0,
                           burst_run_count=1, counted_gaps=100) == "Jumps (no bursts)"
    # Two separate burst runs clears the recurrence floor - back to the
    # normal coverage comparison.
    assert cm.category_of(False, True, True, 8, 100, 80.0,
                           burst_run_count=2, counted_gaps=100) == "Jumps with bursts"


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
