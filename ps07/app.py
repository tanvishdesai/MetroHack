"""Samanvay: station-controller dashboard. Run: streamlit run ps07/app.py --server.address 127.0.0.1"""
import csv
import hashlib
import io
import json
import os
import time
from datetime import datetime
from pathlib import Path

import requests
import streamlit as st

import storage
import triage

HERE = Path(__file__).parent
RECEIVER = os.environ.get("PS07_RECEIVER", "http://127.0.0.1:8001")
with open(HERE.parent / "data" / "metro" / "stations.csv", encoding="utf-8") as f:
    STATIONS = sorted({r["name"] for r in csv.DictReader(f)})
FIX = json.loads((HERE / "fixtures.json").read_text(encoding="utf-8"))
CHANNELS = ["counter", "helpline", "app", "whatsapp (imported)", "email (imported)"]
QUEUES = ["safety", "lifts", "fare", "cleanliness", "staff", "review"]
DOT = {5: "🔴", 4: "🟠", 3: "🟡", 2: "🔵", 1: "⚪", 0: "⚫"}

st.set_page_config(page_title="Samanvay PS-07", layout="wide")
ss = st.session_state


def ago(ts):
    m = int((time.time() - ts) // 60)
    return f"{m} min ago" if m < 120 else datetime.fromtimestamp(ts).strftime("%d %b %H:%M")


def run_triage(ids):
    """One complaint per request, each result persisted before the next starts."""
    bar = st.progress(0.0, text=f"Local triage 0/{len(ids)}")
    for i, cid in enumerate(ids, 1):
        c = storage.get_complaint(cid)
        res, raw, ms, err = triage.call_model(c["text"], c["station"], c["channel"])
        storage.apply_result(cid, res, raw, ms, err, STATIONS)
        bar.progress(i / len(ids), text=f"Local triage {i}/{len(ids)} ({ms / 1000:.1f}s)")
        ss.selected = storage.get_complaint(cid)["incident_id"]
    st.rerun()


# ---- status strip (also the auto-sync loop while this session is open) --------

@st.fragment(run_every="5s")
def status_strip():
    if ss.get("autosync", True):
        sent, acked, err = storage.sync_once(RECEIVER)
        ss.sync_msg = f"receiver unreachable ({err})" if err else (f"sent {acked}/{sent}" if sent else "idle")
    ok, model_msg = triage.model_status()
    n = storage.counts()
    with storage._con() as con:
        lat = con.execute("SELECT latency_ms FROM complaints WHERE latency_ms IS NOT NULL"
                          " ORDER BY received_at DESC LIMIT 1").fetchone()
    cols = st.columns(6)
    cols[0].metric("Local model", model_msg if ok else "unavailable", help=model_msg)
    cols[1].metric("Last inference", f"{lat[0] / 1000:.1f} s" if lat else "—")
    cols[2].metric("Pending triage", n["pending"])
    cols[3].metric("Review required", n["review"])
    cols[4].metric("Pending sync", n["unsynced"], help=ss.get("sync_msg", ""))
    cols[5].metric("Last receiver ack", ago(n["last_ack"]) if n["last_ack"] else "never")
    if not ok:
        st.warning(f"AI unavailable: manual triage. ({model_msg}) Complaints are still saved.")


st.markdown("### Samanvay · different languages, one incident")
st.caption(FIX["label"] + " Imported channel labels are not live integrations. Replies are drafts; nothing is sent.")
status_strip()

left, mid, right = st.columns([1, 1.3, 1.5], gap="medium")

# ---- intake -----------------------------------------------------------------
with left:
    st.subheader("Intake")
    with st.form("intake", clear_on_submit=True):
        text = st.text_area("Complaint (any language)", height=110, max_chars=triage.MAX_CHARS)
        station = st.selectbox("Station (trusted metadata)", STATIONS, index=STATIONS.index("Motera Stadium"))
        channel = st.selectbox("Channel label", CHANNELS)
        if st.form_submit_button("Save & triage", type="primary"):
            try:
                cid = storage.add_complaint(text, channel, station)
                st.toast("Original saved before inference")
                run_triage([cid])
            except ValueError as e:
                st.error(str(e))

    for b in ss.pop("csv_errors", []):
        st.error(b)
    up = st.file_uploader("CSV import (UTF-8: text, station, channel)", type="csv")
    if up and st.button("Import CSV", key="import_csv"):
        digest = hashlib.sha256(up.getvalue()).hexdigest()
        if digest in ss.setdefault("imported", set()):
            st.warning("This file was already imported in this session.")
        else:
            try:
                rows = list(csv.DictReader(io.StringIO(up.getvalue().decode("utf-8-sig"))))
                ids, bad = [], []
                for i, r in enumerate(rows, 2):
                    st_name = (r.get("station") or "").strip()
                    if st_name not in STATIONS:
                        bad.append(f"line {i}: unknown station '{st_name}'")
                        continue
                    try:
                        ids.append(storage.add_complaint(r.get("text"), r.get("channel") or "imported", st_name))
                    except ValueError as e:
                        bad.append(f"line {i}: {e}")
                ss.imported.add(digest)
                ss.csv_errors = bad  # shown after run_triage's rerun
                for b in bad:
                    st.error(b)
                if ids:
                    run_triage(ids)
            except UnicodeDecodeError:
                st.error("File is not UTF-8.")

    with storage._con() as con:
        n_sample = con.execute("SELECT COUNT(*) FROM complaints WHERE is_sample=1").fetchone()[0]
    if not n_sample and st.button("Load sample history (D1–D6)", key="load_sample"):
        now = time.time()
        ids = [storage.add_complaint(d["text"], d["channel"], d["station"], now - 60 * d["minutes_ago"], True)
               for d in FIX["sample_history"]]
        run_triage(ids)
    if n_sample:
        st.caption("Sample history groups with new reports for 30 min after its latest report; "
                   "submit the live complaint within ~20 min of loading, or restart with a fresh PS07_DB.")
    with st.expander("Demo: live complaint text"):
        st.code(FIX["live"]["text"], language=None)

    pend, failed = storage.pending_ids(), storage.counts()["failed"]
    if pend and st.button(f"Run triage on {len(pend)} pending", key="run_pending"):
        run_triage(pend)
    if failed and st.button(f"Retry AI on {failed} failed", key="retry_failed"):
        run_triage([c for c in storage.pending_ids(include_failed=True) if c not in pend])

    st.divider()
    st.subheader("Sync")
    st.caption(f"Local replica of the central receiver at {RECEIVER}; stop it to simulate a connection failure.")
    st.toggle("Auto-sync every 5 s (while this page is open)", value=True, key="autosync")
    c1, c2 = st.columns(2)
    if c1.button("Sync now", key="sync_now"):
        sent, acked, err = storage.sync_once(RECEIVER, batch=200)
        (st.error if err else st.success)(f"Receiver unreachable: {err}" if err else f"Acknowledged {acked}/{sent}")
    if c2.button("Resend acked (dup test)", key="resend"):
        try:
            before = requests.get(f"{RECEIVER}/summary", timeout=2).json()
            n = storage.resend_acked(RECEIVER)
            after = requests.get(f"{RECEIVER}/summary", timeout=2).json()
            st.info(f"Resent {n} acked events. Receiver events {before['events']} → {after['events']}, "
                    f"complaints {before['complaints']} → {after['complaints']}")
        except (requests.RequestException, ValueError) as e:
            st.error(f"Receiver unreachable: {type(e).__name__}")
    if st.button("Receiver summary", key="summary"):
        try:
            st.json(requests.get(f"{RECEIVER}/summary", timeout=2).json())
        except (requests.RequestException, ValueError) as e:
            st.error(f"Receiver unreachable: {type(e).__name__}")

# ---- incident queue ---------------------------------------------------------
with mid:
    st.subheader("Incident queue")
    q = st.radio("Department", ["all"] + QUEUES, horizontal=True, label_visibility="collapsed")
    show_closed = st.checkbox("Show closed", key="show_closed")
    incs = [i for i in storage.incidents(show_closed) if q == "all" or i["department"] == q]
    if not incs:
        st.info("No incidents in this queue. Submit a complaint or load sample history.")
    for inc in incs:
        with st.container(border=True):
            p = inc["priority"] or 0
            where = " · ".join(x for x in (inc["station"], inc["asset_id"]) if x)
            st.markdown(f"{DOT.get(p, '⚫')} **P{p} · {(inc['department'] or 'review').upper()}** · {where}  \n"
                        f"{inc['reports']} report(s) · last {ago(inc['last_report_at'])}"
                        + (" · **closed**" if inc["status"] == "closed" else ""))
            st.caption(f"Why this priority? {inc['reason']}")
            if st.button("Open", key=f"open_{inc['id']}"):
                ss.selected = inc["id"]

# ---- selected incident -------------------------------------------------------
with right:
    st.subheader("Selected incident")
    sel = next((i for i in storage.incidents(True) if i["id"] == ss.get("selected")), None)
    if not sel:
        st.info("Open an incident from the queue.")
    else:
        p = sel["priority"] or 0
        (st.error if p >= 4 else st.info)(f"**{sel['id']} · P{p} · {(sel['department'] or 'review')}** — {sel['reason']}")
        reports = storage.complaints_for(sel["id"])
        st.caption(f"{len(reports)} original report(s) retained. AI extracts; rules group and rank; "
                   "the controller approves.")
        if sel["status"] == "open" and st.button("Close incident", key=f"close_{sel['id']}"):
            storage.close_incident(sel["id"])
            st.rerun()
        others = [i["id"] for i in storage.incidents() if i["id"] != sel["id"]]
        for c in reports:
            hz, issues = json.loads(c["hazards"]), json.loads(c["issues"])
            label = (f"{c['short_id']} · {c['language'] or '?'} · urgency {c['urgency'] or '?'} · "
                     f"{c['status']}{' · corrected' if c['corrected'] else ''}")
            with st.expander(label, expanded=True):
                st.markdown(f"> {c['text']}")
                st.caption(f"{c['channel']} · {c['station']} · received {ago(c['received_at'])}"
                           + (" · **sample history**" if c["is_sample"] else "")
                           + (f" · {c['model']} {c['latency_ms'] / 1000:.1f}s" if c["latency_ms"] else ""))
                if c["evidence"]:
                    st.markdown(f"Evidence: **“{c['evidence']}”**")
                if hz:
                    st.warning(f"Keyword pre-screen (not a detector): {', '.join(hz)} — verify")
                if c["status"] == "failed":
                    st.error(f"AI unavailable: manual triage. {c['error']}")
                elif issues:
                    st.warning("Needs review: " + "; ".join(issues))
                with st.form(f"fix_{c['id']}"):
                    a, b, s = st.columns(3)
                    dep_opts = QUEUES
                    dep = a.selectbox("Department", dep_opts,
                                      index=dep_opts.index(c["department"]) if c["department"] in dep_opts else 5)
                    urg = b.selectbox("Urgency", [1, 2, 3, 4, 5], index=(c["urgency"] or 3) - 1)
                    ack = s.selectbox("Acknowledgement", ["draft", "approved"],
                                      index=["draft", "approved"].index(c["ack_status"]))
                    draft = st.text_area("Draft acknowledgement (not sent)", c["draft"] or
                                         triage.template_reply("unknown", c["text"], c["short_id"], c["urgency"]))
                    if c["ai_draft"]:
                        st.caption(f"AI wording (unreviewed): {c['ai_draft']}")
                    if st.form_submit_button("Save correction"):
                        storage.correct(c["id"], dep, urg, draft, ack)
                        st.rerun()
                if others:
                    t = st.selectbox("Attach to incident", others, key=f"att_sel_{c['id']}")
                    if st.button("Attach", key=f"att_{c['id']}"):
                        storage.attach(c["id"], t)
                        st.rerun()
        with st.expander("Event history (outbox)"):
            st.dataframe([{"time": datetime.fromtimestamp(h["created_at"]).strftime("%H:%M:%S"),
                           "entity": f"{h['entity']} {h['entity_id'][:10]} r{h['revision']}",
                           "note": h["note"], "synced": "yes" if h["acked_at"] else "pending"}
                          for h in storage.history(sel["id"])], hide_index=True, width="stretch")
