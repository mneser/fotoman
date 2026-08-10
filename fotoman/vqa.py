from __future__ import annotations

import base64
import io
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageFile, ImageOps

from fotoman.config import load_settings
from fotoman.db import db
from fotoman.geocode import parse_city_country

MAX_VQA_SIDE = 1536

TASK_PROMPTS = {
    "where": (
        "Look at this photo and identify where it was taken.\n"
        "Reply with ONLY a single place label on one line, using this format:\n"
        "City, Country\n"
        "or\n"
        "City, State, Country\n"
        "or\n"
        "Country\n"
        "If you cannot determine the location, reply with exactly: UNKNOWN\n"
        "Do not include quotes, markdown, or any other text."
    ),
    "describe": (
        "Describe this image in detail. Include objects, people, setting, "
        "colors, text, and any notable features."
    ),
}

VALID_TASKS = frozenset({"where", "describe", "freeform"})


def _photo_path(photo_id: int) -> Path:
    with db() as conn:
        row = conn.execute("SELECT path FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if not row:
        raise LookupError("Photo not found")
    path = Path(row["path"])
    if not path.exists():
        raise FileNotFoundError("Photo file missing")
    return path


def _image_data_url(path: Path) -> str:
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    im = Image.open(path)
    im = ImageOps.exif_transpose(im).convert("RGB")
    w, h = im.size
    scale = min(1.0, MAX_VQA_SIDE / max(w, h))
    if scale < 1.0:
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def _parse_place_answer(answer: str) -> str | None:
    text = (answer or "").strip()
    if not text:
        return None
    # First non-empty line; strip common wrappers.
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    line = re.sub(r"^[*_`\"'\s]+|[*_`\"'\s]+$", "", line)
    line = line.strip().strip(".")
    if not line or line.upper() in {"UNKNOWN", "N/A", "NONE", "UNCLEAR"}:
        return None
    parsed = parse_city_country(line)
    label = (parsed.get("place_label") or line).strip()
    return label or None


def ask_photo(photo_id: int, prompt: str = "", task: str | None = None) -> dict:
    task_name = (task or "freeform").strip().lower() or "freeform"
    if task_name not in VALID_TASKS:
        raise ValueError(f"Unsupported VQA task: {task_name}")

    if task_name in TASK_PROMPTS:
        prompt = TASK_PROMPTS[task_name]
    else:
        prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("Prompt is required")

    settings = load_settings()
    provider = (settings.get("vqa_provider") or "openai").strip().lower()
    if provider in ("", "none", "off"):
        raise RuntimeError("VQA is not configured. Set a provider in Preferences.")

    api_key = str(settings.get("vqa_api_key") or "").strip()
    if not api_key:
        raise RuntimeError("VQA API key is missing. Add it in Preferences.")

    base_url = (settings.get("vqa_base_url") or "https://api.openai.com/v1").rstrip("/")
    model = (settings.get("vqa_model") or "gpt-4o-mini").strip()
    if not model:
        raise RuntimeError("VQA model is missing. Set it in Preferences.")

    path = _photo_path(photo_id)
    data_url = _image_data_url(path)

    if provider == "openai":
        result = _openai_chat(base_url, api_key, model, prompt, data_url)
    else:
        raise RuntimeError(f"Unsupported VQA provider: {provider}")

    result["task"] = task_name
    if task_name == "where":
        result["place_label"] = _parse_place_answer(result.get("answer") or "")
    return result


def _openai_chat(base_url: str, api_key: str, model: str, prompt: str, data_url: str) -> dict:
    url = f"{base_url}/chat/completions"
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "max_tokens": 500,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(detail)
            msg = err.get("error", {}).get("message") or detail
        except Exception:
            msg = detail or str(e)
        raise RuntimeError(msg) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"VQA request failed: {e.reason}") from e

    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError("Empty VQA response")
    message = choices[0].get("message") or {}
    answer = (message.get("content") or "").strip()
    return {
        "answer": answer,
        "model": payload.get("model") or model,
        "provider": "openai",
    }
