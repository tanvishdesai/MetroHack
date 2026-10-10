# Samanvay — the complaint that changes the incident (PS-07)

Different languages. One incident. The urgent report never disappears in the crowd.

A station-controller dashboard that turns Gujarati, Hindi, English and Gujlish complaints into department
queues, groups reports about the same asset, raises priority when new evidence shows danger, drafts
acknowledgements in the passenger's language, and syncs every record to a central receiver without duplicates.
All inference runs locally through Ollama; nothing leaves the laptop except to the local receiver replica.

**Data: synthetic complaints; real station names; fictional asset IDs** (e.g. `Lift L2` at Motera Stadium).

## Run (Windows, from the repo root)

```powershell
ollama pull qwen3:1.7b        # or set $env:PS07_MODEL to another local model, e.g. gemma4:e2b
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
  warns; it does not understand negation. If the AI is unavailable, a hazard hit puts the report at P5 for manual triage.
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

## Evaluation

`ps07/eval_cases.json` holds 11 held-out cases (Gujarati, Hindi, English, Gujlish; smoke, entrapment, violence,
negated smoke, resolved lift, vague text, same lift number at a different station, prompt injection). **Labels are drafts
and need a fluent reviewer.** Results are written to `ps07/eval_results.json` by `evaluate.py`, including failures,
review-required counts, keyword-baseline comparison and median/max latency. No numbers are claimed here until run.

## Limitations

- Small synthetic smoke test, not production validation; model output is untrusted and shown with evidence.
- Gujarati/Hindi reply templates need fluent review before the demo.
- Auto-sync runs only while the dashboard is open; pending events survive and drain on reopen.
- No auth, encryption or personal-data controls (production requirements, out of scope for the prototype).

## Attribution

Station list from `data/metro/stations.csv` (OpenStreetMap / Wikipedia derived, see `data/metro/raw`).
Built with Streamlit, FastAPI, Pydantic, Requests, SQLite and Ollama.
