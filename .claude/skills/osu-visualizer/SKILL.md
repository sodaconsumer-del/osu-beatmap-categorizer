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

Building the HTML and calling it done is not enough - the whole point is
seeing the pattern, and desyncs/bugs in this renderer are visual bugs a
numeric check can miss (a real one already shipped once: object times were
wrongly divided by a mod rate while replay frame times weren't, and it took
the user actually watching it to catch it). After publishing:

1. Open the artifact URL with the Browser tool (`preview_start` or
   `navigate`). If `computer{action:"screenshot"}` fails with "Browser pane
   is not displayed", ask the user to open the Browser pane and retry - it
   is not optional, do not skip the check because of this.
2. Click Play, `wait` a couple seconds, screenshot again. Confirm: approach
   circles are visibly shrinking toward hit circles, the cursor ring tracks
   through/near the circles it should be hitting (not offset from them),
   and circles disappear promptly after being hit rather than lingering.
3. If anything looks wrong, don't guess at the fix from the code alone -
   compute the same numeric check that caught the original desync (compare
   raw object timestamps against raw replay frame timestamps near the start
   and end of the map) before changing anything.

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
