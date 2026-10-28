"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .agent_client import AgentClient
from .api import auth as auth_api
from .api import system as system_api
from .api import websites as websites_api
from .config import Config
from .db import migrate

# Vue compiles its templates in the browser, which needs 'unsafe-eval'.
# Everything is served from the panel itself; nothing is loaded from third parties.
CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-eval'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


def create_app(config: Config) -> FastAPI:
    migrate(config.db_path)

    app = FastAPI(
        title="Tela",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config = config
    app.state.agent = AgentClient(config.agent_socket)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if config.tls_enabled:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(auth_api.router)
    app.include_router(system_api.router)
    app.include_router(websites_api.router)

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__}

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def api_not_found(rest: str):
        return JSONResponse({"detail": "not found"}, status_code=404)

    web_dir = config.web_dir
    app.mount("/static", StaticFiles(directory=web_dir), name="static")
    index = web_dir / "index.html"

    @app.api_route("/{rest:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def spa(rest: str):
        # The frontend handles its own routes; always serve the app shell.
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app
