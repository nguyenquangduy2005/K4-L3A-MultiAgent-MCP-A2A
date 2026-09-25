# L3A Architecture Record

Tài liệu mô tả quyết định thiết kế có thể kiểm chứng. Không ghi prompt bí mật hay chain-of-thought.

## 1. System overview

Orchestration dùng **LangGraph** (`StateGraph`). Mỗi agent là một node; handoff là edge; state chung là `CaseState` (TypedDict) với reducer `messages` (append) và `signals` (merge). Evidence nằm trong `EvidenceStore` riêng của từng case, không nằm trong state.

```text
inputs/<case_id>.json
        │
        ▼
 ┌─────────────┐  task_assigned (fan-out song song)
 │ Coordinator │───────────┬───────────────┬───────────────┐
 └─────────────┘           ▼               ▼               ▼
                    ┌────────────┐  ┌─────────────┐  ┌──────────────┐
                    │ Order/Item │  │   Payment   │  │   Shipment   │
                    └─────┬──────┘  └──────┬──────┘  └──────┬───────┘
                          └── MCP Evidence Collector (EvidenceGateway) ──┘
                                           │ handoff (join)
                                           ▼
                                    ┌─────────────┐
                                    │ Policy Agent│  policy_decided
                                    └──────┬──────┘
                                           │ handoff
                                           ▼
                                    ┌─────────────┐  fail (≤1 lần) ─► Coordinator
                                    │  Verifier   │───────────────────────────┘
                                    └──────┬──────┘
                                           │ verification_completed
                                           ▼
                                  Output (validate schema) ─► case_finalized
```

Luồng LangGraph:

```text
START → coordinator → [order_item ‖ payment ‖ shipment] → policy → verifier
verifier ──(pass)──► END
verifier ──(fail, replans < 1)──► coordinator
verifier ──(fail, hết lượt)──► END với output fallback an toàn
```

`solve_case()` trong [workflow.py](src/student_agent/workflow.py) build graph một lần, gọi `ainvoke` cho mỗi case với `Runtime` và `EvidenceStore` mới, rồi trả về `state["output"]`. Nhờ vậy state và evidence không dùng chung giữa các case. Nếu graph ném exception hoặc quá 180 s, `solve_case` trả output fallback.

## 2. Agent ownership và tool permissions

Quyền gọi tool được cưỡng chế bằng allowlist trong [evidence.py](src/student_agent/evidence.py) (`PERMISSIONS`), không dựa vào prompt; mỗi envelope còn được kiểm tra `domain` khớp với tool đã gọi. Tên tool lấy từ `day09 mcp-tools`.

| Actor | Input | Trách nhiệm | Tool được phép | Output/handoff |
| --- | --- | --- | --- | --- |
| Coordinator | `case` JSON | Lấy `claimed_order_id`, giao việc; claim của khách chỉ là giả thuyết | Không gọi MCP | `task_assigned` → 3 specialist |
| Order/Item (`order-item-agent`) | order_id | Trạng thái đơn, item, seller | `get_order`, `get_order_items`, `get_sellers` | tín hiệu `order` + evidence_refs |
| Payment (`payment-agent`) | order_id | Capture, trùng phí, split payment, reconciliation, refund | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | tín hiệu `payment`, `refund` |
| Shipment (`shipment-agent`) | order_id | Giao trễ thật (so timestamp) và bên chịu trách nhiệm | `get_shipment_summary` | tín hiệu `shipment` |
| Policy (`policy-agent`) | 3 nhóm tín hiệu | Chọn `primary_issue`, lấy status/action/refund/bên chịu trách nhiệm từ policy | `get_policy` | `draft_output`, `policy_decided` |
| Verifier (`verifier-agent`) | draft + evidence | Kiểm tra bất biến (mục 6) | Không gọi MCP | output hoặc replan/fallback |

Không dùng `get_customer_history` / `get_product_context`: không cần cho bất kỳ `primary_issue` nào và evidence không liên quan có thể bị phạt precision.

Cách chọn `primary_issue` (xác định, không dùng LLM): mỗi detector chỉ bật khi evidence chứng minh được (ví dụ event `delivered_late` chỉ tính nếu `delivered_customer_at > estimated_delivery_at`, nên event gây nhiễu bị loại và ghi vào `data_conflicts`). Nếu claim của khách nằm trong tập issue được evidence hỗ trợ thì chọn claim đó; nếu không thì chọn theo thứ tự ưu tiên; nếu không detector nào bật thì `unsupported_claim`. `party_id` của seller lấy từ evidence của chính case, không lấy từ policy vì policy chứa seller mẫu của case khác.

**Kế hoạch evidence theo claim (giảm số MCP call):** Coordinator lập kế hoạch từ claim của khách (`CLAIM_PLAN` trong [rules.py](src/student_agent/rules.py)) và chỉ giao việc cho specialist có việc. Ví dụ `canceled_order_paid` cần `get_order`, `get_order_items`, `get_payment_timeline`; `late_delivery_*` cần `get_order`, `get_order_items`, `get_shipment_summary`; `refund_*` thêm `get_refund_timeline`; `unsupported_claim` kiểm tra mọi domain. Cộng `get_policy`, tổng khoảng 4,4 call mỗi case (440 call cho 100 case, thay vì 800). Nếu evidence được lấy không xác nhận claim, Policy phát `decision_code=widen_evidence`, Verifier yêu cầu replan và Coordinator mở rộng sang `FULL_PLAN` (đủ mọi domain) trước khi kết luận, nên vẫn không kết luận `unsupported_claim` từ bằng chứng thiếu. Đánh đổi: kế hoạch theo claim không phát hiện issue thứ hai khi claim đã được xác nhận, vì vậy confidence ở đường này là 0.9 thay vì 0.95.

Quy tắc bổ sung: hệ thống không dùng LLM; số tiền, `primary_issue` và `evidence_refs` đều tính bằng code từ dữ liệu MCP.

## 3. A2A protocol

Message envelope giữa các node (lưu trong state, tóm tắt vào trace):

```json
{
  "from": "coordinator",
  "to": "specialists",
  "kind": "task | result",
  "case_id": "<case_id>",
  "payload": {}
}
```

- **Correlation**: mọi message và trace event mang `case_id`; `evidence_ref` chỉ hợp lệ trong `CaseState` của chính case đó.
- **Điều kiện handoff**: Coordinator → specialist khi có plan; specialist → Policy khi cả ba đã trả kết quả (join của LangGraph); Policy → Verifier khi có `draft_output`.
- **Tránh vòng lặp**: đồ thị là DAG cộng một back-edge duy nhất Verifier → Coordinator, giới hạn `replan_count ≤ 1`; đặt thêm `recursion_limit` cho graph.
- **Timeout**: mỗi MCP call bọc `asyncio.wait_for` 60 s; mỗi case có ngân sách 180 s (`CASE_TIMEOUT_SECONDS`).
- **Trace** (chỉ sự kiện quan sát được, `attributes` không chứa nội dung suy luận):

| Sự kiện | Actor | Khi nào |
| --- | --- | --- |
| `case_received` | coordinator | CLI, đã có sẵn |
| `task_assigned` | coordinator → `target` | mỗi specialist |
| `tool_result_consumed` | specialist | sau mỗi MCP call dùng được, kèm `tool_name`, `evidence_refs` |
| `handoff` | specialist → policy → verifier | mỗi lần chuyển giao |
| `policy_decided` | policy | `decision_code` = `primary_issue` |
| `verification_completed` | verifier | `decision_code` = `pass` / `replan` / `fallback` |
| `case_finalized` | coordinator | CLI, đã có sẵn |

## 4. Evidence lifecycle

1. `EvidenceCollector` gọi `gateway.call(tool, case_id=..., **args)`. Gateway đã validate envelope theo `mcp-evidence-response-v1`.
2. Kiểm tra bổ sung: `domain` của envelope nằm trong allowlist của agent gọi.
3. Lưu ref vào `EvidenceStore.refs` và cache theo `(tool, args)` trong phạm vi case để không gọi trùng.
4. Specialist emit `tool_result_consumed` cho mỗi evidence lấy được và dùng để rút tín hiệu.
5. Output trích dẫn **toàn bộ** evidence mà các specialist đã lấy thành công cho case (`EVIDENCE_ORDER` trong [rules.py](src/student_agent/rules.py)), lấy nguyên ref từ MCP, không sửa hay tự tạo. Nhóm evidence bắt buộc của scorer là riêng tư nên trích đầy đủ để tránh hard gate `missing_required_evidence`, đổi lại precision của điểm `evidence` có thể giảm. `day09 recite` áp dụng cùng quy tắc offline cho output đã có bằng cách đọc các ref trong trace, không gọi MCP.
6. Verifier kiểm tra mọi ref được trích dẫn đều có trong `EvidenceStore` của đúng case.
7. `EvidenceStore` tạo mới cho mỗi case, nên evidence không bị tái sử dụng giữa các case.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout / lỗi kết nối | Có: tối đa 2 lần, backoff 1 s → 2 s, chỉ cho call đọc (idempotent) | Sau khi hết lượt, đánh dấu domain là thiếu | `attributes` của `handoff findings_ready` (`unavailable`) |
| Tool trả lỗi (vd. `get_refund_timeline` khi đơn không có refund event) | Không | Coi là không có dữ liệu cho domain đó; thiếu evidence bắt buộc thì fallback | `attributes` của `handoff findings_ready` (vd. `refunds=tool_error`) |
| Mất kết nối MCP giữa chừng | Không retry trong session hỏng (`gateway.broken`) | CLI mở session mới (tối đa 10 lần), xóa trace dở của case rồi chạy lại case đó | stderr `reconnecting MCP` |
| Source conflict (event `delivered_late` mâu thuẫn timestamp) | Không | Timestamp của order thắng; ghi vào `data_conflicts` (`TIMESTAMPS_OVERRIDE_EVENT`) | có trong output |
| Envelope không hợp lệ | Không | Bỏ evidence đó | `attributes` của `handoff findings_ready` (`invalid`) |
| Verifier không đạt | Replan 1 lần | Output an toàn: `insufficient_evidence`, `needs_investigation`, refund 0, confidence thấp, chỉ evidence thật đã thu thập | `verification_completed` `decision_code=fallback` |
| Một case ném exception | Không | Fallback như trên; các case khác vẫn chạy (bắt lỗi theo từng case) | `verification_completed` `decision_code=fallback` |

Retry luôn có giới hạn và idempotent (chỉ tool đọc). Missing evidence không bao giờ được chuyển thành dữ liệu phỏng đoán.

## 6. Verification invariants

Verifier kiểm tra trước khi finalize:

1. **Schema**: `Contracts.validate_output` với `l3a-output-v2`.
2. **Entity scope**: mọi `order_ids`, `item_ids`, `seller_ids`, `payment_references`, `shipment_ids` xuất hiện trong evidence của case.
3. **Evidence ownership**: mọi `evidence_ref` (trong output và `claim_assessments`) thuộc `EvidenceStore` của đúng case.
4. **Claim linkage**: mỗi claim có verdict khác `insufficient_evidence` phải có ít nhất một `evidence_ref`.
5. **Tổng tiền**: `recommended_refund_brl == sum(refund_lines.amount_brl)` (dùng `Decimal`) và không vượt tổng đã capture trong evidence.
6. **Nhất quán chéo** (`check_against_policy`): `case_status`, `resolution_actions`, `recommended_refund_brl` và tập `party_type` phải khớp đúng rule của `primary_issue` trong policy; party seller phải có `party_id` thuộc seller trong evidence, party khác phải có `party_id` null (nên seller-late không thể kèm logistics). Ngoài ra `no_action` ⇒ refund 0 và không có refund line.
7. **Confidence**: mức cơ sở theo đường quyết định (0.95 khi chỉ một issue được hỗ trợ và đã kiểm tra mọi domain, 0.9 khi claim được xác nhận bằng evidence theo kế hoạch, 0.85 khi còn tín hiệu gây nhiễu, 0.9 cho `unsupported_claim` sạch, 0.7 khi phải chọn theo thứ tự ưu tiên, 0.4 ở fallback), trừ 0.05 cho mỗi conflict **chưa giải quyết** (hiện: số tiền của policy không xuất hiện trong evidence của case), trần 0.95, không bao giờ 1.0. Conflict được giải quyết xác định (timestamp thắng event `delivered_late`) không bị trừ. Điểm calibration là `1 − (đúng − confidence)²` nên không hạ tay khi phán quyết đã rõ.
8. **Trace**: `case_received` và `case_finalized` do CLI phát; các event còn lại do các agent phát. Lỗi tool được ghi trong `attributes` của `handoff findings_ready`, không tạo handoff riêng, để chuỗi `case_received → task_assigned → tool_result_consumed → handoff → policy_decided → verification_completed → case_finalized` luôn đúng thứ tự. `day09 validate` báo lỗi nếu thiếu event bắt buộc hoặc sai event đầu/cuối, và cảnh báo nếu phần giữa sai thứ tự.

## 7. Reproducibility

- Orchestration: `langgraph>=1.2,<2` (đã đạt v1.2.12 khi phát triển); nên pin phiên bản chính xác khi chốt bài nộp.
- Logic quyết định (`primary_issue`, số tiền, actions) là rule-based, xác định; không dùng seed ngẫu nhiên. Hiện không dùng LLM nên không có model/temperature cần ghi.
- Concurrency: các case chạy tuần tự trên một session MCP (khoảng 3 phút cho 100 case); trong một case, 3 specialist chạy song song.
- Chạy: `pip install -e ".[dev]"` → `day09 validate-inputs` → `day09 run` → `day09 validate` → `day09 package --output dist/submission.zip`.
- Không lưu API key trong tài liệu, output hay trace (đã có bộ quét `sk-team-...` khi validate).
