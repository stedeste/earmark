#!/usr/bin/env python3
"""
clap_server.py - keeps the CLAP model loaded so Earmark can score phrasings live.

Started by Earmark (earmark_clap.py) inside the CLAP environment; listens on
127.0.0.1 only and quits when Earmark quits. Not meant to be run by hand, but
for testing:  python clap_server.py   -> prints "READY <port>".

Audio embeddings come from the same cache files clap_test.py writes
(<audio>_clap/emb_<model>_w<win>_h<hop>.npz), so a file analysed once by either
is instant for the other. Scoring a new phrase = one text embedding + one
matrix product: milliseconds.

Endpoints (JSON):
  POST /prepare {path, win, hop, analyse}   -> {ok, ready} if analysed before; else {ok, needs_analysis, windows}
                                               unless analyse=true -> {ok, job} (poll /status)
  GET  /busy                                -> {busy}
  GET  /status?job=ID                       -> {state, done, total, message, info}
  POST /score   {path, win, hop, queries[]} -> {ok, n, win, hop, duration, values: {query: [...]}}
  POST /similar {path, win, hop, examples: [[start, end], ...]} -> {ok, n, win, hop, duration, values: [...]}
                                               (or {ok: false, needs_analysis: true, windows})
Every request may carry "cache": the folder where Earmark keeps this file's analyses.
Phrases are sent to CLAP exactly as given.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clap_test as ct          # sets the torch/HF environment variables on import

import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

SR = ct.SR
MODEL_NAME = ct.DEFAULT_MODEL
MODEL_TAG = MODEL_NAME.split("/")[-1]

_model = {"model": None, "proc": None, "device": None}
_lock = threading.Lock()          # one model call at a time
_files = {}                       # (path, win, hop) -> {"emb", "n", "duration", "used"}
_text = {}                        # prompt -> unit vector
_jobs = {}
MAX_FILES = 3
IDLE_EXIT = 15 * 60               # quit after 15 idle minutes to free memory (Earmark restarts it on demand)
_last_used = [time.time()]


# ------------------------------------------------------------------ model (swappable for tests)
def load_model():
    import torch
    from transformers import ClapModel, ClapProcessor
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = ClapModel.from_pretrained(MODEL_NAME).to(device).eval()
    proc = ClapProcessor.from_pretrained(MODEL_NAME)
    _model.update(model=model, proc=proc, device=device)


def embed_chunks(chunks):
    import torch
    feats = _model["proc"].feature_extractor(chunks, sampling_rate=SR, return_tensors="pt")
    inputs = {k: v.to(_model["device"]) for k, v in feats.items()}
    with torch.no_grad():
        e = ct.as_tensor(_model["model"].get_audio_features(**inputs))
    return torch.nn.functional.normalize(e.float(), dim=-1).cpu().numpy()


def embed_texts(prompts):
    return ct.embed_text(_model["model"], _model["proc"].tokenizer, prompts, _model["device"])


# ------------------------------------------------------------------ audio embeddings
def cache_file(path, win, hop, cache=None):
    name = "emb_%s_w%g_h%g.npz" % (MODEL_TAG, win, hop)
    if cache:
        return os.path.join(ct.use_cache_dir(cache, path, "_clap", [name]), name)
    stem = os.path.splitext(os.path.basename(path))[0]
    outdir = os.path.join(os.path.dirname(path), stem + "_clap")
    os.makedirs(outdir, exist_ok=True)
    return os.path.join(outdir, name)


def remember(key, emb, duration):
    _files[key] = {"emb": emb, "n": int(emb.shape[0]), "duration": duration, "used": time.time()}
    while len(_files) > MAX_FILES:
        oldest = min(_files, key=lambda k: _files[k]["used"])
        del _files[oldest]


def prepare_job(job, path, win, hop, cache=None):
    key = (path, win, hop)
    try:
        duration = ct.audio_duration(path)
        cf = cache_file(path, win, hop, cache)
        if os.path.isfile(cf):
            c = np.load(cf)
            if abs(int(c["n_samples"]) / float(SR) - duration) < 0.1:
                remember(key, c["emb"], duration)
                job.update(state="done", message="Loaded saved analysis.", info=info(key))
                return
        job["message"] = "Loading audio..."
        audio = ct.load_region(path, SR, 0.0, duration, duration)
        n_samples = len(audio)
        w, h = int(round(win * SR)), int(round(hop * SR))
        starts, padded = ct.make_windows(n_samples, w, h)
        if padded > n_samples:
            audio = np.concatenate([audio, np.zeros(padded - n_samples, dtype=np.float32)])
        job["total"] = len(starts)
        out = []
        batch = 16
        t0 = time.time()
        for b in range(0, len(starts), batch):
            chunks = [audio[s:s + w] for s in starts[b:b + batch]]
            with _lock:
                out.append(embed_chunks(chunks))
            job["done"] = min(b + batch, len(starts))
            per = (time.time() - t0) / job["done"]
            left = per * (job["total"] - job["done"])
            job["message"] = "Analysing windows %d/%d (%.0f ms each, about %s left)..." % (
                job["done"], job["total"], per * 1000,
                ("%d min" % round(left / 60.0)) if left >= 90 else ("%d s" % max(1, round(left))))
            _last_used[0] = time.time()
        del audio
        emb = np.concatenate(out, axis=0)
        np.savez(cf, emb=emb, n_samples=n_samples)
        remember(key, emb, n_samples / float(SR))
        job.update(state="done", message="Analysis done.", info=info(key))
    except Exception as e:
        job.update(state="error", message="CLAP helper error: %s" % e)


def load_cached(path, win, hop, cache=None):
    """Load a saved analysis if there is one; returns True if the file is ready."""
    key = (path, win, hop)
    if key in _files:
        return True
    cf = cache_file(path, win, hop, cache)
    if os.path.isfile(cf):
        c = np.load(cf)
        duration = ct.audio_duration(path)
        if abs(int(c["n_samples"]) / float(SR) - duration) < 0.1:
            remember(key, c["emb"], duration)
            return True
    return False


def info(key):
    f = _files[key]
    return {"n": f["n"], "duration": f["duration"], "win": key[1], "hop": key[2]}


def score(path, win, hop, phrases):
    key = (path, win, hop)
    if key not in _files:
        return {"ok": False, "error": "not prepared"}
    f = _files[key]
    f["used"] = time.time()
    todo = [p for p in phrases if p not in _text]
    if todo:
        with _lock:
            T = embed_texts(todo)
        for p, v in zip(todo, T):
            _text[p] = v
    T = np.stack([_text[p] for p in phrases], axis=0)
    sims = f["emb"] @ T.T
    return {"ok": True, "n": f["n"], "win": win, "hop": hop, "duration": f["duration"],
            "values": dict((p, [round(float(x), 4) for x in sims[:, i]]) for i, p in enumerate(phrases))}


def similar(path, win, hop, examples, cache=None):
    """'Find similar': the average embedding of the example regions, compared with every window."""
    key = (path, win, hop)
    if key not in _files and not load_cached(path, win, hop, cache):
        dur = ct.audio_duration(path)
        return {"ok": False, "needs_analysis": True, "windows": int(math.ceil(max(0.0, dur - win) / hop)) + 1}
    f = _files[key]
    f["used"] = time.time()
    emb, n = f["emb"], f["n"]
    starts = np.arange(n) * hop
    vecs = []
    for s, e in examples:
        s, e = float(s), float(e)
        inside = np.where((starts >= s - 1e-6) & (starts + win <= e + 1e-6))[0]
        if not len(inside):                         # example shorter than a window: windows around its centre
            c = (s + e) / 2.0
            inside = np.where((starts <= c) & (starts + win >= c))[0]
        if len(inside):
            vecs.append(emb[inside].mean(axis=0))
    if not vecs:
        return {"ok": False, "error": "the examples are outside the analysed audio"}
    q = np.mean(vecs, axis=0)
    q /= np.linalg.norm(q) + 1e-9
    sims = emb @ q
    return {"ok": True, "n": n, "win": win, "hop": hop, "duration": f["duration"],
            "values": [round(float(x), 4) for x in sims]}


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        _last_used[0] = time.time()
        u = urlparse(self.path)
        if u.path == "/busy":
            return self.reply({"busy": any(j.get("state") == "running" for j in _jobs.values())})
        if u.path == "/status":
            job = _jobs.get((parse_qs(u.query).get("job") or [""])[0])
            return self.reply(job or {"state": "error", "message": "unknown job"})
        if u.path == "/ping":
            return self.reply({"ok": True})
        self.reply({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        _last_used[0] = time.time()
        u = urlparse(self.path)
        try:
            d = self.body()
            path, win, hop = d["path"], float(d["win"]), float(d["hop"])
        except Exception as e:
            return self.reply({"ok": False, "error": "bad request: %s" % e})
        if u.path == "/prepare":
            try:
                if load_cached(path, win, hop, d.get("cache")):
                    return self.reply({"ok": True, "ready": True, "info": info((path, win, hop))})
            except Exception as e:
                return self.reply({"ok": False, "error": "could not read saved analysis: %s" % e})
            if any(j.get("state") == "running" for j in _jobs.values()):
                return self.reply({"ok": False, "error": "another file is being analysed; wait for it to finish"})
            if not d.get("analyse"):
                dur = ct.audio_duration(path)
                n = int(math.ceil(max(0.0, dur - win) / hop)) + 1
                return self.reply({"ok": True, "needs_analysis": True, "windows": n, "duration": dur})
            job_id = "%x" % int(time.time() * 1000000)
            job = {"state": "running", "done": 0, "total": 0, "message": "Preparing..."}
            _jobs[job_id] = job
            threading.Thread(target=prepare_job, args=(job, path, win, hop, d.get("cache")), daemon=True).start()
            return self.reply({"ok": True, "job": job_id})
        if u.path == "/similar":
            try:
                return self.reply(similar(path, win, hop, d.get("examples") or [], d.get("cache")))
            except Exception as e:
                return self.reply({"ok": False, "error": "find similar failed: %s" % e})
        if u.path == "/score":
            phrases = [p for p in (d.get("queries") or d.get("phrases") or []) if p and p.strip()]
            if not phrases:
                return self.reply({"ok": False, "error": "no phrases"})
            try:
                return self.reply(score(path, win, hop, phrases))
            except Exception as e:
                return self.reply({"ok": False, "error": "scoring failed: %s" % e})
        self.reply({"ok": False, "error": "not found"}, 404)


def watch_parent():
    """Quit when Earmark is gone, or after IDLE_EXIT seconds without use (frees the model's memory)."""
    parent = os.getppid()
    while True:
        time.sleep(3)
        if os.getppid() != parent:
            os._exit(0)
        busy = any(j.get("state") == "running" for j in _jobs.values())
        if not busy and time.time() - _last_used[0] > IDLE_EXIT:
            os._exit(0)


def main():
    load_model()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    print("READY %d" % srv.server_address[1], flush=True)
    sys.stdout = open(os.devnull, "w")          # nobody reads our output after this
    threading.Thread(target=watch_parent, daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
