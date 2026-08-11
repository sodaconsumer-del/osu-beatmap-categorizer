"""
Unit tests for osu_visualizer.py. Replay (.osr) parsing isn't covered here -
it needs a real binary fixture, which isn't something to hand-construct or
commit; it's been validated by hand against real replay files instead (see
AGENTS.md). These cover the parts that are pure functions of known-good
inputs: AR/OD-to-ms formulas, mod adjustments, and frame decimation.
"""
import osu_visualizer as ov


def test_difficulty_range_hits_its_three_anchors():
    assert ov.difficulty_range(0, 80.0, 50.0, 20.0) == 80.0
    assert ov.difficulty_range(5, 80.0, 50.0, 20.0) == 50.0
    assert ov.difficulty_range(10, 80.0, 50.0, 20.0) == 20.0


def test_difficulty_range_interpolates_linearly_on_both_sides():
    assert ov.difficulty_range(2.5, 80.0, 50.0, 20.0) == 65.0
    assert ov.difficulty_range(7.5, 80.0, 50.0, 20.0) == 35.0


def test_preempt_shrinks_as_ar_rises():
    low_preempt, low_fade = ov.preempt_fadein_ms(3)
    high_preempt, high_fade = ov.preempt_fadein_ms(9)
    assert low_preempt > high_preempt
    assert low_fade > high_fade


def test_preempt_at_ar5_is_the_documented_1200ms():
    preempt, fade_in = ov.preempt_fadein_ms(5)
    assert preempt == 1200.0
    assert fade_in == 800.0


def test_great_window_at_od5_is_50ms():
    assert ov.great_window_ms(5) == 50.0


def test_hr_raises_ar_and_od_capped_at_10():
    rate, cs, ar, od = ov.mod_visual_adjustments(["HR"], 4.0, 9.0, 9.0)
    assert ar == 10.0  # 9 * 1.4 = 12.6, capped
    assert od == 10.0
    assert rate == 1.0  # HR doesn't touch rate


def test_ez_halves_ar_and_od():
    rate, cs, ar, od = ov.mod_visual_adjustments(["EZ"], 4.0, 8.0, 8.0)
    assert ar == 4.0
    assert od == 4.0


def test_dt_changes_rate_not_the_ar_od_numbers():
    rate, cs, ar, od = ov.mod_visual_adjustments(["DT"], 4.0, 9.0, 8.0)
    assert rate == 1.5
    assert ar == 9.0
    assert od == 8.0


def test_nm_is_a_no_op():
    rate, cs, ar, od = ov.mod_visual_adjustments([], 4.0, 9.0, 8.0)
    assert (rate, cs, ar, od) == (1.0, 4.0, 9.0, 8.0)


def test_decimate_keeps_first_and_last_frame():
    frames = [(i, float(i), float(i), 0) for i in range(0, 1000, 2)]
    out = ov._decimate_frames(frames, min_gap_ms=12)
    assert out[0] == frames[0]
    assert out[-1] == frames[-1]


def test_decimate_drops_dense_redundant_frames():
    frames = [(i, float(i), float(i), 0) for i in range(0, 100, 2)]  # every 2ms
    out = ov._decimate_frames(frames, min_gap_ms=12)
    assert len(out) < len(frames)


def test_decimate_never_drops_a_keypress_change():
    # Key flips from 0 to 1 at t=5, deep inside what would otherwise be one
    # decimated-away window - a click moment must never be silently dropped.
    frames = [(0, 0.0, 0.0, 0), (5, 1.0, 1.0, 1), (6, 2.0, 2.0, 1), (50, 3.0, 3.0, 1)]
    out = ov._decimate_frames(frames, min_gap_ms=12)
    assert (5, 1.0, 1.0, 1) in out


def test_decimate_handles_empty_and_single_frame():
    assert ov._decimate_frames([]) == []
    one = [(0, 1.0, 2.0, 0)]
    assert ov._decimate_frames(one) == one


def test_mods_from_int_decodes_dt_and_hr_together():
    dt_bit = 1 << 6
    hr_bit = 1 << 4
    assert set(ov.mods_from_int(dt_bit | hr_bit)) == {"DT", "HR"}


def test_mods_from_int_nomod_is_empty():
    assert ov.mods_from_int(0) == []


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
