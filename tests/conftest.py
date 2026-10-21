import os

import pytest

os.environ.setdefault("APP_STORE", "memory")
os.environ.setdefault("APP_API_KEY", "test-key")
os.environ.setdefault("APP_LOG_REQUESTS", "0")


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.store import MemoryStore

    app = create_app(store=MemoryStore())
    with TestClient(app, follow_redirects=False) as c:
        yield c
