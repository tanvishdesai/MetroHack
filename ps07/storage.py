"""SQLite station store: complaints, incidents, transactional outbox, sync client."""
import json
import os
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

import requests

import triage

DB = os.environ.get("PS07_DB", str(Path(__file__).with_name("station.db")))
GROUP_WINDOW_S = 30 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS complaints (
  id TEXT PRIMARY KEY, short_id TEXT, text TEXT NOT NULL, channel TEXT, station TEXT,
  received_at REAL, status TEXT, is_sample INTEGER DEFAULT 0,
  model TEXT, raw TEXT, error TEXT, latency_ms INTEGER, extraction TEXT,
  language TEXT, department TEXT, urgency INTEGER, issue_family TEXT, asset_id TEXT,
  event_state TEXT, evidence TEXT, hazards TEXT DEFAULT '[]', issues TEXT DEFAULT '[]',
  draft TEXT, ai_draft TEXT, ack_status TEXT DEFAULT 'draft', corrected INTEGER DEFAULT 0,
  incident_id TEXT, revision INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS incidents (
  id TEXT PRIMARY KEY, station TEXT, asset_id TEXT, department TEXT, priority INTEGER,
  status TEXT DEFAULT 'open', created_at REAL, last_report_at REAL, reason TEXT,
  revision INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS outbox (
  event_id TEXT PRIMARY KEY, entity TEXT, entity_id TEXT, revision INTEGER,
  created_at REAL, note TEXT, payload TEXT, acked_at REAL);
"""


def _con():
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    return closing(con)


def init():
    with _con() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)


def _emit(con, entity, entity_id, note):
    """Snapshot the row's current revision into the outbox (caller holds the transaction)."""
    row = dict(con.execute(f"SELECT * FROM {entity}s WHERE id=?", (entity_id,)).fetchone())
    con.execute("INSERT INTO outbox VALUES (?,?,?,?,?,?,?,NULL)",
                (str(uuid.uuid4()), entity, entity_id, row["revision"], time.time(), note,
                 json.dumps(row, ensure_ascii=False)))


def add_complaint(text, channel, station, received_at=None, is_sample=False):
    """Persist original + outbox event before any inference. Returns complaint id."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty complaint")
    if len(text) > triage.MAX_CHARS:
        raise ValueError(f"complaint is {len(text)} chars; limit is {triage.MAX_CHARS}")
    cid = str(uuid.uuid4())
    with _con() as con, con:
        con.execute("INSERT INTO complaints (id, short_id, text, channel, station, received_at, status,"
                    " is_sample, hazards) VALUES (?,?,?,?,?,?,'pending',?,?)",
                    (cid, "C-" + cid[:6].upper(), text, channel, station, received_at or time.time(),
                     int(is_sample), json.dumps(triage.hazard_screen(text), ensure_ascii=False)))
        _emit(con, "complaint", cid, "received")
    return cid


def pending_ids(include_failed=False):
    states = ("pending", "failed") if include_failed else ("pending", "pending")
    with _con() as con:
        return [r[0] for r in con.execute(
            "SELECT id FROM complaints WHERE status IN (?,?) AND corrected=0 ORDER BY received_at", states)]


def get_complaint(cid):
    with _con() as con:
        return dict(con.execute("SELECT * FROM complaints WHERE id=?", (cid,)).fetchone())


def _effective(c):
    """Urgency used for incident priority. Failed AI + hazard keyword -> 5 so it cannot be buried."""
    if c["urgency"] is not None:
        return c["urgency"]
    return 5 if json.loads(c["hazards"]) else 0


def _recompute(con, iid, cause):
    """Incident priority = max member priority. Returns note if anything changed."""
    inc = con.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone()
    members = con.execute("SELECT * FROM complaints WHERE incident_id=? ORDER BY received_at",
                          (iid,)).fetchall()
    if not members:
        con.execute("UPDATE incidents SET status='closed', priority=0, reason='No reports left',"
                    " revision=revision+1 WHERE id=?", (iid,))
        _emit(con, "incident", iid, "emptied")
        return
    top = max(members, key=lambda c: (_effective(c), c["received_at"]))
    pri = _effective(top)
    dept = top["department"] if top["status"] == "done" or top["corrected"] else "review"
    last = max(c["received_at"] for c in members)
    old = inc["priority"]
    if old is None:
        reason = f"Priority {pri}: '{top['evidence'] or top['text'][:60]}'"
    elif pri > old:
        reason = f"Priority {old} -> {pri}: {cause} says '{top['evidence'] or top['text'][:60]}'"
    elif pri < old:
        reason = f"Priority {old} -> {pri}: controller correction"
    else:
        reason = inc["reason"]
    if (pri, dept, last, reason) == (old, inc["department"], inc["last_report_at"], inc["reason"]):
        return
    con.execute("UPDATE incidents SET priority=?, department=?, last_report_at=?, reason=?,"
                " revision=revision+1 WHERE id=?", (pri, dept, last, reason, iid))
    _emit(con, "incident", iid, reason)


def _new_incident(con, c, asset):
    iid = "INC-" + uuid.uuid4().hex[:6].upper()
    con.execute("INSERT INTO incidents (id, station, asset_id, created_at, last_report_at, revision)"
                " VALUES (?,?,?,?,?,0)", (iid, c["station"], asset, c["received_at"], c["received_at"]))
    return iid


FAMILY = {"lift": "lift", "other_safety": "lift", "other": "lift"}  # fault + entrapment share a lift incident


def _match(con, station, asset, at, family):
    """Open incident at same station, same explicit asset, compatible issue family, report within 30 min."""
    if not asset:
        return None
    for (iid,) in con.execute("SELECT id FROM incidents WHERE status='open' AND station=? AND asset_id=?"
                              " AND ABS(last_report_at - ?) <= ? ORDER BY last_report_at DESC",
                              (station, asset, at, GROUP_WINDOW_S)).fetchall():
        fams = {FAMILY.get(f, f) for (f,) in con.execute(
            "SELECT issue_family FROM complaints WHERE incident_id=? AND issue_family IS NOT NULL", (iid,))}
        if fams <= {FAMILY.get(family, family)}:
            return iid
    return None


def apply_result(cid, res, raw, ms, err, known_stations):
    """Store AI result (or failure), assign incident, update priority, emit outbox events."""
    with _con() as con, con:
        con.execute("BEGIN IMMEDIATE")  # check-then-write must be atomic across tabs
        c = con.execute("SELECT * FROM complaints WHERE id=?", (cid,)).fetchone()
        if c["status"] not in ("pending", "failed") or c["corrected"]:
            return  # never reclassify completed or controller-corrected rows
        stale = None
        if c["status"] == "failed" and c["incident_id"] and not con.execute(
                "SELECT 1 FROM outbox WHERE entity_id=? AND note LIKE 'attached by controller%'", (cid,)).fetchone():
            stale = c["incident_id"]  # retry after failure: regroup from scratch
            con.execute("UPDATE complaints SET incident_id=NULL WHERE id=?", (cid,))
        if res is None:
            con.execute("UPDATE complaints SET status='failed', error=?, raw=?, latency_ms=?, model=?,"
                        " issues=?, revision=revision+1 WHERE id=?",
                        (err, raw, ms, triage.MODEL, json.dumps([err]), cid))
            asset = None
        else:
            issues = triage.validate(res, c["text"], c["station"], known_stations)
            hz = json.loads(c["hazards"])
            if hz and res.urgency < 4:  # rules warn, the controller decides; no automatic override
                issues.append(f"keyword pre-screen found {', '.join(hz)} but AI urgency is {res.urgency}")
            asset = triage.canonical_asset(res.asset_id, c["text"])
            if asset and asset.split()[0] in ("Lift", "Escalator") and res.issue_family in ("staff", "other", "other_safety"):
                res.issue_family = "lift"  # model labels some lift emergencies "staff"; keep cleanliness/fare separate
            draft = triage.template_reply(res.language, c["text"], c["short_id"], res.urgency)
            ai_draft = res.draft_reply
            if triage.unsafe_reply(ai_draft) or triage.template_key(res.language, ai_draft) !=                     triage.template_key(res.language, c["text"]):
                ai_draft = None  # wrong script or unsupported claim: keep only the reviewed template
            res.language = triage.script_language(res.language, c["text"])
            if res.urgency == 5 and res.department != "review":
                res.department = "safety"  # rule: explicit current threat -> safety queue, even for a lift
            con.execute(
                "UPDATE complaints SET status=?, error=NULL, raw=?, latency_ms=?, model=?, extraction=?,"
                " language=?, department=?, urgency=?, issue_family=?, asset_id=?, event_state=?,"
                " evidence=?, issues=?, draft=?, ai_draft=?, revision=revision+1 WHERE id=?",
                ("review" if issues else "done", raw, ms, triage.MODEL, res.model_dump_json(),
                 res.language, res.department, res.urgency, res.issue_family, asset, res.event_state,
                 res.evidence_quote, json.dumps(issues, ensure_ascii=False), draft, ai_draft, cid))
        c = con.execute("SELECT * FROM complaints WHERE id=?", (cid,)).fetchone()
        # review/failed rows never auto-attach; they get their own incident
        iid = c["incident_id"] or (_match(con, c["station"], asset, c["received_at"], c["issue_family"])
                                   if c["status"] == "done" else None) \
            or _new_incident(con, c, asset if c["status"] == "done" else None)
        con.execute("UPDATE complaints SET incident_id=? WHERE id=?", (iid, cid))
        _emit(con, "complaint", cid, f"triaged: {c['status']}")
        _recompute(con, iid, "new report")
        if stale and stale != iid:
            _recompute(con, stale, "report regrouped after retry")


def correct(cid, department, urgency, draft, ack_status):
    """Controller correction. Original text is never touched."""
    with _con() as con, con:
        con.execute("BEGIN IMMEDIATE")
        con.execute("UPDATE complaints SET department=?, urgency=?, draft=?, ack_status=?, corrected=1,"
                    " revision=revision+1 WHERE id=?", (department, urgency, draft, ack_status, cid))
        _emit(con, "complaint", cid, f"controller correction: {department}, urgency {urgency}, {ack_status}")
        _recompute(con, get_incident_id(con, cid), "controller correction")


def get_incident_id(con, cid):
    return con.execute("SELECT incident_id FROM complaints WHERE id=?", (cid,)).fetchone()[0]


def attach(cid, target_iid):
    """Operator attaches a report to another open incident."""
    with _con() as con, con:
        con.execute("BEGIN IMMEDIATE")
        old = get_incident_id(con, cid)
        if old == target_iid:
            return
        con.execute("UPDATE complaints SET incident_id=?, revision=revision+1 WHERE id=?", (target_iid, cid))
        _emit(con, "complaint", cid, f"attached by controller to {target_iid}")
        _recompute(con, target_iid, "attached report")
        if old:
            _recompute(con, old, "report moved")


def close_incident(iid):
    with _con() as con, con:
        con.execute("UPDATE incidents SET status='closed', reason='Closed by controller',"
                    " revision=revision+1 WHERE id=?", (iid,))
        _emit(con, "incident", iid, "closed by controller")


# ---- reads -----------------------------------------------------------------

def incidents(include_closed=False):
    with _con() as con:
        return [dict(r) for r in con.execute(
            "SELECT i.*, COUNT(c.id) AS reports FROM incidents i LEFT JOIN complaints c"
            " ON c.incident_id=i.id WHERE i.status='open' OR ? GROUP BY i.id"
            " ORDER BY i.priority DESC, i.last_report_at DESC", (include_closed,))]


def complaints_for(iid):
    with _con() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM complaints WHERE incident_id=? ORDER BY received_at", (iid,))]


def unassigned():
    with _con() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM complaints WHERE incident_id IS NULL ORDER BY received_at")]


def history(iid):
    with _con() as con:
        return [dict(r) for r in con.execute(
            "SELECT o.created_at, o.entity, o.entity_id, o.revision, o.note, o.acked_at FROM outbox o"
            " WHERE (o.entity='incident' AND o.entity_id=?) OR (o.entity='complaint' AND o.entity_id IN"
            " (SELECT id FROM complaints WHERE incident_id=?)) ORDER BY o.created_at DESC", (iid, iid))]


def counts():
    with _con() as con:
        q = lambda sql: con.execute(sql).fetchone()[0]
        return {
            "complaints": q("SELECT COUNT(*) FROM complaints"),
            "pending": q("SELECT COUNT(*) FROM complaints WHERE status='pending'"),
            "failed": q("SELECT COUNT(*) FROM complaints WHERE status='failed' AND corrected=0"),
            "review": q("SELECT COUNT(*) FROM complaints WHERE status IN ('review','failed') AND corrected=0"),
            "unsynced": q("SELECT COUNT(*) FROM outbox WHERE acked_at IS NULL"),
            "last_ack": q("SELECT MAX(acked_at) FROM outbox"),
        }


# ---- sync ------------------------------------------------------------------

def _post(url, rows):
    events = [{"event_id": r["event_id"], "entity": r["entity"], "entity_id": r["entity_id"],
               "revision": r["revision"], "created_at": r["created_at"],
               "payload": json.loads(r["payload"])} for r in rows]
    r = requests.post(f"{url}/events", json={"events": events}, timeout=(1, 3))
    r.raise_for_status()
    return set(r.json().get("acked", []))


def sync_once(url, batch=25):
    """Send unacked events. Mark acked only for IDs the receiver echoes back. -> (sent, acked, error)"""
    with _con() as con:
        rows = con.execute("SELECT * FROM outbox WHERE acked_at IS NULL ORDER BY created_at LIMIT ?",
                           (batch,)).fetchall()
    if not rows:
        return 0, 0, None
    try:
        acked = _post(url, rows) & {r["event_id"] for r in rows}
    except (requests.RequestException, ValueError) as e:
        return len(rows), 0, type(e).__name__
    with _con() as con, con:
        con.executemany("UPDATE outbox SET acked_at=? WHERE event_id=? AND acked_at IS NULL",
                        [(time.time(), e) for e in acked])
    return len(rows), len(acked), None


def resend_acked(url, n=5):
    """Demo: resend already-acknowledged events to show the receiver ignores duplicates."""
    with _con() as con:
        rows = con.execute("SELECT * FROM outbox WHERE acked_at IS NOT NULL ORDER BY created_at DESC"
                           " LIMIT ?", (n,)).fetchall()
    return len(_post(url, rows)) if rows else 0


init()
