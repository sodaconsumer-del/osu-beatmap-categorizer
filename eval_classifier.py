#!/usr/bin/env python3
"""
Scores the classifier against ground-truth labels.

Turns "does this threshold change help?" from a matter of opinion into a
number.

Where the labels come from
--------------------------
Hand-label them. That sounds like more work than it is: sort a few dozen
mapsets you know well into folders named after the category you'd put them
in, and build labels.csv from the folder names.

This deliberately does NOT scrape osu!'s community beatmap tags - the
server-side usertags under skillset/, streams/ and so on. That was tried and
dropped: outside the few hundred most popular maps almost nothing carries
one, so the coverage is far too thin to tune against and biased towards
popular maps besides.

A DIFFERENT thing with a confusingly similar name is worth having, and
--tags below is it: the mapper's own `Tags:` line inside the .osu file.
That ships with every copy of the map, needs no network, and 11% of a real
library names a skillset in it. It is not ground truth - mappers write tags
to be searched for - so it can never be a tuning target. It is a second
opinion, and the point of a second opinion is the cases where it disagrees.

If you want an automatic signal later, the osu! API's per-beatmap difficulty
attributes expose aim_difficulty and speed_difficulty, and their ratio
separates jump maps from stream maps rather well. It's one request per
beatmap rather than fifty, so it only suits a sample of a few hundred - which
is all an eval set needs anyway.

labels.csv format - one row per beatmap, header required:

    online_id,label
    1234567,Streams
    2345678,Jumps (no bursts)

Usage
-----
    python classify_maps.py "C:/Users/you/AppData/Roaming/osu" --csv report.csv --no-db
    python eval_classifier.py --csv report.csv --labels labels.csv

Then change a threshold, re-run, and compare:

    python classify_maps.py "C:/Users/you/AppData/Roaming/osu" --csv new.csv --no-db --max-gap-ms 120
    python eval_classifier.py --csv new.csv --labels labels.csv --baseline report.csv

Or, with no labels at all, hold the verdicts up against what the mappers
called their own maps:

    python eval_classifier.py --csv report.csv --tags
    python eval_classifier.py --csv report.csv --tags --baseline old.csv
    python eval_classifier.py --csv report.csv --tags --write-disagreements out.csv

Reading the output
------------------
Accuracy alone is misleading when categories are lopsided, which they are -
most libraries are mostly streams. Per-category precision and recall are the
numbers that actually matter:

  precision  of the maps we called Streams, how many really were
  recall     of the maps that really were Streams, how many we caught

A change that raises one while sinking the other has not improved anything,
it has just moved a threshold. The confusion matrix shows where the mistakes
go, which is usually more informative than the totals.

Only stdlib.
"""

import argparse
import csv
import re
import sys

import classify_maps as cm


def load_predictions(path):
    """online_id -> predicted category, from a report.csv."""
    preds = {}
    missing_online_id = 0
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            raw = (row.get("online_id") or "").strip()
            if not raw or raw.lower() == "unknown":
                missing_online_id += 1
                continue
            try:
                oid = int(raw)
            except ValueError:
                continue
            category = (row.get("category") or "").strip()
            if category not in cm.CATEGORIES:
                # Older CSVs predate the category column; rebuild it with the
                # same shared rules rather than a second copy of them.
                try:
                    bnt = int(row.get("burst_note_total") or 0)
                    snt = int(row.get("stream_note_total") or 0)
                    tnc = int(row.get("total_note_count") or 0)
                    src = int(row.get("stream_runs") or 0)
                    msl = int(row.get("max_stream_len") or 0)
                    jp = float(row.get("jump_pct") or 0)
                except ValueError:
                    bnt = snt = tnc = src = msl = 0
                    jp = 0.0
                category = cm.category_of(
                    row.get("has_streams") == "True",
                    row.get("has_bursts") == "True",
                    row.get("has_jumps") == "True",
                    bnt, tnc, jp, snt, src, msl,
                )
            preds[oid] = category
    return preds, missing_online_id


def load_labels(path):
    labels = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            label = (row.get("label") or "").strip()
            if not label:
                continue
            try:
                labels[int(row["online_id"])] = label
            except (ValueError, KeyError):
                continue
    return labels


def score(preds, labels):
    """Returns (pairs, per-category counts). Only ids present in both count."""
    pairs = [(labels[oid], preds[oid]) for oid in labels if oid in preds]
    stats = {c: {"tp": 0, "fp": 0, "fn": 0} for c in cm.CATEGORIES}
    for truth, pred in pairs:
        if truth == pred:
            stats[pred]["tp"] += 1
        else:
            if pred in stats:
                stats[pred]["fp"] += 1
            if truth in stats:
                stats[truth]["fn"] += 1
    return pairs, stats


def f1(p, r):
    return 2 * p * r / (p + r) if (p + r) else 0.0


def report(pairs, stats, title):
    total = len(pairs)
    correct = sum(1 for t, p in pairs if t == p)
    print(f"\n=== {title} ===")
    print(f"{total} labelled difficulties matched, {correct} correct "
          f"({correct / total * 100:.1f}% accuracy)\n" if total else "no overlap\n")
    if not total:
        return 0.0

    print(f"{'category':<20}{'precision':>10}{'recall':>9}{'f1':>8}{'support':>9}")
    macro = []
    for c in cm.CATEGORIES:
        s = stats[c]
        support = s["tp"] + s["fn"]
        if not support and not s["fp"]:
            continue
        prec = s["tp"] / (s["tp"] + s["fp"]) if (s["tp"] + s["fp"]) else 0.0
        rec = s["tp"] / support if support else 0.0
        macro.append(f1(prec, rec))
        print(f"{c:<20}{prec:>9.1%}{rec:>9.1%}{f1(prec, rec):>8.2f}{support:>9}")
    macro_f1 = sum(macro) / len(macro) if macro else 0.0
    print(f"\nmacro F1: {macro_f1:.3f}   (the headline number - treats every "
          f"category equally,\n          so it can't be gamed by nailing the "
          f"biggest one and ignoring the rest)")

    present = [c for c in cm.CATEGORIES
               if stats[c]["tp"] + stats[c]["fn"] or stats[c]["fp"]]
    print(f"\nconfusion matrix (rows = truth, columns = predicted):")
    head = "".join(f"{c[:9]:>11}" for c in present)
    print(f"{'':<20}{head}")
    for truth in present:
        cells = "".join(
            f"{sum(1 for t, p in pairs if t == truth and p == pred):>11}"
            for pred in present)
        print(f"{truth:<20}{cells}")
    return macro_f1


# --------------------------------------------------------------------------
# Mapper tags as a second opinion
# --------------------------------------------------------------------------
#
# Every .osu carries a `Tags:` line the mapper wrote. Measured over 3,769
# difficulties, 11% name a skillset in it, and where they do they line up
# with the classifier closely enough to be worth listening to:
#
#     tag       n    Streams  Hybrid  Bursts  Jumps+b  Jumps  Misc
#     stream  180        64%     19%      3%       8%     3%    3%
#     jump    120         4%      1%      1%      42%    48%    3%
#     stamina  38        82%      8%      3%       3%     5%    0%
#
# `jump` lands 90% inside the two jump categories and `stream` 83% inside the
# stream family. That is a real signal, and across a whole library it is tens
# of thousands of difficulties against the couple of dozen anyone will
# hand-label - including the only outside evidence that exists for Streams
# and Hybrid, which no hand-labelled set in this repo covers at all.
#
# It is NOT ground truth and must never be tuned against. Mappers tag for
# searchability: sets carry the whole spread of words their guest mappers
# used, tags get copied between diffs of a set that do not play alike, and
# plenty of maps are tagged for a song or a tournament rather than a
# skillset. Fitting thresholds to this would fit tag-writing convention.
# What it is good for is the disagreements, and specifically for telling a
# near miss (a threshold sitting in the wrong place) from a hard miss (a
# detector not seeing what is there).

# Word -> the family of categories that word implies. Only words with a
# defensible mapping onto CATEGORIES are scored; the rest are reported in the
# breakdown but left out of the agreement figure, because guessing at what
# "tech" or "alt" should map to would be inventing the answer.
TAG_FAMILIES = {
    "stream": "stream", "streams": "stream", "deathstream": "stream",
    "deathstreams": "stream", "stamina": "stream",
    "jump": "jump", "jumps": "jump", "jumpy": "jump", "farm": "jump",
}
FAMILY_CATEGORIES = {
    "stream": {"Streams", "Hybrid"},
    "jump": {"Jumps with bursts", "Jumps (no bursts)", "Hybrid"},
}
# Words worth showing but not scoring - they describe skills this tool does
# not have categories for.
UNSCORED_WORDS = ("tech", "technical", "alt", "alternating", "aim", "speed",
                  "burst", "bursts", "flow", "consistency")


def tag_words(row):
    """The skillset words a row's mapper tags and difficulty name contain."""
    raw = (row.get("tags") or "").lower()
    words = {w.strip(" ,.;:()[]") for w in raw.split()}
    found = {w for w in words if w in TAG_FAMILIES or w in UNSCORED_WORDS}
    name = (row.get("diff_name") or "").lower()
    for w in list(TAG_FAMILIES) + list(UNSCORED_WORDS):
        if re.search(rf"\b{re.escape(w)}\b", name):
            found.add(w)
    return found


def load_tagged(path):
    """
    Rows of a report.csv that name a skillset, with everything --tags needs.

    No join and no online_id: a row already carries both the verdict and the
    tags the verdict should be checked against.
    """
    rows = []
    no_tag_column = True
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if "tags" in row:
                no_tag_column = False
            category = (row.get("category") or "").strip()
            if category not in cm.CATEGORIES:
                continue
            words = tag_words(row)
            if not words:
                continue
            families = {TAG_FAMILIES[w] for w in words if w in TAG_FAMILIES}
            rows.append({
                # A stable identity for the row. Membership tests downstream
                # key on this rather than on the dict itself: `r in agree`
                # compares dicts field by field, including the whole embedded
                # CSV row, which on a library-sized report.csv turns a
                # set-difference into minutes of string comparison.
                "i": len(rows),
                "category": category,
                "words": words,
                "families": families,
                "title": row.get("title", ""),
                "diff_name": row.get("diff_name", ""),
                "path": row.get("path", ""),
                "row": row,
            })
    return rows, no_tag_column


def _f(row, key, default=0.0):
    try:
        return float(row.get(key) or default)
    except (TypeError, ValueError):
        return default


def miss_diagnosis(row, families):
    """
    Why the classifier and the mapper disagree, in the classifier's own terms.

    This is the part that makes a disagreement actionable. A map tagged
    `stream` that we called Bursts because its stream coverage came to 14.2%
    against a 15% floor is a threshold question. The same map with no stream
    run over nine notes is a detection question. They want completely
    different fixes, and the number that separates them is right here in the
    CSV.
    """
    notes = _f(row, "total_note_count")
    gaps = _f(row, "counted_gaps")
    bits = []
    if "stream" in families:
        cov = (_f(row, "stream_note_total") / notes * 100) if notes else 0.0
        bits.append(f"stream coverage {cov:.1f}% (floor "
                    f"{cm.DEFAULT_PARAMS['stream_pct_threshold']:.0f}%)")
        bits.append(f"longest stream {int(_f(row, 'max_stream_len'))}")
    if "jump" in families:
        bits.append(f"jump {_f(row, 'jump_pct'):.1f}% (floor "
                    f"{cm.DEFAULT_PARAMS['jump_pct_threshold']:.0f}%)")
        if gaps < cm.DEFAULT_PARAMS["jump_min_transitions"]:
            bits.append(f"only {int(gaps)} gameplay transitions")
    return ", ".join(bits)


def is_near_miss(row, families):
    """
    Whether a disagreement sits within a whisker of the threshold that
    decided it. Deliberately generous - the point is to separate "the line is
    in the wrong place" from "we did not see it", not to be precise.
    """
    notes = _f(row, "total_note_count")
    if "stream" in families:
        cov = (_f(row, "stream_note_total") / notes * 100) if notes else 0.0
        floor = cm.DEFAULT_PARAMS["stream_pct_threshold"]
        if floor * 0.6 <= cov < floor:
            return True
    if "jump" in families:
        jp = _f(row, "jump_pct")
        floor = cm.DEFAULT_PARAMS["jump_pct_threshold"]
        if floor * 0.6 <= jp < floor:
            return True
        # A map that is 100% jumps and lost only because it is too short to
        # be judged is a threshold question too, not a detection failure -
        # jump_min_transitions is the line, and it is nowhere near it.
        gaps = _f(row, "counted_gaps")
        if jp >= floor and gaps < cm.DEFAULT_PARAMS["jump_min_transitions"]:
            return True
    return False


def tag_report(rows, title):
    """Prints the breakdown and returns the agreement rate over scored rows."""
    print(f"\n=== {title} : mapper tags as a second opinion ===")
    print(f"{len(rows)} difficulties name a skillset in their tags or "
          f"difficulty name\n")

    counts = {}
    for r in rows:
        for w in r["words"]:
            counts.setdefault(w, {c: 0 for c in cm.CATEGORIES})
            counts[w][r["category"]] += 1

    print("what the classifier said, per word (rows summing across the map's "
          "verdicts):")
    print(f"  {'word':13s} {'n':>6s} " +
          " ".join(f"{c[:9]:>10s}" for c in cm.CATEGORIES))
    for w in sorted(counts, key=lambda k: -sum(counts[k].values())):
        tot = sum(counts[w].values())
        if not tot:
            continue
        mark = " " if w in TAG_FAMILIES else "*"
        print(f"  {w:13s}{mark}{tot:5d} " +
              " ".join(f"{counts[w][c] / tot * 100:9.0f}%"
                       for c in cm.CATEGORIES))
    print("  (* = shown but not scored - this tool has no category for it)")

    scored = [r for r in rows if len(r["families"]) == 1]
    both = [r for r in rows if len(r["families"]) == 2]
    agree = [r for r in scored
             if r["category"] in FAMILY_CATEGORIES[next(iter(r["families"]))]]
    rate = len(agree) / len(scored) if scored else 0.0
    print(f"\nagreement on the {len(scored)} rows with exactly one scorable "
          f"family: {len(agree)} ({rate:.1%})")

    # Maps tagged as BOTH should be the ones Hybrid exists for. This is the
    # only outside evidence for that category anywhere in the repo, since no
    # hand-labelled set covers it.
    if both:
        hyb = sum(1 for r in both if r["category"] == "Hybrid")
        base = sum(1 for r in rows if r["category"] == "Hybrid") / len(rows)
        print(f"\nHybrid check - {len(both)} maps are tagged BOTH stream and "
              f"jump, and {hyb} of those ({hyb / len(both):.1%}) came out "
              f"Hybrid,\n              against {base:.1%} across every "
              f"tagged map. Hybrid should be over-represented here; if it is "
              f"not,\n              the category is not measuring what its "
              f"name claims.")
    return rate, scored, agree


def tag_disagreements(scored, agree):
    agreed_ids = {r["i"] for r in agree}
    disagreed = [r for r in scored if r["i"] not in agreed_ids]
    near = [r for r in disagreed if is_near_miss(r["row"], r["families"])]
    near_ids = {r["i"] for r in near}
    hard = [r for r in disagreed if r["i"] not in near_ids]
    print(f"\n{len(disagreed)} disagreements: {len(near)} near the threshold, "
          f"{len(hard)} not close")
    for label, group in (("NEAR the threshold - a line in the wrong place",
                          near),
                         ("NOT close - something was not detected", hard)):
        if not group:
            continue
        print(f"\n  --- {label} ---")
        for r in group[:12]:
            fam = next(iter(r["families"]))
            name = f"{r['title'][:34]} [{r['diff_name'][:20]}]"
            print(f"  {name:58s} tagged {fam:6s} -> {r['category']}")
            diag = miss_diagnosis(r["row"], r["families"])
            if diag:
                print(f"      {diag}")
        if len(group) > 12:
            print(f"      ... and {len(group) - 12} more")
    return disagreed


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True, help="report.csv to score")
    ap.add_argument("--labels", default=None,
                     help="labels.csv: online_id,label - see the notes at the top of this file")
    ap.add_argument("--tags", action="store_true",
                     help="score against the mappers' own Tags: lines instead of a labels.csv. "
                          "Needs no labels and no network, but it is a second opinion rather "
                          "than ground truth - read the disagreements, do not tune to the rate.")
    ap.add_argument("--write-disagreements", default=None, metavar="OUT.CSV",
                     help="with --tags, write every disagreement to a CSV (with paths, so the "
                          "maps behind a disagreement can be opened and looked at)")
    ap.add_argument("--baseline", default=None,
                     help="a second report.csv to compare against, so you can see whether "
                          "a change actually helped")
    args = ap.parse_args()

    if args.tags:
        return tag_main(args)
    if not args.labels:
        print("Either --labels or --tags is required.", file=sys.stderr)
        return 1

    labels = load_labels(args.labels)
    if not labels:
        print(f"No usable labels in {args.labels}.", file=sys.stderr)
        return 1

    preds, missing = load_predictions(args.csv)
    if missing:
        print(f"Note: {missing} rows in {args.csv} have no online_id and can't be "
              f"scored.\n      online_id is only populated by the osu!lazer realm fast path.")

    pairs, stats = score(preds, labels)
    if not pairs:
        print("None of the labelled beatmaps appear in that report.csv.", file=sys.stderr)
        return 1
    current = report(pairs, stats, args.csv)

    if args.baseline:
        base_preds, _ = load_predictions(args.baseline)
        base_pairs, base_stats = score(base_preds, labels)
        baseline = report(base_pairs, base_stats, args.baseline)
        delta = current - baseline
        verdict = "BETTER" if delta > 0.001 else ("WORSE" if delta < -0.001 else "no real change")
        print(f"\n>>> macro F1 {baseline:.3f} -> {current:.3f} ({delta:+.3f}) : {verdict}")
    return 0



def tag_main(args):
    try:
        rows, no_tag_column = load_tagged(args.csv)
    except OSError as e:
        print(f"Can't read {args.csv}: {e}", file=sys.stderr)
        return 1
    if no_tag_column:
        print(f"{args.csv} has no `tags` column - it was written before that "
              f"existed.\nRe-run classify_maps.py to produce a report.csv "
              f"that carries it.", file=sys.stderr)
        return 1
    if not rows:
        print(f"No rows in {args.csv} name a skillset in their tags.",
              file=sys.stderr)
        return 1

    rate, scored, agree = tag_report(rows, args.csv)
    disagreed = tag_disagreements(scored, agree)

    if args.write_disagreements:
        with open(args.write_disagreements, "w", newline="",
                  encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["tagged", "category", "near_threshold", "diagnosis",
                        "title", "diff_name", "path"])
            for r in disagreed:
                w.writerow([next(iter(r["families"])), r["category"],
                            is_near_miss(r["row"], r["families"]),
                            miss_diagnosis(r["row"], r["families"]),
                            r["title"], r["diff_name"], r["path"]])
        print(f"\n{len(disagreed)} disagreements written to "
              f"{args.write_disagreements}")

    if args.baseline:
        base_rows, base_missing = load_tagged(args.baseline)
        if base_missing or not base_rows:
            print(f"\n{args.baseline} carries no usable tags - skipping the "
                  f"comparison.")
            return 0
        base_rate, _, _ = tag_report(base_rows, args.baseline)
        delta = rate - base_rate
        verdict = ("BETTER" if delta > 0.001 else
                   ("WORSE" if delta < -0.001 else "no real change"))
        print(f"\n>>> tag agreement {base_rate:.1%} -> {rate:.1%} "
              f"({delta * 100:+.1f}pp) : {verdict}")
        print("    Treat this as a smoke alarm, not a score. It is built from "
              "what mappers\n    wrote in a search field, and a change that "
              "moves it a fraction of a point\n    has not been shown to "
              "have done anything.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
