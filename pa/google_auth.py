"""Google sign-in for Gmail and Google Calendar.

Gmail and Calendar use SEPARATE logins (two token files), so you can read your
college Gmail but put events on whichever Google account your iPhone uses.
Pick the same account both times if you want everything in one place.
"""
from __future__ import annotations

from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import ROOT

CREDENTIALS_FILE = ROOT / "credentials.json"

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]       # read only: can never send or delete
CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]   # add/edit events only

GMAIL_TOKEN = ROOT / "token_gmail.json"
CALENDAR_TOKEN = ROOT / "token_calendar.json"


class LoginNeeded(Exception):
    """Raised when a Google login has expired and must be redone."""


def get_creds(token_path: Path, scopes: list[str], interactive: bool = False) -> Credentials:
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except RefreshError:
            creds = None  # login expired or was revoked

    if not interactive:
        raise LoginNeeded(
            f"Google login needed for {token_path.name}. Run:  python main.py --login"
        )

    if not CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            "credentials.json not found. Follow README step 3 to download it from Google Cloud."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), scopes)
    creds = flow.run_local_server(port=0, prompt="consent")
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def login_all(calendar_enabled: bool) -> None:
    print("\n[1/2] Gmail login: choose your COLLEGE account in the browser window...")
    get_creds(GMAIL_TOKEN, GMAIL_SCOPES, interactive=True)
    print("      Gmail login saved.")
    if calendar_enabled:
        print("\n[2/2] Calendar login: choose the Google account whose calendar you want")
        print("      (the one you will add to your iPhone)...")
        get_creds(CALENDAR_TOKEN, CALENDAR_SCOPES, interactive=True)
        print("      Calendar login saved.")
    print("\nAll done. Now run:  python main.py")
