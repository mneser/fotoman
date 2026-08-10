from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "fotoman.db"
FAISS_PATH = DATA_DIR / "index.faiss"
IDS_PATH = DATA_DIR / "ids.json"
CONFIG_PATH = DATA_DIR / "config.json"
THUMBS_DIR = DATA_DIR / "thumbs"
MODELS_DIR = DATA_DIR / "models"

MODEL_ID = "google/siglip-base-patch16-224"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"}
NEAR_DUP_THRESHOLD = 0.92
FACE_MATCH_THRESHOLD = 0.363  # SFace cosine similarity (LFW)
DEFAULT_SOURCES = ["/home/user/Pictures"]
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
    "vqa_provider",
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
    "write_date_to_mtime": False,
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
    MODELS_DIR.mkdir(parents=True, exist_ok=True)


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


def public_settings(settings: dict | None = None) -> dict:
    """Settings safe to send to the UI (API key is never returned)."""
    s = settings if settings is not None else load_settings()
    out = {k: s.get(k, DEFAULT_SETTINGS.get(k)) for k in PUBLIC_SETTING_KEYS}
    out["vqa_api_key_set"] = bool(str(s.get("vqa_api_key") or "").strip())
    provider = str(s.get("vqa_provider") or "").strip().lower()
    model = str(s.get("vqa_model") or "").strip()
    out["vqa_configured"] = (
        provider not in ("", "none", "off")
        and out["vqa_api_key_set"]
        and bool(model)
    )
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
        elif key in ("write_date_to_exif", "write_date_to_mtime"):
            settings[key] = bool(val)
        elif key in ("model_id", "vqa_provider", "vqa_base_url", "vqa_model"):
            settings[key] = str(val or "").strip() or DEFAULT_SETTINGS[key]
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
