#!/usr/bin/env python3
"""
ast_test.py - "what do you hear?" with AST (Audio Spectrogram Transformer,
fine-tuned on AudioSet). Separate test script; does not touch Earmark.

Unlike CLAP, AST outputs a probability (0-1) for each of 527 fixed AudioSet
sound categories, in every window. So it can say what it hears without being
asked about a specific sound, and a probability means the same for every class.

Run inside the CLAP environment (same libraries):
    source ~/.earmark/clap-env/bin/activate

    # Discover: what is in the file?
    python ast_test.py recording.wav

    # Look up AudioSet category names
    python ast_test.py --find "bird"

    # Search only some categories (separate with ;  - AudioSet names contain commas)
    python ast_test.py recording.wav --classes "voice=Speech|Conversation; pigeon=Pigeon, dove"
    python ast_test.py recording.wav --classes "bird=Bird vocalization, bird call, bird song|Chirp, tweet" --truth "bird:2.89,6.37"

First run downloads the model once (~350 MB) into ~/.cache/huggingface.
Results go to <name>_ast/ next to the recording: events.csv, summary.csv,
and a probability cache so re-runs with other settings take seconds.
"""
import os
os.environ["HF_HUB_OFFLINE"] = "0"          # allow the one-time download (loads try local files first)
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

import argparse
import csv
import json
import sys
import time

import numpy as np

import clap_test
from clap_test import (tc, rolling_median, detect, make_windows, parse_truth, score_truth,
                       rescue_peaks, region, load_region, COLOR_ALIASES, use_cache_dir)

MODEL = "MIT/ast-finetuned-audioset-10-10-0.4593"
SR = 16000


def load_model(name, device):
    from transformers import ASTFeatureExtractor, ASTForAudioClassification
    try:
        fe = ASTFeatureExtractor.from_pretrained(name, local_files_only=True)
        model = ASTForAudioClassification.from_pretrained(name, local_files_only=True)
    except Exception:
        print("STATUS Downloading the AST model (first time only, about 350 MB)..." if clap_test.PROGRESS_LINES
              else "Downloading AST model (first run only, ~350 MB)...", flush=True)
        fe = ASTFeatureExtractor.from_pretrained(name)
        model = ASTForAudioClassification.from_pretrained(name)
    return fe, model.to(device).eval()


def class_names(model):
    id2label = model.config.id2label
    return [id2label[i] if i in id2label else id2label[str(i)] for i in range(len(id2label))]


def compute_probs(fe, model, audio, starts, win, batch, device):
    import torch
    out = []
    n = len(starts)
    t0 = time.time()
    for b in range(0, n, batch):
        chunks = [audio[s:s + win] for s in starts[b:b + batch]]
        feats = fe(chunks, sampling_rate=SR, return_tensors="pt")
        with torch.no_grad():
            logits = model(input_values=feats["input_values"].to(device)).logits
        out.append(torch.sigmoid(logits.float()).cpu().numpy())
        done = min(b + batch, n)
        el = time.time() - t0
        if clap_test.PROGRESS_LINES:
            print("PROGRESS %d %d %.0f ms per window, about %.0f s left" % (done, n, 1000 * el / done,
                                                                             el / done * (n - done)), flush=True)
            continue
        sys.stdout.write("\r  analysing windows %d/%d  (%.0fs elapsed, ~%.0fs left)   "
                         % (done, n, el, el / done * (n - done)))
        sys.stdout.flush()
    if not clap_test.PROGRESS_LINES:
        sys.stdout.write("\n")
    return np.concatenate(out, axis=0)


LABEL_COLORS = {}


# Broad AudioSet categories: ranked after specific ones in discover mode, so that e.g. "Owl"
# is kept and "Bird" / "Animal" covering the same moments is dropped as a duplicate.
GENERIC = set([
    "Animal", "Domestic animals, pets", "Livestock, farm animals, working animals", "Wild animals",
    "Bird", "Bird vocalization, bird call, bird song", "Insect", "Vehicle", "Motor vehicle (road)",
    "Music", "Musical instrument", "Human voice", "Speech", "Human sounds", "Noise", "Environmental noise",
    "Sound effect", "Field recording", "Silence", "Inside, small room", "Inside, large room or hall",
    "Inside, public space", "Outside, urban or manmade", "Outside, rural or natural", "Natural sounds",
    "Water", "Wind", "Mechanisms", "Tools", "Domestic sounds, home sounds", "Liquid", "Wood", "Glass",
    "Generic impact sounds", "Surface contact", "Engine", "Vibration", "Echo", "Reverberation",
])


def discover_pick(names, S, open_t, close_t, top, dedup):
    """Strongest categories first (peak probability; a single strong moment is enough), broad categories
    after specific ones; a category whose above-threshold windows are nearly the same as those of an
    already kept one (overlap / union >= dedup) is a duplicate (Owl / Hoot) and is dropped. A short
    sound inside a long one (a bark during a stream) is not a duplicate. Returns (kept, dropped) where dropped is
    [(index, index_it_duplicates)]."""
    peak = S.max(axis=0)
    cands = [k for k in range(len(names)) if peak[k] >= open_t]
    cands.sort(key=lambda k: (names[k] in GENERIC, -peak[k]))
    masks = S >= close_t
    kept, dropped = [], []
    for k in cands:
        dup = None
        for j in kept:
            inter = np.logical_and(masks[:, k], masks[:, j]).sum()
            union = np.logical_or(masks[:, k], masks[:, j]).sum()
            if union and inter / float(union) >= dedup:        # nearly the same moments, both ways
                dup = j
                break
        if dup is None:
            kept.append(k)
            if len(kept) >= top:
                break
        else:
            dropped.append((k, dup))
    return kept, dropped


def parse_classes(s, names):
    """'voice=Speech|Conversation@0.5#red; pigeon=Pigeon, dove' -> [(label, [class indices], open_or_None)]"""
    lower = {n.lower(): i for i, n in enumerate(names)}
    out = []
    for item in [x.strip() for x in s.split(";") if x.strip()]:
        thr = None
        color = None
        if "#" in item:
            item, col = item.rsplit("#", 1)
            col = col.strip()
            color = COLOR_ALIASES.get(col.lower(), col.capitalize()) if col else None
        if "@" in item:
            item, t = item.rsplit("@", 1)
            thr = float(t)
        label, rest = item.split("=", 1) if "=" in item else (item, item)
        idx = []
        for c in [x.strip() for x in rest.split("|") if x.strip()]:
            if c.lower() in lower:
                idx.append(lower[c.lower()])
            else:
                near = [n for n in names if c.lower() in n.lower()]
                sys.exit('"%s" is not an AudioSet category.%s' % (
                    c, (" Did you mean: " + " / ".join(near[:8])) if near else " Try --find."))
        out.append((label.strip(), idx, thr))
        LABEL_COLORS[label.strip()] = color
    return out


def main():
    ap = argparse.ArgumentParser(description="AST AudioSet tagging test on one recording.")
    ap.add_argument("audio", nargs="?", help="audio file")
    ap.add_argument("--find", default=None, help="list AudioSet categories containing this text, then exit")
    ap.add_argument("--classes", default=None,
                    help='only these categories, ";"-separated, e.g. "voice=Speech|Conversation; Pigeon, dove"')
    ap.add_argument("--win", type=float, default=1.0, help="window length in s (default 1)")
    ap.add_argument("--hop", type=float, default=0.5, help="hop in s (default 0.5)")
    ap.add_argument("--open", type=float, default=0.25, help="probability that starts an event (default 0.25)")
    ap.add_argument("--close", type=float, default=0.12, help="probability below which it ends (default 0.12)")
    ap.add_argument("--smooth", type=int, default=1, help="median smoothing in windows (default off)")
    ap.add_argument("--gap", type=float, default=0.5, help="merge same-label events closer than this, s")
    ap.add_argument("--min-dur", type=float, default=0.5, help="drop events shorter than this, s")
    ap.add_argument("--top", type=int, default=30, help="discover mode: show this many categories")
    ap.add_argument("--truth", default=None, help='known start times per label, e.g. "voice:20.8,50.6"')
    ap.add_argument("--tol", type=float, default=1.5)
    ap.add_argument("--json", default=None, help="also write events as JSON (same format as clap_test.py)")
    ap.add_argument("--json-top", type=int, default=8, help="discover mode: categories written to JSON (default 8)")
    ap.add_argument("--dedup", type=float, default=0.5,
                    help="discover mode: drop a category whose moments overlap a stronger one by this much (overlap/union)")
    ap.add_argument("--rescue", type=int, default=3, help="with --classes: weak candidates for labels with no events")
    ap.add_argument("--cache", default=None, help="folder for saved analyses (default: <recording>_ast/)")
    ap.add_argument("--progress-lines", action="store_true", help="print PROGRESS lines (used by Earmark)")
    ap.add_argument("--start", type=float, default=None, help="only analyse from this time (s)")
    ap.add_argument("--end", type=float, default=None, help="only analyse up to this time (s)")
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--cpu", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()
    clap_test.PROGRESS_LINES = args.progress_lines

    import torch
    device = "mps" if (not args.cpu and torch.backends.mps.is_available()) else "cpu"
    t_all = time.time()
    fe, model = load_model(MODEL, device)
    names = class_names(model)

    if args.find is not None:
        hits = [n for n in names if args.find.lower() in n.lower()]
        print("\n".join(hits) if hits else "No AudioSet category contains '%s'." % args.find)
        return
    if not args.audio:
        sys.exit("Give an audio file (or --find TEXT).")

    path = os.path.abspath(os.path.expanduser(args.audio))
    if not os.path.isfile(path):
        sys.exit("File not found: " + path)
    stem = os.path.splitext(os.path.basename(path))[0]
    outdir = os.path.join(os.path.dirname(path), stem + "_ast")
    if not args.cache:
        os.makedirs(outdir, exist_ok=True)
    print("Device: %s   model loaded in %.1fs" % (device, time.time() - t_all))

    t = time.time()
    t_begin, t_end, full_dur = region(path, args.start, args.end, args.hop)
    is_region = not (t_begin <= 0 and t_end >= full_dur)
    audio = load_region(path, SR, t_begin, t_end, full_dur)
    n_samples = len(audio)
    dur = t_begin + n_samples / float(SR)
    print("Audio: %s  (%s%s, loaded in %.1fs)" % (
        os.path.basename(path), tc(full_dur),
        ("; analysing %s-%s" % (tc(t_begin), tc(dur))) if is_region else "", time.time() - t))

    win = int(round(args.win * SR))
    hop = int(round(args.hop * SR))
    starts, padded = make_windows(n_samples, win, hop)
    if padded > n_samples:
        audio = np.concatenate([audio, np.zeros(padded - n_samples, dtype=np.float32)])
    starts_s = t_begin + starts / float(SR)

    full_name = "probs_w%g_h%g.npz" % (args.win, args.hop)
    region_name = "probs_w%g_h%g_s%g_e%g.npz" % (args.win, args.hop, t_begin, round(dur, 3))
    if args.cache:
        outdir = use_cache_dir(args.cache, path, "_ast", [full_name, region_name])
    full_cache = os.path.join(outdir, full_name)
    cache = full_cache if not is_region else os.path.join(outdir, region_name)
    P = None
    if not args.no_cache and os.path.isfile(cache):
        c = np.load(cache)
        if (is_region or int(c["n_samples"]) == n_samples) and c["probs"].shape[0] == len(starts):
            P = c["probs"]
            print("Using cached probabilities (%d windows)" % len(starts))
    if P is None and is_region and not args.no_cache and os.path.isfile(full_cache):
        c = np.load(full_cache)
        i0 = int(round(t_begin / args.hop))
        if c["probs"].shape[0] >= i0 + len(starts):
            P = c["probs"][i0:i0 + len(starts)]
            print("Using cached probabilities from the whole-file run (%d windows)" % len(starts))
    if P is None:
        print("Analysing %d windows of %gs, hop %gs..." % (len(starts), args.win, args.hop))
        t = time.time()
        P = compute_probs(fe, model, audio, starts, win, args.batch, device)
        t_an = time.time() - t
        np.savez(cache, probs=P, n_samples=n_samples)
        print("Analysis took %.1fs (%.0f ms per window)" % (t_an, 1000 * t_an / len(starts)))

    # labels to examine: chosen ones, or every category (discover mode)
    if args.classes:
        labels = parse_classes(args.classes, names)
        print("\nCategories searched:")
        for lab, idx, thr in labels:
            print("  %-12s = %s" % (lab, " | ".join(names[i] for i in idx)))
    else:
        labels = [(n, [i], None) for i, n in enumerate(names)]

    S = np.stack([P[:, idx].max(axis=1) for _, idx, _ in labels], axis=1)
    S = rolling_median(S, args.smooth)
    offset = args.open - args.close

    rows = []
    summary = []
    for k, (lab, idx, thr) in enumerate(labels):
        o = thr if thr is not None else args.open
        evs = detect(S[:, k], S[:, k], starts_s, args.win, args.hop, dur, o, o - offset, args.gap, args.min_dur,
                     t_begin)
        for ev in evs:
            rows.append([ev[0], ev[1], lab, ev[2], ev[3], False])
        if not evs and args.classes and args.rescue > 0:
            for ev in rescue_peaks(S[:, k], starts_s, args.win, dur, args.rescue, max(1.0, args.win)):
                rows.append([ev[0], ev[1], lab, ev[2], ev[2], True])
        total = sum(e[1] - e[0] for e in evs)
        summary.append((lab, len(evs), total, float(S[:, k].max()), float(np.median(S[:, k]))))
    rows.sort(key=lambda r: r[0])

    with open(os.path.join(outdir, "events.csv"), "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["start_s", "end_s", "start", "end", "label", "peak_probability", "weak"])
        for r in rows:
            wr.writerow(["%.3f" % r[0], "%.3f" % r[1], tc(r[0]), tc(r[1]), r[2], "%.3f" % r[3],
                         "yes" if r[5] else ""])
    found = sorted([s for s in summary if s[1] > 0], key=lambda s: -s[2])
    with open(os.path.join(outdir, "summary.csv"), "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["label", "events", "total_seconds", "share_of_file", "peak_probability", "median_probability"])
        for s in sorted(summary, key=lambda s: (-s[2], -s[3])):
            wr.writerow([s[0], s[1], "%.1f" % s[2], "%.3f" % (s[2] / dur), "%.3f" % s[3], "%.3f" % s[4]])

    if args.classes:
        print("\nEvents (%d):" % len(rows))
        for r in rows:
            print("  %s  %s  %-14s p=%.2f" % (tc(r[0]), tc(r[1]), r[2] + ("?" if r[5] else ""), r[3]))
        print("\nPer label:")
        for s in summary:
            print("  %-14s %3d events  %6.1fs  peak %.2f  median %.2f" % (s[0], s[1], s[2], s[3], s[4]))
    else:
        kept, dropped = discover_pick([l[0] for l in labels], S, args.open, args.open - offset,
                                      args.top, args.dedup)
        by = dict((s[0], s) for s in summary)
        print("\nWhat AST hears most strongly (peak p >= %.2f; broad categories last):" % args.open)
        print("  %-42s %6s %7s %8s" % ("category", "peak", "events", "seconds"))
        for k in kept:
            s_ = by[labels[k][0]]
            print("  %-42s %6.2f %7d %8.1f%s" % (s_[0][:42], s_[3], s_[1], s_[2],
                                                 "   (broad)" if s_[0] in GENERIC else ""))
        if dropped:
            print("\nDropped as duplicates (same moments as a stronger category):")
            for k, j in dropped:
                print("  %-42s -> same as %s" % (labels[k][0][:42], labels[j][0]))

    if args.truth:
        score_truth(rows, parse_truth(args.truth), args.tol)

    if args.json:
        if args.classes:
            keep = list(range(len(labels)))
        else:                                   # discover: strongest distinct categories
            keep, _ = discover_pick([l[0] for l in labels], S, args.open, args.open - offset,
                                    args.json_top, args.dedup)
        names_kept = set(labels[k][0] for k in keep)
        with open(args.json, "w") as f:
            json.dump({"duration": full_dur,
                       "labels": [{"label": labels[k][0], "color": LABEL_COLORS.get(labels[k][0])} for k in keep],
                       "scores": {"win": args.win, "hop": args.hop, "gap": args.gap, "min_dur": args.min_dur,
                                  "offset": offset, "n": int(len(starts)), "t0": t_begin, "t_end": dur,
                                  "labels": [{"label": labels[k][0],
                                              "open": labels[k][2] if labels[k][2] is not None else args.open,
                                              "values": [round(float(v), 4) for v in S[:, k]]} for k in keep]},
                       "events": [{"start": round(r[0], 3), "end": round(r[1], 3), "label": r[2],
                                   "score": round(r[3], 4), "similarity": round(r[4], 4), "weak": bool(r[5])}
                                  for r in rows if r[2] in names_kept]}, f)

    print("\nFiles in: %s" % outdir)
    print("Total time %.1fs" % (time.time() - t_all))


if __name__ == "__main__":
    main()
