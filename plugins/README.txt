EARMARK PLUGINS
===============

Plugins let Earmark listen to audio: find where a sound happens, discover what
is in a recording, or find more sounds like one you have marked. Earmark itself
stays small - it contains no machine-learning code. Each plugin is a folder with
a description file and a program that Earmark runs in the plugin's own Python
environment, so plugins can use whatever libraries they need without conflicts.

Plugins in this folder:

  clap         Free-text search ("a duck quacking"), live phrasing, find similar.
  ast          527 AudioSet categories; can discover what it hears.
  fingerprint  Find near-identical repeats of a sound. No model, nothing to install.

Downloading Earmark does NOT download any model. CLAP and AST download theirs
(and their libraries, a few GB) only when you run their installer.


USING PLUGINS
-------------
1. Install a plugin once:  Mac/Linux  bash plugins/<name>/install.sh
                           Windows    double-click plugins\<name>\install.bat
2. In Earmark, click "Detect" next to a markers field. The panel lists every
   plugin; those not installed yet are greyed out.

Where Earmark looks for plugins:
  - the "plugins" folder next to earmark.py (when running from source);
  - Documents/Earmark/plugins (always; use this one with the downloaded app:
    copy a plugin's folder there and run its installer).

What the panel does with any plugin:
  Run            look for the sounds typed in the box (or discover, if the plugin
                 can) in the whole file or only the selection;
  Score strip    one curve per sound; drag the threshold line and the markers
                 follow;
  Try            (live plugins) type a phrasing, see its curve at once,
                 "Make markers" when it looks right;
  Find similar   select example markers (or a range); every plugin that can do it
                 adds a candidate curve, so models can be compared side by side;
  Rename all     rename every marker of one name in the lane.
Markers made by a plugin are replaced by later runs and threshold drags only
while untouched: anything you move, trim, rename or make yourself is kept.

Analyses are cached in Documents/Earmark/cache/, never next to your recordings.


WRITING A PLUGIN  (plugin contract, version 1)
-----------------------------------------------
A plugin is a folder:

  plugins/<id>/plugin.json    the manifest (below) - required
  plugins/<id>/run.py         batch program - required (any name; see "run")
  plugins/<id>/server.py      live helper - optional
  plugins/<id>/install.sh     (+ install.bat) creates the environment, downloads
                              the model - recommended
  plugins/<id>/LICENSE.txt    plugin + model licence - recommended
  plugins/<id>/README.txt     what it recognises, known limits - recommended

The simplest complete example is fingerprint/ (one file, numpy only).
Earmark never imports a plugin. It reads plugin.json, starts the program with
the arguments below, and reads back a JSON result. Anything that can do that
works: PyTorch, TensorFlow, ONNX, another language.


1. MANIFEST (plugin.json)

{
  "contract": 1,
  "id": "mymodel",
  "name": "My model",
  "version": "1.0",
  "description": "What it recognises, in one line.",
  "license": "Plugin code MIT; model licence ...",

  "python": "~/.earmark/mymodel-env/bin/python",
  "run":    ["{python}", "-u", "run.py"],
  "server": ["{python}", "-u", "server.py"],

  "query": {
    "kind": "classes",
    "separator": ";",
    "discover": true,
    "classes_file": "classes.txt",
    "placeholder": "wren=Eurasian Wren; nightingale=Common Nightingale",
    "help": "Shown under the box."
  },
  "windows": { "choices": [1, 2], "default": 1, "hop_ratio": 0.5 },
  "score":   { "kind": "probability", "default_threshold": 0.5 },
  "similar": { "method": "batch", "default_threshold": 0.5 },
  "options": [
    { "id": "lat", "label": "Latitude", "type": "number", "default": null }
  ]
}

  contract     1. Earmark refuses a plugin whose contract is newer than it knows.
  id           short, lowercase, unique; used for marker sources and caches.
  python       interpreter. Relative = inside the plugin folder; "~" allowed
               (environments shared between plugins); "{earmark_python}" = the
               Python Earmark runs in (only numpy available; works in the app too).
               On Windows ".../bin/python" is also tried as ".../Scripts/python.exe".
               The plugin is shown as "not installed" while this file is missing.
  run          batch command. {python} and {plugin_dir} are replaced.
  server       optional live helper (section 3).
  query.kind   "text"     free text (CLAP)
               "classes"  names from a fixed list (AST)
               "none"     no query; the plugin reports what it finds
               "example"  works only from example sounds (find similar); such a
                          plugin is not listed in the engine menu
  query.separator  between labels in the box ("," for text, ";" for classes,
               because class names contain commas).
  query.discover   true if an empty box means "report the strongest classes".
  query.classes_file  one class name per line.
  windows      window lengths (s) offered, the default, and the hop: a ratio
               ("hop_ratio": 0.5) or fixed choices ("hop_choices", "hop_default").
  score.kind   "similarity" or "probability" (0-1); wording and default threshold.
  similar      optional: the plugin can find sounds similar to examples (section 4).
               default_threshold null = Earmark starts at the 95th percentile.
  options      extra settings shown in the panel and passed as --opt key=value.
               Types: number, int, text, bool, choice (with "choices": [...]).

Label syntax in the box, the same for every plugin, parsed by the PLUGIN:
  label=query|query@threshold#colour
  e.g.  duck=a duck quacking|ducks@0.2#red      wren=Eurasian Wren#green


2. BATCH RUN

Earmark runs (working directory = the plugin folder):

  <run command> --audio PATH --json OUT
                [--query TEXT]            the box, as typed
                [--win S] [--hop S]
                [--start S --end S]       selection only (seconds)
                [--cache DIR]             a folder for this file's cached analysis
                [--opt KEY=VALUE ...]     one per option with a value

Progress, on stdout, one per line (anything else is ignored):
  PROGRESS <done> <total> [message]
  STATUS <message>
  ERROR <message>                         shown to the user
Failure: exit code not 0; the last ERROR line (or the last output line) is shown.

Result, written to OUT:

{
  "contract": 1,
  "plugin": "mymodel",
  "duration": 812.3,                     full length of the file (s)
  "labels": [ { "label": "wren", "color": "Green", "detail": "Eurasian Wren" } ],
  "scores": {                            optional; without it there is no curve
    "kind": "probability",
    "win": 3, "hop": 1.5,
    "t0": 0.0, "t_end": 812.3,           analysed range
    "n": 540,                            number of windows
    "gap": 0.5, "min_dur": 0.5, "offset": 0.1,
    "span": "window",                    optional (see below)
    "labels": [ { "label": "wren", "open": 0.5, "values": [0.01, 0.02, ...] } ]
  },
  "events": [ { "start": 12.0, "end": 16.5, "label": "wren", "score": 0.83, "weak": false } ]
}

  - Times are absolute seconds from the start of the file.
  - Window i covers [t0 + i*hop, t0 + i*hop + win].
  - Threshold drag: a marker opens where the curve reaches "open" and continues
    while it stays above open - offset; markers closer than "gap" merge; shorter
    than "min_dur" are dropped. A marker spans window centres +- hop/2, or whole
    windows with "span": "window".
  - "weak": true marks an uncertain candidate (shown as "wren?").
  - "color" and "detail" are optional. Unknown fields are ignored.


3. LIVE HELPER (optional)

For plugins that can answer in milliseconds once a file is analysed.
Earmark runs the "server" command; the helper loads its model, listens on
127.0.0.1 (this computer only) on a free port chosen by the system (port 0),
and prints one line:   READY <port>

  POST /prepare  {path, win, hop, analyse, cache, options}
       -> {ok, ready: true}                                analysis available
       -> {ok, needs_analysis: true, windows, duration}    (analyse = false)
       -> {ok, job}                                        analysis started
  GET  /status?job=ID  -> {state: running|done|error, done, total, message}
  POST /score    {path, win, hop, queries: [...], options}
       -> {ok, n, win, hop, duration, values: {query: [v0, v1, ...]}}
  GET  /busy     -> {busy}

Quit when the parent process (Earmark) is gone, and after 15 idle minutes;
Earmark restarts the helper when needed. One analysis at a time.


4. FIND SIMILAR (optional)

  "similar": { "method": "live" | "batch", "default_threshold": 0.5 | null }

live  - the helper also answers
  POST /similar {path, win, hop, cache, examples: [[start, end], ...]}
       -> {ok, n, win, hop, duration, values: [v0, v1, ...]}
       -> {ok: false, needs_analysis: true, windows}   (Earmark offers "Analyse")

batch - Earmark runs the run command with
  --audio PATH --json OUT --query LABEL --cache DIR --example S:E [--example S:E ...]
  and reads one label's curve from "scores".


5. MARKERS MADE BY PLUGINS

  start, end, label, color, source: "<plugin id>",
  det_label, det_start, det_end, det_score, det_weak   (what the model proposed)

A run or threshold drag replaces only that plugin's untouched markers of that
label inside the analysed range.
