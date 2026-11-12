CLAP plugin for Earmark
=======================
Free-text sound search with LAION CLAP: type what you hear ("a duck quacking",
"church bells") and Earmark marks where it happens.

  - Run: several sounds at once, comma-separated: label=phrase|phrase@threshold#colour
  - Try: type phrasings and see the score curve change as you type (live)
  - Find similar: select example markers (or a range) and find more of the same kind

Install once (about 3 GB in total, needs an internet connection):
  Mac/Linux:  bash install.sh        Windows: double-click install.bat
This creates ~/.earmark/clap-env (shared with the AST plugin) and downloads the model.

Limits: scores are similarities, only meaningful within one file, so set the
threshold per file with the score strip; short sounds under much louder ones may be
missed; timing is about +/- half a window.

Model: laion/larger_clap_general (Hugging Face), under its own licence.
