from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
from pathlib import Path

from fotoman import __version__


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"))


def _bundle_dir() -> Path:
    if _frozen():
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def _win_local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


def _default_data_dir() -> Path:
    if sys.platform == "win32":
        return _win_local_appdata() / "Fotoman"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "fotoman"
    return Path.home() / ".local" / "share" / "fotoman"


def _default_cache_dir() -> Path:
    if sys.platform == "win32":
        return _win_local_appdata() / "Fotoman" / "cache"
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / "fotoman"
    return Path.home() / ".cache" / "fotoman"


def _dir_has_library(path: Path) -> bool:
    return (path / "config.json").is_file() or (path / "fotoman.db").is_file()


def _move_if_present(src: Path, dest: Path) -> None:
    if not src.exists() or src.resolve() == dest.resolve():
        return
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_dir():
        dest.rmdir()
    shutil.move(str(src), str(dest))


def _data_dir() -> Path:
    override = os.environ.get("FOTOMAN_DATA")
    if override:
        return Path(override)
    dest = _default_data_dir()
    legacy = _bundle_dir() / "data"
    if not _frozen() and not _dir_has_library(dest) and _dir_has_library(legacy):
        _move_if_present(legacy, dest)
    return dest


def _cache_dir(data_dir: Path) -> Path:
    override = os.environ.get("FOTOMAN_CACHE")
    dest = Path(override) if override else _default_cache_dir()
    _move_if_present(data_dir / "models", dest / "models")
    _move_if_present(data_dir / "hf-cache", dest / "hf-cache")
    return dest


ROOT = _bundle_dir()
DATA_DIR = _data_dir()
CACHE_DIR = _cache_dir(DATA_DIR)
DB_PATH = DATA_DIR / "fotoman.db"
FAISS_PATH = DATA_DIR / "index.faiss"
IDS_PATH = DATA_DIR / "ids.json"
CONFIG_PATH = DATA_DIR / "config.json"
THUMBS_DIR = DATA_DIR / "thumbs"
RECLAIMED_DIR = DATA_DIR / "reclaimed"
MODELS_DIR = CACHE_DIR / "models"
HF_CACHE_DIR = CACHE_DIR / "hf-cache"

os.environ.setdefault("HF_HOME", str(HF_CACHE_DIR))

MODEL_ID = "google/siglip-base-patch16-224"
IMAGE_EXTS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".tif",
    ".tiff",
    ".bmp",
    ".heic",
    ".heif",
}
NEAR_DUP_THRESHOLD = 0.92
FACE_MATCH_THRESHOLD = 0.363  # SFace cosine similarity (LFW)
_pictures = Path.home() / "Pictures"
DEFAULT_SOURCES = [str(_pictures if _pictures.is_dir() else Path.home())]
VQA_BASE_URL = "https://api.openai.com/v1"
VQA_MODEL = "gpt-4o-mini"

YUNET_MODEL = MODELS_DIR / "face_detection_yunet_2023mar.onnx"
SFACE_MODEL = MODELS_DIR / "face_recognition_sface_2021dec.onnx"
YUNET_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_detection_yunet/face_detection_yunet_2023mar.onnx"
)
SFACE_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_recognition_sface/face_recognition_sface_2021dec.onnx"
)

# Keys the preferences UI may read/write (migration flags stay untouched).
PUBLIC_SETTING_KEYS = (
    "model_id",
    "near_dup_threshold",
    "face_match_threshold",
    "date_tree_order",
    "place_edit_nav",
    "write_date_to_exif",
    "write_date_to_mtime",
    "collection_copies",
    "fotomob_url",
    "fotoman_name",
    "fotomob_share_code",
    "vqa_base_url",
    "vqa_model",
)

DEFAULT_SETTINGS = {
    "model_id": MODEL_ID,
    "near_dup_threshold": NEAR_DUP_THRESHOLD,
    "face_match_threshold": FACE_MATCH_THRESHOLD,
    "date_tree_order": "newest",  # newest | oldest
    "place_edit_nav": "stay",  # stay | follow
    # Manual date edits: off by default (EXIF rewrite changes content hash).
    "write_date_to_exif": False,
    "write_date_to_mtime": True,
    "collection_copies": "primary",  # primary | cluster
    "fotomob_url": "https://colligator.com/fotomob",
    "fotoman_name": "mini",
    "host": "127.0.0.1",
    "port": 8000,
    "vqa_provider": "openai",
    "vqa_api_key": "",
    "vqa_base_url": VQA_BASE_URL,
    "vqa_model": VQA_MODEL,
}


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    RECLAIMED_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    HF_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict:
    ensure_dirs()
    if CONFIG_PATH.exists():
        raw = json.loads(CONFIG_PATH.read_text())
        settings = {**DEFAULT_SETTINGS, **raw}
        # Persist newly introduced defaults once.
        if any(k not in raw for k in DEFAULT_SETTINGS):
            save_settings(settings)
        return settings
    settings = dict(DEFAULT_SETTINGS)
    save_settings(settings)
    return settings


def save_settings(settings: dict) -> None:
    ensure_dirs()
    CONFIG_PATH.write_text(json.dumps(settings, indent=2))


def _new_share_code() -> str:
    return secrets.token_urlsafe(8)


def new_share_token() -> str:
    return _new_share_code()


def ensure_fotomob_share_code() -> str:
    settings = load_settings()
    code = str(settings.get("fotomob_share_code") or "").strip()
    if code:
        return code
    code = _new_share_code()
    settings["fotomob_share_code"] = code
    save_settings(settings)
    return code


def fotomob_share_url(code: str | None = None) -> str:
    base = str(load_settings().get("fotomob_url") or "").strip().rstrip("/")
    token = (code or ensure_fotomob_share_code()).strip()
    if not base:
        return token
    # Path token so an installed PWA can capture the link; ?share= still works.
    return f"{base}/s/{token}"


def fotomob_subfolder_share() -> bool:
    """QR / share links for collection sub-folders. Off unless FOTOMOB_SUBFOLDER_SHARE is set."""
    raw = (os.environ.get("FOTOMOB_SUBFOLDER_SHARE") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def public_settings(settings: dict | None = None) -> dict:
    """Settings safe to send to the UI (API key is never returned)."""
    s = settings if settings is not None else load_settings()
    out = {k: s.get(k, DEFAULT_SETTINGS.get(k)) for k in PUBLIC_SETTING_KEYS}
    out["fotomob_share_code"] = ensure_fotomob_share_code()
    out["fotomob_share_url"] = fotomob_share_url(out["fotomob_share_code"])
    out["vqa_api_key_set"] = bool(str(s.get("vqa_api_key") or "").strip())
    out["vqa_configured"] = (
        out["vqa_api_key_set"]
        and bool(str(s.get("vqa_base_url") or "").strip())
    )
    out["version"] = __version__
    return out


def update_settings(patch: dict) -> dict:
    """Merge a preferences patch into config.json and return public settings."""
    settings = load_settings()
    prev_model = settings.get("model_id")

    for key in PUBLIC_SETTING_KEYS:
        if key not in patch:
            continue
        val = patch[key]
        if key == "near_dup_threshold":
            settings[key] = float(val)
        elif key == "face_match_threshold":
            settings[key] = float(val)
        elif key == "date_tree_order":
            order = str(val or "").strip().lower()
            settings[key] = order if order in ("newest", "oldest") else "newest"
        elif key == "place_edit_nav":
            nav = str(val or "").strip().lower()
            settings[key] = nav if nav in ("stay", "follow") else "stay"
        elif key == "collection_copies":
            copies = str(val or "").strip().lower()
            settings[key] = copies if copies in ("primary", "cluster") else "primary"
        elif key in ("write_date_to_exif", "write_date_to_mtime"):
            settings[key] = bool(val)
        elif key == "vqa_base_url":
            settings[key] = str(val or "").strip()
        elif key in ("model_id", "vqa_model", "fotomob_url", "fotoman_name"):
            settings[key] = str(val or "").strip() or DEFAULT_SETTINGS[key]
        elif key == "fotomob_share_code":
            settings[key] = _new_share_code()
        else:
            settings[key] = val

    if "vqa_api_key" in patch:
        key_val = patch["vqa_api_key"]
        if key_val is None:
            pass  # leave unchanged
        else:
            settings["vqa_api_key"] = str(key_val).strip()

    save_settings(settings)

    if settings.get("model_id") != prev_model:
        try:
            from fotoman import embed

            embed._load_model.cache_clear()
        except Exception:
            pass

    return public_settings(settings)
