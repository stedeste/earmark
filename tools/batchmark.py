import os, re, json, requests, sys

BASE = "http://127.0.0.1:5000"
COLORS = ["Yellow", "Blue", "Green", "Red", "Purple", "Pink", "Orange", "Cyan"]

def parse_transcription(filepath):
    markers = []
    with open(filepath, encoding="utf-8") as f:
        text = f.read()
    blocks = re.findall(
        r'\[\d+\]\s+(\d+:\d+\.\d+)\s*→\s*(\d+:\d+\.\d+)\s*\n\s*CIM:\s*(.+)',
        text
    )
    for i, (start, end, cim_line) in enumerate(blocks):
        def to_hhmmss(t):
            parts = t.split(":")
            if len(parts) == 2:
                return f"00:{parts[0].zfill(2)}:{parts[1][:2]}"
            return t
        markers.append({
            "start": to_hhmmss(start),
            "end": to_hhmmss(end),
            "label": cim_line.strip(),
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
    folder = sys.argv[1] if len(sys.argv) > 1 else "."
    print(f"Scanning: {folder}")
    batch_import(folder)