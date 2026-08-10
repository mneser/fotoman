"""Platform-specific export assist wizards."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExportWizard:
    platform: str
    source: str
    url: str
    patterns: list[str]
    checklist: list[str]


FACEBOOK = ExportWizard(
    platform="facebook",
    source="facebook",
    url="https://www.facebook.com/dyi/",
    patterns=["facebook", "posts-", "your_facebook", "fb_"],
    checklist=[
        "Log into Facebook in your browser (this tool never asks for your password).",
        "Open Download Your Information (DYI).",
        "Select format: ZIP, media quality: High (or Original if offered).",
        "Include: Photos and videos (and date range you care about).",
        "Request export — Facebook may take minutes to days.",
        "When ready, download the zip into your watch directory (default ~/Downloads).",
        "Re-run this command if the export is still preparing (exit code 4 / reclaim status).",
    ],
)

INSTAGRAM = ExportWizard(
    platform="instagram",
    source="instagram",
    url="https://accountscenter.instagram.com/info_and_permissions/dyi/",
    patterns=["instagram", "your_instagram", "ig-"],
    checklist=[
        "Log into Instagram / Meta Accounts Center in your browser.",
        "Request a download of your information.",
        "Choose format: ZIP; media quality: Highest / Original.",
        "Include Photos and videos (posts, stories if desired).",
        "Submit the request — preparation can take hours or days.",
        "Download the zip into your watch directory when notified.",
        "Re-run this command later if still preparing (reclaim status).",
    ],
)

GOOGLE_TAKEOUT = ExportWizard(
    platform="google_takeout",
    source="google_takeout",
    # Pre-select Google Photos product when possible
    url=(
        "https://takeout.google.com/settings/takeout/"
        "custom/google_photos?dest=drive"
    ),
    patterns=["takeout", "google"],
    checklist=[
        "Stay logged into the Google account that owns the photos.",
        "In Takeout, deselect all except Google Photos (or keep Photos selected).",
        "Use export frequency / file size that fits your library (multi-part zips OK).",
        "Prefer .zip; larger parts mean fewer pieces to track.",
        "Create export — Google may email when ready (often hours–days).",
        "Download ALL parts (…-001.zip, …-002.zip, …) into the watch directory.",
        "Re-run reclaim google takeout when downloads finish, or if still preparing.",
        "Tip: for recurring sync, prefer `reclaim google pull` (API) over full Takeout.",
    ],
)


def wizard_for(platform: str) -> ExportWizard:
    return {
        "facebook": FACEBOOK,
        "instagram": INSTAGRAM,
        "google_takeout": GOOGLE_TAKEOUT,
    }[platform]
