#!/bin/bash
cd "$(dirname "$0")"
echo "Starting Earmark setup..."
if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 not found. Install it from https://www.python.org/downloads/"
    exit 1
fi
python3 -m pip install -r requirements.txt
chmod +x Earmark.command
echo "Done! Double-click Earmark.command to run."
