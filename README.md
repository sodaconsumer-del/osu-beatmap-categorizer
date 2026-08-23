## ❗❗❗DISCLAIMER❗❗❗

This project is not affiliated with, endorsed by, or sponsored by ppy Pty Ltd or osu!. "osu!" and related trademarks belong to their respective owners.

This tool was built largely with AI assistance (code generation, debugging, etc.), with data collection, testing, and direction provided by me. Use at your own discretion.

##

## Contact

If you wish to contact me, you can find me on osu! ( [-soda-](https://osu.ppy.sh/users/17477549) ) Or if you want to message me on discord, my username is plaaanet.

##

## What it does

Scans your osu! beatmap library and sorts every difficulty into **Streams**, **Hybrid**, **Bursts**, **Jumps with bursts**, **Jumps (no bursts)**, or **Misc**, based on the actual note patterns — not tags, not star rating. Works with both osu!stable and osu!lazer, no export step needed. Writes an osu!stable-compatible `collection.db` plus a `report.csv` you should check before trusting the result.



Detection sensitivity is a single **Stricter / Balanced / Looser** choice. Balanced is the default and is what the thresholds were actually measured against; every individual threshold is still there under "Advanced" if you want them, each labelled in plain English.

See [AGENTS.md](AGENTS.md) for how the classification actually works and why.

##

## Download

Grab the latest zip from [Releases](../../releases) and extract it. **Windows only** for now. No Python needed.

Run `osu-beatmap-categorizer.exe`. Keep the `realm-reader` folder next to it — it's the helper that reads osu!lazer's database directly; without it, lazer scans fall back to a slower method.

> **Windows will probably warn you the first time** (SmartScreen — the app isn't code-signed). *More info → Run anyway.* If you'd rather not take that on faith, the source is right here — run it with `python gui.py` instead.

##

## Quick start

1. Point it at your beatmap folder — for stable, your osu! **install folder** (not `Songs` directly.), for lazer, its data folder. Redirects are followed automatically.
2. Choose an export folder and which categories you want.
3. Hit **Run classification**.
4. Check `report.csv`, then **back up your existing `collection.db`** before replacing it — or import via [CollectionManager](https://github.com/Piotrekol/CollectionManager) instead of copying the file directly.

##

## Building from source

Needs Python 3.8+ and nothing else — pure standard library.

```
python gui.py
```

or from the command line:

```
python classify_maps.py "C:/Users/you/AppData/Roaming/osu" --csv report.csv --output collection.db
```

`python classify_maps.py --help` lists every option.

The `realm-reader` helper is optional (speeds up lazer scans) and needs the .NET 8 SDK to build:

```
dotnet publish realm-reader/RealmReader.csproj -c Release -r win-x64 --self-contained true -o realm-reader-dist
```

`realm-reader-dist/` is gitignored and picked up automatically — don't publish into `realm-reader/` itself, or ~190 runtime DLLs land on top of the source.

##

## Credits

- [Piotrekol's CollectionManager](https://github.com/Piotrekol/CollectionManager) — `collection.db` and `osu!.db` format reference, and the approach of reading the game's own databases directly
- [kabiiQ's BeatmapExporter](https://github.com/kabiiQ/BeatmapExporter) — alternative lazer export tool
- [ppy/osu](https://github.com/ppy/osu) — `client.realm` schema reference
- [Realm .NET SDK](https://github.com/realm/realm-dotnet) — powers the `realm-reader` fast path
- [osu!'s official beatmap tags](https://osu.ppy.sh/wiki/en/Beatmap/Beatmap_tags) — reference for what counts as a burst/stream/jump

##

Not affiliated with or endorsed by ppy or the osu! team.

## License

MIT
