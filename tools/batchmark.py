"""Bulk-import markers into Earmark from transcription .txt files.

Usage: python3 batchmark.py FOLDER [--prefix LABEL] [--port 5000]
"""
import os, re, json, argparse

import requests

BASE = "http://127.0.0.1:5000"
PREFIX = "LABEL"
COLORS = ["Yellow", "Blue", "Green", "Red", "Purple", "Pink", "Orange", "Cyan"]

def parse_transcription(filepath):
    markers = []
    with open(filepath, encoding="utf-8") as f:
        text = f.read()
    blocks = re.findall(
        r'\[\d+\]\s+(\d+:\d+\.\d+)\s*→\s*(\d+:\d+\.\d+)\s*\n\s*' + re.escape(PREFIX) + r':\s*(.+)',
        text
    )
    for i, (start, end, label_line) in enumerate(blocks):
        def to_hhmmss(t):
            parts = t.split(":")
            if len(parts) == 2:
                return f"00:{parts[0].zfill(2)}:{parts[1][:2]}"
            return t
        markers.append({
            "start": to_hhmmss(start),
            "end": to_hhmmss(end),
            "label": label_line.strip(),
            "color": COLORS[i % len(COLORS)]
        })
    return markers

def get_markers_field_id():
    """Find the first markers-type field, or None."""
    fields = requests.get(f"{BASE}/api/fields").json()
    for f in fields:
        if f["type"] == "markers":
            return f["id"]
    return None

def get_file_map():
    """Return dict of {filename_without_ext: file_id} from Earmark."""
    files = requests.get(f"{BASE}/api/files").json()
    fmap = {}
    for f in files:
        name = os.path.splitext(f["filename"])[0]
        fmap[name] = f["id"]
    return fmap

def batch_import(folder):
    field_id = get_markers_field_id()
    if not field_id:
        print("ERROR: No markers field found. Create one in Earmark first.")
        return

    file_map = get_file_map()
    txt_files = [f for f in os.listdir(folder) if f.endswith(".txt")]

    matched, skipped = 0, 0
    for txt in sorted(txt_files):
        name = os.path.splitext(txt)[0]
        if name not in file_map:
            print(f"  SKIP  {txt} — no matching audio in Earmark")
            skipped += 1
            continue

        markers = parse_transcription(os.path.join(folder, txt))
        if not markers:
            print(f"  SKIP  {txt} — no markers parsed")
            skipped += 1
            continue

        file_id = file_map[name]
        resp = requests.post(f"{BASE}/api/file/{file_id}/value", json={
            "field_id": field_id,
            "value": json.dumps(markers)
        })
        status = "OK" if resp.ok else f"FAIL ({resp.status_code})"
        print(f"  {status}  {txt} → {len(markers)} markers → file ID {file_id}")
        matched += 1

    print(f"\nDone: {matched} matched, {skipped} skipped")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Bulk-import markers into Earmark from transcription .txt files.")
    ap.add_argument("folder", nargs="?", default=".", help="folder with the .txt files")
    ap.add_argument("--prefix", default=PREFIX, help="word before the colon on each label line (default: LABEL)")
    ap.add_argument("--port", type=int, default=5000, help="port Earmark is running on (default: 5000)")
    args = ap.parse_args()
    PREFIX = args.prefix
    BASE = "http://127.0.0.1:%d" % args.port
    print(f"Scanning: {args.folder}  (label lines start with '{PREFIX}:')")
    batch_import(args.folder)
