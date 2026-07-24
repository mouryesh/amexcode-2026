"""api/main.py — FastAPI application factory.

Imports: routes, backend.database, backend.seed.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import router
from backend.database import init_db, tables_empty
from backend.seed import seed


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
        if tables_empty():
            seed()

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    app.include_router(router)
    return app


app = create_app()
