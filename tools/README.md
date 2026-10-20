# tools

## batchmark.py

Bulk-imports markers into Earmark from transcription `.txt` files.

Each `.txt` file must have the same name as an audio file already in Earmark
(`B2213.txt` -> `B2213.wav`) and contain blocks like:

```
[1] 0:01.20 → 0:03.85
CIM: label text for this marker
```

Usage (with Earmark running):

```
pip3 install requests
python3 batchmark.py /path/to/transcriptions
```

The target database needs at least one field of type **Markers**; the first one found is used.
Earmark must be running on its default port (5000).
