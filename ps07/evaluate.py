"""Run held-out cases through the real pipeline on a throwaway DB. Run: python ps07/evaluate.py"""
import csv
import json
import os
import statistics
import sys
import tempfile
from pathlib import Path

os.environ["PS07_DB"] = os.path.join(tempfile.mkdtemp(), "eval.db")
sys.path.insert(0, os.path.dirname(__file__))
import storage  # noqa: E402
import triage  # noqa: E402

HERE = Path(__file__).parent
cases = json.loads((HERE / "eval_cases.json").read_text(encoding="utf-8"))["cases"]
with open(HERE.parent / "data" / "metro" / "stations.csv", encoding="utf-8") as f:
    stations = [r["name"] for r in csv.DictReader(f)]
rows = []
for case in cases:
    cid = storage.add_complaint(case["text"], "eval", case["station"])
    res, raw, ms, err = triage.call_model(case["text"], case["station"], "eval")
    storage.apply_result(cid, res, raw, ms, err, stations)
    c = storage.get_complaint(cid)
    rows.append({"id": case["id"], "status": c["status"], "latency_ms": ms, "error": c["error"],
                 "expected_dept": case["department"], "got_dept": c["department"],
                 "expected_urgent": case["urgent"], "got_urgency": c["urgency"],
                 "keyword_urgent": bool(triage.hazard_screen(case["text"])),
                 "issues": json.loads(c["issues"]), "incident": c["incident_id"], "group": case["group"]})
    print(f"{case['id']}: {c['status']} dept={c['department']} urgency={c['urgency']} {ms} ms {c['error'] or ''}")

urgent = [r for r in rows if r["expected_urgent"]]
by_inc = {}
for r in rows:
    by_inc.setdefault(r["incident"], set()).add(r["group"])
lat = [r["latency_ms"] for r in rows]
summary = {
    "model": triage.MODEL,
    "n": len(rows),
    "valid_ai": sum(r["status"] == "done" for r in rows),
    "review_required": sum(r["status"] == "review" for r in rows),
    "failed_inference": sum(r["status"] == "failed" for r in rows),
    "urgent_recall_ai": f"{sum((r['got_urgency'] or 0) >= 4 for r in urgent)}/{len(urgent)}",
    "urgent_recall_keyword_baseline": f"{sum(r['keyword_urgent'] for r in urgent)}/{len(urgent)}",
    "false_urgent_ai": f"{sum((r['got_urgency'] or 0) >= 4 for r in rows if not r['expected_urgent'])}"
                       f"/{len(rows) - len(urgent)}",
    "false_urgent_keyword_baseline": f"{sum(r['keyword_urgent'] for r in rows if not r['expected_urgent'])}"
                                     f"/{len(rows) - len(urgent)}",
    "department_correct": f"{sum(r['got_dept'] == r['expected_dept'] for r in rows)}/{len(rows)}",
    "incorrect_merges": sum(len(g) > 1 for g in by_inc.values()),
    "latency_ms_median": statistics.median(lat),
    "latency_ms_max": max(lat),
    "caveat": "Smoke test on a handful of synthetic cases with draft labels; not production validation.",
}
out = {"summary": summary, "rows": rows}
(HERE / "eval_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(summary, indent=2))
