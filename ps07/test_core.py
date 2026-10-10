"""No-model self-check of grouping, escalation, persistence and idempotent sync. Run: python ps07/test_core.py"""
import os
import sys
import tempfile
import time

tmp = tempfile.mkdtemp()
os.environ["PS07_DB"] = os.path.join(tmp, "station.db")
os.environ["PS07_CENTRAL_DB"] = os.path.join(tmp, "central.db")
sys.path.insert(0, os.path.dirname(__file__))

import receiver  # noqa: E402
import storage  # noqa: E402
import triage  # noqa: E402

ST = ["Motera Stadium", "Sabarmati", "Thaltej"]


def R(**kw):
    base = dict(language="en", station_name=None, asset_id=None, department="lifts", issue_family="lift",
                urgency=3, event_state="active", evidence_quote="", needs_review=False, draft_reply="ok")
    return triage.Result(**{**base, **kw})


def add(text, res, station="Motera Stadium", ago_min=0):
    cid = storage.add_complaint(text, "counter", station, time.time() - 60 * ago_min)
    storage.apply_result(cid, res, "{}", 100, None if res else "AI unavailable: ConnectionError", ST)
    return storage.get_complaint(cid)


# grouping by explicit station + asset
d1 = add("Motera Stadium Lift L2 is not working.", R(asset_id="Lift L2", evidence_quote="not working"), ago_min=10)
d2 = add("મોટેરા સ્ટેડિયમ પર Lift L2 બંધ છે.", R(language="gu", asset_id="lift l2", evidence_quote="બંધ છે"), ago_min=8)
d3 = add("Motera Stadium par Lift L2 bandh chhe.", R(language="gujlish", asset_id=None, evidence_quote="bandh chhe"), ago_min=7)
d4 = add("Sabarmati station par ticket ke paise do baar kat gaye.",
         R(language="hi", department="fare", issue_family="fare", urgency=2, evidence_quote="do baar kat gaye"),
         station="Sabarmati", ago_min=6)
assert d1["incident_id"] == d2["incident_id"] == d3["incident_id"] != d4["incident_id"]
assert d3["asset_id"] == "Lift L2"
assert d4["draft"].startswith("Dhanyavaad") and d2["draft"].startswith("આભાર")

# escalation from new evidence
live = "Motera Stadium ni Lift L2 ma andar ek vyakti fasaya chhe. Darvajo khulto nathi."
d7 = add(live, R(language="gujlish", department="safety", urgency=5, asset_id="Lift L2",
                 evidence_quote="andar ek vyakti fasaya chhe"))
inc = next(i for i in storage.incidents() if i["id"] == d1["incident_id"])
assert d7["incident_id"] == inc["id"] and inc["priority"] == 5 and inc["department"] == "safety"
assert inc["reports"] == 4 and "3 -> 5" in inc["reason"] and "fasaya" in inc["reason"], inc
assert storage.incidents()[0]["id"] == inc["id"]
assert "fasaya" in " ".join(triage.hazard_screen(live))
assert d7["draft"].endswith("Aa tatkal samiksha mate chhe.")

# a later low/resolved report cannot silently downgrade
add("Motera Lift L2 is working now", R(urgency=1, event_state="resolved_claim", asset_id="Lift L2",
                                        evidence_quote="working now"))
assert next(i for i in storage.incidents() if i["id"] == inc["id"])["priority"] == 5

# same asset at another station stays separate; bad evidence -> review, no auto-attach
other = add("Sabarmati Lift L2 bandh", R(asset_id="Lift L2", evidence_quote="bandh"), station="Sabarmati")
bad = add("Motera Lift L2 not working", R(asset_id="Lift L2", evidence_quote="invented quote"))
assert other["incident_id"] != inc["id"] and bad["incident_id"] != inc["id"] and bad["status"] == "review"

# AI failure + hazard keyword -> manual triage at top priority, original kept
fail = add("Thaltej platform par dhumado chhe", None, station="Thaltej")
finc = next(i for i in storage.incidents() if i["id"] == fail["incident_id"])
assert fail["status"] == "failed" and finc["priority"] == 5 and finc["department"] == "review"

# controller correction may lower priority; it is recorded
storage.correct(fail["id"], "cleanliness", 2, "ok", "approved")
finc = next(i for i in storage.incidents() if i["id"] == fail["incident_id"])
assert finc["priority"] == 2 and "controller correction" in finc["reason"]
assert storage.get_complaint(fail["id"])["text"] == "Thaltej platform par dhumado chhe"

# sync: unreachable receiver leaves everything pending
pending = storage.counts()["unsynced"]
assert storage.sync_once("http://127.0.0.1:9")[2] is not None and storage.counts()["unsynced"] == pending

# receiver: idempotent on event_id, older revision cannot overwrite newer
with storage._con() as con:
    rows = [dict(r) for r in con.execute("SELECT * FROM outbox")]
evs = [receiver.Event(event_id=r["event_id"], entity=r["entity"], entity_id=r["entity_id"],
                      revision=r["revision"], created_at=r["created_at"], payload={"rev": r["revision"]})
       for r in rows]
receiver.ingest(evs)
first = receiver.summary()
receiver.ingest(list(reversed(evs)))  # duplicates, in the worst order
assert receiver.summary()["events"] == first["events"] == len(evs)
import sqlite3  # noqa: E402
with sqlite3.connect(os.environ["PS07_CENTRAL_DB"]) as con:
    for ent, eid, rev in con.execute("SELECT entity, entity_id, revision FROM snapshots"):
        assert rev == max(e.revision for e in evs if e.entity_id == eid)

assert triage.canonical_asset(None, "lift-l 2 broken") == "Lift L2"
assert triage.hazard_screen("aage jao") == [] and triage.hazard_screen("આગળ જાઓ") == []
print("all checks passed")

# reviewer fixes: retry after failure regroups; family guard; tighter regexes
lift = next(i for i in storage.incidents() if i["id"] == inc["id"])
c = storage.add_complaint("Motera Lift L2 ma koi fasayu chhe", "counter", "Motera Stadium")
storage.apply_result(c, None, "", 5, "AI unavailable: ConnectionError", ST)
failed_inc = storage.get_complaint(c)["incident_id"]
assert failed_inc != lift["id"]
storage.apply_result(c, R(department="safety", urgency=5, asset_id="Lift L2", evidence_quote="fasayu chhe"),
                     "{}", 100, None, ST)
assert storage.get_complaint(c)["incident_id"] == lift["id"]
assert next(i for i in storage.incidents(True) if i["id"] == failed_inc)["status"] == "closed"
dirty = add("Motera Lift L2 andar ganda chhe", R(department="cleanliness", issue_family="cleanliness",
                                                 urgency=2, asset_id="Lift L2", evidence_quote="ganda chhe"))
assert dirty["incident_id"] != lift["id"]
assert triage.canonical_asset(None, "lift ma 20 minute thi band") is None
assert triage.hazard_screen("Gate pase bhid chhe, Phase 2") == []
assert triage.hazard_screen("lift mein log phans gaye") != []
print("reviewer-fix checks passed")
assert triage.script_language("gu", live) == "gujlish" and triage.script_language("gu", "બંધ છે") == "gu"
x = add("Motera Lift L2 ma koi fasaya chhe", R(language="gu", department="lifts", urgency=5, asset_id="Lift L2",
                                             evidence_quote="fasaya chhe", draft_reply="તમારી ફરિયાદ"))
assert x["department"] == "safety" and x["language"] == "gujlish" and x["ai_draft"] is None
# model labels a lift emergency "staff": it must still escalate the open Lift L2 incident
stuck = add("Someone is stuck inside lift 2 at Motera", R(department="safety", issue_family="staff", urgency=5,
                                                          asset_id="Lift L2", evidence_quote="stuck inside lift 2"))
assert stuck["incident_id"] == lift["id"], stuck
assert next(i for i in storage.incidents() if i["id"] == lift["id"])["priority"] == 5
print("rule checks passed")
