#!/usr/bin/env python3
"""
Installs the CLAP plugin for Earmark (once):
  - a Python environment in ~/.earmark/clap-env (shared with the AST plugin),
    with PyTorch, transformers and the audio libraries (about 2 GB);
  - the model laion/larger_clap_general (about 800 MB, from Hugging Face).
Run:  bash install.sh  (Mac/Linux)   or double-click install.bat  (Windows)
  or: python3 install.py
"""
import os
import subprocess
import sys

ENV = os.path.join(os.path.expanduser("~"), ".earmark", "clap-env")
PY = os.path.join(ENV, "Scripts", "python.exe") if os.name == "nt" else os.path.join(ENV, "bin", "python")
PACKAGES = ["torch", "transformers", "librosa", "soundfile", "numpy"]
MODEL = "laion/larger_clap_general"


def run(cmd, **kw):
    print("> " + " ".join(cmd), flush=True)
    subprocess.check_call(cmd, **kw)


def ensure_env():
    if not os.path.exists(PY):
        print("Creating the environment in %s ..." % ENV, flush=True)
        os.makedirs(os.path.dirname(ENV), exist_ok=True)
        run([sys.executable, "-m", "venv", ENV])
    run([PY, "-m", "pip", "install", "--upgrade", "pip"])
    run([PY, "-m", "pip", "install"] + PACKAGES)


if __name__ == "__main__":
    ensure_env()
    print("Downloading the CLAP model (about 800 MB, once)...", flush=True)
    run([PY, "-c", "from transformers import ClapModel, ClapProcessor; "
                   "ClapModel.from_pretrained(%r); ClapProcessor.from_pretrained(%r); print('CLAP ready')" % (MODEL, MODEL)],
        env=dict(os.environ, HF_HUB_OFFLINE="0"))
