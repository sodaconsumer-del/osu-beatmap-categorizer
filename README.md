## ❗❗❗DISCLAIMER❗❗❗

This project is not affiliated with, endorsed by, or sponsored by ppy Pty Ltd or osu!. "osu!" and related trademarks belong to their respective owners.

This tool was built largely with AI assistance (code generation, debugging, etc.), with data collection, testing, and direction provided by me. Use at your own discretion.

##

## Contact

If you wish to contact me, you can find me on osu! ( [-soda-](https://osu.ppy.sh/users/17477549) ) Or if you want to message me on discord, my username is plaaanet.

##

## Download

Grab the latest zip from [Releases](../../releases) and extract it. **Windows
only** for now. No Python needed.

Inside you'll find `osu-beatmap-categorizer.exe` — that's the one to run — next
to a `realm-reader` folder. Leave that folder where it is: it holds the helper
that reads osu!lazer's database directly, and without it a lazer scan falls
back to a much slower method.

> **Windows will probably warn you the first time.** The app isn't code-signed
> (certificates cost money), so SmartScreen shows "Windows protected your PC".
> *More info → Run anyway.* Some antivirus also flags PyInstaller-built
> executables as suspicious — that's a known false positive with how PyInstaller
> packs Python apps, not something specific to this tool. If you'd rather not
> take my word for it, the source is right here and you can run it with
> `python gui.py` instead.

##

## Quick start

1. Point it at your beatmap folder:
   - **stable**: your `Songs` folder
   - **lazer**: your osu! data folder — the one with `client.realm` (`%appdata%\osu` on Windows by default). If you've moved your library to another drive, `%appdata%\osu` is just a stub containing a `storage.ini` that points at the real folder; that redirect is followed automatically, but you can also point straight at the real folder.

2. Choose an export folder and which categories you want.

3. Hit **Run classification**.

4. Check `report.csv` before trusting the result, then **back up your existing `collection.db`** before replacing it. ( or import via [CollectionManager](https://github.com/Piotrekol/CollectionManager) instead of copying the file directly.)

##

## Categories

| Category | Meaning |
|---|---|
| Streams | Has a genuine 10+ note stream (cutstreams count as streams) |
| Bursts | Has 3-9 note burst(s), no full streams |
| Jumps with bursts | Has both, but jumps cover more of the map |
| Jumps (no bursts) | Jump-heavy, no bursts or streams |
| Misc | None of the above |

Three notes is enough to be a burst — short bursts are everywhere in jump, aim-control and flow-aim maps.

You can filter to specific categories, and (lazer only) split ranked from unranked maps. Thresholds are adjustable via CLI flags or in the GUI.

## How it decides

Speed is measured in **absolute milliseconds between taps**, not as a ratio to the map's stored BPM — plenty of maps are authored at a deliberately doubled tempo, and anything keyed to the stored value gets those wrong. Stream BPM is just `15000 / ms`, so the 140ms default is roughly "a 107 BPM stream or faster".

A run also has to be **rhythmically consistent**: a real stream doesn't change tapping speed partway through. A stream broken by a skipped beat is rejoined before lengths are judged, so a cut stream stays one stream instead of becoming two bursts.

A **burst map that streams even once is a stream map**. Bursts and streams are the same motion, so what matters to a burst player isn't how much of the map streams but whether it ever demands sustained stream stamina at all — one run of 12+ notes does. This applies only to burst maps: a short run inside a jump map is usually tightly-spaced jumps rather than real streaming.

Patterns also have to **cover enough of the map to own it**. One 10-note run in a 400-note jump map doesn't make it a stream map, so streams need at least 15% of the map's notes before they count at all — and even then they still have to out-cover the jumps. This is what stops tightly-spaced jump maps (NiNo-style diffs are the classic case, where the jumps sit close enough together to look like a stream) from being filed under Streams.

Spacing is measured from each object's **end** position. Sliders are around 30% of a typical library, and measuring from the head makes a long slider whose tail sits beside the next note read as a full-screen jump. Spinners are skipped — their stored position is a placeholder, not where you actually move.

##

### Mods

`--mods DT`, `--mods HR`, or any combination (checkboxes in the GUI). NM is the baseline. Only two things a mod does can change what a pattern *is*: rate (DT/NC/HT/DC) and circle size (HR/EZ). HR's vertical flip is ignored on purpose — reflecting every object preserves the distances between them.

##

## Accuracy

These are heuristics, so there's tooling to measure them rather than argue about them. Synthetic unit tests:

```
python test_classify.py
```

To score against real maps, hand-label a few dozen mapsets you know well into a `labels.csv` of `online_id,label`, then:

```
python eval_classifier.py --csv report.csv --labels labels.csv
```

That prints per-category precision/recall and a confusion matrix. Add `--baseline old_report.csv` to check whether a threshold change actually helped instead of just moving errors around. `online_id` is only populated on the lazer realm fast path.

Scraping osu!'s community beatmap tags was tried and dropped: outside the most popular few hundred maps, almost nothing carries a usertag, so the coverage is far too thin to tune against.

##

## Running from source

Needs Python 3.8+ and nothing else — the app is pure standard library.

```
python gui.py
```

or from the command line:

```
python classify_maps.py "C:/Users/you/AppData/Roaming/osu" --csv report.csv --output collection.db
```

`python classify_maps.py --help` lists every option.

The `realm-reader` helper is optional and only speeds up osu!lazer scans. To
build it you need the .NET 8 SDK:

```
dotnet publish realm-reader/RealmReader.csproj -c Release -r win-x64 --self-contained true -o realm-reader-dist
```

`realm-reader-dist/` is gitignored and picked up automatically — don't publish
into `realm-reader/` itself, or ~190 runtime DLLs land on top of the source.

##

## Credits

- [Piotrekol's CollectionManager](https://github.com/Piotrekol/CollectionManager) — `collection.db` format reference, and the approach of reading `client.realm` directly

- [kabiiQ's BeatmapExporter](https://github.com/kabiiQ/BeatmapExporter) — alternative lazer export tool

- [ppy/osu](https://github.com/ppy/osu) — `client.realm` schema reference

- [Realm .NET SDK](https://github.com/realm/realm-dotnet) — powers the `realm-reader` fast path

- [osu!'s official beatmap tags](https://osu.ppy.sh/wiki/en/Beatmap/Beatmap_tags) — reference for what counts as a burst/stream/jump

##

Not affiliated with or endorsed by ppy or the osu! team.

## License

MIT
