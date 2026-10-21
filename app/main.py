"""App setup only. Agents never edit this file; features live in app/features/<name>.py."""

import importlib
import os
import pkgutil
import time

from fastapi import FastAPI

from app import features, telemetry
from app.store import make_store

REVISION = os.environ.get("K_REVISION", "local")


class RevisionAndTiming:
    """Pure ASGI middleware: tags every response with the serving revision and matched route,
    and writes one structured log line per request (no visitor IPs)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        start = time.perf_counter()
        status = {"code": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                route = scope.get("route")
                template = getattr(route, "path", None) or "unmatched"
                headers = list(message.get("headers", []))
                headers.append((b"x-revision", REVISION.encode()))
                headers.append((b"x-route", f"{scope['method']} {template}".encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = scope.get("route")
            telemetry.log_request(
                method=scope["method"],
                route=getattr(route, "path", None) or "unmatched",
                status=status["code"],
                ms=(time.perf_counter() - start) * 1000,
                revision=REVISION,
            )


def discover_features():
    """Every module in app/features with a `router` is a feature, loaded in name order."""
    found = []
    for info in sorted(pkgutil.iter_modules(features.__path__), key=lambda m: m.name):
        module = importlib.import_module(f"{features.__name__}.{info.name}")
        if hasattr(module, "router"):
            found.append(module)
    return found


def create_app(store=None) -> FastAPI:
    telemetry.setup()
    app = FastAPI(title="Vallum link shortener", docs_url=None, redoc_url=None)
    app.state.store = store or make_store()
    app.state.revision = REVISION
    app.state.features = []
    for module in discover_features():
        app.include_router(module.router)
        app.state.features.append(module.__name__.rsplit(".", 1)[-1])
    app.add_middleware(RevisionAndTiming)
    return app


app = create_app()
