# Notes for AI agents

Context for anyone (human or agent) picking this codebase up. Most of what
follows was learned the hard way — the "why" matters more than the "what",
because several of these decisions look wrong until you know what they fixed.

## About this branch (`ai-classification`)

This branch holds work that doesn't belong on `main` yet:
`osu_visualizer.py`/`osu_visualizer_preview.py` (replay-driven map viewer,
lets an agent actually look at a beatmap - see
`.claude/skills/osu-visualizer/SKILL.md`), and any classification logic
changes made from the agent's own visual judgement using that viewer, as
opposed to a change the user directed line-by-line. Named for that second
part - it's AI-driven classification tuning, so it's tracked separately
from user-directed changes until it's proven out.

Temporary by design: once the viewer has done its job (an agent spotted a
real discrepancy between what a map looks like and what `classify_diff()`
calls it, and fixed the classifier accordingly, with evidence), the
classifier fix belongs on `main` same as any other change - merge it there.
The viewer tooling itself stays here until it's no longer needed for that
work; it's not meant to ship in the release build (see "Pure standard
library" in Layout below - `osu_visualizer_preview.py` needs Pillow, which
is exactly why it isn't part of that constraint).

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
| `osu_visualizer.py` | Replay-driven (`.osr`) beatmap viewer for sanity-checking a classification against real gameplay — see `.claude/skills/osu-visualizer/SKILL.md` |
| `osu_visualizer_preview.py` | Static PNG keyframe renderer (needs `Pillow`, dev-only, not shipped) — how the agent looks at a map itself, no browser needed |
| `test_osu_visualizer.py` | Unit tests for `osu_visualizer.py`'s pure functions (AR/OD formulas, mod adjustments, frame decimation) |
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

### Scan throughput: it's I/O latency, and the pool size is not about CPUs

Every scan path is dominated by per-file `open()`/`read()` latency, not by
parsing. On a real osu!stable library (59,129 difficulties, 7200rpm SATA HDD,
Defender real-time scanning on) a *serial* read managed 22 diffs/sec — about
45ms per file, roughly 3x what a seek alone accounts for, the rest being
per-open overhead. Parsing is nowhere near that.

So both read loops overlap their I/O with a `ThreadPoolExecutor`
(`SCAN_READ_WORKERS`, 64). Two things worth knowing before touching it:

- **The pool size is deliberately not derived from `os.cpu_count()`.** These
  threads sit blocked in `read()` with the GIL released; the pool hides
  latency, it doesn't use cores, and core count says nothing about how many
  reads the storage stack will service at once. The original
  `min(8, cpu_count * 2)` capped a 12-core machine at 8 concurrent reads.
  Measured, 4 cold 1000-diff slices per setting with the order alternated:
  8 workers gave 50/51/40/43 diffs/sec, 64 gave 64/62/70/70 — every 64-run
  beat every 8-run, 1.43x, taking that library from 21.1 to 14.8 minutes.
  16 and 32 sat inside 8's noise band; 96 was erratic.
- **`scan_folder`'s lazer `files/` peek loop was serial until recently**, on
  the assumption lazer was covered by the `realm-reader` fast path. That
  helper is optional — it needs a .NET build — so a lazer user without it
  lands in the folder walk, where a 180k-blob store took over two hours at
  23 files/sec. It uses the same pool now. The blob store still scales far
  worse than stable does (it is one flat hash-named heap, so every read is a
  cold seek, where stable's diffs at least cluster by mapset folder), so if
  someone reports a slow lazer scan the real answer is still **build
  realm-reader** — the parallel walk is the fallback, not the fix.

Cancel/pause are checked per *file* inside those loops, not just per window:
the window is 4x the worker count, so at 64 workers a per-window-only check
would leave Cancel doing nothing for ~4 seconds.

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
- **Burst** — a run of 3–9 notes, at a snap that is a step up from the map's
  own pulse (1/4 or 1/3, not 1/2 — see `burst_beat_fraction_max`). Speed alone
  isn't enough: at 240 BPM an ordinary 1/2 tap is 125ms, which is fast in
  milliseconds but is just what that map taps all the way through.
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
  rescaling — figuring out whether one should exist (e.g. a DT-specific
  "flow aim" category) is exactly what the `ai-classification` branch is
  for, see the note at the top of that branch's AGENTS.md.

`classify_diff` itself is tuned heuristics, so treat threshold changes as
claims that need evidence.

Decisions that look odd but aren't:

- **Speed is absolute milliseconds, not a ratio to the stored BPM.** Plenty of
  maps are authored at deliberately doubled tempo. Stream BPM is `15000 / ms`.
  An earlier ratio-based test was admitting roughly half its transitions at
  1/2 snap.
- **...but absolute ms alone can't tell a burst from a fast map's own pulse,
  so there is now a second, narrowly-scoped rhythm gate on top.** At 240 BPM
  an ordinary 1/2 tap is 125ms, which clears the 140ms cap, so *every* jump
  map at that tempo grew phantom bursts out of its plain 1/2 tapping. A burst
  is a step UP from the map's own pulse, so a run must also come in at most
  `burst_beat_fraction_max` (0.4) of a beat per note — which admits 1/4 (0.25)
  and 1/3 (0.33) and rejects 1/2 (0.5) with 20% headroom for loose snapping.
  The stored tempo is still not trusted on its own; the gate is deliberately
  confined to the only band where it can matter:
    - Runs at or under `burst_always_fast_ms` (110ms/note, a ~136 BPM stream)
      skip it entirely. That is what keeps **doubled** notation working, and
      it is the same argument as the bullet above: a 200 BPM song written as
      400 taps its 1/4 at 75ms, the file calls that a 1/2, and it passes on
      speed alone exactly as it always did. `test_stored_bpm_does_not_affect_
      the_verdict` pins this.
    - `effective_beat_ms()` folds **halved** notation back out before the
      fraction is taken — the direction speed *can't* rescue, because halved
      notation makes an ordinary 1/2 look like a 1/4. It only ever folds
      downward (long beat → shorter), never the reverse: halving a genuine
      250 BPM map would turn its real 1/2 (120ms) into a "1/4 burst", the
      exact bug being fixed. Blast radius is small and bounded by the 140ms
      cap — only maps notated at 107–125 BPM can have a 1/4 admitted at all,
      which is precisely where halved notation lives.
  So the gate only ever decides runs in the 110–140ms band: too slow to be
  self-evidently burst tapping, fast enough to slip under the absolute cap.
  Measured on the user's hand-sorted mislabel set: all 11 "Jumps with bursts"
  verdicts on maps with no bursts were 1/2-snap runs at 120–135ms in 223–250
  BPM maps, and all 11 are fixed. The twelfth (Chug Jug With You, notated 118
  BPM but played at 236) is the halved-notation case and is fixed by the fold.
  Real library check, the user's full osu!stable library (58,811
  difficulties, old vs new end to end): 866 diffs move "Jumps with bursts" →
  "Jumps (no bursts)". On a 7,572-diff lazer sample the gate rejected 14,339
  runs that previously counted — 12,429 moving and 1,910 zero-distance.
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
- **A cut's own distance is checked too, not just its timing.** Rejoining
  across a skipped beat used to check only that the gap was a clean
  whole-number multiple of the run's tempo - never how FAR the cut
  transition actually traveled. Real library check (ai-classification
  branch): 1979 maps had at least one cut exceeding 3x a hit-circle
  diameter, some past 9x. Visually confirmed on Night of Knights [TAG4] (a
  well-known real map) that a 6.8x-diameter cut is two separate stream
  clusters on opposite sides of the playfield joined by a genuine
  full-screen jump, not one continuous stream with a quietly skipped note.
  `cut_max_dist_ratio` (4.0 - roughly 2x the normal "still readable"
  spacing ceiling, since a cut spans two note-hops merged into one) rejects
  the merge when the cut itself is that far.
- **A slider whose computed tail lands after the next object's start time is
  excluded from jump detection entirely**, not measured. `move_time` (the
  cursor's actual travel time) is floored at 1ms to avoid a divide-by-zero,
  but when the *unfloored* value is negative - the previous slider's own
  duration formula puts its tail after the next object already started -
  there is no real "how far did the cursor move in how long" to compute; the
  floor was manufacturing a huge, meaningless velocity out of it instead.
  This is real on real maps, not a hypothetical: an inherited (SV) timing
  point landing on exactly the same timestamp as a slider's own start is
  legal osu! and does happen (confirmed on "Logical Stimulus [Marselo's
  Extra]" - a green line at the slider's own 55471ms recomputes its velocity
  and yields a mathematically correct ~527ms duration that still runs past
  the next circle at 55552ms). The slider-duration formula itself checks out
  against the file's own timing data; it's the transition model built on top
  of it that can't make sense of two objects overlapping in time. Real
  library check: 2,577 diffs had at least one such overlapping transition
  (27,918 total), 18 direct category flips - all moving away from
  "Jumps"/"Jumps with bursts" toward Streams/Bursts/Misc, the direction
  you'd expect from removing manufactured jumps, and disproportionately on
  maps whose names say what they are ("XNOR XNOR XNOR", "Shitpost Set 2",
  "mapping styles").
- **A stacked 1/4 triple IS a burst** — and a previous rejection of every
  zero-distance run has been REVERTED. That rule fired when
  `mean_dist_ratio == 0.0` (every note in the run on identical (x,y)), on the
  strength of one map ("ESSE CARA! [INSANE!]", six four-note stacks) plus a
  large population statistic: 33.5% of every burst run in the library
  (665,952 of 1,986,316) was exactly zero-distance. The statistic was real
  but was not evidence of a bug. A stacked triple is the single most common
  way a 1/4 burst is written in 2014–2017 Insanes; osu!'s stack leniency
  renders it as a small staircase, and it plays and reads as a burst. A third
  of bursts being that shape is what you would expect.
  Thirteen maps the user hand-sorted as "jumps with bursts that got called
  jumps with no bursts" were all this exact pattern — stacked 1/4 triples at
  78–94ms in 160–200 BPM maps — and the blanket rejection is why every single
  one of them missed. What the rule was reaching for (a stack tapped at the
  map's ordinary pulse isn't a burst) is a statement about RHYTHM, not
  spacing, and is now made directly by the rhythm gate above.
  ESSE CARA itself is the proof that the rhythm gate is the right layer, and
  it is worth being precise about, since it is the evidence being overturned.
  The mapset has a single timing point at 503.57ms (119.2 BPM), so the only
  snap in it that can form a run at all is the notated 1/4 at 125.9ms — which
  under halved notation is a 1/2, and the gate rejects it. Checked directly
  against the diffs in the library:

  | diff | with the stack rule | with the rhythm gate |
  |---|---|---|
  | `[INSANE!]` (the one cited) | 0 bursts → Jumps (no bursts) | 0 bursts → Jumps (no bursts) |
  | `[HARD!]` | **4 bursts → Jumps with bursts** | 0 bursts → Jumps (no bursts) |
  | `[SPECIAL!]` | **2 bursts → Jumps with bursts** | 0 bursts → Jumps (no bursts) |
  | `[EXPERT!]` | 1 burst → Jumps (no bursts) | 0 bursts → Jumps (no bursts) |
  | `[EASY!]`/`[NORMAL!]`/`[EXTRA!]`/`[EXTREME!]`/`[TOMA TOMA]`×2 | 0 bursts | 0 bursts |

  So the stack rule did fix the diff it was checked against — but only that
  one. Two others in the same mapset, `[HARD!]` and `[SPECIAL!]`, still
  reported false bursts under it, because those particular runs happened to
  carry a little movement and so dodged a spacing test. That is the tell: the
  rule was aimed at the wrong property. It was simultaneously too broad
  (throwing away ~90k real stacked triples) and too narrow (leaving false
  bursts in its own motivating mapset). The rhythm gate gets all ten diffs
  right, for the reason none of them is burst content: the rhythm, not the
  spacing.
  Real library check (7,572-diff sample): of the zero-distance runs that
  survive the spacing checks, the rhythm gate keeps 90,082 and rejects 1,910
  — and the kept ones pile up at 70–89ms per note, i.e. 1/4 at 160–200 BPM,
  exactly the stacked-triple shape. Only 234 sit above 120ms. The blanket
  rule was discarding ~90k genuine bursts to catch ~2k, and the gate catches
  those 2k on their own merits. The kept runs are 89,862 bursts (3–9 notes)
  against 220 streams (10+), so this is a burst-detection question almost
  entirely — long stacks are a rounding error, not a population to worry
  about.
  The knock-on effect on Streams is a fix, not a side effect: across the full
  stable library 837 diffs move INTO Streams (732 from "Jumps with bursts",
  105 from "Misc") against 111 leaving, and they are maps whose stream runs
  were being chopped up by the stacked segments inside them — Blue Zenith
  [Faics' Extreme], Jinzou Enemy [Extra] (max run 58), Gigantic O.T.N
  [Climax] (117), Everlasting Eternity (65). Calling Blue Zenith "Jumps with
  bursts" was never right.
  If you are tempted to reinstate a spacing floor here, note that the
  wide-fraction and `mean_diam_ratio_max` caps still bound how far apart a
  run may be, and `MAX_SUSTAINED_NOTES_PER_SEC` still catches the
  audio-visualizer stacks that genuinely aren't gameplay.
- **One real burst is enough for "Jumps with bursts"** — `burst_recurrence_min`
  is back to 1 (it was briefly 2). The 176 single-burst-run "Jumps with
  bursts" results that motivated the floor were mostly not bursts at all;
  they were the two detection bugs above (1/2-snap runs admitted by a
  snap-blind speed test, and genuine stacked triples rejected, which also
  suppressed the second and third runs that would have kept honest maps over
  the floor). A recurrence floor was compensating for a noisy detector by
  discarding its output wholesale, true positives included. The user's
  hand-sorted set is explicit on the point: five of their thirteen
  "jumps with bursts" maps contain exactly ONE burst run at ~1–2% coverage.
  The parameter is kept, not deleted, so the stricter reading stays reachable.
  Scale of the change on its own: 154 diffs in a 7,572-diff sample (2.0%) have
  jumps plus exactly one burst run.

Whole-library effect of all three changes together, measured old vs new over
the user's 58,811-difficulty osu!stable library: **9,574 diffs change category
(16.3%)** — a 15.8% flip rate on an independent 7,572-diff lazer sample, so
the two agree. The movement is where the user's mislabelled set says it should
be:

| flip | count |
|---|---|
| Jumps (no bursts) → Jumps with bursts | 3,727 |
| Misc → Bursts | 3,005 |
| Jumps with bursts → Jumps (no bursts) | 866 |
| Jumps with bursts → Streams | 732 |
| Jumps with bursts → Bursts | 424 |
| Bursts → Misc | 295 |

The two big movers are the two false-negative populations (stacked triples
that were being discarded, in jump maps and in otherwise-quiet maps
respectively); the 866 are the false positives the rhythm gate removes.

### The combined "Jumps" collection

`combine_jumps` (GUI checkbox, `--combine-jumps`) writes an EXTRA `Jumps`
collection holding every diff from both jump categories, **in addition to**
them, not instead. A jumps+bursts map therefore appears in two collections -
that is the intent, not a bug. Nothing is reclassified; `report.csv` still
records the specific category, and `category_of()` is untouched. This is
purely an output-grouping option.

It lives in `build_output_collections()`, applied **after** category and star
filtering and **before** ranked handling. That ordering is deliberate:
unchecking a jump category means "I don't want these maps", so they must not
reappear inside the combined collection by the back door; and running before
the ranked step means `ranked_mode="split"` splits `Jumps` into
`Jumps - Ranked`/`Jumps - Unranked` like every other collection, for free.

The `--from-csv` rebuild carries its own copy of this (it works on
`(md5, status, stars)` tuples read back from the CSV rather than `DiffInfo`
objects, so it can't call the same function). If you change one, change both -
a live run and a `--from-csv` rebuild producing byte-identical `collection.db`
files is the regression check, and it does currently hold with the flag on.

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

**Coverage comparisons (steps 1 and 2) use TRANSITIONS as the common basis**,
not notes, when `counted_gaps` is available (always true for a real scan;
only unavailable when rebuilding from a CSV written before this field
existed). `jump_pct` was always transitions-based; stream/burst coverage
used to be notes-based, which isn't the same thing being measured twice - a
run of N notes is N-1 transitions, so `stream_note_total`/`burst_note_total`
are converted (`- stream_run_count`/`- burst_run_count`) before dividing by
`counted_gaps`, matching exactly how `jump_pct` itself was computed. See
"Jump vs. stream coverage" under Known limitations for why this mattered in
practice, not just in principle.

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
| `cut_max_dist_ratio` | 4.0 | largest cut-transition distance (× hit-circle diameter) still treated as a skipped note rather than a real jump between two separate runs |
| `burst_beat_fraction_max` | 0.4 | slowest snap still counted as burst/stream tapping, as a fraction of a beat per note — admits 1/4 (0.25) and 1/3 (0.33), rejects 1/2 (0.5) |
| `burst_always_fast_ms` | 110.0 | runs at or under this ms/note skip the beat-fraction check entirely — fast enough to be burst tapping whatever snap the file calls it |
| `min_notated_bpm` | 125.0 | tempos notated below this are treated as halved-BPM authoring and doubled back up before the beat fraction is taken |

Plus `burst_promote_stream_len` (12, not in `DEFAULT_PARAMS` — it's a
`category_of()` parameter, not a `classify_diff()` one, since it operates on
already-computed run lengths). `INT_PARAMS` in `classify_maps.py` lists which
of the above must parse as `int` rather than `float` when read from CLI/GUI
text: `burst_min`, `burst_max`, `stream_min`, `jump_min_transitions`.

### Sensitivity presets

Nineteen bare numbers named things like `mean_diam_ratio_max` told a GUI user
nothing, so `SENSITIVITY_PRESETS` gives the common case a single click:
**Stricter / Balanced / Looser**. Rules, all pinned by tests:

- **`Balanced` is byte-for-byte `DEFAULT_PARAMS`.** If it ever diverges,
  opening the GUI and pressing Run classifies differently from the CLI's
  defaults for no stated reason.
- **Presets move exactly four knobs** — `max_gap_ms`,
  `burst_beat_fraction_max`, `stream_pct_threshold`, `jump_pct_threshold`.
  Those are the ones that answer "how much gets flagged". Everything else
  describes what a pattern *is*, and moving it would change the definitions
  rather than the sensitivity.
- **Stricter and Looser are trades, not rival claims** about what's correct.
  Measured on the user's 24 hand-sorted maps: Balanced and Looser both get
  24/24, Stricter gets 23/24 (one map falls to Misc under the raised
  `jump_pct_threshold`) — which is what "stricter" is supposed to do, not a
  regression.
- `sensitivity_of(params)` returns the matching preset name or `None`. The GUI
  uses it to show **Custom** the moment a threshold is hand-edited, rather
  than leaving a radio button selected that no longer describes what will run.

The GUI's per-threshold panel is collapsed behind a disclosure and every field
carries a plain-language label plus a one-line explanation of what its number
means. Nothing was removed — but if you add a param to `DEFAULT_PARAMS` you
must also add it to a section in `gui.py`'s `sections` list, or it is silently
uneditable there: the run path reads `self.param_vars`, wouldn't find it, and
would fall back to the default with no error.
`test_every_param_is_editable_in_the_gui` builds the real window and compares
the two sets, so that mistake fails the suite rather than shipping. It skips
itself (rather than failing) when there's no display, so CI stays green
headless — which does mean a headless-only CI won't catch it for you.

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

`is_junk_diff()` also rejects a diff whose note density (object count /
time span) sustains above `MAX_SUSTAINED_NOTES_PER_SEC` (30, same
module-constant treatment as `MIN_OBJECTS_TO_CLASSIFY` - a hard floor, not
a threshold to tune per-user). No human plays faster than that averaged
across a whole diff. Real library check: density decays continuously up to
~27/sec across ~58,400 real diffs, then a genuine gap - nothing between 28
and 30/sec - before a separate cluster of 11 outliers from 30/sec up to
3968/sec, all troll/audio-visualizer content confirmed by title
("u cant even stream 1000bpm u pleb", "unbeatable", "Miracle Tower
(175000bpm)") and star rating (up to 356 - real maps top out around 9-10).
Visually confirmed via `osu_visualizer_preview.py` on "Left Behind [god has
forasken us]" (74,948 notes averaging 487/sec, `star_rating` 356.09): dozens
of notes stacked at each of four fixed points, firing far faster than a
cursor could move between them - an audio visualizer built out of hit
objects, not gameplay. `classify_diff()` enforces this independently too,
same defense-in-depth reasoning as the object-count floor above. 30 sits in
the gap itself, so it doesn't touch the real (if extreme) tail below it -
same "only fix the unambiguous population" discipline the rest of these
decisions try to follow. (Contrast the burst-recurrence and stack-run
entries earlier in this document, which are recorded as REVERSALS - both
generalised from a population statistic to a rule the user's own labels
later contradicted. A large population is not by itself a bug.)

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
jump_pct, burst_note_total, stream_note_total, total_note_count, counted_gaps,
ranked_status, star_rating, online_id, mods, category, path`

`counted_gaps` (new on this branch) is jump_pct's own denominator, carried
through so a `--from-csv` rebuild can use the same transitions-basis
coverage comparison `category_of()` uses live - see "Coverage comparisons"
under `category_of()` decision order above. A CSV written before this column
existed reads as `0` (`row.get(...) or 0`), which is exactly the signal
`category_of()` uses to fall back to the old notes-basis comparison instead
of silently mixing bases.

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
- **Jump vs. stream/burst coverage used to compare two different measures -
  fixed on this branch.** `jump_pct` was always a percentage of
  *transitions*; stream/burst coverage used to be a percentage of *notes*.
  Comparing them directly in `category_of()` was a proportion-vs-proportion
  judgement, not an exact one - and it turned out to matter concretely, not
  just in principle: maps built from alternating burst-cluster-then-jump
  sections (a common style - see the `osu_visualizer`-driven investigation
  on this branch) drive burst-note-count and jump-transition-count to
  near-equal RAW numbers by construction, which made the old notes-vs-
  transitions comparison between them essentially coin-flip noise at exactly
  the point it was supposed to decide something. `counted_gaps` (new
  `DiffInfo` field, same denominator `jump_pct` already used) lets
  `category_of()` convert stream/burst coverage to the same transitions
  basis. Measured effect on a real ~9,300-map library: 428 categories
  changed (4.6%), every single one moving toward "Jumps with bursts" (311
  from Streams, 117 from Bursts, zero the other direction) - a one-directional
  bias correction, not noise, which is the signature you'd expect from fixing
  a systematic measurement mismatch rather than nudging a threshold. Falls
  back to the old notes-vs-notes comparison when `counted_gaps` isn't
  available (CSVs written before this field existed).
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
