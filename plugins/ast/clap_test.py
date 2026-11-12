#!/usr/bin/env python3
"""
clap_test.py - standalone CLAP detection test (step 0 of CLAPmark).
Does not touch Earmark.

Run inside the CLAP environment:
    source ~/.earmark/clap-env/bin/activate
    python clap_test.py recording.wav "bird=birdsong|bird call@0.17, motorcycle, human voice@0.18"

Word list syntax (comma-separated items):
    motorcycle                          label = phrase, default thresholds
    human voice@0.18                    own open threshold
    pigeon=pigeon|pigeons#grey          own marker colour in Earmark
    bird=birdsong|bird chirping|bird call@0.17
                                        label "bird", best of three phrasings
Each phrase becomes "the sound of <phrase>" (see --template), unless it is
in quotes: voice="people talking in the background" is sent to CLAP as written.

If a label gets no events, its 3 strongest peaks are marked as weak
candidates ("label?"), since every listed sound occurs at least once (--rescue).

Optional ground truth, to score the result:
    --truth "bird:2.89,6.37,11.8; human voice:15"

Results go to a folder next to the recording, <name>_clap/ :
    events.csv   detected events (start, end, label, scores)
    scores.csv   per-window similarity for every phrase (open in Numbers)
    emb_*.npz    cached audio embeddings - re-runs with new words skip
                 the slow part, as long as --win/--hop stay the same
"""
import os
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")  # CPU fallback for any op MPS lacks
os.environ.setdefault("HF_HUB_OFFLINE", "1")               # model is already cached; no network
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

import argparse
import csv
import json
import math
import sys
import time

import numpy as np

DEFAULT_MODEL = "laion/larger_clap_general"
DEFAULT_BACKGROUND = "ambient noise, wind, quiet room tone, distant traffic"
SR = 48000


# ---------------------------------------------------------------- helpers

def tc(sec):
    """Seconds -> HH:MM:SS.mmm (Earmark marker format)."""
    ms = int(round(max(sec, 0.0) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d.%03d" % (h, m, s, ms)


def parse_list(s):
    return [x.strip() for x in s.split(",") if x.strip()]


COLOR_ALIASES = {"gray": "Grey", "grey": "Grey"}


def parse_labels(s):
    """'bird=birdsong|bird call@0.17#red, motorcycle' -> [(label, [phrases], open_or_None)]
    The optional #color is stored in LABEL_COLORS[label]."""
    out = []
    for item in parse_list(s):
        thr = None
        color = None
        if "#" in item:
            item, col = item.rsplit("#", 1)
            col = col.strip()
            color = COLOR_ALIASES.get(col.lower(), col.capitalize()) if col else None
        if "@" in item:
            item, t = item.rsplit("@", 1)
            try:
                thr = float(t)
            except ValueError:
                sys.exit("Bad threshold in word list: @" + t)
        if "=" in item:
            label, rest = item.split("=", 1)
        else:
            label, rest = item, item
        phrases = [p.strip() for p in rest.split("|") if p.strip()]
        label = label.strip()
        if not label or not phrases:
            sys.exit("Bad item in word list: " + item)
        out.append((label, phrases, thr))
        LABEL_COLORS[label] = color
    return out


LABEL_COLORS = {}


def parse_truth(s):
    """'bird:2.89,6.37; voice:15' -> {label: [times]}"""
    truth = {}
    if not s:
        return truth
    for part in s.split(";"):
        if ":" not in part:
            continue
        label, times = part.split(":", 1)
        truth[label.strip()] = [float(t) for t in times.split(",") if t.strip()]
    return truth


QUOTES = ('"', "'", "\u201c", "\u201d", "\u2018", "\u2019")


def make_prompt(word, template):
    """Quoted phrases are used verbatim; others get the template ("the sound of ...")."""
    if len(word) >= 2 and word[0] in QUOTES and word[-1] in QUOTES:
        return word[1:-1].strip()
    if word.lower().startswith(("the sound of", "sound of", "a sound of")):
        return word
    return template.format(word)


def as_tensor(out):
    """get_*_features returns a tensor in transformers 4.x, an output object in some 5.x builds."""
    import torch
    if torch.is_tensor(out):
        return out
    for attr in ("audio_embeds", "text_embeds", "pooler_output"):
        v = getattr(out, attr, None)
        if v is not None:
            return v
    return out[0]


def rolling_median(x, k):
    """Median filter along axis 0, edge-padded. k is forced odd."""
    if k <= 1 or x.shape[0] < 2:
        return x
    if k % 2 == 0:
        k += 1
    pad = k // 2
    xp = np.pad(x, ((pad, pad), (0, 0)), mode="edge")
    win = np.lib.stride_tricks.sliding_window_view(xp, k, axis=0)
    return np.median(win, axis=-1)


PROGRESS_LINES = False          # --progress-lines: machine-readable progress for Earmark


def use_cache_dir(cache_dir, audio_path, legacy_suffix, names):
    """Make cache_dir the output folder. Analyses already saved in the old per-recording folder
    (<recording><legacy_suffix>/) are copied over once, so nothing has to be analysed again."""
    import shutil
    os.makedirs(cache_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(audio_path))[0]
    legacy = os.path.join(os.path.dirname(audio_path), stem + legacy_suffix)
    for name in names:
        src, dst = os.path.join(legacy, name), os.path.join(cache_dir, name)
        if not os.path.exists(dst) and os.path.isfile(src):
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass
    return cache_dir


# ---------------------------------------------------------------- audio / model

def load_audio(path):
    import librosa
    y, _ = librosa.load(path, sr=SR, mono=True)
    return y.astype(np.float32)


def audio_duration(path):
    import librosa
    try:
        return float(librosa.get_duration(path=path))
    except TypeError:
        return float(librosa.get_duration(filename=path))


def region(path, start, end, hop_s):
    """Align a requested [start, end] to the window grid. Returns (t_begin, t_end, full_duration)."""
    full = audio_duration(path)
    if start is None and end is None:
        return 0.0, full, full
    s0 = max(0.0, start or 0.0)
    e0 = min(full, end if end is not None else full)
    if e0 <= s0:
        sys.exit("Empty region: %.3f-%.3f" % (s0, e0))
    t_begin = math.floor(s0 / hop_s + 1e-9) * hop_s
    return t_begin, e0, full


def load_region(path, sr, t_begin, t_end, full):
    import librosa
    if t_begin <= 0 and t_end >= full:
        y, _ = librosa.load(path, sr=sr, mono=True)
    else:
        y, _ = librosa.load(path, sr=sr, mono=True, offset=t_begin, duration=t_end - t_begin)
    return y.astype(np.float32)


def make_windows(n_samples, win, hop):
    """Start samples of each window; returns (starts, padded_length)."""
    if n_samples <= win:
        return np.array([0]), win
    count = int(math.ceil((n_samples - win) / float(hop))) + 1
    starts = np.arange(count) * hop
    return starts, int(starts[-1] + win)


def embed_audio(model, fe, audio, starts, win, batch, device):
    import torch
    out = []
    n = len(starts)
    t0 = time.time()
    for b in range(0, n, batch):
        chunk_starts = starts[b:b + batch]
        chunks = [audio[s:s + win] for s in chunk_starts]
        feats = fe(chunks, sampling_rate=SR, return_tensors="pt")
        inputs = {k: v.to(device) for k, v in feats.items()}
        with torch.no_grad():
            e = as_tensor(model.get_audio_features(**inputs))
        e = torch.nn.functional.normalize(e.float(), dim=-1)
        out.append(e.cpu().numpy())
        done = min(b + batch, n)
        el = time.time() - t0
        eta = el / done * (n - done)
        if PROGRESS_LINES:
            print("PROGRESS %d %d %.0f ms per window, about %.0f s left" % (done, n, 1000 * el / done, eta), flush=True)
            continue
        sys.stdout.write("\r  embedding windows %d/%d  (%.0fs elapsed, ~%.0fs left)   "
                         % (done, n, el, eta))
        sys.stdout.flush()
    if not PROGRESS_LINES:
        sys.stdout.write("\n")
    return np.concatenate(out, axis=0)


def embed_text(model, tok, prompts, device):
    import torch
    t = tok(prompts, padding=True, return_tensors="pt")
    t = {k: v.to(device) for k, v in t.items()}
    with torch.no_grad():
        e = as_tensor(model.get_text_features(**t))
    e = torch.nn.functional.normalize(e.float(), dim=-1)
    return e.cpu().numpy()


# ---------------------------------------------------------------- detection

def detect(z, raw, starts_s, win_s, hop_s, dur, open_t, close_t, gap_s, min_dur, t_begin=0.0):
    """
    Hysteresis on one label's score curve:
    a run of windows with score >= close_t becomes an event if its peak reaches open_t.
    Event bounds = the central (hop-wide) part of the first/last window,
    which is where windows overlap and the sound most likely sits.
    """
    n = len(z)
    events = []
    i = 0
    while i < n:
        if z[i] >= close_t:
            j = i
            while j + 1 < n and z[j + 1] >= close_t:
                j += 1
            if z[i:j + 1].max() >= open_t:
                c0 = starts_s[i] + win_s / 2.0
                c1 = starts_s[j] + win_s / 2.0
                start = t_begin if i == 0 else c0 - hop_s / 2.0
                end = dur if j == n - 1 else c1 + hop_s / 2.0
                events.append([max(t_begin, start), min(dur, end),
                               float(z[i:j + 1].max()), float(raw[i:j + 1].max())])
            i = j + 1
        else:
            i += 1

    merged = []
    for ev in events:
        if merged and ev[0] - merged[-1][1] <= gap_s:
            last = merged[-1]
            last[1] = max(last[1], ev[1])
            last[2] = max(last[2], ev[2])
            last[3] = max(last[3], ev[3])
        else:
            merged.append(ev)
    return [ev for ev in merged if ev[1] - ev[0] >= min_dur]


def rescue_peaks(curve, starts_s, win_s, dur, n, min_sep):
    """Top-n local maxima of a score curve, at least min_sep apart, as window-wide events."""
    m = len(curve)
    peaks = [i for i in range(m)
             if (i == 0 or curve[i] >= curve[i - 1]) and (i == m - 1 or curve[i] >= curve[i + 1])]
    peaks.sort(key=lambda i: -curve[i])
    chosen = []
    for i in peaks:
        c = starts_s[i] + win_s / 2.0
        if all(abs(c - (starts_s[j] + win_s / 2.0)) >= min_sep for j in chosen):
            chosen.append(i)
        if len(chosen) >= n:
            break
    return [[max(0.0, starts_s[i]), min(dur, starts_s[i] + win_s), float(curve[i])] for i in chosen]


def score_truth(rows, truth, tol):
    """A known start is a hit if an event of that label contains it or starts within tol of it."""
    print("\nAgainst known start times (tolerance %gs):" % tol)
    for label, times in truth.items():
        evs = [r for r in rows if r[2] == label]
        used = set()
        hits = 0
        for t in times:
            found = None
            for k, r in enumerate(evs):
                if (r[0] - tol) <= t <= r[1] or abs(r[0] - t) <= tol:
                    found = k
                    break
            if found is None:
                print("  %-14s %8.2fs  MISSED" % (label, t))
            else:
                hits += 1
                used.add(found)
                r = evs[found]
                print("  %-14s %8.2fs  hit   (event %s-%s%s)" % (label, t, tc(r[0]), tc(r[1]),
                                                                ", weak" if r[5] else ""))
        extra = [r for k, r in enumerate(evs) if k not in used]
        print("  %-14s %d/%d found, %d other event(s)%s" % (
            label, hits, len(times), len(extra),
            "".join("\n      other: %s-%s" % (tc(r[0]), tc(r[1])) for r in extra)))


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="CLAP sound-event test on one recording.")
    ap.add_argument("audio", help="audio file (wav, aiff, flac...)")
    ap.add_argument("words", help='e.g. "bird=birdsong|bird call@0.17, motorcycle, human voice@0.18"')
    ap.add_argument("--win", type=float, default=2.0, help="window length in s (default 2)")
    ap.add_argument("--hop", type=float, default=1.0, help="hop in s (default 1 = 50%% overlap)")
    ap.add_argument("--mode", choices=["absolute", "relative"], default="absolute",
                    help="absolute = raw similarity (default); relative = old z-score vs rest of file")
    ap.add_argument("--open", type=float, default=None,
                    help="score needed to start an event (default 0.22 absolute, 2.5 relative)")
    ap.add_argument("--close", type=float, default=None,
                    help="score below which an event ends (default 0.17 absolute, 1.5 relative)")
    ap.add_argument("--smooth", type=int, default=1, help="median smoothing in windows (default 1 = off)")
    ap.add_argument("--gap", type=float, default=0.5, help="merge same-label events closer than this, s")
    ap.add_argument("--min-dur", type=float, default=0.5, help="drop events shorter than this, s")
    ap.add_argument("--template", default="the sound of {}", help='prompt template (default "the sound of {}")')
    ap.add_argument("--background", default=None,
                    help="comma-separated reference prompts (relative mode only)")
    ap.add_argument("--truth", default=None, help='known start times, e.g. "bird:2.89,6.37; owl:40"')
    ap.add_argument("--tol", type=float, default=1.5, help="tolerance for --truth matching, s")
    ap.add_argument("--rescue", type=int, default=3,
                    help="if a label gets no events, mark its N strongest peaks as weak candidates (0 = off)")
    ap.add_argument("--json", default=None, help="also write the events as JSON to this path (used by Earmark)")
    ap.add_argument("--cache", default=None, help="folder for saved analyses (default: <recording>_clap/)")
    ap.add_argument("--progress-lines", action="store_true", help="print PROGRESS lines (used by Earmark)")
    ap.add_argument("--start", type=float, default=None, help="only analyse from this time (s)")
    ap.add_argument("--end", type=float, default=None, help="only analyse up to this time (s)")
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--cpu", action="store_true", help="force CPU instead of the Mac GPU")
    ap.add_argument("--no-cache", action="store_true", help="recompute embeddings")
    args = ap.parse_args()

    relative = args.mode == "relative"
    open_t = args.open if args.open is not None else (2.5 if relative else 0.22)
    close_t = args.close if args.close is not None else (1.5 if relative else 0.17)
    offset = open_t - close_t

    path = os.path.abspath(os.path.expanduser(args.audio))
    if not os.path.isfile(path):
        sys.exit("File not found: " + path)
    labels = parse_labels(args.words)
    if not labels:
        sys.exit("No words given.")
    truth = parse_truth(args.truth)

    phrases = []      # all target phrases, flat
    owner = []        # label index of each phrase
    for li, (_, ph, _) in enumerate(labels):
        for p in ph:
            phrases.append(make_prompt(p, args.template))
            owner.append(li)
    background = []
    if relative:
        background = parse_list(args.background if args.background is not None else DEFAULT_BACKGROUND)
    prompts = phrases + background
    nphr = len(phrases)

    global PROGRESS_LINES
    PROGRESS_LINES = args.progress_lines
    stem = os.path.splitext(os.path.basename(path))[0]
    outdir = os.path.join(os.path.dirname(path), stem + "_clap")
    if not args.cache:
        os.makedirs(outdir, exist_ok=True)

    t_all = time.time()
    import torch
    from transformers import ClapModel, ClapProcessor

    device = "mps" if (not args.cpu and torch.backends.mps.is_available()) else "cpu"
    print("Device: " + device)

    t = time.time()
    try:
        model = ClapModel.from_pretrained(args.model).to(device).eval()
        proc = ClapProcessor.from_pretrained(args.model)
    except Exception as e:
        sys.exit("Could not load model %s from the local cache: %s\n"
                 "If it was never downloaded, run once with HF_HUB_OFFLINE=0." % (args.model, e))
    print("Model loaded in %.1fs" % (time.time() - t))

    t = time.time()
    t_begin, t_end, full_dur = region(path, args.start, args.end, args.hop)
    is_region = not (t_begin <= 0 and t_end >= full_dur)
    audio = load_region(path, SR, t_begin, t_end, full_dur)
    n_samples = len(audio)
    dur = t_begin + n_samples / float(SR)          # absolute end of the analysed part
    print("Audio: %s  (%s%s, loaded and resampled in %.1fs)" % (
        os.path.basename(path), tc(full_dur),
        ("; analysing %s-%s" % (tc(t_begin), tc(dur))) if is_region else "", time.time() - t))

    win = int(round(args.win * SR))
    hop = int(round(args.hop * SR))
    starts, padded = make_windows(n_samples, win, hop)
    if padded > n_samples:
        audio = np.concatenate([audio, np.zeros(padded - n_samples, dtype=np.float32)])
    starts_s = t_begin + starts / float(SR)

    model_tag = args.model.split("/")[-1]
    full_name = "emb_%s_w%g_h%g.npz" % (model_tag, args.win, args.hop)
    region_name = "emb_%s_w%g_h%g_s%g_e%g.npz" % (model_tag, args.win, args.hop, t_begin, round(dur, 3))
    if args.cache:
        outdir = use_cache_dir(args.cache, path, "_clap", [full_name, region_name])
    full_cache = os.path.join(outdir, full_name)
    cache = full_cache if not is_region else os.path.join(outdir, region_name)
    A = None
    if not args.no_cache and os.path.isfile(cache):
        c = np.load(cache)
        if (is_region or int(c["n_samples"]) == n_samples) and c["emb"].shape[0] == len(starts):
            A = c["emb"]
            print("Using cached embeddings (%d windows)" % len(starts))
    if A is None and is_region and not args.no_cache and os.path.isfile(full_cache):
        c = np.load(full_cache)                     # whole-file cache: take the region's windows from it
        i0 = int(round(t_begin / args.hop))
        if c["emb"].shape[0] >= i0 + len(starts):
            A = c["emb"][i0:i0 + len(starts)]
            print("Using cached embeddings from the whole-file run (%d windows)" % len(starts))
    if A is None:
        print("Embedding %d windows of %gs, hop %gs..." % (len(starts), args.win, args.hop))
        t = time.time()
        A = embed_audio(model, proc.feature_extractor, audio, starts, win, args.batch, device)
        t_emb = time.time() - t
        np.savez(cache, emb=A, n_samples=n_samples)
        print("Embedding took %.1fs (%.0f ms per window)" % (t_emb, 1000 * t_emb / len(starts)))

    T = embed_text(model, proc.tokenizer, prompts, device)

    # ---- scoring
    sims = A @ T.T                                   # windows x prompts, cosine similarity
    owner = np.array(owner)
    raw = np.stack([sims[:, :nphr][:, owner == li].max(axis=1)   # best phrasing per label
                    for li in range(len(labels))], axis=1)
    if relative:
        rel = raw - sims[:, nphr:].mean(axis=1, keepdims=True) if background else raw
        med = np.median(rel, axis=0)
        mad = np.median(np.abs(rel - med), axis=0) * 1.4826
        score = (rel - med) / np.maximum(mad, 1e-6)
    else:
        score = raw.copy()
    score = rolling_median(score, args.smooth)

    # ---- detection
    rows = []
    for li, (label, _, thr) in enumerate(labels):
        o = thr if thr is not None else open_t
        cl = o - offset
        found = detect(score[:, li], raw[:, li], starts_s, args.win, args.hop, dur,
                       o, cl, args.gap, args.min_dur, t_begin)
        for ev in found:
            rows.append([ev[0], ev[1], label, ev[2], ev[3], False])
        if not found and args.rescue > 0:
            for ev in rescue_peaks(score[:, li], starts_s, args.win, dur, args.rescue, max(1.0, args.win)):
                rows.append([ev[0], ev[1], label, ev[2], float(raw[:, li].max()), True])
    rows.sort(key=lambda r: r[0])

    # ---- write files
    ev_path = os.path.join(outdir, "events.csv")
    with open(ev_path, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["start_s", "end_s", "start", "end", "label", "peak_score", "peak_similarity", "weak"])
        for r in rows:
            wr.writerow(["%.3f" % r[0], "%.3f" % r[1], tc(r[0]), tc(r[1]), r[2], "%.3f" % r[3], "%.3f" % r[4],
                         "yes" if r[5] else ""])

    sc_path = os.path.join(outdir, "scores.csv")
    with open(sc_path, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["window_center_s", "window_center"]
                    + ["score: " + l[0] for l in labels]
                    + ["similarity: " + p for p in prompts])
        for i in range(len(starts)):
            c = starts_s[i] + args.win / 2.0
            wr.writerow(["%.3f" % c, tc(c)]
                        + ["%.3f" % v for v in score[i]]
                        + ["%.4f" % v for v in sims[i]])

    # ---- report
    print("\nMode: %s, open %s, close %s" % (args.mode, open_t, close_t))
    print("Phrase overview (raw similarity across the file):")
    print("  %-40s %8s %8s" % ("phrase", "median", "max"))
    for k, p in enumerate(prompts):
        col = sims[:, k]
        tag = "[bg] " if k >= nphr else ""
        print("  %-40s %8.3f %8.3f" % ((tag + p)[:40], np.median(col), col.max()))

    print("\nEvents (%d):" % len(rows))
    for r in rows:
        print("  %s  %s  %-16s score %6.3f" % (tc(r[0]), tc(r[1]), r[2] + ("?" if r[5] else ""), r[3]))

    if truth:
        score_truth(rows, truth, args.tol)

    if args.json:
        with open(args.json, "w") as f:
            json.dump({"duration": full_dur,
                       "labels": [{"label": l[0], "color": LABEL_COLORS.get(l[0])} for l in labels],
                       "scores": {"win": args.win, "hop": args.hop, "gap": args.gap, "min_dur": args.min_dur,
                                  "offset": offset, "n": int(len(starts)), "t0": t_begin, "t_end": dur,
                                  "labels": [{"label": l[0],
                                              "open": l[2] if l[2] is not None else open_t,
                                              "values": [round(float(v), 4) for v in score[:, k]]}
                                             for k, l in enumerate(labels)]},
                       "events": [
                {"start": round(r[0], 3), "end": round(r[1], 3), "label": r[2],
                 "score": round(r[3], 4), "similarity": round(r[4], 4), "weak": bool(r[5])}
                for r in rows]}, f)

    print("\nFiles:\n  %s\n  %s" % (ev_path, sc_path))
    print("Total time %.1fs" % (time.time() - t_all))


if __name__ == "__main__":
    main()
