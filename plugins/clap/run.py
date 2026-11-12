#!/usr/bin/env python3
"""Earmark plugin contract v1 - batch entry point for CLAP (wraps clap_test.py)."""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--json", required=True)
    ap.add_argument("--query", default="")
    ap.add_argument("--win", type=float, default=1.0)
    ap.add_argument("--hop", type=float, default=None)
    ap.add_argument("--start", type=float, default=None)
    ap.add_argument("--end", type=float, default=None)
    ap.add_argument("--cache", default=None)
    ap.add_argument("--opt", action="append", default=[])
    a = ap.parse_args()
    if not a.query.strip():
        print("ERROR CLAP needs at least one sound to look for.", flush=True)
        sys.exit(2)
    argv = ["clap_test.py", a.audio, a.query, "--json", a.json, "--progress-lines",
            "--win", str(a.win), "--hop", str(a.hop if a.hop else a.win / 2.0)]
    if a.cache:
        argv += ["--cache", a.cache]
    if a.start is not None and a.end is not None:
        argv += ["--start", str(a.start), "--end", str(a.end)]
    sys.argv = argv
    import clap_test
    print("STATUS Loading the CLAP model...", flush=True)
    clap_test.main()
    with open(a.json) as f:
        data = json.load(f)
    data["contract"], data["plugin"] = 1, "clap"
    data.setdefault("scores", {})["kind"] = "similarity"
    with open(a.json, "w") as f:
        json.dump(data, f)


if __name__ == "__main__":
    main()
