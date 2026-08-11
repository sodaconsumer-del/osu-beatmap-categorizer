---
name: osu-visualizer
description: Render a beatmap difficulty against a real .osr replay's actual recorded cursor path, with correct AR approach circles, OD hit windows, and EZ/HR/DT/HT applied. Use to sanity-check how a diff plays before trusting or second-guessing classify_maps.py's Streams/Bursts/Jumps call on it.
---

# osu-visualizer

Renders what a difficulty actually looks like being played, using a real `.osr`
replay's recorded cursor samples - not a guessed/interpolated path between hit
objects. Built specifically so classification calls from `classify_maps.py`
can be checked against real gameplay instead of just numbers.

## When to use this

- The user disputes a classification and can point to (or has) a replay.
- You want to sanity-check a borderline call (e.g. a "DT flow aim" candidate)
  by actually seeing the pattern play out.
- You want to see how a diff plays differently under EZ/HR/DT/HT before
  reasoning about mod-adjusted classification.

## Actually look at it - don't generate blind

The end goal of this whole tool is that YOU can see osu! maps, not just
generate a page for the user to check. Building the HTML/PNGs and calling it
done is not enough - desyncs/bugs in this renderer are visual bugs a numeric
check can miss (a real one already shipped once: object times were wrongly
divided by a mod rate while replay frame times weren't; it took actually
watching it to catch it). Always look before reporting anything as working.

**Primary method - static PNG keyframes, no browser needed:**

```bash
pip install --quiet Pillow   # one-time, dev-only - see "Why two renderers" below

# whole-map overview (sparse - 20 evenly-spaced frames by default, NOT real
# motion, just checkpoints across the map):
python osu_visualizer_preview.py --osz set.osz --diff "Name" --osr replay.osr --out-dir keyframes/

# zoom mode - dense frames around one specific moment, actual motion
# resolution (default 15fps over a 2s window - tune --fps/--window-ms):
python osu_visualizer_preview.py --osz set.osz --diff "Name" --osr replay.osr --center-ms 8000 --window-ms 1500 --fps 15 --out-dir zoom/
```

Then `Read` each `keyframes/frame_*.png` (or `zoom/frame_*.png`) directly -
the Read tool displays images inline, so you see the actual render yourself
with zero setup, no Browser pane, no local server.

**Use the overview to find where to look, then zoom into it** - this is the
actual point of the tool: identify the pattern yourself from the frames (is
this really a burst-then-jump, a spaced stream, a technical angle change?),
compare that to what `classify_diff()` called it, and if they disagree,
that's a real discrepancy to chase down in `classify_maps.py` - not
something to explain away. Don't reach for classification code to tell you
what a section is when you can just look at it.

For scripted/custom timestamps (Python, not CLI):
`render_keyframes(diff, replay, out_dir, timestamps=[...])`, or build a
window with `window_timestamps(center_ms, window_ms, fps)`.

**Fallback - the interactive HTML/JS widget** (`build_widget_html` /
`osu_visualizer.py`'s CLI): still the right choice when a human needs to
watch it play back with controls, or to publish something shareable. The
Browser tool *can* screenshot it (`preview_start`/`navigate` to the
published Artifact URL, then `computer{action:"screenshot"}`) but has been
unreliable in practice (the pane repeatedly failed to composite frames) and
a local server was explicitly rejected as unnecessary friction for what
should be a zero-setup skill - don't reach for either unless the PNG
keyframe route above doesn't answer the question.

**Why two renderers**: the HTML/JS one is what a human watches; the PNG one
is what lets Claude see without any browser dependency at all. Both read
`compute_render_data()` for the underlying numbers, but each has its own
drawing logic (SVG+JS vs Pillow) - if you change render behavior (fade
timing, colors, letterbox math) in one, change it in the other, or what
Claude sees will silently drift from what the human sees.

If anything looks wrong in either renderer, don't guess at the fix from the
code alone - compute the same numeric check that caught the original desync
(compare raw object timestamps against raw replay frame timestamps near the
start and end of the map, or cross-correlate object position against replay
cursor position at hit time to find any residual time offset) before
changing anything.

## How to run it

```bash
python osu_visualizer.py --osu path/to/diff.osu --osr path/to/replay.osr --output out.html
# or, picking one difficulty out of a mapset archive:
python osu_visualizer.py --osz path/to/set.osz --diff "Difficulty Name" --osr path/to/replay.osr --output out.html
# override the mods the replay is shown under (independent of what it was actually recorded with):
python osu_visualizer.py --osz set.osz --diff "Name" --osr replay.osr --mods DT --output out.html
# no replay available: --osr is optional - falls back to a synthesized
# (linearly interpolated, NOT a real player's motion) cursor path, useful
# for seeing note density/spacing/pattern shape when no .osr exists:
python osu_visualizer.py --osu path/to/diff.osu --mods DT --output out.html
```

Prefer a real replay whenever one exists - synthesized paths are clearly
labeled in the UI ("synthesized path") but are a straight-line approximation,
not real movement.

Finding real replay files: osu!stable keeps them in `<install>/Replays/*.osr`,
named `<player> - <artist> - <title> [<diff>] (<date>) <mode>.osr`. Match the
difficulty name against what you're investigating.

The script writes a self-contained HTML fragment (SVG + inline JS, no
external dependencies). To show it:

- **Inline in chat**: read the file and pass its contents to the `visualize`
  MCP tool's `show_widget` (call `read_me` with `modules: ["interactive"]`
  first if you haven't this session).
- **If the file is large** (long replay = many frames, can exceed a few
  hundred KB): publish it with the `Artifact` tool instead (`file_path`
  pointing at the generated HTML) rather than inlining - `show_widget`
  requires the code inline in the tool call, which wastes context on a big
  file; `Artifact` reads from disk.

## What it gets right that a naive simulation doesn't

- **Real cursor movement**: sourced from the replay's actual (x, y, t)
  samples, not linear interpolation between hit object positions. A first
  attempt at this used interpolation and produced mechanically-smooth but
  wrong-feeling motion - real players don't move at constant velocity.
- **AR-correct approach circles**: shrink timed from `preempt_fadein_ms`
  (osu!'s own `DifficultyRange` formula), not a guessed constant.
- **Mod-aware**: EZ/HR/DT/HT all adjust CS, AR, OD and playback rate the way
  osu! actually applies them (`mod_visual_adjustments` in `osu_visualizer.py`)
  - reuses `classify_maps.mod_adjustments` for CS/rate so the two stay
  consistent with each other.

## What's still approximate

- Hit windows (great/ok/meh) are computed (`great_window_ms`) but not drawn -
  only approach circles are rendered. Add a hit-window ring if a specific
  investigation needs it.
- Slider ball movement along the curve isn't rendered, only the head/tail
  circles - the replay cursor path itself is real regardless, this only
  affects what the slider BODY looks like while playing.
- No hit/miss judgement is shown (300/100/50/miss) even though the replay
  data would allow deriving it - out of scope for pattern-classification
  sanity checks, which is what this exists for.

## Extending it

`build_widget_html(diff, replay, mods_override=None)` is the entry point if
you want to script something beyond the CLI (e.g. batch-render several
diff+replay pairs, or diff the same replay's frames against two different
mod combos side by side). `diff` is a `classify_maps.DiffInfo` (from
`parse_osu_bytes`/`load_diff`), `replay` is the dict `parse_replay()` returns.
