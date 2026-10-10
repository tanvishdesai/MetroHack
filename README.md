# Samanvay — the complaint that changes the incident (PS-07)

Different languages. One incident. The urgent report never disappears in the crowd.

A station-controller dashboard that turns Gujarati, Hindi, English and Gujlish complaints into department
queues, groups reports about the same asset, raises priority when new evidence shows danger, drafts
acknowledgements in the passenger's language, and syncs every record to a central receiver without duplicates.
All inference runs locally through Ollama; nothing leaves the laptop except to the local receiver replica.

**Data: synthetic complaints; real station names; fictional asset IDs** (e.g. `Lift L2` at Motera Stadium).

## Run (Windows, from the repo root)

```powershell
ollama pull gemma4:e2b        # default model; set $env:PS07_MODEL to use another local model
.\.venv\Scripts\python.exe -m streamlit run ps07/app.py --server.address 127.0.0.1
.\.venv\Scripts\python.exe -m uvicorn ps07.receiver:app --host 127.0.0.1 --port 8001   # separate terminal
```

Checks:

```powershell
.\.venv\Scripts\python.exe ps07/test_core.py    # no model needed: grouping, escalation, no-downgrade, idempotent sync
.\.venv\Scripts\python.exe ps07/evaluate.py     # needs the model; writes ps07/eval_results.json
```

Env overrides: `PS07_MODEL`, `PS07_OLLAMA`, `PS07_DB`, `PS07_CENTRAL_DB`, `PS07_RECEIVER`.

## How it works

```text
Streamlit (localhost)
  -> station.db: original complaint + pending state + outbox event   (one transaction, before inference)
  -> local Ollama /api/chat, JSON-schema structured output, one complaint per request
  -> validation + hazard pre-screen + priority rules + incident assignment + reply template
  -> station.db: result/correction + new outbox event
  -> sync (every 5 s while open, or "Sync now") -> receiver.py -> central.db -> ack by event_id
```

- **AI extracts, rules decide, the controller approves.** The model returns language, station, asset,
  department, urgency (1–5 rubric), event state and an exact evidence quote. Deterministic code groups
  incidents and sets priority.
- **Grouping:** same station (trusted intake metadata) + same explicit asset + open incident + last report within
  30 minutes. No asset, failed validation or failed AI -> own incident; the controller can attach it manually.
- **Priority = max of member reports.** A later low or "resolved" report cannot downgrade an open urgent incident;
  only a recorded controller correction or closure can. Escalations are shown as
  `Priority 3 -> 5: new report says 'andar ek vyakti fasaya chhe'`.
- **Validation:** schema-valid output, evidence must be a literal substring of the original, station conflicts
  with intake -> `Needs review`. Truncated or invalid output is a failure, never a partial success.
- **Hazard pre-screen:** small keyword list (trapped/smoke/fire/injury, Gujarati/Hindi/Gujlish forms). It only
  warns; it does not understand negation. If it fires but AI urgency is below 4, the report goes to `Needs review`
  (no automatic override). If the AI is unavailable, a hazard hit puts the report at P5 for manual triage.
- **Deterministic post-rules:** urgency 5 -> safety queue (the model sometimes routes entrapment to lifts);
  gu/gujlish label corrected from the actual script.
- **Replies:** reviewed templates in English, Hindi (Devanagari/Roman), Gujarati and Gujlish with the complaint ID
  inserted by code; the AI's own wording is shown as an unreviewed suggestion. Nothing is sent externally.
- **Sync:** every intake/result/correction writes an outbox snapshot (UUID `event_id`, entity, revision) in the
  same transaction. The receiver inserts `event_id` uniquely and only applies newer revisions. An event is marked
  synced only when the receiver echoes its exact ID. "Resend acked (dup test)" proves retries add nothing.

## 90-second demo

1. Before judges: warm the model, click **Load sample history (D1–D6)** (labelled "sample history"), stop the receiver, Wi-Fi off.
2. Paste the live Gujlish complaint (in the "Demo: live complaint text" box) -> Save & triage. The L2 incident jumps to P5
   with the evidence quote; open it to show four original reports in three languages and the Gujlish draft.
3. Show **Pending sync** > 0. Start the receiver; pending drains. Click **Receiver summary**, then **Resend acked** to show
   the count does not change. "This is a local replica of the central receiver; connection failure simulated."
4. Restart the Streamlit app: every original and correction is still there.
5. If asked about batch import: upload `ps07/demo_batch.csv` (5 fresh mixed-language complaints).

Script: "A controller sees six complaints in different languages. Three describe the same lift. The next message
changes everything." ... "One incident, four passengers, no report discarded. A controller reviews the decision." ...
Close: "We turn multilingual complaints into an accountable incident queue, even when the network disappears."

## Pilot and PPP proposal

Pilot at two stations for two weeks in shadow mode; controllers stay responsible for routing and response. Measure
time-to-triage, missed urgent cases, correction rate and synchronized-record completeness against the current workflow.
GMRC provides de-identified sample complaints, department rules, approved acknowledgement wording and a controller
sponsor, and owns the data and decisions. A private integrator configures the taxonomy, maintains model/software
versions, trains staff and runs the sync receiver; it is paid for setup and support after agreed acceptance criteria.
Illustrative only (not a quotation): saving 30 min/day at an assumed INR 300/hour is INR 4,500/month per station
(30 days). This is a public-private delivery proposal, not an approved PPP concession.

## Evaluation (measured, gemma4:e2b Q4_K_M, GTX 1650 laptop, warm model)

`ps07/eval_cases.json`: 11 held-out cases (Gujarati script, Hindi script, English, Gujlish; smoke, entrapment,
violence, negated smoke, resolved lift, vague text, same lift number at a different station, prompt injection).
**Labels are drafts and need a fluent reviewer.** Re-run with `evaluate.py`; raw rows in `eval_results*.json`.

| Metric | Run 1 (`eval_results_run1.json`) | Run 2, after pre-screen review rule (`eval_results.json`) |
|---|---|---|
| Valid AI / review required / failed | 9 / 2 / 0 | 7 / 4 / 0 |
| Urgent recall, AI (urgency >= 4) | 2/3 | 2/3 (missed case now flagged for review) |
| Urgent recall, keyword baseline | 3/3 | 3/3 |
| False urgent, AI / keyword | 0/8 / 1/8 | 0/8 / 1/8 |
| Department correct | 10/11 | 10/11 |
| Incorrect merges | 0 | 0 |
| Latency median / max | 6.2 s / 7.2 s | 6.5 s / 8.9 s |

AI miss: Hindi-script entrapment ("दो लोग फंसे हुए हैं") rated 3. The review rule was added after seeing that miss,
so run 2 is not a clean held-out result. Cold first call took ~46 s; warm the model before the demo.

End-to-end check (real model, temp DBs): D1–D3 grouped into one Lift L2 incident at P3; the live Gujlish entrapment
complaint attached as the 4th report and raised it to `Priority 3 -> 5`, safety queue, in 6.4 s. With the receiver
down, 21 outbox events stayed pending and survived a process restart; after the receiver started, 21/21 acked;
resending all 21 left the receiver at 21 events and 7 complaints. Rules self-check: `test_core.py`.

## Limitations

- Small synthetic smoke test, not production validation; model output is untrusted and shown with evidence.
- Gujarati/Hindi reply templates need fluent review before the demo.
- Roman-script Hindi is sometimes labelled `en` by the model, so it gets the English template.
- AI-written reply wording is shown only when its script matches the complaint; reviewed templates are the default.
- Auto-sync runs only while the dashboard is open; pending events survive and drain on reopen.
- No auth, encryption or personal-data controls (production requirements, out of scope for the prototype).

## Attribution

Station list from `data/metro/stations.csv` (OpenStreetMap / Wikipedia derived, see `data/metro/raw`).
Built with Streamlit, FastAPI, Pydantic, Requests, SQLite and Ollama.
