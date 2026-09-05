"""FastAPI app — POST /ask on :8001.

CORS is wide open (including the ``null`` origin a file:// console sends)
because the API is read-only over client-safe data.

Optional bearer auth: when the ``SARATHI_TOKEN`` environment variable is
set at app creation, POST /ask requires ``Authorization: Bearer <token>``
and answers 401 (JSON) otherwise. GET /health stays open either way but
reports ``auth_required``. When the env var is unset, behaviour is
unchanged (fully open).

Run:  uvicorn sarathi.app:app --port 8001
"""
from __future__ import annotations

import hmac
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import Settings, load_settings
from .service import CopilotService
from .settings_sync import SettingsSync
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
    grounding: str
    meta: dict
    latency_ms: int


def create_app(
    transport: Transport | None = None,
    settings: Settings | None = None,
    completion_fn=None,
    settings_sync: SettingsSync | None = None,
) -> FastAPI:
    """App factory. Tests pass a StaticTransport (and optionally a fake
    ``completion_fn`` for the LLM seam); production uses Supabase +
    litellm.

    ``settings_sync`` is constructed but never ``.start()``-ed here — see
    the module-level entrypoint at the bottom of this file, which is the
    only place that starts the background poller. Tests that don't pass
    one get a real-but-never-started ``SettingsSync`` whose overrides stay
    empty forever, so ``.get(key)`` is exactly ``os.environ.get(key)`` —
    identical to today's behaviour, no test changes required.
    """
    settings = settings or load_settings()
    transport = transport or SupabaseTransport(
        settings.supabase_url, settings.supabase_key
    )
    settings_sync = settings_sync or SettingsSync(
        settings.supabase_url, settings.supabase_key
    )
    service = CopilotService(
        settings, transport, completion_fn=completion_fn, settings_sync=settings_sync
    )

    app = FastAPI(title="sarathi", version=__version__)
    app.state.service = service
    app.state.settings_sync = settings_sync

    # file:// pages send Origin: null — allow_origins=["*"] covers it as long
    # as credentials stay disabled (they do; the anon key is baked in).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _authorized(request: Request) -> bool:
        """True when no token is configured, or the caller presented it.

        Reads the live SARATHI_TOKEN (table override else env var) on
        every request instead of a snapshot taken at app-creation time, so
        an admin rotating the token from the console takes effect on the
        very next request.
        """
        token = request.app.state.settings_sync.get("SARATHI_TOKEN") or None
        if token is None:
            return True
        supplied = request.headers.get("authorization") or ""
        # Constant-time comparison of the full header vs the expectation:
        # covers wrong scheme, wrong token, and absent header alike.
        return hmac.compare_digest(
            supplied.encode("utf-8"), f"Bearer {token}".encode("utf-8")
        )

    @app.post("/ask", response_model=AskResponse)
    def ask(req: AskRequest, request: Request):
        if not _authorized(request):
            return JSONResponse(
                status_code=401,
                content={
                    "error": "unauthorized",
                    "detail": "missing or invalid bearer token "
                              "(Authorization: Bearer <SARATHI_TOKEN>)",
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
        t0 = time.perf_counter()
        result = request.app.state.service.ask(req.question.strip())
        return AskResponse(
            answer=result.answer,
            evidence=[EvidenceItem(**e) for e in result.evidence],
            tier=result.tier,
            grounding=result.grounding,
            meta=result.meta,
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )

    @app.get("/health")
    def health(request: Request) -> dict:
        payload = request.app.state.service.health()
        payload["auth_required"] = (
            request.app.state.settings_sync.get("SARATHI_TOKEN") is not None
        )
        return payload

    return app


# Default ASGI entrypoint for `uvicorn sarathi.app:app --port 8001`. Only
# this module-level production entrypoint starts the poller — create_app()
# itself never does, so every test that calls create_app() directly keeps
# getting an inert, never-started SettingsSync (see create_app's docstring).
app = create_app()
app.state.settings_sync.start()
