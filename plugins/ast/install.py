#!/usr/bin/env python3
"""
Installs the AST plugin for Earmark (once):
  - the Python environment ~/.earmark/clap-env (shared with CLAP; created if missing, about 2 GB);
  - the model MIT/ast-finetuned-audioset-10-10-0.4593 (about 350 MB, from Hugging Face);
  - classes.txt, the list of the 527 AudioSet categories.
Run:  bash install.sh  (Mac/Linux)   or double-click install.bat  (Windows)
  or: python3 install.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(os.path.expanduser("~"), ".earmark", "clap-env")
PY = os.path.join(ENV, "Scripts", "python.exe") if os.name == "nt" else os.path.join(ENV, "bin", "python")
PACKAGES = ["torch", "transformers", "librosa", "soundfile", "numpy"]
MODEL = "MIT/ast-finetuned-audioset-10-10-0.4593"

FETCH = r"""
import sys
from transformers import ASTFeatureExtractor, ASTForAudioClassification
name = sys.argv[1]
ASTFeatureExtractor.from_pretrained(name)
labels = ASTForAudioClassification.from_pretrained(name).config.id2label
with open(sys.argv[2], "w", encoding="utf-8") as f:
    for i in range(len(labels)):
        f.write((labels[i] if i in labels else labels[str(i)]) + "\n")
print("AST ready: %d categories" % len(labels))
"""


def run(cmd, **kw):
    print("> " + " ".join(cmd[:4]) + (" ..." if len(cmd) > 4 else ""), flush=True)
    subprocess.check_call(cmd, **kw)


if __name__ == "__main__":
    if not os.path.exists(PY):
        print("Creating the environment in %s ..." % ENV, flush=True)
        os.makedirs(os.path.dirname(ENV), exist_ok=True)
        run([sys.executable, "-m", "venv", ENV])
        run([PY, "-m", "pip", "install", "--upgrade", "pip"])
        run([PY, "-m", "pip", "install"] + PACKAGES)
    print("Downloading the AST model (about 350 MB, once) and writing the category list...", flush=True)
    run([PY, "-c", FETCH, MODEL, os.path.join(HERE, "classes.txt")], env=dict(os.environ, HF_HUB_OFFLINE="0"))
