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

import numpy as np
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


# --- perceptual hash ----------------------------------------------------------------------
# Own implementation (feature 007 follow-up): the `imagehash` package did exactly this but
# pulled scipy and PyWavelets (150 MB) into the image for one cosine transform. Same
# algorithm, verified bit-identical on the test scenes: 32x32 greyscale, 2-D DCT-II, keep
# the 8x8 low frequencies, 1 bit each for "above the median". 64 bits; Hamming distance
# between two hashes is how different two scenes look.

_HASH_SIZE = 8
_HIGHFREQ_FACTOR = 4


def _dct_matrix(n: int) -> np.ndarray:
    k = np.arange(n)[:, None]
    x = np.arange(n)[None, :]
    return np.cos(np.pi * k * (2 * x + 1) / (2 * n))  # unnormalised DCT-II; scale is irrelevant


_DCT = _dct_matrix(_HASH_SIZE * _HIGHFREQ_FACTOR)


class PHash:
    """64-bit perceptual hash. `a - b` is the Hamming distance (0 = same, 64 = opposite)."""

    __slots__ = ("bits",)

    def __init__(self, bits: np.ndarray) -> None:
        self.bits = np.asarray(bits, dtype=bool).reshape(-1)

    def __sub__(self, other: PHash) -> int:
        return int(np.count_nonzero(self.bits != other.bits))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, PHash) and bool(np.array_equal(self.bits, other.bits))

    def __hash__(self) -> int:
        return hash(str(self))

    def __str__(self) -> str:  # 16 hex chars, same encoding imagehash used
        return "".join(f"{int(''.join('1' if b else '0' for b in self.bits[i:i + 4]), 2):x}" for i in range(0, 64, 4))

    def __repr__(self) -> str:
        return f"PHash({self})"


def phash(img: Image.Image) -> PHash:
    n = _HASH_SIZE * _HIGHFREQ_FACTOR
    pixels = np.asarray(img.convert("L").resize((n, n), Image.Resampling.LANCZOS), dtype=np.float64)
    low = (_DCT @ pixels @ _DCT.T)[:_HASH_SIZE, :_HASH_SIZE]
    return PHash(low > np.median(low))


class BadImage(ValueError):
    """The upload could not be decoded as an image."""


@dataclass(frozen=True)
class PreparedFrame:
    """What leaves the gate: small JPEG bytes plus the hash that identifies the scene."""

    jpeg: bytes
    phash: PHash
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
    return PreparedFrame(jpeg=out.getvalue(), phash=phash(img), width=img.width, height=img.height)


class ScanCache(Generic[T]):
    """LRU of recent (scene hash, question) -> result, with fuzzy hash matching."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY, max_distance: int = DEFAULT_MAX_DISTANCE) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self.max_distance = max_distance
        self._items: OrderedDict[tuple[str, str], tuple[PHash, T]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _norm(question: str | None) -> str:
        return (question or "").strip().lower()

    def lookup(self, phash: PHash, question: str | None = None) -> T | None:
        """Return the cached result for a near-identical frame with the same question, else None."""
        q = self._norm(question)
        for key, (stored_hash, result) in reversed(self._items.items()):  # most recent first
            if key[1] == q and (stored_hash - phash) <= self.max_distance:
                self._items.move_to_end(key)
                self.hits += 1
                return result
        self.misses += 1
        return None

    def store(self, phash: PHash, question: str | None, result: T) -> None:
        key = (str(phash), self._norm(question))
        self._items[key] = (phash, result)
        self._items.move_to_end(key)
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)  # evict the least recently used

    def __len__(self) -> int:
        return len(self._items)
