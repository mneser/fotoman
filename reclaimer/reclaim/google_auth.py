"""Google Photos OAuth (desktop / installed app)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from reclaim.paths import client_secrets_path, token_path
from reclaim.util import ExitCode

# Read-only library access. User must enable Photos Library API on their GCP project.
SCOPES = ["https://www.googleapis.com/auth/photoslibrary.readonly"]


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def has_client_secrets() -> bool:
    return client_secrets_path().exists()


def has_token() -> bool:
    return token_path().exists()


def load_credentials():
    """Return google.oauth2.credentials.Credentials or raise."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = token_path()
    if not path.exists():
        raise FileNotFoundError(
            "Not authenticated. Run: reclaim google auth\n"
            f"(expected token at {path})"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    creds = Credentials.from_authorized_user_info(data, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_credentials(creds)
    if not creds or not creds.valid:
        raise RuntimeError("Google credentials invalid; run: reclaim google auth")
    return creds


def save_credentials(creds) -> None:
    path = token_path()
    path.write_text(creds.to_json(), encoding="utf-8")
    _chmod_private(path)


def run_auth(client_secrets: Path | None = None) -> ExitCode:
    """Browser OAuth consent; store refresh token under ~/.config/reclaim/."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    secrets = client_secrets or client_secrets_path()
    if not secrets.exists():
        print(
            "Missing OAuth client secrets.\n\n"
            "1. Create a Google Cloud project\n"
            "2. Enable the Photos Library API\n"
            "3. Create an OAuth client (Desktop app)\n"
            "4. Download JSON and save as:\n"
            f"   {client_secrets_path()}\n"
            "   or pass --client-secrets PATH\n\n"
            "See reclaimer/README.md for details."
        )
        return ExitCode.USAGE

    # Copy into config dir if provided elsewhere
    if secrets.resolve() != client_secrets_path().resolve():
        dest = client_secrets_path()
        dest.write_bytes(secrets.read_bytes())
        _chmod_private(dest)
        secrets = dest

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    save_credentials(creds)
    print(f"Authenticated. Token saved to {token_path()} (mode 0600).")
    print("Revoke access anytime at https://myaccount.google.com/permissions")
    print("or run: reclaim google logout")
    return ExitCode.OK


def logout() -> ExitCode:
    path = token_path()
    if path.exists():
        path.unlink()
        print(f"Deleted {path}")
    else:
        print("No token on disk.")
    return ExitCode.OK
