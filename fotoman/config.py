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


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict:
    ensure_dirs()
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    settings = {
        "model_id": MODEL_ID,
        "near_dup_threshold": NEAR_DUP_THRESHOLD,
        "host": "127.0.0.1",
        "port": 8000,
    }
    save_settings(settings)
    return settings


def save_settings(settings: dict) -> None:
    ensure_dirs()
    CONFIG_PATH.write_text(json.dumps(settings, indent=2))