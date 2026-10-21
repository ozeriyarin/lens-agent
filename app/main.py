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

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models import Model

from app.agent import MAX_QUESTION_CHARS, AgentFailed, build_agent, scan
from app.budget import Budget, BudgetExceeded, SpendEntry, estimate_scan_cost
from app.config import Settings
from app.gate import BadImage, ScanCache, prepare
from app.schema import ScanResponse, ScanResult

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
MAX_UPLOAD_BYTES = 12 * 1024 * 1024  # phones produce 3-8 MB JPEGs; anything bigger is not a photo


@dataclass
class Services:
    """Everything a request needs, built once at startup."""

    settings: Settings
    budget: Budget
    cache: ScanCache[ScanResponse]
    agent: Agent[None, ScanResult]
    model_name: str


class StatsResponse(BaseModel):
    model: str
    cap_usd: float
    month_to_date_usd: float
    remaining_usd: float
    scans_this_month: int
    cache_hits: int
    cache_misses: int
    recent: list[SpendEntry]


def create_app(settings: Settings | None = None, model: str | Model | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    chosen_model = model if model is not None else settings.model

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.services = Services(
            settings=settings,
            budget=Budget(settings.db_path, settings.budget_usd),
            cache=ScanCache(capacity=settings.cache_size),
            agent=build_agent(chosen_model),
            model_name=chosen_model if isinstance(chosen_model, str) else getattr(chosen_model, "model_name", "model"),
        )
        yield

    app = FastAPI(title="lens-agent", version="0.1.0", lifespan=lifespan)

    def services(request: Request) -> Services:
        return request.app.state.services

    Svc = Annotated[Services, Depends(services)]

    @app.post("/scan", response_model=ScanResponse)
    async def scan_frame(
        svc: Svc,
        image: Annotated[UploadFile, File(description="JPEG/PNG frame from the camera")],
        question: Annotated[str | None, Form(max_length=MAX_QUESTION_CHARS)] = None,
    ) -> ScanResponse:
        raw = await image.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "image larger than 12 MB")
        try:
            frame = prepare(raw)  # FR-3: downscaled, in memory only
        except BadImage as e:
            raise HTTPException(400, str(e)) from e

        if (hit := svc.cache.lookup(frame.phash, question)) is not None:  # FR-4: free
            return hit.model_copy(update={"cached": True, "cost_usd": 0.0, "latency_ms": 0})

        estimate = estimate_scan_cost(svc.model_name)
        try:
            request_id = svc.budget.reserve(svc.model_name, estimate)  # FR-5: atomic
        except BudgetExceeded as e:
            raise HTTPException(402, str(e)) from e

        try:
            outcome = await scan(svc.agent, frame, question)
        except AgentFailed as e:
            # The provider still billed those tokens: keep the estimate on the ledger.
            svc.budget.fail(request_id)
            raise HTTPException(502, f"model could not produce a valid result: {e}") from e
        except Exception:
            svc.budget.fail(request_id)  # unknown failure: assume money was spent
            raise

        entry = svc.budget.settle(  # FR-6: real tokens, cost, latency
            request_id,
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
            latency_ms=outcome.latency_ms,
        )
        response = ScanResponse(
            result=outcome.result,
            cached=False,
            request_id=entry.request_id,
            model=svc.model_name,
            cost_usd=entry.cost_usd,
            latency_ms=entry.latency_ms,
        )
        svc.cache.store(frame.phash, question, response)
        return response

    @app.get("/stats", response_model=StatsResponse)
    def stats(svc: Svc) -> StatsResponse:
        entries = svc.budget.entries()
        return StatsResponse(
            model=svc.model_name,
            cap_usd=svc.budget.cap_usd,
            month_to_date_usd=svc.budget.month_to_date(),
            remaining_usd=svc.budget.remaining(),
            scans_this_month=len(entries),
            cache_hits=svc.cache.hits,
            cache_misses=svc.cache.misses,
            recent=entries[-20:],
        )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app


app = create_app()
