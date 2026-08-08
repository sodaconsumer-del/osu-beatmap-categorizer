> Built with AI assistance

## Quick start

1. Point it at your beatmap folder:
   - **stable**: your `Songs` folder
   - **lazer**: your osu! data folder — the one with `client.realm` (`%appdata%\osu` on Windows by default). If you've moved your library to another drive, `%appdata%\osu` is just a stub containing a `storage.ini` that points at the real folder; that redirect is followed automatically, but you can also point straight at the real folder.
2. Choose an export folder and which categories you want.
3. Hit **Run classification**.
4. Check `report.csv` before trusting the result, then **back up your existing `collection.db`** before replacing it. ( or import via [CollectionManager](https://github.com/Piotrekol/CollectionManager) instead of copying the file directly.)

## Categories

| Category | Meaning |
|---|---|
| Streams | Has a genuine 10+ note stream (cutstreams count as streams) |
| Bursts | Has 3-9 note burst(s), no full streams |
| Jumps with bursts | Has both, but jumps cover more of the map |
| Jumps (no bursts) | Jump-heavy, no bursts or streams |
| Misc | None of the above |

Three notes is enough to be a burst — short bursts are everywhere in jump,
aim-control and flow-aim maps.

You can filter to specific categories, and (lazer only) split ranked from
unranked maps. Thresholds are adjustable via CLI flags or in the GUI.

## How it decides

Speed is measured in **absolute milliseconds between taps**, not as a ratio to
the map's stored BPM — plenty of maps are authored at a deliberately doubled
tempo, and anything keyed to the stored value gets those wrong. Stream BPM is
just `15000 / ms`, so the 140ms default is roughly "a 107 BPM stream or
faster".

A run also has to be **rhythmically consistent**: a real stream doesn't change
tapping speed partway through. A stream broken by a skipped beat is rejoined
before lengths are judged, so a cut stream stays one stream instead of
becoming two bursts.

Spacing is measured from each object's **end** position. Sliders are around
30% of a typical library, and measuring from the head makes a long slider
whose tail sits beside the next note read as a full-screen jump. Spinners are
skipped — their stored position is a placeholder, not where you actually move.

### Mods

`--mods DT`, `--mods HR`, or any combination (checkboxes in the GUI). NM is
the baseline. Only two things a mod does can change what a pattern *is*: rate
(DT/NC/HT/DC) and circle size (HR/EZ). HR's vertical flip is ignored on
purpose — reflecting every object preserves the distances between them.

## Accuracy

These are heuristics, so there's tooling to measure them rather than argue
about them. Synthetic unit tests:

```
python test_classify.py
```

To score against real maps, osu!'s own community beatmap tags make a
ready-made ground truth:

```
python fetch_osu_tags.py --csv report.csv --out labels.csv
python eval_classifier.py --csv report.csv --labels labels.csv
```

That prints per-category precision/recall and a confusion matrix. Add
`--baseline old_report.csv` to check whether a threshold change actually
helped instead of just moving errors around. You'll need a free osu! OAuth app
for the tag fetch, and `online_id` is only populated on the lazer realm fast
path.

## Credits

- [Piotrekol's CollectionManager](https://github.com/Piotrekol/CollectionManager) — `collection.db` format reference, and the approach of reading `client.realm` directly
- [kabiiQ's BeatmapExporter](https://github.com/kabiiQ/BeatmapExporter) — alternative lazer export tool
- [ppy/osu](https://github.com/ppy/osu) — `client.realm` schema reference
- [Realm .NET SDK](https://github.com/realm/realm-dotnet) — powers the `realm-reader` fast path
- [osu!'s official beatmap tags](https://osu.ppy.sh/wiki/en/Beatmap/Beatmap_tags) — reference for what counts as a burst/stream/jump

Not affiliated with or endorsed by ppy or the osu! team.

## License

MIT
