# osu-beatmap-categorizer

Scans your osu! beatmap library and sorts every difficulty into collections
based on its actual note-pattern content — bursts, streams, cutstreams,
jumps, and useful combinations of those (hybrid maps, jump maps that also
burst, etc.) — then writes a ready-to-import `collection.db`.

Works with both **osu!stable** (point it at your `Songs/` folder) and
**osu!lazer**. For lazer, point the tool at your osu! data folder (the one
containing `client.realm` and `files/` - `%appdata%\osu` on Windows,
`~/.local/share/osu` on Linux, `~/Library/Application Support/osu` on
macOS). Two paths are used automatically:

- **Fast path**: if the `realm-reader` helper is present (bundled with
  released builds), it reads `client.realm` directly - the same approach
  [CollectionManager](https://github.com/Piotrekol/CollectionManager) uses
  - and resolves every `.osu` file's on-disk location in one pass, with no
  filesystem crawling of the (often huge) `files/` blob store at all.
- **Fallback path**: if the helper isn't available or fails for any reason,
  the tool falls back to scanning `files/` directly - lazer stores every
  imported file (audio, images, `.osu`, skins) as a SHA-256-named blob with
  no extension, so this peeks the first bytes of each one and only fully
  reads the ones that are actually beatmap files.

Either way, no export step is required. (You can also use
[BeatmapExporter](https://github.com/kabiiQ/BeatmapExporter) first if you'd
rather work from an exported `.osz`/`.osu` folder instead - both `.osz`
archives and loose `.osu` files are supported too.)

## Download

Grab the standalone executable for your OS from the
[Releases](../../releases) page — no Python required.

## Categories

Every difficulty is tagged with up to four properties - Streams, Bursts,
Cutstreams, Jumps - and lands in **exactly one** collection named for its
exact combination (e.g. a map with both bursts and jumps but no streams
goes in a collection literally called `Bursts, Jumps`). A diff with none of
the four tags goes in `Misc`. This means no duplicates: a hybrid map that
has bursts, jumps, and streams all together shows up once, in
`Streams, Bursts, Jumps` - not separately in three different collections.

| Tag | Meaning |
|---|---|
| Streams | Contains a 10+ note run, fast-snapped and tightly spaced |
| Bursts | Contains a 3-9 note run, fast-snapped and tightly spaced |
| Cutstreams | A stream where a minority of notes have much larger spacing than the rest |
| Jumps | Jump-heavy: wide spacing covered quickly, relative to available time |

Thresholds default close to osu!'s own [official beatmap tag definitions](https://osu.ppy.sh/wiki/en/Beatmap/Beatmap_tag)
(streams: 10+ notes matches exactly; bursts default to a 3-9 note minimum here,
slightly wider than osu!'s own 5-9, to catch shorter runs too), and are
fully adjustable in the GUI or via CLI flags.

## Using it

1. **osu!stable**: run the tool and point it at your `Songs/` folder
   (usually `%localappdata%\osu!\Songs` on Windows).
2. **osu!lazer**: point the tool directly at your lazer `files/` folder -
   there's a one-click button for this in the GUI if it's in the default
   location. No export needed. (Or use
   [BeatmapExporter](https://github.com/kabiiQ/BeatmapExporter) first if
   you'd prefer to work from an exported folder instead.)
3. Review the generated `report.csv` before trusting the results.
4. **Back up your existing `collection.db`** before replacing it, or merge
   the output in using a tool like
   [Piotrekol's CollectionManager](https://github.com/Piotrekol/CollectionManager).

## Running from source

No dependencies beyond the Python standard library.

```
python gui.py
```

or, from the command line:

```
python classify_maps.py "C:/path/to/Songs" --csv report.csv --output collection.db
```

Run `python classify_maps.py --help` for all tunable thresholds.

## Building the executable yourself

```
pip install pyinstaller
pyinstaller --onefile --windowed --name osu-classifier gui.py
```

The output will be in `dist/`. GitHub Actions (`.github/workflows/build.yml`)
does this automatically for Windows/Mac/Linux whenever a `v*` tag is pushed.

## How classification works

A note-to-note transition only joins a burst/stream run if it's fast enough
relative to the map's current BPM. Whether that run counts as a genuine
burst/stream (vs. a jump that merely happens to be fast) depends on how much
of the run is tightly spaced vs. widely spaced — matching e.g. 1/4-snap jump
patterns (fast timing, wide spacing) being correctly excluded from bursts/streams.

Jump detection is velocity-based: distance traveled (in circle diameters),
normalized by time available (in beats) — not raw distance — so low-density
Easy/Normal diffs aren't false-flagged as "jump maps" just because their
notes are naturally spread out over more time.

## License

MIT
