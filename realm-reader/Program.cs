// realm-reader: a small helper for osu-beatmap-categorizer.
//
// Reads osu!lazer's client.realm directly (the same way Piotrekol's
// CollectionManager does) using the official Realm .NET SDK, and resolves
// every .osu file referenced inside it to its actual on-disk path in the
// content-addressed files/ store. This avoids walking and peeking every
// single file in files/ (which can be 100k+ files including audio/images)
// just to find the ones that are beatmaps.
//
// Opens the realm file in DYNAMIC schema mode - it reads whatever schema is
// embedded in the file itself, rather than requiring a reference to the
// full osu.Game project's model classes. This keeps the dependency surface
// to just the Realm package.
//
// Output: one resolved file path per line, written to the given output file
// (or stdout if no output path given). Never modifies client.realm - opens
// read-only. If anything about the schema doesn't match what's expected
// (osu! bumps the realm schema version periodically), this exits with a
// non-zero code and an error on stderr; osu-beatmap-categorizer's Python
// side falls back to its own filesystem scan when that happens, so a
// mismatch here isn't fatal to the overall tool.

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Realms;

class Program
{
    static int Main(string[] args)
    {
        if (args.Length < 1)
        {
            Console.Error.WriteLine("Usage: realm-reader <path-to-client.realm> [output-file]");
            Console.Error.WriteLine("If output-file is omitted, paths are printed to stdout.");
            return 1;
        }

        string realmPath = Path.GetFullPath(args[0]);
        string? outputPath = args.Length > 1 ? args[1] : null;

        if (!File.Exists(realmPath))
        {
            Console.Error.WriteLine($"ERROR: realm file not found: {realmPath}");
            return 2;
        }

        // osu!'s files/ store lives next to client.realm, in the same osu! data folder.
        string? dataDir = Path.GetDirectoryName(realmPath);
        if (dataDir == null)
        {
            Console.Error.WriteLine("ERROR: could not determine osu! data directory from realm path.");
            return 2;
        }
        string filesDir = Path.Combine(dataDir, "files");
        if (!Directory.Exists(filesDir))
        {
            Console.Error.WriteLine($"ERROR: expected files/ folder not found next to client.realm: {filesDir}");
            return 2;
        }

        var config = new RealmConfiguration(realmPath)
        {
            IsReadOnly = true,
            IsDynamic = true,
        };

        Realm realm;
        try
        {
            realm = Realm.GetInstance(config);
        }
        catch (Exception e)
        {
            Console.Error.WriteLine($"ERROR: could not open client.realm: {e.Message}");
            return 3;
        }

        using (realm)
        {
            // Diagnostic: list every class actually present in the schema
            // this realm file resolved to. If BeatmapSetInfo isn't in this
            // list, the fix is to use whatever name IS here instead of
            // assuming - schema class names can differ across osu! versions.
            var schemaNames = realm.Schema.Select(s => s.Name).OrderBy(n => n).ToList();
            Console.Error.WriteLine($"realm-reader: schema contains {schemaNames.Count} classes: {string.Join(", ", schemaNames)}");

            if (!schemaNames.Contains("BeatmapSet"))
            {
                Console.Error.WriteLine("ERROR: BeatmapSet not found in this realm's schema (see class list above) - "
                    + "falling back to filesystem scan.");
                return 4;
            }

            // Diagnostic: print the actual property names for the classes
            // we're about to use, in case those don't match assumptions
            // either (same issue as the class-name mismatch above).
            foreach (var className in new[] { "BeatmapSet", "RealmNamedFileUsage", "File", "Beatmap" })
            {
                var objSchema = realm.Schema.FirstOrDefault(s => s.Name == className);
                if (objSchema != null)
                {
                    var propNames = objSchema.Select(p => p.Name);
                    Console.Error.WriteLine($"realm-reader: {className} properties: {string.Join(", ", propNames)}");
                }
            }

            var writer = outputPath != null ? new StreamWriter(outputPath) : Console.Out;
            int setIndex = 0;
            try
            {
                int written = 0;
                int missing = 0;

                // Star rating lives on the individual difficulty ("Beatmap")
                // object, not the file itself, so build a hash -> star
                // rating lookup first.
                //
                // Join key is Beatmap.Hash, which is the SHA-256 of the .osu
                // file content - the SAME hash the content-addressed files/
                // store is keyed by, and therefore the same value we already
                // have in hand as RealmNamedFileUsage.File.Hash below. So the
                // join is a straight dictionary hit with no file IO at all.
                //
                // NOTE: Beatmap also exposes MD5Hash, which is a DIFFERENT
                // hash (the MD5 used for collection.db / online lookups).
                // Joining File.Hash against MD5Hash - or against an MD5
                // computed from the file's bytes - never matches, and paying
                // a full read of every .osu in the library to compute that
                // MD5 is what made this step take tens of minutes on a large
                // library (long enough for the Python side to hit its
                // subprocess timeout and silently fall back to a filesystem
                // scan). Keep this joining on Hash, not MD5Hash.
                var starRatingByHash = new Dictionary<string, double>();
                bool loggedStarRatingError = false;
                try
                {
                    dynamic beatmaps = realm.DynamicApi.All("Beatmap");
                    foreach (dynamic beatmap in beatmaps)
                    {
                        try
                        {
                            string bHash = (string)beatmap.Hash;
                            double sr = (double)beatmap.StarRating;
                            if (!string.IsNullOrEmpty(bHash))
                                starRatingByHash[bHash] = sr;
                        }
                        catch (Exception e)
                        {
                            if (!loggedStarRatingError)
                            {
                                Console.Error.WriteLine($"realm-reader: couldn't read Hash/StarRating on a Beatmap ({e.Message}) - star rating won't be available.");
                                loggedStarRatingError = true;
                            }
                        }
                    }
                    Console.Error.WriteLine($"realm-reader: loaded star ratings for {starRatingByHash.Count} difficulties.");
                }
                catch (Exception e)
                {
                    Console.Error.WriteLine($"realm-reader: couldn't read Beatmap class at all ({e.Message}) - star rating won't be available.");
                }

                dynamic beatmapSets = realm.DynamicApi.All("BeatmapSet");
                bool loggedFilesError = false;
                bool loggedFileInfoError = false;
                foreach (dynamic set in beatmapSets)
                {
                    setIndex++;
                    if (setIndex % 5000 == 0)
                        Console.Error.WriteLine($"realm-reader: processed {setIndex} beatmap sets so far...");
                    // osu!'s BeatmapOnlineStatus enum: -2=Graveyard, -1=WIP,
                    // 0=Pending, 1=Ranked, 2=Approved, 3=Qualified, 4=Loved.
                    // Ranked/Approved/Loved/Qualified all have real
                    // leaderboards and are what most players mean by
                    // "ranked" in casual usage - only Graveyard/WIP/Pending
                    // count as "unranked" here.
                    string rankedStatus = "unranked";
                    try
                    {
                        int statusValue = (int)set.Status;
                        if (statusValue >= 1)
                            rankedStatus = "ranked";
                    }
                    catch (Exception)
                    {
                        rankedStatus = "unknown";
                    }

                    IEnumerable<dynamic> files;
                    try
                    {
                        files = set.Files;
                    }
                    catch (Exception e)
                    {
                        if (!loggedFilesError)
                        {
                            Console.Error.WriteLine($"realm-reader: couldn't read .Files on BeatmapSet ({e.Message}) - skipping affected sets.");
                            loggedFilesError = true;
                        }
                        continue;
                    }

                    foreach (dynamic namedFileUsage in files)
                    {
                        string filename;
                        string hash;
                        try
                        {
                            filename = (string)namedFileUsage.Filename;
                            hash = (string)namedFileUsage.File.Hash;
                        }
                        catch (Exception e)
                        {
                            if (!loggedFileInfoError)
                            {
                                Console.Error.WriteLine($"realm-reader: couldn't read Filename/File.Hash on a file entry ({e.Message}) - skipping affected entries.");
                                loggedFileInfoError = true;
                            }
                            continue;
                        }

                        if (!filename.EndsWith(".osu", StringComparison.OrdinalIgnoreCase))
                            continue;
                        if (string.IsNullOrEmpty(hash) || hash.Length < 2)
                            continue;

                        // osu!'s content-addressed store uses a two-level prefix fanout,
                        // same convention as git's object store: files/<h0>/<h0h1>/<hash>
                        string resolved = Path.Combine(filesDir, hash.Substring(0, 1), hash.Substring(0, 2), hash);
                        if (File.Exists(resolved))
                        {
                            // Straight lookup on the store hash we already
                            // have - see the note above on why this must not
                            // go via MD5.
                            string starRatingStr = "unknown";
                            if (starRatingByHash.TryGetValue(hash, out double sr))
                                starRatingStr = sr.ToString("F2", System.Globalization.CultureInfo.InvariantCulture);
                            // Tab-separated: path, ranked status, star rating
                            writer.WriteLine($"{resolved}\t{rankedStatus}\t{starRatingStr}");
                            written++;
                        }
                        else
                        {
                            missing++;
                        }
                    }
                }

                Console.Error.WriteLine($"realm-reader: resolved {written} .osu file paths ({missing} referenced but not found on disk).");
            }
            catch (Exception e)
            {
                Console.Error.WriteLine($"ERROR: unexpected failure while processing beatmap sets (around set #{setIndex}): {e}");
                return 5;
            }
            finally
            {
                if (outputPath != null)
                    writer.Dispose();
            }
        }

        return 0;
    }
}
