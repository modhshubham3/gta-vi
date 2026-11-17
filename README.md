# Clip watcher

Watches a live Twitch stream and cuts a captioned 9:16 clip around every chat spike, while the stream is still going.

1. **Streamlink** records the stream from the moment it goes live.
2. **Twitch chat** is read anonymously (read-only, no account, no token) and counted every 10 seconds.
3. When a 10-second count jumps to 3x the recent normal, that's a spike.
4. **FFmpeg** cuts 30 seconds around it from the recording: about 25 s before the spike and 5 s after, because chat reacts a few seconds late.
5. **Whisper** writes word-timed captions, burned into a 1080x1920 clip.

Everything here is free and runs on your own computer. One script, about 320 lines.

## 1. Install (once)

You need **Python 3.10 to 3.12** and **FFmpeg**.

- FFmpeg on Windows: `winget install Gyan.FFmpeg`. On a Mac: `brew install ffmpeg`. Check it with `ffmpeg -version`.
- Then, in this folder:

```bash
python -m venv .venv
.venv\Scripts\activate          # Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

The first clip downloads the Whisper model (about 150 MB for `base`).

## 2. Run it

```bash
python clip_watcher.py CHANNEL
```

`CHANNEL` is the part after `twitch.tv/`. Start it before the stream goes live: it waits, then records from the first second. Leave it running. Clips land in `output/clips/`, with a `spikes.csv` log. Stop it with Ctrl+C.

Clip one moment from a saved recording instead:

```bash
python clip_watcher.py --file output/recordings/REC.ts --at 754
```

## 3. Tune it

| Option | Default | What it does |
|---|---|---|
| `--ratio` | 3 | A spike is this many times the normal count. Lower it for more clips. |
| `--min-msgs` | 20 | Ignore spikes smaller than this. Raise it for huge chats. |
| `--window` | 10 | Seconds per chat count. |
| `--baseline` | 180 | Seconds of chat that set "normal". |
| `--length` | 30 | Clip length in seconds. |
| `--before` | 15 | Seconds kept before the spiking window. |
| `--gap` | 60 | Minimum seconds between clips. |
| `--quality` | best | Streamlink quality, e.g. `720p60`, `480p30`. Falls back to best. |
| `--model` | base | Whisper model: `tiny` is fastest, `small` is more accurate. |
| `--device` | auto | Where Whisper runs. Auto tries your GPU and falls back to the CPU. |
| `--crop` | off | Centre-crop to 9:16 instead of the full frame over a blurred fill. |
| `--no-captions` | off | Skip Whisper. |
| `--verbose` | off | Print every chat count, to see what "normal" looks like. |

Big streams: start with `--ratio 2.5 --min-msgs 60`. Small streams: `--ratio 3 --min-msgs 8`.

## Good to know

- **It only works while they're live.** It can't go back and clip a stream that already ended.
- **Anonymous chat reading** uses Twitch's long-standing `justinfan` read-only login. It's community practice, not part of Twitch's documented API, so it could stop working one day.
- **Disk:** a stream at `best` quality is roughly 3 to 6 GB an hour. Use `--quality 720p60` to save space, and delete old recordings in `output/recordings/`.
- **Speed:** on a laptop CPU, a 30-second clip takes about 30 seconds with `tiny` and longer with `base`. Clips queue up and never block the recording.
- **Rights:** the stream belongs to the streamer, and game footage to the publisher. Ask the streamer before you post their clips, credit them, and add your own commentary. Plain reposts can lose monetization on YouTube for reused content.

Built with Claude Code.

---

## YouTube live + phone control (added)

**YouTube live streams.** `python clip_watcher.py --url "https://www.youtube.com/watch?v=ID"`. YouTube blocks scraping of its chat page, so chat is read through the official YouTube Data API v3: create a free API key (Google Cloud Console -> enable *YouTube Data API v3* -> Credentials -> API key, restricted to that API) and pass it with `--yt-api-key` or the `YT_API_KEY` environment variable. Each poll costs about 5 quota units of the 10,000/day default, so at `--api-poll 10` it lasts about 5.5 hours; raise `--api-poll` for longer streams. Recording uses Streamlink, which works from a normal home connection but is often blocked on datacenter IPs (Colab, cloud VMs).

**Phone control with Telegram (`bot.py`).**
1. Create a bot with `@BotFather`, copy the token.
2. `copy bot.env.example bot.env`, fill in `BOT_TOKEN` and `YT_API_KEY`, leave `CHAT_ID` empty.
3. `python bot.py`, send `/id` to your bot, put that number in `CHAT_ID`, restart the bot.
4. Commands: `/watch LINK`, `/stop`, `/restart`, `/status`, `/clips`, `/disk`, `/clean`, `/settings`, `/set NAME VALUE`.

New clips are sent to your phone. The bot restarts the watcher if it crashes, resumes after a reboot, and only obeys the one chat in `CHAT_ID`. **Never commit `bot.env`** (it is in `.gitignore`).

To start at login on Windows: `schtasks /Create /TN ClipBot /SC ONLOGON /TR "\"C:\path\to\run_bot.bat\"" /RL LIMITED`.
