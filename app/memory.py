"""Entity memory: link detections to objects seen before (feature 004).

Why this module: a scan says "a white mug". Memory turns that into "the white mug with
the blue rim you have scanned 12 times since Tuesday". It stores descriptions and
embeddings, never pixels (constitution IV), in two SQLite tables next to the ledger.

Linking rule (spec FR-4): a detection with an identity is embedded; among remembered
entities of the *same category* the nearest by cosine wins if it is above the threshold
and clearly ahead of the runner-up (margin). A near-tie is treated as new: a wrong
"known" badge is worse than a missed one. Sightings are rate-limited per entity so live
mode counts visits, not frames.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from app.embeddings import Embeddings
from app.schema import DetectedObject, EntityRef, ScanResult

log = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 0.86
DEFAULT_MARGIN = 0.02
DEFAULT_MIN_GAP_S = 60


@dataclass(frozen=True)
class Entity:
    id: str
    category: str
    label: str
    identity: str
    first_seen: datetime
    last_seen: datetime
    sightings: int
    last_position: str
    last_scene: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _row(r: tuple) -> Entity:
    return Entity(r[0], r[1], r[2], r[3], datetime.fromisoformat(r[4]), datetime.fromisoformat(r[5]), r[6], r[7], r[8])


_ENTITY_COLS = "id, category, label, identity, first_seen, last_seen, sightings, last_position, last_scene"


class Memory:
    def __init__(
        self,
        db_path: str | Path,
        embeddings: Embeddings | None,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        margin: float = DEFAULT_MARGIN,
        min_gap_s: int = DEFAULT_MIN_GAP_S,
    ) -> None:
        self.db_path = str(db_path)
        self.embeddings = embeddings
        self.threshold = threshold
        self.margin = margin
        self.min_gap = timedelta(seconds=min_gap_s)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entities (
                    id            TEXT PRIMARY KEY,
                    category      TEXT NOT NULL,
                    label         TEXT NOT NULL,
                    identity      TEXT NOT NULL,
                    embedding     BLOB NOT NULL,
                    dim           INTEGER NOT NULL,
                    first_seen    TEXT NOT NULL,
                    last_seen     TEXT NOT NULL,
                    sightings     INTEGER NOT NULL,
                    last_position TEXT NOT NULL,
                    last_scene    TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sightings (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id  TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
                    ts         TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    position   TEXT NOT NULL,
                    scene      TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS entities_cat ON entities(category)")
            conn.execute("CREATE INDEX IF NOT EXISTS sightings_entity ON sightings(entity_id, ts)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @property
    def enabled(self) -> bool:
        return self.embeddings is not None

    # --- linking ---------------------------------------------------------------------------

    @staticmethod
    def text_for(o: DetectedObject) -> str:
        return f"{o.category}: {o.label}. {o.identity}"

    async def link(self, result: ScanResult, request_id: str, *, source: str = "tap",
                   now: datetime | None = None) -> list[EntityRef | None]:
        """One EntityRef per object in result.objects (None where not linkable)."""
        refs: list[EntityRef | None] = [None] * len(result.objects)
        if not self.enabled:
            return refs
        candidates = [(i, o) for i, o in enumerate(result.objects) if o.identity and o.category != "person"]
        if not candidates:
            return refs
        vectors = await self.embeddings.embed([self.text_for(o) for _, o in candidates], source=source)
        if vectors is None:
            return refs
        now = now or _utcnow()
        for (i, o), vec in zip(candidates, vectors):
            try:
                refs[i] = self._link_one(o, np.asarray(vec, dtype=np.float32), request_id, result.scene, now)
            except sqlite3.Error as e:  # memory must never fail a scan
                log.warning("memory link failed for %s: %s", o.label, e)
        return refs

    def _link_one(self, o: DetectedObject, vec: np.ndarray, request_id: str, scene: str, now: datetime) -> EntityRef:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_ENTITY_COLS}, embedding, dim FROM entities WHERE category = ?", (o.category,)
            ).fetchall()
            best: tuple[float, tuple] | None = None
            second = -1.0
            for r in rows:
                stored = np.frombuffer(r[9], dtype=np.float32)
                if stored.shape[0] != vec.shape[0]:
                    continue  # embedding model changed; old vectors are not comparable
                sim = float(np.dot(stored, vec) / (np.linalg.norm(stored) * np.linalg.norm(vec) or 1.0))
                if best is None or sim > best[0]:
                    second = best[0] if best else second
                    best = (sim, r)
                elif sim > second:
                    second = sim
            if best is not None and best[0] >= self.threshold and best[0] - second >= self.margin:
                e = _row(best[1][:9])
                counted = now - e.last_seen >= self.min_gap
                if counted:
                    conn.execute(
                        "UPDATE entities SET last_seen = ?, sightings = sightings + 1, last_position = ?, last_scene = ?, "
                        "label = ?, identity = ? WHERE id = ?",
                        (now.isoformat(), o.position, scene[:200], o.label, o.identity, e.id),
                    )
                    conn.execute("INSERT INTO sightings (entity_id, ts, request_id, position, scene) VALUES (?, ?, ?, ?, ?)",
                                 (e.id, now.isoformat(), request_id, o.position, scene[:200]))
                return EntityRef(id=e.id, known=True, sightings=e.sightings + (1 if counted else 0),
                                 first_seen=e.first_seen, last_seen=now if counted else e.last_seen, identity=o.identity or "")
            new_id = uuid.uuid4().hex[:12]
            conn.execute(
                "INSERT INTO entities (id, category, label, identity, embedding, dim, first_seen, last_seen, sightings, "
                "last_position, last_scene) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (new_id, o.category, o.label, o.identity, vec.tobytes(), int(vec.shape[0]), now.isoformat(),
                 now.isoformat(), o.position, scene[:200]),
            )
            conn.execute("INSERT INTO sightings (entity_id, ts, request_id, position, scene) VALUES (?, ?, ?, ?, ?)",
                         (new_id, now.isoformat(), request_id, o.position, scene[:200]))
            return EntityRef(id=new_id, known=False, sightings=1, first_seen=now, last_seen=now, identity=o.identity or "")

    # --- reading --------------------------------------------------------------------------

    def list(self, limit: int = 200) -> list[Entity]:
        with self._connect() as conn:
            rows = conn.execute(f"SELECT {_ENTITY_COLS} FROM entities ORDER BY last_seen DESC LIMIT ?", (limit,)).fetchall()
        return [_row(r) for r in rows]

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0])

    def context(self, *, within_s: int = 300, limit: int = 8, now: datetime | None = None) -> str:
        """FR-6: a compact summary of entities seen in the last few minutes, for the prompt.
        Memory is read *before* a scan, so this describes the previous scans of the same
        scene, which is what "have you seen this before?" usually refers to."""
        now = now or _utcnow()
        since = (now - timedelta(seconds=within_s)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_ENTITY_COLS} FROM entities WHERE last_seen >= ? ORDER BY last_seen DESC LIMIT ?", (since, limit)
            ).fetchall()
        lines = []
        for e in map(_row, rows):
            lines.append(f"{e.label} ({e.identity}): seen {e.sightings}x since {e.first_seen.date()}, last {e.last_position}")
        return "; ".join(lines)

    # --- forgetting --------------------------------------------------------------------------

    def forget(self, entity_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
            return cur.rowcount > 0

    def forget_all(self) -> int:
        with self._connect() as conn:
            return conn.execute("DELETE FROM entities").rowcount
