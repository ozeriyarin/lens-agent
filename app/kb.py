"""Knowledge base: the owner's Markdown notes, chunked, embedded, searchable (feature 005).

Why this module: a question like "how often do I water this?" has no answer in the pixels.
The owner writes notes in `KB_DIR/*.md`; this indexes them into passages with embeddings
(same embedding model and ledger as memory) and answers `search(query)` with the few most
similar passages above a floor. The agent reaches it through the `lookup` tool (agent.py).

    uv run python -m app.kb index            # (re)index changed notes
    uv run python -m app.kb search "cactus"  # see what the agent would get

Design: memory.py's shape without sightings. Passages are keyed by file + position and
carry the file's content hash and the embedding model name, so a changed note or a changed
model re-embeds exactly what needs it. Cosine search in numpy; a floor turns "nearest" into
"relevant", which is what keeps a mug note out of a watering answer.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import re
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.embeddings import Embeddings

log = logging.getLogger(__name__)

DEFAULT_KB_DIR = Path("kb")
MAX_PASSAGE_CHARS = 800
DEFAULT_FLOOR = 0.3
DEFAULT_TOP_K = 3


@dataclass(frozen=True)
class Passage:
    file: str
    heading: str
    text: str
    score: float = 0.0


# --- chunking ----------------------------------------------------------------------------


def chunk_markdown(text: str) -> list[tuple[str, str]]:
    """Split a note into (heading, passage) pairs: one per heading section, sections longer
    than MAX_PASSAGE_CHARS split at paragraph breaks. Heading path is 'H1 › H2'."""
    sections: list[tuple[str, list[str]]] = []
    stack: list[tuple[int, str]] = []
    current: list[str] = []

    def flush() -> None:
        body = "\n".join(current).strip()
        if body:
            sections.append((" › ".join(h for _, h in stack) or "(top)", _split_long(body)))
        current.clear()

    for line in text.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*\S)\s*$", line)
        if m:
            flush()
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2).strip()))
        else:
            current.append(line)
    flush()
    return [(heading, part) for heading, parts in sections for part in parts]


def _split_long(body: str) -> list[str]:
    if len(body) <= MAX_PASSAGE_CHARS:
        return [body]
    parts: list[str] = []
    buf = ""
    for para in re.split(r"\n\s*\n", body):
        para = para.strip()
        if not para:
            continue
        if buf and len(buf) + len(para) + 2 > MAX_PASSAGE_CHARS:
            parts.append(buf)
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
        while len(buf) > MAX_PASSAGE_CHARS:  # a single huge paragraph: hard-wrap it
            parts.append(buf[:MAX_PASSAGE_CHARS])
            buf = buf[MAX_PASSAGE_CHARS:]
    if buf:
        parts.append(buf)
    return parts


# --- store ---------------------------------------------------------------------------------


class KnowledgeBase:
    def __init__(self, db_path: str | Path, embeddings: Embeddings | None, kb_dir: str | Path = DEFAULT_KB_DIR,
                 *, floor: float = DEFAULT_FLOOR, top_k: int = DEFAULT_TOP_K) -> None:
        self.db_path = str(db_path)
        self.embeddings = embeddings
        self.kb_dir = Path(kb_dir)
        self.floor = floor
        self.top_k = top_k
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS passages (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    file      TEXT NOT NULL,
                    position  INTEGER NOT NULL,
                    heading   TEXT NOT NULL,
                    text      TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    dim       INTEGER NOT NULL,
                    model     TEXT NOT NULL,
                    file_hash TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS passages_file ON passages(file)")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=5.0)

    @property
    def enabled(self) -> bool:
        return self.embeddings is not None

    def count(self) -> int:
        with closing(self._connect()) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM passages").fetchone()[0])

    def files(self) -> dict[str, str]:
        """Indexed files -> content hash."""
        with closing(self._connect()) as conn:
            return {r[0]: r[1] for r in conn.execute("SELECT DISTINCT file, file_hash FROM passages")}

    # --- indexing -------------------------------------------------------------------------

    async def index(self) -> dict[str, int]:
        """Embed new or changed notes, drop passages of deleted notes. Returns counts."""
        stats = {"files": 0, "indexed": 0, "unchanged": 0, "removed": 0, "passages": 0}
        if not self.enabled:
            return stats
        known = self.files()
        seen: set[str] = set()
        model = self.embeddings.model_name
        for path in sorted(self.kb_dir.rglob("*.md")) if self.kb_dir.is_dir() else []:
            if path.name.lower() == "readme.md":
                continue
            rel = path.relative_to(self.kb_dir).as_posix()
            seen.add(rel)
            stats["files"] += 1
            content = path.read_text(encoding="utf-8", errors="replace")
            digest = hashlib.sha256(f"{model}\n{content}".encode()).hexdigest()[:16]
            if known.get(rel) == digest:
                stats["unchanged"] += 1
                continue
            chunks = chunk_markdown(content)
            vectors = await self.embeddings.embed([f"{rel} › {h}\n{t}" for h, t in chunks]) if chunks else []
            if vectors is None:
                log.warning("kb: embedding failed for %s; keeping the old index for it", rel)
                continue
            with closing(self._connect()) as conn, conn:
                conn.execute("DELETE FROM passages WHERE file = ?", (rel,))
                for pos, ((heading, text), vec) in enumerate(zip(chunks, vectors, strict=True)):
                    arr = np.asarray(vec, dtype=np.float32)
                    conn.execute(
                        "INSERT INTO passages (file, position, heading, text, embedding, dim, model, file_hash) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (rel, pos, heading, text, arr.tobytes(), int(arr.shape[0]), model, digest),
                    )
            stats["indexed"] += 1
        with closing(self._connect()) as conn, conn:
            for rel in set(known) - seen:
                conn.execute("DELETE FROM passages WHERE file = ?", (rel,))
                stats["removed"] += 1
        stats["passages"] = self.count()
        return stats

    # --- search ---------------------------------------------------------------------------

    async def search(self, query: str, *, top_k: int | None = None, floor: float | None = None) -> list[Passage]:
        """Top passages by cosine similarity above the floor; [] when nothing is relevant."""
        if not self.enabled or not query.strip():
            return []
        vectors = await self.embeddings.embed([query.strip()[:500]])
        if not vectors:
            return []
        q = np.asarray(vectors[0], dtype=np.float32)
        qn = float(np.linalg.norm(q)) or 1.0
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT file, heading, text, embedding, dim FROM passages").fetchall()
        scored: list[Passage] = []
        for file, heading, text, blob, _dim in rows:
            v = np.frombuffer(blob, dtype=np.float32)
            if v.shape[0] != q.shape[0]:
                continue  # indexed with another embedding model: reindex to use it
            score = float(np.dot(v, q) / ((float(np.linalg.norm(v)) or 1.0) * qn))
            if score >= (self.floor if floor is None else floor):
                scored.append(Passage(file, heading, text, round(score, 4)))
        scored.sort(key=lambda p: p.score, reverse=True)
        return scored[: (top_k or self.top_k)]


# --- CLI -------------------------------------------------------------------------------------


def _make_kb() -> KnowledgeBase:
    from app.budget import Budget
    from app.config import Settings

    settings = Settings.from_env()
    budget = Budget(settings.db_path, settings.budget_usd)
    return KnowledgeBase(settings.db_path, Embeddings(settings.embedding_model, budget),
                         os.getenv("KB_DIR", str(DEFAULT_KB_DIR)), floor=settings.kb_floor)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.kb", description="index and search the owner's notes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("index", help="(re)index changed notes in KB_DIR")
    s = sub.add_parser("search", help="show the passages the agent would get")
    s.add_argument("query")
    s.add_argument("--k", type=int, default=DEFAULT_TOP_K)
    args = ap.parse_args(argv)
    kb = _make_kb()
    if args.cmd == "index":
        stats = asyncio.run(kb.index())
        print(f"{stats['files']} notes: {stats['indexed']} indexed, {stats['unchanged']} unchanged, "
              f"{stats['removed']} removed; {stats['passages']} passages in {kb.db_path}")
    else:
        hits = asyncio.run(kb.search(args.query, top_k=args.k))
        if not hits:
            print("no relevant passage (above the floor)", file=sys.stderr)
        for p in hits:
            print(f"[{p.score:.3f}] {p.file} › {p.heading}\n    {p.text[:200].replace(chr(10), ' ')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
