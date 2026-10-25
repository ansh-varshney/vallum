"""The state store (FR-STATE-1). Firestore documents changed only by transactions from main-pipeline
jobs. The Ledger issue is a mirror of this, rewritten by the Notifier.

Documents:
  vallum_state/release   the release slot: idle | merged | driving, plus the current release
  vallum_state/meta      counters, Ledger issue number, Safe mode, circuit breaker
  vallum_state/modes     allowance and mode per tier (Layer 3 computes these)
  vallum_state/recent    the last events, newest first (status page and Ledger)
  vallum_sanctioned/<sha>   MR head commits the Merge Desk merged (provenance gate)
  vallum_releases/<id>      one audit summary per release
  vallum_status/<target>    timeline behind each live status comment
  vallum_leases/<key>       work-item leases for the Watchdog
"""

import copy
import threading

from vallum.config import iso

STATE = "vallum_state"
RECENT_MAX = 30


class Abort(Exception):
    """Raised inside a transaction function to abort it without writing."""


class MemoryState:
    def __init__(self):
        self.docs = {}
        self.lock = threading.Lock()

    def read(self, collection, key):
        doc = self.docs.get((collection, key))
        return copy.deepcopy(doc) if doc is not None else None

    def put(self, collection, key, data):
        with self.lock:
            self.docs[(collection, key)] = copy.deepcopy(data)

    def delete(self, collection, key):
        with self.lock:
            self.docs.pop((collection, key), None)

    def list(self, collection):
        return [(k, copy.deepcopy(v)) for (c, k), v in self.docs.items() if c == collection]

    def transact(self, collection, key, fn):
        with self.lock:
            current = copy.deepcopy(self.docs.get((collection, key)) or {})
            new = fn(current)
            self.docs[(collection, key)] = copy.deepcopy(new)
            return copy.deepcopy(new)


class FirestoreState:
    def __init__(self, client):
        self.client = client

    def _ref(self, collection, key):
        return self.client.collection(collection).document(key)

    def read(self, collection, key):
        snap = self._ref(collection, key).get()
        return snap.to_dict() if snap.exists else None

    def put(self, collection, key, data):
        self._ref(collection, key).set(data)

    def delete(self, collection, key):
        self._ref(collection, key).delete()

    def list(self, collection):
        return [(d.id, d.to_dict()) for d in self.client.collection(collection).stream()]

    def transact(self, collection, key, fn):
        from google.cloud import firestore

        ref = self._ref(collection, key)

        @firestore.transactional
        def _run(transaction):
            snap = ref.get(transaction=transaction)
            current = snap.to_dict() if snap.exists else {}
            new = fn(copy.deepcopy(current))
            transaction.set(ref, new)
            return new

        return _run(self.client.transaction())


class Store:
    """Domain helpers on top of either backend."""

    def __init__(self, backend):
        self.b = backend

    @classmethod
    def from_env(cls) -> "Store":
        from vallum import gcp

        return cls(FirestoreState(gcp.firestore_client()))

    # generic
    def doc(self, name: str) -> dict:
        return self.b.read(STATE, name) or {}

    def update(self, name: str, fn):
        return self.b.transact(STATE, name, fn)

    # release slot ------------------------------------------------------------
    def release(self) -> dict:
        r = self.doc("release")
        r.setdefault("slot", "idle")
        return r

    def live_commit(self, service: str) -> str | None:
        """The commit visitors are on now: recorded at promotion, or read from the revision name
        (<service>-<sha7>-<pipeline>) for releases made before it was recorded."""
        import re

        rel = self.release()
        # last_good is what serves now: it moves on promotion and on every rollback
        if rel.get("promoted_commit") and rel.get("promoted_revision") == rel.get("last_good"):
            return rel["promoted_commit"]
        m = re.match(re.escape(service) + r"-([0-9a-f]{7,40})-", rel.get("last_good") or "")
        return m.group(1) if m else rel.get("promoted_commit")

    def claim_release(self, expect_slots: tuple, **fields) -> dict:
        def fn(cur):
            slot = cur.get("slot", "idle")
            if slot not in expect_slots:
                raise Abort(f"release slot is {slot}, expected one of {expect_slots}")
            cur.update(fields)
            cur["updated_at"] = iso()
            return cur

        return self.update("release", fn)

    def set_release(self, **fields) -> dict:
        def fn(cur):
            cur.update(fields)
            cur["updated_at"] = iso()
            return cur

        return self.update("release", fn)

    def next_release_id(self) -> str:
        meta = self.update("meta", lambda cur: {**cur, "release_counter": int(cur.get("release_counter", 0)) + 1})
        return f"r-{meta['release_counter']:04d}"

    # provenance --------------------------------------------------------------
    def sanction(self, sha: str, **info):
        self.b.put("vallum_sanctioned", sha, {"at": iso(), **info})

    def sanctioned(self, sha: str) -> dict | None:
        return self.b.read("vallum_sanctioned", sha)

    def unsanction(self, sha: str):
        self.b.delete("vallum_sanctioned", sha)

    # events ------------------------------------------------------------------
    def event(self, kind: str, summary: str, **data) -> dict:
        entry = {"at": iso(), "kind": kind, "summary": summary, **data}

        def fn(cur):
            events = [entry] + list(cur.get("events", []))
            return {"events": events[:RECENT_MAX]}

        self.update("recent", fn)
        return entry

    # circuit breaker (FR-LOOP-3) ----------------------------------------------
    def record_rollback(self, window_hours: float, limit: int) -> bool:
        """Count a rollback; returns True if the circuit breaker tripped (Safe mode on)."""
        from datetime import timedelta

        from vallum.config import parse_iso, utcnow

        tripped = {"now": False}

        def fn(cur):
            tripped["now"] = False
            cutoff = utcnow() - timedelta(hours=window_hours)
            recent = [t for t in cur.get("rollbacks", []) if parse_iso(t) and parse_iso(t) > cutoff]
            recent.append(iso())
            cur["rollbacks"] = recent
            if len(recent) >= limit and not cur.get("safe_mode"):
                cur["safe_mode"] = True
                cur["safe_mode_reason"] = f"circuit breaker: {len(recent)} rollbacks within {window_hours:g} h"
                cur["safe_mode_at"] = iso()
                tripped["now"] = True
            return cur

        self.update("meta", fn)
        return tripped["now"]

    # status timelines ----------------------------------------------------------
    def timeline(self, target: str) -> dict:
        return self.b.read("vallum_status", target) or {}

    def add_milestone(self, target: str, step: str, state: str, note: str = "", **header) -> dict:
        def fn(cur):
            cur.update({k: v for k, v in header.items() if v is not None})
            ms = list(cur.get("milestones", []))
            for m in ms:
                if m["step"] == step:
                    m.update(state=state, at=iso(), note=note)
                    break
            else:
                ms.append({"step": step, "state": state, "at": iso(), "note": note})
            cur["milestones"] = ms
            return cur

        return self.b.transact("vallum_status", target, fn)

    def set_timeline(self, target: str, **fields) -> dict:
        def fn(cur):
            cur.update(fields)
            return cur

        return self.b.transact("vallum_status", target, fn)

    # audit -----------------------------------------------------------------------
    def save_release(self, release_id: str, record: dict):
        self.b.put("vallum_releases", release_id, record)

    def load_release(self, release_id: str) -> dict | None:
        return self.b.read("vallum_releases", release_id)
