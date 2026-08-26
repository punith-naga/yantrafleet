"""FastAPI app — POST /ask on :8001.

CORS is wide open (including the ``null`` origin a file:// console sends)
because the API is read-only over client-safe data.

Run:  uvicorn sarathi.app:app --port 8001
"""
from __future__ import annotations

import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import __version__
from .config import Settings, load_settings
from .service import CopilotService
from .transport import SupabaseTransport, Transport


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class EvidenceItem(BaseModel):
    label: str
    ref: str


class AskResponse(BaseModel):
    answer: str
    evidence: list[EvidenceItem]
    tier: str
    latency_ms: int


def create_app(
    transport: Transport | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """App factory. Tests pass a StaticTransport; production uses Supabase."""
    settings = settings or load_settings()
    transport = transport or SupabaseTransport(
        settings.supabase_url, settings.supabase_key
    )
    service = CopilotService(settings, transport)

    app = FastAPI(title="sarathi", version=__version__)
    app.state.service = service

    # file:// pages send Origin: null — allow_origins=["*"] covers it as long
    # as credentials stay disabled (they do; the anon key is baked in).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.post("/ask", response_model=AskResponse)
    def ask(req: AskRequest, request: Request) -> AskResponse:
        t0 = time.perf_counter()
        result = request.app.state.service.ask(req.question.strip())
        return AskResponse(
            answer=result.answer,
            evidence=[EvidenceItem(**e) for e in result.evidence],
            tier=result.tier,
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )

    @app.get("/health")
    def health(request: Request) -> dict:
        return request.app.state.service.health()

    return app


# Default ASGI entrypoint for `uvicorn sarathi.app:app --port 8001`.
app = create_app()
