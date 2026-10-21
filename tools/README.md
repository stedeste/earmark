# tools

## batchmark.py

Bulk-imports markers into Earmark from transcription `.txt` files.

Each `.txt` file must have the same name as an audio file already in Earmark
(`rec_001.txt` -> `rec_001.wav`) and contain blocks like:

```
[1] 0:01.20 → 0:03.85
LABEL: label text for this marker
```

Usage (with Earmark running):

```
pip3 install requests
python3 batchmark.py /path/to/transcriptions
```

If your label lines start with a different word, e.g. `TEXT:`, pass it with `--prefix TEXT`.
If Earmark is running on another port, add `--port <n>`.

The target database needs at least one field of type **Markers**; the first one found is used.
Earmark must be running (default port 5000).
