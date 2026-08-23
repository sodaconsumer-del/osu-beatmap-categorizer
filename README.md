## ❗❗❗DISCLAIMER❗❗❗
This project is not affiliated with, endorsed by, or sponsored by ppy Pty Ltd or osu!. "osu!" and related trademarks belong to their respective owners.
##
This tool was built largely with AI assistance, Though I have endured rigorous testing and debugging to ensure that the classification is near-perfect.

It is really good at detecting maps with streams, but there may be some false-positives with detecting high bpm jump maps and certain maps with halved or doubled bpms.

Currently the tool only uses NM difficulty attributes to categorize the maps. Though I plan on making it work for DT and HR in the future.

Note that if you play private servers, maps that are ranked on there but not on bancho will be marked as ranked, I believe that this can be fixed by deleting your osu!.db and processing your beatmaps, but i haven't tested that yet.
##
## Contact
If you wish to contact me, you can find me on osu! ( [-soda-](https://osu.ppy.sh/users/17477549) ) Or if you want to message me on discord, my username is plaaanet.
##
## What it does
Allows you to find maps matching your skillset using a vast amount of classification methods, you can configure the tool to your needs as well. each method works in harmony to ensure the best outcome, I recommend using [Piotrekol's CollectionManager](https://github.com/Piotrekol/CollectionManager) instead of overwriting your existing collection.db, It works for both lazer and stable.
##
## Contributing 
If you wish to contribute to the project, feel free to make a PR and list what you changed, or improved. 
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
