#!/usr/bin/env python3
"""bot.py: control clip_watcher from your phone through a private Telegram bot.

What it does
  - /watch LINK starts clip_watcher on a YouTube live link or a Twitch channel, /stop stops it.
  - Restarts clip_watcher if it crashes, and resumes after a reboot (state is saved in output/bot_state.json).
  - Sends every new clip to your phone as a video, and warns you when the disk is nearly full.
  - Keeps Windows awake while it runs.

It only talks OUT to Telegram (no ports to open) and only obeys the one chat in CHAT_ID.
Secrets live in bot.env, never in this file. See bot.env.example.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
OUT = HERE / 'output'
CLIPS = OUT / 'clips'
STATE_FILE = OUT / 'bot_state.json'
WATCHER_LOG = OUT / 'watcher.log'
WATCHER = os.environ.get('BOT_WATCHER_SCRIPT') or str(HERE / 'clip_watcher.py')
WIN = os.name == 'nt'

DEFAULTS = {'ratio': 2.5, 'min_msgs': 20, 'window': 10.0, 'quality': '720p60', 'model': 'tiny',
            'api_poll': 10.0, 'gap': 60.0, 'length': 30.0, 'before': 15.0, 'captions': True, 'verbose': True}
KINDS = {'ratio': float, 'min_msgs': int, 'window': float, 'quality': str, 'model': str,
         'api_poll': float, 'gap': float, 'length': float, 'before': float, 'captions': bool, 'verbose': bool}
FLAGS = {'ratio': '--ratio', 'min_msgs': '--min-msgs', 'window': '--window', 'quality': '--quality',
         'model': '--model', 'api_poll': '--api-poll', 'gap': '--gap', 'length': '--length', 'before': '--before'}


def log(msg):
    print(time.strftime('%H:%M:%S'), msg, flush=True)


def load_env():
    f = HERE / 'bot.env'
    if f.exists():
        for line in f.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ---------- Telegram (plain HTTPS long polling, outbound only) ----------
class Telegram:
    def __init__(self, token, chat_id):
        base = os.environ.get('TELEGRAM_API_BASE', 'https://api.telegram.org')
        self.url, self.chat_id, self.http = f'{base}/bot{token}/', chat_id, requests.Session()

    def call(self, method, http_timeout=40, files=None, **params):
        try:
            r = self.http.post(self.url + method, data=params, files=files, timeout=http_timeout)
            j = r.json()
            return j.get('result') if j.get('ok') else None
        except Exception as e:  # noqa: BLE001 - print only the type: the message holds the URL with the token
            log(f'telegram {method} failed: {type(e).__name__}')
            return None

    def send(self, text, chat_id=None):
        for i in range(0, max(len(text), 1), 4000):
            self.call('sendMessage', chat_id=chat_id or self.chat_id, text=text[i:i + 4000])

    def send_video(self, path, caption=''):
        with open(path, 'rb') as f:
            return self.call('sendVideo', http_timeout=300, files={'video': f}, chat_id=self.chat_id,
                             caption=caption[:900], supports_streaming='true', width=1080, height=1920)


# ---------- State ----------
def load_state():
    try:
        s = json.loads(STATE_FILE.read_text())
    except Exception:  # noqa: BLE001
        s = {}
    s.setdefault('settings', {})
    for k, v in DEFAULTS.items():
        s['settings'].setdefault(k, v)
    s.setdefault('url', None)
    s.setdefault('desired', False)
    return s


def save_state(s):
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(s, indent=1))
    tmp.replace(STATE_FILE)


def normalise(url):
    url = url.strip().strip('<>')
    if ('youtube.com' in url or 'youtu.be' in url) and not url.startswith('http'):
        url = 'https://' + url
    return url


def build_cmd(url, s):
    m = re.match(r'(?:https?://)?(?:www\.)?twitch\.tv/(\w+)', url)
    if m:
        target = [m.group(1).lower()]
    elif url.startswith('http'):
        target = ['--url', url]
    else:
        target = [url.lower()]  # a bare word is a Twitch channel
    cmd = [sys.executable, '-u', WATCHER] + target + ['--out', str(OUT)]
    for k, flag in FLAGS.items():
        cmd += [flag, str(s[k])]
    if not s['captions']:
        cmd.append('--no-captions')
    if s['verbose']:  # log every 10 s chat count so /status shows what chat is doing
        cmd.append('--verbose')
    return cmd


def kill_tree(pid):
    if WIN:
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except Exception:  # noqa: BLE001
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:  # noqa: BLE001
                pass


def looks_like_watcher(pid):
    """True if this pid is a leftover clip_watcher (so we never kill an unrelated process that reused the pid)."""
    try:
        if WIN:
            out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'], capture_output=True, text=True).stdout
            return 'python' in out.lower()
        return Path(WATCHER).name.encode() in Path(f'/proc/{pid}/cmdline').read_bytes()
    except Exception:  # noqa: BLE001
        return False


def kill_leftover(state):
    """If the bot was killed or closed, its watcher keeps running alone. Stop it before starting a new one."""
    pid = state.get('pid')
    if pid and looks_like_watcher(pid):
        log(f'stopping a leftover watcher (pid {pid})')
        kill_tree(pid)
        time.sleep(2)
    state['pid'] = None


# ---------- Running clip_watcher ----------
class Manager:
    def __init__(self, tg, state):
        self.tg, self.state = tg, state
        self.proc, self.started, self.crashes = None, None, []
        self.lock = threading.RLock()

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, url=None):
        with self.lock:
            if url:
                self.state['url'] = url
            if not self.state['url']:
                return False
            self.stop_process()
            OUT.mkdir(parents=True, exist_ok=True)
            logf = open(WATCHER_LOG, 'a', encoding='utf-8', errors='replace')
            logf.write(f'\n===== {time.strftime("%Y-%m-%d %H:%M:%S")} start {self.state["url"]} =====\n')
            logf.flush()
            kw = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if WIN else {'start_new_session': True}
            self.proc = subprocess.Popen(build_cmd(self.state['url'], self.state['settings']), cwd=HERE,
                                         stdout=logf, stderr=subprocess.STDOUT, env=os.environ.copy(), **kw)
            self.started = time.time()
            self.state['pid'] = self.proc.pid
            self.state['desired'] = True
            save_state(self.state)
            return True

    def stop_process(self):
        p = self.proc
        if p is None or p.poll() is not None:
            self.proc = None
            return
        try:  # ask clip_watcher to stop cleanly so it also stops its streamlink child
            if WIN:
                p.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(p.pid, signal.SIGINT)
            p.wait(timeout=20)
        except Exception:  # noqa: BLE001
            if WIN:
                subprocess.run(['taskkill', '/PID', str(p.pid), '/T', '/F'], capture_output=True)
            else:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except Exception:  # noqa: BLE001
                    p.kill()
        self.proc = None

    def stop(self):
        with self.lock:
            self.state['desired'] = False
            save_state(self.state)
            self.stop_process()

    def supervise(self):
        """If it should be running but isn't, restart it. Give up after 5 crashes in 10 minutes."""
        while True:
            time.sleep(5)
            with self.lock:
                if not self.state['desired'] or self.running():
                    continue
                now = time.time()
                self.crashes = [t for t in self.crashes if now - t < 600] + [now]
                if len(self.crashes) > 5:
                    self.state['desired'] = False
                    save_state(self.state)
                    self.tg.send('clip_watcher keeps crashing, so I stopped it. Check /status, then /watch again.')
                    continue
                self.tg.send('clip_watcher stopped unexpectedly. Restarting in 10 s.')
                time.sleep(10)
                self.start()


def tail(path, n=6):
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4000))
            lines = f.read().decode('utf-8', 'replace').splitlines()
    except Exception:  # noqa: BLE001
        return 'no log yet'
    lines = [l for l in lines if l.strip()]
    return '\n'.join(lines[-n:])[-1200:] if lines else 'no log yet'


def free_gb():
    OUT.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(OUT).free / 1e9


def recordings():
    return sorted((OUT / 'recordings').glob('*.ts'), key=lambda p: p.stat().st_mtime) if (OUT / 'recordings').exists() else []


def status(m):
    s = m.state
    lines = []
    if m.running():
        up = int(time.time() - m.started)
        lines.append(f'RUNNING for {up // 3600}h {up % 3600 // 60}m on {s["url"]}')
    else:
        lines.append('STOPPED' + (f' (wanted: {s["url"]})' if s['desired'] else ''))
    recs = recordings()
    if recs:
        lines.append(f'latest recording: {recs[-1].name} ({recs[-1].stat().st_size / 1e9:.2f} GB)')
    lines.append(f'disk free: {free_gb():.1f} GB')
    lines.append(f'clips so far: {len(list(CLIPS.glob("*.mp4"))) if CLIPS.exists() else 0}')
    lines.append('--- last log lines ---')
    lines.append(tail(WATCHER_LOG))
    return '\n'.join(lines)


HELP = '''/watch LINK   start (YouTube live link or Twitch channel name/link)
/stop   stop watching
/status   what is happening now
/clips [n]   send the last n clips (default 1)
/disk   free disk space
/clean   delete finished recordings to free space
/settings   show settings
/set NAME VALUE   change a setting, then /restart
/restart   restart with the current settings
/id   show your chat id'''


def handle(m, tg, text):
    parts = text.strip().split()
    cmd = parts[0].lower().split('@')[0]
    args = parts[1:]
    s = m.state
    if cmd in ('/start', '/help'):
        tg.send(HELP)
    elif cmd == '/watch':
        if not args:
            return tg.send('Usage: /watch LINK')
        url = normalise(args[0])
        m.start(url)
        tg.send(f'Started watching {url}\nIt waits until the stream is live, then records and clips. /status to check.')
    elif cmd == '/stop':
        m.stop()
        tg.send('Stopped.')
    elif cmd == '/restart':
        tg.send('Restarting.' if m.start() else 'Nothing to restart. Use /watch LINK first.')
    elif cmd == '/status':
        tg.send(status(m))
    elif cmd == '/disk':
        tg.send(f'{free_gb():.1f} GB free')
    elif cmd == '/clean':
        recs = recordings()
        if m.running() and recs:
            recs = recs[:-1]  # never touch the file being written
        freed = 0
        for p in recs:
            try:
                freed += p.stat().st_size
                p.unlink()
            except Exception:  # noqa: BLE001
                pass
        tg.send(f'Deleted {len(recs)} recordings, freed {freed / 1e9:.2f} GB. Free now: {free_gb():.1f} GB')
    elif cmd == '/clips':
        n = min(int(args[0]), 5) if args and args[0].isdigit() else 1
        files = sorted(CLIPS.glob('*.mp4'), key=lambda p: p.stat().st_mtime)[-n:] if CLIPS.exists() else []
        files = [f for f in files if not f.name.endswith('.raw.mp4')]
        if not files:
            tg.send('No clips yet.')
        for f in files:
            send_clip(tg, f)
    elif cmd == '/settings':
        tg.send('\n'.join(f'{k} = {v}' for k, v in s['settings'].items()))
    elif cmd == '/set':
        if len(args) != 2 or args[0] not in KINDS:
            return tg.send('Usage: /set NAME VALUE. Names: ' + ', '.join(KINDS))
        k, v = args
        try:
            if KINDS[k] is bool:
                val = v.lower() in ('on', 'true', '1', 'yes')
            elif KINDS[k] is str:
                if not re.fullmatch(r'[A-Za-z0-9._-]{1,20}', v):
                    raise ValueError
                val = v
            else:
                val = KINDS[k](v)
        except ValueError:
            return tg.send(f'Bad value for {k}.')
        s['settings'][k] = val
        save_state(s)
        tg.send(f'{k} = {val}. Send /restart to apply.')
    else:
        tg.send('Unknown command. /help')


def send_clip(tg, path):
    size = path.stat().st_size
    if size > 49 * 1024 * 1024:
        return tg.send(f'Clip {path.name} is {size / 1e6:.0f} MB, too big for Telegram. It is on the laptop in output/clips.')
    tg.send_video(path, path.name)


def clip_sender(tg, state):
    """Send each new finished clip. A clip counts as finished when it has not changed for 10 s."""
    sent = set(state.get('sent') or [])
    if 'sent' not in state:  # first run: do not flood the phone with old test clips
        sent = {p.name for p in CLIPS.glob('*.mp4')} if CLIPS.exists() else set()
        state['sent'] = sorted(sent)
        save_state(state)
    while True:
        time.sleep(5)
        if not CLIPS.exists():
            continue
        for p in sorted(CLIPS.glob('*.mp4'), key=lambda p: p.stat().st_mtime):
            if p.name in sent or p.name.endswith('.raw.mp4') or time.time() - p.stat().st_mtime < 10:
                continue
            send_clip(tg, p)
            sent.add(p.name)
            state['sent'] = sorted(sent)
            save_state(state)


def disk_watch(tg):
    last = 0
    while True:
        time.sleep(60)
        if free_gb() < 10 and time.time() - last > 1800:
            last = time.time()
            tg.send(f'LOW DISK: only {free_gb():.1f} GB free. Send /clean to delete finished recordings.')


def keep_awake():
    if WIN:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED


def main():
    load_env()
    token, chat = os.environ.get('BOT_TOKEN'), os.environ.get('CHAT_ID')
    if not token:
        sys.exit('BOT_TOKEN missing. Copy bot.env.example to bot.env and fill it in.')
    OUT.mkdir(parents=True, exist_ok=True)
    tg = Telegram(token, chat)
    state = load_state()
    m = Manager(tg, state)
    keep_awake()
    kill_leftover(state)
    for fn, a in ((m.supervise, ()), (clip_sender, (tg, state)), (disk_watch, (tg,))):
        threading.Thread(target=fn, args=a, daemon=True).start()
    if chat:
        tg.send('Bot is online on the laptop.' + (f'\nResuming {state["url"]}' if state['desired'] else ''))
        if state['desired'] and state['url']:
            m.start()
    else:
        log('CHAT_ID not set yet: send /id to the bot and put the number in bot.env')
    log('bot running')
    offset = None
    try:
        loop(tg, m, chat, offset)
    except KeyboardInterrupt:
        log('bot stopping')
    finally:
        m.stop_process()


def loop(tg, m, chat, offset):
    while True:
        updates = tg.call('getUpdates', http_timeout=45, offset=offset, timeout=30)
        if updates is None:
            time.sleep(5)
            continue
        for u in updates:
            offset = u['update_id'] + 1
            msg = u.get('message') or {}
            text, cid = msg.get('text') or '', str((msg.get('chat') or {}).get('id', ''))
            if not text:
                continue
            if text.split()[0].lower().split('@')[0] == '/id':
                tg.send(f'Your chat id is {cid}', chat_id=cid)
            elif chat and cid == str(chat):
                try:
                    handle(m, tg, text)
                except Exception as e:  # noqa: BLE001 - one bad command must never kill the bot
                    tg.send(f'Error: {type(e).__name__}')
            # anyone else is ignored


if __name__ == '__main__':
    main()
