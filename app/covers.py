"""Обложки плейлистов: скачать картинку источника и подготовить JPEG для приёмника."""
from __future__ import annotations

import io

import httpx

MAX_BYTES = 256 * 1024   # предел Spotify для обложки (после base64 чуть больше, но в лимит укладывается)


def to_jpeg(raw: bytes, max_bytes: int = MAX_BYTES, size: int = 640) -> bytes:
    """Любая картинка → квадратный JPEG не больше max_bytes."""
    from PIL import Image
    img = Image.open(io.BytesIO(raw))
    img = img.convert("RGB")
    w, h = img.size
    side = min(w, h)
    img = img.crop(((w - side) // 2, (h - side) // 2, (w - side) // 2 + side, (h - side) // 2 + side))
    if side > size:
        img = img.resize((size, size), Image.LANCZOS)
    for quality in (90, 80, 70, 60, 50, 40):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True)
        if buf.tell() * 4 / 3 <= max_bytes:   # Spotify считает размер вместе с base64
            return buf.getvalue()
        if quality == 60 and img.size[0] > 300:
            img = img.resize((300, 300), Image.LANCZOS)
    raise ValueError("картинка слишком большая даже после сжатия")


def download(url: str, proxy: str | None = None) -> bytes:
    with httpx.Client(proxy=proxy, timeout=30, follow_redirects=True) as h:
        r = h.get(url)
    r.raise_for_status()
    return r.content
