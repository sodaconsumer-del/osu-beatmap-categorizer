#!/usr/bin/env python3
"""
Fetches osu!'s official community beatmap tags and turns them into ground
truth labels for the classifier.

Why this exists
---------------
Every threshold in classify_maps.py was tuned by eye. That means there is no
way to tell whether changing one made the classifier better or worse - a
change either "looks right" on a handful of maps or it doesn't. This script
plus eval_classifier.py replace that with a number.

osu! itself has community-voted beatmap tags, including the exact concepts
this tool classifies by:
    https://osu.ppy.sh/wiki/en/Beatmap/Beatmap_tag
Ranked maps carry them, and a decent library has tens of thousands of ranked
difficulties, so the labels are already there for the taking.

Setup
-----
You need an osu! OAuth application (free, takes a minute):
    https://osu.ppy.sh/home/account/edit  ->  OAuth  ->  New OAuth Application
Client credentials grant is enough - no user login, no redirect URL needed.

Then either export them:
    OSU_CLIENT_ID=12345 OSU_CLIENT_SECRET=xxxx python fetch_osu_tags.py ...
or pass --client-id / --client-secret.

Usage
-----
    python fetch_osu_tags.py --csv report.csv --out labels.csv

Reads the online_id column from a report.csv produced by classify_maps.py
(populated only via the osu!lazer realm fast path), fetches each beatmap's
tags, maps them onto this tool's categories, and writes a labels.csv of
    online_id,label,raw_tags

Only stdlib - no requests/httpx needed.
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


API = "https://osu.ppy.sh/api/v2"
TOKEN_URL = "https://osu.ppy.sh/oauth/token"

# osu! tag -> our category. Only tags that map cleanly onto what this tool
# actually decides are listed; anything else is ignored rather than guessed at.
#
# Order matters on collision: a map tagged both "streams" and "jumps" is a
# stream map under our own dominant-pattern rule, so Streams wins. This
# mirrors derive_collections() rather than inventing a second ranking.
TAG_TO_CATEGORY = [
    ("streams", "Streams"),
    ("deathstream", "Streams"),
    ("cutstreams", "Streams"),
    ("spaced-streams", "Streams"),
    ("bursts", "Bursts"),
    ("jumps", "Jumps (no bursts)"),
    ("spaced-jumps", "Jumps (no bursts)"),
    ("aim", "Jumps (no bursts)"),
]


def get_token(client_id, client_secret):
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
        "scope": "public",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["access_token"]


def fetch_beatmaps(token, ids):
    """GET /beatmaps?ids[]=... - up to 50 per call, which is the API's limit."""
    qs = "&".join(f"ids[]={i}" for i in ids)
    req = urllib.request.Request(f"{API}/beatmaps?{qs}", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp).get("beatmaps", [])


def tags_of(beatmap):
    """Community top_tag_ids plus the mapper's own free-text tags."""
    out = []
    for t in beatmap.get("top_tag_ids") or []:
        name = (t.get("tag") or {}).get("name") if isinstance(t, dict) else None
        if name:
            out.append(name.lower())
    beatmapset = beatmap.get("beatmapset") or {}
    raw = beatmapset.get("tags") or ""
    out.extend(w.lower() for w in raw.split())
    return out


def label_for(tags):
    tagset = set(tags)
    for tag, category in TAG_TO_CATEGORY:
        if tag in tagset:
            return category
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True, help="report.csv from classify_maps.py")
    ap.add_argument("--out", default="labels.csv", help="where to write the labels")
    ap.add_argument("--client-id", default=os.environ.get("OSU_CLIENT_ID"))
    ap.add_argument("--client-secret", default=os.environ.get("OSU_CLIENT_SECRET"))
    ap.add_argument("--limit", type=int, default=None,
                     help="stop after this many beatmaps (handy for a quick trial run)")
    ap.add_argument("--sleep", type=float, default=1.0,
                     help="seconds between API calls. Be polite - the osu! API is a "
                          "free service run for the community, and there is no reason "
                          "to hammer it for a one-off labelling job.")
    args = ap.parse_args()

    if not args.client_id or not args.client_secret:
        print("Error: need osu! API credentials. Set OSU_CLIENT_ID and OSU_CLIENT_SECRET,\n"
              "or pass --client-id/--client-secret. Create an app at:\n"
              "  https://osu.ppy.sh/home/account/edit  ->  OAuth", file=sys.stderr)
        return 2

    ids = []
    seen = set()
    with open(args.csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            raw = (row.get("online_id") or "").strip()
            if not raw or raw.lower() == "unknown":
                continue
            try:
                oid = int(raw)
            except ValueError:
                continue
            if oid in seen:
                continue
            seen.add(oid)
            ids.append(oid)

    if not ids:
        print("No usable online_id values in that CSV.\n"
              "online_id is only populated by the osu!lazer realm fast path - a plain\n"
              "folder scan has no way to know a beatmap's online ID.", file=sys.stderr)
        return 1

    if args.limit:
        ids = ids[:args.limit]
    print(f"{len(ids)} beatmaps to look up ({(len(ids) + 49) // 50} API calls).")

    try:
        token = get_token(args.client_id, args.client_secret)
    except urllib.error.HTTPError as e:
        print(f"Auth failed ({e.code}). Check the client id/secret.", file=sys.stderr)
        return 1

    labelled = 0
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["online_id", "label", "raw_tags"])
        for start in range(0, len(ids), 50):
            chunk = ids[start:start + 50]
            try:
                beatmaps = fetch_beatmaps(token, chunk)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    print("  rate limited - backing off 30s")
                    time.sleep(30)
                    continue
                print(f"  chunk failed ({e.code}), skipping", file=sys.stderr)
                continue
            for b in beatmaps:
                tags = tags_of(b)
                label = label_for(tags)
                if label:
                    labelled += 1
                w.writerow([b.get("id"), label or "", " ".join(tags)])
            done = min(start + 50, len(ids))
            print(f"  {done}/{len(ids)} fetched, {labelled} labelled", end="\r")
            time.sleep(args.sleep)

    print(f"\nWrote {args.out}: {labelled} of {len(ids)} beatmaps carry a usable tag.")
    print(f"Now score the classifier against it:\n"
          f"  python eval_classifier.py --csv {args.csv} --labels {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
