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
| `eval_classifier.py` | Scores a `report.csv` against hand-labelled maps, or (`--tags`) against the mappers' own `Tags:` lines |
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

### osu!.db is not the whole library

The stable fast path takes its file list from `osu!.db`, which means anything
the db has not indexed is not merely missing its ranked status — it is
**absent from the scan entirely**. osu! writes a map into its db when it
imports it, at startup; a library that has had maps added since osu! last ran
is simply short by that many maps.

On the user's library that is **2,466 folders and 4,856 difficulties, 9.7% of
Songs/**, and **91% of those folders are named with a bare set id**
(`1000624`, `2589428`) — the shape osu!collector and other external
downloaders leave, as against osu!'s own `<id> Artist - Title`. So this is
not an edge case for anyone who downloads maps outside the client.

`_scan_unindexed_folders()` picks them up after the db pass. It opens **only
the folders the db does not name**, which is what keeps it affordable:

| | cost | difficulties found |
|---|---:|---:|
| one `listdir` of `Songs/` + the 2,466 unknown folders | **0.4s** | **4,856** |
| also checking all 22,868 indexed folders for new files | 168.6s | +995 |

420x the time for 20% more maps, so the indexed folders are deliberately not
reopened, and `test_an_indexed_folder_is_not_reopened` holds that line. The
995 stragglers are difficulties added to a mapset the db already knows; the
plain `scan_folder()` path finds them if anyone needs them.

Two details that are easy to get wrong:

- **Read the db with `want_mode=None`, then filter to standard.** The folder
  names of taiko/ctb/mania entries are still needed, or a mania-only folder
  looks unindexed and every file in it gets opened just to discover it is not
  standard. `read_osu_db()` yields `mode` for exactly this.
- **Recovered difficulties have no ranked status, star rating or online id.**
  Those live in the db, and the db is what has not seen them. They report as
  unranked, which is the honest reading of "not known". They do carry a
  correct `version_hash`, because `parse_osu_file()` computes the same
  content MD5 the db would have supplied — so they can still go into a
  collection.db.

Worth recording alongside this, because it was the original hypothesis and it
turned out to be wrong: **stable's cached ranked status is not stale.** Of
22,843 difficulties it calls `pending`, a cross-check against the lazer realm
(joined on MD5, 57,749 maps in both) confirms 22,821 as graveyard/wip/none —
**22 disagree, 0.1%**. The 45% of the library that reads as unranked really is
unranked. The gap was never wrong statuses; it was missing maps.

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

### Slider geometry comes from osu!, not from an approximation

`_slider_path_points()` builds a slider's real path — bezier, perfect-circle
arc, catmull, linear — and `_point_at_path_length()` walks it. Ported from
`osu.Game/Rulesets/Objects/SliderPath.cs` and osu-framework's
`PathApproximator`.

This used to walk the straight polyline through the control points instead.
That is exact for linear sliders and wrong for everything else, because a
control polygon is longer than the curve it defines, so the walk stops short
of the true tail. Measured over **798,166 single-span sliders** in the user's
library, in hit-circle diameters (`spaced_diam_ratio`, the whole jump-spacing
threshold, is 2.0):

| curve type | n | mean | p90 | p99 | max |
|---|---:|---:|---:|---:|---:|
| linear | 279,977 | 0.001 | 0.002 | 0.012 | 0.036 |
| catmull | 454 | 0.005 | 0.008 | 0.093 | 0.128 |
| perfect arc | 366,877 | 0.044 | 0.113 | 0.325 | 2.921 |
| **bezier** | **142,661** | **0.251** | **0.940** | **2.262** | **98.895** |

Linear agreeing to 0.001 is the control that says the port is right — it is
the one type where both methods must give the same answer. Bezier is the
problem: at p99 the old tail sat a full jump-threshold away from the real one.

Consequences, measured over 7,497 difficulties: **0.455%** of transitions
cross the `spaced_diam_ratio` line, jump transitions fall **0.391%**, and
**0.43%** of verdicts change. The category totals barely move (net ±5 across
six categories) and the flips are near-symmetric — Hybrid→Streams 7 against
Streams→Hybrid 7. That is the signature of removing measurement noise rather
than correcting a bias, which is what this is.

Two details that are easy to get wrong and are both pinned by tests:

- **Repeated control points split the curve.** The "red anchors" in the
  editor. Treating the whole list as one curve rounds a corner off instead of
  turning it.
- **A declared length past the end of the path extends it**, along the final
  segment's own direction — *unless* the last two path points coincide, where
  osu-stable performs no extension and lazer preserves the quirk. Without
  that second rule a degenerate slider extrapolates off the playfield; it
  measured as a 93-diameter outlier the first time this was run.

Bezier sampling is one point per 8px of control polygon, checked against a
faithful port of osu-framework's adaptive subdivision: they agree to 0.0035
diameters, seventy times finer than the error being removed.

#### What this costs, and the three things that pay most of it back

The first version of this cost **3.3x** on parse+classify — 138 → 42
difficulties/sec over 400 real files with the I/O taken out. That was
originally waved through as "about 6% of scan wall time, the scan is
I/O-bound", which was measured but is the wrong reading: the I/O ceiling on
that HDD was ~67 diffs/sec, so a CPU rate of 42 does not hide under it, it
*becomes* the ceiling. On a machine with a faster disk or a warm cache there
is nothing to hide under at all, and the whole 3.3x lands on the user.

Three changes take it to **65 diffs/sec**, a median of 1.53x per difficulty,
with the answers unchanged or better:

- **A perfect circle is not flattened at all.** Arc length is exactly
  `r * theta`, so `_arc_point_at_length()` answers in closed form. This is
  the single biggest win — 44,889 of 115,968 real sliders take it — and it
  is strictly *more* accurate than the polyline it replaces, which lands on
  a chord across the arc rather than on the arc. It declines the two cases
  where the closed form would answer a different question (a degenerate arc,
  and a length past the arc's end, where osu! extends in a straight line).
- **Low-degree beziers use closed forms.** Of 55,516 bezier segments, 61%
  have two control points, 25% three and 10% four. A two-point bezier IS the
  straight line between its endpoints, so sampling it 17 times was pure
  waste; the quadratic and cubic forms are the same arithmetic de Casteljau
  does without rebuilding a list per sample. All three are exact — pinned
  against a generic de Casteljau by test.
- **A work budget for high-degree segments.** de Casteljau is one lerp per
  control-point pair per sample, so cost is quadratic in the degree. 120
  segments of 10+ control points — 0.2% of them, one at 146 points — were
  65% of all bezier time, and that one 146-point art slider took 240ms by
  itself. `_BEZIER_LERP_BUDGET` caps it. Over 115,968 sliders the cap moves
  **one** of them by more than 0.01 diameters, at most 0.059.

A fourth change is not about curves at all: the `[HitObjects]` and
`[TimingPoints]` bodies were pulled out with a non-greedy `re.S` search whose
`.*?` retries its terminator at every character of the largest part of the
file. `_section()` does it with `str.find` and a slice: 0.507s to 0.016s over
the same 400 difficulties, ~11% of parse, agreeing line for line (only
trailing whitespace differs).

Together these land at **109 diffs/sec against 198 for the pre-port code** -
1.8x rather than 3.3x. What is left is inherent: real curve arithmetic the
old code simply did not do. Verified
against the pre-port implementation over 115,968 sliders — 46,847 endpoints
move, none by more than 0.059 diameters, against the 0.251-diameter *mean*
error of the control-polygon walk in the table above.

### Doubled-BPM notation: why a hard tempo threshold is unavoidable

`looks_like_doubled_notation()` refuses to fold unless the notated tempo
exceeds `max_plausible_bpm` (300). A hard BPM threshold looks like exactly
the kind of thing the rest of this file refuses to do, so:

**An honest 320 BPM map and a 160 BPM map notated at 320 contain the same
notes.** Doubling the written tempo renames every layer - the real 1/2
backbone becomes a written 1/1, the real 1/4 becomes a written 1/2 - but not
one timestamp moves. There is no content signal to find, because the content
is identical. The only difference between the two files is the number in the
timing point, so that number has to decide.

The content conditions still carry most of the weight, in the other
direction. Surveyed over 1,501 real difficulties, 117 notated above 240 BPM;
of the five above 300, four are rejected on content alone:

| map | notated | 1/1 | 1/2 | 1/4 | folded? |
|---|---:|---:|---:|---:|---|
| KOODA [Dicky stiffy uh] | 358 | 24% | 0% | 0% | no - no 1/2 layer |
| Acid Rain [Aspire] | 340 | 21% | 32% | 23% | no - real 1/4 layer |
| IMMORTAL [Eva Phonk] | 340 | 67% | 6% | 0% | no - 1/2 too sparse |
| Super-Fast-Internet-san [vrooom] | 320 | 33% | 50% | 6% | yes |
| Power Up [300 BPM MAD STREAM] | 300 | 17% | 0% | 72% | no - 72% real 1/4 |

Where the bound earns its place is the 240-300 band, which content cannot
defend. "Setsuna Trip" is a real 290 BPM map whose notated 1/2 is 50% of its
gaps at 103ms, with no 1/4 at all - from the notes alone that is
indistinguishable from a doubled 145. Fold it and half the map's ordinary
tapping becomes bursts.

#### Notation is resolved over the mapset

Notation used to be decided per DIFFICULTY, which is wrong in a specific way:
a quiet difficulty carries no 1/4 at all, and "no 1/4" is exactly the
signature the doubled test looks for. So the easy diffs of a fast honest set
could read as doubled while their harder siblings, carrying a real 1/4 layer
at the same tempo, plainly did not - and a set is written against ONE set of
timing points, so they cannot actually disagree.

`notation_evidence()` gathers the raw counts each test is decided from, and
`resolve_set_notation()` pools them over a set and returns one verdict. The
busy difficulties speak for the quiet ones. `classify_diff(notation=...)`
takes that verdict; `notation=None` keeps the old per-difficulty behaviour,
which is what every direct caller and every test still uses, and is right
when there are no siblings to ask.

The pooling happens in `run_pipeline`, which buffers difficulties by
`BeatmapSetID` and flushes a set when the next one starts (or at
`SET_BUFFER_MAX`, or at end of scan). All three scan paths hand difficulties
over folder by folder - set by set on the realm path - so a set arrives
together and the buffer holds one at a time. If they ever arrive interleaved
each flushes alone, which is exactly the per-difficulty behaviour this
replaces: it degrades rather than breaks. Peak memory is now one mapset's
notes rather than one difficulty's, which is the reason for the cap.

The set id comes from the .osu's own `BeatmapSetID`, not from the folder,
because the lazer blob store has no folders to group by. `-1` (unsubmitted)
is treated as "no set": every unsubmitted map in a library carries it, so
using it as an identity would pool unrelated maps' evidence together.

**How much this moves.** Over 700 real mapsets / 2,004 difficulties, the
difficulties of **4.14%** of sets disagreed with each other about their own
set's notation, and pooling changes the verdict for **2.45%** of
difficulties. Not common, as expected — but note what it mostly fixes:
almost every disagreement is about HALVED notation, not doubled. That is the
opposite of what motivated the change. It makes sense in hindsight: the
halved test keys on the size of the 1/4 layer, which is exactly what varies
between a set's easy and hard difficulties, so a set commonly splits 8-3 or
2-1 on a question that has one answer by construction.

Typical shape, from that survey:

| set | difficulties | decided alone | pooled |
|---|---:|---|---|
| 1903462 H-Kray - Tam Long Son | 11 | 8 halved, 3 honest | halved (3 changed) |
| 737103 JUNNA - Here | 4 | 1 halved, 3 honest | halved (3 changed) |
| 993854 Itou Miku et al. | 5 | 2 halved, 3 honest | honest (2 changed) |

### Things checked against the osu! sources and deliberately NOT done

Each of these was implemented far enough to measure, then dropped. Don't
re-derive them from scratch.

- **Stacking.** osu! offsets stacked objects by `StackHeight * Scale * -6.4`
  in both axes and lazer's own difficulty calculator measures every distance
  between `StackedPosition`s, so this looked obviously right. Ported
  `OsuBeatmapProcessor.applyStacking` in full and measured it: **2 extra
  verdict changes in 3,675 difficulties** (0.05%) on top of the slider fix.
  The offset is ~5px per stack level against a ~73px diameter, far too small
  to move a 2.0-diameter threshold. Not worth the code.
- **Declared break periods.** Every `.osu` names its breaks outright in
  `[Events]` as `2,start,end`, and osu! honours them past
  `BreakPeriod.MIN_BREAK_DURATION` (650ms), where we infer breaks from
  `jump_gap_cap_ms` (1000ms). Since `eligible` is the denominator under every
  coverage figure, a systematic error here would move everything at once.
  Measured over 2,624,987 transitions: **44 of them** sit inside a declared
  break that we count as gameplay — 0.002%. Exactly one difficulty in 3,688
  saw its denominator move by more than 1%. The cap already does this job.

For the record, one thing that *was* confirmed rather than changed:
`DiffUtils.MillisecondsToBPM` is `60000 / (ms * 4)`, i.e. `15000 / ms`, which
is the stream-BPM formula quoted throughout this file and in the CLI help.

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
- **Burst** — a run of 3–9 notes that is genuinely FAST: at most
  `burst_max_gap_ms` (105ms) per note. Being a fast *snap* is not the same
  thing and is not sufficient — a 125 BPM map's honest 1/4 is 120ms, a real
  1/4 and not a burst. Nor is spacing the discriminator: the disputed
  clusters look burst-shaped and pass every spacing check.
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
- **Hybrid** — a map with stream passages AND jump passages, in comparable
  amounts and in *different parts* of the map. Its own category, because
  coverage cannot express it: "jumps for a minute then streams for a minute"
  and "both mixed evenly throughout" average to identical numbers. See
  "Sections" below.
- **Section** — a fixed 2000ms slice of the map. A pattern *owns* a section
  when it holds `section_dominance` of that section's transitions. Sections
  are what make structure visible where coverage only sees totals.
- **Coverage** — what fraction of the *map* a pattern actually occupies.
  `category_of()` measures all three patterns in transitions over
  `counted_gaps`, so they partition one denominator and can be ranked
  directly; the `has_streams` presence gate is the one place still measured
  in notes over notes, because `stream_pct_threshold` was calibrated there. Classification cares about coverage, not mere presence — a
  single 10-note stream buried in an otherwise pure jump map does not make
  the map a "stream map"; see "Patterns must out-cover each other" below.
- **NM (no mod)** — the only thing classification runs against. Mod support
  was removed rather than left half-working; see "NM only" below for why DT
  and a natively-fast map cannot both be judged consistently. `rate` still
  threads through `classify_diff()` at a fixed 1.0, so restoring it is small.

`classify_diff` itself is tuned heuristics, so treat threshold changes as
claims that need evidence.

Decisions that look odd but aren't:

- **Speed is absolute milliseconds, not a ratio to the stored BPM.** Plenty of
  maps are authored at deliberately doubled tempo. Stream BPM is `15000 / ms`.
  An earlier ratio-based test was admitting roughly half its transitions at
  1/2 snap.
- **The snap test now applies to EVERY run, and both notations are folded
  out from the notes.** It used to be skipped for anything under
  `burst_always_fast_ms` (110ms), a blunt way to protect doubled notation.
  That exempted every map above ~273 BPM, whose ordinary 1/2 jump pulse is
  under 110ms - "Flowering Night Fever [Ekoro's Fever]" (290 BPM) reported
  **127 bursts**, most of them its 103ms 1/2. `looks_like_doubled_notation()`
  replaces the blanket skip, so the gate can run on everything.
  **Its decisive condition is the notated tempo, not the notes, and that is
  the point.** Notation belongs to the MAPSET, so every difficulty must agree
  - and content cannot deliver that. Across Flowering Night Fever's eight
  difficulties the quiet ones carry no 1/4 at all, so they look exactly like
  doubled notation while their busy siblings do not: three of eight flipped,
  and their burst counts inflated (Insane 71, pishi's Lunatic 114). With the
  tempo bound all eight agree. The halved detector needs no such bound
  because there the content genuinely decides - see its docstring.
- **...but absolute ms alone can't tell a burst from a fast map's own pulse,
  so there is now a second, narrowly-scoped rhythm gate on top.** At 240 BPM
  an ordinary 1/2 tap is 125ms, which clears the 140ms cap, so *every* jump
  map at that tempo grew phantom bursts out of its plain 1/2 tapping. A burst
  is a step UP from the map's own pulse, so a run must also come in at most
  `burst_beat_fraction_max` (0.4) of a beat per note — which admits 1/4 (0.25)
  and 1/3 (0.33) and rejects 1/2 (0.5) with 20% headroom for loose snapping.
  The stored tempo is still not trusted on its own. Rather than exempt part
  of the speed range from the gate, both notations are folded out first, so
  the beat the gate measures against is the one a player actually feels:
    - `looks_like_halved_notation()` halves the beat for **halved** notation
      (a 260 BPM song written as 130), which otherwise makes an ordinary 1/2
      look like a 1/4 and sail through. It reads the notes, not the stored
      BPM — see its own section below.
    - `looks_like_doubled_notation()` doubles it for **doubled** notation (a
      180 BPM song written as 360), where a genuine 1/4 burst is written as a
      1/2 and would otherwise be thrown away. Its decisive condition comes
      from the timing points, so every difficulty in a mapset agrees.
  The gate then applies to **every** run. An earlier design exempted anything
  under 110ms (`burst_always_fast_ms`) to protect doubled notation; that
  parameter is gone — see the bullet above for why.

  **Read the next bullet before relying on any of this for bursts.** The gate
  was added to fix 11 maps and did, but a later and simpler rule
  (`burst_max_gap_ms`) turned out to fix those same 11 *and* four the gate got
  wrong — and it makes the gate inert for bursts. What is left of the gate's
  job is long runs at 1/2 snap that are fast in milliseconds, i.e. streams,
  which no label in this repo currently covers. It is kept because that job is real and
  removing it would change stream behaviour on no evidence, not because it is
  still load-bearing for bursts. If you are here to simplify, this is the
  first thing to consider deleting — measure the stream side first.
- **A burst must be genuinely FAST, not merely a fast snap.**
  `burst_max_gap_ms` (105ms/note, ≈143 BPM stream) is the rule that actually
  separates the labelled data. A slow song's honest 1/4 is a real 1/4 and
  still not a burst: 125 BPM gives 120ms, 130 BPM gives 115ms, both well
  inside the old 140ms cap and both passing the rhythm gate at exactly 0.250
  of a beat. Reported on "Jump & Stream Practice [Arastelia's Dizzy]" and the
  whole MONTAGEM BATCHI set.
  Across all 28 hand-labelled difficulties the separation is clean, and it is
  on **speed** — not snap, not spacing:

  | | fastest tight run per map |
  |---|---|
  | has bursts (13) | 75, 76, 78, 79, 83, 83, 84, 84, 90, 90, 93, 94 ms |
  | no bursts (15) | 115, 116, 116, 120, 120, 120, 120, 122, 125, 125, 125, 127, 128, 129, 134 ms |

  Any cap in 95–110 scores 28/28; 105 sits in the middle of the gap. The
  disputed clusters are burst-*shaped* (which is exactly why every spacing
  check passes them) and simply too slow, so spacing was never going to
  separate them.
  **Deliberately scoped to bursts rather than lowering `max_gap_ms`**, even
  though a ~105ms global cap also scores 28/28 on this data. `max_gap_ms`
  builds the runs streams are found in too, so lowering it would silently
  stop calling ~130 BPM stream maps streams — and all 28 labels are about
  bursts. Not changing stream behaviour on a guess.
  Earlier measurements, from when the rhythm gate was carrying this fix:
  866 diffs moved "Jumps with bursts" → "Jumps (no bursts)" across the user's
  full 58,811-difficulty stable library; a 7,572-diff lazer sample had the
  gate rejecting 14,339 runs (12,429 moving, 1,910 zero-distance). Those
  numbers predate `burst_max_gap_ms`, so treat them as the order of magnitude,
  not the current figure.
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
  transition actually traveled. Real library check: 1979 maps had at
  least one cut exceeding 3x a hit-circle
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
` Jumps-ranked`/`Jumps-unranked` like every other collection, for free.

The `--from-csv` rebuild carries its own copy of this (it works on
`(md5, status, stars)` tuples read back from the CSV rather than `DiffInfo`
objects, so it can't call the same function). If you change one, change both -
a live run and a `--from-csv` rebuild producing byte-identical `collection.db`
files is the regression check, and it does currently hold with the flag on.

`category_of()` is the single source of truth for category rules. Both the
live path and the `--from-csv` rebuild go through it — they used to carry
separate copies that could drift. `--from-csv` reproducing a byte-identical
`collection.db` is a good regression check.

### One transition, one label

Every gameplay transition (i.e. every one in `counted_gaps`) is described by
at most ONE of burst, stream or jump. `classify_diff()` classifies runs
first; whatever the runs don't claim is then offered to the jump test.
`burst_transitions` / `stream_transitions` / `jump_transitions` are the
result, and because they partition one denominator they can be compared
directly.

This was not always true, and the bug was not theoretical. The run pass and
the jump pass used to run independently over all transitions, so a note
inside a perfectly good stream could also be counted as a jump - and then
stream coverage and jump coverage, which exist to be weighed against each
other, were partly measuring the same notes. Measured over 870 real diffs:
**41% had at least one double-counted transition**, inflating `jump_pct` by
up to 9.3 percentage points (Ember Lights: 43.0% → 33.7%). It biased every
streams-vs-jumps comparison toward jumps, in proportion to how much stream
content the map had.

A run REJECTED for being too jump-spaced is not discarded either: its
transitions fall through to the jump test and the wide ones are counted as
the jumps they are. Evidence changes hands instead of evaporating.

The note-count fields (`burst_note_total` etc.) are still written, and
`category_of()` still derives transition counts from them when given raw
arguments or an old CSV. That derivation is arithmetically exact (a run of N
notes is N-1 transitions) - what it cannot express is two passes claiming the
same transition, which is the thing the partition fixes.

### Sections, and the Hybrid category

Coverage is a total: it says how much of a map is streams and how much is
jumps. It cannot say *where*, and that is a real gap - a map that is jumps for
a minute and then streams for a minute averages to exactly the same numbers as
one that mixes both evenly all the way through, and those are completely
different maps to play. The contest below would hand each of them to whichever
pattern edged ahead, losing the thing that actually characterises the first.

`section_pattern_counts()` slices the map into `section_ms` chunks and counts
how many sections each pattern *owns* (holds `section_dominance` of).

**On `section_ms = 2000`:** osu!'s own `StrainSkill` uses 400ms, but that is
for strain peaks, and at 200 BPM it is barely one beat. Printing real maps'
section timelines at 400ms gives fragmented noise; at 2000ms (about two bars
at 200 BPM) the structure reads straight off the line. FREEDOM DiVE [ENDLESS
DiMENSiONS] shows its jump passages and its long stream passage exactly where
the map has them. This was picked by looking, not by taste.

**Hybrid needs two things**, and the second is not optional:

1. Streams own ≥ `hybrid_section_min` of the sections, and jumps own ≥ that too.
2. They are BALANCED - the smaller owns ≥ `hybrid_balance_min` (0.5) of what
   the larger does. Without this, a map with 61% jump sections against 19%
   stream sections is called a "mix" at any flat threshold, when it is plainly
   a jump map that has a stream section in it. On a 7,572-diff sample the
   balance rule alone removes 240 such maps (765 → 525).

Both patterns must also clear their own coverage bars (`has_streams` /
`has_jumps`), so Hybrid never promotes a map on evidence too thin to count on
its own. `hybrid_section_min` is one of the five knobs the sensitivity presets
move: Stricter 0.25, Balanced 0.15, Looser 0.10.

Measured over that sample: **525 diffs (6.9%) become Hybrid** - 346 from
Streams, 174 from "Jumps with bursts", 5 from "Jumps (no bursts)".

### `category_of()` decision order

Hybrid is checked first, because it is the one verdict the coverage contest
structurally cannot reach - it is about *where* patterns sit, not how much
there is of them. Everything else is one contest:

0. `has_hybrid` (and streams and jumps both present) → **Hybrid**
1. Rank `has_streams`/`has_bursts`/`has_jumps` by coverage; nothing present →
   **Misc**
2. Streams win → **Streams**
3. Bursts win → `burst_or_stream()` (see 5)
4. Jumps win → **Jumps with bursts** if bursts are also present, else
   **Jumps (no bursts)**
5. `burst_or_stream()`: a run ≥ `burst_promote_stream_len` (12) exists →
   **Streams**, else **Bursts**

Ties go stream > burst > jump, which is exactly what the old ordered cascade
did (it gave a stream-vs-jump tie to streams with `>=`, and a burst-vs-jump
tie to bursts by making jumps need a strict `>`), so ranking changes nothing
about ties.

**This replaced an ordered chain of pairwise comparisons**, which had two
faults. Streams and bursts were never compared at all - a map where bursts
out-covered streams, and streams out-covered jumps, was called a stream map
on the strength of the first comparison it happened to reach. And order stood
in for strength: whichever pattern got compared first had an advantage
unrelated to how much of the map it occupied.

`has_streams` requires ≥15% coverage (`stream_pct_threshold`) — it is not raw
presence. A map can contain a 10-note stream run and still have
`has_streams == False`; it then only shows up via step 5's
`burst_promote_stream_len` check, which reads `max_stream_len` directly
rather than coverage. Deliberate — see "Except: a burst map that streams
once" above. Note this leaves the three presence bars asymmetric
(`has_bursts` is pure presence, `has_jumps` needs 15% *and* 40 transitions),
which is a known wart, not a design.

Measured effect of the partition and the contest together, over a 7,572-diff
sample: **50 diffs change category (0.66%)** — 42 "Jumps with bursts" →
"Streams" and 7 → "Bursts" (both from jump coverage no longer being inflated
by double-counted run transitions), and 1 "Streams" → "Bursts" (the
three-way contest, on a map where bursts covered 41% against streams' 25%).
The 24-map reference set and the 28 burst labels are unchanged.

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
| `burst_max_gap_ms` | 105.0 | slowest ms/note a run may tap and still be a **burst** (≈143 BPM stream). Streams are deliberately unaffected |
| `section_ms` | 2000.0 | length of one section, in ms — roughly two bars at 200 BPM |
| `section_dominance` | 0.5 | share of a section one pattern must hold to own it |
| `section_min_transitions` | 4 | fewest notes for a section to be counted at all |
| `hybrid_section_min` | 0.15 | share of sections streams must own AND jumps must own for **Hybrid** |
| `hybrid_balance_min` | 0.5 | how balanced that mix must be — smaller side / larger side |
| `halved_quarter_share_min` | 0.15 | share of note gaps on the notated 1/4 before that layer counts as the map's backbone rather than accents — one of two content signals for halved-BPM authoring. No BPM threshold is involved |

Plus `burst_promote_stream_len` (12, not in `DEFAULT_PARAMS` — it's a
`category_of()` parameter, not a `classify_diff()` one, since it operates on
already-computed run lengths). `INT_PARAMS` in `classify_maps.py` lists which
of the above must parse as `int` rather than `float` when read from CLI/GUI
text: `burst_min`, `burst_max`, `stream_min`, `jump_min_transitions`.

### Detecting halved-BPM notation

A mapper may notate a song at half its real tempo - 130 for a 260 BPM song -
and osu! plays it identically either way, so nothing in the file declares
which was meant. It matters because the snap test asks "is this faster than
1/2?", and under halved notation ordinary 1/2 tapping is written as 1/4.

`looks_like_halved_notation()` decides this **from the notes**. An earlier
version used a threshold on the stored BPM (`min_notated_bpm = 125`, fold
anything slower). That was wrong on both counts:

- **Arbitrary.** MONTAGEM BATCHI is notated 130 and *is* halved (it plays as a
  ~260 BPM jump map) - five BPM the wrong side of the line, so it was missed.
  Nudging the threshold up to catch it would have started folding correctly
  notated maps instead.
- **It ignored the evidence sitting right there.** The note timings say a
  great deal about which reading is right.

Two conditions, both required:

1. **The notated 1/4 carries a workhorse share of the map**
   (`halved_quarter_share_min`, 0.15). In a correctly-notated map 1/4 is
   accent content - bursts and streams - and stays a minority. Under halved
   notation the map's real 1/2 backbone lands there and dominates.
2. **Most of that 1/4 layer is slower than `burst_max_gap_ms`** - too slow to
   be genuine burst or stream content. This is what protects real stream
   maps: a 180 BPM stream map is also half 1/4 notes, but at 83ms each that
   is obviously streaming, whereas a 130 BPM map's 1/4 is 115ms, which nobody
   streams - so it is far more likely to be a 260 BPM map's 1/2.

Condition 2 is deliberately *derived* from `burst_max_gap_ms` rather than
being another invented number: the same measured "too slow to be burst
content" line does both jobs.

Measured on 35 difficulties with known notation - Chug Jug (118) and the
11-diff MONTAGEM BATCHI set (130), both halved, against 23 correctly-notated
maps from 160 to 250 BPM - **33/35, with no real misses**. The two are
BATCHI's Easy and Normal, which contain no 1/4 content at all: there is
nothing to detect, and equally nothing for the fold to affect, so they cost
nothing.

Two caveats worth knowing:

- Notation is a property of the **mapset** (its timing points), but each diff
  is judged alone, because `classify_diff()` never sees its siblings. A quiet
  diff in a halved set may not read as halved - harmless, per above.
- The residual risk is a genuinely slow *stream* map: something notated around
  110-140 BPM whose 1/4 is both a large share and slower than 105ms. Condition
  2 cannot tell that from halved notation, because at that point the two are
  the same thing physically. No map in the labelled set is like this.

If you are tempted to replace this with a tempo-detection library: the 2x
ambiguity is the classic octave-error problem, and halving maps 1/2 onto 1/1,
both of which are perfectly normal primary layers. The signal that works here
is not "what is the tempo" but "is the 1/4 layer doing a job only a 1/2 layer
would do".

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
The worst of them, "Left Behind [god has forasken us]" (74,948 notes
averaging 487/sec, `star_rating` 356.09), stacks dozens of notes on each of
four fixed points and fires them far faster than a cursor could move between
them - an audio visualizer built out of hit objects, not gameplay. `classify_diff()` enforces this independently too,
same defense-in-depth reasoning as the object-count floor above. 30 sits in
the gap itself, so it doesn't touch the real (if extreme) tail below it -
same "only fix the unambiguous population" discipline the rest of these
decisions try to follow. (Contrast the burst-recurrence and stack-run
entries earlier in this document, which are recorded as REVERSALS - both
generalised from a population statistic to a rule the user's own labels
later contradicted. A large population is not by itself a bug.)

### NM only

Classification is NM-only. `--mods`, the GUI checkboxes and the `mods`
parameter are gone from the classification path; `report.csv` keeps its
`mods` column, always "NM", so the file shape and `--from-csv` are unchanged.

Removed because mods and native tempo gave contradictory answers to the same
physical question. `test_dt_turns_half_tapping_into_a_stream` asserted that a
200 BPM map tapping 1/2 at 150ms becomes a stream under DT, because DT
compresses it to 100ms. But DT-on-200BPM and native-290BPM are the same thing
at the keyboard - both are ~100ms at 1/2 snap - so any rule that promotes the
first must promote the second, and promoting the second is exactly the false
positive the snap test was added to kill. There is no consistent rule that
does both.
Dropping mods removes the contradiction; the snap test then applies cleanly.

`mod_adjustments()` is deliberately KEPT - its verification against ppy/osu is
worth preserving, `test_mod_adjustments_match_osu` still pins it, and `rate`
still threads through `classify_diff()` at a fixed 1.0. Restoring mods is
small. It has no caller in the shipped path; that is expected, not dead code
to tidy away.

### Mod math (no current caller - kept for restoring mods)

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
has_hybrid, active_sections, stream_sections, jump_sections, burst_runs,
stream_runs, cutstream_runs, max_burst_len, max_stream_len, jump_pct,
burst_note_total, stream_note_total, total_note_count, counted_gaps,
burst_transitions, stream_transitions, jump_transitions, ranked_status,
star_rating, online_id, mods, category, tags, path`

`burst_transitions` / `stream_transitions` / `jump_transitions` are the
partition described under "One transition, one label" — written so a
`--from-csv` rebuild gets the exact counts rather than re-deriving them.
Absent in older CSVs, where `category_of()` falls back to the note-total
derivation automatically.

`counted_gaps` is jump_pct's own denominator, carried
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

### Mapper tags are a check, never an input

Every `.osu` carries a `Tags:` line the mapper wrote. `DiffInfo.tags` holds
it, `report.csv` has a `tags` column, and `eval_classifier.py --tags` holds
the classifier's verdicts up against it.

**Do not feed tags into `classify_diff()`, and do not tune thresholds to the
agreement rate.** `test_tags_never_reach_the_classifier` enforces the first
half. The reason for the second is that mappers tag for searchability, not
for accuracy: a set carries the whole spread of words its guest mappers used,
tags get copied across diffs of a set that do not play alike (the Easy of a
stream mapset is tagged `stream` too), and plenty are tagged for a tournament
or an artist. Fitting to this fits tag-writing convention. What it is for is
the disagreements.

Why it is worth having anyway: it needs no network and no hand-labelling, 11%
of a real library names a skillset, and it is **the only outside evidence
that exists for Streams and Hybrid** — the hand-labelled set in
`D:\!Claude reference folder\Mislabel` is entirely about jumps and bursts.

Note the name collision with the thing `eval_classifier.py`'s header says was
tried and dropped. Those are osu!'s **server-side community usertags**
(`skillset/streams`, and so on), which needed the API and covered almost
nothing outside the top few hundred maps. This is the mapper's own metadata
field, shipped inside every copy of the map. Different source, different
coverage.

Measured over 459 tagged difficulties in one shard:

| word | n | Streams | Hybrid | Bursts | Jumps+b | Jumps | Misc |
|---|---:|---:|---:|---:|---:|---:|---:|
| `stream` | 131 | 69% | 15% | 2% | 8% | 3% | 3% |
| `deathstream` | 57 | 65% | 30% | 0% | 5% | 0% | 0% |
| `stamina` | 39 | 79% | 10% | 3% | 3% | 5% | 0% |
| `jump` | 89 | 6% | 0% | 1% | 47% | 40% | 6% |
| `jumps` | 58 | 0% | 2% | 0% | 41% | 53% | 3% |
| `farm` | 19 | 21% | 0% | 0% | 21% | 53% | 5% |

Only words with a defensible mapping onto `CATEGORIES` are scored
(`TAG_FAMILIES`). `tech`, `alt`, `aim`, `speed`, `flow` and `burst` are shown
in the breakdown and left out of the agreement figure, because this tool has
no category for them and inventing a mapping would be inventing the answer.

**Reading the output.** Disagreements are split into *near the threshold* and
*not close*, because they need opposite fixes. A map tagged `stream` that
came out Bursts on 14.2% coverage against a 15% floor is a threshold
question; the same map with no stream run at all is a detection question.
`--write-disagreements out.csv` dumps them with paths, so the maps behind a
disagreement can be opened and looked at.

**The Hybrid check.** Maps tagged BOTH `stream` and `jump` are what Hybrid
claims to be for, so Hybrid should be over-represented among them. This is
the only independent test of that category anywhere in the repo. On the first
run it was *under*-represented — 1 of 19 against a 10% base rate — on a
sample far too small to conclude from, but small enough that widening it is
the obvious next step before trusting Hybrid.

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
  since fixed.** `jump_pct` was always a percentage of
  *transitions*; stream/burst coverage used to be a percentage of *notes*.
  Comparing them directly in `category_of()` was a proportion-vs-proportion
  judgement, not an exact one - and it turned out to matter concretely, not
  just in principle: maps built from alternating burst-cluster-then-jump
  sections (a common style) drive burst-note-count and jump-transition-count to
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
