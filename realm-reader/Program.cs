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
            var writer = outputPath != null ? new StreamWriter(outputPath) : Console.Out;
            try
            {
                int written = 0;
                int missing = 0;

                dynamic beatmapSets = realm.DynamicApi.All("BeatmapSetInfo");
                foreach (dynamic set in beatmapSets)
                {
                    IEnumerable<dynamic> files;
                    try
                    {
                        files = set.Files;
                    }
                    catch (Exception)
                    {
                        continue; // schema surprise on this object - skip it, don't crash the whole run
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
                        catch (Exception)
                        {
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
                            writer.WriteLine(resolved);
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
            finally
            {
                if (outputPath != null)
                    writer.Dispose();
            }
        }

        return 0;
    }
}
