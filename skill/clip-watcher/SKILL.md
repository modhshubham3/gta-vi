---
name: clip-watcher
description: Watch a live Twitch stream and auto-cut captioned 9:16 clips around chat spikes (Streamlink + FFmpeg + Whisper). Use when the user asks to clip, watch, record or monitor a Twitch channel, or to cut a clip from a saved recording at a timestamp.
---

# Clip watcher

Tool lives at `CLIP_WATCHER_DIR` (default `~/gta6-clip-watcher`, script `clip_watcher.py`, venv in `.venv`).

## Live watch
```bash
cd $CLIP_WATCHER_DIR && . .venv/bin/activate
python clip_watcher.py CHANNEL --verbose     # run in background; Ctrl+C / kill to stop
```
Clips -> `output/clips/`, recordings -> `output/recordings/`, log -> `output/clips/spikes.csv`.

## Clip from recording
```bash
python clip_watcher.py --file output/recordings/REC.ts --at 754
```

## Tuning
`--ratio` (3; lower = more clips), `--min-msgs` (20; big chats 60), `--quality` (e.g. 720p60), `--model` (tiny/base/small), `--crop`, `--gap` (60), `--no-captions`.

## Rules
- Only works while the stream is live; cannot clip ended streams.
- Always remind the user: ask the streamer before posting, credit them, add commentary (copyright/monetization risk).
- Recording uses ~3-6 GB/hour at best quality; warn before long runs.
- Anonymous chat (justinfan) is unofficial and may stop working.
