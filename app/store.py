"""Link storage. Firestore in the cloud and in CI (emulator); memory for unit tests."""

import os
import threading
import time
from datetime import datetime, timezone

LINKS = "links"
CACHE_SECONDS = 60


class LinkExists(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


class MemoryStore:
    def __init__(self):
        self._links = {}
        self._docs = {}
        self._lock = threading.Lock()

    def create(self, code, url):
        with self._lock:
            if code in self._links:
                raise LinkExists(code)
            link = {"code": code, "url": url, "created_at": _now()}
            self._links[code] = link
            return dict(link)

    def get(self, code):
        link = self._links.get(code)
        return dict(link) if link else None

    def delete(self, code):
        with self._lock:
            return self._links.pop(code, None) is not None

    def read_doc(self, collection, doc_id):
        return self._docs.get((collection, doc_id))

    def write_doc(self, collection, doc_id, data):
        self._docs[(collection, doc_id)] = data


class FirestoreStore:
    """Links in the `links` collection, with a short per-instance cache for redirects."""

    def __init__(self, project=None):
        from google.cloud import firestore

        kwargs = {"project": project or os.environ.get("GOOGLE_CLOUD_PROJECT") or "vallum-512306"}
        if os.environ.get("FIRESTORE_EMULATOR_HOST"):
            from google.auth.credentials import AnonymousCredentials

            kwargs["credentials"] = AnonymousCredentials()
        self.client = firestore.Client(**kwargs)
        self._cache = {}

    def create(self, code, url):
        from google.api_core.exceptions import AlreadyExists

        link = {"code": code, "url": url, "created_at": _now()}
        try:
            self.client.collection(LINKS).document(code).create(link)
        except AlreadyExists as exc:
            raise LinkExists(code) from exc
        self._cache[code] = (time.monotonic(), link)
        return dict(link)

    def get(self, code):
        hit = self._cache.get(code)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return dict(hit[1]) if hit[1] else None
        snap = self.client.collection(LINKS).document(code).get()
        link = snap.to_dict() if snap.exists else None
        self._cache[code] = (time.monotonic(), link)
        return dict(link) if link else None

    def delete(self, code):
        ref = self.client.collection(LINKS).document(code)
        existed = ref.get().exists
        if existed:
            ref.delete()
        self._cache.pop(code, None)
        return existed

    def read_doc(self, collection, doc_id):
        snap = self.client.collection(collection).document(doc_id).get()
        return snap.to_dict() if snap.exists else None

    def write_doc(self, collection, doc_id, data):
        self.client.collection(collection).document(doc_id).set(data)


def make_store():
    choice = os.environ.get("APP_STORE")
    if choice == "memory":
        return MemoryStore()
    if choice == "firestore" or os.environ.get("FIRESTORE_EMULATOR_HOST") or os.environ.get("K_SERVICE"):
        return FirestoreStore()
    return MemoryStore()
