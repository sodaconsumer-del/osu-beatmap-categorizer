# Notes for AI agents

Context for anyone (human or agent) picking this codebase up. Most of what
follows was learned the hard way — the "why" matters more than the "what",
because several of these decisions look wrong until you know what they fixed.

## The one hard rule

**Never write to an osu! installation.** Not the stable folder, not the lazer
data folder, not `Songs/`, not `osu!.db`, not `client.realm`. Read-only,
always.

Users run this against a live game install, often while playing. A stray write
risks corrupting their library or tripping anti-cheat. Every scan path opens
files `rb`; `realm-reader` opens the realm with `IsReadOnly = true`. Write
reports, CSVs and `collection.db` to an export folder the user chose — never
back into an install.

`collection.db` in particular is the user's curated collections. The tool
writes a *new* file to an export folder; it never overwrites the live one.

## Layout

| File | Role |
|---|---|
| `classify_maps.py` | Everything non-GUI: parsing, classification, the three scan paths, `collection.db` writer, CLI |
| `gui.py` | tkinter front end over `run_pipeline` |
| `test_classify.py` | Synthetic unit tests. `python test_classify.py`, no pytest needed |
| `eval_classifier.py` | Scores a `report.csv` against hand-labelled maps |
| `realm-reader/` | C# helper that reads osu!lazer's `client.realm` |

Pure standard library on the Python side. Keep it that way — the app ships as
a PyInstaller build and every dependency is another thing to bundle and
another antivirus false positive.

## Three scan paths

`run_pipeline` tries these in order, each falling back to the next:

1. **lazer** — `scan_lazer_realm`, if `client.realm` is found. Shells out to
   the `realm-reader` binary, which prints `path\tstatus\tstars\tonline_id`.
2. **stable** — `scan_stable_db`, if `osu!.db` is found next to a `Songs/`
   folder. Reads the beatmap list straight out of the database.
3. **folder walk** — `scan_folder`. Handles `.osu`, `.osz` (read in memory),
   and extensionless files sniffed for the `osu file format` magic header.

Paths 1 and 2 exist because the walk is genuinely slow: about 180 seconds on a
25k-folder library *before a single beatmap is parsed*. They also supply
ranked status, star rating and beatmap id, none of which a folder scan can
recover.

Every fallback is silent-by-design but **logged**. If you add a new failure
mode, log it — see "silent phases" below for why.

## Gotchas that cost real time

**`storage.ini` redirects lazer's data folder.** When a user moves their lazer
library, `%APPDATA%\osu` is left behind as a stub with no `client.realm` and a
near-empty `files/`. Only a `storage.ini` naming the real location. Missing
this looks exactly like "the realm reader is broken" — the scan finds nothing
and reports zero maps. `resolve_lazer_storage()` follows it.

**`Beatmap.Hash` is SHA-256, `Beatmap.MD5Hash` is MD5.** In lazer's realm, the
content-addressed `files/` store is keyed by SHA-256, and `Beatmap.Hash`
matches it. Joining against `MD5Hash` — or against an MD5 computed from file
bytes — never matches. That bug also made `realm-reader` read the entire
library to compute hashes it then failed to use, taking it from 3 seconds to
over ten minutes and blowing the subprocess timeout.

**osu!.db's status byte is its own encoding.** Not lazer's realm values, not
the API's. `4` ranked, `5` approved, `6` qualified, `7` loved. Lazer's realm
uses `1` ranked, `2` approved, `3` qualified, `4` loved, plus negatives for
graveyard/WIP/pending/none. Don't reuse one mapping for the other.

**Loved and Qualified are not ranked.** Neither awards pp. A user asking for
"ranked only" does not want the Loved section. `RANKED_STATUSES` is
`{ranked, approved}` and everything routes through `is_ranked()`.

**Silent phases make the app look frozen.** The directory walk, the
`realm-reader` subprocess and CSV writing emit no per-file progress. Users
reported both "stable scanning doesn't work" and "pause won't resume" when the
real problem was minutes of no feedback. `progress_cb(done, None)` signals an
indeterminate phase and the GUI pulses the bar. If you add a long phase,
report from inside it.

**ttk needs specific option names.** Checkbox and radio indicators use
`indicatorbackground` / `indicatorforeground`, *not* `indicatorcolor` — the
wrong name is silently ignored and you get stock white indicators on a dark
background. Entries are drawn by `Entry.field`, whose only colour knobs are
`fieldbackground` / `bordercolor` / `lightcolor`; a plain `background` does
nothing. The theme uses `clam` because the native Windows theme draws through
the OS and ignores colours entirely.

**`pack` puts leftover space to the right.** A widget packed after
`side="left"` siblings lands beside them, not below. This silently squeezed a
hint label into a narrow column where it clipped its own text. Give rows their
own frames.

## Classification

"Classification" here means one specific thing: scanning a beatmap
difficulty's hit objects and sorting it into exactly one category — Streams,
Bursts, Jumps with bursts, Jumps (no bursts), or Misc — based on the *shape*
of the movement/tapping it asks for, not its difficulty or star rating. A
5-star jump map and a 2-star jump map both classify as some flavor of
"Jumps" if the pattern shape matches; star rating never enters the decision.
`classify_diff()` is the function that does this; `category_of()` turns its
output flags into the single final category name.

The vocabulary, for anyone new to osu! pattern terms or this codebase:

- **Hit object** — a circle, slider, or spinner in a difficulty. Spinners are
  dropped before classification (their stored position is a meaningless
  placeholder, not where the cursor goes).
- **Transition** — the movement from one hit object to the next: a gap in
  time (`tap_gap`), a travel time (`move_time`, differs from `tap_gap` only
  on sliders), and a distance (`dist`), measured from the *previous* object's
  END position, not its start — see "Spacing is measured from..." below.
- **Run** — a sequence of consecutive transitions that are both fast (gap
  under `max_gap_ms`) and rhythmically consistent (gap doesn't drift more
  than `gap_consistency_tol` from the run's own running average). Runs are
  built from TIMING alone; what kind of run it is (see below) is then
  decided by SPACING. This split matters: a fast, evenly-spaced sequence of
  wide jumps is still a "run" by the timing test, but its spacing marks it as
  a jump run, not a burst or stream.
- **Burst** — a run of 3–9 notes.
- **Stream** — a run of 10+ notes. "Spaced stream" is the same thing with
  non-overlapping but still deliberate, readable spacing (spacing tier
  "spaced" below) — still a stream, not a jump.
- **Cutstream** — a stream broken by one skipped beat (a gap that's a clean
  whole-number multiple of the run's own tempo) and rejoined into one run
  rather than scored as two shorter ones.
- **Jump** — spacing-only, independent of speed or run membership: a
  transition whose distance is genuinely wide relative to circle size *and*
  whose distance/time ratio is high. A map can be jump-heavy at any snap
  speed, and jumps can coexist with bursts/streams in the same difficulty
  (hence "Jumps with bursts" as its own category, separate from "Jumps (no
  bursts)").
- **Spacing tiers** — every transition's distance, relative to hit-circle
  diameter, falls into one of three tiers: **tight** (stacked/overlapping,
  ≤ `tight_diam_ratio`), **spaced** (readable gap but still stream-like, ≤
  `spaced_diam_ratio`), or **jump-wide** (beyond that). A run is only
  rejected as "actually a jump, not a burst/stream" if too much of it falls
  in the jump-wide tier — see `run_wide_fraction_max`/`mean_diam_ratio_max`
  in the params table.
- **Coverage** — what fraction of the *map* (by note count, for
  bursts/streams; by transition count, for jumps) a pattern actually
  occupies. Classification cares about coverage, not mere presence — a
  single 10-note stream buried in an otherwise pure jump map does not make
  the map a "stream map"; see "Patterns must out-cover each other" below.
- **NM (no mod)** — the baseline every classification runs against by
  default. Mods (DT/HT/HR/EZ) are opt-in via `--mods`/the GUI and rescale
  timing and/or circle size before the same logic runs — see "Mod math"
  below. There is currently no mod-aware classification LOGIC beyond that
  rescaling (see `osu-visualizer`/AI-classification branch work for the
  ongoing "does DT change what category this deserves" investigation).

`classify_diff` itself is tuned heuristics, so treat threshold changes as
claims that need evidence.

Decisions that look odd but aren't:

- **Speed is absolute milliseconds, not a ratio to the stored BPM.** Plenty of
  maps are authored at deliberately doubled tempo. Stream BPM is `15000 / ms`.
  An earlier ratio-based test was admitting roughly half its transitions at
  1/2 snap.
- **Runs must be rhythmically consistent.** A real stream doesn't change
  tapping speed partway through.
- **Spacing is measured from the previous object's END.** Sliders are around
  30% of hit objects; measuring head-to-head makes a long slider whose tail
  sits beside the next note read as a full-screen jump. Slider end position is
  approximated by walking the control polyline — good enough for spacing,
  and far better than using the head.
- **Spinners are dropped.** Their stored position is a placeholder.
- **Patterns must out-cover each other to own a map.** Presence alone isn't
  enough; a single run in a long jump map doesn't make it a stream map.
- **Except: a burst map that streams once (12+ notes) is a stream map.**
  Scoped to burst outcomes only — deliberately not extended to jump maps,
  where a stray run is usually tightly-spaced jumps rather than streaming.

`category_of()` is the single source of truth for category rules. Both the
live path and the `--from-csv` rebuild go through it — they used to carry
separate copies that could drift. `--from-csv` reproducing a byte-identical
`collection.db` is a good regression check.

### `category_of()` decision order

Evaluated top to bottom, first match wins:

1. `has_streams` AND (no jumps, OR stream coverage ≥ jump coverage) → **Streams**
2. Jumps and bursts both present → jump coverage > burst coverage ?
   **Jumps with bursts** : `burst_or_stream()` (see 5)
3. Bursts present (no jumps) → `burst_or_stream()` (see 5)
4. Jumps present (no bursts) → **Jumps (no bursts)**
5. `burst_or_stream()`: a run ≥ `burst_promote_stream_len` (12) exists →
   **Streams**, else **Bursts**
6. Nothing matched → **Misc**

`has_streams` itself already requires ≥15% coverage (`stream_pct_threshold`) —
it is not raw presence. So a map can contain a 10-note stream run and still
have `has_streams == False` if that run is a small fraction of the map; it
only shows up via step 5's `burst_promote_stream_len` check, which looks at
`max_stream_len` directly rather than coverage. This is deliberate — see
"Except: a burst map that streams once" above.

### `DEFAULT_PARAMS` reference

All in `classify_diff`'s signature and `DEFAULT_PARAMS` at module level, all
CLI-overridable (`--max-gap-ms`, `--burst-min`, etc.) and GUI-editable:

| param | default | meaning |
|---|---|---|
| `max_gap_ms` | 140.0 | max ms between taps to count as "fast" (stream BPM = 15000/ms) |
| `gap_consistency_tol` | 0.18 | max fractional deviation from a run's running-mean gap before it splits |
| `tight_diam_ratio` | 1.35 | dist/diameter ≤ this = "tight" (stacked) spacing |
| `spaced_diam_ratio` | 2.0 | dist/diameter ≤ this = "spaced" (still stream-like); above = jump-wide |
| `burst_min` | 3 | fewest notes to count as a burst |
| `burst_max` | 9 | most notes still a burst; above is `stream_min` territory |
| `stream_min` | 10 | fewest notes to count as a stream run |
| `jump_velocity_ratio` | 0.75 | jump speed cutoff, diameters per 100ms |
| `jump_pct_threshold` | 15.0 | min % of transitions flagged as jumps before `has_jumps` |
| `jump_min_transitions` | 40 | floor on in-play transitions before `jump_pct` can decide anything |
| `jump_gap_cap_ms` | 1000.0 | gaps longer than this are breaks, excluded from `jump_pct`'s denominator |
| `stream_pct_threshold` | 15.0 | min % of a map's notes in stream runs before `has_streams` |
| `run_wide_fraction_max` | 0.4 | max fraction of a run's transitions that may be jump-wide before it's rejected as a stream/burst |
| `mean_diam_ratio_max` | 1.5 | max *average* dist/diameter across a run before it's rejected |
| `cut_max_multiple` | 3.0 | largest skipped-note gap multiple still treated as a cut inside one stream |

Plus `burst_promote_stream_len` (12, not in `DEFAULT_PARAMS` — it's a
`category_of()` parameter, not a `classify_diff()` one, since it operates on
already-computed run lengths). `INT_PARAMS` in `classify_maps.py` lists which
of the above must parse as `int` rather than `float` when read from CLI/GUI
text: `burst_min`, `burst_max`, `stream_min`, `jump_min_transitions`.

`MIN_OBJECTS_TO_CLASSIFY` (10, module-level constant, not in `DEFAULT_PARAMS`
and not CLI/GUI-adjustable) treats a diff under 10 hit objects as junk - a
broken upload, a storyboard-only "difficulty", a leftover test file - rather
than real gameplay worth reporting on. `is_junk_diff()` gates every scan path
(`scan_folder`, `scan_lazer_realm`, `scan_stable_db`) before a diff is added
to results, so these are excluded from the CSV, `collection.db`, and the
difficulty count entirely - not merely filed under Misc. `classify_diff()`
independently enforces the same floor and returns Misc-shaped defaults if
called directly on a too-small `DiffInfo` (tests do this; it's the fallback
for any caller that bypasses the scan-level gate). `total_note_count` is set
before that internal check, not after, so a direct `classify_diff()` call
still reports the real count even though nothing else runs.

### Mod math

`mod_adjustments(mods, circle_size)` → `(rate, effective_circle_size)`.
Verified against `ModDoubleTime.cs` / `OsuModHardRock.cs` in ppy/osu:

| mod | effect |
|---|---|
| DT / NC | rate × 1.5 |
| HT / DC | rate × 0.75 |
| HR | circle_size × 1.3, capped at 10.0 |
| EZ | circle_size / 2.0 |

`rate` divides every time value (gaps, durations); `effective_circle_size`
feeds the diameter that all spacing ratios are measured against. HR's
`ReflectVerticallyAlongPlayfield` is deliberately not modelled — reflecting
every object is an isometry, so pairwise distances are unchanged and nothing
spacing-based can tell the difference.

### `report.csv` columns

`title, diff_name, bpm, has_bursts, has_streams, has_jumps, has_cutstreams,
burst_runs, stream_runs, cutstream_runs, max_burst_len, max_stream_len,
jump_pct, burst_note_total, stream_note_total, total_note_count,
ranked_status, star_rating, online_id, mods, category, path`

`category` is written by the live run (via `category_of()`) so a re-read
doesn't need to recompute it — `eval_classifier.py` and `collection_from_csv`
both prefer this column and only fall back to recomputing from the raw flags
for CSVs from before it existed. `online_id`/`star_rating`/`ranked_status`
read `"unknown"` when the scan path couldn't supply them (a bare folder walk
never can — see "Three scan paths").

## Testing

```
python test_classify.py
```

Synthetic maps built note by note, so the right answer is known by
construction rather than by judgement about a real beatmap. CI runs this
before building. Add a case for every behaviour change.

For real-map accuracy, `eval_classifier.py` scores a `report.csv` against a
hand-labelled `labels.csv` of `online_id,label`. Read macro F1 and the
confusion matrix, not accuracy — the categories are lopsided. `--baseline`
compares two runs so you can tell whether a change helped or just moved errors
around.

Scraping osu!'s community beatmap tags was tried and dropped: outside the most
popular few hundred maps almost nothing carries a usertag.

**Threshold changes need measurement, not argument.** If you can't show the
effect on labelled maps, say so plainly rather than asserting an improvement.

## Build and release

Windows only. `.github/workflows/build.yml` runs the tests, then PyInstaller
for the GUI and `dotnet publish` for the helper.

The self-contained .NET publish is ~190 files and ~76MB. Those go in a
`realm-reader/` subfolder, **not** the release root — `default_realm_reader_path()`
already looks there, and dumping them beside the exe makes the folder
unnavigable. When building from source, publish to `realm-reader-dist/`
(gitignored) rather than into `realm-reader/`, which holds the source.

The helper build is `continue-on-error`, so a green check does **not** mean it
shipped. Check the assemble step's log. Tagged releases hard-fail when it's
missing, because a release without it silently degrades every lazer user to
the slow path.

## Known limitations

- **Custom stable Songs location isn't detected.** `find_stable_db()` only
  looks for `Songs/` sitting next to `osu!.db`. A user who moved their Songs
  folder via `BeatmapDirectory` in `osu!.<user>.cfg` gets silently routed to
  the slow folder-walk fallback instead of an error — worth fixing if it comes
  up, by reading that cfg key when the default `Songs/` isn't there.
- **Jump vs. stream coverage compares two different measures.** `jump_pct` is
  a percentage of *transitions*; stream/burst coverage is a percentage of
  *notes*. They're compared directly in `category_of()` (step 2 above)
  because that's the basis the original burst-vs-jump rule already used, but
  it's a proportion-vs-proportion judgement, not an exact one, and may lean
  systematically toward one side. Flagged but not fixed as of the last
  classification pass — see the `a6eab84`/`fed982c` commit messages for the
  measurements that motivated the current thresholds anyway.
- **No labelled eval set ships with the repo.** `eval_classifier.py` needs a
  `labels.csv` the user builds themselves (hand-labelled mapsets, or osu!
  API `online_id`s with known categories). There's no bundled ground truth,
  so "does this threshold change help" can only be answered by whoever has
  labels on hand.
- **`realm-reader` is prebuilt and can lag the Python.** Column counts in its
  tab-separated output have grown twice (added `online_id`, before that
  `star_rating`). Every consumer (`scan_lazer_realm`) checks `len(parts)`
  before indexing, so this is handled — but if you add a new column, add it
  at the end and keep the length checks, or old compiled binaries floating
  around break silently instead of falling back.

## Conventions

- Comments explain *why*, especially where a line looks wrong without
  history. Several of the gotchas above exist as comments at their site.
- Commit messages: what changed, why, and the evidence. Measured numbers beat
  adjectives.
- Don't commit build output. `.gitignore` covers `bin/`, `obj/`,
  `__pycache__/`, PyInstaller output, `realm-reader-dist/` and the Fody files
  the Realm weaver generates on first build.
- Never commit `.pyc` files — they embed the absolute source path, which leaks
  the building user's directory layout and username.
