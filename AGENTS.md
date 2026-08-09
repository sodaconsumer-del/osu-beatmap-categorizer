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

`classify_diff` is the product. It's tuned heuristics, so treat threshold
changes as claims that need evidence.

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
