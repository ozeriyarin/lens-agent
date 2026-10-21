"""Text embeddings under the budget guard (feature 004).

Why a wrapper: the framework's `Embedder` does the API call; what the project needs on
top is the same discipline every paid call has (reserve -> call -> settle/fail on the
ledger, tier 'embedding'), a conservative token estimate before the call, and a quiet
failure mode: memory is a nice-to-have on top of a scan, so an embedding error returns
None and the scan still succeeds.

`embed_fn` lets tests (and offline runs) supply a deterministic function instead of a
provider: `(texts) -> (vectors, input_tokens)`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from pydantic_ai.embeddings import Embedder, EmbeddingModel

from app.budget import Budget, BudgetExceeded, estimate_cost

log = logging.getLogger(__name__)

EmbedFn = Callable[[list[str]], Awaitable[tuple[list[list[float]], int]]]


class Embeddings:
    def __init__(self, model: str | EmbeddingModel, budget: Budget, *, embed_fn: EmbedFn | None = None) -> None:
        self.model_name = model if isinstance(model, str) else getattr(model, "model_name", "embedding")
        self.budget = budget
        self._embed_fn = embed_fn
        self._embedder = None if embed_fn else Embedder(model)

    @staticmethod
    def estimate_tokens(texts: list[str]) -> int:
        return sum(len(t) // 3 + 2 for t in texts)  # ~3 chars/token for short English, rounded up

    async def embed(self, texts: list[str], *, source: str = "tap") -> list[list[float]] | None:
        """Vectors for `texts`, or None when the budget refused or the provider failed."""
        if not texts:
            return []
        est_tokens = self.estimate_tokens(texts)
        try:
            rid = self.budget.reserve(self.model_name, estimate_cost(self.model_name, est_tokens, 0),
                                      tier="embedding", source=source)
        except BudgetExceeded as e:
            log.info("embedding skipped, budget: %s", e)
            return None
        started = time.perf_counter()
        try:
            if self._embed_fn:
                vectors, used = await self._embed_fn(texts)
            else:
                result = await self._embedder.embed_documents(texts)
                vectors = [list(map(float, v)) for v in result.embeddings]
                used = result.usage.input_tokens or est_tokens
        except Exception as e:
            self.budget.fail(rid, input_tokens=est_tokens, latency_ms=int((time.perf_counter() - started) * 1000))
            log.warning("embedding failed: %s: %s", type(e).__name__, str(e)[:200])
            return None
        self.budget.settle(rid, input_tokens=used, output_tokens=0, latency_ms=int((time.perf_counter() - started) * 1000))
        return vectors


async def hashing_embed(texts: list[str]) -> tuple[list[list[float]], int]:
    """Deterministic offline embedding: character-trigram hashing into 128 dims, L2-normalised.
    Similar strings land close together, which is all tests and demo mode need."""
    import hashlib

    dim = 128
    out: list[list[float]] = []
    for t in texts:
        v = [0.0] * dim
        s = f"  {t.lower()}  "
        for i in range(len(s) - 2):
            h = int(hashlib.blake2b(s[i:i + 3].encode(), digest_size=4).hexdigest(), 16)
            v[h % dim] += 1.0 if (h >> 31) & 1 else -1.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        out.append([x / norm for x in v])
    return out, Embeddings.estimate_tokens(texts)
