#!/usr/bin/env python3
"""One-time sign-in so the bot can upload to YOUR YouTube channel (as Private).

Run once on the laptop:   python yt_auth.py
It opens a browser: sign in with the Google account that owns the channel and allow "upload videos".
It needs client_secret.json (an OAuth client of type "Desktop app", downloaded from Google Cloud) in this folder,
and writes yt_token.json next to it. Both files are secrets: never share or commit them.
"""
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

HERE = Path(__file__).resolve().parent
SCOPES = ['https://www.googleapis.com/auth/youtube.upload']

secret = HERE / 'client_secret.json'
if not secret.exists():
    raise SystemExit('client_secret.json not found in this folder. Download it from Google Cloud (OAuth client, Desktop app).')
creds = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES).run_local_server(port=0, prompt='consent')
(HERE / 'yt_token.json').write_text(creds.to_json())
print('Done. yt_token.json saved. Restart the bot; clips now carry an Upload button.')
