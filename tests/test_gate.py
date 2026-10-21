"""Gate tests (constitution III and V): resize, hashing, and the dedupe cache.

Images are generated in-memory with Pillow so the suite needs no fixtures on
disk and no network.
"""

from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from app.gate import MAX_SIDE, BadImage, ScanCache, prepare


def make_scene(seed: int, size=(1600, 1200), fmt="JPEG") -> bytes:
    """A deterministic 'scene': coloured background with a few shapes placed by `seed`."""
    img = Image.new("RGB", size, color=((seed * 37) % 256, (seed * 91) % 256, (seed * 53) % 256))
    d = ImageDraw.Draw(img)
    for i in range(4):
        x = (seed * 131 + i * 307) % (size[0] - 300)
        y = (seed * 71 + i * 211) % (size[1] - 300)
        d.rectangle([x, y, x + 250, y + 180], fill=((i * 80 + seed) % 256, 30, 200))
        d.ellipse([x + 60, y + 40, x + 200, y + 150], fill=(250, (i * 60) % 256, 20))
    out = BytesIO()
    img.save(out, format=fmt)
    return out.getvalue()


def recapture(raw: bytes) -> bytes:
    """Simulate re-taking the same photo: slightly different brightness and JPEG quality."""
    img = Image.open(BytesIO(raw)).convert("RGB")
    img = img.point(lambda v: min(255, int(v * 1.04) + 2))  # a bit brighter
    out = BytesIO()
    img.save(out, format="JPEG", quality=70)
    return out.getvalue()


# --- prepare -----------------------------------------------------------------


def test_prepare_downscales_long_side_to_max_and_keeps_aspect():
    frame = prepare(make_scene(1, size=(4000, 3000)))
    assert max(frame.width, frame.height) == MAX_SIDE
    assert frame.width == 1024 and frame.height == 768
    assert Image.open(BytesIO(frame.jpeg)).size == (1024, 768)


def test_prepare_never_upscales_small_images():
    frame = prepare(make_scene(1, size=(640, 480)))
    assert (frame.width, frame.height) == (640, 480)


def test_prepare_output_is_jpeg_and_smaller_than_input():
    raw = make_scene(2, size=(3000, 3000), fmt="PNG")
    frame = prepare(raw)
    assert frame.jpeg[:2] == b"\xff\xd8"  # JPEG magic
    assert frame.media_type == "image/jpeg"
    assert len(frame.jpeg) < len(raw)


def test_prepare_handles_png_with_alpha():
    img = Image.new("RGBA", (1200, 800), (10, 20, 30, 0))
    out = BytesIO()
    img.save(out, format="PNG")
    frame = prepare(out.getvalue())
    assert frame.width == 1024


def test_prepare_rejects_garbage():
    with pytest.raises(BadImage):
        prepare(b"definitely not an image")


# --- hashing -------------------------------------------------------------------


def test_recapture_of_same_scene_hashes_close_and_other_scene_far():
    a = prepare(make_scene(1))
    a_again = prepare(recapture(make_scene(1)))
    b = prepare(make_scene(2))
    assert a.phash - a_again.phash <= 8
    assert a.phash - b.phash > 8


# --- cache ---------------------------------------------------------------------


def test_cache_miss_then_hit_on_recapture():
    cache: ScanCache[str] = ScanCache()
    first = prepare(make_scene(1))
    assert cache.lookup(first.phash) is None
    cache.store(first.phash, None, "result-1")

    again = prepare(recapture(make_scene(1)))
    assert cache.lookup(again.phash) == "result-1"
    assert (cache.hits, cache.misses) == (1, 1)


def test_cache_misses_on_a_different_scene():
    cache: ScanCache[str] = ScanCache()
    cache.store(prepare(make_scene(1)).phash, None, "result-1")
    assert cache.lookup(prepare(make_scene(2)).phash) is None


def test_same_frame_different_question_is_a_miss():
    cache: ScanCache[str] = ScanCache()
    frame = prepare(make_scene(1))
    cache.store(frame.phash, "what is this?", "answer-a")
    assert cache.lookup(frame.phash, "  What is THIS? ") == "answer-a"  # whitespace/case-insensitive
    assert cache.lookup(frame.phash, "is it broken?") is None
    assert cache.lookup(frame.phash, None) is None


def test_cache_evicts_least_recently_used():
    cache: ScanCache[str] = ScanCache(capacity=2)
    f1, f2, f3 = (prepare(make_scene(s)) for s in (1, 2, 3))
    cache.store(f1.phash, None, "r1")
    cache.store(f2.phash, None, "r2")
    assert cache.lookup(f1.phash) == "r1"  # touch f1 so f2 becomes the oldest
    cache.store(f3.phash, None, "r3")
    assert len(cache) == 2
    assert cache.lookup(f2.phash) is None
    assert cache.lookup(f1.phash) == "r1" and cache.lookup(f3.phash) == "r3"


def test_cache_capacity_must_be_positive():
    with pytest.raises(ValueError):
        ScanCache(capacity=0)
