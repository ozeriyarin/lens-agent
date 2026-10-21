"""HTTP surface: `POST /scan`, `GET /stats`, and the static PWA (FR-1, FR-7).

This is the only module that knows about all the others. The request path is
the architecture diagram from plan.md, in order:

    bytes -> gate.prepare -> cache.lookup -> budget.reserve -> agent.scan
          -> budget.settle -> cache.store -> ScanResponse

Each arrow is a place a request can stop early and cheaply: bad image (400),
cache hit (free), budget reached (402, nothing called), model failure (502,
estimate kept on the ledger because the provider billed the attempt). The order matters: the cache is consulted *before*
the budget so a repeated frame never costs anything, and the reservation is
taken *before* the model call so two simultaneous scans cannot both pass.

`create_app(settings, model=...)` is a factory so tests can inject a fake
model and a temp database; `app` at the bottom is what uvicorn imports.
"""


from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import secrets

from urllib.parse import unquote

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models import Model

import logging

from pydantic_ai.exceptions import UserError

from app.agent import MAX_QUESTION_CHARS, AgentFailed, build_agent
from app.budget import Budget, BudgetExceeded, ModelStats, SpendEntry
from app.config import Settings
from app.embeddings import Embeddings, hashing_embed
from app.gate import BadImage, ScanCache, prepare
from app.memory import Entity, Memory
from app.router import Router
from app.schema import ScanResponse, ScanResult

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
MAX_UPLOAD_BYTES = 12 * 1024 * 1024  # phones produce 3-8 MB JPEGs; anything bigger is not a photo


@dataclass
class Services:
    """Everything a request needs, built once at startup."""

    settings: Settings
    budget: Budget
    cache: ScanCache[ScanResponse]
    router: Router
    model_name: str
    memory: Memory


class StatsResponse(BaseModel):
    model: str
    escalation_model: str | None
    memory: bool
    entities: int
    cap_usd: float
    month_to_date_usd: float
    remaining_usd: float
    scans_this_month: int
    scans_by_source: dict[str, int]
    escalations: int
    cache_hits: int
    cache_misses: int
    per_model: list[ModelStats]
    recent: list[SpendEntry]


def _model_label(model: str | Model) -> str:
    return model if isinstance(model, str) else getattr(model, "model_name", "model")


def _build_escalation(settings: Settings) -> tuple[Agent[None, ScanResult] | None, str]:
    """The second tier is optional: no model configured, or no key for it, = no escalation."""
    name = settings.escalation_model
    if not name:
        return None, ""
    try:
        return build_agent(name), name
    except UserError as e:  # e.g. ANTHROPIC_API_KEY missing; Pydantic AI raises at construction
        log.warning("escalation disabled: %s", str(e).splitlines()[0])
        return None, ""


def _demo_model() -> Model:
    """`MODEL=test`: Pydantic AI's offline TestModel with a canned desk scene, so the
    whole app can be clicked through without an API key or a cent of spend."""
    from pydantic_ai.models.test import TestModel

    return TestModel(
        custom_output_args={
            "scene": "demo mode: a desk with a laptop and a mug (no real model was called)",
            "objects": [
                {"label": "laptop", "category": "electronics", "position": "on_table", "confidence": 0.95,
                 "bbox": {"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.4},
                 "identity": "silver 14-inch laptop with a round blue sticker on the lid"},
                {"label": "coffee mug", "category": "other", "position": "on_table", "confidence": 0.8,
                 "identity": "white ceramic mug with a blue rim and a chipped handle"},
                {"label": "person", "category": "person", "position": "sitting", "confidence": 0.7,
                 "person": {"mood": {"value": "neutral", "confidence": 0.5},
                            "age_range": {"value": "adult", "confidence": 0.6}}},
            ],
        }
    )


def create_app(settings: Settings | None = None, model: str | Model | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    chosen_model = model if model is not None else settings.model
    if chosen_model == "test":
        chosen_model = _demo_model()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        budget = Budget(settings.db_path, settings.budget_usd)
        primary_name = _model_label(chosen_model)
        # Demo mode and injected fake models never escalate: the second tier is a real-key feature.
        escalation, escalation_name = (None, "") if not isinstance(chosen_model, str) else _build_escalation(settings)
        # Memory (004): real embeddings with a real model, the offline hashing embedder otherwise,
        # so demo mode shows "known" badges without a key. Same SQLite file as the ledger.
        embeddings = None
        if settings.memory:
            embeddings = (Embeddings(settings.embedding_model, budget) if isinstance(chosen_model, str)
                          else Embeddings("hashing", budget, embed_fn=hashing_embed))
        app.state.services = Services(
            settings=settings,
            budget=budget,
            cache=ScanCache(capacity=settings.cache_size),
            router=Router(
                budget,
                build_agent(chosen_model), primary_name,
                escalation, escalation_name,
                escalate_below=settings.escalate_below,
            ),
            model_name=primary_name,
            memory=Memory(settings.db_path, embeddings, threshold=settings.memory_threshold),
        )
        yield

    app = FastAPI(title="lens-agent", version="0.1.0", lifespan=lifespan)

    def services(request: Request, authorization: Annotated[str | None, Header()] = None) -> Services:
        """Resolve the shared services and, when SCAN_TOKEN is set, require it.

        The token guards the *budget*, not the data: with the server on a public
        tunnel, anyone who finds the URL could otherwise spend the month's cap.
        """
        svc: Services = request.app.state.services
        expected = svc.settings.scan_token
        if expected:
            presented = (authorization or "").removeprefix("Bearer ").strip()
            if not secrets.compare_digest(presented, expected):  # constant-time compare
                raise HTTPException(401, "missing or wrong token", headers={"WWW-Authenticate": "Bearer"})
        return svc

    Svc = Annotated[Services, Depends(services)]

    @app.post("/scan", response_model=ScanResponse, openapi_extra={
        "requestBody": {"required": True, "content": {
            "image/jpeg": {"schema": {"type": "string", "format": "binary"}},
            "image/png": {"schema": {"type": "string", "format": "binary"}}}}})
    async def scan_frame(
        svc: Svc,
        request: Request,
        x_question: Annotated[str | None, Header(description="Optional question, URL-encoded UTF-8")] = None,
        x_live: Annotated[str | None, Header(description="'1' when sent by the live loop (feature 003)")] = None,
    ) -> ScanResponse:
        source = "live" if x_live == "1" else "tap"
        # The frame is the raw request body, not a multipart form: Starlette spools
        # multipart parts over 1 MB to a temp file on disk, and constitution IV says raw
        # frames never touch disk. Reading the stream ourselves also lets us stop at the
        # byte limit instead of ingesting the whole upload first.
        if not (request.headers.get("content-type") or "").startswith("image/"):
            raise HTTPException(415, "send the frame as the request body with Content-Type image/jpeg or image/png")
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "image larger than 12 MB")
            chunks.append(chunk)
        raw = b"".join(chunks)
        question = unquote(x_question) if x_question else None
        if question and len(question) > MAX_QUESTION_CHARS:
            raise HTTPException(422, f"question longer than {MAX_QUESTION_CHARS} characters")
        try:
            frame = prepare(raw)  # FR-3: downscaled, in memory only
        except BadImage as e:
            raise HTTPException(400, str(e)) from e

        if (hit := svc.cache.lookup(frame.phash, question)) is not None:  # FR-4: free
            return hit.model_copy(update={"cached": True, "cost_usd": 0.0, "latency_ms": 0})

        # The router does reserve -> scan -> settle/fail for each tier (001 FR-5/FR-6, 002 FR-1/FR-2).
        # Memory context (004 FR-6) is read before the scan and only used with a question.
        context = svc.memory.context() if (question and svc.memory.enabled) else None
        try:
            routed = await svc.router.run(frame, question, source=source, context=context or None)
        except BudgetExceeded as e:
            raise HTTPException(402, str(e)) from e
        except AgentFailed as e:
            # Generic on purpose: the detail is in the server log (see agent.scan).
            raise HTTPException(502, "the model could not produce a valid result; try again") from e

        # Memory (004): link detections to known entities. After the scan, never fatal.
        entities = await svc.memory.link(routed.result, routed.request_id, source=source)
        response = ScanResponse(
            result=routed.result,
            cached=False,
            request_id=routed.request_id,
            model=routed.model,
            primary_model=routed.primary_model,
            escalated=routed.escalated,
            cost_usd=routed.cost_usd,
            latency_ms=routed.latency_ms,
            entities=entities,
        )
        # An empty result ("nothing identifiable") is not cached: a repeated near-identical
        # frame deserves a fresh look, since blur or a bad exposure may have cleared by then.
        if not routed.result.is_empty:
            svc.cache.store(frame.phash, question, response)
        return response

    @app.get("/stats", response_model=StatsResponse)
    def stats(svc: Svc) -> StatsResponse:
        entries = svc.budget.entries()
        return StatsResponse(
            model=svc.model_name,
            escalation_model=svc.router.escalation_model or None,
            memory=svc.memory.enabled,
            entities=svc.memory.count(),
            cap_usd=svc.budget.cap_usd,
            month_to_date_usd=svc.budget.month_to_date(),
            remaining_usd=svc.budget.remaining(),
            scans_this_month=sum(1 for e in entries if e.tier == "primary"),
            scans_by_source=svc.budget.scans_by_source(),
            escalations=svc.budget.escalations(),
            cache_hits=svc.cache.hits,
            cache_misses=svc.cache.misses,
            per_model=svc.budget.per_model(),
            recent=entries[-20:],
        )

    # --- memory (004 FR-7) ---------------------------------------------------------------

    def memory_access(svc: Svc, request: Request) -> Services:
        """Memory is personal data. With SCAN_TOKEN set the bearer check above already
        applies. Without one, these routes answer only to the local machine (no proxy
        header, loopback client), so a tunnel or LAN exposure cannot read or wipe it."""
        if not svc.settings.scan_token:
            host = request.client.host if request.client else ""
            if host not in ("127.0.0.1", "::1", "testclient") or request.headers.get("x-forwarded-for"):
                raise HTTPException(401, "set SCAN_TOKEN to access memory from outside this machine")
        return svc

    Mem = Annotated[Services, Depends(memory_access)]

    @app.get("/entities", response_model=list[Entity])
    def list_entities(svc: Mem) -> list[Entity]:
        return svc.memory.list()

    @app.delete("/entities/{entity_id}", status_code=204)
    def forget_entity(svc: Mem, entity_id: str) -> None:
        if not svc.memory.forget(entity_id):
            raise HTTPException(404, "no such entity")

    @app.delete("/entities")
    def forget_all_entities(svc: Mem) -> dict[str, int]:
        return {"forgotten": svc.memory.forget_all()}

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app


app = create_app()
