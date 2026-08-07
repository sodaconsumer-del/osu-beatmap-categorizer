# osu-beatmap-categorizer

Sorts your entire osu! beatmap library into collections by what the maps
actually *play like* — streams, bursts, or jumps — by reading the real note
data instead of relying on tags, star rating, or guesswork. Point it at your
library, get back a ready-to-use `collection.db`.

> **This project was built with AI assistance.** The code (Python, C#, and
> this README) was written collaboratively with Claude (Anthropic's AI
> assistant), with a human (the repo owner) directing the design, testing
> every fix against real beatmaps, and catching mistakes along the way. If
> you hit a bug, please open an issue — that's exactly how several of the
> classification bugs already fixed in this project got caught.

## What it actually does

You give it a folder of beatmaps. It reads every difficulty's note timing
and positions, works out whether each one is dominated by streams, bursts,
or jumps, and writes the result into collections you can drop straight into
osu!. No manual tagging, no relying on other players having already tagged
the map.

- Works with **osu!stable** and **osu!lazer**
- No export step needed for either client
- Handles edge cases that trip up simpler tools: doubled-BPM maps (where a
  stream is authored at half the displayed BPM for finer snap precision),
  DT-friendly maps, wide-angle jump patterns that happen to be fast-snapped,
  and more — see [How it classifies maps](#how-it-classifies-maps) below
- Pick exactly which categories you want (e.g. only Jumps, if you're an aim
  player who doesn't care about anything else)
- Optionally split ranked and unranked maps into separate collections
  (osu!lazer only, see [Ranked status](#ranked-status))

## Download

Grab the app for your OS from the [Releases](../../releases) page — no
Python or programming knowledge required.

## Quick start

1. Open the app.
2. Point it at your beatmap folder:
   - **osu!stable**: your `Songs` folder (`%localappdata%\osu!\Songs` on Windows).
   - **osu!lazer**: your osu! data folder (`%appdata%\osu` on Windows) — the
     one containing `client.realm` and a `files` folder. There's a one-click
     button for this if it's in the default location.
3. Choose an export folder — this is where `collection.db` and a `report.csv`
   will be saved.
4. Pick which categories you want included, and whether to split ranked
   from unranked maps (lazer only).
5. Hit **Run classification**.
6. Open `report.csv` and spot-check a few maps you know well before trusting
   the result.
7. **Back up your existing `collection.db`** first, then drop the new one
   in. On osu!lazer, since collections live inside `client.realm` rather
   than a separate file, import via
   [CollectionManager](https://github.com/Piotrekol/CollectionManager)
   instead of copying the file directly.

## Categories

Every difficulty lands in **exactly one** category, based on whatever
pattern *dominates* the map — not just whether it contains a little bit of
everything. A map that's mostly streams with a short jump section is still
a stream map; a smaller secondary pattern doesn't change what the map
fundamentally is.

Priority order: **Streams > Bursts > Jumps > Misc**

| Category | What it means |
|---|---|
| **Streams** | Contains a genuine stream (10+ notes, fast and tightly spaced). Cutstreams (a stream with a few wider-spaced notes mixed in) still count as streams — they're not a separate category. |
| **Bursts** | Contains short burst(s) of 3-9 fast, tightly-spaced notes, but no full streams. |
| **Jumps** | Jump-heavy — wide spacing covered quickly — with no bursts or streams at all. A "pure" jump map. |
| **Misc** | None of the above (normal-density play, low-intensity diffs, etc). |

The exact thresholds (how many notes make a burst vs. stream, how tight the
spacing needs to be, etc.) default close to
[osu!'s own official beatmap tag definitions](https://osu.ppy.sh/wiki/en/Beatmap/Beatmap_tag),
and are fully adjustable if you want to tune them yourself.

### Choosing categories

Only want jump maps? Uncheck everything except Jumps before running, and
the output `collection.db` will only contain that one collection — nothing
else gets written, even though the whole library still gets scanned to
figure out what's what.

### Ranked status

osu!lazer stores each beatmap's ranked status inside `client.realm`, so
when the app reads your library that way, it knows whether each map is
ranked or not. You can choose to:

- keep ranked and unranked maps together (default),
- only include ranked maps,
- only include unranked maps, or
- split every category in two, e.g. `Streams - Ranked` / `Streams - Unranked`.

This only works when scanning via osu!lazer directly — a Songs folder scan
(stable) or a `.osz`/exported-folder scan has no way to know a map's ranked
status, since that isn't stored in the beatmap file itself.

## How it classifies maps

This is the part that took the most iteration, so it's worth explaining
honestly, including what didn't work at first:

- **Timing is BPM-relative, not absolute.** A "fast" transition is judged
  by how it compares to the map's own beat length, not a fixed millisecond
  number — so DT, HT, and different BPMs don't need special-casing.
- **Doubled BPM is accounted for.** Some mappers author extreme stream maps
  with the file's stored BPM deliberately doubled from the song's true
  tempo, for finer snap precision in the editor. A real 1/4-snap stream
  note in that case lands at exactly half the *stored* beat length — this
  tool's timing threshold is wide enough to catch that, while still
  ignoring genuinely slow, unrelated taps.
- **Spacing has three tiers, not one.** Overlapping/near-overlapping notes
  ("tight"), non-overlapping but still deliberately readable spacing
  ("spaced" — common in high-BPM finger-control patterns), and genuinely
  wide spacing ("jump"). Only the last one counts against a run being a
  real stream or burst.
- **Average spacing matters, not just the worst-case fraction.** A wide-angle
  jump pattern can, by geometric coincidence, have some individual
  transitions that land under the "wide" cutoff (e.g. a star or zigzag
  shape that occasionally swings back close to a previous note). A real
  stream — even a spaced one — still averages close to circle-diameter
  spacing across the whole run; a run whose *average* spacing is too wide
  gets correctly excluded even if no single transition alone would trigger it.
- **Jump density is velocity-based, not distance-based.** Distance traveled
  is normalized by both circle size and time available, so low-density
  Easy/Normal diffs aren't falsely flagged as "jump maps" just because
  their notes are naturally spread out over more time.

None of these were obvious up front — most were found by testing against
real, specific maps that got misclassified and tracing exactly why.

## Running from source / advanced usage

No dependencies beyond the Python standard library for the core tool.

```
python gui.py
```

or from the command line:

```
python classify_maps.py "C:/path/to/Songs" --csv report.csv --output collection.db
```

Run `python classify_maps.py --help` for every tunable threshold, category
filter (`--categories`), and ranked-status option (`--ranked-mode`).

If you already have a `report.csv` from a previous run and just want to
regenerate `collection.db` (e.g. after a crash, or to try different
category/ranked filters) without rescanning your whole library:

```
python classify_maps.py --from-csv report.csv --output collection.db
```

### Building the executable yourself

```
pip install pyinstaller
pyinstaller --onefile --windowed --name osu-beatmap-categorizer gui.py
```

GitHub Actions (`.github/workflows/build.yml`) does this automatically for
Windows/Mac/Linux whenever a `v*` tag is pushed, and also builds the
optional `realm-reader` helper (see below).

### The realm-reader fast path (osu!lazer)

For osu!lazer, this tool can read `client.realm` directly — the same
general approach [CollectionManager](https://github.com/Piotrekol/CollectionManager)
uses — via a small companion C# program (`realm-reader/`) using the
official [Realm .NET SDK](https://github.com/realm/realm-dotnet). This
resolves every beatmap's file location in one pass, with no need to crawl
through lazer's `files` folder (which can contain 100,000+ files including
every piece of audio and every image in your library).

If `realm-reader` isn't available for some reason (not built, or it fails
to open your `client.realm`), the tool automatically falls back to scanning
the `files` folder directly instead — lazer stores every imported file as a
SHA-256-named blob with no extension, so this fallback reads just the first
few bytes of each one to identify which are actually beatmap files, without
touching your audio or image files.

You never need to run `realm-reader` yourself — the main app calls it
automatically when it's present.

## Credits

- [Piotrekol's CollectionManager](https://github.com/Piotrekol/CollectionManager) —
  reference for the `collection.db` binary format and the general approach
  of reading `client.realm` directly for osu!lazer support.
- [kabiiQ's BeatmapExporter](https://github.com/kabiiQ/BeatmapExporter) —
  an alternative way to get playable `.osz` copies out of osu!lazer, useful
  if you want more than just classification data.
- [ppy/osu](https://github.com/ppy/osu) — the osu!lazer client itself, used
  as the reference for the `client.realm` schema.
- [Realm .NET SDK](https://github.com/realm/realm-dotnet) — the database
  library `realm-reader` is built on.
- [osu!'s official beatmap tag definitions](https://osu.ppy.sh/wiki/en/Beatmap/Beatmap_tag) —
  the reference used to calibrate what actually counts as a "burst" vs
  "stream" vs "jump."

This project isn't affiliated with or endorsed by ppy or the osu! team.

## Disclaimers

- This is a hobby project, not a professionally audited tool. Classification
  is heuristic-based and won't be perfect on every map — always check
  `report.csv` before trusting the results on something you care about.
- **Always back up your existing `collection.db`** before replacing it.
- Ranked-status detection relies on assumptions about `client.realm`'s
  internal schema that haven't been independently verified against osu!'s
  source code — treat it as "probably right" rather than guaranteed.

## License

MIT
