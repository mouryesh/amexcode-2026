"""api/main.py — FastAPI application factory.

Imports: routes, backend.database, backend.seed.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from api.routes import router
from backend import database, docstore
from backend.database import init_db, tables_empty
from backend.seed import seed
from shared import splunk

_WEB = Path(__file__).resolve().parent.parent / "web"


def create_app() -> FastAPI:
    app = FastAPI(title="Servicing Agent API", version="1.0")

    # Configure CORS now — you will hit this at the hour-8 integration.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    def _startup() -> None:
        init_db()
        docstore.init()
        if tables_empty():
            seed()

    @app.get("/health")
    def health() -> dict:
        """Reports which engine actually resolved, so the UI can't misreport it."""
        return {
            "status": "ok",
            **database.engine_info(),
            "sessions": docstore.backend(),
            "splunk_stats": splunk.stats(),
        }

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        """The member UI. One file, no build step — deliberately."""
        return FileResponse(_WEB / "index.html")

    app.include_router(router)
    return app


app = create_app()
