> Built with AI assistance

## Quick start

1. Point it at your beatmap folder:
   - **stable**: your `Songs` folder
   - **lazer**: your osu! data folder (`%appdata%\osu` on Windows — the one with `client.realm`)
2. Choose an export folder and which categories you want.
3. Hit **Run classification**.
4. Check `report.csv` before trusting the result, then **back up your existing `collection.db`** before replacing it. ( or import via [CollectionManager](https://github.com/Piotrekol/CollectionManager) instead of copying the file directly.)

## Categories

| Category | Meaning |
|---|---|
| Streams | Has a genuine 10+ note stream |
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
- [osu!'s official beatmap tags](https://osu.ppy.sh/wiki/en/Beatmap/Beatmap_tags) — reference for what counts as a burst/stream/jump

Not affiliated with or endorsed by ppy or the osu! team.

## License

MIT
