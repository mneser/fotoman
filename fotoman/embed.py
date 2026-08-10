from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import numpy as np
import torch
from PIL import Image, ImageFile, ImageOps
from transformers import AutoModel, AutoProcessor

from fotoman.config import load_settings

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def _from_pretrained(cls, model_id: str):
    """Load from local HF cache when possible to avoid Hub auth / rate-limit noise."""
    try:
        return cls.from_pretrained(model_id, local_files_only=True)
    except (OSError, ValueError):
        return cls.from_pretrained(model_id)


@lru_cache(maxsize=1)
def _load_model():
    model_id = load_settings().get("model_id", "google/siglip-base-patch16-224")
    # SigLIP config defaults trip a harmless transformers vocab warning (CLIP leftover ids).
    log = logging.getLogger("transformers.configuration_utils")
    prev = log.level
    log.setLevel(logging.ERROR)
    try:
        processor = _from_pretrained(AutoProcessor, model_id)
        model = _from_pretrained(AutoModel, model_id)
    finally:
        log.setLevel(prev)
    model.eval()
    model.to(DEVICE)
    return processor, model


def _open_rgb(path: Path) -> Image.Image:
    # Allow partial JPEGs (common in synced/export libraries) instead of aborting.
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)
    return im.convert("RGB")


def _as_embed(feats: torch.Tensor | object) -> torch.Tensor:
    if hasattr(feats, "pooler_output") and feats.pooler_output is not None:
        feats = feats.pooler_output
    elif hasattr(feats, "last_hidden_state"):
        feats = feats.last_hidden_state[:, 0]
    return feats / feats.norm(p=2, dim=-1, keepdim=True)


@torch.inference_mode()
def embed_images(paths: list[Path], batch_size: int = 8) -> np.ndarray:
    processor, model = _load_model()
    vectors: list[np.ndarray] = []
    for i in range(0, len(paths), batch_size):
        batch_paths = paths[i : i + batch_size]
        images = [_open_rgb(p) for p in batch_paths]
        inputs = processor(images=images, return_tensors="pt")
        pixel_values = inputs["pixel_values"].to(DEVICE)
        feats = _as_embed(model.get_image_features(pixel_values=pixel_values))
        vectors.append(feats.cpu().numpy().astype(np.float32))
    if not vectors:
        return np.zeros((0, 768), dtype=np.float32)
    return np.vstack(vectors)


@torch.inference_mode()
def embed_text(query: str) -> np.ndarray:
    processor, model = _load_model()
    inputs = processor(
        text=[query],
        return_tensors="pt",
        padding="max_length",
        truncation=True,
    )
    input_ids = inputs["input_ids"].to(DEVICE)
    kwargs = {
        "input_ids": input_ids,
    }
    if "attention_mask" in inputs:
        kwargs["attention_mask"] = inputs["attention_mask"].to(DEVICE)
    feats = _as_embed(model.get_text_features(**kwargs))
    return feats.cpu().numpy().astype(np.float32)[0]