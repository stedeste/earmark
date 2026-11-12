#!/usr/bin/env python3
"""
Fingerprint plugin for Earmark (plugin contract v1, "find similar" only).

Finds near-identical repeats of a sound - the same call, bell, engine, door -
by comparing the shape of its spectrum over time (MFCC-style features), with no
AI model. Needs only numpy; runs in Earmark's own Python.

  run.py --audio FILE --json OUT --query LABEL --example START:END [--example ...]
         [--cache DIR] [--hop 0.05]

Audio: WAV read directly (PCM 8/16/24/32-bit, float, extensible); other formats
are decoded with ffmpeg if it is installed.
"""
import argparse
import json
import math
import os
import shutil
import struct
import subprocess
import sys

import numpy as np

N_MELS, N_COEF, FMIN, FMAX = 40, 20, 60.0, 12000.0
FRAME_S = 0.032                     # analysis frame ~32 ms, hop a quarter of it


def progress(done, total, msg=""):
    print("PROGRESS %d %d %s" % (done, total, msg), flush=True)


# ---------------------------------------------------------------- audio
def read_wav_mono(path):
    with open(path, "rb") as f:
        head = f.read(12)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            return None
        fmt = None
        while True:
            ch = f.read(8)
            if len(ch) < 8:
                return None
            cid, size = ch[:4], struct.unpack("<I", ch[4:])[0]
            if cid == b"fmt ":
                d = f.read(size)
                tag, nch, rate = struct.unpack("<HHI", d[:8])
                bits = struct.unpack("<H", d[14:16])[0]
                if tag == 0xFFFE and len(d) >= 26:
                    tag = struct.unpack("<H", d[24:26])[0]
                fmt = (tag, nch, rate, bits)
                if size & 1:
                    f.seek(1, 1)
            elif cid == b"data" and fmt:
                tag, nch, rate, bits = fmt
                raw = f.read(size)
                break
            else:
                f.seek(size + (size & 1), 1)
    width = bits // 8
    n = len(raw) // (width * nch)
    raw = raw[:n * width * nch]
    if tag == 3:
        x = np.frombuffer(raw, dtype="<f4" if bits == 32 else "<f8").astype(np.float32)
    elif width == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        x = ((b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)) << 8 >> 8).astype(np.float32) / 8388608.0
    elif width == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        return None
    return x.reshape(-1, nch).mean(axis=1), rate


def read_any_mono(path):
    if os.path.splitext(path)[1].lower() in (".wav", ".wave", ".bwf"):
        r = read_wav_mono(path)
        if r is not None:
            return r
    exe = shutil.which("ffmpeg") or next((p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")
                                          if os.path.exists(p)), None)
    if not exe:
        print("ERROR Fingerprint reads WAV directly; other formats need ffmpeg installed.", flush=True)
        sys.exit(2)
    out = subprocess.run([exe, "-v", "error", "-i", path, "-ac", "1", "-ar", "22050", "-f", "f32le", "-"],
                         capture_output=True).stdout
    return np.frombuffer(out, dtype="<f4").astype(np.float32), 22050


# ---------------------------------------------------------------- features
def mel_filters(sr, n_fft):
    def hz2mel(h): return 2595.0 * np.log10(1.0 + h / 700.0)
    def mel2hz(m): return 700.0 * (10 ** (m / 2595.0) - 1.0)
    fmax = min(FMAX, sr / 2.0)
    pts = mel2hz(np.linspace(hz2mel(FMIN), hz2mel(fmax), N_MELS + 2))
    bins = np.fft.rfftfreq(n_fft, 1.0 / sr)
    fb = np.zeros((N_MELS, len(bins)), dtype=np.float32)
    for i in range(N_MELS):
        l, c, r = pts[i], pts[i + 1], pts[i + 2]
        fb[i] = np.clip(np.minimum((bins - l) / (c - l), (r - bins) / (r - c)), 0, None)
    return fb


def features(x, sr):
    """Per-frame MFCC-style vectors (loudness removed), normalised per file, unit length."""
    n_fft = 1 << int(math.ceil(math.log2(FRAME_S * sr)))
    hop = n_fft // 4
    if len(x) < n_fft:
        x = np.pad(x, (0, n_fft - len(x)))
    n = 1 + (len(x) - n_fft) // hop
    win = np.hanning(n_fft).astype(np.float32)
    fb = mel_filters(sr, n_fft)
    k = np.arange(N_MELS)
    dct = np.cos(np.pi / N_MELS * (k[None, :] + 0.5) * np.arange(N_COEF)[:, None]).astype(np.float32)
    out = np.empty((n, N_COEF - 1), dtype=np.float32)
    step = 4096
    for b in range(0, n, step):
        idx = np.arange(b, min(n, b + step))
        frames = np.lib.stride_tricks.as_strided(x[idx[0] * hop:], shape=(len(idx), n_fft),
                                                 strides=(x.strides[0] * hop, x.strides[0]))
        spec = np.abs(np.fft.rfft(frames * win, axis=1)) ** 2
        mel = np.log10(spec @ fb.T + 1e-10)
        out[idx] = (mel @ dct.T)[:, 1:]                     # drop c0 = overall loudness
        progress(min(n, b + step), n, "fingerprinting")
    out -= out.mean(axis=0)
    out /= out.std(axis=0) + 1e-6
    out /= np.linalg.norm(out, axis=1, keepdims=True) + 1e-9
    return out, hop / float(sr)


def similarity(F, a, b, max_len=300):
    """Mean frame-by-frame cosine between the template F[a:b] and every position of F."""
    T = F[a:b]
    stride = max(1, int(math.ceil(len(T) / float(max_len))))
    T = T[::stride]
    L, N = len(T), len(F)
    if L == 0 or N < L * stride:
        return None
    S = F @ T.T                                              # N x L
    m = N - (L - 1) * stride
    acc = np.zeros(m, dtype=np.float32)
    for j in range(L):
        acc += S[j * stride:j * stride + m, j]
    return acc / L


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--json", required=True)
    ap.add_argument("--query", default="similar")
    ap.add_argument("--example", action="append", default=[])
    ap.add_argument("--cache", default=None)
    ap.add_argument("--win", type=float, default=None)
    ap.add_argument("--hop", type=float, default=0.05)
    ap.add_argument("--start", type=float, default=None)
    ap.add_argument("--end", type=float, default=None)
    ap.add_argument("--opt", action="append", default=[])
    a = ap.parse_args()
    examples = []
    for e in a.example:
        s, t = e.split(":")
        if float(t) > float(s):
            examples.append((float(s), float(t)))
    if not examples:
        print("ERROR Fingerprint needs at least one example (a selected marker or range).", flush=True)
        sys.exit(2)

    feats = None
    cf = os.path.join(a.cache, "fingerprint_v1.npz") if a.cache else None
    if cf and os.path.isfile(cf):
        c = np.load(cf)
        feats, fhop, dur = c["F"], float(c["fhop"]), float(c["dur"])
        print("STATUS Using saved fingerprints (cached)", flush=True)
    if feats is None:
        print("STATUS Reading audio...", flush=True)
        x, sr = read_any_mono(a.audio)
        dur = len(x) / float(sr)
        feats, fhop = features(x, sr)
        if cf:
            os.makedirs(a.cache, exist_ok=True)
            np.savez(cf, F=feats, fhop=fhop, dur=dur)

    curves, lengths = [], []
    for s, t in examples:
        fa, fb = int(round(s / fhop)), max(int(round(s / fhop)) + 1, int(round(t / fhop)))
        c = similarity(feats, fa, min(fb, len(feats)))
        if c is not None:
            curves.append(c)
            lengths.append((fb - fa) * fhop)
    if not curves:
        print("ERROR The example is longer than the file or empty.", flush=True)
        sys.exit(2)
    m = min(len(c) for c in curves)
    curve = np.max(np.stack([c[:m] for c in curves]), axis=0)      # best match to any example
    win = float(np.median(lengths))

    hop = max(fhop, a.hop)                                          # pool to the output grid
    k = max(1, int(round(hop / fhop)))
    nb = int(math.ceil(len(curve) / float(k)))
    pad = np.full(nb * k - len(curve), -1.0, dtype=np.float32)
    vals = np.concatenate([curve, pad]).reshape(nb, k).max(axis=1)
    hop = k * fhop

    open_t, offset = 0.5, 0.1
    events, i, n = [], 0, len(vals)
    while i < n:
        if vals[i] >= open_t - offset:
            j = i
            while j + 1 < n and vals[j + 1] >= open_t - offset:
                j += 1
            pk = float(vals[i:j + 1].max())
            if pk >= open_t:
                p = i + int(np.argmax(vals[i:j + 1]))
                events.append({"start": round(p * hop, 3), "end": round(min(dur, p * hop + win), 3),
                               "label": a.query, "score": round(pk, 4), "weak": False})
            i = j + 1
        else:
            i += 1
    out = {"contract": 1, "plugin": "fingerprint", "duration": dur,
           "labels": [{"label": a.query, "color": None}],
           "scores": {"kind": "similarity", "span": "window", "win": win, "hop": hop, "gap": 0.0, "min_dur": 0.0, "offset": offset,
                      "n": int(n), "t0": 0.0, "t_end": dur,
                      "labels": [{"label": a.query, "open": open_t, "values": [round(float(v), 4) for v in vals]}]},
           "events": events}
    with open(a.json, "w") as f:
        json.dump(out, f)


if __name__ == "__main__":
    main()
