"""The control room's routes. Deployed as its own Cloud Run service (vallum-status) from the app's image with
APP_MODULE=control.main:app. Reads only vallum_state/* documents, with short caches so a demo audience can't load
the state store; never the secrets."""

import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.store import make_store
from control import ui, views

APP_URL = os.environ.get("APP_URL", "https://vallum-256944994567.us-central1.run.app").rstrip("/")
DOCS = ("release", "modes", "recent", "queue", "meta", "public", "security", "flags")
STATE_SECONDS, LIVE_SECONDS, DOCS_SECONDS = 20, 2, 60


class Cache:
    def __init__(self, seconds: float):
        self.seconds, self.at, self.data = seconds, 0.0, None

    def get(self, load):
        if self.data is not None and time.monotonic() - self.at < self.seconds:
            return self.data
        try:
            self.data = load()
        except Exception:  # the pages must render even if the state store is unreachable
            if self.data is None:
                return {"error": True}
        self.at = time.monotonic()
        return self.data


def create_app(store=None) -> FastAPI:
    app = FastAPI(title="Vallum control room", docs_url=None, redoc_url=None)
    app.state.store = store or make_store()
    state, live, docs = Cache(STATE_SECONDS), Cache(LIVE_SECONDS), Cache(DOCS_SECONDS)

    def data() -> dict:
        got = state.get(lambda: {n: app.state.store.read_doc("vallum_state", n) or {} for n in DOCS})
        return {n: got.get(n) or {} for n in DOCS} | {"error": bool(got.get("error"))}

    def live_payload(d: dict) -> dict:
        drive = live.get(lambda: app.state.store.read_doc("vallum_state", "drive_live") or {})
        release = d.get("release") or {}
        return {"drive": {} if drive.get("error") else drive, "project_url": d["public"].get("project_url") or "",
                "release": {k: release.get(k) for k in ("slot", "release_id", "feature", "step", "released_at")}}

    def html(title: str, active: str, body: str, d: dict, *, script: str = "", status: int = 200, cache: int = 15):
        if d.get("error"):
            body = '<p class="note bad">The state store could not be read just now; showing what is known.</p>' + body
        if d["public"].get("rolled_back"):
            body = ('<p class="note info">Production was rolled back on purpose to an earlier version; live checks of '
                    'newer features wait for the next release.</p>') + body
        return HTMLResponse(ui.page(title, active, body, app_url=APP_URL, links=d["public"], css=views.CSS, script=script),
                            status_code=status, headers={"Cache-Control": f"public, max-age={cache}"})

    def switches(d: dict) -> dict:
        return (d.get("flags") or {}).get("flags") or {}

    @app.get("/", response_class=HTMLResponse)
    def overview():
        d = data()
        return html("Overview", "overview", views.overview(d, live_payload(d), switches(d)), d, script=views.LIVE_SCRIPT)

    @app.get("/releases", response_class=HTMLResponse)
    def releases():
        d = data()
        return html("Releases", "releases", views.releases(d, live_payload(d)), d, script=views.LIVE_SCRIPT)

    @app.get("/features", response_class=HTMLResponse)
    def features():
        d = data()
        return html("Features", "features", views.features_page(d["public"], switches(d)), d, script=views.FILTER_SCRIPT)

    @app.get("/features/{name}", response_class=HTMLResponse)
    def feature(name: str):
        d = data()
        body = views.feature_page(d["public"], switches(d), name)
        if body is None:
            return html("Not found", "features", views.phead("No such feature", f'There is no live feature called '
                        f'“{ui.esc(name)}”. <a href="/features">All features</a>.'), d, status=404)
        return html(name, "features", body, d)

    @app.get("/allowance", response_class=HTMLResponse)
    def allowance():
        d = data()
        return html("Mistake allowance", "allowance", views.allowance(d), d)

    @app.get("/security", response_class=HTMLResponse)
    def security():
        d = data()
        return html("Security", "security", views.security(d["security"], d["public"].get("project_url") or ""), d)

    @app.get("/rules", response_class=HTMLResponse)
    @app.get("/rules/{doc}", response_class=HTMLResponse)
    def rules(doc: str | None = None):
        d = data()
        copies = docs.get(lambda: app.state.store.read_doc("vallum_state", "docs") or {})
        body = views.rules({} if copies.get("error") else copies, d["public"].get("project_url") or "", doc)
        if body is None:
            return html("Not found", "rules", views.phead("No such document", 'See <a href="/rules">the rules</a>.'), d, status=404)
        return html("Rules and code", "rules", body, d, cache=60)

    @app.get("/live.json")
    def live_json():
        return JSONResponse(live_payload(data()), headers={"Cache-Control": "no-store"})

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


app = create_app()
