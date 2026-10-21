"""The gate: pay only for new information (constitution III).

Why this file exists: a phone camera produces near-identical frames all day.
Sending each one to a vision model is the fastest way to burn the budget on
nothing. So every frame goes through two cheap steps before anything paid:

1. `prepare`: decode, downscale to <=1024px on the long side, re-encode as
   JPEG in memory (FR-3). Smaller bytes = fewer image tokens, and the model
   does not need more pixels to tell a mug from a laptop. Nothing touches
   disk (constitution IV).
2. `ScanCache`: a perceptual hash (pHash) of the prepared image is compared
   with the hashes of recent scans (FR-4). Two frames of the same scene hash
   to almost the same 64 bits even with camera noise or a slight shift; a
   different scene does not. A hit returns the previous result for free.

Design choices:
- pHash over an exact byte hash: a byte hash misses on every re-capture
  because JPEG noise differs; pHash compares *what the picture looks like*.
- Hamming distance threshold: 0 = identical, 64 = opposite. Imagehash's
  guidance is that <=~10 means "probably the same image"; we use 8 to lean
  towards "changed", because a false hit shows stale results.
- The cache key includes the question: the same frame with a new question
  deserves a new answer. Feature 003 will revisit this for live mode.
- Small LRU (32 entries): the gate is about consecutive frames, not a
  long-term memory. Entity memory is feature 004.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO
from typing import Generic, TypeVar

import imagehash
from PIL import Image, UnidentifiedImageError

MAX_SIDE = 1024
JPEG_QUALITY = 85
DEFAULT_MAX_DISTANCE = 8
DEFAULT_CAPACITY = 32

# A tiny JPEG/PNG header can declare a 100k x 100k canvas; decoding it would
# eat gigabytes of RAM before we ever downscale. 40 MP covers every phone
# camera. Checked from the header *before* pixels are decoded.
MAX_PIXELS = 40_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS  # Pillow raises DecompressionBombError above 2x this

T = TypeVar("T")


class BadImage(ValueError):
    """The upload could not be decoded as an image."""


@dataclass(frozen=True)
class PreparedFrame:
    """What leaves the gate: small JPEG bytes plus the hash that identifies the scene."""

    jpeg: bytes
    phash: imagehash.ImageHash
    width: int
    height: int

    @property
    def media_type(self) -> str:
        return "image/jpeg"


def prepare(raw: bytes) -> PreparedFrame:
    """Decode, downscale to MAX_SIDE, re-encode as JPEG, hash. All in memory."""
    try:
        img = Image.open(BytesIO(raw))  # lazy: reads the header only
        if img.width * img.height > MAX_PIXELS:
            raise BadImage(f"image too large: {img.width}x{img.height} px, max {MAX_PIXELS} px")
        img.draft("RGB", (MAX_SIDE, MAX_SIDE))  # JPEG: decode at reduced scale straight away
        img.load()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as e:
        raise BadImage(f"not a decodable image: {e}") from e

    img = img.convert("RGB")  # drops alpha / palette so JPEG encoding always works
    img.thumbnail((MAX_SIDE, MAX_SIDE))  # keeps aspect ratio, never upscales

    out = BytesIO()
    img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return PreparedFrame(jpeg=out.getvalue(), phash=imagehash.phash(img), width=img.width, height=img.height)


class ScanCache(Generic[T]):
    """LRU of recent (scene hash, question) -> result, with fuzzy hash matching."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY, max_distance: int = DEFAULT_MAX_DISTANCE) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self.max_distance = max_distance
        self._items: OrderedDict[tuple[str, str], tuple[imagehash.ImageHash, T]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _norm(question: str | None) -> str:
        return (question or "").strip().lower()

    def lookup(self, phash: imagehash.ImageHash, question: str | None = None) -> T | None:
        """Return the cached result for a near-identical frame with the same question, else None."""
        q = self._norm(question)
        for key, (stored_hash, result) in reversed(self._items.items()):  # most recent first
            if key[1] == q and (stored_hash - phash) <= self.max_distance:
                self._items.move_to_end(key)
                self.hits += 1
                return result
        self.misses += 1
        return None

    def store(self, phash: imagehash.ImageHash, question: str | None, result: T) -> None:
        key = (str(phash), self._norm(question))
        self._items[key] = (phash, result)
        self._items.move_to_end(key)
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)  # evict the least recently used

    def __len__(self) -> int:
        return len(self._items)
