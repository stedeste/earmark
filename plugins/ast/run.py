#!/usr/bin/env python3
"""Earmark plugin contract v1 - batch entry point for AST (wraps ast_test.py)."""
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
    argv = ["ast_test.py", a.audio, "--json", a.json, "--progress-lines",
            "--win", str(a.win), "--hop", str(a.hop if a.hop else a.win / 2.0)]
    if a.query.strip():
        argv += ["--classes", a.query]          # empty query = discover
    if a.cache:
        argv += ["--cache", a.cache]
    if a.start is not None and a.end is not None:
        argv += ["--start", str(a.start), "--end", str(a.end)]
    sys.argv = argv
    import ast_test
    print("STATUS Loading the AST model...", flush=True)
    ast_test.main()
    with open(a.json) as f:
        data = json.load(f)
    data["contract"], data["plugin"] = 1, "ast"
    data.setdefault("scores", {})["kind"] = "probability"
    with open(a.json, "w") as f:
        json.dump(data, f)


if __name__ == "__main__":
    main()
