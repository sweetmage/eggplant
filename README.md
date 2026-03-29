# Eggplant

Utilities and exported output for browsing story/dialogue data from a local `Trials in Tainted Space` source snapshot.

## What This Project Does

- parses the local source files with `extract_tits_story.py`
- generates a browsable HTML export in `export/`
- keeps the latest generated dataset in version control so the current working result is easy to open and share

## Current Export

Open `export/index.html` to browse the latest generated story export.

## Source Snapshot

The export currently reads from:

`C:/Users/Avery/Downloads/Trials-in-Tainted-Space-master/Trials-in-Tainted-Space-master`

## Run The Export

From this folder:

```bash
python extract_tits_story.py
```

Or on Windows:

```bat
run_windows_export.bat
```
