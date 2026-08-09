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

This deliberately does NOT scrape osu!'s community beatmap tags. That was
tried and dropped - outside the few hundred most popular maps, almost nothing
carries a usertag, so the coverage is far too thin to tune against and biased
towards popular maps besides.

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


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True, help="report.csv to score")
    ap.add_argument("--labels", required=True, help="labels.csv: online_id,label - see the notes at the top of this file")
    ap.add_argument("--baseline", default=None,
                     help="a second report.csv to compare against, so you can see whether "
                          "a change actually helped")
    args = ap.parse_args()

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


if __name__ == "__main__":
    raise SystemExit(main())
