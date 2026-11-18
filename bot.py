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
import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
OUT = HERE / 'output'
CLIPS = OUT / 'clips'
STATE_FILE = OUT / 'bot_state.json'
WATCHER_LOG = OUT / 'watcher.log'
WATCHER = os.environ.get('BOT_WATCHER_SCRIPT') or str(HERE / 'clip_watcher.py')
WIN = os.name == 'nt'
POLL_SECONDS = float(os.environ.get('BOT_POLL_SECONDS', '45'))
STREAMLINK = os.environ.get('BOT_STREAMLINK_CMD', '').split() or [sys.executable, '-m', 'streamlink_cli']
UPDATE_FILES = ('bot.py', 'clip_watcher.py', 'requirements.txt', 'yt_auth.py')
YT_TOKEN = HERE / 'yt_token.json'
YT_DAILY_LIMIT = 5  # one upload costs ~1600 of the 10,000 daily API quota units
BAD_WORDS = ('fuck', 'shit', 'bitch', 'asshole', 'dick', 'cunt', 'bastard', 'nigga', 'nigger')
CURRENT_STATE = {}
BACKUP = HERE / 'backup'
TG_LIMIT = 48 * 1024 * 1024  # Telegram bots can upload up to 50 MB

DEFAULTS = {'ratio': 2.5, 'min_msgs': 20, 'window': 10.0, 'quality': '720p60', 'model': 'tiny',
            'api_poll': 10.0, 'gap': 60.0, 'length': 30.0, 'before': 15.0, 'captions': True, 'verbose': True, 'topic': 'GTA_6', 'crop': False, 'audio': True, 'audio_db': 7.0}
KINDS = {'ratio': float, 'min_msgs': int, 'window': float, 'quality': str, 'model': str,
         'api_poll': float, 'gap': float, 'length': float, 'before': float, 'captions': bool, 'verbose': bool, 'topic': str, 'crop': bool, 'audio': bool, 'audio_db': float}
FLAGS = {'ratio': '--ratio', 'min_msgs': '--min-msgs', 'window': '--window', 'quality': '--quality',
         'model': '--model', 'api_poll': '--api-poll', 'gap': '--gap', 'length': '--length', 'before': '--before', 'audio_db': '--audio-db'}


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
        self.viewers = []

    def call(self, method, http_timeout=40, files=None, _retry=True, **params):
        try:
            r = self.http.post(self.url + method, data=params, files=files, timeout=http_timeout)
            j = r.json()
            wait = (j.get('parameters') or {}).get('retry_after') if not j.get('ok') else None
            if wait and _retry:  # Telegram says "too many requests": wait as told and try once more
                time.sleep(min(int(wait), 30) + 1)
                for f in (files or {}).values():
                    f.seek(0)
                return self.call(method, http_timeout, files, _retry=False, **params)
            return j.get('result') if j.get('ok') else None
        except Exception as e:  # noqa: BLE001 - print only the type: the message holds the URL with the token
            log(f'telegram {method} failed: {type(e).__name__}')
            return None

    def send(self, text, chat_id=None, html=False):
        for i in range(0, max(len(text), 1), 4000):
            extra = {'parse_mode': 'HTML'} if html else {}
            self.call('sendMessage', chat_id=chat_id or self.chat_id, text=text[i:i + 4000], **extra)

    def send_video(self, path, caption='', only=None, markup=None):
        """Send a clip to the owner and every friend (or just `only`). Uploaded once; the others get it by file_id.
        Returns True if the first recipient really got it."""
        targets = [only] if only else [self.chat_id] + list(self.viewers)
        file_id, first_ok = None, False
        extra = {'reply_markup': markup} if markup else {}
        for i, t in enumerate(targets):
            if file_id:
                r = self.call('sendVideo', chat_id=t, video=file_id, caption=caption[:900], **extra)
            else:
                with open(path, 'rb') as f:
                    r = self.call('sendVideo', http_timeout=300, files={'video': f}, chat_id=t,
                                  caption=caption[:900], supports_streaming='true', width=1080, height=1920, **extra)
                if isinstance(r, dict):
                    file_id = (r.get('video') or {}).get('file_id')
            if i == 0:
                first_ok = bool(r)
        return first_ok

    def send_all(self, text, only=None, html=False):
        for t in ([only] if only else [self.chat_id] + list(self.viewers)):
            self.send(text, chat_id=t, html=html)

    def broadcast(self, text):
        for t in [self.chat_id] + list(self.viewers):
            self.send(text, chat_id=t)


class Replier:
    """Answers one specific chat, so a friend's /status goes to the friend and not to the owner."""

    def __init__(self, tg, cid):
        self.tg, self.cid = tg, cid

    def send(self, text, chat_id=None):
        self.tg.send(text, chat_id=self.cid)

    def send_video(self, path, caption='', markup=None):
        self.tg.send_video(path, caption, only=self.cid, markup=markup)

    def send_all(self, text, html=False):
        self.tg.send(text, chat_id=self.cid, html=html)


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
    s.setdefault('channel', None)
    s.setdefault('viewers', [])
    s.setdefault('admins', [])
    s.setdefault('uploaded', {})
    s.setdefault('uploads', {'day': '', 'n': 0})
    s.setdefault('credit', None)
    s.setdefault('permission', False)
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


def probe_live(url):
    """Ask Streamlink (no API quota) whether this URL is live right now. Returns {'id', 'title'} or None."""
    try:
        p = subprocess.run(STREAMLINK + ['--json', url], capture_output=True, text=True, timeout=45)
        d = json.loads(p.stdout)
    except Exception:  # noqa: BLE001
        return None
    meta = d.get('metadata') or {}
    if d.get('error') or not d.get('streams') or not meta.get('id'):
        return None
    return {'id': meta['id'], 'title': meta.get('title') or '', 'author': meta.get('author') or ''}


def channel_url(arg):
    arg = arg.strip().strip('<>')
    if arg.startswith('@'):
        return f'https://www.youtube.com/{arg}/live'
    if not arg.startswith('http'):
        arg = 'https://' + arg
    return arg if arg.rstrip('/').endswith('/live') else arg.rstrip('/') + '/live'


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
    if s.get('crop'):  # fill the whole vertical frame instead of a small band over a blurred background
        cmd.append('--crop')
    if not s.get('audio', True):
        cmd.append('--no-audio')
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
def write_credit_file(author):
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        f = OUT / 'credit.txt'
        f.write_text(author, encoding='utf-8') if author else f.unlink(missing_ok=True)
    except OSError:
        pass


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
            threading.Thread(target=self._fill_credit, args=(self.state['url'],), daemon=True).start()
            self.state['pid'] = self.proc.pid
            self.state['desired'] = True
            save_state(self.state)
            return True

    def _fill_credit(self, url):
        c = self.state.get('credit') or {}
        if c.get('url') == url and c.get('author'):
            return
        info = probe_live(url) or {}
        if info.get('author') or not c:
            self.state['credit'] = {'author': info.get('author', ''), 'url': url}
            write_credit_file(info.get('author', ''))

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

    def channel_tick(self, ch):
        """Channel mode: wait for the channel to go live, record that stream, go back to waiting when it ends."""
        now = time.time()
        if now - getattr(self, 'last_poll', 0) < POLL_SECONDS:
            return
        self.last_poll = now
        if self.running():
            if probe_live(self.state['url']):
                self.misses = 0
            else:
                self.misses = getattr(self, 'misses', 0) + 1
                if self.misses >= 3:
                    self.misses = 0
                    self.stop_process()
                    self.state['url'] = None
                    save_state(self.state)
                    self.tg.broadcast('The stream ended. Waiting for the next live on this channel.')
            return
        info = probe_live(ch)
        if info:
            url = f'https://www.youtube.com/watch?v={info["id"]}'
            self.tg.broadcast(f'LIVE now: {info["title"][:80]}\nRecording and clipping {url}')
            self.state['credit'] = {'author': info.get('author', ''), 'url': url}
            self.start(url)

    def supervise(self):
        """If it should be running but isn't, restart it. Give up after 5 crashes in 10 minutes."""
        while True:
            time.sleep(5)
            with self.lock:
                if not self.state['desired']:
                    continue
                if self.state.get('channel'):
                    self.channel_tick(self.state['channel'])
                    continue
                if self.running():
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
    elif s['desired'] and s.get('channel'):
        lines.append(f'WAITING for {s["channel"]} to go live (checks every {POLL_SECONDS:.0f} s)')
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
/waitfor @channel   wait for that YouTube channel to go live, then record and clip it by itself
/stop   stop watching
/status   what is happening now
/clips [n]   send the last n clips (default 1)
/disk   free disk space
/clean   delete finished recordings to free space\n/clearclips [hours]   delete old clips (default: older than 1 hour)
Tap the ⬆ button under a clip to upload it to YouTube as Private (owner and admins)
/settings   show settings
/set NAME VALUE   change a setting (audio_db = how loud a moment must be; audio off = chat only)
/clipnow [seconds]   cut the last 25 s (or that many) of what is being recorded right now
/permission yes|no   say whether the streamer allowed clips (default no)
/restart   restart with the current settings
/update   download the latest code from GitHub and restart
/rollback   undo the last /update
/ytauth   connect your YouTube channel for the upload button (sign-in from your phone)
/addfriend CHATID   let a friend receive clips and see status (they send /start and /id first)
/addadmin CHATID   same, plus every command except adding or removing people
/removefriend CHATID   remove a friend
/friends   list friends
/id   show your chat id'''


FRIEND_CMDS = ('/start', '/help', '/status', '/clips', '/disk')
FRIEND_HELP = '''You can receive the clips here and use:
/status   what is happening now
/clips [n]   send the last n clips (default 1)
/disk   free disk space
Only the owner can start, stop or change things.'''


OWNER_ONLY = ('/ytauth', '/ytcode', '/addfriend', '/removefriend', '/addadmin', '/removeadmin', '/friends')


def handle(m, tg, text, role='owner'):
    shared = re.search(r'http://localhost:\d+/\S+', text)
    if shared and role == 'owner':  # the address shared straight from the browser's Share button
        return yt_auth_finish(tg, shared.group(0))
    parts = text.strip().split()
    cmd = parts[0].lower().split('@')[0]
    args = parts[1:]
    s = m.state
    if (role == 'friend' and cmd not in FRIEND_CMDS) or (role != 'owner' and cmd in OWNER_ONLY):
        return tg.send('Only the owner can do that.')
    if cmd in ('/start', '/help'):
        tg.send(FRIEND_HELP if role == 'friend' else HELP)
    elif cmd == '/ytauth':
        yt_auth_start(tg)
    elif cmd == '/ytcode':
        yt_auth_finish(tg, args[0] if args else '')
    elif cmd == '/permission':
        s['permission'] = bool(args) and args[0].lower() in ('yes', 'on', 'haan', '1')
        save_state(s)
        tg.send('Permission for this stream: ' + ('YES, you may make clips public' if s['permission'] else 'NO, keep everything Private'))
    elif cmd == '/addfriend':
        if len(args) != 1 or not re.fullmatch(r'-?\d{5,15}', args[0]):
            return tg.send('Usage: /addfriend CHATID (your friend sends /id to this bot to get it, and must send /start first)')
        if args[0] not in s['viewers']:
            s['viewers'].append(args[0])
            save_state(s)
        tg.viewers = list(s['viewers'])
        tg.send(f'Added {args[0]}. They now get every clip and can use /status, /clips, /disk.')
        tg.send('You were added to receive clips. Send /help to see what you can do.', chat_id=args[0])
    elif cmd == '/addadmin':
        if len(args) != 1 or not re.fullmatch(r'-?\d{5,15}', args[0]):
            return tg.send('Usage: /addadmin CHATID (they send /start and /id to this bot first)')
        for lst in ('viewers', 'admins'):
            if args[0] not in s[lst]:
                s[lst].append(args[0])
        save_state(s)
        tg.viewers = list(s['viewers'])
        tg.send(f'Added {args[0]} as admin: gets every clip and can use every command except adding or removing people.')
        tg.send('You are now an admin of this bot. Send /help to see the commands.', chat_id=args[0])
    elif cmd == '/removeadmin':
        s['admins'] = [v for v in s['admins'] if v not in args]
        save_state(s)
        tg.send('Admin rights removed (they still receive clips as a friend). /removefriend removes them completely.')
    elif cmd == '/removefriend':
        s['viewers'] = [v for v in s['viewers'] if v not in args]
        s['admins'] = [v for v in s['admins'] if v not in args]
        save_state(s)
        tg.viewers = list(s['viewers'])
        tg.send('Removed.')
    elif cmd == '/friends':
        tg.send('Friends: ' + (', '.join(v + (' (admin)' if v in s['admins'] else '') for v in s['viewers']) or 'none'))
    elif cmd == '/watch':
        if not args:
            return tg.send('Usage: /watch LINK')
        url = normalise(args[0])
        s['channel'], s['permission'] = None, False
        s['credit'] = {'author': '', 'url': url}
        threading.Thread(target=lambda: s.__setitem__('credit', {'author': (probe_live(url) or {}).get('author', ''), 'url': url}), daemon=True).start()
        m.start(url)
        tg.send(f'Started watching {url}\nIt waits until the stream is live, then records and clips. /status to check.')
    elif cmd == '/waitfor':
        if not args:
            return tg.send('Usage: /waitfor @channelhandle')
        m.stop_process()
        s['channel'], s['url'], s['desired'] = channel_url(args[0]), None, True
        save_state(s)
        m.last_poll = 0
        tg.send(f'Waiting for {s["channel"]} to go live. I will start by myself and tell you.')
    elif cmd == '/stop':
        m.stop()
        tg.send('Stopped.')
    elif cmd == '/clipnow':
        back = min(max(int(args[0]), 10), 60) if args and args[0].isdigit() else 25
        threading.Thread(target=clip_now, args=(tg, m, back), daemon=True).start()
    elif cmd == '/restart':
        tg.send('Restarting.' if m.start() else 'Nothing to restart. Use /watch LINK first.')
    elif cmd == '/status':
        tg.send(f'code {code_version()}\n' + status(m))
    elif cmd == '/update':
        do_update(tg, m)
    elif cmd == '/rollback':
        do_rollback(tg, m)
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
    elif cmd == '/clearclips':
        clear_clips(tg, int(args[0]) if args and args[0].isdigit() else 1)
    elif cmd == '/clips':
        n = min(int(args[0]), 5) if args and args[0].isdigit() else 1
        files = sorted(CLIPS.glob('*.mp4'), key=lambda p: p.stat().st_mtime)[-n:] if CLIPS.exists() else []
        files = [f for f in files if '.raw.' not in f.name and '.tmp.' not in f.name]
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


def shrink_for_telegram(path):
    """Re-encode a clip that is too big for Telegram. Tries smaller targets until it fits. Returns a temp copy."""
    dur = float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(path)],
                               capture_output=True, text=True, check=True).stdout.strip())
    tmpdir = OUT / 'tg_tmp'
    tmpdir.mkdir(parents=True, exist_ok=True)
    out = tmpdir / path.name
    for target_mb in (36, 26, 16):
        vbr = max(200_000, int(target_mb * 1024 * 1024 * 8 / dur) - 128_000)
        subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(path), '-c:v', 'libx264', '-preset', 'veryfast',
                        '-b:v', str(vbr), '-maxrate', str(vbr), '-bufsize', str(vbr * 2), '-pix_fmt', 'yuv420p',
                        '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', str(out)], check=True)
        if out.stat().st_size <= TG_LIMIT:
            return out
    raise RuntimeError('still too big')


def read_transcript(path):
    """The words Whisper heard in this clip, from the .srt that clip_watcher filed away."""
    srt = path.parent / 'captions' / (path.stem + '.srt')
    try:
        lines = srt.read_text(encoding='utf-8').splitlines()
    except OSError:
        return ''
    words = [l.strip() for l in lines if l.strip() and not l.strip().isdigit() and '-->' not in l
             and not l.strip().startswith('{\\')]  # the on-screen credit cue is not speech
    return re.sub(r'\{[^}]*\}', '', ' '.join(words)).strip()


def clean_text(text):
    """Mask swear words so they do not end up in a public title."""
    def mask(m):
        w = m.group(0)
        return w[0] + '*' * (len(w) - 1)
    return re.sub(r'(?i)\b\w*(' + '|'.join(BAD_WORDS) + r')\w*\b', mask, text)


KEYWORDS = [  # (words to look for, emoji, label shown in the title, extra hashtags)
    (('police', 'cop', 'cops', 'wanted', 'arrest', 'chase'), '🚓', 'Police Chase', ['GTA6Police', 'GTA6Chase']),
    (('heist', 'rob', 'robbery', 'bank', 'money', 'cash', 'steal'), '💰', 'Heist Moment', ['GTA6Heist']),
    (('shotgun', 'gun', 'shoot', 'weapon', 'bullet', 'longarm', 'rifle'), '🔫', 'Weapons', ['GTA6Weapons']),
    (('jason',), '🕶️', 'Jason', ['Jason', 'GTA6Jason']),
    (('lucia',), '👩', 'Lucia', ['Lucia', 'GTA6Lucia']),
    (('vice city', 'miami', 'beach', 'leonida', 'florida', 'keys'), '🌴', 'Vice City', ['Leonida', 'GTA6ViceCity']),
    (('car', 'drive', 'driving', 'race', 'speed', 'engine'), '🏎️', 'Car Moment', ['GTA6Cars']),
    (('bike', 'motorcycle', 'helicopter', 'plane', 'boat', 'jet'), '🏍️', 'Ride', ['GTA6Vehicles']),
    (('fight', 'punch', 'kill', 'dead', 'die', 'explosion', 'blast'), '💥', 'Action Scene', ['GTA6Action']),
    (('funny', 'lol', 'haha', 'laugh', 'joke'), '😂', 'Funny Moment', ['GTA6Funny']),
    (('wow', 'no way', 'oh my god', 'omg', 'insane', 'crazy', 'unbelievable', 'what the'), '😱', 'Reaction', ['GTA6Reaction']),
]
CORE_TAGS = ['GTA6', 'Shorts', 'GTAVI', 'GrandTheftAutoVI', 'GTA6Gameplay', 'GTA6Launch', 'GTA6Release', 'ViceCity', 'Rockstar', 'Gaming']
STOP = {'it', "it's", 'the', 'a', 'an', 'and', 'but', 'so', 'no', 'i', "i'm", 'to', 'of', 'is', 'in', 'on', 'that', 'this', 'you', 'we', 'was',
        'with', 'for', 'or', 'oh', 'um', 'uh', 'he', 'she', 'they', 'me', 'my', 'at', 'be', 'are', 'do', 'if', 'what', 'just',
        'before', 'after', 'because', 'when', 'while', 'then', 'than', 'there', 'here', 'out', 'up', 'from', 'about', 'into',
        'get', 'need', 'want', 'going', 'gonna', 'got', 'have', 'has', 'can', 'will', 'would', 'should'}


def cut_words(text, limit):
    """Longest start of `text` that fits `limit` characters, ending on a whole word."""
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(' ', 1)[0].rstrip(' ,.-:;')


def make_caption(path, state):
    import random
    rng = random.Random(path.name)  # same clip -> same text, different clips -> different wording
    topic = state['settings'].get('topic', 'GTA_6').replace('_', ' ')
    tag = re.sub(r'\W', '', topic) or 'Clips'
    raw = clean_text(read_transcript(path))
    low = raw.lower()
    words = raw.split()
    first = re.split(r'(?<=[.!?])\s+', raw.strip())[0] if raw.strip() else ''
    head = (first.split() if len(first.split()) >= 3 else words)[:8]
    while head and head[-1].lower().strip(' ,.!?-') in STOP:
        head.pop()
    meaningful = [w for w in head if w.lower().strip(' ,.!?-') not in STOP]
    hook = ' '.join(head).strip(' ,.-').capitalize() if len(meaningful) >= 3 else ''
    hook = re.sub(r'([.!?]\s+)([a-z])', lambda m: m.group(1) + m.group(2).upper(), hook)
    if '*' in hook:  # a masked swear word makes a poor, risky title: use a clean one
        hook = ''
    emo, label, extra = '🔥', '', []
    for keys, e, lab, tags in KEYWORDS:
        if any(k in low for k in keys):
            emo, label, extra = e, lab, tags
            break
    e2 = '🔥' if emo == '😱' else '😱'
    # ----- title: hook + emojis + what it is + launch day angle
    if hook and label:
        opts = [f'{hook} {emo}{e2} | GTA 6 {label}', f'GTA 6 Day 1: {label} {emo} {hook}', f'{hook}?! {emo}{e2} {topic} Launch Day']
    elif hook:
        opts = [f'{hook} 😱🔥 | {topic} Launch Day', f'{topic} Day 1 😱 {hook}', f'{hook}?! 🤯 {topic}']
    elif label:
        opts = [f'{topic} {label} {emo}{e2} Launch Day Moment', f'Ye {topic} {label} dekha? {emo}{e2}', f'{topic} Day 1: {label} {emo} Crazy!']
    else:
        opts = [f'{topic} Launch Day pe chat ka reaction 😱🔥', f'Chat went wild 🔥 {topic} Day 1 moment 😱', f'{topic} Launch Day: dekho kya hua 😳🔥']
    title = cut_words(rng.choice(opts), 95)
    # ----- hashtags (YouTube shows the first three above the title)
    tags_all = []
    for t in [CORE_TAGS[0], CORE_TAGS[1], CORE_TAGS[2]] + extra + CORE_TAGS[3:]:
        if t not in tags_all:
            tags_all.append(t)
    yt_tags = ' '.join('#' + t for t in tags_all[:7])
    ig_tags = ' '.join('#' + t for t in [t for t in tags_all if t != 'Shorts'][:4]) + ' #Reels'
    credit = state.get('credit') or {}
    who = credit.get('author') or '(add the streamer name)'
    src = f' ({credit["url"]})' if credit.get('url') else ''
    quote = f'🎙️ Moment: "{cut_words(raw, 120)}"\n' if len(meaningful) >= 3 else ''
    what = f'{emo} {label}\n' if label else ''
    desc = (f'{title}\n\n'
            f'{topic} Launch Day ka ek aur crazy moment! 🔥😱 Aapko kaisa laga? Comment karke batao 👇\n\n'
            f'{what}{quote}'
            f'👍 Like karo • 🔔 Subscribe karo roz ke {topic} clips aur reactions ke liye • 💬 Apna reaction comment me likho\n\n'
            f'🎬 Source: {who}{src}\n'
            f'Not affiliated with Rockstar Games or Take-Two. Fan clip: bolo toh turant hata dunga, DM karo.\n\n'
            f'{yt_tags}')
    ig_hook = hook or label or 'Chat went wild'
    ig = (f'{ig_hook} {emo}{e2}\n'
          f'{topic} Launch Day moment 😱 Aapko kaisa laga? Comment karo 👇\n'
          f'Follow karo roz ke {topic} clips ke liye 🎮\n'
          f'Credit: {who}\n\n'
          f'{ig_tags}')
    tags_list = [topic, 'GTA VI', 'GTA 6 gameplay', 'GTA 6 launch day', label or 'GTA 6 reaction', 'Shorts', 'Gaming']
    q = f'{label} ka ye scene aapko kaisa laga? 1 se 10 rating do 👇🔥' if label else f'{topic} Launch Day ka ye moment aapko kaisa laga? Comment karo 👇🔥'
    comment = f'{q}\nRoz ke {topic} clips ke liye follow/subscribe karo 🎮'
    replies = ['Thanks bhai 🔥 aur clips ke liye follow karo!', 'Agla clip isse bhi crazy hoga, bane raho 😎', 'Source stream ka link description me hai 🎮 Credit unhi ko']
    return {'title': title, 'description': desc, 'ig': ig, 'tags': tags_list, 'comment': comment, 'replies': replies}


def clip_quality(path):
    """Look at the finished clip: how much of it is black, and is there any sound. Never raises."""
    info = {'black': 0.0, 'silent': False}
    try:
        dur = float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(path)],
                                   capture_output=True, text=True, timeout=30).stdout.strip() or 0)
        p = subprocess.run(['ffmpeg', '-hide_banner', '-nostats', '-i', str(path), '-vf', 'blackdetect=d=0.4:pic_th=0.95:pix_th=0.12',
                            '-af', 'volumedetect', '-f', 'null', '-'], capture_output=True, text=True, timeout=120)
        blk = sum(float(x) for x in re.findall(r'black_duration:([\d.]+)', p.stderr))
        info['black'] = min(1.0, blk / dur) if dur else 0.0
        m = re.search(r'mean_volume:\s*(-?[\d.]+) dB', p.stderr)
        info['silent'] = bool(m) and float(m.group(1)) < -55
    except Exception:  # noqa: BLE001
        pass
    return info


def spike_ratio(path):
    """What triggered this clip, from clips/spikes.csv: ('chat', ratio), ('audio', dB above normal), or None for a /clipnow clip."""
    try:
        for line in (CLIPS / 'spikes.csv').read_text(encoding='utf-8').splitlines():
            parts = line.split(',')
            if len(parts) >= 4 and parts[3].strip() == path.name:
                if len(parts) >= 6 and parts[4].strip() == 'audio':
                    return ('audio', float(parts[5]))
                return ('chat', int(parts[1]) / max(float(parts[2]), 1.0))
    except Exception:  # noqa: BLE001
        pass
    return None


def score_clip(path, info, cap):
    """1-10 and a plain-words verdict, so you can decide in two seconds."""
    sc, why = 5, []
    trig = spike_ratio(path)
    if trig and trig[0] == 'audio':
        sc += 2 if trig[1] >= 12 else 1
        why.append(f'loud moment +{trig[1]:.0f} dB')
    elif trig:
        sc += 2 if trig[1] >= 3 else (1 if trig[1] >= 2 else 0)
        why.append(f'chat spike {trig[1]:.1f}x')
    else:
        sc += 1
        why.append('picked by you')
    transcript = read_transcript(path)
    if len(transcript.split()) >= 6:
        sc += 1
        why.append('clear speech')
    else:
        sc -= 1
        why.append('little speech')
    if cap.get('title') and not any(w in cap['title'] for w in ('chat ka reaction', 'Chat went wild')):
        sc += 1
    if info.get('black', 0) > 0.3:
        sc -= 3
        why.append('mostly dark')
    if info.get('silent'):
        sc -= 2
        why.append('no sound')
    sc = max(1, min(10, sc))
    verdict = 'POST NOW 🔥' if sc >= 7 else ('okay, post if nothing better' if sc >= 5 else 'skip it, weak')
    return sc, f'{verdict} ({", ".join(why)})'


def clip_markup(path, cap):
    """Buttons under each clip: upload to YouTube (Private) and one-tap COPY of the title and the Instagram caption."""
    rows = []
    if YT_TOKEN.exists() and path.name not in CURRENT_STATE.get('uploaded', {}):
        rows.append([{'text': '⬆ YouTube (Private)', 'callback_data': 'yt:' + path.name}])
    rows.append([{'text': '📋 Title', 'copy_text': {'text': cap['title'][:256]}},
                 {'text': '📋 Instagram caption', 'copy_text': {'text': cap['ig'][:256]}}])
    return json.dumps({'inline_keyboard': rows})


def upload_youtube(path, cap):
    """Upload as PRIVATE (Google forces that for unaudited API projects anyway). Returns the video id."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    creds = Credentials.from_authorized_user_file(str(YT_TOKEN), ['https://www.googleapis.com/auth/youtube.upload'])
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        YT_TOKEN.write_text(creds.to_json())
    yt = build('youtube', 'v3', credentials=creds, cache_discovery=False)
    body = {'snippet': {'title': cap['title'], 'description': cap['description'][:4900], 'tags': cap['tags'], 'categoryId': '20'},
            'status': {'privacyStatus': 'private', 'selfDeclaredMadeForKids': False}}
    req = yt.videos().insert(part='snippet,status', body=body, media_body=MediaFileUpload(str(path), mimetype='video/mp4', resumable=True, chunksize=-1))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp['id']


def do_upload(tg, state, name, replier):
    path = CLIPS / name
    day = time.strftime('%Y-%m-%d', time.gmtime(time.time() - 8 * 3600))  # YouTube quota resets at midnight Pacific
    try:
        if state['uploads']['day'] != day:
            state['uploads'] = {'day': day, 'n': 0}
        if state['uploads']['n'] >= YT_DAILY_LIMIT:
            return replier.send(f'Daily upload limit reached ({YT_DAILY_LIMIT}): the YouTube quota allows about 6 uploads a day. Post this one by hand, or wait until tomorrow.')
        replier.send(f'Uploading {name} to YouTube as Private...')
        vid = upload_youtube(path, make_caption(path, state))
        state['uploads']['n'] += 1
        state['uploaded'][name] = vid
        save_state(state)
        tg.broadcast(f'Uploaded as PRIVATE: https://youtu.be/{vid}\nMake it public: https://studio.youtube.com/video/{vid}/edit\n(uploads today: {state["uploads"]["n"]}/{YT_DAILY_LIMIT})')
    except Exception as e:  # noqa: BLE001
        replier.send(f'YouTube upload failed ({type(e).__name__}: {str(e)[:150]}). Post this clip by hand.')


def handle_callback(tg, m, cb):
    cid = str((cb.get('message') or {}).get('chat', {}).get('id', ''))
    data = cb.get('data') or ''
    tg.call('answerCallbackQuery', callback_query_id=cb.get('id'))
    state = m.state
    is_owner = cid == str(tg.chat_id)
    if not (is_owner or cid in [str(a) for a in state['admins']]):
        return tg.send('Only the owner or an admin can upload.', chat_id=cid)
    replier = tg if is_owner else Replier(tg, cid)
    if not data.startswith('yt:'):
        return
    name = data[3:]
    if not re.fullmatch(r'[\w.-]+\.mp4', name) or not (CLIPS / name).exists():
        return replier.send('That clip is not on the laptop any more.')
    if name in state['uploaded']:
        return replier.send(f'Already uploaded: https://youtu.be/{state["uploaded"][name]}')
    threading.Thread(target=do_upload, args=(tg, state, name, replier), daemon=True).start()


def send_clip(tg, path, info=None):
    size = path.stat().st_size
    try:
        cap = make_caption(path, CURRENT_STATE)
    except Exception:  # noqa: BLE001
        cap = None
    markup = clip_markup(path, cap) if cap else None
    ok = True
    if size <= TG_LIMIT:
        ok = tg.send_video(path, path.name, markup=markup)
    else:
        tg.send(f'{path.name} is {size / 1e6:.0f} MB, too big for Telegram. Making a smaller copy...')
        try:
            small = shrink_for_telegram(path)
        except Exception as e:  # noqa: BLE001
            return tg.send(f'Could not shrink {path.name} ({type(e).__name__}). The full clip is on the laptop in output/clips.')
        try:
            ok = tg.send_video(small, path.name, markup=markup)
        finally:
            small.unlink(missing_ok=True)
    try:
        import html as H
        pre = lambda s: '<pre>' + H.escape(s) + '</pre>'  # Telegram: tap a code block to copy all of it
        head = ''
        if cap:
            sc, verdict = score_clip(path, info or {}, cap)
            perm = '✅ Creator permission: yes' if CURRENT_STATE.get('permission') else '🔒 NO PERMISSION YET: keep it PRIVATE'
            head = f'<b>⭐ {sc}/10, {H.escape(verdict)}</b>\n{perm}\n\n'
        tg.send_all(head + '<b>YOUTUBE TITLE</b> (tap to copy)\n' + pre(cap['title']) +
                    '\n<b>YOUTUBE DESCRIPTION</b> (tap to copy)\n' + pre(cap['description']) +
                    '\n<b>INSTAGRAM CAPTION</b> (tap to copy)\n' + pre(cap['ig']) +
                    '\n<b>FIRST COMMENT, post and pin it</b>\n' + pre(cap['comment']) +
                    '\n<b>REPLIES for comments</b>\n' + pre('\n'.join(cap['replies'])), html=True)
    except Exception as e:  # noqa: BLE001
        tg.send_all(f'(could not write a caption: {type(e).__name__})')
    return ok


PENDING_AUTH = {}



def yt_auth_start(tg):
    secret = HERE / 'client_secret.json'
    if not secret.exists():
        return tg.send('client_secret.json is not on the laptop yet.')
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), ['https://www.googleapis.com/auth/youtube.upload'])
    flow.redirect_uri = 'http://localhost:8080/'
    url, _ = flow.authorization_url(prompt='consent', access_type='offline')
    PENDING_AUTH['flow'], PENDING_AUTH['t'] = flow, time.time()
    tg.send('YouTube sign-in, on your phone:\n'
            '1) Tap the link in the NEXT message.\n'
            '2) Sign in with the channel account. Tap Continue (not Back to safety), then Continue or Allow.\n'
            '3) You reach a page that says "This site can\'t be reached". That is correct.\n'
            '4) Tap the Share icon at the top right, choose Telegram, choose this bot, tap Send. Nothing else to do.\n'
            'You have 15 minutes. (If the link will not open, long-press it, Copy, paste it in Chrome.)')
    tg.send(url)


def yt_auth_finish(tg, address):
    flow = PENDING_AUTH.get('flow')
    if not flow or time.time() - PENDING_AUTH.get('t', 0) > 900:
        return tg.send('No sign-in in progress (or it expired). Send /ytauth first.')
    if not address.startswith('http://localhost'):
        return tg.send('That is not the right address. It must start with http://localhost:8080/?state=...')
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'  # the redirect is http://localhost, on purpose
    try:
        flow.fetch_token(authorization_response=address)
    except Exception as e:  # noqa: BLE001
        return tg.send(f'Sign-in failed ({type(e).__name__}). Send /ytauth for a fresh link and try again.')
    YT_TOKEN.write_text(flow.credentials.to_json())
    PENDING_AUTH.clear()
    tg.send('YouTube is connected. New clips now have the ⬆ YouTube (Private) button. You can delete the message with the address.')


def latest_recording():
    recs = recordings()
    return recs[-1] if recs else None


def clip_now(tg, m, back):
    """Cut the last `back` seconds of the recording that is being written right now, and let the normal sender deliver it."""
    time.sleep(6)  # let the recording catch up a little
    rec = latest_recording()
    if rec is None:
        return tg.send('No recording yet. Start one with /watch.')
    try:
        dur = float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(rec)],
                                   capture_output=True, text=True, check=True).stdout.strip())
    except Exception:  # noqa: BLE001
        return tg.send('Could not read the recording yet. Try again in a few seconds.')
    s = m.state['settings']
    cmd = [sys.executable, WATCHER, '--file', str(rec), '--at', str(int(dur)), '--before', str(back), '--window', '0',
           '--length', str(back + 5), '--model', str(s['model']), '--out', str(OUT)]
    if not s['captions']:
        cmd.append('--no-captions')
    if s.get('crop'):
        cmd.append('--crop')
    tg.send(f'Cutting the last {back} s. It arrives in a minute or two.')
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=HERE)
    if p.returncode != 0:
        tg.send('Cutting failed: ' + (p.stderr or p.stdout)[-200:])


def clear_clips(tg, hours=1):
    """Delete finished clips (and their caption files) older than `hours`. Never touches a clip still being made."""
    if not CLIPS.exists():
        return tg.send('No clips folder yet.')
    limit = time.time() - hours * 3600
    n, freed = 0, 0
    for p in list(CLIPS.glob('*.mp4')) + list(CLIPS.glob('*.srt')) + list((CLIPS / 'captions').glob('*.srt')):
        if '.raw.' in p.name or '.tmp.' in p.name:
            continue
        try:
            if p.stat().st_mtime < limit:
                freed += p.stat().st_size
                p.unlink()
                n += 1
        except OSError:
            pass
    tg.send(f'Deleted {n} old files ({freed / 1e6:.0f} MB). Clips newer than {hours} h were kept.')


def code_version():
    h = hashlib.sha1()
    for n in ('bot.py', 'clip_watcher.py'):
        try:
            h.update((HERE / n).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:8]


def apply_files(files):
    """Write files next to bot.py, each through a temp file so a crash never leaves a half-written script."""
    for name, data in files.items():
        tmp = HERE / (name + '.new')
        tmp.write_bytes(data)
        tmp.replace(HERE / name)


def restart_after_change(tg, m, changed):
    """bot.py itself changed: exit so run_bot.bat starts the new one (it resumes the watch). Otherwise just restart the watcher."""
    if 'bot.py' in changed:
        if os.environ.get('CLIPBOT_SUPERVISED'):
            tg.send('Restarting the bot with the new code (back in ~15 s).')
            m.stop_process()
            os._exit(3)
        tg.send('bot.py changed, but I was not started by run_bot.bat, so restart me by hand (python bot.py).')
    elif m.state.get('desired'):
        m.start()
        tg.send('Watcher restarted with the new code.')


def do_update(tg, m):
    repo = os.environ.get('UPDATE_REPO', 'modhshubham3/gta-vi')
    branch = os.environ.get('UPDATE_BRANCH', 'ccr-cb619668-8pcm8x')
    url = os.environ.get('UPDATE_URL') or f'https://codeload.github.com/{repo}/zip/refs/heads/{branch}'
    headers = {'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN']} if os.environ.get('GITHUB_TOKEN') else {}
    try:
        r = requests.get(url, headers=headers, timeout=60)
    except Exception as e:  # noqa: BLE001
        return tg.send(f'Update failed: could not reach GitHub ({type(e).__name__}).')
    if r.status_code == 404:
        return tg.send('Update failed: 404. If the repo is private, put a read-only GITHUB_TOKEN in bot.env.')
    if r.status_code != 200:
        return tg.send(f'Update failed: HTTP {r.status_code}.')
    try:
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        new = {}
        for info in zf.infolist():
            parts = info.filename.split('/')
            if len(parts) == 2 and parts[1] in UPDATE_FILES:
                new[parts[1]] = zf.read(info)
        if 'bot.py' not in new or 'clip_watcher.py' not in new:
            return tg.send('Update failed: the download did not contain bot.py and clip_watcher.py. Nothing changed.')
        for name, data in new.items():
            if name.endswith('.py'):
                compile(data, name, 'exec')  # refuse code that does not even parse
    except Exception as e:  # noqa: BLE001
        return tg.send(f'Update refused, nothing changed: {type(e).__name__}.')
    changed = [n for n, d in new.items() if not (HERE / n).exists() or (HERE / n).read_bytes() != d]
    if not changed:
        return tg.send(f'Already up to date (code {code_version()}).')
    BACKUP.mkdir(exist_ok=True)
    for n in changed:
        if (HERE / n).exists():
            shutil.copy2(HERE / n, BACKUP / n)
    apply_files({n: new[n] for n in changed})
    note = ''
    if 'requirements.txt' in changed:
        p = subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', '-r', str(HERE / 'requirements.txt')], capture_output=True, text=True)
        note = '\npip install ok' if p.returncode == 0 else '\npip install FAILED, check the laptop'
    tg.send(f'Updated {", ".join(changed)} (code {code_version()}). /rollback undoes it.{note}')
    restart_after_change(tg, m, changed)


def do_rollback(tg, m):
    files = {p.name: p.read_bytes() for p in BACKUP.glob('*') if p.name in UPDATE_FILES} if BACKUP.exists() else {}
    if not files:
        return tg.send('No backup to roll back to.')
    apply_files(files)
    tg.send(f'Rolled back {", ".join(files)} (code {code_version()}).')
    restart_after_change(tg, m, list(files))


def clip_sender(tg, state):
    """Send each new finished clip. A clip counts as finished when it has not changed for 10 s."""
    sent = set(state.get('sent') or [])
    tries = {}
    if 'sent' not in state:  # first run: do not flood the phone with old test clips
        sent = {p.name for p in CLIPS.glob('*.mp4')} if CLIPS.exists() else set()
        state['sent'] = sorted(sent)
        save_state(state)
    while True:
        time.sleep(5)
        if not CLIPS.exists():
            continue
        for p in sorted(CLIPS.glob('*.mp4'), key=lambda p: p.stat().st_mtime):
            if p.name in sent or '.raw.' in p.name or '.tmp.' in p.name or time.time() - p.stat().st_mtime < 10:
                continue
            info = clip_quality(p)
            if info['black'] > 0.6:
                tg.send(f'Skipped {p.name}: it is mostly black. (/clips can still send it.)')
            else:
                if send_clip(tg, p, info) is False:
                    tries[p.name] = tries.get(p.name, 0) + 1
                    if tries[p.name] < 3:
                        continue  # try again in a few seconds
            sent.add(p.name)
            state['sent'] = sorted(sent)
            save_state(state)


def heartbeat(tg, m):
    """One short 'still alive' message every 30 minutes while a stream is being watched. No message = something is wrong."""
    while True:
        time.sleep(1800)
        if not m.running():
            continue
        up = int(time.time() - m.started)
        warn = '\n⚠ YouTube quota used up: auto-clips are off, use /clipnow.' if 'quota used up' in tail(WATCHER_LOG, 40) else ''
        n = len(list(CLIPS.glob('*.mp4'))) if CLIPS.exists() else 0
        tg.send(f'💓 alive • watching {up // 3600}h {up % 3600 // 60}m • clips {n} • disk {free_gb():.0f} GB{warn}')


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
    global CURRENT_STATE
    load_env()
    token, chat = os.environ.get('BOT_TOKEN'), os.environ.get('CHAT_ID')
    if not token:
        sys.exit('BOT_TOKEN missing. Copy bot.env.example to bot.env and fill it in.')
    OUT.mkdir(parents=True, exist_ok=True)
    tg = Telegram(token, chat)
    state = load_state()
    m = Manager(tg, state)
    CURRENT_STATE = state
    tg.viewers = [str(v) for v in state['viewers']]
    keep_awake()
    kill_leftover(state)
    for fn, a in ((m.supervise, ()), (clip_sender, (tg, state)), (disk_watch, (tg,)), (heartbeat, (tg, m))):
        threading.Thread(target=fn, args=a, daemon=True).start()
    if chat:
        tg.send(f'Bot is online on the laptop (code {code_version()}).' + (f'\nResuming {state["url"]}' if state['desired'] else ''))
        if state['desired'] and state['url'] and not state.get('channel'):
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
            if u.get('callback_query'):
                try:
                    handle_callback(tg, m, u['callback_query'])
                except Exception as e:  # noqa: BLE001
                    log(f'callback error: {type(e).__name__}')
                continue
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
            elif cid in [str(v) for v in m.state['viewers']]:
                try:
                    role = 'admin' if cid in [str(a) for a in m.state['admins']] else 'friend'
                    handle(m, Replier(tg, cid), text, role)
                except Exception as e:  # noqa: BLE001
                    tg.send(f'Error: {type(e).__name__}', chat_id=cid)
            # anyone else is ignored


if __name__ == '__main__':
    main()
