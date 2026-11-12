AST plugin for Earmark
======================
527 AudioSet sound categories (Audio Spectrogram Transformer), with probabilities.
Leave the box empty to discover the strongest categories in a recording, or name
categories (see classes.txt after installing): label=Category|Category@probability#colour

Install once (needs an internet connection):
  Mac/Linux:  bash install.sh        Windows: double-click install.bat
Uses the same environment as CLAP (~/.earmark/clap-env; created if missing), downloads
the model (about 350 MB) and writes classes.txt.

Model: MIT/ast-finetuned-audioset-10-10-0.4593 (Hugging Face), under its own licence.
