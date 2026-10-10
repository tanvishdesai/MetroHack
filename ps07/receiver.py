"""Independent central receiver (local replica). Idempotent on event_id, newest revision wins."""
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

DB = os.environ.get("PS07_CENTRAL_DB", str(Path(__file__).with_name("central.db")))

with closing(sqlite3.connect(DB)) as _c:
    _c.executescript("""
    CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, entity TEXT, entity_id TEXT,
      revision INTEGER, received_at REAL);
    CREATE TABLE IF NOT EXISTS snapshots (entity TEXT, entity_id TEXT, revision INTEGER, payload TEXT,
      PRIMARY KEY (entity, entity_id));""")


class Event(BaseModel):
    event_id: str
    entity: str
    entity_id: str
    revision: int
    created_at: float
    payload: dict


class Batch(BaseModel):
    events: list[Event]


def ingest(events):
    """Commit all events in one transaction, then return their IDs as the acknowledgement."""
    with closing(sqlite3.connect(DB, timeout=10)) as con, con:
        for e in events:
            con.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?)",
                        (e.event_id, e.entity, e.entity_id, e.revision, time.time()))
            con.execute("INSERT INTO snapshots VALUES (?,?,?,?) ON CONFLICT(entity, entity_id) DO UPDATE"
                        " SET revision=excluded.revision, payload=excluded.payload"
                        " WHERE excluded.revision > snapshots.revision",
                        (e.entity, e.entity_id, e.revision, json.dumps(e.payload, ensure_ascii=False)))
    return [e.event_id for e in events]


def summary():
    with closing(sqlite3.connect(DB)) as con:
        complaints = [json.loads(p) for (p,) in con.execute(
            "SELECT payload FROM snapshots WHERE entity='complaint'")]
        inc = {}
        for c in complaints:
            if c.get("incident_id"):
                inc[c["incident_id"]] = inc.get(c["incident_id"], 0) + 1
        return {
            "events": con.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "complaints": len(complaints),
            "incidents": con.execute("SELECT COUNT(*) FROM snapshots WHERE entity='incident'").fetchone()[0],
            "reports_per_incident": inc,
        }


app = FastAPI(title="PS-07 central receiver (local replica)")


@app.post("/events")
def post_events(batch: Batch):
    return {"acked": ingest(batch.events)}


@app.get("/summary")
def get_summary():
    return summary()
