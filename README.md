# osu-beatmap-categorizer

Scans your osu! beatmap library and sorts every difficulty into collections
by what it actually plays like — Streams, Bursts, or Jumps — based on real
note data, not tags or star rating. Works with both **osu!stable** and
**osu!lazer**, no export step needed.

> Built with AI assistance (Claude, by Anthropic) — human-directed and
> tested against real beatmaps, but not professionally audited. Please
> open an issue if you find a misclassified map.

## Download

Grab the app for your OS from [Releases](../../releases) — no Python needed.

## Quick start

1. Point it at your beatmap folder:
   - **stable**: your `Songs` folder
   - **lazer**: your osu! data folder (`%appdata%\osu` on Windows — the one with `client.realm`)
2. Choose an export folder and which categories you want.
3. Hit **Run classification**.
4. Check `report.csv` before trusting the result, then **back up your existing `collection.db`** before replacing it. (On lazer, import via [CollectionManager](https://github.com/Piotrekol/CollectionManager) instead of copying the file directly.)

## Categories

Each diff lands in **one** category, by dominant pattern:
**Streams > Bursts > Jumps > Misc**

| Category | Meaning |
|---|---|
| Streams | Has a genuine 10+ note stream (cutstreams count as streams) |
| Bursts | Has 3-9 note burst(s), no full streams |
| Jumps | Jump-heavy, no bursts or streams |
| Misc | None of the above |

You can filter to specific categories, and (lazer only) split ranked from
unranked maps. Thresholds are adjustable via CLI flags or in the GUI.

## Credits

- [Piotrekol's CollectionManager](https://github.com/Piotrekol/CollectionManager) — `collection.db` format reference, and the approach of reading `client.realm` directly
- [kabiiQ's BeatmapExporter](https://github.com/kabiiQ/BeatmapExporter) — alternative lazer export tool
- [ppy/osu](https://github.com/ppy/osu) — `client.realm` schema reference
- [Realm .NET SDK](https://github.com/realm/realm-dotnet) — powers the `realm-reader` fast path
- [osu!'s official beatmap tags](https://osu.ppy.sh/wiki/en/Beatmap/Beatmap_tag) — reference for what counts as a burst/stream/jump

Not affiliated with or endorsed by ppy or the osu! team.

## Running from source

```
python gui.py
```

or

```
python classify_maps.py "path/to/Songs" --csv report.csv --output collection.db
```

`python classify_maps.py --help` for all options.

## License

MIT
