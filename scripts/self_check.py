"""Tự kiểm tra outputs/ và traces/trace.jsonl trước khi đóng gói.

Kiểm tra consistency, liên kết evidence-trace và thứ tự lifecycle. Nếu có
`.local_mcp/` (chạy với server local) thì kiểm tra thêm provenance theo audit log
và so kết quả với nhãn kỳ vọng.

Dùng:
    python scripts/self_check.py
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_EVENTS = {
    "case_received",
    "task_assigned",
    "handoff",
    "verification_completed",
    "case_finalized",
}


def main() -> None:
    case_ids = json.loads((ROOT / "case-set.json").read_text("utf-8"))["case_ids"]
    outputs = {
        cid: json.loads((ROOT / "outputs" / f"{cid}.json").read_text("utf-8")) for cid in case_ids
    }
    events = defaultdict(list)
    for line in (ROOT / "traces" / "trace.jsonl").read_text("utf-8").splitlines():
        if line.strip():
            event = json.loads(line)
            events[event["case_id"]].append(event)

    problems: list[str] = []
    for cid, out in outputs.items():
        status = out["assessment"]["case_status"]
        refund = out["financial_resolution"]["recommended_refund_brl"]
        lines = out["financial_resolution"]["refund_lines"]
        if abs(refund - round(sum(x["amount_brl"] for x in lines), 2)) > 0.01:
            problems.append(f"{cid}: refund total != sum(refund_lines)")
        actions = out["resolution_actions"]
        if status == "no_action" and (refund > 0 or actions != ["no_action_required"]):
            problems.append(f"{cid}: no_action nhưng có refund/action")
        for party in out["root_cause_analysis"]["responsible_parties"]:
            if party["party_type"] == "seller" and not party["party_id"]:
                problems.append(f"{cid}: seller chịu trách nhiệm nhưng thiếu party_id")
        if not out["evidence_refs"]:
            problems.append(f"{cid}: không có evidence ref")

        case_events = events.get(cid, [])
        types = [e["event_type"] for e in case_events]
        if missing := REQUIRED_EVENTS - set(types):
            problems.append(f"{cid}: thiếu event {sorted(missing)}")
        if types.count("case_received") != 1 or types.count("case_finalized") != 1:
            problems.append(f"{cid}: case_received/case_finalized không đúng 1 lần")
        elif types[0] != "case_received" or types[-1] != "case_finalized":
            problems.append(f"{cid}: thứ tự receive/finalize sai")
        consumed = {
            ref
            for e in case_events
            if e["event_type"] == "tool_result_consumed"
            for ref in e.get("evidence_refs", [])
        }
        cited = set(out["evidence_refs"]) | {
            r for c in out.get("claim_assessments", []) for r in c["evidence_refs"]
        }
        if cited - consumed:
            problems.append(f"{cid}: ref được cite nhưng không có tool_result_consumed")

    audit_path = ROOT / ".local_mcp" / "audit.jsonl"
    labels_path = ROOT / ".local_mcp" / "expected_labels.json"
    if audit_path.exists():
        audit = {
            row["evidence_ref"]: row["case_id"]
            for row in map(json.loads, audit_path.read_text("utf-8").splitlines())
        }
        for cid, out in outputs.items():
            for ref in out["evidence_refs"]:
                if audit.get(ref) != cid:
                    problems.append(f"{cid}: ref {ref} không có trong audit của case")

    issues = Counter(o["assessment"]["primary_issue"] for o in outputs.values())
    print("Phân bố primary_issue:", dict(issues))
    if labels_path.exists():
        labels = json.loads(labels_path.read_text("utf-8"))
        issue_ok = refund_ok = 0
        for cid, out in outputs.items():
            label = labels[cid]
            if out["assessment"]["primary_issue"] == label["primary_issue"]:
                issue_ok += 1
            else:
                got_issue = out["assessment"]["primary_issue"]
                problems.append(f"{cid}: issue {got_issue} != kỳ vọng {label['primary_issue']}")
            got = out["financial_resolution"]["recommended_refund_brl"]
            if abs(got - label["recommended_refund_brl"]) <= 0.01:
                refund_ok += 1
            else:
                problems.append(f"{cid}: refund {got} != kỳ vọng {label['recommended_refund_brl']}")
            if label.get("late_seller_id") and label["late_seller_id"] not in out[
                "affected_entities"
            ]["seller_ids"]:
                problems.append(f"{cid}: thiếu seller giao trễ {label['late_seller_id']}")
        total = len(outputs)
        print(f"Đúng primary_issue: {issue_ok}/{total}; đúng refund: {refund_ok}/{total}")

    print(f"Số vấn đề: {len(problems)}")
    for problem in problems[:50]:
        print(" -", problem)
    raise SystemExit(1 if problems else 0)


if __name__ == "__main__":
    main()
