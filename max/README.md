# Earmark for Max

Query an Earmark collection from Max/MSP: find files by any field, tag or text, pull out markers with their times, get statistics, and play the results.

## Install

1. Download **`Earmark-Max.zip`** from the [latest release](https://github.com/stedeste/earmark/releases/latest).
2. Unzip it and move the **`earmark-query`** folder into **`Documents/Max 9/Packages/`** (or `Max 8/Packages`).
3. Restart Max. The scripts are now found by name from any patch. No `npm install` is needed: the one library they use (`sql.js`) is included.

## Two ways to connect

| | `earmark-node.js` (recommended) | `earmark-max.js` |
|---|---|---|
| Max object | `[node.script earmark-node.js @autostart 1]` | `[js earmark-max.js]` |
| Reads | the Earmark database (`.db`) directly | a `[dict]` loaded from **File → Export for Max** |
| Stays current | send `reload` after editing in Earmark | re-export, then re-read the dict |

### earmark-node.js

By default it opens `Documents/Earmark/earmark.db`, where the Earmark app keeps its databases. For another database:

```
[loadbang]
|
[delay 1000]      <- give Node for Max a moment to start
|
[setdb /Users/you/Documents/Earmark/archive.db(
|
[node.script earmark-node.js @autostart 1]
```

Messages it understands:

| Message | Result |
|---|---|
| `setdb <path>` | use another database |
| `reload` | re-read the database after changes in Earmark |
| `count` | number of files |
| `fields` | every field with its type |
| `listall` | every file |
| `gt` / `gte` / `lt` / `lte <field> <n>` | files with a parameter above / below a value |
| `between <field> <min> <max>` | files with a parameter in a range |
| `eq <field> <value>` | exact match (choice, text, number) |
| `contains <field> <text>` | field (or a linked file's text) contains text |
| `hastag <tag>` | files with a tag |
| `search <text>` | search every text field |
| `searchmarkers <text>` | markers whose label contains text, in every markers field |
| `markers <field> [text]` | all markers in a field, optionally filtered |
| `sort <field> asc\|desc` | all files sorted by a field |
| `getfile <i>` | all metadata of file *i* |
| `getfilepath <i>` | the audio file's path |
| `getfield <i> <field>` | one field of file *i* (linked files also send their text) |
| `random` / `randomfile` | a random file from the last query / from all files |
| `stats <field>` / `stats tags` | statistics for a field, or for file tags |
| `wordfreq <field> [n]` / `wordfreq <field> count` | most frequent words / number of distinct words |

What comes out of the outlet (route on the first word):

| Output | Meaning |
|---|---|
| `file <i> <filename>` … `done <count>` | query results; *i* is the file's index |
| `marker <i> <filename> <label> <start_ms> <end_ms>` … `markers_done <count>` | marker results, times in milliseconds |
| `filepath <i> <path>` | from `getfilepath`, ready for `[sfplay~]`, `[buffer~]` or `[polybuffer~]` |
| `field <name> <type> <details>` … `fields_done <count>` | from `fields` |
| `stats …` … `stats_done` / `wordfreq …` … `wordfreq_done` | statistics |
| `count <n>`, `random <i> <filename>` | as named |

### earmark-max.js

1. In Earmark: **File → Export for Max** saves `earmark-max.json`.
2. In Max:

```
[loadmess read /path/to/earmark-max.json(
|
[dict earmark]

[js earmark-max.js]     outlet 0: results    outlet 1: status / errors
```

It understands the same query messages (`fields`, `all`, `gt`, `between`, `eq`, `contains`, `hastag`, `search`, `markers`, `getfile`, `getfield`, `random`, `sort` …); send `help` for the list, and `setdict <name>` to use a dict with another name.

## Tips

- Index numbers (*i*) are positions in Earmark's file list, stable until files are added or removed. Use `getfilepath <i>` to load audio rather than relying on order.
- After editing in Earmark, send `reload` (node) or re-export (dict) so Max sees the changes.
