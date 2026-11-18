#!/usr/bin/env python3
"""One-time sign-in so the bot can upload to YOUR YouTube channel (as Private).

  python yt_auth.py            opens a browser on this computer
  python yt_auth.py --manual   no browser here: you open a link on ANY device (your phone is fine),
                               sign in, and paste back the address you end up on

It needs client_secret.json (an OAuth client of type "Desktop app", downloaded from Google Cloud) in this folder,
and writes yt_token.json next to it. Both files are secrets: never share or commit them.
"""
import os
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

HERE = Path(__file__).resolve().parent
SCOPES = ['https://www.googleapis.com/auth/youtube.upload']
REDIRECT = 'http://localhost:8080/'

secret = HERE / 'client_secret.json'
if not secret.exists():
    raise SystemExit('client_secret.json not found in this folder. Download it from Google Cloud (OAuth client, Desktop app).')
flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)

if '--manual' in sys.argv:
    flow.redirect_uri = REDIRECT
    url, _ = flow.authorization_url(prompt='consent', access_type='offline')
    print('\n1. Open this link on any device (your phone is fine) and sign in with the account that owns the channel:\n')
    print(url)
    print('\n2. Allow it. At the end the page will say it cannot be reached (localhost). That is expected.')
    print('3. Copy the FULL address from the address bar of that page (it starts with http://localhost:8080/?...code=...)')
    answer = input('\nPaste that address here and press Enter:\n> ').strip()
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'  # the redirect is http://localhost, on purpose
    flow.fetch_token(authorization_response=answer)
    creds = flow.credentials
else:
    creds = flow.run_local_server(port=0, prompt='consent')

(HERE / 'yt_token.json').write_text(creds.to_json())
print('Done. yt_token.json saved. Restart the bot; clips now carry an Upload button.')
